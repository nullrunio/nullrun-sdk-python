"""Unit tests for the Layer-1 structured exception hierarchy.

Every public SDK exception class should:
  1. Inherit from ``NullRunError`` so a single ``except NullRunError``
     clause catches them all (with structured fields).
  2. Carry a stable ``error_code`` (e.g. ``"NR-A001"``) so users can
     grep / log / document per-code behaviour.
  3. Carry a ``user_action`` string telling the user what to do next.
  4. Set ``retryable`` correctly — ``True`` only for transient
     failures, ``False`` for configuration / permission / budget.
  5. Have a ``docs_url`` for the per-code docs page.

Back-compat invariants (do not break in Layer 1):
  A. ``except NullRunAuthenticationError`` still catches
     ``NullRunAuthError`` (subclass).
  B. ``except NullRunBlockedException`` still catches
     ``NullRunBudgetError`` and ``NullRunToolBlockedError``.
  C. ``except NullRunTransportError`` still catches
     ``NullRunBackendError`` and ``RateLimitError``.
  D. ``except WorkflowKilledException`` still catches
     ``WorkflowKilledInterrupt`` (BaseException inheritance).
  E. ``except Exception`` does NOT catch ``WorkflowKilledInterrupt``.

The tests below are the safety net for the above — a future
refactor that breaks one of them is a regression even if no other
test fails.
"""

import pytest

from nullrun.breaker.exceptions import (
    # Base
    BreakerError,
    NullRunAuthenticationError,
    NullRunAuthError,
    NullRunBackendError,
    # Block
    NullRunBlockedException,
    NullRunBudgetError,
    # Config / auth
    NullRunConfigError,
    NullRunError,
    NullRunToolBlockedError,
    # Transport
    NullRunTransportError,
    RateLimitError,
    TransportErrorSource,
    WorkflowKilledException,
    WorkflowKilledInterrupt,
    # Workflow state
    WorkflowPausedException,
)


# ---------------------------------------------------------------------------
# 1. Base class — every public exception must inherit from NullRunError
# ---------------------------------------------------------------------------
class TestHierarchyRoots:
    def test_all_exceptions_inherit_from_nullrun_error(self):
        for cls in (
            NullRunAuthenticationError,
            NullRunAuthError,
            NullRunConfigError,
            NullRunTransportError,
            NullRunBackendError,
            RateLimitError,
            NullRunBlockedException,
            NullRunBudgetError,
            NullRunToolBlockedError,
            WorkflowPausedException,
        ):
            assert issubclass(cls, NullRunError), (
                f"{cls.__name__} must inherit from NullRunError so users "
                f"can do `except NullRunError:` to catch every structured "
                f"SDK failure."
            )

    def test_killed_interrupt_does_not_inherit_from_exception(self):
        # WorkflowKilledInterrupt is a BaseException subclass by design
        # (per docs/kill-contract.md). It MUST NOT inherit from
        # NullRunError (which is an Exception subclass), so that
        # `except Exception` does not catch the kill signal.
        assert not issubclass(WorkflowKilledInterrupt, Exception)
        assert not issubclass(WorkflowKilledInterrupt, NullRunError)
        # But it MUST inherit from WorkflowKilledException (legacy
        # back-compat shim) so old `except WorkflowKilledException`
        # clauses still match.
        assert issubclass(WorkflowKilledInterrupt, WorkflowKilledException)


# ---------------------------------------------------------------------------
# 2. Structured fields — error_code, user_action, retryable, docs_url
# ---------------------------------------------------------------------------
class TestStructuredFields:
    def test_default_fields_present_on_base(self):
        exc = NullRunError("oops")
        assert exc.error_code == "NR-0000"
        assert exc.user_action == ""
        assert exc.retryable is False
        assert exc.docs_url == "https://docs.nullrun.io/errors"

    def test_per_instance_overrides(self):
        exc = NullRunError(
            "boom",
            error_code="NR-X999",
            user_action="do X",
            retryable=True,
            docs_url="https://docs/x",
        )
        assert exc.error_code == "NR-X999"
        assert exc.user_action == "do X"
        assert exc.retryable is True
        assert exc.docs_url == "https://docs/x"

    def test_subclass_class_attribute_inheritance(self):
        # NullRunBackendError is a real class with a real
        # ``error_code`` / ``user_action`` / ``retryable`` triple.
        exc = NullRunBackendError("5xx", endpoint="/api/v1/check")
        assert exc.error_code == "NR-B002"
        assert "NullRun backend" in exc.user_action
        assert exc.retryable is True

    def test_cause_chains_via_from(self):
        original = RuntimeError("underlying")
        try:
            raise NullRunError("wrapper", cause=original) from original
        except NullRunError as exc:
            assert exc.cause is original
            assert exc.__cause__ is original


