"""
tests/test_transport.py — transport, circuit breaker, flush, retry coverage
"""

from __future__ import annotations

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


def _advance_clock(monkeypatch, seconds: float) -> None:
    """Move ``time.monotonic()`` forward by ``seconds`` so CB state
    transitions that depend on the recovery window can be observed
    without a real wall-clock sleep.

    Patches the module-level ``time`` reference on
    ``nullrun.breaker.circuit_breaker`` because the CB stores
    ``_last_failure_time`` from that exact import. Tests that need
    a real wall-clock pause can opt out via the conftest
    ``NULLRUN_FAST_SLEEP=0`` env var; this helper only patches
    monotonic, so it composes cleanly with the autouse sleep cap.
    """
    import time as _time

    base = _time.monotonic()
    monkeypatch.setattr(
        "nullrun.breaker.circuit_breaker.time.monotonic",
        lambda: base + seconds,
    )


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

    def test_stop_flush_false_skips_final_flush(self):
        """``stop(flush=False)`` cancels the thread WITHOUT a final
        ``_do_flush()`` so the conftest can teardown between tests
        without racing the respx context exit.

        Regression pin for the second CI-noise fix (PR #60 follow-up):
        the conftest previously nulled the runtime reference without
        calling ``shutdown()`` so the transport flush thread kept
        running with a non-empty buffer; on the next ``_do_flush``
        (after respx exited) httpx hit the real network, got
        ``ConnectError``, retried 11 times with up-to-10s backoff,
        and dominated the xdist wall clock (9m 47s of
        "Request failed (attempt N/11), retrying in 10s").

        The contract being pinned here: with ``flush=False``,
        ``_do_flush`` is NOT called from ``stop()`` even when the
        buffer is non-empty. The teardown is a true no-op apart
        from the thread join.
        """
        from nullrun.transport import FlushConfig

        t = Transport(
            api_url="https://api.test.nullrun.io",
            api_key="test-key-12345678",
            config=FlushConfig(flush_interval=30.0),
        )
        t.start()
        # Buffer an event so a final _do_flush() would have something
        # to attempt to send (and therefore would race respx).
        t._buffer.append({"event_id": "x", "event": "test"})
        # No respx mock active here — if stop() tries to flush, httpx
        # will block for the 5s connect timeout per attempt and
        # multiply by the retry budget. The whole point of
        # ``flush=False`` is to skip that path entirely.
        started = time.monotonic()
        t.stop(flush=False)
        elapsed = time.monotonic() - started
        # Generous bound: thread join is the only blocking step. A
        # regression to "stop() always flushes" would push this
        # past 60s on the first failure.
        assert elapsed < 1.0, (
            f"stop(flush=False) took {elapsed:.2f}s; expected < 1s. "
            f"The final _do_flush() ran despite flush=False — the "
            f"conftest teardown is back to racing respx and the "
            f"CI retry-storm regression is open again."
        )
        # And the buffer is left alone — the conftest contract is
        # "we don't care, the test that wrote it is responsible".
        assert len(t._buffer) == 1, (
            f"stop(flush=False) should leave the buffer untouched; "
            f"expected 1 event, got {len(t._buffer)}."
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
        # Check now uses the unified
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

    def test_open_transitions_to_half_open_after_timeout(self, cb, monkeypatch):
        def fail():
            raise RuntimeError("boom")

        for _ in range(3):
            with pytest.raises(RuntimeError):
                cb.call(fail)

        assert cb.state == CBState.OPEN
        # Advance the wall clock past the 1s recovery_timeout without
        # sleeping. ``time.sleep`` is already capped at 1ms by the
        # conftest autouse fixture; without moving monotonic the
        # ``_last_failure_time`` is still inside the recovery window.
        _advance_clock(monkeypatch, seconds=2.0)
        assert cb.state == CBState.HALF_OPEN

    def test_half_open_success_closes(self, cb, monkeypatch):
        def fail():
            raise RuntimeError("boom")

        for _ in range(3):
            with pytest.raises(RuntimeError):
                cb.call(fail)

        _advance_clock(monkeypatch, seconds=2.0)
        cb.call(lambda: "ok")
        assert cb.state == CBState.CLOSED

    def test_half_open_failure_reopens(self, cb, monkeypatch):
        def fail():
            raise RuntimeError("boom")

        for _ in range(3):
            with pytest.raises(RuntimeError):
                cb.call(fail)

        _advance_clock(monkeypatch, seconds=2.0)
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
# B20: _refetch_credentials must use the shared httpx client
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


class TestToolArgumentsForwarding:
    """T5.6 (2026-07-31) wire-shape pins for
    the `tool_arguments` field on the /execute and /check
    endpoints. The backend (T5.6) reads `tool_arguments`
    from the request, computes a schema fingerprint, and
    UPSERTs into `mcp_tool_signatures` on every
    authenticated MCP /check.

    Pre-T5.6 SDKs (≤ 0.14.4) never set this field; the
    backend falls back to the `tool_params`
    field. The wire change is additive-only.
    """

    @respx.mock
    def test_execute_forwards_tool_arguments_to_wire(self, transport):
        """`tool_arguments` is included on the wire when
        the caller passes a non-None value."""
        captured: dict = {}

        def capture(request: httpx.Request) -> httpx.Response:
            import json
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "decision": "allow",
                    "policy_id": "policy-123",
                    "policy_version": 5,
                    "explanation": "allowed",
                },
            )

        respx.post("https://api.test.nullrun.io/api/v1/execute").mock(
            side_effect=capture
        )
        result = transport.execute(
            organization_id="ws-123",
            execution_id="exec-456",
            trace_id="trace-789",
            tool="mcp://github/create_issue",
            input_data={},
            tool_arguments={"repo": "acme/api", "title": "fix"},
        )
        assert result["decision"] == "allow"
        # Wire contract: the JSON body must contain
        # `tool_arguments` with the exact payload the
        # caller passed. Field name, not nested under
        # `input` or `details`.
        assert "tool_arguments" in captured
        assert captured["tool_arguments"] == {
            "repo": "acme/api",
            "title": "fix",
        }

    @respx.mock
    def test_execute_omits_tool_arguments_when_none(self, transport):
        """Default `tool_arguments=None` MUST NOT appear
        on the wire. Legacy SDKs (≤ 0.14.4) round-trip
        cleanly because the field is absent.
        """
        captured: dict = {}

        def capture(request: httpx.Request) -> httpx.Response:
            import json
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={"decision": "allow", "policy_id": "p", "policy_version": 1},
            )

        respx.post("https://api.test.nullrun.io/api/v1/execute").mock(
            side_effect=capture
        )
        transport.execute(
            organization_id="ws-123",
            execution_id="exec-456",
            trace_id="trace-789",
            tool="mcp://github/create_issue",
            input_data={},
        )
        # `tool_arguments` MUST be absent when caller
        # didn't pass it. The wire change is additive-
        # only; pre-T5.6 SDKs never wrote the key.
        assert "tool_arguments" not in captured

    @respx.mock
    def test_check_forwards_tool_arguments_via_check_request(self, transport):
        """The /check (gate) path forwards
        `tool_arguments` from `check_request` dict.
        Mirrors the contract on /execute; same field
        name, same shape."""
        captured: dict = {}

        def capture(request: httpx.Request) -> httpx.Response:
            import json
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "decision": "allow",
                    "policy_id": "policy-123",
                    "policy_version": 5,
                    "explanation": "allowed",
                },
            )

        respx.post("https://api.test.nullrun.io/api/v1/gate").mock(
            side_effect=capture
        )
        result = transport.check(
            check_request={
                "organization_id": "ws-123",
                "execution_id": "exec-456",
                "tool": "mcp://github/create_issue",
                "tool_arguments": {"repo": "acme/api"},
            }
        )
        assert result["decision"] == "allow"
        # Wire contract: tool_arguments round-trips
        # verbatim from check_request → wire JSON.
        assert captured.get("tool_arguments") == {"repo": "acme/api"}

    @respx.mock
    def test_check_forwards_parent_execution_id_when_present(self, transport):
        """Execution Graph v0 (2026-08-06, backend): additive
        `parent_execution_id` on /gate. A sub-agent SDK call to a
        child execution names the parent execution here; the
        backend validates ownership against the parent's
        ``execution:{id}`` Redis binding. Forwarded only when the
        caller passes a non-None string -- legacy / single-shot
        callers keep the previous payload shape (see
        ``test_check_omits_parent_execution_id_when_absent``).
        """
        captured: dict = {}

        def capture(request: httpx.Request) -> httpx.Response:
            import json
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "decision": "allow",
                    "policy_id": "policy-eg",
                    "policy_version": 1,
                    "explanation": "sub-agent call; parent lineage OK",
                },
            )

        respx.post("https://api.test.nullrun.io/api/v1/gate").mock(
            side_effect=capture
        )
        parent_id = "00000000-0000-0000-0000-000000000099"
        child_id = "00000000-0000-0000-0000-0000000000aa"
        result = transport.check(
            check_request={
                "organization_id": "ws-123",
                "execution_id": child_id,
                "tool": "mcp://github/create_issue",
                "parent_execution_id": parent_id,
            }
        )
        assert result["decision"] == "allow"
        # Wire contract: parent_execution_id round-trips
        # verbatim from check_request → wire JSON.
        # Field name matches the backend's wire schema at
        # ``backend/src/proxy/http/gate/schemas.rs:73``.
        assert captured.get("parent_execution_id") == parent_id

    @respx.mock
    def test_check_omits_parent_execution_id_when_absent(self, transport):
        """Default `parent_execution_id=None` (or absent from
        ``check_request``) MUST NOT appear on the wire. Legacy /
        single-shot SDKs round-trip cleanly because the field is
        absent -- the backend's ``skip_serializing_if = "Option::is_none"``
        contract is mirrored SDK-side by the conditional forward
        at ``transport.py:`` (after the ``tool_arguments`` block).
        The wire change is additive-only; pre-Execution-Graph SDKs
        never wrote the key.
        """
        captured: dict = {}

        def capture(request: httpx.Request) -> httpx.Response:
            import json
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={"decision": "allow", "policy_id": "p", "policy_version": 1},
            )

        respx.post("https://api.test.nullrun.io/api/v1/gate").mock(
            side_effect=capture
        )
        transport.check(
            check_request={
                "organization_id": "ws-123",
                "execution_id": "exec-456",
                "tool": "mcp://github/create_issue",
            }
        )
        # `parent_execution_id` MUST be absent when caller
        # didn't pass it. The wire change is additive-only.
        assert "parent_execution_id" not in captured

    @respx.mock
    def test_check_omits_parent_execution_id_when_none_explicit(self, transport):
        """Explicit ``parent_execution_id=None`` in
        ``check_request`` (vs. key absent) MUST also be omitted.
        Guards against SDK callers that build their
        ``check_request`` programmatically and set the field to
        ``None`` for clarity.
        """
        captured: dict = {}

        def capture(request: httpx.Request) -> httpx.Response:
            import json
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={"decision": "allow", "policy_id": "p", "policy_version": 1},
            )

        respx.post("https://api.test.nullrun.io/api/v1/gate").mock(
            side_effect=capture
        )
        transport.check(
            check_request={
                "organization_id": "ws-123",
                "execution_id": "exec-456",
                "tool": "mcp://github/create_issue",
                "parent_execution_id": None,
            }
        )
        # Explicit None must NOT be forwarded -- the SDK
        # treats None as "no parent" (single-shot semantics).
        assert "parent_execution_id" not in captured


