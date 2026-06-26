"""Contract test: SDK 0.7.0 no longer maintains a local Policy cache.

Every enforcement decision arrives from the backend via /gate and
/api/v1/execute. This file pins that invariant so any future
regression that re-introduces a local Policy class trips the test
loudly.

Audit context (D-01, 2026-06-26): ``Policy.from_dict()`` was silently
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
    """track() forwards to transport without local pre-filter.

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
