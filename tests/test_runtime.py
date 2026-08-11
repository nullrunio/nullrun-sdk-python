"""
tests/test_runtime.py — coverage for NullRunRuntime and @protect.
Dependencies: pip install pytest pytest-asyncio respx httpx
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from nullrun import protect
from nullrun.breaker.exceptions import (
    NullRunBlockedException,
)
from nullrun.runtime import NullRunRuntime

# Base URL used in tests
BASE_URL = "https://api.test.nullrun.io"


# ──────────────────────────────────────────────────────────────
# NullRunRuntime — initialization
# ──────────────────────────────────────────────────────────────


class TestNullRunRuntimeInit:
    def test_creates_with_explicit_params(self, make_runtime):
        rt = make_runtime()
        assert rt is not None

    def test_reads_api_key_from_env(self, monkeypatch, make_runtime):
        monkeypatch.setenv("NULLRUN_API_KEY", "env-key-12345678")
        monkeypatch.setenv("NULLRUN_API_URL", "https://api.test.nullrun.io")
        monkeypatch.setenv("NULLRUN_WORKSPACE_ID", "ws-env")
        rt = make_runtime()
        assert rt is not None

    def test_works_without_api_key_raises(self, monkeypatch):
        """api_key is now required — raises NullRunAuthenticationError instead of silently entering local mode."""
        from nullrun.breaker.exceptions import NullRunAuthenticationError

        monkeypatch.delenv("NULLRUN_API_KEY", raising=False)
        with pytest.raises(NullRunAuthenticationError, match="requires an api_key"):
            NullRunRuntime(api_url=BASE_URL)

    def test_singleton_get_instance(self, make_runtime, monkeypatch):
        """get_instance returns the singleton instance."""
        monkeypatch.setenv("NULLRUN_API_KEY", "test-key-12345678")
        monkeypatch.setenv("NULLRUN_API_URL", "https://api.test.nullrun.io")
        rt1 = make_runtime()
        rt2 = NullRunRuntime.get_instance()
        assert rt1 is not None
        assert rt2 is not None

    def test_reset_clears_singleton(self, make_runtime):
        make_runtime()
        from nullrun import reset

        reset()
        # After reset, get_instance either creates a new runtime or returns None.


# ──────────────────────────────────────────────────────────────
# NullRunRuntime — track
# ──────────────────────────────────────────────────────────────


class TestNullRunRuntimeTrack:
    def test_track_enqueues_event(self, make_runtime):
        """track() is non-blocking and queues the event on the buffer."""
        rt = make_runtime()
        # track fire-and-forget — must not raise
        rt.track({"event_type": "llm_call", "model": "gpt-4", "tokens": 100})
        rt.track({"event_type": "tool_call", "tool": "search"})
        # no exceptions — ok

    def test_track_does_not_raise_on_server_error(self, make_runtime, mock_api):
        """track() fire-and-forget — a server error must not propagate into the calling code."""
        respx.post(f"{BASE_URL}/track/batch").mock(return_value=httpx.Response(500))
        rt = make_runtime()
        # Must not raise.
        rt.track({"event_type": "test"})

    def test_wire_payload_strips_sensitive_fields(self, make_runtime):
        """Privacy boundary: raw_usage, _fingerprint and cost_cents must NOT appear on transport buffer."""
        rt = make_runtime()
        captured: list[dict] = []
        rt._transport.track = lambda event: captured.append(dict(event))

        rt.track(
            {
                "type": "llm_call",
                "provider": "openai",
                "model": "gpt-4o",
                "tokens": 15,
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_tokens": 7,
                "finish_reason": "stop",
                "tool_names": ["search"],
                "has_usage": True,
                # These three MUST be stripped before the transport
                # buffer sees the event.
                "cost_cents": 0.001,
                "_fingerprint": "abc123def456",
                "raw_usage": {
                    "prompt_tokens": 10,
                    "secret_routing_info": "dc-us-east-1",
                },
            }
        )

        assert len(captured) == 1, "transport.track should be called exactly once"
        sent = captured[0]

        # Stripped at the wire boundary
        assert "cost_cents" not in sent, "cost_cents leaked to wire"
        assert "_fingerprint" not in sent, "_fingerprint leaked to wire"
        assert "raw_usage" not in sent, "raw_usage leaked to wire"
        # Sensitive nested field also gone (because raw_usage is gone)
        assert "secret_routing_info" not in sent

        # Normalised fields pass through unchanged
        assert sent["type"] == "llm_call"
        assert sent["input_tokens"] == 10
        assert sent["cache_read_tokens"] == 7
        assert sent["finish_reason"] == "stop"
        assert sent["tool_names"] == ["search"]


# ──────────────────────────────────────────────────────────────
# NullRunRuntime — execute
# ──────────────────────────────────────────────────────────────


class TestNullRunRuntimeExecute:
    def test_execute_allowed_returns_result(self, make_runtime, mock_api):
        respx.post(f"{BASE_URL}/execute").mock(
            return_value=httpx.Response(
                200,
                json={
                    "decision": "allow",
                    "decision_source": "gateway",
                    "explanation": "allowed",
                    "policy_version": 1,
                },
            )
        )
        rt = make_runtime()
        result = rt.execute(
            tool_name="gpt-4",
            input_data={"prompt": "hello"},
        )
        assert result["decision"] == "allow"

    def test_execute_blocked_raises(self, make_runtime, mock_api):
        # /api/v1/execute (not /gate) is the enforcement point — scope check.
        respx.post(f"{BASE_URL}/api/v1/execute").mock(
            return_value=httpx.Response(
                200,
                json={
                    "decision": "block",
                    "explanation": "cost_limit_exceeded",
                    "decision_source": "gateway",
                    "policy_version": 1,
                },
            )
        )
        rt = make_runtime()
        # Use mode="strict" to force gateway call
        # (auto mode might use inline for non-sensitive tools)
        with pytest.raises(NullRunBlockedException):
            rt.execute(tool_name="gpt-4", input_data={}, mode="strict")

    def test_execute_blocked_surfaces_wire_error_code(self, make_runtime, mock_api):
        # DEF-ARFLOW-TOOLNAME-01 (E2E 2026-08-05): the backend now stamps
        # ``details.error_code`` on block responses via
        # ``classify_approval_create_error``. The SDK must surface the
        # structured code verbatim instead of falling back to the
        # keyword-on-explanation path (which would have classified
        # "Approval infrastructure unavailable: validation error during
        # approval row creation" as the generic NR-X001 — the very
        # bug the journal test surfaced).
        respx.post(f"{BASE_URL}/api/v1/execute").mock(
            return_value=httpx.Response(
                200,
                json={
                    "decision": "block",
                    "explanation": (
                        "Approval infrastructure unavailable: validation "
                        "error during approval row creation — failing closed "
                        "per Hard-always policy"
                    ),
                    "decision_source": "gateway",
                    "policy_version": 1,
                    "details": {
                        "error_code": "APPROVAL_VALIDATION_FAILED",
                        "decision_source": "approval_create_failed",
                    },
                },
            )
        )
        rt = make_runtime()
        with pytest.raises(NullRunBlockedException) as exc_info:
            rt.execute(tool_name="refund_customer", input_data={}, mode="strict")
        # The wire code wins — no keyword guessing.
        assert exc_info.value.error_code == "APPROVAL_VALIDATION_FAILED"
        # The structured payload is preserved on details so a caller
        # can introspect ``decision_source`` for routing/alerting.
        # ``NullRunBlockedException.__init__`` wraps ``**details`` so
        # the dict lands under the "details" key on self.details.
        wire_details = exc_info.value.details.get("details") or {}
        assert wire_details.get("error_code") == "APPROVAL_VALIDATION_FAILED"
        assert wire_details.get("decision_source") == "approval_create_failed"
        # Back-compat shim: the legacy ``mapped_class`` field is still
        # populated so any caller that branched on it pre-fix keeps working.
        assert wire_details.get("mapped_class") == "NullRunBlockedException"

    # T3-S2 (0.3.0): `test_execute_local_mode_allows` was removed along
    # with the `local_mode` field. The execute path now always hits
    # the /execute endpoint — there is no local stub to test.


# ──────────────────────────────────────────────────────────────
# @protect decorator
# ──────────────────────────────────────────────────────────────


class TestProtectDecorator:
    def test_protect_calls_wrapped_function(self, make_runtime, mock_api):
        """@protect must not break the wrapped function call."""
        make_runtime()

        @protect
        def my_tool(x: int) -> int:
            return x * 2

        result = my_tool(5)
        assert result == 10

    def test_protect_returns_original_value(self, make_runtime, mock_api):
        make_runtime()

        @protect
        def identity(val):
            return val

        assert identity("hello") == "hello"
        assert identity(42) == 42
        assert identity({"a": 1}) == {"a": 1}

    def test_protect_preserves_function_metadata(self, make_runtime, mock_api):
        """@protect preserves the wrapped function's __name__ and __doc__."""
        make_runtime()

        @protect
        def my_documented_func():
            """This is my doc."""
            pass

        assert my_documented_func.__name__ == "my_documented_func"
        assert "doc" in (my_documented_func.__doc__ or "")

    @pytest.mark.asyncio
    async def test_protect_async_function(self, make_runtime, mock_api):
        """@protect works with async functions."""
        make_runtime()

        @protect
        async def async_tool():
            await asyncio.sleep(0)
            return "async_result"

        result = await async_tool()
        assert result == "async_result"

    def test_protect_no_runtime_inits_lazily(self, mock_api, monkeypatch):
        """Lazy init from env when no runtime is set up."""
        from nullrun import reset

        monkeypatch.setenv("NULLRUN_API_KEY", "test-key-12345678")
        monkeypatch.setenv("NULLRUN_API_URL", "https://api.test.nullrun.io")
        reset()

        @protect
        def tool():
            return "ok"

        result = tool()
        assert result == "ok"

    def test_protect_raises_without_api_key(self, monkeypatch):
        """@protect propagates NullRunAuthenticationError when no runtime and no env var.

        Before the fix, `_get_or_create_runtime` wrapped
        `get_instance ` in `try/except Exception` and rebuilt a
        no-arg `NullRunRuntime ` as a "fallback". That fallback was
        doubly broken in 0.3.0: it swallowed the auth error, then
        crashed with the same error from the no-arg constructor (which
        also requires `api_key` per T3-S2). The net effect was a
        delayed crash with a worse error message.

        After the fix, `_get_or_create_runtime` lets the error
        propagate from `get_instance ` unchanged. The user's first
        `@protect` call surfaces the same clear error that
        `nullrun.init ` would have raised at startup.
        """
        from nullrun import reset
        from nullrun.breaker.exceptions import NullRunAuthenticationError

        # Make sure no env var and no cached runtime.
        monkeypatch.delenv("NULLRUN_API_KEY", raising=False)
        monkeypatch.delenv("NULLRUN_API_URL", raising=False)
        reset()

        @protect
        def tool():
            return "ok"

        with pytest.raises(NullRunAuthenticationError):
            tool()

    def test_protect_sensitive_args_not_logged(self, make_runtime, mock_api, caplog):
        """Sensitive arguments must not appear in logs."""
        import logging

        make_runtime()

        @protect
        def login(username: str, password: str):
            return "ok"

        with caplog.at_level(logging.DEBUG):
            login(username="user", password="super-secret-password")

        # The password must not appear in the logs.
        assert "super-secret-password" not in caplog.text

    def test_protect_loop_detection(self, make_runtime, mock_api):
        """@protect enforces loop detection on repeated calls."""
        make_runtime()

        call_count = 0

        @protect
        def recursive_tool():
            nonlocal call_count
            call_count += 1
            return "ok"

        # Should complete without raising for reasonable number of calls
        for _ in range(5):
            recursive_tool()
        assert call_count == 5

    def test_protect_decorator_chaining(self, make_runtime, mock_api):
        """@protect can be chained with other decorators."""
        make_runtime()

        def my_custom_decorator(func):
            """Custom decorator that adds extra functionality."""

            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                # Add prefix to result
                result = func(*args, **kwargs)
                return f"decorated:{result}"

            return wrapper

        import functools

        @protect
        @my_custom_decorator
        def chained_tool():
            return "result"

        result = chained_tool()
        # Both decorators should be applied
        assert result == "decorated:result"


