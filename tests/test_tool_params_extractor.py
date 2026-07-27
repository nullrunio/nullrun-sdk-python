"""Phase 1 / MVP 1.1 -- SDK e2e for the ToolParameters path.

These tests pin the wire shape produced by ``@sensitive`` when
paired with ``ToolParamsExtractor`` (the Tier 2 / Razryv 2
follow-up to ``MoneyImpactExtractor``). Mirrors the structure of
``test_sensitive_extractor.py`` so a reader who knows one file
knows the other.

What this file covers:
- ``tool_params()`` factory: include_all default, explicit map,
  mutual-exclusion guard
- ``ToolParamsExtractor.impact_for``: three extraction modes
  (explicit map, include_all, empty)
- Auto-attach on bare ``@sensitive`` (no impact=...) ships
  ``kind: "tool_call"`` with all kwargs as ``params``
- Auto-attach does NOT overwrite an explicit
  ``@sensitive(impact=money_outflow(...))``
- PII-masked sentinels (``"***"``) are filtered out
- Unsupported types (float) cause fail-CLOSED at extraction time
- Wire payload contains ``business_impact`` (ToolCall shape) +
  ``action_digest`` (byte-identical to the SDK's own computation)

The tests use ``_enforce_sensitive_tool`` directly rather than
``@sensitive`` decoration when checking the extraction layer in
isolation, and ``@sensitive`` decoration when checking the
auto-attach wiring. Both paths are exercised; the second is
the production-critical one.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from nullrun._registry import get_registry
from nullrun.business_impact import (
    KIND_TOOL_CALL,
    BusinessImpact,
    TOOL_PARAMETERS_MAX_PARAM_NAME,
    ToolCallParams,
    compute_action_digest,
)
from nullrun.decorators import (
    _do_sensitive_register,
    _enforce_sensitive_tool,
    _find_extractor_in_chain,
    _stamp_extractor_on_innermost,
)
from nullrun.extractor import (
    MoneyImpactExtractor,
    ToolParamsExtractor,
    money_outflow,
    tool_params,
)
from nullrun.runtime import NullRunRuntime


# ---------------------------------------------------------------------------
# Wire payload capture (same pattern as test_sensitive_extractor.py)
# ---------------------------------------------------------------------------


class _PayloadCapture:
    """Trampoline that records the most recent kwargs to
    ``runtime._transport.execute`` and returns a synthetic "allow"
    decision.
    """

    def __init__(self) -> None:
        self.last_kwargs: dict[str, Any] | None = None

    def __call__(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
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
    singleton, and rebind ``_transport.execute`` to a recorder.
    """
    NullRunRuntime.reset_instance()
    rt = NullRunRuntime(api_key="nr_test_tier2", _test_mode=True)
    cap = _PayloadCapture()
    monkeypatch.setattr(rt._transport, "execute", cap)
    get_registry().set(rt)
    yield rt
    get_registry().clear()
    NullRunRuntime.reset_instance()


@pytest.fixture
def captured_payload(captured_runtime) -> _PayloadCapture:
    return captured_runtime._transport.execute  # type: ignore[attr-defined,return-value]


def _register_tool(rt: NullRunRuntime, fn: Any) -> Any:
    """Manual registration helper -- mirrors what @sensitive does
    at decoration time but without paying the runtime-singleton
    init cost on every test. Used for the extraction-layer tests.
    """
    rt.add_sensitive_tool(fn.__name__)
    return fn


# ---------------------------------------------------------------------------
# 1. Factory tests (no runtime, no extraction -- just constructor shape)
# ---------------------------------------------------------------------------


