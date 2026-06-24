"""Tests for the Layer 3 ``nullrun.status()`` introspection API.

The contract:

  * No runtime → ``NullRunConfigError`` with ``NR-C004``.
  * Runtime present → frozen ``NullRunStatus`` snapshot with:
      - ``state`` ∈ ``{"ok", "degraded", "offline", "misconfigured"}``
      - ``recent_errors`` is a list (possibly empty) of
        ``RecentError`` entries.
  * The recent-errors ring buffer is fed by ``_emit_sdk_error``
    (Layer 2 path). Capacity 10.
  * Status is a synchronous read-only snapshot. Calling it
    must NEVER mutate the runtime or create a new one.
  * Equality works on the frozen dataclass (``s1 == s2`` when
    every field is equal) — important for caching / diffing.
"""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

import nullrun
from nullrun.breaker.exceptions import (
    NullRunConfigError,
    NullRunError,
)
from nullrun.observability.status import (
    NullRunStatus,
    RecentError,
    WorkflowState,
    _RecentErrorRing,
)
from nullrun.runtime import NullRunRuntime


# Each test gets a fresh module-level runtime slot — Layer-3
# reads ``nullrun.runtime._runtime`` directly so we MUST
# clean up to avoid leaking state between tests.
@pytest.fixture(autouse=True)
def _reset_runtime():
    import nullrun.runtime as _rt_mod

    _rt_mod._runtime = None
    NullRunRuntime._instance = None
    yield
    _rt_mod._runtime = None
    NullRunRuntime._instance = None


def _make_runtime(api_key: str = "nr_live_test_key_1234") -> NullRunRuntime:
    """Construct a NullRunRuntime in _test_mode without going
    through ``init()`` (which would try to call the backend).
    """
    rt = NullRunRuntime(api_key=api_key, _test_mode=True)
    import nullrun.runtime as _rt_mod

    _rt_mod._runtime = rt
    NullRunRuntime._instance = rt
    return rt


# ---------------------------------------------------------------------------
# 1. No runtime
# ---------------------------------------------------------------------------
class TestNoRuntime:
    def test_status_raises_when_no_runtime(self):
        with pytest.raises(NullRunConfigError) as info:
            nullrun.status()
        err = info.value
        assert err.error_code == "NR-C004"
        assert "init" in err.user_action.lower()
        assert err.retryable is False

    def test_status_never_lazily_creates_runtime(self):
        # Sanity: calling status() must NOT trigger
        # NullRunRuntime.get_instance() (which would itself
        # raise a different config error about missing
        # api_key). The whole point of NR-C004 is a clean
        # "no runtime" signal.
        with patch("nullrun.runtime.NullRunRuntime.get_instance") as mock_get:
            with pytest.raises(NullRunConfigError):
                nullrun.status()
            mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# 2. With runtime — snapshot fields
# ---------------------------------------------------------------------------
class TestSnapshotFields:
    def test_minimal_runtime_yields_ok_state(self):
        _make_runtime()
        s = nullrun.status()
        assert s.state == "ok"
        assert s.api_key_prefix == "nr_live_te"
        assert s.is_healthy() is True

    def test_snapshot_is_frozen(self):
        _make_runtime()
        s = nullrun.status()
        with pytest.raises(Exception):  # FrozenInstanceError
            s.state = "degraded"  # type: ignore[misc]

    def test_snapshot_supports_equality(self):
        _make_runtime()
        s1 = nullrun.status()
        s2 = nullrun.status()
        assert s1 == s2

    def test_api_key_prefix_truncated_to_10_chars(self):
        _make_runtime(api_key="nr_live_SsBF9OMYcVCgRCNcCVcJ4khTOPKx79JG")
        s = nullrun.status()
        assert s.api_key_prefix == "nr_live_Ss"
        assert len(s.api_key_prefix) == 10
        # Full key MUST NOT leak into the snapshot.
        assert "TOPKx79JG" not in str(s)

    def test_backend_reachable_none_when_no_attempt(self):
        _make_runtime()
        s = nullrun.status()
        assert s.backend_reachable is None

    def test_ws_connected_none_when_no_ws_started(self):
        _make_runtime()
        s = nullrun.status()
        assert s.ws_connected is None


# ---------------------------------------------------------------------------
# 3. State derivation
# ---------------------------------------------------------------------------
class TestStateDerivation:
    def test_misconfigured_when_no_api_key(self):
        # Bypass __init__'s api_key check via _test_mode + later
        # clearing. The status builder reads ``self.api_key`` —
        # setting it to None after construction triggers the
        # misconfigured branch.
        rt = _make_runtime()
        rt.api_key = None
        s = nullrun.status()
        assert s.state == "misconfigured"
        assert s.api_key_valid is None
        assert s.api_key_prefix is None

    def test_degraded_when_using_fallback_policy(self):
        # Construct a runtime where ``_policy`` is strict_local
        # but ``_last_good_policy`` is a permissive policy —
        # this is the post-fetch-failure state.
        rt = _make_runtime()
        from nullrun.runtime import Policy

        rt._last_good_policy = Policy(budget_cents=1000, rate_limit=100)
        rt._policy = Policy.strict_local()
        rt._last_policy_fetch_failed_at = rt.api_key and 1000000000.0 or 1000000000.0
        s = nullrun.status()
        assert s.state == "degraded"
        assert s.fallback_policy is not None
        assert s.fallback_policy is not s.active_policy
        assert s.fallback_reason is not None
        assert "failed" in s.fallback_reason.lower()

    def test_ok_when_active_policy_is_healthy(self):
        rt = _make_runtime()
        from nullrun.runtime import Policy

        rt._policy = Policy(budget_cents=500, rate_limit=100)
        rt._last_good_policy = None  # no fallback in use
        s = nullrun.status()
        assert s.state == "ok"


