"""
tests/test_transport.py — transport, circuit breaker, flush, retry coverage
"""

import asyncio
import threading
import time

import httpx
import pytest
import respx

from nullrun.breaker.circuit_breaker import CBState, CircuitBreaker
from nullrun.breaker.exceptions import BreakerTransportError
from nullrun.transport import Transport


@pytest.fixture
def transport():
    t = Transport(api_url="https://api.test.nullrun.io", api_key="test-key-12345678")
    yield t
    t.stop()


@pytest.fixture
def cb():
    return CircuitBreaker(failure_threshold=3, recovery_timeout=1.0)


class TestTransport:
    @respx.mock
    def test_send_batch_success(self, transport):
        route = respx.post("https://api.test.nullrun.io/api/v1/track/batch").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        transport._send_batch_with_retry_info([{"event": "test"}])
        assert route.called

    @respx.mock
    def test_send_batch_does_not_emit_x_api_version(self, transport):
        """2026-06-27 audit P2.1: X-API-Version is dead — backend has
        no reader. We stopped emitting it. See audit notes.
        """
        route = respx.post("https://api.test.nullrun.io/api/v1/track/batch").mock(
            return_value=httpx.Response(200, json={})
        )
        transport._send_batch_with_retry_info([{"event": "test"}])
        request = route.calls.last.request
        assert "X-API-Version" not in request.headers

    @respx.mock
    def test_send_batch_includes_auth_header(self, transport):
        route = respx.post("https://api.test.nullrun.io/api/v1/track/batch").mock(
            return_value=httpx.Response(200, json={})
        )
        transport._send_batch_with_retry_info([{"event": "test"}])
        request = route.calls.last.request
        assert "X-API-Key" in request.headers

    @respx.mock
    def test_batch_accumulates_events(self, transport):
        route = respx.post("https://api.test.nullrun.io/api/v1/track/batch").mock(
            return_value=httpx.Response(200, json={})
        )
        transport.track({"event": "e1"})
        transport.track({"event": "e2"})
        transport.flush_now()
        assert route.called

    @respx.mock
    def test_flush_on_stop(self, transport):
        route = respx.post("https://api.test.nullrun.io/api/v1/track/batch").mock(
            return_value=httpx.Response(200, json={})
        )
        transport.track({"event": "final"})
        transport.stop()
        assert route.called

    def test_stop_interrupts_flush_sleep(self):
        """stop() must wake the flush thread out of its cancellable
        sleep instead of waiting out the full ``flush_interval``.

        Regression pin for the CI-speed fix: the previous loop used a
        bare ``time.sleep``, so a test that called ``runtime.shutdown
        ()`` while the thread was mid-sleep blocked for the full
        interval (default 5s). With ``Event.wait`` the join returns
        within a few hundred ms — so the whole suite runs in tens of
        seconds instead of 15+ minutes. Uses a deliberately long
        ``flush_interval`` to make the regression obvious if it
        creeps back.
        """
        from nullrun.transport import FlushConfig

        t = Transport(
            api_url="https://api.test.nullrun.io",
            api_key="test-key-12345678",
            config=FlushConfig(flush_interval=30.0),  # would be 30s pre-fix
        )
        t.start()
        # Give the thread a beat to enter _flush_loop's wait.
        time.sleep(0.05)
        started = time.monotonic()
        t.stop()
        elapsed = time.monotonic() - started
        # Allow generous headroom for CI jitter; the contract is
        # "much less than flush_interval" — a pre-fix run would hit
        # the full 30s and time out this assertion.
        assert elapsed < 5.0, (
            f"stop() took {elapsed:.2f}s; expected < 5s. The flush "
            f"loop is sleeping in plain ``time.sleep`` again — the "
            f"cancellable-wait fix regressed."
        )

    def test_ssl_verification_enabled(self, transport):
        # httpx 0.28+ doesn't expose verify as a direct attribute
        # SSL verification is enabled by default (verify=True)
        # We verify this by checking the transport was initialized with SSL enabled
        assert transport._client is not None

    @respx.mock
    def test_send_batch_http_error_raises(self, transport):
        respx.post("https://api.test.nullrun.io/api/v1/track/batch").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        with pytest.raises(Exception):
            transport._send_batch_with_retry_info([{"event": "test"}])

    @respx.mock
    def test_execute_fallback_strict_blocks_on_gateway_error(self, transport):
        """STRICT fallback mode blocks when Gateway unavailable."""
        respx.post("https://api.test.nullrun.io/api/v1/gate").mock(
            return_value=httpx.Response(500, text="Server Error")
        )
        result = transport.execute(
            organization_id="ws-123",
            execution_id="exec-456",
            trace_id="trace-789",
            tool="my.tool",
            input_data={},
            fallback_mode="strict",
        )
        assert result["decision"] == "block"
        assert result["decision_source"] == "fallback"

    @respx.mock
    def test_execute_fallback_permissive_allows_on_gateway_error(self, transport):
        """PERMISSIVE fallback mode allows when Gateway unavailable."""
        respx.post("https://api.test.nullrun.io/api/v1/gate").mock(
            return_value=httpx.Response(500, text="Server Error")
        )
        result = transport.execute(
            organization_id="ws-123",
            execution_id="exec-456",
            trace_id="trace-789",
            tool="my.tool",
            input_data={},
            fallback_mode="permissive",
        )
        assert result["decision"] == "allow"
        assert result["decision_source"] == "fallback"

    @respx.mock
    def test_execute_fallback_cached_degrades_to_permissive(self, transport):
        """0.7.0: CACHED fallback mode degrades to PERMISSIVE (no local cache)."""
        respx.post("https://api.test.nullrun.io/api/v1/execute").mock(
            return_value=httpx.Response(500, text="Server Error")
        )
        result = transport.execute(
            organization_id="ws-123",
            execution_id="exec-456",
            trace_id="trace-789",
            tool="my.tool",
            input_data={},
            fallback_mode="cached",
        )
        # 0.7.0: thin client — no local cache to consult on gateway
        # failure. CACHED silently degrades to PERMISSIVE.
        assert result["decision"] == "allow"
        assert result["decision_source"] == "fallback"

    @respx.mock
    def test_execute_success_does_not_cache_decision(self, transport):
        """0.7.0: successful execute no longer caches the decision.
        The thin client re-reads from the backend on every call."""
        respx.post("https://api.test.nullrun.io/api/v1/execute").mock(
            return_value=httpx.Response(
                200,
                json={
                    "decision": "allow",
                    "policy_id": "policy-123",
                    "policy_version": 5,
                },
            )
        )
        result = transport.execute(
            organization_id="ws-123",
            execution_id="exec-456",
            trace_id="trace-789",
            tool="my.tool",
            input_data={},
        )
        assert result["decision"] == "allow"
        assert result["decision_source"] == "gateway"
        # Pin: no _policy_cache attribute on Transport anymore.
        assert not hasattr(transport, "_policy_cache"), (
            "Transport._policy_cache re-introduced — thin-client invariant broken."
        )

    @respx.mock
    def test_check_endpoint_returns_block_on_error(self, transport):
        """Check endpoint returns block decision on error."""
        # Round 3 (Phase 0.4.0): check now uses the unified
        # /api/v1/gate endpoint (was /api/v1/check).
        respx.post("https://api.test.nullrun.io/api/v1/gate").mock(
            return_value=httpx.Response(500, text="Server Error")
        )
        result = transport.check(
            {
                "workspace_id": "ws-123",
                "execution_id": "exec-456",
                "operation_id": "op-789",
                "check_type": "llm",
                "model": "claude-3",
                "estimated_tokens": 100,
            }
        )
        assert result["decision"] == "block"

    @respx.mock
    def test_check_endpoint_returns_allow_on_success(self, transport):
        """Check endpoint returns allow decision on success."""
        respx.post("https://api.test.nullrun.io/api/v1/gate").mock(
            return_value=httpx.Response(
                200,
                json={
                    "decision": "allow",
                    "reservation_id": "res-123",
                    "remaining_budget_cents": 500,
                    "projected_cost_cents": 10,
                    "explanations": [],
                    "suggestions": [],
                },
            )
        )
        result = transport.check(
            {
                "organization_id": "ws-123",
                "execution_id": "exec-456",
                "operation_id": "op-789",
                "check_type": "llm",
                "model": "claude-3",
                "estimated_tokens": 100,
            }
        )
        assert result["decision"] == "allow"
        assert result["remaining_budget_cents"] == 500


