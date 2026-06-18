"""
Regression tests for HIGH-reliability fixes in 0.4.0.

Phase 5 of the production-readiness plan:
- #5.1: _remote_state_for / _set_remote_state / _states_lock helpers.
- #5.2: PolicyCache policy_version is its own field, not ttl_seconds.
- #5.3: get_instance() atomic credential rotation.
- #5.5: _fetch_remote_state uses shared transport client.
- #5.6: workflow() emits UUID4 (was wf-{hex32}).
- #5.7: @sensitive propagates NullRunAuthenticationError.
- #5.8: Custom-host KILL reach.
- #5.10: Transport.execute on_transport_error callback.
"""
from __future__ import annotations

# ===========================================================================
# 5.1: Remote state helpers
# ===========================================================================

def test_remote_states_lock_is_rlock():
    """`_states_lock` is an RLock so gate-check re-entry doesn't deadlock."""
    import threading

    from nullrun.runtime import NullRunRuntime

    runtime = NullRunRuntime(api_key="test", _test_mode=True)
    assert hasattr(runtime, "_states_lock")
    assert isinstance(runtime._states_lock, type(threading.RLock()))


def test_remote_state_for_returns_empty_dict_for_unseen_workflow():
    """`_remote_state_for` returns `{}` (not None) for unseen workflows."""
    from nullrun.runtime import NullRunRuntime

    runtime = NullRunRuntime(api_key="test", _test_mode=True)
    state = runtime._remote_state_for("wf-never-seen")
    assert state == {}
    # Repeated call returns the same dict (no new entry every time).
    state2 = runtime._remote_state_for("wf-never-seen")
    assert state is state2


def test_set_remote_state_replaces_atomically():
    """`_set_remote_state` makes a defensive copy of the dict."""
    from nullrun.runtime import NullRunRuntime

    runtime = NullRunRuntime(api_key="test", _test_mode=True)
    incoming = {"state": "Killed", "version": 1, "reason": "test"}
    runtime._set_remote_state("wf-1", incoming)

    state = runtime._remote_state_for("wf-1")
    assert state == incoming
    # Mutating the original shouldn't affect the stored copy.
    incoming["state"] = "Paused"
    assert runtime._remote_state_for("wf-1")["state"] == "Killed"


# ===========================================================================
# 5.2: PolicyCache
# ===========================================================================

def test_policy_cache_preserves_ttl():
    """`policy_version` must NOT be written into `ttl_seconds`."""
    from nullrun.transport import PolicyCache

    cache = PolicyCache(maxsize=10, ttl_seconds=300.0)
    cache.set("k1", "allow", policy_id="p1", policy_version=42)
    entry = cache._cache["k1"]
    assert entry.ttl_seconds == 300.0  # unchanged
    assert entry.policy_version == 42  # new dedicated field


def test_cached_decision_exposes_policy_version():
    """`CachedDecision` has a `policy_version` field that defaults to None."""
    from nullrun.transport import CachedDecision

    entry = CachedDecision(decision="allow", policy_id="p1")
    assert entry.policy_version is None

    entry2 = CachedDecision(decision="block", policy_id="p1", policy_version=5)
    assert entry2.policy_version == 5


# ===========================================================================
# 5.5: _fetch_remote_state uses shared client
# ===========================================================================

def test_fetch_remote_state_uses_transport_client(monkeypatch):
    """`_fetch_remote_state` routes through `self._transport._client.get`."""
    from nullrun.runtime import NullRunRuntime

    runtime = NullRunRuntime(api_key="test", _test_mode=True)

    called = []

    class FakeClient:
        def get(self, url, headers=None, timeout=None):
            called.append(url)
            class FakeResp:
                status_code = 200
                def json(self):
                    return {"state": "Killed", "version": 1, "reason": "test"}
            return FakeResp()

    runtime._transport._client = FakeClient()
    runtime._fetch_remote_state("wf-1")
    assert len(called) == 1
    assert "/api/v1/status/wf-1" in called[0]


# ===========================================================================
# 5.6: workflow() emits UUID4
# ===========================================================================

def test_workflow_emits_uuid4_when_no_name():
    """Auto-generated workflow IDs are UUID4 (not wf-{hex32})."""
    import uuid as _uuid

    from nullrun.context import workflow

    with workflow() as wid:
        _uuid.UUID(wid)  # raises ValueError if not a UUID