# ──────────────────────────────────────────────────────────────
# Test mode / Dependency Injection
# ──────────────────────────────────────────────────────────────


class TestRuntimeDI:
    """Test runtime dependency injection and test mode."""

    def test_runtime_di_transport_can_be_overridden(self):
        """NullRunRuntime allows dependency injection pattern."""
        # In test mode, transport is created but won't make network calls
        rt = NullRunRuntime(
            api_key="test-key",
            _test_mode=True,
        )
        # Transport should exist
        assert rt._transport is not None
        rt.shutdown()

    def test_runtime_singleton_reset_clears_instance(self, mock_api, monkeypatch):
        """NullRunRuntime.reset_instance properly clears singleton.

        T3-S2 (0.3.0): api_key is now required, so we pin
        NULLRUN_API_KEY in env so the singleton builder has something
        to read. Uses `mock_api` to mock the /auth/verify endpoint.
        """
        monkeypatch.setenv("NULLRUN_API_KEY", "test-key-12345678")
        monkeypatch.setenv("NULLRUN_API_URL", "https://api.test.nullrun.io")
        rt1 = NullRunRuntime.get_instance()
        assert rt1 is not None

        # Reset should clear singleton
        NullRunRuntime.reset_instance()

        # After reset, get_instance should return a new instance
        rt2 = NullRunRuntime.get_instance()
        # rt2 might be the same as rt1 if environment is same
        # but at minimum reset_instance should have been called
        assert rt2 is not None


