"""Contract test: SDK 0.7.0 no longer maintains a local Policy cache.

Every enforcement decision arrives from the backend via /gate and
/api/v1/execute. This file pins that invariant so any future
regression that re-introduces a local Policy class trips the test
loudly.

Audit context (D-01, 2026-06-26): ``Policy.from_dict `` was silently
parsing backend responses and falling back to hardcoded defaults
(budget_cents=1000, rate_limit=100, loop_threshold=6) when fields
were missing. Per-org policy enforcement through the SDK was an
illusion. Removing the local class makes the SDK a true thin client
and eliminates the drift surface.
"""

from dataclasses import fields

from nullrun.observability.status import NullRunStatus
from nullrun.runtime import NullRunRuntime


def test_runtime_module_has_no_policy_class():
    """SDK 0.7.0: no local Policy class in nullrun.runtime."""
    import nullrun.runtime as rt

    assert not hasattr(rt, "Policy"), (
        "Local Policy class re-introduced — drift from thin-client model. "
        "See audit D-01 (2026-06-26)."
    )


def test_runtime_has_no_local_enforcement_attrs():
    """Internal loop/rate tracker + hardcoded thresholds removed."""
    rt = NullRunRuntime(api_key="nr_live_test", _test_mode=True)
    for attr in [
        "_policy",
        "_last_good_policy",
        "_last_policy_fetch_at",
        "_last_policy_fetch_failed_at",
        "_loop_tracker",
        "_rate_tracker",
        "_local_loop_threshold",
        "_local_rate_limit",
    ]:
        assert not hasattr(rt, attr), (
            f"{attr} re-introduced — local enforcement has been removed in 0.7.0."
        )


def test_runtime_has_no_policy_property():
    """NullRunRuntime.policy property was the public read of local policy."""
    rt = NullRunRuntime(api_key="nr_live_test", _test_mode=True)
    public_attrs = [a for a in dir(rt) if not a.startswith("_")]
    assert "policy" not in public_attrs, (
        "NullRunRuntime.policy property re-introduced — was removed in 0.7.0."
    )


def test_status_has_no_policy_fields():
    """NullRunStatus no longer exposes Policy objects."""
    field_names = {f.name for f in fields(NullRunStatus)}
    forbidden = {
        "active_policy",
        "fallback_policy",
        "fallback_reason",
        "last_policy_fetch",
        "last_policy_fetch_age_seconds",
    }
    leaked = forbidden & field_names
    assert not leaked, (
        f"NullRunStatus leaked policy fields: {leaked}. See audit D-01 — backend owns policy state."
    )


def test_loop_tracker_class_removed():
    import nullrun.runtime as rt

    for cls in ["LoopTracker", "RateTracker", "LocalDecision"]:
        assert not hasattr(rt, cls), (
            f"{cls} re-introduced — local enforcement has been removed in 0.7.0."
        )


def test_track_does_no_local_check():
    """track forwards to transport without local pre-filter.

    With local enforcement removed, the SDK does not block calls
    based on internal counters — every gate decision comes from
    the backend via /gate and /api/v1/execute.
    """
    rt = NullRunRuntime(api_key="nr_live_test", _test_mode=True)
    assert not hasattr(rt, "_local_check"), (
        "_local_check re-introduced — local enforcement has been removed in 0.7.0."
    )


def test_fetch_policy_method_removed():
    """Transport.fetch_policy was the wire-level GET /policies caller."""
    from nullrun.transport import Transport

    assert not hasattr(Transport, "fetch_policy"), (
        "Transport.fetch_policy re-introduced — SDK no longer caches local policy."
    )


def test_fallback_mode_cached_removed():
    from nullrun.transport import FallbackMode

    assert not hasattr(FallbackMode, "CACHED"), (
        "FallbackMode.CACHED re-introduced — was removed in 0.7.0 (SDK is thin client)."
    )


def test_execute_config_default_fallback_mode_is_strict():
    """v3.53 audit #4 — ExecuteConfig.fallback_mode default flipped to STRICT.

    Per CLAUDE.md §4 ("DEFAULT: fail-CLOSED для всех enforcement
    путей") the SDK must default to blocking local execution when the
    /execute endpoint is unreachable. The pre-v3.53 PERMISSIVE default
    was a silent fail-OPEN on the primary enforcement path — a body
    could run when the policy engine was unreachable without any
    explicit opt-out from the caller.

    Source-pin on ``ExecuteConfig.fallback_mode: str = FallbackMode.STRICT``
    so a future refactor that flips the default back to PERMISSIVE
    fails loudly in CI rather than silently re-introducing the
    fail-OPEN enforcement path.
    """
    from nullrun.transport import ExecuteConfig, FallbackMode

    cfg = ExecuteConfig()
    assert cfg.fallback_mode == FallbackMode.STRICT, (
        "ExecuteConfig.fallback_mode default flipped back to PERMISSIVE — "
        "v3.53 audit #4 closure REGRESSED. /execute is the primary "
        "enforcement path per transport.py docstring; the default "
        "must be fail-CLOSED (STRICT) per CLAUDE.md §4."
    )