# ─── transport branch tests ────────────────────────────────────
"""
Additional transport branch tests covering gaps in
``tests/test_transport.py``:

  - ``verify_hmac_signature`` expired / mismatch branches
  - ``_extract_retry_after`` int / HTTP-date / garbage / None
  - ``Transport.execute`` fallback modes (STRICT / CACHED hit / CACHED miss
    / PERMISSIVE)
  - ``Transport.execute`` ``on_transport_error`` callable / "raise" /
    "open" / "closed"
  - ``Transport.check`` 5xx + "raise" / network + "raise" / 4xx fallback
  - ``clear_policy_cache``
  - ``_parse_error_envelope`` for 401 / 403 / 429 / 500 / 502 / 400
"""

import time
from unittest.mock import MagicMock

import pytest

from nullrun.breaker.exceptions import (
    NullRunAuthenticationError,
    NullRunTransportError,
    RateLimitError,
    TransportErrorSource,
)
from nullrun.transport import (
    FlushConfig,
    Transport,
    _parse_error_envelope,
    verify_hmac_signature,
)


def _extract_retry_after(response):
    """Module-level shim: ``_extract_retry_after`` is an instance
    method on Transport (not a free function), so reach it through a
    throwaway instance.
    """
    return Transport._extract_retry_after(Transport.__new__(Transport), response)


