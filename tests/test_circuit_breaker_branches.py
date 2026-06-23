"""
Additional circuit-breaker branch tests covering the gaps left after
``test_cb_halfopen_publish.py`` and ``test_buffer_invariants.py``.

Focuses on:

  - ``_call_async`` happy path and exception paths
  - ``_maybe_apply_open_jitter_sync`` (no-op when not ready, sleep when ready)
  - ``_maybe_apply_open_jitter_async``
  - Redis state branches (``_check_global_state``, ``_publish_open_state``,
    ``_publish_half_open_state``, ``_clear_global_state``,
    ``_global_state_allows_call``)
  - ``get_metrics()`` format
  - ``CircuitBreakerMetrics.__init__`` coverage
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from nullrun.breaker.circuit_breaker import (
    CBState,
    CircuitBreaker,
    CircuitBreakerMetrics,
)

# ─── CircuitBreakerMetrics ───────────────────────────────────────────


def test_metrics_default_initialisation():
    """All counters start at zero."""
    m = CircuitBreakerMetrics()
    assert m.circuit_open_count == 0
    assert m.circuit_half_open_count == 0
    assert m.circuit_closed_count == 0
    assert m.total_failure_count == 0
    assert m.total_success_count == 0
    assert m.half_open_duration_sum == 0.0
    assert m.half_open_duration_count == 0
    assert m.fallback_activations == 0


# ─── _maybe_apply_open_jitter_sync ──────────────────────────────────


def test_open_jitter_sync_no_op_when_state_closed():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.0)
    # State is CLOSED → no-op (don't even read ``_opened_at``).
    with patch("time.sleep") as mock_sleep:
        cb._maybe_apply_open_jitter_sync()
        mock_sleep.assert_not_called()


def test_open_jitter_sync_no_op_when_recovery_not_elapsed():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=30.0)
    cb._state = CBState.OPEN
    cb._opened_at = 0.0
    # State OPEN but recovery_timeout hasn't elapsed → no-op.
    with patch("time.monotonic", return_value=1.0):  # 1s < 30s
        with patch("time.sleep") as mock_sleep:
            cb._maybe_apply_open_jitter_sync()
            mock_sleep.assert_not_called()


def test_open_jitter_sync_sleeps_when_recovery_elapsed():
    """Once recovery_timeout elapsed, sync jitter sleeps up to 5s."""
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.0)
    cb._state = CBState.OPEN
    cb._opened_at = 0.0

    with patch("time.sleep") as mock_sleep:
        cb._maybe_apply_open_jitter_sync()
        mock_sleep.assert_called_once()
        # Sleep must be 0 ≤ t ≤ 5.0 (capped per §7.2 #35).
        args = mock_sleep.call_args.args
        assert 0.0 <= args[0] <= 5.0


@pytest.mark.asyncio
async def test_open_jitter_async_sleeps_when_recovery_elapsed():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.0)
    cb._state = CBState.OPEN
    cb._opened_at = 0.0

    with patch("asyncio.sleep") as mock_sleep:
        await cb._maybe_apply_open_jitter_async()
        mock_sleep.assert_called_once()
        args = mock_sleep.call_args.args
        assert 0.0 <= args[0] <= 5.0


@pytest.mark.asyncio
async def test_open_jitter_async_no_op_when_recovery_not_elapsed():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=30.0)
    cb._state = CBState.OPEN
    cb._opened_at = 0.0

    with patch("time.monotonic", return_value=1.0):
        with patch("asyncio.sleep") as mock_sleep:
            await cb._maybe_apply_open_jitter_async()
            mock_sleep.assert_not_called()


# ─── _call_async ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_call_async_success():
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=30.0)

    async def ok():
        return "result"

    result = await cb.call(ok)
    assert result == "result"
    assert cb.state == CBState.CLOSED


@pytest.mark.asyncio
async def test_call_async_failure():
    """Async failure increments failure_count; opens after threshold."""
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=30.0)

    async def bad():
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await cb.call(bad)
    with pytest.raises(RuntimeError):
        await cb.call(bad)
    # Threshold (2) reached → state transitions to OPEN.
    assert cb.state == CBState.OPEN


@pytest.mark.asyncio
async def test_call_async_success_in_half_open_closes():
    """After OPEN→HALF_OPEN, a successful async probe closes the CB."""
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.0)
    cb._state = CBState.OPEN
    cb._opened_at = 0.0
    cb._last_failure_time = 0.0  # recovery timeout check uses _last_failure_time

    async def ok():
        return "fine"

    # Reading ``.state`` triggers OPEN→HALF_OPEN.
    assert cb.state == CBState.HALF_OPEN
    result = await cb.call(ok)
    assert result == "fine"
    assert cb.state == CBState.CLOSED


# ─── get_metrics ────────────────────────────────────────────────────


def test_get_metrics_format_includes_all_counters():
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=30.0)
    cb._metrics.circuit_open_count = 1
    cb._metrics.circuit_half_open_count = 2
    cb._metrics.circuit_closed_count = 3
    cb.total_failures = 5
    cb.total_opens = 1
    cb.total_successes = 10

    metrics = cb.get_metrics()
    assert metrics["state"] == "closed"
    assert metrics["circuit_open_count"] == 1
    assert metrics["circuit_half_open_count"] == 2
    assert metrics["circuit_closed_count"] == 3
    assert metrics["total_failures"] == 5
    assert metrics["total_opens"] == 1
    assert metrics["total_successes"] == 10


def test_get_metrics_avg_half_open_duration_zero_when_no_data():
    cb = CircuitBreaker()
    metrics = cb.get_metrics()
    assert metrics["avg_half_open_duration"] == 0


def test_get_metrics_avg_half_open_duration_with_data():
    """When half-open has been entered and exited, average is computed."""
    cb = CircuitBreaker()
    cb._metrics.half_open_duration_sum = 6.0
    cb._metrics.half_open_duration_count = 3
    metrics = cb.get_metrics()
    assert metrics["avg_half_open_duration"] == 2.0


# ─── Redis distributed state ────────────────────────────────────────


def test_check_global_state_no_redis_returns_none():
    cb = CircuitBreaker()
    assert cb._check_global_state() is None


def test_check_global_state_with_redis_returns_state():
    cb = CircuitBreaker(name="test_cb_r1")
    cb._redis_client = MagicMock()
    # The SDK reads the value verbatim and compares against string
    # literals in ``_global_state_allows_call``; using a str return
    # mirrors the production redis client's decode behaviour.
    cb._redis_client.get.return_value = "OPEN"
    assert cb._check_global_state() == "OPEN"


def test_check_global_state_redis_returns_empty_string():
    """Empty string from Redis is treated as no global state."""
    cb = CircuitBreaker(name="test_cb_r2")
    cb._redis_client = MagicMock()
    cb._redis_client.get.return_value = ""
    assert cb._check_global_state() is None


def test_check_global_state_redis_error_returns_none(caplog):
    """Redis exceptions are logged at WARNING and the breaker falls back
    to local state without crashing the user's call."""
    import logging

    cb = CircuitBreaker(name="test_cb_r3")
    cb._redis_client = MagicMock()
    cb._redis_client.get.side_effect = ConnectionError("redis down")
    with caplog.at_level(logging.WARNING, logger="nullrun.breaker.circuit_breaker"):
        result = cb._check_global_state()
    assert result is None
    assert any("Redis state check failed" in r.getMessage() for r in caplog.records)


