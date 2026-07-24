"""Phase 1 / MVP 1.0 -- SDK e2e for the @sensitive(impact=...) path.

These tests pin the wire shape produced by the auto-wire path:
when a sensitive tool decorated with ``@sensitive(impact=...)``
is invoked through ``@protect``, the SDK sends
``business_impact`` + ``action_digest`` on the wire so the
backend can stamp the approval row with the digest and refuse
tampered payloads on the post-approval re-check.

The previous attempt (rolled back) failed because the test
fixture built a fresh ``NullRunRuntime`` instance but did NOT
register it in the ``RuntimeRegistry``. ``_get_or_create_runtime``
therefore re-created a new singleton, and the
``monkeypatch.setattr(rt, "_transport", cap)`` swap was on the
unregistered instance. The new tests use ``get_registry().set(rt)``
to wire the test runtime as the active one.
"""

from __future__ import annotations

from typing import Any

import pytest

from nullrun._registry import get_registry
from nullrun.business_impact import (
    OUTFLOW,
    BusinessImpact,
    compute_action_digest,
)
from nullrun.decorators import _enforce_sensitive_tool
from nullrun.extractor import money_outflow
from nullrun.runtime import NullRunRuntime

# ---------------------------------------------------------------------------
# Wire payload capture
# ---------------------------------------------------------------------------