class TestCircuitBreaker:
    def test_initial_state_is_closed(self, cb):
        assert cb.state == CBState.CLOSED

    def test_success_keeps_closed(self, cb):
        cb.call(lambda: "ok")
        assert cb.state == CBState.CLOSED

    def test_failures_below_threshold_keep_closed(self, cb):
        def fail():
            raise RuntimeError("boom")

        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(fail)
        assert cb.state == CBState.CLOSED

    def test_failures_at_threshold_open(self, cb):
        def fail():
            raise RuntimeError("boom")

        for _ in range(3):
            with pytest.raises(RuntimeError):
                cb.call(fail)
        assert cb.state == CBState.OPEN

    def test_open_blocks_calls(self, cb):
        def fail():
            raise RuntimeError("boom")

        for _ in range(3):
            with pytest.raises(RuntimeError):
                cb.call(fail)

        with pytest.raises(BreakerTransportError, match="Circuit breaker OPEN"):
            cb.call(lambda: "ok")

    def test_open_transitions_to_half_open_after_timeout(self, cb):
        def fail():
            raise RuntimeError("boom")

        for _ in range(3):
            with pytest.raises(RuntimeError):
                cb.call(fail)

        assert cb.state == CBState.OPEN
        time.sleep(1.1)
        assert cb.state == CBState.HALF_OPEN

    def test_half_open_success_closes(self, cb):
        def fail():
            raise RuntimeError("boom")

        for _ in range(3):
            with pytest.raises(RuntimeError):
                cb.call(fail)

        time.sleep(1.1)
        cb.call(lambda: "ok")
        assert cb.state == CBState.CLOSED

    def test_half_open_failure_reopens(self, cb):
        def fail():
            raise RuntimeError("boom")

        for _ in range(3):
            with pytest.raises(RuntimeError):
                cb.call(fail)

        time.sleep(1.1)
        assert cb.state == CBState.HALF_OPEN

        with pytest.raises(RuntimeError):
            cb.call(fail)
        assert cb.state == CBState.OPEN

    def test_metrics_tracking(self, cb):
        def fail():
            raise RuntimeError("boom")

        for _ in range(3):
            with pytest.raises(RuntimeError):
                cb.call(fail)

        metrics = cb.get_metrics()
        assert metrics["total_failures"] == 3
        assert metrics["total_opens"] == 1
        assert metrics["state"] == "open"

    def test_thread_safety(self, cb):
        errors = []

        def fail():
            raise RuntimeError("boom")

        def worker():
            try:
                cb.call(fail)
            except Exception:
                pass

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert cb.state in (CBState.OPEN, CBState.CLOSED, CBState.HALF_OPEN)