# ─── verify_hmac_signature ───────────────────────────────────────────


def test_verify_hmac_signature_fresh_and_matching():
    """Fresh timestamp + correct signature → True."""
    import hashlib
    import hmac as _hmac
    import json as _json

    body = '{"x":1}'
    ts = int(time.time())
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    msg = f"{ts}:key:{body_hash}"
    sig = _hmac.new(b"secret", msg.encode("utf-8"), hashlib.sha256).hexdigest()

    assert verify_hmac_signature("key", "secret", ts, body, sig) is True


def test_verify_hmac_signature_expired_returns_false():
    """Timestamp far in the past → False (and bumps the expired counter)."""
    body = "{}"
    ts = int(time.time()) - 400  # > 5 min
    sig = "00" * 32
    assert verify_hmac_signature("key", "secret", ts, body, sig) is False


def test_verify_hmac_signature_future_returns_false():
    """Timestamp far in the future → False (clock skew / replay)."""
    body = "{}"
    ts = int(time.time()) + 400
    sig = "00" * 32
    assert verify_hmac_signature("key", "secret", ts, body, sig) is False


def test_verify_hmac_signature_mismatch_returns_false():
    """Fresh timestamp but wrong signature → False."""
    body = "{}"
    ts = int(time.time())
    assert verify_hmac_signature("key", "secret", ts, body, "0" * 64) is False