def test_check_global_recovered_returns_true_when_closed_in_redis():
    cb = CircuitBreaker(name="test_cb_r4")
    cb._redis_client = MagicMock()
    cb._redis_client.get.return_value = "CLOSED"
    assert cb._check_global_recovered() is True


def test_check_global_recovered_returns_false_when_open_in_redis():
    cb = CircuitBreaker(name="test_cb_r5")
    cb._redis_client = MagicMock()
    cb._redis_client.get.return_value = "OPEN"
    assert cb._check_global_recovered() is False


def test_check_global_recovered_no_redis_returns_false():
    cb = CircuitBreaker()
    assert cb._check_global_recovered() is False


def test_publish_open_state_writes_to_redis():
    cb = CircuitBreaker(name="test_cb_r6")
    cb._redis_client = MagicMock()
    cb._publish_open_state()
    cb._redis_client.setex.assert_called_once()
    args = cb._redis_client.setex.call_args.args
    assert args[0] == "cb:test_cb_r6:state"
    assert args[1] == 60  # _state_ttl
    assert args[2] == "OPEN"


def test_publish_half_open_state_writes_to_redis():
    cb = CircuitBreaker(name="test_cb_r7")
    cb._redis_client = MagicMock()
    cb._publish_half_open_state()
    cb._redis_client.setex.assert_called_once()
    args = cb._redis_client.setex.call_args.args
    assert args[2] == "HALF_OPEN"