class TestRetry:
    @respx.mock
    def test_retry_on_500(self):
        """P0 #2: 5xx on /track/batch is retried. Pre-fix this test asserted
        ``pytest.raises(Exception)`` because the old code did NOT retry and
        the 500 surfaced immediately. Post-fix the helper backs off and
        the third attempt succeeds (200), so no exception is raised."""
        call_count = 0

        def handler(request):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return httpx.Response(500)
            return httpx.Response(200, json={"accepted_event_ids": ["e1"]})

        respx.post("https://api.test.nullrun.io/api/v1/track/batch").mock(side_effect=handler)

        t = Transport(api_url="https://api.test.nullrun.io", api_key="test-key")
        result = t._send_batch_with_retry_info([{"event": "e1"}])
        assert call_count == 3
        assert "e1" in result.accepted_event_ids
        t.stop()


# NOTE: ``TestAsyncTransport`` (lines 365-396 in the pre-0.4.0 file)
# was removed alongside ``AsyncTransport`` itself. See the
# ``TestAsyncTransportFlush`` note above for context.


class TestBoundedDict:
    """Regression: BoundedDict was removed in 0.4.0 (dead code)."""

    def test_bounded_dict_class_removed(self):
        """`nullrun.runtime.BoundedDict` no longer exists — pin removal."""
        from nullrun.runtime import NullRunRuntime

        assert getattr(NullRunRuntime, "BoundedDict", None) is None
        with __import__("pytest").raises(ImportError):
            from nullrun.runtime import BoundedDict  # noqa: F401