class TestToolParamsFactory:
    def test_default_is_include_all_true(self) -> None:
        """``tool_params()`` with no args must capture every kwarg.

        Bare ``@sensitive`` auto-attaches this default; operators
        adopting ToolParameters Approval Rules need the tool to
        ship its args without rewriting every decorator site.
        """
        e = tool_params()
        assert e.param_extractors is None
        assert e.include_all is True
        assert e.extractor_id == "nullrun.tool_call.path"
        assert e.extractor_version == "1"

    def test_explicit_map_overrides_include_all(self) -> None:
        """Explicit ``{rule_param: arg_name}`` map wins over
        ``include_all``. Operators use this when the rule name
        diverges from the function arg name.
        """
        e = tool_params({"user_id": "uid"})
        assert e.param_extractors == {"user_id": "uid"}
        # ``include_all`` is irrelevant when param_extractors is
        # set; the extractor ignores it.
        assert e.include_all is True

    def test_mutual_exclusion_raises_at_construct_time(self) -> None:
        """Setting both ``param_extractors`` and
        ``include_all=False`` is almost certainly a typo. Fail
        at decorator-application time rather than silently
        dropping rules at run time.
        """
        with pytest.raises(ValueError) as exc_info:
            tool_params({"a": "b"}, include_all=False)
        assert "mutually exclusive" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 2. Extraction tests (runtime fixture, no @sensitive decorator)
# ---------------------------------------------------------------------------


