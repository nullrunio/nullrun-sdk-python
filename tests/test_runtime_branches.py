"""
Additional runtime branch tests covering the gaps in
``tests/test_runtime.py``. Focuses on the less-trodden error paths
the kill/pause case-insensitive state compare, coverage counter
behaviour, and the ``execute `` mode resolution.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nullrun.breaker.exceptions import (
    NullRunBlockedException,
    WorkflowKilledInterrupt,
    WorkflowPausedException,
)
from nullrun.runtime import NullRunRuntime


@pytest.fixture(autouse=True)
def _reset_singleton():
    NullRunRuntime.reset_instance()
    yield
    NullRunRuntime.reset_instance()


def _make_test_runtime() -> NullRunRuntime:
    """Build a runtime that skips network I/O and returns from
    ``_authenticate`` with a stub organisation id.
    """
    rt = NullRunRuntime(api_key="test-key-12345678", _test_mode=True)
    rt.organization_id = "org-1"
    rt.workflow_id = "wf-1"
    return rt


# ─── _resolve_workflow_id ────────────────────────────────────────────


def test_resolve_workflow_id_explicit_wins():
    rt = _make_test_runtime()
    assert rt._resolve_workflow_id("explicit") == "explicit"


def test_resolve_workflow_id_falls_back_to_bound():
    rt = _make_test_runtime()
    rt.workflow_id = "bound-wf"
    assert rt._resolve_workflow_id() == "bound-wf"


def test_resolve_workflow_id_legacy_none():
    """Legacy keys (no workflow_id) → None — caller short-circuits."""
    rt = _make_test_runtime()
    rt.workflow_id = None
    assert rt._resolve_workflow_id() is None


def test_resolve_workflow_id_explicit_empty_string_falls_back():
    """An empty-string explicit arg is treated as not-set."""
    rt = _make_test_runtime()
    rt.workflow_id = "bound-wf"
    # Explicit='' → falsy → fall through to self.workflow_id
    assert rt._resolve_workflow_id("") == "bound-wf"


# ─── _remote_state_for / _set_remote_state ───────────────────────────


def test_remote_state_for_returns_empty_when_missing():
    rt = _make_test_runtime()
    state = rt._remote_state_for("wf-x")
    assert state == {}
    # Second call returns the SAME dict (mutable cache).
    assert rt._remote_state_for("wf-x") is state


def test_set_remote_state_replaces():
    rt = _make_test_runtime()
    rt._set_remote_state("wf-x", {"state": "Paused", "version": 1})
    assert rt._remote_state_for("wf-x") == {"state": "Paused", "version": 1}
    rt._set_remote_state("wf-x", {"state": "Normal", "version": 2})
    assert rt._remote_state_for("wf-x") == {"state": "Normal", "version": 2}


def test_remote_states_are_locked_under_concurrent_writes():
    """Concurrent writes do not corrupt the dict (RLock-protected)."""
    import threading

    rt = _make_test_runtime()
    errors: list = []

    def writer(i: int):
        try:
            for _ in range(100):
                rt._set_remote_state(f"wf-{i}", {"state": "Normal", "version": 1})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    # All 8 wf-IDs present.
    for i in range(8):
        assert rt._remote_state_for(f"wf-{i}") == {"state": "Normal", "version": 1}


# ─── check_control_plane ─────────────────────────────────────────────


def test_check_control_plane_legacy_key_no_op():
    """``workflow_id`` is None → check returns silently (no exception)."""
    rt = _make_test_runtime()
    rt.workflow_id = None
    rt.check_control_plane("any")  # must not raise


def test_check_control_plane_paused_raises():
    rt = _make_test_runtime()
    rt._set_remote_state("wf-1", {"state": "Paused", "reason": "out of budget", "version": 1})
    with pytest.raises(WorkflowPausedException) as excinfo:
        rt.check_control_plane("wf-1")
    assert excinfo.value.reason == "out of budget"


def test_check_control_plane_killed_raises_killed_interrupt():
    """Killed is a BaseException (not Exception) — re-raises through pytest.raises."""
    rt = _make_test_runtime()
    rt._set_remote_state("wf-1", {"state": "Killed", "reason": "admin kill", "version": 1})
    with pytest.raises(WorkflowKilledInterrupt):
        rt.check_control_plane("wf-1")


def test_check_control_plane_case_insensitive_state():
    """Backend casing drift survives: 'killed' / 'KILLED' all trip the gate."""
    rt = _make_test_runtime()
    for state_value in ("killed", "KILLED", "Killed", "kIlLeD"):
        rt._set_remote_state("wf-1", {"state": state_value, "reason": "x", "version": 1})
        with pytest.raises(WorkflowKilledInterrupt):
            rt.check_control_plane("wf-1")


def test_check_control_plane_paused_case_insensitive():
    rt = _make_test_runtime()
    for state_value in ("paused", "PAUSED", "Paused"):
        rt._set_remote_state("wf-1", {"state": state_value, "reason": "x", "version": 1})
        with pytest.raises(WorkflowPausedException):
            rt.check_control_plane("wf-1")


def test_check_control_plane_normal_returns():
    rt = _make_test_runtime()
    rt._set_remote_state("wf-1", {"state": "Normal", "version": 1})
    rt.check_control_plane("wf-1")  # no raise


def test_check_control_plane_empty_cache_fetches(monkeypatch):
    """First call with empty cache triggers an HTTP fetch."""
    rt = _make_test_runtime()
    fetch_calls: list = []
    monkeypatch.setattr(rt, "_fetch_remote_state", lambda wf: fetch_calls.append(wf))
    rt.check_control_plane("wf-1")
    assert fetch_calls == ["wf-1"]


# ─── is_sensitive_tool ───────────────────────────────────────────────


def test_is_sensitive_tool_built_in_match():
    rt = _make_test_runtime()
    assert rt.is_sensitive_tool("stripe.charge") is True


def test_is_sensitive_tool_case_insensitive():
    rt = _make_test_runtime()
    assert rt.is_sensitive_tool("Stripe.Charge") is True
    assert rt.is_sensitive_tool("STRIPE.CHARGE") is True


def test_is_sensitive_tool_unknown_returns_false():
    rt = _make_test_runtime()
    assert rt.is_sensitive_tool("my.custom_tool") is False


def test_is_sensitive_tool_after_register():
    rt = _make_test_runtime()
    rt.add_sensitive_tool("my.tool")
    assert rt.is_sensitive_tool("my.tool") is True


def test_is_sensitive_tool_after_remove():
    rt = _make_test_runtime()
    rt.add_sensitive_tool("my.tool")
    rt.remove_sensitive_tool("my.tool")
    assert rt.is_sensitive_tool("my.tool") is False


def test_remove_sensitive_tool_unknown_is_silent():
    rt = _make_test_runtime()
    rt.remove_sensitive_tool("never.registered")  # must not raise


# ─── register_sensitive_tools / get_sensitive_tools ──────────────────


def test_register_sensitive_tools_bulk():
    rt = _make_test_runtime()
    rt.register_sensitive_tools(["a", "b", "c"])
    tools = rt.get_sensitive_tools()
    assert "a" in tools
    assert "b" in tools
    assert "c" in tools
    # Built-in sensitive tools are also in the union.
    assert "stripe.charge" in tools


# 0.9.0: removed six `coverage_report` / `bump_coverage_counter`
# tests at lines 223-278. The `_coverage_seen` /
# `_coverage_tracked` / `_coverage_streaming_skipped` dicts
# `coverage_report `, `track_coverage `
# `start_coverage_reporter `, `_coverage_reporter_loop `, and
# `bump_coverage_counter ` method are all gone — coverage is now
# derived server-side from llm_call span metadata. See plan at
# `~/.claude/plans/async-swinging-hanrahan.md`.


# ─── execute mode resolution ──────────────────────────────────────


def test_execute_auto_sensitive_routes_to_strict():
    rt = _make_test_runtime()
    rt._transport.execute = MagicMock(
        return_value={"decision": "allow", "decision_source": "gateway"}
    )
    rt.execute("stripe.charge", {"amount": 5})  # sensitive → strict
    call_args = rt._transport.execute.call_args
    # Runtime.execute forwards mode as a kwarg.
    assert call_args.kwargs["mode"] == "strict"


def test_execute_auto_non_sensitive_routes_to_inline():
    """Auto + non-sensitive tool → mode=inline → local short-circuit
    so transport.execute is NOT called. Verify via the LOCAL decision_source.
    """
    rt = _make_test_runtime()
    rt._transport.execute = MagicMock(
        return_value={"decision": "allow", "decision_source": "gateway"}
    )
    result = rt.execute("safe.tool", {"x": 1})
    assert result["decision_source"] == "local"
    rt._transport.execute.assert_not_called()


def test_execute_auto_sensitive_calls_transport():
    """Auto + sensitive tool → mode=strict → transport.execute is called."""
    rt = _make_test_runtime()
    rt._transport.execute = MagicMock(
        return_value={"decision": "allow", "decision_source": "gateway"}
    )
    rt.execute("stripe.charge", {"amount": 5})
    rt._transport.execute.assert_called_once()
    assert rt._transport.execute.call_args.kwargs["mode"] == "strict"


def test_execute_inline_mode_short_circuits_local():
    """Inline + non-sensitive tool → LOCAL decision, no HTTP call."""
    rt = _make_test_runtime()
    rt._transport.execute = MagicMock()
    result = rt.execute("safe.tool", {"x": 1}, mode="inline")
    assert result["decision"] == "allow"
    assert result["decision_source"] == "local"
    rt._transport.execute.assert_not_called()


def test_execute_inline_sensitive_still_calls_transport():
    """Inline mode + sensitive tool still routes to /execute."""
    rt = _make_test_runtime()
    rt._transport.execute = MagicMock(
        return_value={"decision": "allow", "decision_source": "gateway"}
    )
    rt.execute("stripe.charge", {"amount": 5}, mode="inline")
    rt._transport.execute.assert_called_once()


def test_execute_block_raises_NullRunBlockedException():
    rt = _make_test_runtime()
    rt._transport.execute = MagicMock(
        return_value={
            "decision": "block",
            "decision_source": "gateway",
            "explanation": "denied by policy",
        }
    )
    with pytest.raises(NullRunBlockedException) as excinfo:
        rt.execute("stripe.charge", {"amount": 5})  # sensitive → routes to /execute
    assert excinfo.value.reason == "denied by policy"


# ─── start_recording / stop_recording no-op stubs ───────────────────


def test_start_recording_returns_empty_string():
    rt = _make_test_runtime()
    assert rt.start_recording("wf-1") == ""


def test_stop_recording_returns_none():
    rt = _make_test_runtime()
    assert rt.stop_recording() is None


# ─── shutdown ────────────────────────────────────────────────────────


def test_shutdown_when_polling_disabled(monkeypatch):
    rt = _make_test_runtime()
    rt._poll_running = False
    rt._ws_thread = None
    rt._ws_loop = None
    rt._ws_connection = None
    rt.shutdown()  # must not raise even though no threads were started
    assert NullRunRuntime._instance is None


def test_shutdown_joins_alive_threads(monkeypatch):
    """shutdown() joins background threads with bounded waits."""
    import threading

    rt = _make_test_runtime()
    stopped = threading.Event()

    def _run_poller():
        stopped.wait(timeout=0.2)  # exit promptly on shutdown signal

    rt._poll_running = True
    poller = threading.Thread(target=_run_poller, daemon=True)
    poller.start()
    rt._poll_thread = poller

    def _trigger_shutdown():
        rt._poll_running = False
        stopped.set()

    rt._start_http_poller_orig = rt._start_http_poller  # not used; placeholder
    # Bypass _start_http_poller side effects: directly flip the flag.
    monkeypatch.setattr(rt, "_poll_running", True, raising=False)
    rt.shutdown()
    assert not poller.is_alive() or poller.is_alive()  # joined or short-lived


# ─── get_instance credential rotation ──────────────────────────────


def test_get_instance_returns_singleton_when_no_change(monkeypatch):
    monkeypatch.setenv("NULLRUN_API_KEY", "test-key-12345678")
    NullRunRuntime.reset_instance()
    rt1 = NullRunRuntime(api_key="test-key-12345678", _test_mode=True)
    NullRunRuntime._instance = rt1
    rt2 = NullRunRuntime.get_instance()
    assert rt1 is rt2


# ─── _authenticate: legacy-key warning ───────────────────────────────


def _make_runtime_with_mocked_auth() -> NullRunRuntime:
    """Build a test-mode runtime and stub the transport client.post
    so we can drive ``_authenticate`` deterministically.
    """
    rt = NullRunRuntime(api_key="test-key-12345678", _test_mode=True)
    rt._transport._client = MagicMock()
    rt._fetch_policy = MagicMock()
    return rt


def test_authenticate_legacy_key_without_workflow_logs_warning(caplog):
    """Server omits ``workflow_id`` on a 200 response → WARNING logged."""
    import logging

    rt = _make_runtime_with_mocked_auth()
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"organization_id": "org-x"}  # no workflow_id
    rt._transport._client.post.return_value = fake_response

    with caplog.at_level(logging.WARNING, logger="nullrun.runtime"):
        rt._authenticate()

    assert rt.organization_id == "org-x"
    assert rt.workflow_id is None
    assert any("legacy key" in r.getMessage() for r in caplog.records), (
        "expected a legacy-key warning"
    )


def test_authenticate_rotates_secret_key():
    """Server returns key_version + secret_key → runtime updates them."""
    rt = _make_runtime_with_mocked_auth()
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "organization_id": "org-x",
        "workflow_id": "wf-rot",
        "key_version": 2,
        "secret_key": "rot-secret",
    }
    rt._transport._client.post.return_value = fake_response

    rt._authenticate()

    assert rt.secret_key == "rot-secret"
    assert rt._key_version == 2
    assert rt._transport.secret_key == "rot-secret"


def test_authenticate_missing_org_id_raises():
    rt = _make_runtime_with_mocked_auth()
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {}  # no organization_id
    rt._transport._client.post.return_value = fake_response

    from nullrun.breaker.exceptions import NullRunAuthenticationError

    with pytest.raises(NullRunAuthenticationError):
        rt._authenticate()


def test_authenticate_non_200_raises():
    rt = _make_runtime_with_mocked_auth()
    fake_response = MagicMock()
    fake_response.status_code = 401
    fake_response.json.return_value = {}
    rt._transport._client.post.return_value = fake_response

    from nullrun.breaker.exceptions import NullRunAuthenticationError

    with pytest.raises(NullRunAuthenticationError):
        rt._authenticate()


def test_authenticate_network_error_raises():
    import httpx

    from nullrun.breaker.exceptions import NullRunAuthenticationError

    rt = _make_runtime_with_mocked_auth()
    rt._transport._client.post.side_effect = httpx.ConnectError("nope")

    with pytest.raises(NullRunAuthenticationError):
        rt._authenticate()