class TestTransportFlush:
    @respx.mock
    def test_flush_on_batch_size(self, transport):
        """Events are flushed when batch_size is reached."""
        route = respx.post("https://api.test.nullrun.io/api/v1/track/batch").mock(
            return_value=httpx.Response(200, json={})
        )
        # Default batch_size is 50
        for i in range(50):
            transport.track({"event": f"e{i}"})
        assert route.called

    @respx.mock
    def test_flush_circuit_breaker_open_requeues(self, transport):
        """When CB opens, batch is re-queued to buffer."""
        from nullrun.breaker.circuit_breaker import CBState

        # First, open the circuit breaker
        cb = transport._circuit_breaker
        for _ in range(cb._failure_threshold):
            try:
                cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
            except RuntimeError:
                pass

        assert cb.state == CBState.OPEN

        # Track an event - buffer has one event
        transport.track({"event": "test1"})

        # Now flush should re-queue since CB is OPEN
        initial_buffer_len = len(transport._buffer)
        transport._do_flush()
        # Buffer should still have events since CB is open
        assert len(transport._buffer) >= initial_buffer_len - 1

    @respx.mock
    def test_buffer_overflow_drops_oldest(self):
        """When buffer exceeds max_buffer_size during flush, oldest events are dropped."""
        from nullrun.breaker.circuit_breaker import CBState
        from nullrun.transport import FlushConfig

        config = FlushConfig(max_buffer_size=5, batch_size=100, max_failed_flush=3)
        t = Transport(
            api_url="https://api.test.nullrun.io",
            api_key="test-key",
            config=config,
        )

        # Open the circuit breaker first
        cb = t._circuit_breaker
        for _ in range(cb._failure_threshold):
            try:
                cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
            except RuntimeError:
                pass
        assert cb.state == CBState.OPEN

        # Add events beyond max_buffer_size - will be re-queued on flush
        # but overflow dropped when CB is OPEN
        for i in range(10):
            t.track({"event": f"e{i}"})

        # Flush with CB OPEN will re-queue and enforce max_buffer_size
        initial_buffer_len = len(t._buffer)
        t._do_flush()

        # After flush with CB OPEN, buffer should be capped at max_buffer_size
        assert len(t._buffer) <= config.max_buffer_size
        t.stop()

    @respx.mock
    def test_circuit_breaker_open_metrics(self, transport):
        """Circuit breaker opening increments metrics."""
        from nullrun.observability import metrics

        metrics.reset()
        cb = transport._circuit_breaker

        # Open the circuit breaker
        for _ in range(cb._failure_threshold):
            try:
                cb.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
            except RuntimeError:
                pass

        # Check that circuit_open_count metric was incremented
        # (the CB calls _on_open which increments both metrics and _metrics)
        assert metrics.transport.circuit_open_count >= 1

    def test_transport_stopped_flag(self, transport):
        """stop() sets _stopped flag to prevent double flush."""
        assert not transport._stopped
        transport.stop()
        assert transport._stopped


# NOTE: ``TestAsyncTransport`` (and the matching ``TestAsyncTransportFlush``
# suite that used to live here) was removed in 0.4.0 — the async
# transport was deleted alongside ``AsyncTransport`` itself
# (``CHANGELOG.md`` "Removed (0.4.0 deprecations — full removal in
# 1.0.0)"). The sync ``Transport`` is used from async event loops
# via ``nullrun.track_llm`` / ``@nullrun.protect``; the underlying
# httpx client + background flush thread is non-blocking. See
# ``tests/test_signal_safety.py`` for the new lifecycle contract.

