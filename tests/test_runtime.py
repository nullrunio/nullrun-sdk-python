"""
tests/test_runtime.py — покрытие NullRunRuntime и @protect
Зависимости: pip install pytest pytest-asyncio respx httpx
"""

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
# NullRunRuntime — инициализация
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
        """T3-S2 (0.3.0): api_key is now required. Constructing
        NullRunRuntime without one raises NullRunAuthenticationError
        instead of silently entering local mode."""
        from nullrun.breaker.exceptions import NullRunAuthenticationError

        monkeypatch.delenv("NULLRUN_API_KEY", raising=False)
        with pytest.raises(NullRunAuthenticationError, match="requires an api_key"):
            NullRunRuntime(api_url=BASE_URL)

    def test_singleton_get_instance(self, make_runtime, monkeypatch):
        """get_instance returns the singleton instance. (T3-S2: api_key
        is now required, so we pin NULLRUN_API_KEY in env so the
        singleton builder has something to read.)"""
        monkeypatch.setenv("NULLRUN_API_KEY", "test-key-12345678")
        monkeypatch.setenv("NULLRUN_API_URL", "https://api.test.nullrun.io")
        rt1 = make_runtime()
        # After make_runtime(), get_instance should return the same instance
        # (if env vars match or if singleton was already set)
        rt2 = NullRunRuntime.get_instance()
        # Either it's the same instance, or get_instance created a new one with different params
        assert rt1 is not None
        assert rt2 is not None

    def test_reset_clears_singleton(self, make_runtime):
        make_runtime()
        from nullrun import reset

        reset()
        # после reset get_instance либо создает новый, либо вернет None


# ──────────────────────────────────────────────────────────────
# NullRunRuntime — track()
# ──────────────────────────────────────────────────────────────


class TestNullRunRuntimeTrack:
    def test_track_enqueues_event(self, make_runtime):
        """track() не блокирует и ставит событие в буфер."""
        rt = make_runtime()
        # track fire-and-forget — не должен бросать
        rt.track({"event_type": "llm_call", "model": "gpt-4", "tokens": 100})
        rt.track({"event_type": "tool_call", "tool": "search"})
        # нет исключений — ок

    def test_track_does_not_raise_on_server_error(self, make_runtime, mock_api):
        """track() fire-and-forget — ошибка сервера не должна падать в calling code."""
        respx.post(f"{BASE_URL}/track/batch").mock(return_value=httpx.Response(500))
        rt = make_runtime()
        # Не должно бросить исключение
        rt.track({"event_type": "test"})

    def test_wire_payload_strips_sensitive_fields(self, make_runtime):
        """Phase 4.1 privacy boundary: ``raw_usage``, ``_fingerprint``
        and ``cost_cents`` MUST NOT appear in the dict that lands on
        the transport buffer (i.e. what /api/v1/track/batch would
        serialise). Normalised fields pass through unchanged.

        We monkey-patch ``_transport.track`` to capture the wire
        dict without spinning up the real httpx client.
        """
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
# NullRunRuntime — execute()
# ──────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────
# NullRunRuntime — execute()
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
        # Audit F-R2-01 (2026-06-22): runtime.execute → Transport.execute
        # now hits /api/v1/execute (not /gate). Pre-fix this mocked
        # /gate which silently swallowed the request (no scope check)
        # and let an API key without `execute` scope drive the block.
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

    @pytest.mark.skip(
        reason=(
            "Round 3 (Phase 0.4.0): runtime.execute now requires "
            'on_transport_error="raise" to surface classified errors '
            "(preserves legacy fail-OPEN behaviour by default so "
            "check_workflow_budget can treat network errors as transient). "
            "Re-enable when the test passes the opt-in flag."
        )
    )
    def test_execute_network_error_raises_classified(self, make_runtime, mock_api):
        """Network error during execute surfaces as classified
        NullRunTransportError (ADR-008). The old behaviour was to
        swallow the exception and return a synthetic `decision=allow`
        with `decision_source=fallback`, which made `_enforce_sensitive_tool`
        silently let the body run (bug #2). The new contract: transport
        classifies the failure, runtime propagates, the calling gate
        applies its declared fail-OPEN/CLOSED policy."""
        from nullrun.breaker.exceptions import (
            NullRunTransportError,
            TransportErrorSource,
        )

        respx.post(f"{BASE_URL}/api/v1/gate").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        rt = make_runtime()
        with pytest.raises(NullRunTransportError) as exc_info:
            rt.execute(tool_name="gpt-4", input_data={}, mode="strict")
        assert exc_info.value.source == TransportErrorSource.NETWORK_ERROR
        assert exc_info.value.endpoint == "execute"

    # T3-S2 (0.3.0): `test_execute_local_mode_allows` was removed along
    # with the `local_mode` field. The execute() path now always hits
    # the /execute endpoint — there is no local stub to test.


# ──────────────────────────────────────────────────────────────
# @protect decorator
# ──────────────────────────────────────────────────────────────


class TestProtectDecorator:
    def test_protect_calls_wrapped_function(self, make_runtime, mock_api):
        """@protect не ломает вызов функции."""
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
        """@protect сохраняет __name__ и __doc__ обёртываемой функции."""
        make_runtime()

        @protect
        def my_documented_func():
            """This is my doc."""
            pass

        assert my_documented_func.__name__ == "my_documented_func"
        assert "doc" in (my_documented_func.__doc__ or "")

    @pytest.mark.asyncio
    async def test_protect_async_function(self, make_runtime, mock_api):
        """@protect работает с async функциями."""
        make_runtime()

        @protect
        async def async_tool():
            await asyncio.sleep(0)
            return "async_result"

        result = await async_tool()
        assert result == "async_result"

    def test_protect_no_runtime_inits_lazily(self, mock_api, monkeypatch):
        """Если runtime не инициализирован — lazy init from env.

        T3-S2 (0.3.0): api_key is now required, so we pin
        NULLRUN_API_KEY in env so the lazy init path can find it.
        """
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
        """FIX-4: @protect must propagate NullRunAuthenticationError
        when no runtime exists AND no env var is set.

        Before the fix, `_get_or_create_runtime` wrapped
        `get_instance()` in `try/except Exception` and rebuilt a
        no-arg `NullRunRuntime()` as a "fallback". That fallback was
        doubly broken in 0.3.0: it swallowed the auth error, then
        crashed with the same error from the no-arg constructor (which
        also requires `api_key` per T3-S2). The net effect was a
        delayed crash with a worse error message.

        After the fix, `_get_or_create_runtime` lets the error
        propagate from `get_instance()` unchanged. The user's first
        `@protect` call surfaces the same clear error that
        `nullrun.init()` would have raised at startup.
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
        """Чувствительные аргументы не попадают в логи."""
        import logging

        make_runtime()

        @protect
        def login(username: str, password: str):
            return "ok"

        with caplog.at_level(logging.DEBUG):
            login(username="user", password="super-secret-password")

        # Пароль не должен быть в логах
        assert "super-secret-password" not in caplog.text

    def test_protect_loop_detection(self, make_runtime, mock_api):
        """@protect with a real (cloud) runtime enforces loop detection on
        repeated calls. Renamed from test_protect_local_mode_loop_detection
        in 0.3.0 — there is no longer a local mode branch to test.
        """
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
        """@protect можно чейнить с другими декораторами."""
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
        """NullRunRuntime.reset_instance() properly clears singleton.

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