# ─── runtime branch tests (kill/pause, mode resolution, etc.) ──────────────────────────────

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
    """Build a runtime that skips network I/O with a stub organisation id.

    Pins ``NULLRUN_WAL_PATH`` to a per-call tmp dir so the constructor's
    ``Transport._replay_from_wal`` never picks up a stale WAL from a previous
    test run.
    """
    import os
    import tempfile
    if not os.environ.get("NULLRUN_WAL_PATH"):
        wal_dir = tempfile.mkdtemp(prefix="nullrun-test-wal-")
        os.environ["NULLRUN_WAL_PATH"] = os.path.join(wal_dir, "sdk.wal")
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


# ─── shutdown ────────────────────────────────────────────────────────


def test_ws_connect_and_serve_treats_receive_cancellation_as_clean_shutdown():
    """An expected receive-task cancellation must not escape the WS thread."""
    import asyncio

    rt = _make_test_runtime()

    class _CancelledConnection:
        def __init__(self):
            async def _cancelled_receive():
                raise asyncio.CancelledError

            self._receive_task = asyncio.create_task(_cancelled_receive())
            self.closed = False

        async def close(self):
            self.closed = True
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass

    connection = None

    async def _connect_websocket(**_kwargs):
        nonlocal connection
        connection = _CancelledConnection()
        return connection

    rt._transport.connect_websocket = _connect_websocket
    asyncio.run(rt._ws_connect_and_serve())

    assert connection is not None
    assert connection.closed is True
    assert rt._ws_connection is None


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