class TestToolParamsExtraction:
    def test_include_all_true_captures_every_kwarg(
        self, captured_payload: _PayloadCapture, captured_runtime: NullRunRuntime
    ) -> None:
        """``include_all=True`` (default): every kwarg lands on the
        wire under its own name, regardless of how many args the
        function has or what their types are.
        """
        def delete_user(user_id: int, force: bool = False) -> None:
            pass

        ext = tool_params(include_all=True)
        fn = delete_user
        fn._nullrun_extractor = ext
        _register_tool(captured_runtime, fn)

        _enforce_sensitive_tool(
            captured_runtime, fn, (), {"user_id": 42, "force": True}
        )

        kwargs = captured_payload.last_kwargs
        assert kwargs is not None
        assert "business_impact" in kwargs
        assert "action_digest" in kwargs

        impact = kwargs["business_impact"]
        assert impact["kind"] == KIND_TOOL_CALL
        assert impact["tool_name"] == "delete_user"
        assert impact["params"] == {"user_id": 42, "force": True}
        assert impact["extractor_id"] == "nullrun.tool_call.path"
        assert impact["extractor_version"] == "1"

    def test_explicit_map_only_captures_listed_kwargs(
        self, captured_payload: _PayloadCapture, captured_runtime: NullRunRuntime
    ) -> None:
        """``{rule_param: arg_name}`` map: only the listed args are
        captured; everything else is dropped. The rule param name
        (key) and the function arg name (value) may differ.
        """
        def delete_user(uid: int, force: bool = False) -> None:
            pass

        ext = tool_params({"user_id": "uid", "force": "force"})
        fn = delete_user
        fn._nullrun_extractor = ext
        _register_tool(captured_runtime, fn)

        _enforce_sensitive_tool(
            captured_runtime, fn, (), {"uid": 42, "force": True, "extra": "ignored"}
        )

        impact = captured_payload.last_kwargs["business_impact"]
        # Only the mapped keys land on the wire, with the rule's
        # chosen name (user_id, force -- not "uid" or "extra").
        assert impact["params"] == {"user_id": 42, "force": True}
        assert "extra" not in impact["params"]
        assert "uid" not in impact["params"]

    def test_include_all_false_with_no_map_yields_empty_params(
        self, captured_payload: _PayloadCapture, captured_runtime: NullRunRuntime
    ) -> None:
        """``include_all=False`` and no ``param_extractors``: the
        wire shape is ``kind: tool_call`` with ``params: {}``.
        Rare, but the documented "empty args" path -- a tool with
        no args is still eligible for ToolCall-kind Approval
        Rules.
        """
        def list_accounts() -> None:
            pass

        # Constructor rejects (param_extractors=None, include_all=False),
        # so we build the extractor directly.
        ext = ToolParamsExtractor(include_all=False)
        fn = list_accounts
        fn._nullrun_extractor = ext
        _register_tool(captured_runtime, fn)

        _enforce_sensitive_tool(captured_runtime, fn, (), {})

        impact = captured_payload.last_kwargs["business_impact"]
        assert impact["kind"] == KIND_TOOL_CALL
        assert impact["tool_name"] == "list_accounts"
        assert impact["params"] == {}

    def test_pii_masked_sentinel_is_dropped(
        self, captured_payload: _PayloadCapture, captured_runtime: NullRunRuntime
    ) -> None:
        """PII-masked values (literal ``"***"`` string) are
        filtered out before the wire. The operator would never
        see the real value, so shipping the sentinel would never
        match a real rule -- it's dead weight on the wire.

        The decorator wrapper masks PAN/password values to
        ``"***"`` via ``_safe_kwargs`` BEFORE the extractor sees
        them; this test simulates that pre-masked state.
        """
        def charge_card(pan: str, amount: int) -> None:
            pass

        ext = tool_params(include_all=True)
        fn = charge_card
        fn._nullrun_extractor = ext
        _register_tool(captured_runtime, fn)

        # pan was masked by the decorator's _safe_kwargs layer
        # before reaching the extractor.
        _enforce_sensitive_tool(
            captured_runtime, fn, (), {"pan": "***", "amount": 5000}
        )

        impact = captured_payload.last_kwargs["business_impact"]
        assert "pan" not in impact["params"], (
            "PII-masked sentinel '***' leaked to the wire; "
            "operators would see a placeholder they cannot match"
        )
        # amount is non-PII and survives masking, so it must be on
        # the wire.
        assert impact["params"]["amount"] == 5000

    def test_float_arg_is_silently_dropped(
        self, captured_payload: _PayloadCapture, captured_runtime: NullRunRuntime
    ) -> None:
        """Phase 1 filter-not-block: a kwarg whose type is not
        JSON-roundtrippable (``float``) is silently dropped from
        the wire payload rather than failing the pre-check.

        Why filter rather than fail: a function with a mixed
        signature (``set_rate(rate: float, count: int)``) is
        still useful -- the ``count`` arg is wire-safe and should
        reach the operator. A wholesale block would force every
        user with a single ``float`` kwarg to migrate to ``str``
        just to keep their other args matched against rules.

        The strict-mode alternative (explicit ``param_extractors``
        listing only the JSON-safe keys) is documented but
        opt-in: bare ``@sensitive`` drops ``float`` silently.
        """
        def set_rate(rate: float, count: int) -> None:
            pass

        ext = tool_params(include_all=True)
        fn = set_rate
        fn._nullrun_extractor = ext
        _register_tool(captured_runtime, fn)

        # Must NOT raise: float is filtered, int is captured.
        _enforce_sensitive_tool(
            captured_runtime, fn, (), {"rate": 1.5, "count": 7}
        )

        impact = captured_payload.last_kwargs["business_impact"]
        assert "rate" not in impact["params"], (
            "float value leaked to the wire despite _safe_for_wire filter"
        )
        assert impact["params"]["count"] == 7, (
            "JSON-safe kwarg was incorrectly filtered alongside the float"
        )

    def test_unsupported_type_is_silently_filtered_in_explicit_map(
        self, captured_payload: _PayloadCapture, captured_runtime: NullRunRuntime
    ) -> None:
        """``param_extractors`` mode: an explicit {rule: arg}
        map whose arg value is JSON-unsafe (``float``) drops
        that specific arg silently rather than failing the
        whole pre-check. The other args still ship.

        Why filter rather than fail: the explicit map is a
        one-to-one rename between function-arg and rule-param.
        If a particular rename pair turns out to be
        wire-incompatible, the operator can rename the rule
        param (``{rate_str: "rate"}``) and stringify the value
        before calling the tool. Failing the whole call would
        punish the JSON-safe args.
        """
        def set_rate(rate: float, count: int) -> None:
            pass

        ext = tool_params({"rate": "rate", "count": "count"})
        fn = set_rate
        fn._nullrun_extractor = ext
        _register_tool(captured_runtime, fn)

        _enforce_sensitive_tool(
            captured_runtime, fn, (), {"rate": 1.5, "count": 7}
        )

        impact = captured_payload.last_kwargs["business_impact"]
        # rate was filtered; count survived.
        assert "rate" not in impact["params"]
        assert impact["params"]["count"] == 7

    def test_action_digest_matches_sdk_computation(
        self, captured_payload: _PayloadCapture, captured_runtime: NullRunRuntime
    ) -> None:
        """The wire ``action_digest`` MUST equal the SDK's own
        computation byte-for-byte. Otherwise the backend's
        digest-bound approval row rejects every legitimate
        post-approval re-check.
        """
        def delete_user(user_id: int, force: bool = False) -> None:
            pass

        ext = tool_params(include_all=True)
        fn = delete_user
        fn._nullrun_extractor = ext
        _register_tool(captured_runtime, fn)

        _enforce_sensitive_tool(
            captured_runtime, fn, (), {"user_id": 42, "force": True}
        )

        kwargs = captured_payload.last_kwargs
        impact = BusinessImpact.tool_call(
            tool_name="delete_user",
            params={"user_id": 42, "force": True},
        )
        expected_digest = compute_action_digest(impact)
        assert kwargs["action_digest"] == expected_digest