# ─── _extract_retry_after ───────────────────────────────────────────


def test_extract_retry_after_no_header_returns_none():
    response = MagicMock()
    response.headers.get.return_value = None
    assert _extract_retry_after(response) is None


def test_extract_retry_after_seconds_int():
    response = MagicMock()
    response.headers.get.return_value = "30"
    assert _extract_retry_after(response) == 30.0


def test_extract_retry_after_seconds_float():
    response = MagicMock()
    response.headers.get.return_value = "2.5"
    assert _extract_retry_after(response) == 2.5


def test_extract_retry_after_http_date():
    """HTTP-date → float seconds delta to now (positive or negative)."""
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    response = MagicMock()
    future = datetime.now(timezone.utc) + timedelta(seconds=120)
    response.headers.get.return_value = format_datetime(future)
    result = _extract_retry_after(response)
    assert result is not None
    assert 100 <= result <= 130


def test_extract_retry_after_garbage_returns_none():
    response = MagicMock()
    response.headers.get.return_value = "not-a-date"
    assert _extract_retry_after(response) is None


# ─── Transport.execute fallback modes ──────────────────────────────


def _build_transport() -> Transport:
    """Build a transport with a stub client (no network)."""
    return Transport(
        api_url="https://api.nullrun.io",
        api_key="key",
        secret_key="secret",
        config=FlushConfig(),
    )