def test_transport_execute_kwarg_default_fallback_mode_is_strict():
    """v3.53 audit #4 — Transport.execute() fallback_mode kwarg default is STRICT.

    Pin on the kwarg signature so a future refactor that flips the
    default back to PERMISSIVE breaks here. ``Transport.execute`` is
    the primary /api/v1/execute caller — see transport.py docstring
    lines 1022-1024 ("PRIMARY enforcement point").
    """
    import inspect

    from nullrun.transport import FallbackMode, Transport

    sig = inspect.signature(Transport.execute)
    param = sig.parameters["fallback_mode"]
    assert param.default == FallbackMode.STRICT, (
        "Transport.execute() fallback_mode kwarg default flipped back to "
        "PERMISSIVE — v3.53 audit #4 closure REGRESSED. The /execute "
        "enforcement path must default to fail-CLOSED per CLAUDE.md §4."
    )


def test_runtime_init_default_fallback_mode_is_strict():
    """v3.53 audit #4 — NullRunRuntime(fallback_mode=None) lands on STRICT.

    Pre-v3.53 ``None`` / unset silently mapped to PERMISSIVE. Now
    None → STRICT (fail-CLOSED). Only an explicit
    ``fallback_mode="permissive"`` / ``"PERMISSIVE"`` opt-in flips
    to the legacy fail-OPEN path.
    """
    from nullrun.breaker.exceptions import BreakerTransportError
    from nullrun.transport import FallbackMode

    import nullrun

    # Build a runtime with ``_test_mode=True`` so the constructor
    # short-circuits auth + WS plumbing — we only care about the
    # default of ``_fallback_mode``.
    rt = nullrun.NullRunRuntime(
        api_key="nr_test_dummy_for_v3_53_source_pin",
        _test_mode=True,
        polling=False,
    )
    assert rt._fallback_mode == FallbackMode.STRICT, (
        "NullRunRuntime(fallback_mode=None) default flipped back to "
        "PERMISSIVE — v3.53 audit #4 closure REGRESSED. The default "
        "must be STRICT per CLAUDE.md §4."
    )

    # Suppress unused-import lint; BreakerTransportError referenced
    # so a future code path change that introduces a new import here
    # triggers a name-resolution check.
    del BreakerTransportError


def test_runtime_init_permissive_kwarg_still_opt_in():
    """v3.53 audit #4 — explicit fallback_mode="permissive" still opt-in.

    The legacy behavior must remain reachable for dev / test harnesses
    that intentionally run without a live policy engine. This test
    pins the opt-in path so a future refactor that "removes the
    deprecated kwarg" doesn't break CI workflows that depend on it.
    """
    import nullrun
    from nullrun.transport import FallbackMode

    rt = nullrun.NullRunRuntime(
        api_key="nr_test_dummy_for_v3_53_source_pin",
        _test_mode=True,
        polling=False,
        fallback_mode="permissive",
    )
    assert rt._fallback_mode == FallbackMode.PERMISSIVE


def test_runtime_init_has_no_policy_kwarg():
    """NullRunRuntime(policy=...) kwarg was removed in 0.7.0."""
    import inspect

    sig = inspect.signature(NullRunRuntime.__init__)
    assert "policy" not in sig.parameters, (
        "NullRunRuntime(policy=...) kwarg re-introduced — was removed in 0.7.0."
    )


def test_policy_cache_classes_removed():
    """CachedDecision / PolicyCache were tied to the deleted CACHED fallback mode."""
    from nullrun import transport as t

    assert not hasattr(t, "CachedDecision"), (
        "CachedDecision re-introduced — was removed in 0.7.0 (no local cache)."
    )
    assert not hasattr(t, "PolicyCache"), (
        "PolicyCache re-introduced — was removed in 0.7.0 (no local cache)."
    )


def test_transport_has_no_clear_policy_cache():
    """Transport.clear_policy_cache is gone — there is nothing to clear."""
    from nullrun.transport import Transport

    assert not hasattr(Transport, "clear_policy_cache"), (
        "Transport.clear_policy_cache re-introduced — was removed in 0.7.0."
    )