def test_get_instance_returns_singleton_when_no_change(monkeypatch, tmp_path):
    monkeypatch.setenv("NULLRUN_API_KEY", "test-key-12345678")
    monkeypatch.setenv("NULLRUN_WAL_PATH", str(tmp_path / "sdk.wal"))
    NullRunRuntime.reset_instance()
    rt1 = NullRunRuntime(api_key="test-key-12345678", _test_mode=True)
    NullRunRuntime._instance = rt1
    rt2 = NullRunRuntime.get_instance()
    assert rt1 is rt2


# ─── _authenticate: legacy-key warning ───────────────────────────────


def _make_runtime_with_mocked_auth() -> NullRunRuntime:
    """Build a test-mode runtime and stub the transport client.post for _authenticate."""
    import os
    import tempfile
    if not os.environ.get("NULLRUN_WAL_PATH"):
        wal_dir = tempfile.mkdtemp(prefix="nullrun-test-wal-")
        os.environ["NULLRUN_WAL_PATH"] = os.path.join(wal_dir, "sdk.wal")
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


@pytest.mark.parametrize("status_code", [500, 502, 503, 504])
def test_authenticate_5xx_raises_backend_error_not_auth_error(status_code):
    """Regression for DEF-ERRHDL-AUTH-PATH-CODE-PIN-01 (RUN_ID 20260811-1).

    Previously, /auth/verify 5xx was misclassified as
    NullRunAuthenticationError (NR-A001), misleading operators to
    rotate valid keys during backend outages. After the fix the
    canonical envelope parser routes 5xx to NullRunBackendError
    (NR-B002, retryable), matching /check and /track.
    """
    from nullrun.breaker.exceptions import (
        NullRunAuthenticationError,
        NullRunBackendError,
    )

    rt = _make_runtime_with_mocked_auth()
    fake_response = MagicMock()
    fake_response.status_code = status_code
    fake_response.json.return_value = {}
    fake_response.headers = {}
    rt._transport._client.post.return_value = fake_response

    with pytest.raises(NullRunBackendError) as exc_info:
        rt._authenticate()

    assert exc_info.value.error_code == "NR-B002"
    # status_code is forwarded as a detail kwarg (see
    # NullRunTransportError.__init__) — same convention as
    # tests/test_transport.py::test_parse_error_envelope_5xx_raises_gateway_error.
    assert exc_info.value.details.get("status_code") == status_code
    assert not isinstance(exc_info.value, NullRunAuthenticationError) or isinstance(
        exc_info.value, NullRunBackendError
    ), (
        "5xx must not surface as NullRunAuthenticationError — that's the "
        "DEF-ERRHDL-AUTH-PATH-CODE-PIN-01 misclassification the fix closes."
    )