# ---------------------------------------------------------------------------
# 3. Auto-attach tests (the production-critical wiring)
# ---------------------------------------------------------------------------


class TestAutoAttachOnBareSensitive:
    """Verify ``_do_sensitive_register`` stamps a
    ``ToolParamsExtractor(include_all=True)`` on every bare
    ``@sensitive`` tool that doesn't already carry an explicit
    extractor.

    This is the behavior the user asked for: ``@sensitive``
    (no impact=...) is sufficient -- no extra ``@protect``
    decorator change, no extra decorator argument required.
    """

    def test_bare_function_gets_toolparams_extractor(
        self, captured_payload: _PayloadCapture, captured_runtime: NullRunRuntime
    ) -> None:
        """After ``_do_sensitive_register`` runs, a bare function
        carries ``_nullrun_extractor = ToolParamsExtractor(...)``,
        and the wire payload carries ``kind: tool_call`` with
        all kwargs as ``params``.
        """
        def delete_user(user_id: int, force: bool = False) -> None:
            pass

        # No pre-existing extractor.
        assert getattr(delete_user, "_nullrun_extractor", None) is None

        # Run the registration path that ``@sensitive`` would
        # take at decoration time.
        _do_sensitive_register(delete_user)

        ext = getattr(delete_user, "_nullrun_extractor")
        assert isinstance(ext, ToolParamsExtractor), (
            f"bare @sensitive should auto-attach ToolParamsExtractor, "
            f"got {type(ext).__name__}"
        )
        assert ext.include_all is True
        assert ext.param_extractors is None

        # Verify the wire payload uses the auto-attached extractor.
        _enforce_sensitive_tool(
            captured_runtime, delete_user, (), {"user_id": 42, "force": True}
        )
        impact = captured_payload.last_kwargs["business_impact"]
        assert impact["kind"] == KIND_TOOL_CALL
        assert impact["params"] == {"user_id": 42, "force": True}

    def test_explicit_money_extractor_is_not_overwritten(
        self, captured_payload: _PayloadCapture, captured_runtime: NullRunRuntime
    ) -> None:
        """``@sensitive(impact=money_outflow(...))`` already
        stamps ``MoneyImpactExtractor``. The auto-attach path
        MUST NOT overwrite the explicit extractor with the
        default ``ToolParamsExtractor``. Money semantics win.
        """
        def refund_customer(amount_cents: int, customer_id: str = "c-1") -> None:
            pass

        # Stamp the explicit money extractor (what
        # ``@sensitive(impact=money_outflow(...))`` does at
        # decoration time).
        money_ext = money_outflow(argument="amount_cents")
        _stamp_extractor_on_innermost(refund_customer, money_ext)

        # Run the registration path -- must NOT replace
        # money_ext with a ToolParamsExtractor.
        _do_sensitive_register(refund_customer)

        ext = getattr(refund_customer, "_nullrun_extractor")
        assert ext is money_ext, (
            "explicit money extractor was overwritten by "
            "auto-attach -- this would silently regress the "
            "MoneyImpact contract"
        )
        assert isinstance(ext, MoneyImpactExtractor)

        # Verify the wire payload still uses money semantics.
        _enforce_sensitive_tool(
            captured_runtime, refund_customer, (5000,), {"customer_id": "c-1"}
        )
        impact = captured_payload.last_kwargs["business_impact"]
        assert impact["kind"] == "money"
        assert impact["amount_minor"] == 5000
        assert impact["currency"] == "USD"