# ---------------------------------------------------------------------------
# 3. Back-compat — every existing except clause must still match
# ---------------------------------------------------------------------------
class TestBackCompat:
    def test_auth_error_caught_by_authentication_error(self):
        with pytest.raises(NullRunAuthenticationError):
            raise NullRunAuthError("key rejected")

    def test_budget_error_caught_by_blocked_exception(self):
        with pytest.raises(NullRunBlockedException):
            raise NullRunBudgetError(workflow_id="wf-1", reason="budget exhausted")

    def test_tool_blocked_error_caught_by_blocked_exception(self):
        with pytest.raises(NullRunBlockedException):
            raise NullRunToolBlockedError(
                workflow_id="wf-1", reason="blocked", tool_name="send_email"
            )

    def test_backend_error_caught_by_transport_error(self):
        with pytest.raises(NullRunTransportError):
            raise NullRunBackendError("5xx", endpoint="/api/v1/check", status_code=503)

    def test_killed_interrupt_caught_by_killed_exception(self):
        # Back-compat shim — legacy `except WorkflowKilledException`
        # must still match the new interrupt subclass.
        with pytest.raises(WorkflowKilledException):
            raise WorkflowKilledInterrupt("wf-1", reason="killed via API")

    def test_killed_interrupt_not_caught_by_exception(self):
        # The whole point of BaseException inheritance: kill must
        # not be swallowable by `except Exception`.
        with pytest.raises(BaseException) as exc_info:
            raise WorkflowKilledInterrupt("wf-1", reason="killed")
        assert isinstance(exc_info.value, WorkflowKilledInterrupt)
        assert not isinstance(exc_info.value, Exception)


# ---------------------------------------------------------------------------
# 4. Specific error codes — the catalog
# ---------------------------------------------------------------------------
class TestErrorCodeCatalog:
    """Spot-checks for the most common error codes. If a future
    refactor accidentally renames a code, this test fails loudly
    with a `git grep`-friendly message."""

    def test_no_api_key_is_NR_C001(self):
        with pytest.raises(NullRunConfigError) as info:
            raise NullRunConfigError("no api_key", error_code="NR-C001")
        assert info.value.error_code == "NR-C001"

    def test_api_key_rejected_is_NR_A003(self):
        with pytest.raises(NullRunAuthError) as info:
            raise NullRunAuthError("key rejected")
        assert info.value.error_code == "NR-A003"

    def test_backend_5xx_is_NR_B002(self):
        with pytest.raises(NullRunBackendError) as info:
            raise NullRunBackendError("5xx", endpoint="/api/v1/check")
        assert info.value.error_code == "NR-B002"
        assert info.value.retryable is True

    def test_budget_exhausted_is_NR_B004(self):
        with pytest.raises(NullRunBudgetError) as info:
            raise NullRunBudgetError("wf-1", reason="budget exhausted")
        assert info.value.error_code == "NR-B004"
        assert info.value.retryable is False

    def test_tool_blocked_is_NR_T001(self):
        with pytest.raises(NullRunToolBlockedError) as info:
            raise NullRunToolBlockedError("wf-1", reason="blocked", tool_name="send_email")
        assert info.value.error_code == "NR-T001"
        assert info.value.tool_name == "send_email"

    def test_killed_is_NR_W002(self):
        with pytest.raises(WorkflowKilledInterrupt) as info:
            raise WorkflowKilledInterrupt("wf-1", reason="killed")
        # BaseException subclass so we use .value not .excinfo
        assert info.value.error_code == "NR-W002"

    def test_paused_is_NR_W003(self):
        with pytest.raises(WorkflowPausedException) as info:
            raise WorkflowPausedException("wf-1", reason="paused")
        assert info.value.error_code == "NR-W003"

    def test_rate_limit_is_NR_R001(self):
        with pytest.raises(RateLimitError) as info:
            raise RateLimitError(
                "429",
                source=TransportErrorSource.GATEWAY_ERROR,
                endpoint="/api/v1/check",
            )
        assert info.value.error_code == "NR-R001"
        assert info.value.retryable is True


# ---------------------------------------------------------------------------
# 5. Transport-error → code mapping
# ---------------------------------------------------------------------------
class TestTransportCodeMapping:
    """The transport layer classifies failures by ``TransportErrorSource``;
    each class maps to a stable ``error_code`` so cookbook code and
    Sentry rules can branch on it without parsing the message."""

    def test_network_error_maps_to_NR_B001(self):
        exc = NullRunTransportError(
            "timeout",
            source=TransportErrorSource.NETWORK_ERROR,
            endpoint="/api/v1/check",
        )
        assert exc.error_code == "NR-B001"
        assert exc.retryable is True

    def test_gateway_error_maps_to_NR_B002(self):
        exc = NullRunTransportError(
            "5xx",
            source=TransportErrorSource.GATEWAY_ERROR,
            endpoint="/api/v1/check",
        )
        assert exc.error_code == "NR-B002"

    def test_auth_error_maps_to_NR_A003(self):
        exc = NullRunTransportError(
            "401",
            source=TransportErrorSource.AUTH_ERROR,
            endpoint="/api/v1/check",
        )
        assert exc.error_code == "NR-A003"
