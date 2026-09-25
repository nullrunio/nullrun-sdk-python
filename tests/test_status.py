"""Tests for the Layer 3 ``runtime.status()`` introspection API.

The contract:

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

History
-------
In 0.18.4 the top-level ``nullrun.status()`` wrapper was removed.
Tests now drive the runtime directly (``rt.status()``) rather than
going through the deleted wrapper. The wrapper existed only to
render an ``NR-C004`` config error before ``init``; that path is
covered by runtime's own self-consistency checks below.
"""

from datetime import datetime, timezone

import pytest

import nullrun
from nullrun.breaker.exceptions import NullRunError
from nullrun.observability.status import (
    NullRunStatus,
    RecentError,
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
    through ``init`` (which would try to call the backend).
    """
    rt = NullRunRuntime(api_key=api_key, _test_mode=True)
    import nullrun.runtime as _rt_mod

    _rt_mod._runtime = rt
    NullRunRuntime._instance = rt
    return rt


# ---------------------------------------------------------------------------
# 1. With runtime — snapshot fields
# ---------------------------------------------------------------------------
class TestSnapshotFields:
    def test_minimal_runtime_yields_ok_state(self):
        rt = _make_runtime()
        s = rt.status()
        assert s.state == "ok"
        assert s.api_key_prefix == "nr_live_te"
        assert s.is_healthy() is True

    def test_snapshot_is_frozen(self):
        rt = _make_runtime()
        s = rt.status()
        with pytest.raises(Exception):  # FrozenInstanceError
            s.state = "degraded"  # type: ignore[misc]

    def test_snapshot_supports_equality(self):
        rt = _make_runtime()
        s1 = rt.status()
        s2 = rt.status()
        assert s1 == s2

    def test_api_key_prefix_truncated_to_10_chars(self):
        _make_runtime(api_key="nr_live_SsBF9OMYcVCgRCNcCVcJ4khTOPKx79JG")
        s = _make_runtime(
            api_key="nr_live_SsBF9OMYcVCgRCNcCVcJ4khTOPKx79JG"
        ).status()
        assert s.api_key_prefix == "nr_live_Ss"
        assert len(s.api_key_prefix) == 10
        # Full key MUST NOT leak into the snapshot.
        assert "TOPKx79JG" not in str(s)

    def test_backend_reachable_none_when_no_attempt(self):
        s = _make_runtime().status()
        assert s.backend_reachable is None

    def test_ws_connected_none_when_no_ws_started(self):
        s = _make_runtime().status()
        assert s.ws_connected is None


# ---------------------------------------------------------------------------
# 2. State derivation
# ---------------------------------------------------------------------------
class TestStateDerivation:
    def test_misconfigured_when_no_api_key(self):
        # Bypass __init__'s api_key check via _test_mode + later
        # clearing. The status builder reads ``self.api_key`` —
        # setting it to None after construction triggers the
        # misconfigured branch.
        rt = _make_runtime()
        rt.api_key = None
        s = rt.status()
        assert s.state == "misconfigured"
        assert s.api_key_valid is None
        assert s.api_key_prefix is None


# ---------------------------------------------------------------------------
# 3. Recent-errors ring buffer
# ---------------------------------------------------------------------------
class TestRecentErrors:
    def test_recent_errors_empty_on_fresh_runtime(self):
        s = _make_runtime().status()
        assert s.recent_errors == []

    def test_recent_errors_populated_by_emit(self):
        rt = _make_runtime()
        # Simulate an error firing through the Layer-2 path.
        err = NullRunError("boom", error_code="NR-X999")
        rt._emit_sdk_error(
            err,
            stage="init",
            workflow_id="wf-1",
            tool_name="send_email",
        )
        s = rt.status()
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
        # The FIRST 5 were evicted; the LAST 10 (err-5.. err-14)
        # are present.
        assert snap[0].message == "err-5"
        assert snap[-1].message == "err-14"

    def test_recent_errors_pushed_even_with_no_hook(self):
        # Layer-3 is a no-instrumentation path: the ring
        # buffer fires even when no on_error hook is
        # registered. This is the whole point of Layer 3.
        rt = _make_runtime()
        rt._emit_sdk_error(
            NullRunError("test"),
            stage="init",
        )
        # No on_error hook registered. snapshot still works.
        s = rt.status()
        assert len(s.recent_errors) == 1


# ---------------------------------------------------------------------------
# 4. Workflow state from cache
# ---------------------------------------------------------------------------
class TestWorkflowState:
    def test_workflow_state_none_when_no_remote_state(self):
        s = _make_runtime().status()
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
        s = rt.status()
        assert s.workflow_state is not None
        assert s.workflow_state.workflow_id == "wf-test-1"
        assert s.workflow_state.state == "Killed"
        assert s.workflow_state.reason == "manual kill"


# ---------------------------------------------------------------------------
# 5. summary — human-readable one-liner
# ---------------------------------------------------------------------------
class TestSummary:
    def test_ok_summary(self):
        out = _make_runtime().status().summary()
        assert "ok" in out
        assert "nr_live_te" in out

    def test_summary_with_organization_and_workflow(self):
        # Covers the ``if self.organization_id`` and
        # ``if self.workflow_id`` branches of summary.
        rt = _make_runtime()
        rt.organization_id = "org_abcdef1234567890"
        rt.workflow_id = "wf_xyzzy1234567890"
        out = rt.status().summary()
        assert "org=org_abcd" in out
        assert "wf=wf_xyzzy" in out

    def test_summary_includes_workflow_state_when_not_normal(self):
        # Branch: ``self.workflow_state and.state != "Normal"``.
        rt = _make_runtime()
        rt.workflow_id = "wf-test-1"
        rt._set_remote_state(
            "wf-test-1",
            {"state": "Killed", "version": 5, "reason": "manual kill"},
        )
        out = rt.status().summary()
        assert "wf_state=Killed" in out

    def test_summary_omits_normal_workflow_state(self):
        # Sanity: a Normal workflow state should NOT appear in summary.
        rt = _make_runtime()
        rt.workflow_id = "wf-test-1"
        rt._set_remote_state(
            "wf-test-1",
            {"state": "Normal", "version": 1, "reason": None},
        )
        out = rt.status().summary()
        assert "wf_state=" not in out

    def test_summary_includes_backend_unreachable(self):
        # Branch: ``self.backend_reachable is False``.
        # ``backend_reachable`` is a local in ``status``, not a stored
        # attribute on the runtime — construct the snapshot directly.
        s = NullRunStatus(
            state="degraded",
            api_key_valid=True,
            api_key_prefix="nr_live_te",
            organization_id=None,
            workflow_id=None,
            api_url="https://api.nullrun.io",
            backend_reachable=False,
            ws_connected=None,
            workflow_state=None,
            recent_errors=[],
        )
        assert "backend=unreachable" in s.summary()

    def test_summary_includes_ws_disconnected(self):
        # Branch: ``self.ws_connected is False``. Same reasoning as above.
        s = NullRunStatus(
            state="degraded",
            api_key_valid=True,
            api_key_prefix="nr_live_te",
            organization_id=None,
            workflow_id=None,
            api_url="https://api.nullrun.io",
            backend_reachable=None,
            ws_connected=False,
            workflow_state=None,
            recent_errors=[],
        )
        assert "ws=False" in s.summary()

    def test_summary_includes_recent_errors_count(self):
        # Branch: ``if self.recent_errors``.
        rt = _make_runtime()
        for i in range(3):
            rt._emit_sdk_error(
                NullRunError(f"err-{i}", error_code="NR-X000"),
                stage="init",
            )
        out = rt.status().summary()
        assert "errors=3" in out


# ---------------------------------------------------------------------------
# 6. Public API surface regression guards (0.18.4)
# ---------------------------------------------------------------------------
class TestStatusRemovedFromTopLevel:
    """0.18.4 removed the top-level ``nullrun.status()`` wrapper.

    These tests pin that removal against a future re-add. The
    runtime method ``NullRunRuntime.status()`` is the only
    public status entry point now — callers reach it via
    ``nullrun.get_runtime().status()`` or by holding a runtime
    reference they constructed themselves.
    """

    def test_status_not_in_dir(self):
        # ``status`` is no longer a curated surface entry — the
        # runtime method is reached via ``nullrun.get_runtime()``
        # rather than via a top-level wrapper.
        assert "status" not in dir(nullrun)
        assert not callable(getattr(nullrun, "status", None))

    def test_status_not_in_all(self):
        import nullrun as n

        assert "status" not in n.__all__