# ---------------------------------------------------------------------------
# 4. Pydantic-style shape pin (pure unit, no runtime)
# ---------------------------------------------------------------------------


class TestToolCallParamsShape:
    """Pin the SDK-side ``ToolCallParams`` shape against the backend
    ``BusinessImpact::ToolCall(ToolCallParams)`` contract at
    ``backend/src/proxy/gate/business_impact.rs:62-307``.

    Drift here is a P0 -- the backend would reject every
    ToolCall-kind impact on the wire.
    """

    def test_to_wire_dict_shape(self) -> None:
        p = ToolCallParams(
            tool_name="delete_user",
            params={"user_id": 42, "force": True},
        )
        d = p.to_wire_dict()
        assert d == {
            "kind": KIND_TOOL_CALL,
            "tool_name": "delete_user",
            "params": {"user_id": 42, "force": True},
            "extractor_id": "nullrun.tool_call.path",
            "extractor_version": "1",
        }

    def test_validate_rejects_empty_tool_name(self) -> None:
        p = ToolCallParams(tool_name="", params={})
        with pytest.raises(ValueError) as exc_info:
            p.validate()
        assert "non-empty" in str(exc_info.value)

    def test_validate_rejects_overlong_tool_name(self) -> None:
        p = ToolCallParams(tool_name="x" * 129, params={})
        with pytest.raises(ValueError) as exc_info:
            p.validate()
        assert "exceeds max 128" in str(exc_info.value)

    def test_validate_rejects_overlong_param_name(self) -> None:
        long_key = "x" * (TOOL_PARAMETERS_MAX_PARAM_NAME + 1)
        p = ToolCallParams(
            tool_name="x", params={long_key: 1}
        )
        with pytest.raises(ValueError) as exc_info:
            p.validate()
        assert "key length" in str(exc_info.value)

    def test_validate_rejects_float_param_value(self) -> None:
        p = ToolCallParams(tool_name="x", params={"rate": 1.5})
        with pytest.raises(ValueError) as exc_info:
            p.validate()
        assert "float" in str(exc_info.value)

    def test_validate_accepts_all_json_kinds(self) -> None:
        # Boundary check: every JSON kind (null/bool/int/str/
        # list/dict) survives validate. Recursive validation
        # reaches nested structures.
        p = ToolCallParams(
            tool_name="x",
            params={
                "a": None,
                "b": True,
                "c": 42,
                "d": "hello",
                "e": [1, 2, "three", {"nested": True}],
                "f": {"deep": {"deeper": [None, False]}},
            },
        )
        # Must not raise.
        p.validate()

    def test_business_impact_kind_dispatch(self) -> None:
        """``BusinessImpact.kind`` discriminates Money vs ToolCall.
        A round-trip through ``to_wire_dict`` must preserve the
        kind discriminator so the backend's
        ``serde(tag = "kind")`` picks the right variant.
        """
        m = BusinessImpact.money("outflow", 1000, "USD")
        assert m.kind == "money"
        assert m.to_wire_dict()["kind"] == "money"

        t = BusinessImpact.tool_call(
            tool_name="x", params={"y": 1}
        )
        assert t.kind == KIND_TOOL_CALL
        assert t.to_wire_dict()["kind"] == KIND_TOOL_CALL


# ---------------------------------------------------------------------------
# 5. Regression: explicit-extractor-vs-auto-attach priority (Phase 1 / MVP 1.1)
# ---------------------------------------------------------------------------
#
# Bug found via ad-hoc verification after the initial auto-attach
# commit (40d391a): the auto-attach path called
# ``getattr(fn, "_nullrun_extractor", None)`` on the @protect
# wrapper. The explicit extractor (set by ``@sensitive(impact=...)``
# factory form) lives on the BARE function -- the wrapper does NOT
# carry the attribute -- so the check returned None and the
# auto-attach path silently overwrote the user's explicit map with
# ``ToolParamsExtractor(include_all=True)``. Result: ``impact=
# tool_params({"delete_force": "force"})`` looked like it took
# effect at decoration time but the wire payload used the default
# ``{delete_force: <anything>}`` mapping -- silent param-drop.
#
# The fix walks the ``__wrapped__`` chain in
# ``_do_sensitive_register``. The regression tests below pin both
# the bare case and the explicit-map case.


