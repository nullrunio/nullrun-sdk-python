"""
Regression tests for Phase 8 release polish.

Phase 8:
- #8.1: get_org_status() public method on NullRunRuntime.
- #8.4: NULLRUN_BATCH_SIZE / NULLRUN_FLUSH_INTERVAL_MS env vars.
- #8.6: RecordingSession does not persist _fingerprint.
- Circuit-breaker sleep capped at 5s.
"""
from __future__ import annotations

import pytest

# ===========================================================================
# 8.1: get_org_status
# ===========================================================================

def test_get_org_status_requires_org_id():
    """get_org_status raises NullRunAuthenticationError when no org_id and runtime has none."""
    import pytest

    from nullrun.breaker.exceptions import NullRunAuthenticationError
    from nullrun.runtime import NullRunRuntime

    runtime = NullRunRuntime(api_key="test", _test_mode=True)
    # organization_id is None until _authenticate runs; get_org_status
    # should refuse to send a request.
    with pytest.raises(NullRunAuthenticationError):
        runtime.get_org_status()


def test_get_org_status_calls_endpoint(monkeypatch):
    """get_org_status routes through transport._client and parses JSON."""
    from nullrun.runtime import NullRunRuntime

    runtime = NullRunRuntime(api_key="test", _test_mode=True)
    runtime.organization_id = "org-1"

    seen = []

    class FakeResponse:
        status_code = 200
        def json(self):
            return {"usage_today_cents": 1234, "plan": "growth"}
        def raise_for_status(self):
            pass

    class FakeClient:
        def get(self, url, headers=None, timeout=None):
            seen.append((url, headers, timeout))
            return FakeResponse()

    runtime._transport._client = FakeClient()
    body = runtime.get_org_status()
    assert body == {"usage_today_cents": 1234, "plan": "growth"}
    assert len(seen) == 1
    assert "/api/v1/orgs/org-1/status" in seen[0][0]


# ===========================================================================
# 8.4: env vars
# ===========================================================================

def test_batch_size_env_override(monkeypatch):
    """NULLRUN_BATCH_SIZE overrides FlushConfig.batch_size."""
    from nullrun.transport import Transport

    monkeypatch.setenv("NULLRUN_BATCH_SIZE", "200")
    t = Transport(api_url="https://api.test.com", api_key="test")
    assert t.config.batch_size == 200


def test_flush_interval_env_override(monkeypatch):
    """NULLRUN_FLUSH_INTERVAL_MS overrides FlushConfig.flush_interval."""
    from nullrun.transport import Transport

    monkeypatch.setenv("NULLRUN_FLUSH_INTERVAL_MS", "1000")
    t = Transport(api_url="https://api.test.com", api_key="test")
    assert t.config.flush_interval == 1.0


def test_batch_size_env_invalid_ignored(monkeypatch):
    """Non-int NULLRUN_BATCH_SIZE is logged + ignored (not crash)."""
    from nullrun.transport import Transport

    monkeypatch.setenv("NULLRUN_BATCH_SIZE", "not-a-number")
    # Should not raise.
    t = Transport(api_url="https://api.test.com", api_key="test")
    # Defaults to FlushConfig default (50).
    assert t.config.batch_size == 50


# ===========================================================================
# 8.6: _fingerprint not persisted
# ===========================================================================
# Sprint 2.1: the local decision-history recorder was deleted (the
# feature moved to the backend dashboard; the SDK does not store
# request/response payloads). The ``start_recording`` / ``stop_recording``
# methods on ``NullRunRuntime`` are kept as no-op stubs for one minor
# version. This test pins the no-op contract so a future regression
# that re-introduces a working recorder (or a hard failure) breaks
# here, not in a production call-site.


def test_start_stop_recording_are_noop_stubs():
    """``start_recording`` returns "" and ``stop_recording`` returns None.

    Pre-Sprint-2.1 these returned a ``RecordingSession`` /
    ``session_id`` and persisted events to disk. The recorder
    itself was deleted, so the methods are now no-op stubs. This
    test pins the new contract.
    """
    from nullrun.runtime import NullRunRuntime

    runtime = NullRunRuntime(api_key="test", _test_mode=True)
    session_id = runtime.start_recording("wf-test")
    assert session_id == "", (
        f"start_recording() must return '' as a no-op stub; got {session_id!r}"
    )

    session = runtime.stop_recording()
    assert session is None, (
        f"stop_recording() must return None as a no-op stub; got {session!r}"
    )


def test_decision_history_module_does_not_exist():
    """The ``nullrun.decision_history`` module was deleted in 0.4.0.

    Any code that still does ``from nullrun.decision_history import X``
    must fail at import time, not silently get a different module.
    """
    import importlib
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("nullrun.decision_history")


# ===========================================================================
# Circuit-breaker sleep cap
# ===========================================================================

def test_open_to_halfopen_sleep_capped_at_5s():
    """The OPEN -> HALF_OPEN jitter sleep is bounded by 5.0s.

    We pin the cap by reading the source of the jitter helpers
    — §7.2 #35 split the cap into ``_maybe_apply_open_jitter_sync``
    and ``_maybe_apply_open_jitter_async`` so async callers can
    await instead of blocking the event loop. The cap itself
    stays at 5.0s in both branches.
    """
    import inspect

    from nullrun.breaker import circuit_breaker

    sync_src = inspect.getsource(circuit_breaker.CircuitBreaker._maybe_apply_open_jitter_sync)
    async_src = inspect.getsource(circuit_breaker.CircuitBreaker._maybe_apply_open_jitter_async)
    assert "random.uniform(0, 5.0)" in sync_src
    assert "random.uniform(0, 5.0)" in async_src
    assert "random.uniform(0, 30.0)" not in sync_src
    assert "random.uniform(0, 30.0)" not in async_src