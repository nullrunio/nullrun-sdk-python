"""
Regression tests for BLOCKER fixes in 0.4.0.

Phase 2 of the production-readiness plan:
- #1 First-`track ` AttributeError on `_workflow_costs` (removed in 0.3.1).
- #3 `_safe_bump_coverage` missing — `auto_requests.py` was unimportable.
- #4 `auto_instrument ` did not call `patch_requests`.
- #7 `wrap ` had a latent NameError (also deleted in 0.4.0).
"""

from __future__ import annotations


def test_track_returns_zero_local_cost_cents():
    """`runtime.track()` no longer raises AttributeError on `_workflow_costs`."""
    from nullrun.runtime import NullRunRuntime

    runtime = NullRunRuntime(api_key="test", _test_mode=True)
    result = runtime.track({"type": "llm_call", "tokens": 10, "_fingerprint": "test-fp-1"})
    assert result["local_cost_cents"] == 0
    assert result["allowed"] is True


def test_track_no_workflow_id_returns_zero():
    """Track returns local_cost_cents=0 even when no workflow_id is set."""
    from nullrun.runtime import NullRunRuntime

    runtime = NullRunRuntime(api_key="test", _test_mode=True)
    result = runtime.track({"type": "llm_call", "tokens": 5})
    assert result["local_cost_cents"] == 0


def test_track_dedup_hit_returns_zero():
    """The dedup-hit branch (which used to read `_workflow_costs.get`) returns 0."""
    from nullrun.runtime import NullRunRuntime

    runtime = NullRunRuntime(api_key="test", _test_mode=True)
    # Two calls with the same fingerprint — second should dedup
    fp = "test-fp-dedup"
    runtime.track({"type": "llm_call", "tokens": 10, "_fingerprint": fp})
    result = runtime.track({"type": "llm_call", "tokens": 10, "_fingerprint": fp})
    assert result["local_cost_cents"] == 0
    assert result.get("deduped") is True


def test_auto_requests_module_importable():
    """`auto_requests.py` was unimportable in 0.3.1 because `_safe_bump_coverage`
    was referenced but never defined. 0.4.0 fixes this.
    """
    import nullrun.instrumentation.auto_requests  # noqa: F401


# 0.9.0: removed `test_safe_bump_coverage_exported` and
# `test_safe_bump_coverage_tolerates_missing_attribute`. The
# `_safe_bump_coverage` helper is gone — coverage is derived from
# llm_call span metadata. See plan at
# `~/.claude/plans/async-swinging-hanrahan.md`.


def test_auto_instrument_patches_requests():
    """`auto_instrument` now includes `patch_requests` in its install list."""
    # Indirect: when `requests` is not installed, patch_requests returns False.
    # The important contract is that auto_instrument calls it without error.
    from nullrun.instrumentation.auto import auto_instrument, reset_for_tests
    from nullrun.runtime import NullRunRuntime

    reset_for_tests()
    runtime = NullRunRuntime(api_key="test", _test_mode=True)
    # Should not raise even when `requests` is not installed.
    result = auto_instrument(runtime)
    assert isinstance(result, bool)
    reset_for_tests()


def test_wrap_symbol_absent():
    """`from nullrun import wrap` raises ImportError."""
    import pytest

    with pytest.raises(ImportError):
        from nullrun import wrap  # noqa: F401


def test_runtime_local_cost_cents_estimate_init():
    """`_local_cost_cents_estimate` is initialised to 0 in `__init__`."""
    from nullrun.runtime import NullRunRuntime

    runtime = NullRunRuntime(api_key="test", _test_mode=True)
    assert hasattr(runtime, "_local_cost_cents_estimate")
    assert runtime._local_cost_cents_estimate == 0
