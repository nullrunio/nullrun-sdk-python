"""Pin tests for SDK 0.18.1 @protect-only public API.

Three properties pinned here, each in a single regression test:

1. ``@protect`` auto-attaches a default ``ToolParamsExtractor`` so the
   wire payload carries ``tool_name + params`` for any protected
   tool, not just those that opted in via bare ``@sensitive``.
   Explicit ``@sensitive(impact=...)`` still wins (chain walk).

2. The default extraction is bounded:
   - oversized string values get a deterministic
     ``...[truncated:N bytes]`` suffix;
   - circular references in nested dict/list structures return
     the partial walk instead of raising ``RecursionError``;
   - dropped values (float / bytes / unsupported types) get an
     aggregate DEBUG log line, never one per dropped field.

3. Bare ``@sensitive`` emits ``DeprecationWarning`` while still
   running its legacy behaviour (auto-attach + sensitive-tool
   registration). The factory form ``@sensitive(impact=...)``
   is not deprecated — it remains the explicit advanced API.
"""

from __future__ import annotations

import logging
import warnings

import pytest

import nullrun
from nullrun.decorators import protect, sensitive
from tests.conftest import BASE_URL


# Tests that touch ``@sensitive`` (which calls
# ``_do_sensitive_register`` → ``_get_or_create_runtime``) need the
# SDK to be able to construct a ``NullRunRuntime`` instance. We
# initialise once per test against the ``mock_api`` HTTP stub so
# registration does not raise ``NullRunAuthenticationError``
# before our assertions run. ``reset_runtime`` (autouse) tears the
# singleton down again after each test, so this init is cheap.
@pytest.fixture(autouse=True)
def _init_sdk(mock_api):
    nullrun.init(api_key="test-key-12345678", api_url=BASE_URL)
    yield


# ---------------------------------------------------------------------------
# Property 1 — @protect auto-attaches a default ToolParamsExtractor
# ---------------------------------------------------------------------------


def test_protect_auto_attaches_default_tool_params_extractor() -> None:
    """Bare @protect stamps a ToolParamsExtractor(include_all=True) on the fn."""

    @protect
    def refund_customer(customer_id: str, amount: int) -> str:
        return "ok"

    extractor = getattr(refund_customer, "_nullrun_extractor", None)
    assert extractor is not None, (
        "@protect must auto-attach a default ToolParamsExtractor"
    )
    assert extractor.include_all is True, (
        "default @protect extractor must capture every kwarg"
    )
    assert extractor.param_extractors is None, (
        "default @protect extractor must use include_all mode, not a map"
    )
    # 0.18.1: the auto-attached extractor must be tagged so the
    # policy gate can skip the /execute round-trip for bare
    # @protect (latency split — bare @protect is cheap, explicit
    # @sensitive(impact=...) is policy-gated).
    assert getattr(extractor, "_nullrun_auto_attached", False) is True, (
        "auto-attached extractor must carry the _nullrun_auto_attached "
        "marker so _enforce_sensitive_tool can distinguish it from "
        "explicit @sensitive(impact=...) extractors"
    )


def test_protect_does_not_overwrite_explicit_sensitive_impact_extractor() -> None:
    """@protect chain walk preserves an explicit @sensitive(impact=...) extractor."""

    from nullrun.extractor import money_outflow

    # Order: @protect inner, @sensitive(impact=...) outer.
    # Decorators apply bottom-up, so:
    #   1. @protect wraps explicit_money → sync_wrapper1.
    #      At this point NO extractor exists in the chain; my new
    #      @protect auto-attach stamps ToolParamsExtractor on the
    #      bare fn. Then @sensitive(impact=...) factory runs and
    #      stamps MoneyImpactExtractor on the same bare fn (overwriting
    #      the ToolParamsExtractor), THEN registers the tool.
    #   2. The end-state on the bare fn is MoneyImpactExtractor.
    @protect
    @sensitive(impact=money_outflow(argument="amount_cents", currency="USD", units="minor"))
    def explicit_money(amount_cents: int) -> str:
        return "ok"

    extractor = getattr(explicit_money, "_nullrun_extractor", None)
    assert extractor is not None
    # money_outflow returns a MoneyImpactExtractor, not a ToolParamsExtractor;
    # we identify it by class name to avoid importing the concrete class here.
    assert type(extractor).__name__ == "MoneyImpactExtractor", (
        "@protect chain walk must preserve an explicit extractor; got "
        f"{type(extractor).__name__!r}"
    )


# ---------------------------------------------------------------------------
# Property 2 — bounded extraction
# ---------------------------------------------------------------------------


def test_oversize_string_value_is_truncated_with_marker() -> None:
    """String values over 1024 bytes get a deterministic truncation suffix."""

    @protect
    def upload(description: str) -> str:
        return "ok"

    big = "x" * 5000
    # Reach into the extractor the way the gate would, by calling
    # ``impact_for`` directly so we don't have to spin up a runtime.
    extractor = upload._nullrun_extractor
    impact = extractor.impact_for(upload, (), {"description": big})
    params = impact.to_wire_dict()["params"]
    assert "description" in params
    value = params["description"]
    assert value.endswith(" bytes]"), (
        f"truncated value must end with the marker; got tail={value[-30:]!r}"
    )
    assert "...[truncated:" in value
    # The returned string must be bounded (marker is sized so the
    # result never exceeds the cap, including the marker itself).
    assert len(value.encode("utf-8")) <= 1024, (
        f"truncated value must be <= 1024 bytes; got {len(value.encode('utf-8'))}"
    )