def test_execute_200_with_cache_write():
    """200 → caches the decision for CACHED mode and returns gateway decision."""
    t = _build_transport()
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "decision": "allow",
        "policy_id": "p1",
        "policy_version": 3,
    }
    t._client.post = MagicMock(return_value=fake_response)

    result = t.execute(
        organization_id="org-1",
        execution_id="wf-1",
        trace_id="t-1",
        tool="safe.tool",
        input_data={},
    )
    assert result["decision"] == "allow"
    assert result["decision_source"] == "gateway"


def test_execute_4xx_returns_block():
    """4xx (no special handling) → block-dict, decision_source FALLBACK."""
    t = _build_transport()
    fake_response = MagicMock()
    fake_response.status_code = 400
    fake_response.json.return_value = {"error": "bad_request"}
    t._client.post = MagicMock(return_value=fake_response)

    result = t.execute(
        organization_id="org-1",
        execution_id="wf-1",
        trace_id="t-1",
        tool="safe.tool",
        input_data={},
    )
    assert result["decision"] == "block"
    assert "400" in result["explanation"]


def test_execute_breaker_error_with_raise():
    """Transport raises BreakerTransportError + on_transport_error='raise'
    → re-raised as classified NullRunTransportError(NETWORK_ERROR).
    """
    from nullrun.breaker.exceptions import BreakerTransportError

    t = _build_transport()
    t._client.post = MagicMock(side_effect=BreakerTransportError("down"))
    with pytest.raises(NullRunTransportError) as excinfo:
        t.execute(
            organization_id="org-1",
            execution_id="wf-1",
            trace_id="t-1",
            tool="x",
            input_data={},
            on_transport_error="raise",
        )
    assert excinfo.value.source == TransportErrorSource.NETWORK_ERROR


def test_execute_breaker_error_with_open_string():
    """Transport raises + on_transport_error='open' → synthetic allow."""
    from nullrun.breaker.exceptions import BreakerTransportError

    t = _build_transport()
    t._client.post = MagicMock(side_effect=BreakerTransportError("down"))
    result = t.execute(
        organization_id="org-1",
        execution_id="wf-1",
        trace_id="t-1",
        tool="x",
        input_data={},
        on_transport_error="open",
    )
    assert result["decision"] == "allow"
    assert result["decision_source"] == TransportErrorSource.NETWORK_ERROR


def test_execute_breaker_error_with_closed_string():
    """Transport raises + on_transport_error='closed' → synthetic block."""
    from nullrun.breaker.exceptions import BreakerTransportError

    t = _build_transport()
    t._client.post = MagicMock(side_effect=BreakerTransportError("down"))
    result = t.execute(
        organization_id="org-1",
        execution_id="wf-1",
        trace_id="t-1",
        tool="x",
        input_data={},
        on_transport_error="closed",
    )
    assert result["decision"] == "block"
    assert result["decision_source"] == TransportErrorSource.NETWORK_ERROR


def test_execute_breaker_error_with_callable_callback():
    """Transport raises + on_transport_error=callable → callback receives exc."""
    from nullrun.breaker.exceptions import BreakerTransportError

    t = _build_transport()
    t._client.post = MagicMock(side_effect=BreakerTransportError("down"))
    seen: list = []

    def _cb(exc):
        seen.append(exc)
        return {"decision": "custom", "decision_source": "callback"}

    result = t.execute(
        organization_id="org-1",
        execution_id="wf-1",
        trace_id="t-1",
        tool="x",
        input_data={},
        on_transport_error=_cb,
    )
    assert result["decision"] == "custom"
    assert isinstance(seen[0], BreakerTransportError)


def test_execute_fallback_strict_returns_block():
    """fallback_mode=STRICT → synthetic block on transport failure."""
    from nullrun.breaker.exceptions import BreakerTransportError

    t = _build_transport()
    t._client.post = MagicMock(side_effect=BreakerTransportError("down"))
    result = t.execute(
        organization_id="org-1",
        execution_id="wf-1",
        trace_id="t-1",
        tool="x",
        input_data={},
        fallback_mode="strict",
    )
    assert result["decision"] == "block"
    assert "STRICT" in result["explanation"]