# ---------------------------------------------------------------------------
# 4. Recent-errors ring buffer
# ---------------------------------------------------------------------------
class TestRecentErrors:
    def test_recent_errors_empty_on_fresh_runtime(self):
        _make_runtime()
        s = nullrun.status()
        assert s.recent_errors == []

    def test_recent_errors_populated_by_emit(self):
        rt = _make_runtime()
        # Simulate an error firing through the Layer-2 path.
        from nullrun.observability.error_hooks import ErrorContext

        err = NullRunError("boom", error_code="NR-X999")
        rt._emit_sdk_error(
            err,
            stage="init",
            workflow_id="wf-1",
            tool_name="send_email",
        )
        s = nullrun.status()
        assert len(s.recent_errors) == 1
        entry = s.recent_errors[0]
        assert entry.error_code == "NR-X999"
        assert entry.stage == "init"
        assert entry.workflow_id == "wf-1"
        assert entry.tool_name == "send_email"
        assert entry.message == "boom"

    def test_recent_errors_respects_capacity(self):
        # Default capacity 10 — pushing 15 should keep the last 10.
        ring = _RecentErrorRing(capacity=10)
        for i in range(15):
            ring.push(
                RecentError(
                    error_code="NR-X000",
                    stage="test",
                    workflow_id=None,
                    tool_name=None,
                    timestamp=datetime.now(tz=timezone.utc),
                    message=f"err-{i}",
                )
            )
        snap = ring.snapshot()
        assert len(snap) == 10
        # The FIRST 5 were evicted; the LAST 10 (err-5 .. err-14)
        # are present.
        assert snap[0].message == "err-5"
        assert snap[-1].message == "err-14"

    def test_recent_errors_pushed_even_with_no_hook(self):
        # Layer-3 is a no-instrumentation path: the ring
        # buffer fires even when no on_error hook is
        # registered. This is the whole point of Layer 3.
        rt = _make_runtime()
        from nullrun.observability.error_hooks import ErrorContext

        rt._emit_sdk_error(
            NullRunError("test"),
            stage="init",
        )
        # No on_error hook registered. snapshot still works.
        s = nullrun.status()
        assert len(s.recent_errors) == 1


# ---------------------------------------------------------------------------
# 5. Workflow state from cache
# ---------------------------------------------------------------------------
class TestWorkflowState:
    def test_workflow_state_none_when_no_remote_state(self):
        _make_runtime()
        s = nullrun.status()
        assert s.workflow_state is None

    def test_workflow_state_reads_from_cache(self):
        # Push a synthetic remote_state into the cache and
        # verify the status builder surfaces it.
        rt = _make_runtime()
        rt.workflow_id = "wf-test-1"
        rt._remote_state_for("wf-test-1")
        rt._set_remote_state(
            "wf-test-1",
            {"state": "Killed", "version": 5, "reason": "manual kill"},
        )
        s = nullrun.status()
        assert s.workflow_state is not None
        assert s.workflow_state.workflow_id == "wf-test-1"
        assert s.workflow_state.state == "Killed"
        assert s.workflow_state.reason == "manual kill"


# ---------------------------------------------------------------------------
# 6. summary() — human-readable one-liner
# ---------------------------------------------------------------------------
class TestSummary:
    def test_ok_summary(self):
        _make_runtime()
        s = nullrun.status()
        out = s.summary()
        assert "ok" in out
        assert "nr_live_te" in out

    def test_degraded_summary_includes_fallback(self):
        rt = _make_runtime()
        from nullrun.runtime import Policy

        rt._last_good_policy = Policy(budget_cents=1000, rate_limit=100)
        rt._policy = Policy.strict_local()
        rt._last_policy_fetch_failed_at = 1000000000.0
        s = nullrun.status()
        out = s.summary()
        assert "degraded" in out
        assert "fallback" in out or "last_good" in out


# ---------------------------------------------------------------------------
# 7. Public API surface
# ---------------------------------------------------------------------------
class TestPublicAPI:
    def test_status_in_dir(self):
        assert callable(nullrun.status)
        assert "status" in dir(nullrun)

    def test_status_in_all(self):
        import nullrun as n

        assert "status" in n.__all__

    def test_status_dataclasses_importable(self):
        # All four dataclasses reachable from the public
        # namespace for type annotations.
        from nullrun.observability import (
            NullRunStatus as NS,
        )
        from nullrun.observability import (
            RecentError as RE,
        )
        from nullrun.observability import (
            WorkflowState as WS,
        )

        assert NS is NullRunStatus
        assert RE is RecentError
        assert WS is WorkflowState