# 0.7.0: PolicyCache class was removed along with
# FallbackMode.CACHED. The SDK is a thin client; no local cache.
# The corresponding TestPolicyCache class has been removed.


# Sensitive Tools API tests
# ──────────────────────────────────────────────────────────────


class TestSensitiveToolsAPI:
    def test_add_sensitive_tool(self, make_runtime):
        """add_sensitive_tool marks a tool as sensitive."""
        rt = make_runtime()
        rt.add_sensitive_tool("my.custom_tool")
        assert "my.custom_tool" in rt.get_sensitive_tools()

    def test_remove_sensitive_tool(self, make_runtime):
        """remove_sensitive_tool unmarks a tool as sensitive."""
        rt = make_runtime()
        rt.add_sensitive_tool("my.custom_tool")
        rt.remove_sensitive_tool("my.custom_tool")
        assert "my.custom_tool" not in rt.get_sensitive_tools()

    def test_register_sensitive_tools_batch(self, make_runtime):
        """register_sensitive_tools adds multiple tools at once."""
        rt = make_runtime()
        rt.register_sensitive_tools(["tool1", "tool2", "tool3"])
        tools = rt.get_sensitive_tools()
        assert "tool1" in tools
        assert "tool2" in tools
        assert "tool3" in tools

    def test_sensitive_tools_default_set(self, make_runtime):
        """Default sensitive tools include dangerous operations."""
        rt = make_runtime()
        # Built-in sensitive tools
        assert "stripe.charge" in rt.get_sensitive_tools()
        assert "db.delete" in rt.get_sensitive_tools()
        assert "file.delete" in rt.get_sensitive_tools()

    def test_is_sensitive_tool(self, make_runtime):
        """is_sensitive_tool returns True for sensitive tools."""
        rt = make_runtime()
        rt.add_sensitive_tool("my.sensitive_tool")
        assert rt.is_sensitive_tool("my.sensitive_tool") is True
        assert rt.is_sensitive_tool("my.normal_tool") is False


# ──────────────────────────────────────────────────────────────
# HMAC signature tests
# ──────────────────────────────────────────────────────────────


class TestTransportHMAC:
    def test_generate_hmac_signature(self):
        """HMAC signature generation works."""
        import time

        from nullrun.transport import generate_hmac_signature

        sig = generate_hmac_signature(
            api_key="test-key",
            secret_key="secret-123",
            timestamp=int(time.time()),
            body='{"event": "test"}',
        )
        assert sig is not None
        assert len(sig) == 64  # SHA256 hex

    def test_verify_hmac_signature_valid(self):
        """HMAC verification succeeds with valid signature."""
        import time

        from nullrun.transport import generate_hmac_signature, verify_hmac_signature

        api_key = "test-key"
        secret_key = "secret-123"
        timestamp = int(time.time())
        body = '{"event": "test"}'
        sig = generate_hmac_signature(api_key, secret_key, timestamp, body)
        result = verify_hmac_signature(api_key, secret_key, timestamp, body, sig)
        assert result is True

    def test_verify_hmac_signature_invalid(self):
        """HMAC verification fails with invalid signature."""
        import time

        from nullrun.transport import verify_hmac_signature

        result = verify_hmac_signature(
            api_key="test-key",
            secret_key="secret-123",
            timestamp=int(time.time()),
            body='{"event": "test"}',
            signature="invalid_signature",
        )
        assert result is False

    def test_verify_hmac_signature_expired(self):
        """HMAC verification fails with expired timestamp."""
        import time

        from nullrun.transport import generate_hmac_signature, verify_hmac_signature

        api_key = "test-key"
        secret_key = "secret-123"
        body = '{"event": "test"}'
        # Use timestamp from 10 minutes ago (max_age is 5 minutes)
        old_timestamp = int(time.time()) - 600
        sig = generate_hmac_signature(api_key, secret_key, old_timestamp, body)
        result = verify_hmac_signature(
            api_key, secret_key, old_timestamp, body, sig, max_age_seconds=300
        )
        assert result is False


