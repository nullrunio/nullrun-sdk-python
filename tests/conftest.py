"""
conftest.py - shared pytest fixtures and respx mocking
"""

import os

import pytest
import respx
from httpx import Response

# Base URL used in tests
BASE_URL = "https://api.test.nullrun.io"


@pytest.fixture(autouse=True)
def reset_runtime():
    """Reset all singletons before each test (not after - avoids double-flush issues)."""
    import nullrun.actions as _act
    import nullrun.decorators as _dec
    import nullrun.runtime as _rt_mod
    from nullrun.context import _call_model_var, _call_tools_var
    from nullrun.runtime import NullRunRuntime

    NullRunRuntime.reset_instance()
    _dec._runtime = None
    _act._action_handler = None
    # Module-level cache used by `nullrun.track_llm` / `nullrun.track_tool` →
    # `get_runtime`. Without this, a stale singleton from a previous test
    # leaks across the suite.
    _rt_mod._runtime = None
    _call_model_var.set(None)
    _call_tools_var.set(())

    yield

    # Stop any running transport flush thread BEFORE dropping the reference.
    # Without this the thread keeps running across tests, the buffer drains
    # through httpx with no respx context active, and the worker logs a
    # ConnectError retry storm for the rest of the xdist session.
    # flush=False skips the final _do_flush / _persist_to_wal so the teardown
    # is a true no-op even when the buffer still has events.
    inst = NullRunRuntime._instance
    if inst is not None:
        try:
            inst.shutdown(flush=False)
        except Exception:
            pass
    NullRunRuntime._instance = None
    _dec._runtime = None
    _act._action_handler = None
    _rt_mod._runtime = None
    _call_model_var.set(None)
    _call_tools_var.set(())


@pytest.fixture
def mock_api():
    """Mock all HTTP calls to NullRun API."""
    with respx.mock:
        # Auth endpoint
        respx.post(f"{BASE_URL}/api/v1/auth/verify").mock(
            return_value=Response(
                200,
                json={
                    "organization_id": "ws-test",
                    "workflow_id": "00000000-0000-0000-0000-000000000001",
                    "plan": "pro",
                    "features": [],
                    "limits": {"max_cost_cents": 10000},
                },
            )
        )
        # Gate (execute) endpoint
        respx.post(f"{BASE_URL}/api/v1/gate").mock(
            return_value=Response(
                200,
                json={
                    "decision": "allow",
                    "actions": [],
                    "local_cost_cents": 0,
                    "policy_id": "policy-test",
                    "decision_source": "gateway",
                },
            )
        )
        # Execute endpoint
        respx.post(f"{BASE_URL}/api/v1/execute").mock(
            return_value=Response(
                200,
                json={
                    "decision": "allow",
                    "decision_source": "gateway",
                    "explanation": "allowed",
                    "policy_version": 1,
                },
            )
        )
        # Check endpoint
        respx.post(f"{BASE_URL}/check").mock(
            return_value=Response(
                200,
                json={
                    "allowed": True,
                    "actions": [],
                    "blocked_reason": None,
                },
            )
        )
        # Track batch endpoint
        respx.post(f"{BASE_URL}/api/v1/track/batch").mock(
            return_value=Response(200, json={"ok": True, "accepted": 1})
        )
        # Capabilities endpoint (canonical /api/v1/capabilities).
        # Empty capabilities object — SDK treats this as a non-v3 backend
        # and continues in compatibility mode.
        respx.get(f"{BASE_URL}/api/v1/capabilities").mock(
            return_value=Response(
                200,
                json={
                    "min_protocol_version": 1,
                    "max_protocol_version": 1,
                    "protocol_version": 1,
                    "capabilities": {
                        "server_minted_execution_id": False,
                        "per_execution_reservations": False,
                        "enforcement_modes_soft": False,
                        "heartbeat_time_based": False,
                    },
                },
            )
        )
        yield


@pytest.fixture
def make_runtime(mock_api):
    """Factory for creating isolated NullRunRuntime in tests.

    Pins the created runtime into the @protect decorator's module-level
    slot so `@protect` resolves the test runtime rather than trying to
    construct one with no api_key.
    """
    import nullrun.decorators as _dec
    from nullrun.runtime import NullRunRuntime

    def _make(**kwargs):
        defaults = dict(
            api_key="test-key-12345678",
            api_url=BASE_URL,
            polling=False,  # Internal flag: no background WS/HTTP poller opening real sockets.
        )
        defaults.update(kwargs)
        rt = NullRunRuntime(**defaults)
        _dec._runtime = rt  # Pin for @protect decorator's lazy resolution.
        return rt

    return _make


@pytest.fixture
def make_test_runtime(monkeypatch, tmp_path):
    """Factory for tests that build a real ``NullRunRuntime`` inline (no ``mock_api`` indirection).

    Pins ``NULLRUN_WAL_PATH`` to a tmp_path-scoped file so the constructor's
    ``Transport._replay_from_wal`` never reads the default WAL (which may carry
    real on-disk events from a previous run).
    """
    from unittest.mock import MagicMock

    from nullrun.runtime import NullRunRuntime

    NullRunRuntime.reset_instance()
    monkeypatch.setenv("NULLRUN_WAL_PATH", str(tmp_path / "sdk.wal"))

    def _factory(**overrides):
        api_key = overrides.pop("api_key", "test-key-12345678")
        rt = NullRunRuntime(api_key=api_key, _test_mode=True)
        rt._transport._do_flush = lambda: None
        rt._transport._do_flush_locked = lambda: None
        rt._transport._client = MagicMock()
        for k, v in overrides.items():
            setattr(rt, k, v)
        return rt

    yield _factory
    NullRunRuntime.reset_instance()


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch, request):
    # Neutralise time.sleep in test code so the suite is no longer gated on
    # the retry loop's real wall-clock wait. The CB state machine inspects
    # time.monotonic() so we don't need to move a clock.
    # A test that needs the real wall clock can decorate itself with
    # ``@pytest.mark.slow_sleep``. Legacy env-var override
    # ``NULLRUN_FAST_SLEEP=0`` is also honoured.
    if os.environ.get("NULLRUN_FAST_SLEEP") == "0":
        yield
        return
    if request.node.get_closest_marker("slow_sleep") is not None:
        yield
        return

    import time as _time

    _real_sleep = _time.sleep

    def _fast_sleep(seconds):
        # Cap any test sleep at 1ms.
        if seconds > 0.001:
            return _real_sleep(0.001)
        return _real_sleep(seconds)

    monkeypatch.setattr(_time, "sleep", _fast_sleep)
    # Stub the modules that captured a module-level reference at import time.
    try:
        import nullrun.transport as _transport_mod

        monkeypatch.setattr(_transport_mod.time, "sleep", _fast_sleep)
    except Exception:
        pass
    try:
        import nullrun.breaker.circuit_breaker as _cb_mod

        monkeypatch.setattr(_cb_mod.time, "sleep", _fast_sleep)
    except Exception:
        pass
    yield



@pytest.fixture(autouse=True)
def _isolated_wal(monkeypatch, tmp_path):
    # CI flakefix: every test gets a private NULLRUN_WAL_PATH so
    # Transport._replay_from_wal cannot replay events from a previous run
    # against the real backend. Without this, NullRunRuntime.__init__ reads
    # the default tempfile.gettempdir()/nullrun.wal and tries to drain any
    # events found there against the real api_url — the backend returns 401
    # and NullRunAuthError propagates back into the test fixture setup.
    monkeypatch.setenv("NULLRUN_WAL_PATH", str(tmp_path / "sdk.wal"))
    yield