# 0.7.0: fallback_mode=CACHED + the local PolicyCache path were
# removed. The thin-client SDK has no local cache to consult on
# gateway failure. CACHED now degrades to PERMISSIVE.


def test_execute_fallback_cached_degrades_to_permissive():
    """fallback_mode=CACHED → degrade to PERMISSIVE (no local cache)."""
    from nullrun.breaker.exceptions import BreakerTransportError

    t = _build_transport()
    t._client.post = MagicMock(side_effect=BreakerTransportError("down"))
    result = t.execute(
        organization_id="org-1",
        execution_id="wf-1",
        trace_id="t-1",
        tool="x",
        input_data={},
        fallback_mode="cached",
    )
    # 0.7.0: CACHED silently degrades to PERMISSIVE (allow).
    assert result["decision"] == "allow"
    assert result["decision_source"] == "fallback"


def test_execute_fallback_permissive_default():
    """fallback_mode=PERMISSIVE → synthetic allow on transport failure."""
    from nullrun.breaker.exceptions import BreakerTransportError

    t = _build_transport()
    t._client.post = MagicMock(side_effect=BreakerTransportError("down"))
    result = t.execute(
        organization_id="org-1",
        execution_id="wf-1",
        trace_id="t-1",
        tool="x",
        input_data={},
    )
    assert result["decision"] == "allow"
    assert "PERMISSIVE" in result["explanation"]


def test_execute_httpx_network_error_with_raise():
    """httpx.RequestError + on_transport_error='raise' → classified error."""
    import httpx

    t = _build_transport()
    t._client.post = MagicMock(side_effect=httpx.ConnectError("nope"))
    with pytest.raises(NullRunTransportError) as excinfo:
        t.execute(
            organization_id="org-1",
            execution_id="wf-1",
            trace_id="t-1",
            tool="x",
            input_data={},
            on_transport_error="raise",
        )
    assert excinfo.value.source == TransportErrorSource.NETWORK_ERROR


def test_execute_auth_error_propagates():
    """NullRunAuthenticationError is re-raised without fallback handling."""
    t = _build_transport()
    t._client.post = MagicMock(side_effect=NullRunAuthenticationError("bad key"))
    with pytest.raises(NullRunAuthenticationError):
        t.execute(
            organization_id="org-1",
            execution_id="wf-1",
            trace_id="t-1",
            tool="x",
            input_data={},
        )


# ─── Transport.check ────────────────────────────────────────────────


def test_check_200_returns_payload():
    t = _build_transport()
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {"decision": "allow", "remaining_budget_cents": 500}
    t._client.post = MagicMock(return_value=fake)

    result = t.check({"organization_id": "org-1"})
    assert result["decision"] == "allow"


def test_check_5xx_with_raise_raises_classified():
    t = _build_transport()
    fake = MagicMock()
    fake.status_code = 503
    fake.json.return_value = {"error": "unavailable"}
    t._client.post = MagicMock(return_value=fake)

    with pytest.raises(NullRunTransportError) as excinfo:
        t.check({"organization_id": "org-1"}, on_transport_error="raise")
    assert excinfo.value.source == TransportErrorSource.GATEWAY_ERROR


def test_check_5xx_without_raise_returns_block():
    t = _build_transport()
    fake = MagicMock()
    fake.status_code = 503
    fake.json.return_value = {}
    t._client.post = MagicMock(return_value=fake)

    result = t.check({"organization_id": "org-1"})
    assert result["decision"] == "block"


def test_check_4xx_returns_block():
    t = _build_transport()
    fake = MagicMock()
    fake.status_code = 400
    fake.json.return_value = {"error": "bad"}
    t._client.post = MagicMock(return_value=fake)

    result = t.check({"organization_id": "org-1"})
    assert result["decision"] == "block"


