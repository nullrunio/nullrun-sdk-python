"""
conftest.py - shared pytest fixtures and respx mocking
"""

import pytest
import respx
from httpx import Response

# Base URL used in tests
BASE_URL = "https://api.test.nullrun.io"


@pytest.fixture(autouse=True)
def reset_runtime():
    """Reset all singletons before each test (not after - avoids double-flush issues)."""
    # Import here to avoid circular issues
    import nullrun.actions as _act
    import nullrun.decorators as _dec
    import nullrun.runtime as _rt_mod
    from nullrun.context import _call_model_var, _call_tools_var
    from nullrun.runtime import NullRunRuntime

    # Disable polling for all tests via the runtime's internal `polling` flag
    # (see make_runtime below — passes polling=False by default). The legacy
    # NULLRUN_DISABLE_POLLING env var is gone as of Commit 5.

    # Reset before test only - don't call shutdown in teardown
    # because mock_api fixture already cleaned up its respx context
    NullRunRuntime.reset_instance()
    _dec._runtime = None
    _act._action_handler = None
    # Module-level cache used by `nullrun.track_llm` / `nullrun.track_tool` →
    # `get_runtime `. Without this, a stale singleton from a previous test
    # leaks across the suite (e.g. a test that did `nullrun.init(...)` with
    # the prod URL leaves that URL pinned for the next test).
    _rt_mod._runtime = None
    # T4 (2026-06-27): reset the per-call context (model + tools) so a
    # previous test's `set_call_context(...)` doesn't leak into the next
    # test's wire payload.
    _call_model_var.set(None)
    _call_tools_var.set(())

    yield

    # Stop any running transport flush thread BEFORE we drop the
    # reference. Without this the thread keeps running across tests,
    # the buffer drains through httpx with no respx context active,
    # and the worker logs a ``ConnectError`` retry storm for the rest
    # of the xdist session — observed 9m 47s of "Request failed
    # (attempt N/11), retrying in 10s" on PR #60, which dwarfed the
    # actual test time. ``flush=False`` skips the final ``_do_flush``
    # / ``_persist_to_wal`` so the teardown is a true no-op even when
    # the buffer still has events; the test that wrote them is
    # responsible for asserting on what it cared about. Best-effort:
    # the runtime may be in any state at teardown, and we don't want
    # a flaky shutdown to mask the real test failure that just ran.
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
        # Execute endpoint. 2026-07-05 retry-budget bump surfaced
        # the test suite previously relied on respx allow-all for
        # unmocked URLs, which only worked because the old
        #  × 5s httpx timeout still completed in
        # <2s. Adding the explicit mock makes the execute path
        # deterministic regardless of the retry count.
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
        # 0.7.0: SDK no longer fetches /policies on init (backend
        # owns all policy state; SDK is a thin client).
        # Health endpoint
        respx.get(f"{BASE_URL}/health").mock(return_value=Response(200, json={"status": "ok"}))
        yield


@pytest.fixture
def make_runtime(mock_api):
    """Factory for creating isolated NullRunRuntime in tests.

    Pins the created runtime into the @protect decorator's module-level
    slot so `@protect` (which resolves a runtime lazily via
    `decorators._get_or_create_runtime`) finds the test runtime, not a
    fallback that would try to construct one with no api_key.
    """
    import nullrun.decorators as _dec
    from nullrun.runtime import NullRunRuntime

    def _make(**kwargs):
        defaults = dict(
            api_key="test-key-12345678",
            api_url=BASE_URL,
            # Internal flag — tests don't want a background WS/HTTP poller
            # opening real sockets. The mocked respx context only covers
            # auth/policy/track endpoints, not the long-lived control plane.
            polling=False,
        )
        defaults.update(kwargs)
        rt = NullRunRuntime(**defaults)
        # Pin for @protect decorator's lazy resolution. Without this
        # @protect would call NullRunRuntime.get_instance which reads
        # env vars, finds no NULLRUN_API_KEY in the test environment
        # and raise NullRunAuthenticationError.
        _dec._runtime = rt
        return rt

    return _make