class _PayloadCapture:
    """Trampoline that records the most recent kwargs to
    ``runtime._transport.execute`` and returns a synthetic "allow"
    decision.

    The recorder is bound to a freshly-built Transport instance via
    ``monkeypatch.setattr(rt, "_transport", instance)`` and the SDK
    invokes ``instance.execute(**kwargs)``. We capture the kwargs
    by overriding ``execute`` on the instance via
    ``monkeypatch.setattr(instance, "execute", self)`` in the
    fixture below — this is the pattern already used by
    ``test_execute_approval_flow.py``.
    """

    def __init__(self) -> None:
        self.last_kwargs: dict[str, Any] | None = None

    def __call__(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        # Real transport.execute takes kwargs only. We accept
        # *args for forward-compat (a future transport may pass
        # positional metadata) but pin the contract on kwargs.
        del args
        self.last_kwargs = kwargs
        return {
            "decision": "allow",
            "decision_source": "test_capture",
            "policy_version": 0,
            "allow_execution": True,
        }


@pytest.fixture
def captured_runtime(monkeypatch):
    """Build a test-mode runtime, register it as the active
    singleton in ``RuntimeRegistry``, and rebind
    ``_transport.execute`` to a recorder. Yield the runtime for
    tests to register tools on.

    We bind ``execute`` on the freshly-built transport rather
    than swapping the whole transport object: that's the pattern
    in ``test_execute_approval_flow.py`` and it works with the
    SDK's ``self._transport.execute(**kwargs)`` method call.
    """
    NullRunRuntime.reset_instance()
    rt = NullRunRuntime(api_key="nr_test_phase1", _test_mode=True)
    cap = _PayloadCapture()
    monkeypatch.setattr(rt._transport, "execute", cap)
    # Wire the test runtime as the singleton so the SDK's
    # ``_get_or_create_runtime()`` returns OUR instance (not a
    # freshly-constructed one).
    get_registry().set(rt)
    yield rt
    get_registry().clear()
    NullRunRuntime.reset_instance()


@pytest.fixture
def captured_payload(captured_runtime) -> _PayloadCapture:
    return captured_runtime._transport.execute  # type: ignore[attr-defined,return-value]


# ---------------------------------------------------------------------------
# Phase 1 typed tools: built manually instead of via the @sensitive
# decorator. The decorator wiring is exercised by
# ``test_decorator_factory_form_attaches_extractor`` below.
# ---------------------------------------------------------------------------


def _refund_customer_impl(amount_cents: int, customer_id: str = "c-1") -> dict[str, Any]:
    return {"customer": customer_id, "amount": amount_cents}


def _register_refund_tool(rt: NullRunRuntime) -> Any:
    """Bind ``_refund_customer_impl`` with the Phase 1 extractor
    and register it as a sensitive tool. Mirrors what
    ``@sensitive(impact=money_outflow(argument="amount_cents"))``
    would do at decorator-application time, but without paying
    the ``@sensitive`` registration cost on every test.
    """
    fn = _refund_customer_impl
    extractor = money_outflow(argument="amount_cents")
    setattr(fn, "_nullrun_extractor", extractor)
    rt.add_sensitive_tool(fn.__name__)
    return fn


def _register_legacy_tool(rt: NullRunRuntime) -> Any:
    """Bind a sensitive tool WITHOUT a Phase 1 extractor (legacy
    Phase 0 path). The wrapper must NOT attach business_impact
    or action_digest to the wire.
    """
    def search_docs(query: str) -> list[str]:
        return [query]

    rt.add_sensitive_tool(search_docs.__name__)
    return search_docs


# ---------------------------------------------------------------------------
# 6.4: SDK e2e -- the wire payload contains business_impact + action_digest
# ---------------------------------------------------------------------------


class TestSensitiveExtractorWirePayload:
    """Pin the wire shape produced by the Phase 1 auto-wire path.

    These tests replace the SDK's transport with a recorder and
    invoke ``_enforce_sensitive_tool`` directly. The capture
    captures the kwargs the SDK sends; the test asserts those
    kwargs match the wire shape documented in
    ``contracts/openapi.yaml`` for ``GateRequest``.
    """

    def test_refund_customer_50_dollars_sends_typed_business_impact(
        self, captured_payload: _PayloadCapture, captured_runtime: NullRunRuntime
    ) -> None:
        """Refund $50: business_impact + action_digest must land
        on the wire.
        """
        fn = _register_refund_tool(captured_runtime)

        _enforce_sensitive_tool(captured_runtime, fn, (5_000,), {"customer_id": "c-1"})

        assert captured_payload.last_kwargs is not None
        kwargs = captured_payload.last_kwargs

        # Both Phase 1 fields must be present because the function
        # has the extractor attribute set.
        assert "business_impact" in kwargs, (
            f"Phase 1 contract broken: business_impact missing from "
            f"wire kwargs: {sorted(kwargs.keys())}"
        )
        assert "action_digest" in kwargs, (
            f"Phase 1 contract broken: action_digest missing from "
            f"wire kwargs: {sorted(kwargs.keys())}"
        )

        impact = kwargs["business_impact"]
        assert impact["kind"] == "money"
        assert impact["direction"] == "outflow"
        assert impact["amount_minor"] == 5_000
        assert impact["currency"] == "USD"

        # Digest is byte-identical to the SDK's own computation.
        expected = compute_action_digest(
            BusinessImpact.money(OUTFLOW, 5_000, "USD")
        )
        assert kwargs["action_digest"] == expected

    def test_legacy_sensitive_tool_sends_no_business_impact(
        self, captured_payload: _PayloadCapture, captured_runtime: NullRunRuntime
    ) -> None:
        """Phase 0 path: tool is sensitive but has no impact
        extractor. The SDK MUST NOT attach business_impact or
        action_digest to the wire -- the backend falls back to
        approval_id-only grant consume.
        """
        fn = _register_legacy_tool(captured_runtime)

        _enforce_sensitive_tool(captured_runtime, fn, ("hello",), {})

        kwargs = captured_payload.last_kwargs
        assert kwargs is not None
        assert "business_impact" not in kwargs
        assert "action_digest" not in kwargs

    def test_extractor_rejects_unknown_argument_at_call_time(
        self, captured_runtime: NullRunRuntime
    ) -> None:
        """Phase 1 fail-CLOSED: if the extractor raises (e.g.
        argument name mismatch), the pre-check MUST fail. The
        body NEVER runs.
        """
        from nullrun.breaker.exceptions import NullRunBlockedException

        def bad_tool(amount: int) -> dict[str, Any]:
            return {"amount": amount}

        # Bind with an extractor that points to a non-existent
        # argument. This deliberately raises TypeError in the
        # extractor.
        bad_tool._nullrun_extractor = money_outflow(argument="not_an_argument")
        captured_runtime.add_sensitive_tool(bad_tool.__name__)

        with pytest.raises(NullRunBlockedException) as exc_info:
            _enforce_sensitive_tool(captured_runtime, bad_tool, (42,), {})
        assert exc_info.value.error_code == "NR-B003"
        assert "not_an_argument" in exc_info.value.reason

    def test_extractor_rejects_negative_amount(
        self, captured_runtime: NullRunRuntime
    ) -> None:
        """Phase 1 fail-CLOSED: a negative amount must NOT pass
        the pre-check. Without this, a hostile SDK caller could
        subtract their way past the rule threshold by passing a
        negative number.
        """
        from nullrun.breaker.exceptions import NullRunBlockedException

        fn = _register_refund_tool(captured_runtime)

        with pytest.raises(NullRunBlockedException) as exc_info:
            _enforce_sensitive_tool(captured_runtime, fn, (-1,), {"customer_id": "c-1"})
        assert exc_info.value.error_code == "NR-B003"
        # Phase 1.1 hardening: the negative-amount guard now
        # lives in ``_to_minor_units`` (not in
        # ``MoneyImpact.validate``), so the reason text
        # matches the new "rejected negative" message. The
        # legacy "non-negative" wording remains for
        # backward-compatible callers via
        # ``MoneyImpact.validate`` when an amount is somehow
        # negative on the wire (defense-in-depth).
        assert "rejected negative" in exc_info.value.reason

    def test_decorator_factory_form_attaches_extractor(
        self, captured_payload: _PayloadCapture, captured_runtime: NullRunRuntime
    ) -> None:
        """@sensitive(impact=money_outflow(...)) factory form:
        after the decorator applies, ``_nullrun_extractor`` is
        stamped on the function and the wire payload carries the
        typed business_impact + digest.

        This exercises the public decorator API end-to-end
        rather than setting the attribute manually.
        """
        from nullrun import sensitive

        @sensitive(impact=money_outflow(argument="amount_cents"))
        def refund(amount_cents: int, customer_id: str = "c-1") -> dict[str, Any]:
            return {"customer": customer_id, "amount": amount_cents}

        # The decorator must have stamped the extractor on the
        # wrapped function.
        assert hasattr(refund, "_nullrun_extractor"), (
            "decorator did not stamp _nullrun_extractor on the function"
        )

        # Also: the runtime must have registered the tool as
        # sensitive. ``is_sensitive_tool`` is the public predicate.
        assert captured_runtime.is_sensitive_tool(refund.__name__), (
            "decorator did not register the tool as sensitive"
        )

        _enforce_sensitive_tool(captured_runtime, refund, (5_000,), {"customer_id": "c-1"})

        kwargs = captured_payload.last_kwargs
        assert kwargs is not None
        assert "business_impact" in kwargs
        assert "action_digest" in kwargs
        assert kwargs["business_impact"]["amount_minor"] == 5_000