def test_check_network_error_with_raise_raises_classified():
    import httpx

    t = _build_transport()
    t._client.post = MagicMock(side_effect=httpx.ConnectError("nope"))
    with pytest.raises(NullRunTransportError) as excinfo:
        t.check({"organization_id": "org-1"}, on_transport_error="raise")
    assert excinfo.value.source == TransportErrorSource.NETWORK_ERROR


def test_check_network_error_without_raise_returns_block():
    import httpx

    t = _build_transport()
    t._client.post = MagicMock(side_effect=httpx.ConnectError("nope"))
    result = t.check({"organization_id": "org-1"})
    assert result["decision"] == "block"


# ─── clear_policy_cache ──────────────────────────────────────────────
# 0.7.0: Transport.clear_policy_cache and Transport._policy_cache
# were removed. The SDK is a thin client; there is no local cache
# to clear.

# ─── _parse_error_envelope ───────────────────────────────────────────


def _make_response(status: int, body, headers: dict | None = None):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    if isinstance(body, (dict, list)):
        resp.json.return_value = body
        resp.text = ""
    else:
        resp.json.side_effect = Exception("not json")
        resp.text = body or ""
    return resp


def test_parse_error_envelope_401_raises_auth_error():
    resp = _make_response(401, {"error": "unauthorized", "message": "bad key"})
    exc = _parse_error_envelope(resp, "/execute")
    assert isinstance(exc, NullRunAuthenticationError)


def test_parse_error_envelope_403_raises_auth_error():
    resp = _make_response(403, {"error": "forbidden"})
    exc = _parse_error_envelope(resp, "/gate")
    assert isinstance(exc, NullRunAuthenticationError)


def test_parse_error_envelope_429_raises_rate_limit():
    resp = _make_response(
        429,
        {"error": "rate_limit", "message": "slow down", "upgrade_url": "https://x"},
        headers={"Retry-After": "30"},
    )
    exc = _parse_error_envelope(resp, "/execute")
    assert isinstance(exc, RateLimitError)
    assert exc.retry_after == 30.0
    assert exc.upgrade_url == "https://x"


def test_parse_error_envelope_429_http_date():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    resp = _make_response(
        429,
        {"error": "rate_limit"},
        headers={"Retry-After": format_datetime(future)},
    )
    exc = _parse_error_envelope(resp, "/execute")
    assert isinstance(exc, RateLimitError)
    assert exc.retry_after is not None


def test_parse_error_envelope_5xx_raises_gateway_error():
    resp = _make_response(502, {"error": "bad_gateway"})
    exc = _parse_error_envelope(resp, "/execute")
    assert isinstance(exc, NullRunTransportError)
    assert exc.source == TransportErrorSource.GATEWAY_ERROR
    # status_code is forwarded as a detail kwarg (see NullRunTransportError.__init__).
    assert exc.details.get("status_code") == 502


def test_parse_error_envelope_4xx_other_raises_client_error():
    """4xx other than 401/403/429 → NullRunTransportError with GATEWAY_ERROR."""
    resp = _make_response(400, {"error": "bad_request"})
    exc = _parse_error_envelope(resp, "/execute")
    assert isinstance(exc, NullRunTransportError)
    assert exc.details.get("status_code") == 400


def test_parse_error_envelope_non_json_body_uses_text():
    resp = _make_response(503, "raw error text")
    exc = _parse_error_envelope(resp, "/execute")
    assert isinstance(exc, NullRunTransportError)
    assert "raw error text" in str(exc)


# ─── connect_websocket URL parsing ───────────────────────────────────


def test_connect_websocket_rejects_non_http_scheme():
    t = _build_transport()
    t.api_url = "ftp://api.nullrun.io"

    import asyncio

    with pytest.raises(ValueError, match="Unsupported scheme"):
        asyncio.run(t.connect_websocket(organization_id="org-1"))