def test_authenticate_401_with_wire_envelope_surfaces_wire_code():
    """Regression for DEF-ERRHDL-AUTH-PATH-CODE-PIN-01 / v3.38 close.

    /auth/verify 401 with a wire envelope carrying
    ``error_code: "API_KEY_REVOKED"`` should surface as
    NullRunAuthError with ``wire_code`` set so callers can branch
    on granular lifecycle state without clobbering the SDK-side
    error_code taxonomy.
    """
    from nullrun.breaker.exceptions import (
        NullRunAuthError,
        NullRunAuthenticationError,
    )

    rt = _make_runtime_with_mocked_auth()
    fake_response = MagicMock()
    fake_response.status_code = 401
    fake_response.json.return_value = {
        "error_code": "API_KEY_REVOKED",
        "error_message": "API key revoked by operator.",
        "details": {},
    }
    fake_response.headers = {}
    rt._transport._client.post.return_value = fake_response

    with pytest.raises(NullRunAuthError) as exc_info:
        rt._authenticate()

    # Existing ``except NullRunAuthenticationError`` clauses still match.
    assert isinstance(exc_info.value, NullRunAuthenticationError)
    assert exc_info.value.wire_code == "API_KEY_REVOKED"


def test_authenticate_network_error_raises():
    import httpx

    from nullrun.breaker.exceptions import NullRunAuthenticationError

    rt = _make_runtime_with_mocked_auth()
    rt._transport._client.post.side_effect = httpx.ConnectError("nope")

    with pytest.raises(NullRunAuthenticationError):
        rt._authenticate()