class TestAutoAttachChainWalk:
    """Pin the ``_do_sensitive_register`` chain walk so future
    decorator reordering does not silently regress the
    explicit-extractor priority.
    """

    def test_bare_sensitive_chain_walk_attaches_default(
        self, captured_runtime: NullRunRuntime
    ) -> None:
        """Bare ``@sensitive`` (no impact=...) walks the chain
        and finds NO extractor, so auto-attach stamps the default.
        """
        def tool_fn(user_id: int) -> None:
            pass

        assert _find_extractor_in_chain(tool_fn) is None, (
            "sanity: bare function should not have an extractor"
        )

        _do_sensitive_register(tool_fn)

        ext = _find_extractor_in_chain(tool_fn)
        assert ext is not None
        assert isinstance(ext, ToolParamsExtractor)
        assert ext.include_all is True

    def test_explicit_tool_params_chain_walk_preserves_map(
        self, captured_runtime: NullRunRuntime
    ) -> None:
        """``@sensitive(impact=tool_params({...}))`` stamped on the
        bare function MUST survive ``_do_sensitive_register``.

        Pre-fix this silently overwrote the explicit extractor
        with the auto-attach default ``ToolParamsExtractor(
        include_all=True)`` because ``getattr(wrapper,
        "_nullrun_extractor", None)`` returned None even though
        the bare function carried the attribute.
        """
        def tool_fn(force: bool) -> None:
            pass

        # Simulate the @sensitive(impact=tool_params({...}))
        # factory form stamping the explicit extractor on the
        # bare function via ``_stamp_extractor_on_innermost``.
        explicit = tool_params({"delete_force": "force"})
        _stamp_extractor_on_innermost(tool_fn, explicit)

        # Now run the registration path -- the auto-attach MUST
        # see the explicit extractor and skip the default.
        _do_sensitive_register(tool_fn)

        ext = _find_extractor_in_chain(tool_fn)
        assert ext is explicit, (
            "explicit tool_params map was overwritten by "
            "auto-attach default -- this is the regression fixed "
            "in the chain-walk patch"
        )
        assert ext.param_extractors == {"delete_force": "force"}
        assert ext.include_all is True

    def test_explicit_money_outflow_chain_walk_preserved(
        self, captured_runtime: NullRunRuntime
    ) -> None:
        """``@sensitive(impact=money_outflow(...))`` also survives
        the auto-attach path (the original Phase 1 / MVP 1.0
        money variant must NOT be overwritten by the Tier 2
        auto-attach).
        """
        def tool_fn(amount_cents: int) -> None:
            pass

        explicit = money_outflow(argument="amount_cents")
        _stamp_extractor_on_innermost(tool_fn, explicit)

        _do_sensitive_register(tool_fn)

        ext = _find_extractor_in_chain(tool_fn)
        assert isinstance(ext, MoneyImpactExtractor), (
            f"explicit MoneyImpactExtractor was overwritten by "
            f"auto-attach default; got {type(ext).__name__}"
        )

    def test_chain_walk_does_not_loop_on_circular_wraps(
        self, captured_runtime: NullRunRuntime
    ) -> None:
        """Defensive: a pathological ``__wrapped__`` cycle must
        not hang ``_find_extractor_in_chain``. We construct a
        3-call cycle and verify the walk returns None within
        the bounded hop count.
        """
        # Build a self-referential __wrapped__ cycle.
        class Cycle:
            def __init__(self) -> None:
                self._attr = "marker"

        a = Cycle()
        a.__wrapped__ = a  # direct self-cycle
        # The walk must return None and not hang.
        assert _find_extractor_in_chain(a) is None
        # And a longer cycle: a -> b -> a -> b ...
        b = Cycle()
        a.__wrapped__ = b
        b.__wrapped__ = a
        assert _find_extractor_in_chain(a) is None