def test_circular_reference_in_nested_dict_does_not_recurse_infinitely() -> None:
    """A self-referential dict returns the partial walk, not RecursionError."""

    @protect
    def process(config: dict) -> str:
        return "ok"

    cyclic: dict = {"outer": "value"}
    cyclic["self"] = cyclic  # type: ignore[assignment]

    extractor = process._nullrun_extractor
    # Should NOT raise RecursionError. The bound walk stops on cycle.
    impact = extractor.impact_for(process, (), {"config": cyclic})
    params = impact.to_wire_dict()["params"]
    assert "config" in params
    # The outer key survived; the cycle marker is the partial walk.
    inner = params["config"]
    assert "outer" in inner
    # The recursive key was bounded to a partial structure.
    assert isinstance(inner["self"], dict)


def test_dropped_values_emit_aggregate_debug_log_not_per_field(caplog) -> None:
    """Dropped values (float, bytes, custom) get ONE aggregate DEBUG line."""

    class Custom:
        def __repr__(self) -> str:
            return "Custom()"

    @protect
    def mixed(a: float, b: bytes, c: object) -> str:
        return "ok"

    extractor = mixed._nullrun_extractor
    with caplog.at_level(logging.DEBUG, logger="nullrun.extractor"):
        impact = extractor.impact_for(
            mixed,
            (),
            {"a": 1.5, "b": b"hello", "c": Custom()},
        )
    params = impact.to_wire_dict()["params"]
    assert "a" not in params and "b" not in params and "c" not in params, (
        f"unsupported types must be dropped; got params={params!r}"
    )
    debug_lines = [
        r for r in caplog.records if r.name == "nullrun.extractor"
    ]
    assert len(debug_lines) == 1, (
        f"expected exactly one aggregate DEBUG line, got {len(debug_lines)}"
    )
    msg = debug_lines[0].getMessage()
    assert "3" in msg, f"aggregate line must report count=3; got {msg!r}"
    assert "float" in msg and "bytes" in msg and "Custom" in msg, (
        f"aggregate line must list type names; got {msg!r}"
    )
    # Argument names ("a", "b", "c") are NEVER logged as standalone
    # tokens. Substring matches against type names like "bytes" are
    # acceptable (the line format is "type_name=count" with a space
    # delimiter, so a literal arg name like "a" would never appear
    # unaccompanied). We assert on the structured format instead.
    tokens = set(msg.replace("=", " ").replace(",", " ").split())
    assert "a" not in tokens and "b" not in tokens and "c" not in tokens, (
        f"log line tokens must NOT contain argument names; got tokens={tokens!r}"
    )


# ---------------------------------------------------------------------------
# Property 3 — bare @sensitive DeprecationWarning
# ---------------------------------------------------------------------------


def test_bare_sensitive_emits_deprecation_warning() -> None:
    """Bare @sensitive still works in 0.18.x but emits DeprecationWarning."""

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")

        @sensitive
        def legacy_tool(x: int) -> str:
            return "ok"

    deprecation_warnings = [
        w for w in caught if issubclass(w.category, DeprecationWarning)
    ]
    assert len(deprecation_warnings) == 1, (
        f"bare @sensitive must emit exactly one DeprecationWarning; got "
        f"{len(deprecation_warnings)}: {[str(w.message) for w in deprecation_warnings]}"
    )
    assert "0.18.1" in str(deprecation_warnings[0].message)
    # Legacy behaviour is preserved for this release.
    extractor = getattr(legacy_tool, "_nullrun_extractor", None)
    assert extractor is not None, (
        "bare @sensitive must still stamp a default extractor in 0.18.x"
    )


def test_sensitive_factory_with_explicit_impact_does_not_warn() -> None:
    """@sensitive(impact=...) is the advanced API and must NOT warn."""

    from nullrun.extractor import money_outflow

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")

        @sensitive(impact=money_outflow(argument="x", currency="USD", units="minor"))
        def advanced_tool(x: int) -> str:
            return "ok"

    deprecation_warnings = [
        w for w in caught if issubclass(w.category, DeprecationWarning)
    ]
    assert deprecation_warnings == [], (
        f"@sensitive(impact=...) must not emit DeprecationWarning; got "
        f"{[str(w.message) for w in deprecation_warnings]}"
    )


# ---------------------------------------------------------------------------
# Sanity: the auto-attach on @protect composes with bare @sensitive
# ---------------------------------------------------------------------------


def test_bare_sensitive_then_protect_does_not_double_attach() -> None:
    """@sensitive outside @protect must not double-attach an extractor."""

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)

        @sensitive
        @protect
        def composed(x: int) -> str:
            return "ok"

    extractor = getattr(composed, "_nullrun_extractor", None)
    assert extractor is not None
    # Only one ToolParamsExtractor is attached; the chain walk sees
    # the explicit one and skips auto-attach on @protect.
    assert type(extractor).__name__ == "ToolParamsExtractor"


def test_auto_attached_extractor_is_distinguished_from_explicit() -> None:
    """The auto-attached marker survives on the inner extractor; explicit impact wins."""

    from nullrun.extractor import money_outflow

    # Bare @protect → auto-attached extractor is stamped with marker.
    @protect
    def bare_protected(x: int) -> str:
        return "ok"

    bare_extractor = getattr(bare_protected, "_nullrun_extractor")
    assert getattr(bare_extractor, "_nullrun_auto_attached", False) is True

    # Explicit @sensitive(impact=...) overwrites the auto-attached
    # extractor; the new one does NOT carry the marker.
    @protect
    @sensitive(impact=money_outflow(argument="x", currency="USD", units="minor"))
    def explicit_protected(x: int) -> str:
        return "ok"

    explicit_extractor = getattr(explicit_protected, "_nullrun_extractor")
    assert getattr(explicit_extractor, "_nullrun_auto_attached", False) is False, (
        "explicit @sensitive(impact=...) must stamp an extractor WITHOUT "
        "the auto-attached marker so the policy gate fires"
    )
