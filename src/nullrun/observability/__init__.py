"""
NullRun observability — thread-safe in-process metrics counters
and the Layer-2 error hook registry.

Modules:

  * ``metrics`` (this file) — counter / gauge reporting. Transport
    and runtime modules call into it for thread-safe increments.
  * ``error_hooks`` — the ``nullrun.on_error()`` global hook
    registry. See that module for the Layer-2 design.

Both are reachable as ``nullrun.observability.metrics`` /
``nullrun.observability.error_hooks`` for back-compat. The
metrics singleton lives here (was previously a module-level
constant in ``observability.py``) — moving it into a package
was needed to make room for the ``error_hooks`` submodule.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Any

# Re-export the Layer-2 registry so users can do
# ``from nullrun.observability import ErrorContext`` without
# reaching into the submodule. Also surfaces the
# ``register_hook`` / ``emit_error`` primitives for advanced
# callers (most users go through ``nullrun.on_error``).
from nullrun.observability.error_hooks import (  # noqa: F401
    ErrorContext,
    emit_error,
    has_hooks,
    register_hook,
)

# Re-export the Layer-3 status dataclasses so users can do
# ``from nullrun.observability import NullRunStatus`` without
# reaching into the submodule. The instance is built by
# ``nullrun.status()`` — these are the return-shape primitives.
from nullrun.observability.status import (  # noqa: F401
    NullRunStatus,
    RecentError,
    WorkflowState,
)

# ----------------------------------------------------------------
# SDK Metrics (in-memory, no external dependencies)
# ----------------------------------------------------------------


@dataclass
class TransportMetrics:
    """Transport layer metrics. Reset on reset()."""

    events_enqueued: int = 0
    events_sent: int = 0
    events_dropped: int = 0
    batches_sent: int = 0
    batches_failed: int = 0
    retries_total: int = 0
    circuit_breaker_opens: int = 0
    last_flush_at: float | None = None
    last_error: str | None = None
    # Circuit breaker state transition metrics
    circuit_open_count: int = 0
    circuit_half_open_count: int = 0
    circuit_closed_count: int = 0
    fallback_mode_activations: int = 0
    # Sprint 1.5 (B13): HMAC verification failures on the control
    # plane WebSocket. Pre-fix, a signature mismatch on a signed
    # ``state_change`` / ``key_rotated`` / ``policy_invalidated``
    # message was logged at WARNING and the message was silently
    # dropped — meaning a forged or mis-rotated kill command could
    # be lost without a counter to alert on. The metric here is
    # what a SRE alerts on for "control plane signature integrity".
    hmac_verify_failures_total: int = 0
    # §7.2 #6: separate counter for the timestamp-expired branch
    # of verify_hmac_signature. A spike here is almost always
    # a clock-skew issue (NTP drift, VM resume, container clock
    # jump) rather than a forged packet — operators should
    # investigate date / chrony before suspecting tampering.
    # We split it from hmac_verify_failures_total so the two
    # alert paths can have different runbooks.
    hmac_verify_expired_total: int = 0


@dataclass
class RuntimeMetrics:
    """Runtime layer metrics."""

    track_calls: int = 0
    execute_calls: int = 0
    execute_allowed: int = 0
    execute_blocked: int = 0
    check_calls: int = 0
    cost_limit_exceeded: int = 0
    timeouts: int = 0
    loop_detections: int = 0


class MetricsRegistry:
    """
    Global SDK metrics registry.

    Used for monitoring without external dependencies.
    Can integrate with Prometheus or OpenTelemetry on top.

    Thread-safe: All counter operations use locks to prevent race conditions
    in multi-threaded environments.

    Usage:
        from nullrun.observability import metrics
        print(metrics.transport.events_sent)
        print(metrics.to_dict())

        # Thread-safe increments (preferred over direct +=)
        metrics.inc_transport("events_enqueued")
        metrics.inc_transport("events_sent", 50)
        metrics.inc_runtime("execute_calls")
    """

    def __init__(self) -> None:
        self.transport = TransportMetrics()
        self.runtime = RuntimeMetrics()
        self._lock = Lock()

    # ----------------------------------------------------------------
    # Thread-safe metric increment methods
    # ----------------------------------------------------------------

    def inc_transport(self, field: str, value: int = 1) -> None:
        """Thread-safe increment of transport metric counter.

        Args:
            field: Metric name (e.g., "events_enqueued", "batches_sent")
            value: Amount to increment (default 1)
        """
        with self._lock:
            current = getattr(self.transport, field, 0)
            setattr(self.transport, field, current + value)

    def inc_runtime(self, field: str, value: int = 1) -> None:
        """Thread-safe increment of runtime metric counter.

        Args:
            field: Metric name (e.g., "track_calls", "execute_calls")
            value: Amount to increment (default 1)
        """
        with self._lock:
            current = getattr(self.runtime, field, 0)
            setattr(self.runtime, field, current + value)

    def set_transport(self, field: str, value: Any) -> None:
        """Thread-safe set of transport metric field.

        Args:
            field: Metric field (e.g., "last_error", "last_flush_at")
            value: Value to set
        """
        with self._lock:
            setattr(self.transport, field, value)

    def to_dict(self) -> dict[str, Any]:
        """Export all metrics to dict. Convenient for /health endpoint."""
        with self._lock:
            return {
                "transport": {
                    "events_enqueued": self.transport.events_enqueued,
                    "events_sent": self.transport.events_sent,
                    "events_dropped": self.transport.events_dropped,
                    "batches_sent": self.transport.batches_sent,
                    "batches_failed": self.transport.batches_failed,
                    "retries_total": self.transport.retries_total,
                    "circuit_breaker_opens": self.transport.circuit_breaker_opens,
                    "last_flush_at": self.transport.last_flush_at,
                    "last_error": self.transport.last_error,
                    "circuit_open_count": self.transport.circuit_open_count,
                    "circuit_half_open_count": self.transport.circuit_half_open_count,
                    "circuit_closed_count": self.transport.circuit_closed_count,
                    "fallback_mode_activations": self.transport.fallback_mode_activations,
                    "hmac_verify_failures_total": self.transport.hmac_verify_failures_total,
                    "hmac_verify_expired_total": self.transport.hmac_verify_expired_total,
                },
                "runtime": {
                    "track_calls": self.runtime.track_calls,
                    "execute_calls": self.runtime.execute_calls,
                    "execute_allowed": self.runtime.execute_allowed,
                    "execute_blocked": self.runtime.execute_blocked,
                    "check_calls": self.runtime.check_calls,
                    "cost_limit_exceeded": self.runtime.cost_limit_exceeded,
                    "timeouts": self.runtime.timeouts,
                    "loop_detections": self.runtime.loop_detections,
                },
            }

    def reset(self) -> None:
        """Reset all counters (useful in tests)."""
        with self._lock:
            self.transport = TransportMetrics()
            self.runtime = RuntimeMetrics()


# Global singleton registry
metrics = MetricsRegistry()