def test_clear_global_state_deletes_redis_key():
    cb = CircuitBreaker(name="test_cb_r8")
    cb._redis_client = MagicMock()
    cb._clear_global_state()
    cb._redis_client.delete.assert_called_once_with("cb:test_cb_r8:state")


# ─── _global_state_allows_call ──────────────────────────────────────


def test_global_state_allows_call_no_redis_returns_true():
    cb = CircuitBreaker()
    assert cb._global_state_allows_call() is True


def test_global_state_allows_call_redis_open_returns_false():
    cb = CircuitBreaker(name="test_cb_g1")
    cb._redis_client = MagicMock()
    cb._redis_client.get.return_value = "OPEN"
    assert cb._global_state_allows_call() is False


def test_global_state_allows_call_redis_closed_syncs_local():
    """Redis says CLOSED → sync local state to CLOSED, allow."""
    cb = CircuitBreaker(name="test_cb_g2")
    cb._redis_client = MagicMock()
    cb._redis_client.get.return_value = "CLOSED"
    cb._state = CBState.OPEN  # local says OPEN
    cb._failure_count = 99
    assert cb._global_state_allows_call() is True
    assert cb._state == CBState.CLOSED
    assert cb._failure_count == 0


def test_global_state_allows_call_redis_half_open_below_cap():
    cb = CircuitBreaker(name="test_cb_g3")
    cb._redis_client = MagicMock()
    cb._redis_client.get.return_value = "HALF_OPEN"
    cb._half_open_calls = 0
    cb._half_open_max_calls = 1
    assert cb._global_state_allows_call() is True


def test_global_state_allows_call_redis_half_open_at_cap():
    cb = CircuitBreaker(name="test_cb_g4")
    cb._redis_client = MagicMock()
    cb._redis_client.get.return_value = "HALF_OPEN"
    cb._half_open_calls = 1
    cb._half_open_max_calls = 1
    assert cb._global_state_allows_call() is False


# ─── call() routes async coroutines ─────────────────────────────────


def test_call_sync_function_via_call_returns_result():
    cb = CircuitBreaker()

    def sync_func():
        return "sync-result"

    result = cb.call(sync_func)
    assert result == "sync-result"


def test_call_sync_failure_increments_failure_count():
    cb = CircuitBreaker(failure_threshold=5)

    def bad():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        cb.call(bad)
    assert cb._failure_count == 1
    assert cb.total_failures == 1


def test_call_sync_failure_opens_circuit():
    cb = CircuitBreaker(failure_threshold=2)

    def bad():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        cb.call(bad)
    with pytest.raises(ValueError):
        cb.call(bad)
    assert cb.state == CBState.OPEN


def test_call_after_open_raises_breaker_transport_error():
    """Once the circuit is OPEN, subsequent calls raise immediately."""
    from nullrun.breaker.exceptions import BreakerTransportError

    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=30.0)

    def bad():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        cb.call(bad)
    # Now OPEN — next call raises BreakerTransportError before invoking func.
    with pytest.raises(BreakerTransportError, match="OPEN"):
        cb.call(lambda: "should not run")