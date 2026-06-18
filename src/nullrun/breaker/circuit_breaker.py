"""
Circuit breaker implementation for NullRun SDK.

Provides a proper three-state circuit breaker (CLOSED/OPEN/HALF_OPEN).
Supports distributed state sharing via Redis for multi-worker deployments.
"""

import asyncio
import logging
import random
import threading
import time
from collections.abc import Callable
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


from nullrun.breaker.exceptions import BreakerTransportError
from nullrun.observability import metrics


class CBState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerMetrics:
    """Metrics for circuit breaker observability."""

    def __init__(self):
        self.circuit_open_count = 0
        self.circuit_half_open_count = 0
        self.circuit_closed_count = 0
        self.total_failure_count = 0
        self.total_success_count = 0
        self.half_open_duration_sum = 0.0
        self.half_open_duration_count = 0
        self.fallback_activations = 0


class CircuitBreaker:
    """
    Full-featured circuit breaker with three states.

    CLOSED -> (failures >= threshold) -> OPEN
    OPEN -> (timeout elapsed) -> HALF_OPEN
    HALF_OPEN -> (success) -> CLOSED
    HALF_OPEN -> (failure) -> OPEN

    Supports distributed state sharing via Redis for multi-worker deployments.
    When one worker opens the circuit, all workers see it via Redis.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
        redis_client: Optional[Any] = None,
        name: str = "default",
    ):
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._half_open_max_calls = half_open_max_calls

        # Redis-based distributed state sharing
        self._redis_client = redis_client
        self._redis_key_prefix = f"cb:{name}:"
        self._state_ttl = 60  # seconds - state expires if not refreshed

        self._state = CBState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float | None = None
        self._opened_at: float | None = None  # Track when circuit last opened
        self._half_open_calls = 0
        self._half_open_start: float | None = None  # Track half-open entry time
        self._lock = threading.Lock()
        self._async_lock: asyncio.Lock | None = None  # Lazily created

        # Metrics
        self._metrics = CircuitBreakerMetrics()
        self.total_failures = 0
        self.total_opens = 0
        self.total_successes = 0

    def _get_async_lock(self) -> asyncio.Lock:
        """Get or create async lock. Must be called from async context."""
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock

    # =============================================================================
    # Redis-based distributed state sharing
    # =============================================================================

    def _check_global_state(self) -> Optional[str]:
        """
        Check if any instance has the circuit open in Redis.

        Returns 'OPEN', 'HALF_OPEN', 'CLOSED', or None if no global state exists.
        """
        if not self._redis_client:
            return None
        try:
            key = f"{self._redis_key_prefix}state"
            state = self._redis_client.get(key)
            return state if state else None
        except Exception as e:
            logger.warning(f"Redis state check failed: {e}")
            return None

    def _check_global_recovered(self) -> bool:
        """
        Check if another instance recovered the circuit (closed it in Redis).

        Returns True if another instance closed the circuit.
        """
        if not self._redis_client:
            return False
        try:
            key = f"{self._redis_key_prefix}state"
            state = self._redis_client.get(key)
            return state == "CLOSED"
        except Exception as e:
            logger.warning(f"Redis recovery check failed: {e}")
            return False

    def _publish_open_state(self) -> None:
        """Publish OPEN state to Redis with TTL."""
        if not self._redis_client:
            return
        try:
            key = f"{self._redis_key_prefix}state"
            self._redis_client.setex(key, self._state_ttl, "OPEN")
        except Exception as e:
            logger.warning(f"Redis publish OPEN state failed: {e}")

    def _publish_half_open_state(self) -> None:
        """Publish HALF_OPEN state to Redis with TTL."""
        if not self._redis_client:
            return
        try:
            key = f"{self._redis_key_prefix}state"
            self._redis_client.setex(key, self._state_ttl, "HALF_OPEN")
        except Exception as e:
            logger.warning(f"Redis publish HALF_OPEN state failed: {e}")

    def _clear_global_state(self) -> None:
        """Clear global state from Redis when circuit closes."""
        if not self._redis_client:
            return
        try:
            key = f"{self._redis_key_prefix}state"
            self._redis_client.delete(key)
        except Exception as e:
            logger.warning(f"Redis clear state failed: {e}")

    # =============================================================================
    # State transition helpers
    # =============================================================================

    def _global_state_allows_call(self) -> bool:
        """
        Check if global Redis state allows this call to proceed.

        If Redis says OPEN, reject immediately.
        If Redis says HALF_OPEN, allow up to half_open_max_calls.
        If Redis says CLOSED or no state, allow the call.
        """
        global_state = self._check_global_state()
        if global_state is None:
            return True  # No global state, local logic applies

        if global_state == "OPEN":
            return False  # Another instance has it open

        if global_state == "HALF_OPEN":
            # Allow if we haven't exhausted our half-open attempts
            with self._lock:
                if self._half_open_calls >= self._half_open_max_calls:
                    return False
            return True

        # global_state == "CLOSED" - another instance recovered, sync local
        with self._lock:
            self._state = CBState.CLOSED
            self._failure_count = 0
        return True

    def _on_state_change(self, old_state: CBState, new_state: CBState) -> None:
        """Record state transition metrics."""
        if new_state == CBState.OPEN:
            metrics.inc_transport("circuit_open_count")
            # Sprint 3 follow-up (B24): also bump the
            # ``circuit_breaker_opens`` global counter on
            # ``TransportMetrics`` (was 0-call). This is the
            # cross-CB-instance counter — the operator alerts
            # on its rate, not on the per-CB ``circuit_open_count``.
            metrics.inc_transport("circuit_breaker_opens")
            self._metrics.circuit_open_count += 1
        elif new_state == CBState.HALF_OPEN:
            metrics.inc_transport("circuit_half_open_count")
            self._metrics.circuit_half_open_count += 1
        elif new_state == CBState.CLOSED and old_state != CBState.CLOSED:
            metrics.inc_transport("circuit_closed_count")
            self._metrics.circuit_closed_count += 1

    def _on_half_open(self) -> None:
        """Record half-open state entry."""
        self._half_open_start = time.monotonic()

    def _on_closed(self) -> None:
        """Record circuit closure and half-open duration."""
        if self._half_open_start:
            duration = time.monotonic() - self._half_open_start
            self._metrics.half_open_duration_sum += duration
            self._metrics.half_open_duration_count += 1
            self._half_open_start = None

    @property
    def state(self) -> CBState:
        # Phase 0.3.1: hold the lock for the whole transition so
        # concurrent threads do not race into HALF_OPEN. The
        # previous version only held the lock for the dict read,
        # which let two workers independently decide they should
        # both probe in HALF_OPEN at the same wall-clock moment.
        # The fix also publishes HALF_OPEN to Redis (was defined
        # but never called) so other workers see the state via
        # ``_check_global_state`` instead of falling back to
        # PERMISSIVE.
        with self._lock:
            if self._state == CBState.OPEN:
                if (
                    self._last_failure_time is not None
                    and time.monotonic() - self._last_failure_time >= self._recovery_timeout
                ):
                    old_state = self._state
                    self._state = CBState.HALF_OPEN
                    self._half_open_calls = 0
                    self._on_state_change(old_state, self._state)
                    self._on_half_open()
                    # Publish the new state so other workers see
                    # HALF_OPEN in Redis and respect
                    # _half_open_max_calls (instead of treating
                    # the local probe as fresh and sending
                    # uncapped traffic).
                    self._publish_half_open_state()
            return self._state

    def call(self, func: Callable[..., Any], *args, **kwargs) -> Any:
        """Execute func through circuit breaker. Supports both sync and async functions."""

        # Check global Redis state first - reject if another instance has it open
        if not self._global_state_allows_call():
            raise BreakerTransportError(
                f"Circuit breaker OPEN (global) -- service unavailable. "
                f"Retry in {self._recovery_timeout:.0f}s"
            )

        # Add jitter before transitioning from OPEN to HALF_OPEN to prevent thundering herd
        if self._state == CBState.OPEN and self._opened_at is not None:
            time_in_open = time.monotonic() - self._opened_at
            if time_in_open >= self._recovery_timeout:
                # Add random jitter (0-30 seconds) to prevent thundering herd
                # Phase 8: cap at 5s (was 30s). The previous value
                # blocked the caller's thread for up to 30s on
                # every OPEN->HALF_OPEN transition. 5s is plenty
                # to spread reconnects across workers.
                jitter = random.uniform(0, 5.0)
                time.sleep(jitter)

        state = self.state

        if state == CBState.OPEN:
            raise BreakerTransportError(
                f"Circuit breaker OPEN -- service unavailable. "
                f"Retry in {self._recovery_timeout:.0f}s"
            )

        if state == CBState.HALF_OPEN:
            with self._lock:
                if self._half_open_calls >= self._half_open_max_calls:
                    raise BreakerTransportError("Circuit breaker HALF_OPEN -- waiting")
                self._half_open_calls += 1

        # Check if func is a coroutine function (async)
        import inspect
        if inspect.iscoroutinefunction(func):
            return self._call_async(func, *args, **kwargs)
        else:
            return self._call_sync(func, *args, **kwargs)

    def _call_sync(self, func: Callable[..., Any], *args, **kwargs) -> Any:
        """Execute sync func through circuit breaker."""
        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception:
            self._on_failure()
            raise

    async def _call_async(self, func: Callable[..., Any], *args, **kwargs) -> Any:
        """Execute async func through circuit breaker."""
        try:
            result = await func(*args, **kwargs)
            await self._on_success_async()
            return result
        except Exception:
            await self._on_failure_async()
            raise

    def _on_success(self) -> None:
        old_state = self._state
        with self._lock:
            self._state = CBState.CLOSED
            self._failure_count = 0
            self.total_successes += 1
            self._metrics.total_success_count += 1

        self._on_state_change(old_state, CBState.CLOSED)
        self._on_closed()

        # Update Redis - clear OPEN state since we recovered
        if self._redis_client:
            self._clear_global_state()

    def _on_failure(self) -> None:
        old_state = self._state
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            self.total_failures += 1
            self._metrics.total_failure_count += 1
            if self._failure_count >= self._failure_threshold:
                if old_state != CBState.OPEN:
                    self.total_opens += 1
                    self._on_state_change(old_state, CBState.OPEN)
                self._state = CBState.OPEN
                self._opened_at = time.monotonic()

        # Publish OPEN state to Redis so other workers see it
        if self._redis_client and self._state == CBState.OPEN:
            self._publish_open_state()

    async def _on_success_async(self) -> None:
        """Async-safe success handler."""
        old_state = self._state
        async_lock = self._get_async_lock()
        async with async_lock:
            self._state = CBState.CLOSED
            self._failure_count = 0
            self.total_successes += 1
            self._metrics.total_success_count += 1

        self._on_state_change(old_state, CBState.CLOSED)
        self._on_closed()

        # Update Redis - clear OPEN state since we recovered
        if self._redis_client:
            self._clear_global_state()

    async def _on_failure_async(self) -> None:
        """Async-safe failure handler."""
        old_state = self._state
        async_lock = self._get_async_lock()
        async with async_lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            self.total_failures += 1
            self._metrics.total_failure_count += 1
            if self._failure_count >= self._failure_threshold:
                if old_state != CBState.OPEN:
                    self.total_opens += 1
                    self._on_state_change(old_state, CBState.OPEN)
                self._state = CBState.OPEN
                self._opened_at = time.monotonic()

        # Publish OPEN state to Redis so other workers see it
        if self._redis_client and self._state == CBState.OPEN:
            self._publish_open_state()

    def get_metrics(self) -> dict:
        return {
            "state": self.state.value,
            "failure_count": self._failure_count,
            "total_failures": self.total_failures,
            "total_opens": self.total_opens,
            "total_successes": self.total_successes,
            "circuit_open_count": self._metrics.circuit_open_count,
            "circuit_half_open_count": self._metrics.circuit_half_open_count,
            "circuit_closed_count": self._metrics.circuit_closed_count,
            "fallback_activations": self._metrics.fallback_activations,
            "avg_half_open_duration": (
                self._metrics.half_open_duration_sum /
                self._metrics.half_open_duration_count
                if self._metrics.half_open_duration_count > 0 else 0
            ),
        }