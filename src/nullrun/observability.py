"""
src/nullrun/observability.py

Structured logging + metrics for production readiness.

Exposes:
  * `get_logger(name)` — a `StructuredLogger` factory that tags every
    log record with a `structured` extra dict for JSON ingest pipelines.
  * `TenantFilter` / `configure_logging_with_tenant_context()` — a
    `logging.Filter` that attaches `organization_id` / `api_key_id`
    to every record so logs can be partitioned per tenant in the
    downstream pipeline. Opt-in: call
    `configure_logging_with_tenant_context()` once at startup.
  * `metrics` — a global `MetricsRegistry` (thread-safe) for SDK
    counters. See `MetricsRegistry.inc_transport` /
    `MetricsRegistry.inc_runtime` / `MetricsRegistry.set_transport`
    for the supported write paths. Direct `metrics.transport.x = N`
    assignment is also supported but bypasses the lock.
  * `timed(logger, event)` — context manager for measuring
    operation time.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Any

# ----------------------------------------------------------------
# Structured Logger
# ----------------------------------------------------------------

class StructuredLogger:
    """
    Logger with JSON-structured format for production.

    Usage:
        logger = StructuredLogger("nullrun.transport")
        logger.info("batch_sent", events=50, duration_ms=12.3)
        logger.error("batch_failed", error="timeout", attempt=2)
    """

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    def _log(self, level: int, event: str, **kwargs: Any) -> None:
        extra = {"structured": {"event": event, **kwargs}}
        self._logger.log(level, event, extra=extra)

    def debug(self, event: str, **kwargs: Any) -> None:
        self._log(logging.DEBUG, event, **kwargs)

    def info(self, event: str, **kwargs: Any) -> None:
        self._log(logging.INFO, event, **kwargs)

    def warning(self, event: str, **kwargs: Any) -> None:
        self._log(logging.WARNING, event, **kwargs)

    def error(self, event: str, **kwargs: Any) -> None:
        self._log(logging.ERROR, event, **kwargs)


def get_logger(name: str) -> StructuredLogger:
    """Logger factory. Use instead of logging.getLogger() in SDK."""
    return StructuredLogger(f"nullrun.{name}")


# ----------------------------------------------------------------
# Tenant Context Filter for Structured Logging
# ----------------------------------------------------------------

class TenantFilter(logging.Filter):
    """Adds tenant context to all log records for structured logging isolation.

    This filter automatically adds org_id, organization_id, and api_key_id
    from the nullrun context to every log record.

    Usage:
        import logging

        # Add filter to root logger
        handler = logging.StreamHandler()
        handler.addFilter(TenantFilter())

        # Or add to specific logger
        logger = logging.getLogger("nullrun.transport")
        logger.addFilter(TenantFilter())

    Tenant fields are pulled from nullrun.context module via ContextVars,
    so they automatically propagate to all log calls within a tenant_context().
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Import here to avoid circular imports
        from nullrun.context import get_api_key_id, get_organization_id

        # Add tenant fields to the record for structured logging.
        # Only the canonical `organization_id` is set; the legacy
        # `org_id` field is gone (was tied to the deprecated
        # `get_org_id()` helper, removed in 0.4.0 alongside the
        # workspace_id → organization_id migration).
        record.organization_id = get_organization_id() or "none"
        record.api_key_id = get_api_key_id() or "none"

        return True


def configure_logging_with_tenant_context() -> None:
    """Configure SDK logging to include tenant context in all log records.

    Call this once at SDK initialization time to enable tenant-isolated logging.

    Usage:
        from nullrun.observability import configure_logging_with_tenant_context

        configure_logging_with_tenant_context()
    """
    # Add TenantFilter to all nullrun loggers
    for logger_name in ["nullrun.transport", "nullrun.runtime", "nullrun.breaker",
                        "nullrun.observability", "nullrun.context"]:
        logger = logging.getLogger(logger_name)
        logger.addFilter(TenantFilter())


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
            field: Metric name (e.g., "track_calls", "execute_allowed")
            value: Amount to increment (default 1)
        """
        with self._lock:
            current = getattr(self.runtime, field, 0)
            setattr(self.runtime, field, current + value)

    def set_transport(self, field: str, value: Any) -> None:
        """Thread-safe set of transport metric field.

        Args:
            field: Metric name (e.g., "last_error", "last_flush_at")
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
                },
                "runtime": {
                    "track_calls": self.runtime.track_calls,
                    "execute_calls": self.runtime.execute_calls,
                    "execute_allowed": self.runtime.execute_allowed,
                    "execute_blocked": self.runtime.execute_blocked,
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


# ----------------------------------------------------------------
# Timer context manager (for logging duration_ms)
# ----------------------------------------------------------------

@contextmanager
def timed(logger: StructuredLogger, event: str, **kwargs: Any) -> Generator[None, None, None]:
    """
    Context manager for measuring operation time.

    Usage:
        with timed(logger, "batch_flush", batch_size=50):
            send_batch(events)
        # Logs: batch_flush duration_ms=12.3 batch_size=50
    """
    start = time.monotonic()
    try:
        yield
        duration_ms = (time.monotonic() - start) * 1000
        logger.info(event, duration_ms=round(duration_ms, 2), **kwargs)
    except Exception as exc:
        duration_ms = (time.monotonic() - start) * 1000
        logger.error(
            f"{event}_error",
            duration_ms=round(duration_ms, 2),
            error=type(exc).__name__,
            detail=str(exc)[:200],
            **kwargs,
        )
        raise