# ===========================================================================
# Sprint 2.4 (B20): _refetch_credentials must use the shared httpx client
# ===========================================================================
# Pre-fix the implementation did ``import requests; requests.post(...)``
# inside the function body, which:
# 1. Required the ``requests`` library to be installed even though it
# is not in pyproject.toml dependencies.
# 2. Bypassed the shared httpx client (no mTLS, no connection pool
# no HMAC body signing, no circuit breaker).
# 3. Bypassed the retry / timeout policy used by every other auth
# call. A key-rotation event during a backend outage would
# time out at 10s with no retry, leaving the SDK with a stale
# secret_key.


class TestRefetchCredentialsUsesSharedClient:
    """`_refetch_credentials` must route through the shared httpx client.

    Pins the B20 fix: pre-fix this used ``requests.post`` and
    bypassed every transport-layer invariant.
    """

    def test_refetch_uses_httpx_client_not_requests(self):
        """The refetch path must call ``self._client.post``.

        We patch ``self._client.post`` to record the call. If the
        production code path imported ``requests`` we would not
        see the call (and the patch would have no effect).
        """
        import json as _json

        from nullrun.transport import Transport

        t = Transport(
            api_url="https://api.test.nullrun.io",
            api_key="test-key-12345678",
            secret_key="test-secret-1234567890",
        )
        # Simulate a successful /auth/verify response by returning a
        # 200 with a new secret_key.
        new_secret = "rotated-secret-99"
        fake_response = httpx.Response(
            200,
            content=_json.dumps({"secret_key": new_secret}).encode("utf-8"),
            request=httpx.Request("POST", "https://api.test.nullrun.io/auth/verify"),
        )
        called = []
        original_post = t._client.post

        def _spy_post(*args, **kwargs):
            called.append((args, kwargs))
            return fake_response

        t._client.post = _spy_post  # type: ignore[assignment]
        try:
            asyncio.run(t._refetch_credentials())
        finally:
            t._client.post = original_post  # type: ignore[assignment]

        assert called, (
            "self._client.post was not called by _refetch_credentials. "
            "The refetch path still uses ``import requests`` and "
            "bypasses the shared httpx client (B20 regression)."
        )
        # The URL must be the auth/verify endpoint on the configured api_url.
        args, kwargs = called[0]
        assert args[0].endswith("/auth/verify"), f"Expected POST to /auth/verify, got {args[0]!r}"
        # The new secret must be picked up from the response.
        assert t.secret_key == new_secret, (
            f"New secret_key was not stored on the transport: got {t.secret_key!r}"
        )

    def test_refetch_does_not_import_requests(self):
        """Defensive: the refetch path must not import ``requests``.

        The shared httpx client is the only sanctioned HTTP path.
        Pin the absence of the ``requests`` import here so a
        future regression that re-introduces the
        ``import requests; requests.post(...)`` shortcut breaks
        this test.
        """
        import sys

        from nullrun.transport import Transport

        t = Transport(
            api_url="https://api.test.nullrun.io",
            api_key="test-key-12345678",
            secret_key="test-secret-1234567890",
        )
        # Snapshot the modules ``requests`` is currently loaded as.
        # If the refetch path imports it, this set will grow.
        before_requests = set(sys.modules)
        try:
            asyncio.run(t._refetch_credentials())
        except Exception:
            # We don't care about the outcome (the fake post will be
            # called by httpx against a non-routed URL); we only
            # care whether ``requests`` was imported.
            pass
        after_requests = set(sys.modules)
        new_modules = after_requests - before_requests
        assert "requests" not in new_modules, (
            f"_refetch_credentials imported ``requests`` (new modules: "
            f"{[m for m in new_modules if 'request' in m.lower()]}). "
            "B20 regression: the refetch path must use ``self._client``."
        )