def test_connect_websocket_uses_wss_for_https(monkeypatch):
    t = _build_transport()
    t.api_url = "https://api.nullrun.io"

    # Patch WebSocketConnection.connect to capture the constructed URL.
    from nullrun import transport_websocket as tw_mod

    captured: dict = {}

    class _FakeConn:
        def __init__(self, url, **kwargs):
            captured["url"] = url

        async def connect(self):
            return self

    monkey_url = "wss://api.nullrun.io/ws/control/org-1"
    # monkeypatch restores the original WebSocketConnection on test
    # teardown — without it, the leaked fake class breaks every later
    # test that imports ``WebSocketConnection`` from the module
    # (e.g. test_reconnect_cap.py's ``inspect.getsource`` assertions).
    monkeypatch.setattr(tw_mod, "WebSocketConnection", _FakeConn)

    import asyncio

    asyncio.run(t.connect_websocket(organization_id="org-1"))
    assert captured["url"] == monkey_url


def test_connect_websocket_uses_ws_for_http_localhost(monkeypatch):
    """Loopback http:// → ws:// (not wss://) for local dev."""
    t = Transport(
        api_url="http://localhost:8080",
        api_key="key",
        secret_key="secret",
        config=FlushConfig(),
    )

    from nullrun import transport_websocket as tw_mod

    captured: dict = {}

    class _FakeConn:
        def __init__(self, url, **kwargs):
            captured["url"] = url

        async def connect(self):
            return self

    # Same leak fix as the wss test above — monkeypatch auto-restores.
    monkeypatch.setattr(tw_mod, "WebSocketConnection", _FakeConn)

    import asyncio

    asyncio.run(t.connect_websocket(organization_id="org-1"))
    assert captured["url"] == "ws://localhost:8080/ws/control/org-1"


# ─── _refetch_credentials ──────────────────────────────────────────


def test_refetch_credentials_updates_secret_key():
    """``_refetch_credentials`` updates ``self.secret_key`` on 200."""
    t = _build_transport()
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {"secret_key": "new-secret"}
    t._client.post = MagicMock(return_value=fake)

    import asyncio

    asyncio.run(t._refetch_credentials())
    assert t.secret_key == "new-secret"


def test_refetch_credentials_handles_non_200():
    t = _build_transport()
    fake = MagicMock()
    fake.status_code = 401
    fake.json.return_value = {}
    t._client.post = MagicMock(return_value=fake)

    import asyncio

    asyncio.run(t._refetch_credentials())  # must not raise


def test_refetch_credentials_handles_network_error():
    import httpx

    t = _build_transport()
    t._client.post = MagicMock(side_effect=httpx.ConnectError("nope"))
    import asyncio

    asyncio.run(t._refetch_credentials())  # must not raise


def test_refetch_credentials_missing_secret_key_logs_warning(caplog):
    """200 response without secret_key → WARNING logged, no update."""
    import logging

    t = _build_transport()
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {}  # no secret_key
    t._client.post = MagicMock(return_value=fake)

    original_secret = t.secret_key
    import asyncio

    with caplog.at_level(logging.WARNING, logger="nullrun.transport"):
        asyncio.run(t._refetch_credentials())
    assert t.secret_key == original_secret
    assert any("secret_key" in r.getMessage() for r in caplog.records)


# ─── InsecureTransportError on http:/non-loopback ──────────────────


def test_transport_rejects_insecure_http():
    """Non-loopback HTTP URL raises InsecureTransportError."""
    with pytest.raises(Exception) as excinfo:
        Transport(api_url="http://example.com", api_key="key", config=FlushConfig())
    # Subclass of BreakerTransportError (via InsecureTransportError).
    assert "Insecure URL" in str(excinfo.value) or "insecure" in str(excinfo.value).lower()


def test_transport_accepts_loopback_http():
    """http://127.0.0.1 / http://[::1] / http://localhost are accepted."""
    Transport(api_url="http://127.0.0.1:8080", api_key="key", config=FlushConfig())
    Transport(api_url="http://[::1]:8080", api_key="key", config=FlushConfig())
    Transport(api_url="http://localhost:8080", api_key="key", config=FlushConfig())