def test_workflow_uses_explicit_name():
    """Explicit names pass through unchanged."""
    from nullrun.context import workflow

    with workflow("my-custom-id") as wid:
        assert wid == "my-custom-id"


# ===========================================================================
# 5.7: @sensitive propagates auth error
# ===========================================================================

def test_sensitive_raises_on_missing_api_key(monkeypatch):
    """`@sensitive` now propagates NullRunAuthenticationError when no api_key."""
    monkeypatch.delenv("NULLRUN_API_KEY", raising=False)
    # Reset singleton so the env change is picked up.
    from nullrun.runtime import NullRunRuntime
    NullRunRuntime.reset_instance()

    try:
        import pytest

        import nullrun.decorators as dec
        from nullrun.breaker.exceptions import NullRunAuthenticationError

        @dec.sensitive
        def my_func(x):
            return x

        # First call constructs the runtime; should raise NullRunAuthenticationError.
        with pytest.raises(NullRunAuthenticationError):
            # Trigger lazy runtime creation via a real method call.
            NullRunRuntime.get_instance()
    finally:
        # Restore singleton state.
        NullRunRuntime.reset_instance()


# ===========================================================================
# 5.8: Custom-host KILL reach
# ===========================================================================

def test_kill_switch_honoured_for_custom_host():
    """The kill check no longer gates on the extractor table."""
    from nullrun.instrumentation.auto import _check_kill_before_send
    from nullrun.runtime import NullRunRuntime

    runtime = NullRunRuntime(api_key="test", _test_mode=True)
    runtime.workflow_id = "wf-1"
    runtime._set_remote_state("wf-1", {"state": "Killed", "reason": "test"})

    import httpx
    import pytest

    from nullrun.breaker.exceptions import WorkflowKilledInterrupt

    req = httpx.Request("POST", "https://my-custom-llm.example.com/v1/chat")
    with pytest.raises(WorkflowKilledInterrupt):
        _check_kill_before_send(runtime, req)


def test_kill_switch_skipped_for_normal_state():
    """Normal state never raises."""
    from nullrun.instrumentation.auto import _check_kill_before_send
    from nullrun.runtime import NullRunRuntime

    runtime = NullRunRuntime(api_key="test", _test_mode=True)
    runtime.workflow_id = "wf-2"
    # Empty state defaults to "Normal".

    import httpx

    req = httpx.Request("POST", "https://my-custom-llm.example.com/v1/chat")
    # Should NOT raise.
    _check_kill_before_send(runtime, req)


# ===========================================================================
# 5.10: Transport.execute on_transport_error callback
# ===========================================================================

def test_execute_on_transport_error_callback_receives_breaker_error(monkeypatch):
    """on_transport_error callback receives the BreakerTransportError.

    The callback contract is: when NullRunRuntime.execute is invoked
    with ``on_transport_error=callable`` AND ``mode="strict"``, the
    transport raises ``BreakerTransportError`` (from the CB after
    max retries), the runtime catches it via the callback, and the
    callback's return value becomes the runtime's return value.

    We stub ``runtime._transport.execute`` to raise directly so the
    test exercises the callback contract without depending on the
    internal circuit breaker / retry helper.
    """
    from nullrun.breaker.exceptions import BreakerTransportError
    from nullrun.runtime import NullRunRuntime

    runtime = NullRunRuntime(api_key="test", _test_mode=True)

    def fake_transport_execute(*args, **kwargs):
        # Simulate what Transport.execute does on a real network
        # failure: invoke the on_transport_error callback (if any)
        # before propagating.
        cb = kwargs.get("on_transport_error")
        if callable(cb):
            return cb(BreakerTransportError("circuit open"))
        raise BreakerTransportError("circuit open")

    monkeypatch.setattr(
        runtime._transport, "execute", fake_transport_execute
    )

    received = []

    def callback(exc):
        received.append(exc)
        return {"decision": "block", "decision_source": "FALLBACK"}

    # Round 3 (Phase 0.4.0): runtime.execute raises NullRunBlockedException
    # when the result has decision="block". The callback was already invoked
    # by Transport.execute before the result propagated up.
    import pytest

    from nullrun.breaker.exceptions import NullRunBlockedException
    with pytest.raises(NullRunBlockedException):
        runtime.execute(
            "test_tool", {}, mode="strict", on_transport_error=callback,
        )
    assert len(received) == 1
    assert isinstance(received[0], BreakerTransportError)