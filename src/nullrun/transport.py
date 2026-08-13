"""
Transport layer for NullRun SDK.

Handles HTTP communication with batching and background flush.
Includes fallback modes for Gateway unavailability.
"""

import hashlib
import hmac
import json
import logging
import os
import random
import tempfile
import threading
import time
import uuid
import weakref
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import httpx

from nullrun.actions import handle_action
from nullrun.breaker.circuit_breaker import CircuitBreaker
from nullrun.breaker.exceptions import (
    BreakerTransportError,
    InsecureTransportError,
    NullRunAuthenticationError,
    NullRunTransportError,
    RateLimitError,
    TransportErrorSource,
)
from nullrun.observability import metrics

if TYPE_CHECKING:
    # Forward-referenced to avoid transport.py ⇄ transport_websocket.py cycle.
    from nullrun.transport_websocket import WebSocketConnection

# OpenTelemetry imports (lazy-loaded to support optional dependency)
try:
    from opentelemetry import trace
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False
    trace = None  # type: ignore[assignment]
    TraceContextTextMapPropagator = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

__api_version__ = "1.0"

# Wire-protocol version handshake. Backend rejects signed POSTs without
# `X-NULLRUN-PROTOCOL: <n>` with 400. Bump must be coordinated with backend
# `proxy::http::gate::protocol` and `/api/v1/capabilities`.
NULLRUN_PROTOCOL_VERSION: int = 3
HEADER_PROTOCOL: str = "X-NULLRUN-PROTOCOL"


def _protocol_header_value() -> str:
    """Return the current wire-protocol version as a string (backend stores u32)."""
    return str(NULLRUN_PROTOCOL_VERSION)


def _emit_for_transport_error(
    err: BaseException,
    stage: str,
    correlation_id: str | None,
    *,
    status_code: int | None = None,
) -> None:
    """Layer 2: fire the on_error hook for transport-level raises. Best-effort, never raises.

    The transport module is stateless, so context is minimal — just
    ``stage`` + ``correlation_id`` + ``status_code``.
    """
    from nullrun.observability.error_hooks import (
        ErrorContext,
        emit_error,
        has_hooks,
    )

    if not has_hooks():
        return
    extra: dict[str, Any] = {}
    if status_code is not None:
        extra["status_code"] = status_code
    emit_error(
        err,
        ErrorContext(
            stage=stage,
            correlation_id=correlation_id,
            extra=extra,
        ),
    )


# =============================================================================
# HMAC Request Signing (Task 11)
# =============================================================================


def generate_hmac_signature(
    api_key: str,
    secret_key: str,
    timestamp: int,
    body: str | bytes,
) -> str:
    """
    Generate HMAC-SHA256 signature for request authentication.

    Signature = HMAC-SHA256(secret_key, timestamp + ":" + api_key + ":" + body_hash)
    Body hash = SHA256(request_body)
    """
    # Accept both ``str`` (legacy callers) and ``bytes`` (canonical wire form).
    body_bytes = body.encode("utf-8") if isinstance(body, str) else body
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    message = f"{timestamp}:{api_key}:{body_hash}"

    signature = hmac.new(
        secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    return signature


def verify_hmac_signature(
    api_key: str,
    secret_key: str,
    timestamp: int,
    body: str | bytes,
    signature: str,
    max_age_seconds: int = 300,
) -> bool:
    """
    Verify HMAC signature from request.

    Args:
        api_key: Client's API key
        secret_key: Client's secret key
        timestamp: Unix timestamp from request
        body: Request body as JSON string or UTF-8 bytes
        signature: HMAC signature to verify
        max_age_seconds: Maximum allowed age of request (default 5 min)

    Returns:
        True if signature is valid and request is fresh
    """
    # Check timestamp freshness
    current_time = int(time.time())
    if abs(current_time - timestamp) > max_age_seconds:
        # Separate counter so SRE can distinguish clock drift from forgeries.
        try:
            from nullrun.observability import metrics

            metrics.inc_transport("hmac_verify_expired_total")
        except Exception:  # noqa: BLE001 — best-effort counter
            pass
        logger.warning(f"Request timestamp too old: {timestamp} vs current {current_time}")
        return False

    # Recompute expected signature
    expected = generate_hmac_signature(api_key, secret_key, timestamp, body)

    # Constant-time comparison to prevent timing attacks
    return hmac.compare_digest(expected, signature)


def _signed_request_body(payload: dict[str, Any]) -> bytes:
    """Serialise a JSON payload to the canonical bytes the HMAC signature is computed over.

    All four signed POST call sites must serialise via this helper and pass
    the result with ``content=body`` to httpx (NOT ``json=...`` — that
    re-serialises with different separators and breaks the HMAC match).
    ``default=str`` accepts Decimal / bytes / datetime / UUID.
    """
    return json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")


# =============================================================================
# Retry with exponential backoff + jitter
# =============================================================================


def _retry_with_backoff(
    func: Callable[[], Any],
    max_retries: int = 10,
    base_delay: float = 0.5,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
    jitter: float = 0.1,
    last_retry_after_seconds: float = 0.0,
    on_transport_error: str | Callable[[Exception], dict[str, Any]] | None = None,
) -> Any:
    """Retry with exponential backoff + jitter; honors Retry-After (429) header.

    Formula (without Retry-After): delay = min(base_delay * backoff_factor^attempt, max_delay)
                                    delay += random.uniform(-jitter * delay, jitter * delay)
    Formula (with Retry-After): actual_delay = min(last_retry_after_seconds, max_delay)
    """
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            result = func()

            if hasattr(result, "status_code"):
                if result.status_code == 401:
                    from nullrun.breaker.exceptions import NullRunAuthError

                    err = NullRunAuthError(
                        "Invalid API key",
                        error_code="NR-A003",
                        user_action=(
                            "The NullRun backend rejected the API key (401). "
                            "Verify it at https://app.nullrun.io/settings/api-keys "
                            "and rotate if it was revoked. The key may also be "
                            "for a different environment (prod vs. staging) — "
                            "check the API_URL vs. where the key was issued."
                        ),
                    )
                    _emit_for_transport_error(
                        err,
                        "execute",
                        result.headers.get("x-correlation-id"),
                        status_code=result.status_code,
                    )
                    raise err
                if result.status_code >= 500 and on_transport_error == "raise":
                    # 5xx is a classified GATEWAY_ERROR. Don't retry; only raise
                    # when caller opted into the typed-error contract.
                    from nullrun.breaker.exceptions import NullRunBackendError

                    err = NullRunBackendError(
                        f"Gateway returned {result.status_code}",
                        endpoint="execute",
                        status_code=result.status_code,
                    )
                    _emit_for_transport_error(
                        err,
                        "execute",
                        result.headers.get("x-correlation-id"),
                        status_code=result.status_code,
                    )
                    raise err
                if result.status_code >= 400:
                    result.raise_for_status()

            return result

        except (BreakerTransportError, NullRunAuthenticationError, NullRunTransportError):
            raise

        except Exception as exc:
            last_exc = exc
            metrics.set_transport("last_error", f"{type(exc).__name__}: {exc}")
            if isinstance(exc, (httpx.TimeoutException, httpx.ConnectTimeout, httpx.ReadTimeout)):
                metrics.inc_transport("timeouts")

            if attempt >= max_retries:
                break

            metrics.inc_transport("retries_total")

            if last_retry_after_seconds > 0:
                actual_delay = min(last_retry_after_seconds, max_delay)
                last_retry_after_seconds = 0.0
                logger.warning(
                    "Request failed (attempt %d/%d), honoring Retry-After %.2fs: %s",
                    attempt + 1,
                    max_retries + 1,
                    actual_delay,
                    type(exc).__name__,
                )
            else:
                delay = min(base_delay * (backoff_factor**attempt), max_delay)
                jitter_amount = delay * jitter
                actual_delay = delay + random.uniform(-jitter_amount, jitter_amount)  # noqa: S311
                actual_delay = max(0.0, actual_delay)
                logger.warning(
                    "Request failed (attempt %d/%d), retrying in %.2fs: %s",
                    attempt + 1,
                    max_retries + 1,
                    actual_delay,
                    type(exc).__name__,
                )

            time.sleep(actual_delay)

    raise BreakerTransportError(f"Request failed after {max_retries + 1} attempts") from last_exc


# =============================================================================
# Fallback Modes (SDK Resilience)
# =============================================================================


class FallbackMode:
    """
    SDK behavior when Gateway is unavailable.

    This is CRITICAL for production - Gateway unavailability should NOT
    block agent execution, but behavior must be defined and logged.
    """

    # Block if Gateway unavailable. v3.53 audit #4 — DEFAULT for
    # ``Transport.execute()`` and ``ExecuteConfig.fallback_mode``.
    # Per CLAUDE.md §4 "DEFAULT: fail-CLOSED для всех enforcement
    # путей", the /execute enforcement path must not silently allow
    # local execution when the policy engine is unreachable.
    STRICT = "strict"
    # Allow if Gateway unavailable, log locally. **Opt-in only** —
    # pass ``fallback_mode=FallbackMode.PERMISSIVE`` explicitly when
    # the caller accepts silent fail-OPEN on the enforcement path.
    # Required for any test / dev harness that intentionally runs
    # without a live policy engine.
    PERMISSIVE = "permissive"


class DecisionSource:
    """
    Where the decision originated - for provenance tracking.
    """

    GATEWAY = "gateway"
    CACHED = "cached"
    FALLBACK = "fallback"
    LOCAL = "local"


@dataclass
class FlushConfig:
    """Configuration for transport flush behavior."""

    batch_size: int = 50
    flush_interval: float = 5.0  # seconds
    # Mirror _retry_with_backoff default.
    max_retries: int = 10
    retry_delay: float = 1.0  # seconds
    max_buffer_size: int = 1000  # Max events before dropping oldest
    max_failed_flush: int = 10  # Circuit breaker: stop trying after this many failures


@dataclass
class ExecuteConfig:
    """Configuration for execute (strict mode) behavior."""

    # Fallback mode when Gateway is unavailable. v3.53 audit #4 —
    # default is STRICT (fail-CLOSED on enforcement) per CLAUDE.md §4.
    # Pre-v3.53 the default was PERMISSIVE which silently allowed
    # local execution on transport failure; that was fail-OPEN on the
    # primary enforcement path (Transport.execute → /api/v1/execute).
    fallback_mode: str = FallbackMode.STRICT
    # Gateway timeout in seconds
    timeout: float = 5.0
    # Max retries for execute calls
    max_retries: int = 10
    # Cache TTL for CACHED mode (seconds)
    cache_ttl: float = 60.0
    # Cache max size
    cache_max_size: int = 10000


class Transport:
    """
    HTTP transport with batching support.

    Features:
    - Non-blocking track calls (append to buffer)
    - Background flush at intervals or when batch_size reached
    - Retry logic for failed requests
    - Thread-safe for sync usage
    - HMAC request signing for secure authentication
    - Distributed circuit breaker via Redis for multi-worker deployments
    """

    def __init__(
        self,
        api_url: str,
        api_key: str | None = None,
        secret_key: str | None = None,
        config: FlushConfig | None = None,
        redis_client: Any = None,
    ):
        self.api_url = api_url.rstrip("/")

        # TLS enforcement: reject non-localhost HTTP. Uses urlparse + ip_address
        # so homograph attacks (e.g. 127.0.0.1.attacker.com) don't slip through
        # a naive startswith("127.") check.
        from ipaddress import ip_address
        from urllib.parse import urlparse

        parsed = urlparse(self.api_url)
        if parsed.scheme == "http":
            host = (parsed.hostname or "").lower()
            allowed = host == "localhost" or host == "::1"
            if not allowed:
                try:
                    addr = ip_address(host)
                    allowed = addr.is_loopback
                except ValueError:
                    allowed = False
            if not allowed:
                raise InsecureTransportError(
                    f"Insecure URL detected: {self.api_url}. "
                    f"HTTP is only allowed for localhost / 127.0.0.0/8 / ::1. "
                    f"Use https:// for production."
                )

        self.api_key = api_key
        self.secret_key = secret_key  # HMAC signing key
        self.config = config or FlushConfig()
        # Allow env-var override of batch size and flush interval.
        if "NULLRUN_BATCH_SIZE" in os.environ:
            try:
                self.config.batch_size = int(os.environ["NULLRUN_BATCH_SIZE"])
            except ValueError:
                logger.warning(
                    "NULLRUN_BATCH_SIZE=%r is not an int; ignoring",
                    os.environ["NULLRUN_BATCH_SIZE"],
                )
        if "NULLRUN_FLUSH_INTERVAL_MS" in os.environ:
            try:
                self.config.flush_interval = int(os.environ["NULLRUN_FLUSH_INTERVAL_MS"]) / 1000.0
            except ValueError:
                logger.warning(
                    "NULLRUN_FLUSH_INTERVAL_MS=%r is not an int; ignoring",
                    os.environ["NULLRUN_FLUSH_INTERVAL_MS"],
                )
        self._buffer: list[dict[str, Any]] = []
        self._in_flight: dict[str, dict[str, Any]] = {}  # event_id -> event for retry dedup
        # RLock so re-entrant acquisition (e.g. test fixtures that hold the
        # lock while calling lock-acquiring methods) doesn't deadlock.
        self._lock = threading.RLock()
        self._flush_thread: threading.Thread | None = None
        self._running = False
        # Cancellable sleep primitive: Event.wait returns immediately when
        # stop() sets the event, so teardown doesn't block for the full
        # flush_interval. Pin: tests/test_transport.py::test_stop_interrupts_flush_sleep.
        self._stop_event = threading.Event()

        # mTLS client certificate support
        # NULLRUN_TLS_CLIENT_CERT and NULLRUN_TLS_CLIENT_KEY env vars for client cert auth
        client_cert_path = os.environ.get("NULLRUN_TLS_CLIENT_CERT")
        client_key_path = os.environ.get("NULLRUN_TLS_CLIENT_KEY")
        ca_cert_path = os.environ.get("NULLRUN_TLS_CA_CERT")  # Optional custom CA

        # Build SSL configuration for mTLS
        # For client cert auth: verify is a CA cert, cert is tuple of (client_cert, client_key)
        verify_cert: bool | str = True
        client_cert: tuple[str, str] | None = None
        if client_cert_path and client_key_path:
            # Client certificate authentication (mTLS)
            client_cert = (client_cert_path, client_key_path)
            verify_cert = ca_cert_path if ca_cert_path else True
            logger.debug(f"mTLS enabled: client_cert={client_cert_path}")
        elif ca_cert_path:
            # Custom CA certificate only (no client cert)
            verify_cert = ca_cert_path
            logger.debug(f"Custom CA configured: ca_cert={ca_cert_path}")

        self._client = httpx.Client(
            timeout=httpx.Timeout(
                connect=5.0,
                read=30.0,
                write=10.0,
                pool=5.0,
            ),
            verify=verify_cert,
            cert=client_cert,
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
                keepalive_expiry=30.0,
            ),
        )
        self._redis_client = redis_client
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=self.config.max_failed_flush,
            recovery_timeout=30.0,
            redis_client=redis_client,
            name="transport",
        )
        self._stopped = False  # Track if stop was called
        # 0.7.0 thin client: no local policy cache. Backend is authoritative.
        _masked = api_key[:8] + "***" if api_key and len(api_key) >= 8 else "***"
        logger.debug(f"Transport initialized: api_url={self.api_url}, api_key={_masked}")

        # OpenTelemetry tracer (lazy-loaded: only if opentelemetry is installed)
        self._tracer = None
        self._propagator = None
        if _OTEL_AVAILABLE:
            self._tracer = trace.get_tracer("nullrun.transport")
            self._propagator = TraceContextTextMapPropagator()

        # Final-flush hook via weakref.finalize — only fires if this Transport
        self._finalizer = weakref.finalize(self, self._atexit_flush_safe)

    @staticmethod
    def _atexit_flush_safe(_self_id: int | None = None) -> None:
        """Weakref finalizer entry point.

        ``weakref.finalize`` calls this with no arguments (``self`` is gone).
        The recommended lifecycle is explicit ``stop()`` or ``with Transport(...)``.
        If neither was used, we log a one-time DEBUG line.
        """
        logger.debug(
            "Transport finalizer fired without explicit stop(); "
            "remaining events may be lost. Use Transport as a context "
            "manager or call stop() explicitly."
        )

    # WAL rotation threshold (default 64 MB). Override via NULLRUN_WAL_MAX_BYTES.
    _WAL_MAX_BYTES_DEFAULT: int = 64 * 1024 * 1024

    @property
    def _wal_max_bytes(self) -> int:
        """Effective WAL rotation threshold."""
        raw = os.environ.get("NULLRUN_WAL_MAX_BYTES", "").strip()
        if not raw:
            return self._WAL_MAX_BYTES_DEFAULT
        try:
            value = int(raw)
            return value if value > 0 else self._WAL_MAX_BYTES_DEFAULT
        except ValueError:
            return self._WAL_MAX_BYTES_DEFAULT

    def _wal_path(self) -> str:
        """Resolve WAL path. Honours ``NULLRUN_WAL_PATH``; defaults to platform tempdir."""
        env_path = os.environ.get("NULLRUN_WAL_PATH")
        if env_path:
            return env_path
        return os.path.join(tempfile.gettempdir(), "nullrun.wal")

    def _rotate_wal_if_needed(self) -> None:
        """Rotate ``<path>`` to ``<path>.1`` if it exceeds the size cap."""
        wal_path = self._wal_path()
        try:
            size = os.path.getsize(wal_path)
        except OSError:
            return
        if size < self._wal_max_bytes:
            return
        rotated = f"{wal_path}.1"
        try:
            os.replace(wal_path, rotated)
            logger.info(
                f"WAL rotated: {wal_path} ({size} bytes) -> {rotated} "
                f"after exceeding cap of {self._wal_max_bytes} bytes"
            )
        except OSError as e:
            logger.warning(f"Failed to rotate WAL {wal_path}: {e}")

    def _persist_to_wal(self) -> None:
        """Persist unflushed events to WAL file for replay on restart."""
        if not self._buffer:
            return
        event_count = len(self._buffer)
        wal_path = self._wal_path()
        self._rotate_wal_if_needed()
        wal_dir = os.path.dirname(wal_path) or "."
        try:
            os.makedirs(wal_dir, exist_ok=True)
        except OSError as e:
            logger.warning(f"Cannot create WAL directory {wal_dir}: {e}")
            return
        tmp_path = f"{wal_path}.tmp.{os.getpid()}"
        try:
            with open(tmp_path, "a") as f:
                for event in self._buffer:
                    # 2026-07-24 (Decimal serialization): same default=str as
                    f.write(json.dumps(event, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, wal_path)
            self._buffer.clear()
            logger.debug(f"Persisted {event_count} events to WAL at {wal_path}")
        except OSError as e:
            logger.warning(f"Failed to persist {event_count} events to WAL: {e}")

    def _replay_from_wal(self) -> None:
        """Replay events from WAL file on startup.

        P1-5b: also drains the rotated ``.wal.1`` (oldest
        surviving recovery window) before the active ``.wal`` so
        a crash between rotation and replay doesn't lose events.
        Both files are removed only after a successful flush.
        """
        events: list[dict[str, Any]] = []
        for candidate in (f"{self._wal_path()}.1", self._wal_path()):
            try:
                with open(candidate) as f:
                    for line in f:
                        try:
                            events.append(json.loads(line.strip()))
                        except json.JSONDecodeError:
                            continue
            except FileNotFoundError:
                continue
            except OSError as e:
                logger.warning(f"Failed to read WAL {candidate}: {e}")
                continue
            try:
                os.remove(candidate)
            except OSError as e:
                logger.warning(f"Failed to remove WAL {candidate}: {e}")
        if events:
            self._buffer.extend(events)
            self._do_flush()
        if events:
            logger.info(f"Replayed {len(events)} events from WAL")

    def track(self, event: dict[str, Any]) -> None:
        """
        Add event to buffer. Non-blocking.

        Events are flushed either when batch_size is reached or
        flush_interval elapses.
        """
        with self._lock:
            # Generate event_id if not provided
            if "event_id" not in event or not event["event_id"]:
                event["event_id"] = str(uuid.uuid4())

            # Store in-flight for retry dedup
            self._in_flight[event["event_id"]] = event

            self._buffer.append(event)
            metrics.inc_transport("events_enqueued")

            if len(self._buffer) >= self.config.batch_size:
                self._do_flush_locked()

    def start(self) -> None:
        """Start background flush thread."""
        if self._running:
            return
        # Replay any events from WAL that were persisted due to previous crash
        self._replay_from_wal()
        self._running = True
        # Clear the stop latch so a previous stop() does not short-circuit
        # the new flush loop on its first sleep.
        self._stop_event.clear()
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()
        logger.info("Transport flush thread started")

    def __enter__(self) -> "Transport":
        """Context-manager entry: start the flush thread and return self.

        Pairs with ``__exit__`` so callers can write
        ``with Transport(...) as t:`` and rely on ``stop `` running
        on the way out. Replaces the manual ``start / stop `` pair
        that was easy to forget in long-running services.
        """
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context-manager exit: stop the flush thread and persist WAL.

        Always stops, regardless of whether the body raised. The
        exception (if any) is NOT swallowed — the caller still sees
        it after the with-block.
        """
        try:
            self.stop()
        except Exception as e:  # noqa: BLE001 — best-effort on context exit
            logger.debug(f"Transport.__exit__: stop() raised: {e}")

    def stop(self, timeout: float = 10.0, flush: bool = True) -> None:
        """Stop background flush thread and flush remaining events.

        Args:
            timeout: max seconds to wait for the flush thread to exit.
            flush: when True (default) the final ``_do_flush()`` and
                ``_persist_to_wal()`` run after the thread joins. When
                False, the thread is cancelled but the buffer is left
                alone. The test conftest uses ``flush=False`` to teardown
                between tests without a final httpx call.
        """
        self._running = False
        self._stopped = True  # Mark as stopped to prevent double flush
        self._stop_event.set()  # Wake flush thread out of its cancellable sleep.
        if self._flush_thread:
            self._flush_thread.join(timeout=timeout)
        if flush:
            self._do_flush()  # Final flush
            self._persist_to_wal()  # WAL any remaining events
        self._client.close()
        if getattr(self, "_finalizer", None) is not None and self._finalizer.alive:
            self._finalizer.detach()
        logger.info("Transport stopped")

    def _flush_loop(self) -> None:
        """Background loop that periodically flushes."""
        while self._running:
            # Event.wait returns True when stop() sets the event (cancel signal).
            cancelled = self._stop_event.wait(timeout=self.config.flush_interval)
            if cancelled:
                break
            if self._running:
                self._do_flush()

    def _do_flush(self) -> None:
        """Perform the actual flush."""
        with self._lock:
            self._do_flush_locked()

    def _do_flush_locked(self) -> None:
        """Flush under lock. Must be called with _lock held."""
        if not self._buffer:
            logger.debug("Buffer empty, skipping flush")
            return

        batch = self._buffer[:]
        self._buffer.clear()
        logger.debug(f"Sending batch of {len(batch)} events")

        # Circuit breaker wrapped send - uses proper 3-state circuit breaker
        def send_batch():
            result = self._send_batch_with_retry_info(batch)
            # Remove accepted events from in-flight
            if result.accepted_event_ids:
                for event in batch:
                    if event.get("event_id") in result.accepted_event_ids:
                        self._in_flight.pop(event.get("event_id"), None)
            logger.debug(f"Flushed {len(batch)} events")
            # Update metrics on successful flush (thread-safe)
            metrics.inc_transport("batches_sent")
            metrics.inc_transport("events_sent", len(batch))
            metrics.set_transport("last_flush_at", time.monotonic())
            return result

        try:
            self._circuit_breaker.call(send_batch)
        except BreakerTransportError:
            logger.warning(f"Circuit breaker OPEN. Batch of {len(batch)} events will be re-queued.")
            # Drop NEWEST non-critical (state_change etc.) so oldest events
            # (incident start, billing-period start) survive — they power
            # monthly rollups. Critical control-plane events are kept.
            available_space = self.config.max_buffer_size - len(self._buffer)
            if available_space < len(batch):
                overflow = len(batch) - available_space
                if overflow > 0:
                    batch = self._drop_newest_with_priority(batch, overflow)
            self._buffer.extend(batch)  # Append to END so oldest events retry first.
            metrics.inc_transport("batches_failed")

    def _drain_batch(self) -> list[dict[str, Any]] | None:
        """Public, lock-acquiring snapshot of the current buffer. Returns ``None`` when empty."""
        with self._lock:
            if not self._buffer:
                return None
            batch = list(self._buffer)
            del self._buffer[:]
            return batch

    # Control-plane events that MUST NOT be dropped on overflow.
    _CRITICAL_EVENT_TYPES = frozenset(
        {
            "state_change",
            "kill_received",
            "policy_invalidated",
            "key_rotated",
        }
    )

    def _drop_newest_with_priority(
        self,
        batch: list[dict[str, Any]],
        overflow: int,
    ) -> list[dict[str, Any]]:
        """Drop ``overflow`` newest non-critical events; keep critical events and oldest.

        Cost-audit invariant: under overflow we keep the OLDEST events
        (incident / billing-period start) — dropping oldest would silently
        break monthly rollups. Never drop critical events at the cost of a
        brief buffer overshoot.
        """
        if overflow <= 0:
            return batch
        kept: list[dict[str, Any]] = []
        dropped = 0
        for event in reversed(batch):
            if dropped < overflow and event.get("type") not in self._CRITICAL_EVENT_TYPES:
                dropped += 1
                continue
            kept.append(event)
        if dropped > 0:
            logger.warning(
                f"buffer overflow: dropped {dropped} newest non-critical "
                f"events (kept {len(kept)}, preserved {len(batch) - len(kept) - dropped} critical)"
            )
            metrics.inc_transport("events_dropped", dropped)
        kept.reverse()
        return kept

    @dataclass
    class SendResult:
        accepted_event_ids: list[str]
        retry_after_ms: float | None = None
        is_policy_limit: bool = False

    def _add_hmac_headers(self, headers: dict[str, str], body: str | bytes) -> None:
        """Add X-Signature-Timestamp + X-Signature headers. No-op if secret_key/api_key missing."""
        if not self.secret_key or not self.api_key:
            return

        timestamp = int(time.time())
        signature = generate_hmac_signature(
            self.api_key,
            self.secret_key,
            timestamp,
            body,
        )

        headers["X-Signature-Timestamp"] = str(timestamp)
        headers["X-Signature"] = signature

    def _build_signed_headers(
        self,
        body: str | bytes | None = None,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Build the canonical signed-headers dict for every signed POST.

        Always includes Content-Type: application/json and X-API-Key (when
        api_key is set). Adds HMAC headers when secret_key is set and a
        body is provided. ``extra`` is merged on top of defaults so callers
        can override Content-Type or add custom headers.
        """
        headers: dict[str, str] = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["X-API-Key"] = self.api_key
            # Backend CSRF middleware bypasses cookie-double-submit when an
            # Authorization header is present (backend/src/auth/csrf.rs).
            # Without this, SDK POSTs hit the "state-changing request without
            # session cookie" branch and get 403, which the SDK silently swallowed.
            headers["Authorization"] = f"Bearer {self.api_key}"
        if body is not None and self.secret_key and self.api_key:
            timestamp = int(time.time())
            signature = generate_hmac_signature(self.api_key, self.secret_key, timestamp, body)
            headers["X-Signature-Timestamp"] = str(timestamp)
            headers["X-Signature"] = signature
        if extra:
            headers.update(extra)
        # Backend rejects signed POSTs without X-NULLRUN-PROTOCOL: 3 with 400.
        headers[HEADER_PROTOCOL] = _protocol_header_value()
        self._inject_trace_context(headers)
        return headers

    def _inject_trace_context(self, headers: dict[str, str]) -> None:
        """
        Inject trace context into request headers (W3C Trace Context format).

        This enables distributed tracing across SDK and backend.
        Uses W3C Trace Context standard for trace_id propagation.
        """
        if not _OTEL_AVAILABLE or not self._propagator:
            return

        carrier: dict[str, str] = {}
        self._propagator.inject(carrier)
        headers.update(carrier)

    def _extract_retry_after(self, response: httpx.Response) -> float | None:
        """Extract Retry-After header value as seconds.

        Handles both:
        - Integer seconds (e.g., "30")
        - HTTP-date format (e.g., "Wed, 21 Oct 2015 07:28:00 GMT")
        """
        retry_after = response.headers.get("Retry-After")
        if not retry_after:
            return None

        # Try parsing as seconds (integer or float)
        try:
            return float(retry_after)
        except ValueError:
            pass

        # Try parsing as HTTP datetime (RFC 7231)
        try:
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(retry_after)
            from datetime import datetime, timezone

            return (dt - datetime.now(timezone.utc)).total_seconds()
        except Exception:
            pass

        return None

    def _send_batch_with_retry_info(self, batch: list[dict[str, Any]]) -> "SendResult":
        """Send batch to server. Returns SendResult with retry info. Wrapped by _retry_with_backoff."""
        logger.debug(f"Sending batch of {len(batch)} events to {self.api_url}/api/v1/track/batch")
        body = _signed_request_body({"events": batch})
        headers = self._build_signed_headers(body=body)

        # Inner function is the unit of retry:
        # * 5xx → retry helper backs off. 429 honors Retry-After.
        # * 4xx (other than 429) → return as-is; these are real client bugs
        #   (auth, payload) and must NOT be retried.
        def _post_batch() -> httpx.Response:
            resp = self._client.post(
                f"{self.api_url}/api/v1/track/batch",
                content=body,
                headers=headers,
            )
            if resp.status_code >= 500 or resp.status_code == 429:
                # raise_for_status turns this into HTTPStatusError; the retry
                # helper wraps that into BreakerTransportError after retries.
                resp.raise_for_status()
            return resp

        max_track_retries = getattr(self, "_track_max_retries", 10)
        response = _retry_with_backoff(
            _post_batch,
            max_retries=max_track_retries,
            base_delay=0.5,
            max_delay=10.0,
            backoff_factor=2.0,
            jitter=0.1,
        )

        # P0: Extract retry_after from response headers or body
        retry_after_seconds: float | None = None
        retry_after_ms: float | None = None
        is_policy_limit = False

        # Check Retry-After header (may be seconds or HTTP-date)
        retry_after_seconds = self._extract_retry_after(response)

        # Check response body for retry info
        try:
            data = response.json()
            # Check for rejection info
            if "rejected" in data and data["rejected"]:
                rejected_info = data["rejected"]
                if isinstance(rejected_info, dict):
                    if "retry_after_ms" in rejected_info:
                        retry_after_ms = rejected_info["retry_after_ms"]
                    if "reason" in rejected_info and rejected_info["reason"] == "policy_limit":
                        is_policy_limit = True
        except Exception:  # noqa: S110
            pass

        # Store for next retry calculation (prefer header seconds, fallback to body ms)
        if retry_after_seconds is not None:
            self._last_retry_after_seconds = retry_after_seconds
            retry_after_ms = retry_after_seconds * 1000
        elif retry_after_ms is not None:
            self._last_retry_after_seconds = retry_after_ms / 1000.0
        else:
            self._last_retry_after_seconds = 0.0
        self._last_failure_policy_limit = is_policy_limit

        # Handle 429 response - extract and store Retry-After before raising
        if response.status_code == 429:
            retry_after = self._extract_retry_after(response)
            if retry_after:
                self._last_retry_after_seconds = retry_after
            response.raise_for_status()
        response.raise_for_status()

        # Process actions from server response. Per-element try/except so one
        # malformed entry doesn't abort the whole loop.
        try:
            data = response.json()
            actions = data.get("actions") or []
            for action in actions:
                try:
                    if not isinstance(action, dict):
                        logger.warning("Skipping non-dict action from /track/batch: %r", action)
                        continue
                    action_type = action.get("type", "")
                    workflow_id = action.get("workflow_id", "unknown")
                    reason = action.get("reason", "")
                    if action_type:
                        handle_action(action_type, workflow_id, reason)
                except Exception as item_err:
                    logger.warning("Skipping malformed action %r: %s", action, item_err)
            for msg in data.get("messages", []) or []:
                logger.info("Backend message: %s", msg)
        except Exception as e:
            logger.warning(f"Failed to process actions: {e}")

        # Return accepted event_ids for retry dedup
        accepted_event_ids = data.get("accepted_event_ids", []) if "data" in locals() else []
        logger.debug(f"Batch track: sent {len(batch)} events")
        return self.SendResult(
            accepted_event_ids=accepted_event_ids,
            retry_after_ms=retry_after_ms,
            is_policy_limit=is_policy_limit,
        )

    def flush_now(self) -> None:
        """Force immediate flush."""
        self._do_flush()

    # =============================================================================
    # Execute (Strict Mode)
    # =============================================================================

    def execute(
        self,
        organization_id: str,
        execution_id: str,
        trace_id: str,
        tool: str,
        input_data: dict[str, Any],
        mode: str = "auto",
        # v3.53 audit #4 — default flipped from PERMISSIVE to STRICT
        # to match CLAUDE.md §4 ("DEFAULT: fail-CLOSED для всех
        # enforcement путей"). /execute is the primary enforcement
        # point (see docstring) — when the gateway is unreachable the
        # body MUST NOT run on a silent local pass. Callers that
        # intentionally want fail-OPEN on this path (dev / test
        # harnesses without a live engine) must opt in by passing
        # ``fallback_mode=FallbackMode.PERMISSIVE`` explicitly.
        fallback_mode: str = FallbackMode.STRICT,
        operation_id: str | None = None,
        approval_id: str | None = None,
        # Typed-impact + digest-bound approval. Forwarded when @sensitive(impact=...)
        # built them so the backend can stamp the approval row with the digest.
        business_impact: dict[str, Any] | None = None,
        action_digest: str | None = None,
        # Tool-call argument bag forwarded on /execute so the gate can compute
        # a schema fingerprint and write it to mcp_tool_signatures.
        tool_arguments: dict[str, Any] | None = None,
        on_transport_error: Callable[[Exception], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Pre-execution policy evaluation via /api/v1/execute (PRIMARY enforcement point).

        The SDK MUST call /api/v1/execute (which checks the ``execute`` scope on
        the API key) rather than /api/v1/gate (advisory, no scope check).
        /api/v1/gate is reserved for budget pre-flight via ``Transport.check``.

        Args:
            organization_id: Organization identifier
            execution_id: Execution identifier
            trace_id: Distributed trace ID
            tool: Tool to execute
            input_data: Tool input
            mode: Execution mode ("auto", "inline", "strict")
            fallback_mode: What to do if Gateway unavailable
            operation_id: Optional idempotency key
            on_transport_error: Optional callback invoked on BreakerTransportError.
                When set, the callback's return value is returned verbatim; otherwise
                the request falls through to fallback_mode. The decorator's
                _enforce_sensitive_tool sets this to convert the error into a
                NullRunBlockedException (fail-CLOSED).

        Returns:
            Dict with:
                - decision: "allow" | "block" | "flag" | "pause" | "require_approval"
                - decision_source: "gateway" | "cached" | "fallback"
                - explanation: Human-readable explanation
                - policy_version: Policy version used
                - decision_context: Context for replay (if available)
        """
        gate_request = {
            "organization_id": organization_id,
            "execution_id": execution_id,
            "trace_id": trace_id,
            "tool": tool,
            "input": input_data,
            "mode": mode,  # Wire-present but unused by backend; kept for compat.
            "operation_id": operation_id or str(uuid.uuid4()),
        }
        if approval_id is not None:
            gate_request["approval_id"] = approval_id
        if business_impact is not None:
            gate_request["business_impact"] = business_impact
        if action_digest is not None:
            gate_request["action_digest"] = action_digest
        if tool_arguments is not None:
            gate_request["tool_arguments"] = tool_arguments

        body = _signed_request_body(gate_request)
        headers = self._build_signed_headers(body=body)

        def do_execute_request() -> httpx.Response:
            return self._client.post(
                f"{self.api_url}/api/v1/execute",
                content=body,
                headers=headers,
                timeout=5.0,
            )

        # Per-instance override so tests/CI can shrink the retry budget.
        max_execute_retries = getattr(self, "_execute_max_retries", 10)
        try:
            response = _retry_with_backoff(
                do_execute_request,
                max_retries=max_execute_retries,
                base_delay=0.5,
                on_transport_error=on_transport_error,
            )

            if response.status_code == 200:
                data = response.json()
                data["decision_source"] = DecisionSource.GATEWAY
                # 0.7.0 thin client: no local policy cache. The next
                return data  # type: ignore[no-any-return]
            elif response.status_code >= 400:
                # 4xx - don't retry, return block
                return {
                    "decision": "block",
                    "decision_source": DecisionSource.FALLBACK,
                    "explanation": f"Gateway returned {response.status_code}",
                    "policy_version": 0,
                }

        except BreakerTransportError as exc:
            # ADR-008: on_transport_error accepts callables AND strings:
            if callable(on_transport_error):
                return on_transport_error(exc)
            if on_transport_error == "raise":
                raise NullRunTransportError(
                    f"Gateway unreachable on /execute: {exc}",
                    source=TransportErrorSource.NETWORK_ERROR,
                    endpoint="execute",
                ) from exc
            if on_transport_error == "open":
                return {
                    "decision": "allow",
                    "decision_source": TransportErrorSource.NETWORK_ERROR,
                    "explanation": f"Gateway unreachable: {exc}",
                    "policy_version": 0,
                }
            if on_transport_error == "closed":
                return {
                    "decision": "block",
                    "decision_source": TransportErrorSource.NETWORK_ERROR,
                    "explanation": f"Gateway unreachable: {exc}",
                    "policy_version": 0,
                }
            pass  # fall through to fallback mode
        except NullRunTransportError:
            raise  # Already classified -- propagate as-is
        except httpx.RequestError as exc:
            if callable(on_transport_error):
                return on_transport_error(exc)
            if on_transport_error == "raise":
                raise NullRunTransportError(
                    f"Network error on /execute: {exc}",
                    source=TransportErrorSource.NETWORK_ERROR,
                    endpoint="execute",
                ) from exc
            raise
        except NullRunAuthenticationError:
            raise  # Don't fall back on auth errors

        # All attempts failed - apply fallback mode.
        metrics.inc_transport("fallback_mode_activations")
        if fallback_mode == FallbackMode.STRICT:
            return {
                "decision": "block",
                "decision_source": DecisionSource.FALLBACK,
                "explanation": "Gateway unavailable, fallback=STRICT",
                "policy_version": 0,
            }
        else:  # PERMISSIVE (opt-in)
            # v3.53 audit #4 — PERMISSIVE no longer the default; it
            # requires the caller to pass fallback_mode=FallbackMode.
            # PERMISSIVE explicitly. Synthesizes an allow + decision_
            # source=FALLBACK so the caller / @sensitive decorator can
            # still observe that the engine was unreachable.
            return {
                "decision": "allow",
                "decision_source": DecisionSource.FALLBACK,
                "explanation": "Gateway unavailable, fallback=PERMISSIVE",
                "policy_version": 0,
            }

    def check(
        self,
        check_request: dict[str, Any],
        on_transport_error: Callable[[Exception], dict[str, Any]] | str | None = None,
        parent_execution_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Call /api/v1/gate endpoint for pre-execution budget checking.

        Uses the unified gate endpoint with check_type for budget validation.
        Supports idempotency via operation_id field.

        Args:
            check_request: Dict with:
                - organization_id: Organization identifier
                - execution_id: Execution identifier
                - operation_id: Operation identifier (for idempotency)
                - check_type: "llm" or "tool"
                - model: Model name (for LLM checks)
                - tool_name: Tool name (for tool checks)
                - estimated_tokens: Token count (for LLM checks)
                - input: Optional input data

        Returns:
            Dict with:
                - decision: "allow" | "block" | "throttle"
                - reservation_id: Optional reservation ID
                - remaining_budget_cents: Remaining budget
                - projected_cost_cents: Projected cost for this operation
                - explanations: List of explanation strings
                - suggestions: List of suggestion strings
        """
        # Convert check_request to gate_request format
        gate_request = {
            "organization_id": check_request.get("organization_id"),
            "execution_id": check_request.get("execution_id"),
            "trace_id": check_request.get("trace_id", str(uuid.uuid4())),
            "tool": check_request.get("tool_name") or check_request.get("tool"),
            "input": check_request.get("input"),
            "mode": "auto",
            "check_type": check_request.get("check_type"),
            "model": check_request.get("model"),
            "estimated_tokens": check_request.get("estimated_tokens"),
            "operation_id": check_request.get("operation_id") or str(uuid.uuid4()),
            # Forward the per-call `tools` list so the backend's
            # `gate/internal.rs::check_tool_block` can match each
            # tool against the workflow's effective `blocked_tools`
            # aggregate. When unset (None) we omit the key entirely
            # -- the backend distinguishes "no tools sent" from
            # "explicit []".
            **({"tools": check_request["tools"]} if "tools" in check_request else {}),
        }

        # Wire-protocol v3 fields. Forwarded only when present so
        if check_request.get("chain_id") is not None:
            gate_request["chain_id"] = check_request["chain_id"]
        if check_request.get("chain_op") is not None:
            gate_request["chain_op"] = check_request["chain_op"]
        if check_request.get("idempotency_key") is not None:
            gate_request["idempotency_key"] = check_request["idempotency_key"]
        if "stream" in check_request:
            gate_request["stream"] = bool(check_request["stream"])
        # Forward the `tool_arguments` bag alongside `tool` so
        # the gate can hash it via `signature::compute_schema_hash`
        # and write the fingerprint into `mcp_tool_signatures`.
        # Legacy SDKs never set this; the backend's gate falls
        # back to `tool_params` when the field is missing, so
        # legacy callers do not regress. The shape is
        # `Optional[dict[str, Any]]` -- the backend
        # canonicalises the JSON before hashing, so field
        # ordering inside the dict does not affect the
        # fingerprint.
        if "tool_arguments" in check_request and check_request["tool_arguments"] is not None:
            gate_request["tool_arguments"] = check_request["tool_arguments"]
        # Execution Graph v0 (2026-08-06, backend): additive
        _parent_execution_id = check_request.get("parent_execution_id", parent_execution_id)
        if _parent_execution_id is not None:
            gate_request["parent_execution_id"] = _parent_execution_id

        # 2026-07-02 (v0.11.0 refactor): route through the canonical
        body = _signed_request_body(gate_request)
        headers = self._build_signed_headers(body=body)

        try:
            response = self._client.post(
                f"{self.api_url}/api/v1/gate",
                content=body,
                headers=headers,
                timeout=5.0,
            )

            if response.status_code == 200:
                return response.json()  # type: ignore[no-any-return]
            else:
                # 4xx always -> synthetic block. 5xx only raises when
                # the caller opted into the typed-error contract via
                # on_transport_error="raise"; otherwise it's also a
                # synthetic block (legacy behaviour).
                if response.status_code >= 500 and on_transport_error == "raise":
                    raise NullRunTransportError(
                        f"Gateway returned {response.status_code}",
                        source=TransportErrorSource.GATEWAY_ERROR,
                        endpoint="check",
                        status_code=response.status_code,
                    )
                return {
                    "decision": "block",
                    "decision_source": DecisionSource.FALLBACK,
                    "reservation_id": None,
                    "remaining_budget_cents": 0,
                    "projected_cost_cents": 0,
                    "explanations": [f"Gate endpoint returned {response.status_code}"],
                    "suggestions": ["Check API availability"],
                }
        except httpx.RequestError as e:
            # Classify network errors. By default fall through
            # to synthetic block (legacy); raise only when the
            # caller opted in via on_transport_error="raise".
            if on_transport_error == "raise":
                raise NullRunTransportError(
                    f"Network error on /check: {e}",
                    source=TransportErrorSource.NETWORK_ERROR,
                    endpoint="check",
                ) from e
            logger.warning(f"Gate request failed: {e}")
            return {
                "decision": "block",
                "decision_source": DecisionSource.FALLBACK,
                "reservation_id": None,
                "remaining_budget_cents": 0,
                "projected_cost_cents": 0,
                "explanations": [f"Gate request failed: {e}"],
                "suggestions": ["Check API availability"],
            }

    # =============================================================================
    # WebSocket Connection
    # =============================================================================

    async def connect_websocket(
        self,
        organization_id: str,
        on_state_change: Callable[[dict[str, Any]], None] | None = None,
        on_policy_invalidated: Callable[[str, str, int], None] | None = None,
        on_key_rotated: Callable[[str, str, int], None] | None = None,
        on_approval_resolved: Callable[[dict[str, Any]], None] | None = None,
    ) -> "WebSocketConnection":
        """
        Connect to WebSocket control plane for real-time workflow state updates.

        This replaces polling GET /status/{workflow_id} with WebSocket push.
        When the workflow state changes (KILL/PAUSE), the server pushes the update.

        Args:
            organization_id: Organization identifier
            on_state_change: Optional callback for state change notifications
            on_policy_invalidated: Optional callback for policy cache invalidation.
                                  When called, clears local policy cache so next
                                  gate/execute fetches fresh policy from backend.
                                  Args: (organization_id, policy_id, new_version)
            on_key_rotated: Optional callback for HMAC key rotation.
                           When called, should re-fetch secret_key from /auth/verify.
                           Args: (organization_id, key_id, new_version)

        Returns:
            WebSocketConnection instance

        Raises:
            ConnectionError: If WebSocket connection fails
        """
        # Build the WS URL via urllib.parse instead of string
        # replace. Reject unknown schemes with a clear error.
        from urllib.parse import urlparse, urlunparse

        from nullrun.transport_websocket import WebSocketConnection

        parsed = urlparse(self.api_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"Unsupported scheme for control plane: {parsed.scheme!r}")
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        ws_url = urlunparse(
            parsed._replace(
                scheme=ws_scheme,
                path=f"/ws/control/{organization_id}",
                params="",
                query="",
                fragment="",
            )
        )

        # WS upgrade is a GET-with-no-body so the signed-headers helper (which
        # adds HMAC for the body) does not fit. Use the GET helper instead —
        # same Content-Type + X-API-Key + Authorization + X-NULLRUN-PROTOCOL
        # + trace context shape, no HMAC. The backend's protocol middleware
        # runs on the WS upgrade path too, so the header is mandatory here.
        headers = self._auth_headers_for_get()

        # 0.7.0 thin client: no local policy cache; the next /gate or /execute
        # call re-reads from the backend. Just forward the notification.
        async def wrapped_policy_invalidated(ws_id: str, policy_id: str, new_version: int) -> None:
            logger.info(f"Policy {policy_id} invalidated (v{new_version})")
            if on_policy_invalidated:
                on_policy_invalidated(ws_id, policy_id, new_version)

        async def wrapped_key_rotated(ws_id: str, key_id: str, new_version: int) -> None:
            logger.info(f"Key {key_id} rotated (v{new_version}), re-fetching credentials")
            await self._refetch_credentials()
            if on_key_rotated:
                on_key_rotated(ws_id, key_id, new_version)

        # Synchronous adapter: dispatch is dict-only, not awaitable. An
        # async def would produce a coroutine the handler ignores.
        def wrapped_approval_resolved(payload: dict[str, Any]) -> None:
            if on_approval_resolved:
                on_approval_resolved(payload)

        conn = WebSocketConnection(
            url=ws_url,
            headers=headers,
            api_key=self.api_key,
            secret_key=self.secret_key,
            on_state_change=on_state_change,
            on_policy_invalidated=wrapped_policy_invalidated,
            on_key_rotated=wrapped_key_rotated,
            on_approval_resolved=wrapped_approval_resolved,
        )
        await conn.connect()
        return conn

    async def _refetch_credentials(self) -> None:
        """Re-fetch credentials from /auth/verify after key rotation.

        Routes through ``self._client`` so the same TLS configuration,
        connection pool, and HMAC signing path apply. Body is serialised via
        ``_signed_request_body`` so the wire bytes match the signed bytes.
        """
        try:
            payload = {"api_key": self.api_key}
            body = _signed_request_body(payload)
            headers = self._build_signed_headers(body=body)

            response = self._client.post(
                # P0 #5: contract drift — other auth-verify call sites
                # in this file use `/api/v1/auth/verify` (see runtime.py:599).
                # Align this rotation call site to the same v1 prefix so the
                # contract-drift-guard CI catches future divergence.
                f"{self.api_url}/api/v1/auth/verify",
                content=body,
                headers=headers,
                timeout=10.0,
            )
            if response.status_code == 200:
                data = response.json()
                new_secret = data.get("secret_key")
                if new_secret:
                    logger.info("Successfully fetched new secret_key from /auth/verify")
                    self.secret_key = new_secret
                else:
                    logger.warning("/auth/verify did not return secret_key in response")
            else:
                logger.warning(f"Failed to refetch credentials: {response.status_code}")
        except Exception as e:
            logger.error(f"Error refetching credentials: {e}")

    # =============================================================================
    # Wire-protocol v3 endpoints
    # =============================================================================
    #
    # The v3 wire contract adds six endpoints that the legacy /gate +
    # /execute + /track/batch surface does not cover. Each new method
    # follows the same shape as the existing `check` method:
    #
    # 1. Build headers via ``_build_signed_headers`` (gets X-API-Key +
    # Authorization + X-NULLRUN-PROTOCOL + HMAC + trace context).
    # 2. Serialise the body via ``_signed_request_body`` so the wire
    # bytes match the HMAC-signed bytes.
    # 3. POST through the shared ``self._client`` (mTLS, connection
    # pool, circuit breaker all apply).
    # 4. Map non-2xx responses through ``_parse_v3_error_envelope``
    # so callers can ``except NullRunBudgetError`` / ``except
    # NullRunConsumeOverbudgetError`` / etc. without parsing the
    # raw error_code string.

    def check_v3(
        self,
        request: dict[str, Any],
        on_transport_error: Callable[[Exception], dict[str, Any]] | str | None = None,
    ) -> dict[str, Any]:
        """Pre-execution gate — wire-protocol v3 (B1 fix 2026-07-04).

        Pre-fix this method POSTed to ``/api/v1/check``. That endpoint
        was removed on 2026-06-27 — the handler now returns
        ``410 Gone`` with a ``replacement: /api/v1/gate`` hint. The
        SDK's ``check `` method already targets ``/api/v1/gate`` and
        forwards every v3 wire field — ``chain_id``
        ``chain_op``, ``idempotency_key``, ``stream``. This method
        is kept as a v3-named alias so existing call sites and tests
        continue to work; internally it delegates to ``check `` with
        the same body.

        Args:
            request: Gate request body. Must include ``organization_id``
                ``execution_id`` (for backward compat — server mints its
                own on /check), ``operation_id``, and ``check_type``.
            on_transport_error: Mirrors the ``check `` flag.

        Returns:
            Parsed JSON dict, augmented with ``decision_source =
            DecisionSource.GATEWAY`` so callers distinguish it from a
            fallback synthetic response.

        Raises:
            NullRunAuthenticationError: 401/403 (PROTOCOL_TOO_OLD
                PROTOCOL_TOO_NEW, API_KEY_REVOKED, CHAIN_CROSS_ORG).
            NullRunConsumeOverbudgetError: 422 (placeholder for /track
                not raised on /gate).
            NullRunBudgetError: 402 BUDGET_HARD_BLOCKED /
                BUDGET_SOFT_BLOCKED / BUDGET_OVERDRAFT_EXCEEDED.
            NullRunChainError: 402 CHAIN_MAX_DURATION_EXCEEDED /
                403 CHAIN_ORG_MISMATCH.
            NullRunWorkflowInactiveError: 403 WORKFLOW_INACTIVE.
            NullRunBackendError: 5xx / BUDGET_DATA_UNAVAILABLE /
                RATE_LIMIT_REDIS_UNAVAILABLE.
        """
        # 2026-07-04 (B1): /api/v1/check returns 410 Gone.
        return self.check(request, on_transport_error=on_transport_error)

    def track_single(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """POST /api/v1/track — wire-protocol v3 single-event consume.

        . The single-event path is the v3
                replacement for the legacy `/api/v1/track/batch` POST body.
                It runs the CONSUME_SCRIPT invariant
                ``actual_cost <= reserved_cents + epsilon_cents`` (§25
                ADR-005) and rejects with 422 CONSUME_OVERBUDGET on
                violation. The reserved binding is the one created by the
                matching ``/check`` call (same ``reservation_id``).

                The wire shape is built by ``runtime._build_v3_track_payload``
                (see ``runtime.py:2679-2776``); this method just forwards
                whatever dict the caller hands it. The post-fix schema is:

                Args:
                    request: Consume request body. Must include:

                        * ``reservation_id`` (str, server-minted uuidv7 from
                          the matching /check response — wired via
                          ``_capture_server_minted_execution_id``)
                        * ``workflow_id`` (str, the workflow the call belongs to)
                        * ``tokens`` (int, sum of input + output tokens)
                        * ``cost_cents`` (int, ``0`` — backend computes the
                          authoritative cost from tokens + the org's
                          pricing policy; sending a wrong number risks
                          double-billing, see _WIRE_STRIP_FIELDS in runtime.py)
                        * ``cost_source`` (str, ``"provisional"`` /
                          ``"authoritative"`` per — SDK always emits
                          ``"provisional"``)

                        Optional fields: ``input_tokens``, ``output_tokens``
                        ``model``, ``latency_ms``, ``metadata``, ``trace_id``
                        ``span_id``, ``agent_id``, ``environment``
                        ``agent_type``, ``attempt_index``, ``is_retry``
                        ``idempotency_key``.

                Returns:
                    Parsed JSON dict with at least
                    ``{"status": "ok"|"idempotent_replay",...}``.

                Raises:
                    NullRunConsumeOverbudgetError: 422 CONSUME_OVERBUDGET —
                        ``actual_cost > reserved + epsilon_cents``. The
                        reservation is NOT silently re-reserved.
                    NullRunBackendError: 503 RESERVATION_NOT_FOUND /
                        EXECUTION_NOT_BOUND.
                    NullRunAuthenticationError: 401/403.

                 2026-07-04 (B2): pre-fix this docstring (and the
                surrounding module comment) described a fictitious wire
                shape ``{execution_id, actual_cost_cents, api_key_id
                cost_source}``. The backend's actual ``TrackRequestRaw`` is
                ``{workflow_id, tokens, cost_cents,...}``; ``execution_id``
                is replaced by ``reservation_id``, ``actual_cost_cents`` is
                replaced by ``cost_cents`` (the SDK always sends 0 — see
                ``_WIRE_STRIP_FIELDS``), and ``api_key_id`` is derived
                server-side from the request auth, not supplied by the SDK.
                The docstring now matches the real wire contract.
        """
        # 2026-07-06 (bug-fix): the previous shape called
        body = _signed_request_body(request)
        headers = self._build_signed_headers(body=body)

        try:
            response = self._client.post(
                f"{self.api_url}/api/v1/track",
                content=body,
                headers=headers,
                timeout=5.0,
            )
        except httpx.RequestError as e:
            raise NullRunTransportError(
                f"Network error on /track: {e}",
                source=TransportErrorSource.NETWORK_ERROR,
                endpoint="track",
            ) from e

        if response.status_code == 200:
            return response.json()  # type: ignore[no-any-return]

        raise _parse_v3_error_envelope(response, "track")

    def cancel(
        self,
        execution_id: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/v1/cancel — cancel an in-flight execution.

        . The server uses
                ``cancel:{execution_id}`` SETNX to deduplicate repeated
                cancellations: a 200 OK response is idempotent. A
                non-existent ``execution_id`` returns 404 — we surface it
                as ``NullRunBackendError`` because retrying with the same
                id is not a valid recovery path (the execution already
                terminated).

                Args:
                    execution_id: Server-minted id from the matching /check
                        response.
                    reason: Optional human-readable reason for the
                        cancellation (audit trail).

                Returns:
                    Parsed JSON dict (typically ``{"status": "ok"
                    "execution_id":..., "cancelled_at": ts}``).
        """
        request: dict[str, Any] = {"execution_id": execution_id}
        if reason:
            request["reason"] = reason

        # 2026-07-06 (bug-fix): same body-before-headers reorder as
        body = _signed_request_body(request)
        headers = self._build_signed_headers(body=body)

        try:
            response = self._client.post(
                f"{self.api_url}/api/v1/cancel",
                content=body,
                headers=headers,
                timeout=5.0,
            )
        except httpx.RequestError as e:
            raise NullRunTransportError(
                f"Network error on /cancel: {e}",
                source=TransportErrorSource.NETWORK_ERROR,
                endpoint="cancel",
            ) from e

        if response.status_code == 200:
            return response.json()  # type: ignore[no-any-return]

        raise _parse_v3_error_envelope(response, "cancel")

    def heartbeat(
        self,
        chain_id: str,
    ) -> dict[str, Any]:
        """POST /api/v1/heartbeat — extend a chain's idle TTL.

        . The server runs
                ``EXPIRE chain:{org}:{chain_id} 300`` atomically and
                deduplicates repeated heartbeats via
                ``heartbeat:{chain_id}:{ts_floor_30s}`` SETNX
                (TTL = 35s — the 5s tail absorbs ±5s skew per).

                Recommended cadence: every 30s of wall-clock time (the
                SDK's ``ping_chain`` helper wraps this method with the
                time-based scheduler). Bursting heartbeats more often than
                once per 30s is wasted bandwidth — the SETNX dedups them.

                Args:
                    chain_id: Active chain_id.

                Returns:
                    Parsed JSON dict (typically ``{"status": "ok"
                    "chain_id":..., "last_active": ts}``).
        """
        request = {"chain_id": chain_id}
        # 2026-07-06 (bug-fix): same body-before-headers reorder as
        # track_single above.
        body = _signed_request_body(request)
        headers = self._build_signed_headers(body=body)

        try:
            response = self._client.post(
                f"{self.api_url}/api/v1/heartbeat",
                content=body,
                headers=headers,
                timeout=5.0,
            )
        except httpx.RequestError as e:
            raise NullRunTransportError(
                f"Network error on /heartbeat: {e}",
                source=TransportErrorSource.NETWORK_ERROR,
                endpoint="heartbeat",
            ) from e

        if response.status_code == 200:
            return response.json()  # type: ignore[no-any-return]

        raise _parse_v3_error_envelope(response, "heartbeat")

    def chain_end(
        self,
        chain_id: str,
    ) -> dict[str, Any]:
        """Close a chain explicitly via /api/v1/gate with chain_op=end
        .

                Pre-fix this method POSTed to ``/api/v1/chain/end``. That
                endpoint was never registered on the backend
                (``backend/src/proxy/http/routes.rs`` has zero matches for
                ``chain/end`` or ``chain_end_handler``) — the only documented
                way to close a chain is to POST /api/v1/gate with
                ``{"chain_id": "...", "chain_op": "end"}``. The handler is
                already idempotent — a no-op 200 OK for an unknown chain_id
                is the documented success path. The SDK still raises through
                the envelope parser on a true non-2xx so unexpected backend
                regressions surface.

                Args:
                    chain_id: Chain to close.

                Returns:
                    Parsed JSON dict (typically ``{"decision": "allow"
                    "chain_id":...}``).
        """
        # 2026-07-04 (B3): POST /api/v1/gate with
        request = {
            "chain_id": chain_id,
            "chain_op": "end",
            # execution_id is required by the backend's gate handler
            # even on chain_end — the handler reads it but does not
            # mint a reservation for op=end. Use a fresh uuidv7
            # call (the server ignores it on this path).
            "execution_id": uuid.uuid4().hex,
        }
        # 2026-07-06 (bug-fix): same body-before-headers reorder as
        body = _signed_request_body(request)
        headers = self._build_signed_headers(body=body)

        try:
            response = self._client.post(
                f"{self.api_url}/api/v1/gate",
                content=body,
                headers=headers,
                timeout=5.0,
            )
        except httpx.RequestError as e:
            raise NullRunTransportError(
                f"Network error on /gate (chain_end): {e}",
                source=TransportErrorSource.NETWORK_ERROR,
                endpoint="chain_end",
            ) from e

        if response.status_code == 200:
            return response.json()  # type: ignore[no-any-return]

        raise _parse_v3_error_envelope(response, "chain_end")

    def approximate_budget(
        self,
        organization_id: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/v1/budget/approximate — UI-only budget estimation.

        . NEVER for enforcement — the backend stamps
                ``is_approximate: true`` on every response. The endpoint
                returns 503 ``BUDGET_DATA_UNAVAILABLE`` if all three sources
                (Redis period counter → Postgres cost_events → last-known
                cache) fail — NEVER returns 0, because a UI that displays
                "≈ $0 spent" when no data is available misleads the user.

                Used by ``nullrun.cost_dashboard `` / ``examples/cost_dashboard.py``
                and the dashboard rollup panel.

                Args:
                    organization_id: Optional org override; defaults to the
                        transport's bound org via the auth/verify result.

                Returns:
                    Parsed JSON dict with ``current_spend_cents_estimate``
                    ``is_approximate: True``, ``source`` (BudgetSource enum
                    string), ``confidence`` (High/Medium/Low), and
                    ``last_updated_at``.

                Raises:
                    NullRunBackendError: 503 BUDGET_DATA_UNAVAILABLE (all
                        sources failed) — caller should display "Data
                        unavailable" + retry button, NOT "$0 spent".
                    NullRunAuthenticationError: 401/403.
        """
        # ApproximateBudget uses GET (not POST) per the wire contract
        headers = self._auth_headers_for_get()
        url = f"{self.api_url}/api/v1/budget/approximate"

        try:
            response = self._client.get(url, headers=headers, timeout=5.0)
        except httpx.RequestError as e:
            raise NullRunTransportError(
                f"Network error on /budget/approximate: {e}",
                source=TransportErrorSource.NETWORK_ERROR,
                endpoint="approximate_budget",
            ) from e

        if response.status_code == 200:
            return response.json()  # type: ignore[no-any-return]

        raise _parse_v3_error_envelope(response, "approximate_budget")

    # ====================================================================
    # ADR-009 P1 — Audit log governance surface (v0.15.0)
    # ====================================================================
    # Five methods exposing the /api/v1/orgs/:org_id/audit-log/* family
    # of endpoints to SDK consumers. Pre-v0.15.0 SDKs had no audit
    # client — operators had to curl the wire directly. Now they can
    # call ``runtime.audit.list(...)`` etc. and get typed dataclasses
    # back without writing JSON parsing glue.
    #
    # All five methods route through the same auth + protocol +
    # trace-context machinery as the other Transport methods — see
    # ``_auth_headers_for_get`` below. Audit reads are GET, so no
    # HMAC body signing is required.

    def audit_log(
        self,
        organization_id: str,
        query: Any | None = None,
    ) -> dict[str, Any]:
        """GET /api/v1/orgs/:org_id/audit-log — read governance audit log.

        Args:
            organization_id: Org UUID — required because the
                /audit-log endpoint is org-scoped. The runtime
                proxy passes ``self.organization_id`` automatically
                so direct callers rarely need to set this.
            query: Optional :class:`nullrun.audit.AuditQuery`
                instance describing the filter set (event_type,
                decision, policy_id, execution_id, action, actor,
                since, until, limit). Pass ``None`` for "all rows"
                (rarely what you want — chains grow unbounded).

        Returns:
            Parsed JSON dict with ``data`` (list of
            AuditEntryResponse shapes) and ``meta`` (AuditLogMeta
            pagination summary). Use
            :func:`nullrun.audit.AuditLogPage.from_wire` to parse
            into typed dataclasses.

        Raises:
            NullRunBackendError: 401/403/5xx.
            NullRunAuthenticationError: 401.
        """
        from nullrun.audit import AuditQuery

        q: AuditQuery = query if isinstance(query, AuditQuery) else (query or AuditQuery())
        qs = q.to_query_string()
        url = f"{self.api_url}/api/v1/orgs/{organization_id}/audit-log"
        if qs:
            url = f"{url}?{qs}"
        headers = self._auth_headers_for_get()
        try:
            response = self._client.get(url, headers=headers, timeout=10.0)
        except httpx.RequestError as e:
            raise NullRunTransportError(
                f"Network error on /audit-log: {e}",
                source=TransportErrorSource.NETWORK_ERROR,
                endpoint="audit_log",
            ) from e
        if response.status_code == 200:
            return response.json()  # type: ignore[no-any-return]
        raise _parse_v3_error_envelope(response, "audit_log")

    def audit_verify(
        self,
        organization_id: str,
        *,
        since: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/v1/orgs/:org_id/audit-log/verify — chain integrity.

        Walks the chain forward from `since` (or from row 1 if
        omitted) and re-computes content_hash + previous_hash
        continuity. Returns the same payload the audit page's
        "Integrity" banner reads — use
        :func:`nullrun.audit.AuditVerifyResult.from_wire` to parse.

        Args:
            organization_id: Org UUID — required.
            since: Optional RFC3339 lower bound. With `since`,
                only rows since that timestamp are walked (plus a
                prior anchor row for hash continuity). Without
                `since`, the full chain from row 1 is re-verified.

        Returns:
            Parsed JSON dict with `verified`, `chain_valid`,
            `record_count`, `first_hash`, `last_hash`,
            `first_failure_reason`, `timestamp`, `hmac_checked`.

        Raises:
            NullRunBackendError / NullRunAuthenticationError.
        """
        params: list[tuple[str, str]] = []
        if since:
            params.append(("since", since))
        qs = "&".join(f"{k}={v}" for k, v in params)
        url = f"{self.api_url}/api/v1/orgs/{organization_id}/audit-log/verify"
        if qs:
            url = f"{url}?{qs}"
        headers = self._auth_headers_for_get()
        try:
            response = self._client.get(url, headers=headers, timeout=30.0)
        except httpx.RequestError as e:
            raise NullRunTransportError(
                f"Network error on /audit-log/verify: {e}",
                source=TransportErrorSource.NETWORK_ERROR,
                endpoint="audit_verify",
            ) from e
        if response.status_code == 200:
            return response.json()  # type: ignore[no-any-return]
        raise _parse_v3_error_envelope(response, "audit_verify")

    def audit_list_exports(
        self,
        organization_id: str,
    ) -> list[dict[str, Any]]:
        """GET /api/v1/orgs/:org_id/audit-log/export — list recent export jobs.

        Returns the raw JSON list of recent export job summaries
        (last 10). Use :func:`nullrun.audit.AuditExportJob.from_wire`
        to parse each entry.

        Raises:
            NullRunBackendError / NullRunAuthenticationError.
        """
        url = f"{self.api_url}/api/v1/orgs/{organization_id}/audit-log/export"
        headers = self._auth_headers_for_get()
        try:
            response = self._client.get(url, headers=headers, timeout=10.0)
        except httpx.RequestError as e:
            raise NullRunTransportError(
                f"Network error on /audit-log/export (list): {e}",
                source=TransportErrorSource.NETWORK_ERROR,
                endpoint="audit_list_exports",
            ) from e
        if response.status_code == 200:
            body = response.json()
            # Wire shape is `{"exports": [...]}` per the audit export
            # list handler in backend/src/proxy/http/audit.rs.
            if isinstance(body, dict):
                return body.get("exports", []) or []
            return body if isinstance(body, list) else []
        raise _parse_v3_error_envelope(response, "audit_list_exports")

    def audit_create_export(
        self,
        organization_id: str,
    ) -> dict[str, Any]:
        """POST /api/v1/orgs/:org_id/audit-log/export — enqueue 30-day export.

        The backend creates a job, returns ``{"job_id", "status":
        "pending"}`` immediately, and processes in the background.
        Poll :meth:`audit_export_status` for completion.

        The export covers the trailing 30 days; the backend hard-codes
        that window today (audit.rs:692-700 — ``chrono::Utc::now() -
        Duration::days(30)``). When the per-job window becomes
        configurable this method will accept a `since`/`until`
        override.

        Returns:
            Parsed JSON dict with ``job_id`` (UUID) and ``status``.

        Raises:
            NullRunBackendError / NullRunAuthenticationError.
        """
        url = f"{self.api_url}/api/v1/orgs/{organization_id}/audit-log/export"
        headers = self._build_signed_headers(body=b"{}")
        try:
            response = self._client.post(url, content=b"{}", headers=headers, timeout=10.0)
        except httpx.RequestError as e:
            raise NullRunTransportError(
                f"Network error on /audit-log/export (create): {e}",
                source=TransportErrorSource.NETWORK_ERROR,
                endpoint="audit_create_export",
            ) from e
        if response.status_code == 200:
            return response.json()  # type: ignore[no-any-return]
        raise _parse_v3_error_envelope(response, "audit_create_export")

    def audit_export_status(
        self,
        organization_id: str,
        job_id: str,
    ) -> dict[str, Any]:
        """GET /api/v1/orgs/:org_id/audit-log/export/:job_id/status.

        Polls a previously-enqueued export job. When ``status`` flips
        to ``completed`` the ``file_url`` field carries an S3
        presigned URL (or `/tmp/...` path on dev), and an
        ``error_message`` is set on the ``failed`` transition.

        Args:
            organization_id: Org UUID — required.
            job_id: UUID returned by :meth:`audit_create_export`.

        Returns:
            Parsed JSON dict with ``job_id``, ``status``,
            ``file_url``, ``record_count``, ``created_at``,
            ``completed_at``, ``error_message``.

        Raises:
            NullRunBackendError / NullRunAuthenticationError.
        """
        url = (
            f"{self.api_url}/api/v1/orgs/{organization_id}"
            f"/audit-log/export/{job_id}/status"
        )
        headers = self._auth_headers_for_get()
        try:
            response = self._client.get(url, headers=headers, timeout=10.0)
        except httpx.RequestError as e:
            raise NullRunTransportError(
                f"Network error on /audit-log/export/{job_id}/status: {e}",
                source=TransportErrorSource.NETWORK_ERROR,
                endpoint="audit_export_status",
            ) from e
        if response.status_code == 200:
            return response.json()  # type: ignore[no-any-return]
        raise _parse_v3_error_envelope(response, "audit_export_status")

    def _auth_headers_for_get(self) -> dict[str, str]:
        """Headers for an unsigned GET (no HMAC body).

        Same shape as ``_build_signed_headers`` minus the HMAC
        headers. Used by ``approximate_budget`` which is a GET with
        no body, so there's nothing to sign. Keeps the protocol +
        CSRF-bypass + trace-context headers consistent with the
        signed-POST path.
        """
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers[HEADER_PROTOCOL] = _protocol_header_value()
        self._inject_trace_context(headers)
        return headers


# 2026-07-02 (v0.11.0): ACTIVE v3 error envelope parser.
def _extract_error_envelope(
    body: Any,
    raw_text: str,
) -> tuple[str, str, dict[str, Any]]:
    """Pull ``(error_code, message, details)`` from any error envelope.

    Drift §3 (2026-07-06): the backend emits three distinct shapes
    for non-2xx responses. This helper normalises them into the
    ``(error_code, message, details)`` tuple the rest of
    ``_parse_v3_error_envelope`` consumes.

    Lookup priority:

    1. **v3 envelope** -- ``{"error_code": "BUDGET_HARD_BLOCKED",
       "error_message": "...", "details": {...}, ...}``. The
       canonical shape from ``gate/internal.rs`` and
       ``handlers.rs::track_handler``.

    2. **v3 mixed** -- ``{"error_code": "BUDGET_DATA_UNAVAILABLE",
       "message": "...", "retry_after_ms": N}``. The 503 path
       from ``budget.rs:107-112``; same v3 semantics but the
       message field is called ``message`` not ``error_message``.

    3. **Legacy slug** -- ``{"error": "chain_not_extendable",
       "message": "...", "chain_state": "..."}``. From
       ``heartbeat.rs:199-205`` and the ``ApiError`` path on
       ``cancel.rs``. The slug is lowercased and SCREAMING_SNAKE'd
       so it matches ``_V3_ERROR_CODE_MAP`` lookups.

    4. **Plaintext** -- ``response.text`` containing a free-form
       error string (heartbeat.rs:157, heartbeat.rs:166). No JSON,
       so ``body`` is empty.

    Args:
        body: Parsed JSON body from the response (``{}`` on parse
            failure or non-JSON content).
        raw_text: Raw ``response.text`` fallback for plaintext
            envelopes.

    Returns:
        ``(backend_code, message, details)`` where:

        * ``backend_code`` is uppercase SCREAMING_SNAKE if it
          originated from the v3 envelope, or the lowercased slug
          otherwise. The mapping table keys are uppercase; the
          dispatcher lowercases the lookup key before consulting
          the map.
        * ``message`` is the human-readable string for the
          exception class. Falls back to ``raw_text`` if no JSON
          body.
        * ``details`` is the machine-readable context payload
          (``details: {...}`` on the v3 envelope, all other
          JSON fields flattened on the legacy slug, ``{}`` on
          plaintext).
    """
    if not isinstance(body, dict) or not body:
        # No JSON body -- plaintext error envelope.
        # Heartbeat's 404 "chain not found" and 403
        # "chain org mismatch" land here.
        return ("", raw_text or "", {})

    # Shape 1: v3 envelope.
    if "error_code" in body:
        code = str(body.get("error_code", "") or "")
        # The 503 budget path uses "message" instead of
        # "error_message". Accept both.
        message = str(body.get("error_message") or body.get("message") or raw_text or "")
        details_raw = body.get("details") or {}
        if not isinstance(details_raw, dict):
            details_raw = {}
        # Forward any extra top-level fields that look like
        # context (e.g. ``chain_state`` on heartbeat 409) into
        # details so downstream code can introspect them.
        details: dict[str, Any] = dict(details_raw)
        for key, value in body.items():
            if key in (
                "error_code",
                "error_message",
                "message",
                "details",
                "retry_after_ms",
            ):
                continue
            details.setdefault(key, value)
        return (code, message, details)

    # Shape 2: legacy slug. ``error`` is the slug,
    # ``message`` is the human-readable string.
    if "error" in body:
        slug = str(body.get("error", "") or "")
        message = str(body.get("message", "") or raw_text or "")
        # Convert the legacy lowercase slug to uppercase
        # SCREAMING_SNAKE so the mapping table can find it.
        code = slug.upper()
        # Everything except ``error`` and ``message`` goes into
        # details for diagnostic context.
        details = {
            k: v for k, v in body.items() if k not in ("error", "message") and not k.startswith("_")
        }
        return (code, message, details)

    # JSON body but not a recognised envelope shape. Pass through.
    return ("", raw_text or str(body), dict(body) if isinstance(body, dict) else {})


def _safe_json(response: httpx.Response, endpoint: str) -> Any:
    """Parse a response body as JSON, wrapping parse failures.

    DEF-ERRHDL-INVALID-JSON-01 (2026-08-11, RUN_ID 20260811-1): the SDK
    previously propagated ``json.JSONDecodeError`` unchanged to user
    code, which leaks internal file paths and the raw broken payload
    fragment in tracebacks. This helper wraps the parse failure in
    NullRunTransportError with a stable ``error_code`` so callers can
    ``except`` cleanly and the user sees a short NullRun-family
    message instead of a Python traceback.

    ``body_preview`` is intentionally truncated to 200 chars and the
    raw ``JSONDecodeError.lineno/colno`` are NOT included in the
    surfaced message -- both are info-leak surface (line numbers
    hint at response shape; partial body may carry PII like
    organization_id fragments).
    """
    try:
        return response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        # Body preview capped at 200 chars; truncated to avoid
        # flooding logs / exception chain.
        try:
            body_preview = (response.text or "")[:200]
        except Exception:
            body_preview = "<unreadable>"
        raise NullRunTransportError(
            f"Received malformed JSON from {endpoint} "
            f"(status={response.status_code}): {type(exc).__name__}",
            source=TransportErrorSource.GATEWAY_ERROR,
            endpoint=endpoint,
            error_code="NR-T001",
        ) from exc


def _parse_v3_error_envelope(
    response: httpx.Response,
    endpoint: str,
) -> Exception:
    """Translate a non-2xx ``httpx.Response`` into the right v3
    SDK exception.

    The backend returns errors as a JSON envelope of the shape
    ``{"error_code": "BUDGET_HARD_BLOCKED", "error_message": "..."
    "details": {...}, "retry_after_ms": N}``. The
    parser maps the backend's ``error_code`` string to the closest
    SDK exception class, attaching the structured envelope fields
    as instance attributes so callers can introspect them.

    Mapping table lives at ``_V3_ERROR_CODE_MAP`` below — keep the
    helper as a thin dispatcher.
    """
    # Lazy imports: the exception classes import the transport
    # types (TransportErrorSource), so a top-level import here
    # would create a cycle. The price is one extra import
    # non-2xx response — irrelevant for the failure path.
    from nullrun.breaker.exceptions import (
        NullRunApprovalDeniedError,
        NullRunApprovalDigestMismatchError,
        NullRunApprovalExpiredError,
        NullRunApprovalNotYetApprovedError,
        NullRunApprovalReplayRejectedError,
        NullRunApprovalToolDigestMismatchError,
        NullRunAuthError,
        NullRunBackendError,
        NullRunBudgetError,
        NullRunBudgetRecheckFailedError,
        NullRunChainError,
        NullRunConsumeOverbudgetError,
        NullRunProtocolError,
        NullRunRateLimitRedisError,
        NullRunWorkflowInactiveError,
        RateLimitError,
    )

    status = response.status_code
    try:
        body = response.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        body = {}

    # Drift §3 (2026-07-06): the wire envelope is NOT one shape.
    backend_code, message, details = _extract_error_envelope(body, response.text)
    retry_after_ms: float | None = body.get("retry_after_ms") if isinstance(body, dict) else None
    # Retry-After header takes precedence over the JSON field when
    # both are present (server-side convention — header is canonical
    # per RFC 7231, JSON is a NullRun-specific fallback).
    retry_after_header = response.headers.get("Retry-After")
    if retry_after_header:
        try:
            retry_after_ms = float(retry_after_header) * 1000.0
        except ValueError:
            # HTTP-date form is non-numeric — leave JSON value intact.
            pass

    # Per-class dispatcher. Each exception has its own constructor
    # signature (RateLimitError requires source+endpoint
    # NullRunBackendError requires endpoint+status_code, etc.) so a
    # uniform ``error_cls(**kwargs)`` does not work. The switches
    # below mirror the exact field mapping from.
    full_message = f"{endpoint}: {message}"

    if backend_code == "PROTOCOL_TOO_OLD" or backend_code == "PROTOCOL_TOO_NEW":
        # NullRunProtocolError → NullRunInfrastructureError →
        # NullRunError base. Base constructor does NOT accept
        # a generic ``details=`` kwarg. Pass message only — the
        # catalog value already encodes error_code + retryable.
        return NullRunProtocolError(full_message)

    if backend_code == "CONSUME_OVERBUDGET":
        return NullRunConsumeOverbudgetError(
            full_message,
            execution_id=details.get("execution_id"),
            reserved_cents=details.get("reserved_cents"),
            max_allowed_cents=details.get("max_allowed_cents"),
            actual_cost_cents=details.get("actual_cost_cents"),
            epsilon_cents=details.get("epsilon_cents"),
            status_code=status,  # 422 per backend mapping
        )

    if (
        backend_code == "CHAIN_MAX_DURATION_EXCEEDED"
        or backend_code == "CHAIN_CROSS_ORG"
        or backend_code == "CHAIN_ORG_MISMATCH"
    ):
        return NullRunChainError(
            full_message,
            chain_id=details.get("chain_id"),
            backend_code=backend_code,
            details=details,
            status_code=status,  # 402/403 per backend mapping
        )

    if backend_code == "WORKFLOW_INACTIVE":
        return NullRunWorkflowInactiveError(
            full_message,
            workflow_id=details.get("workflow_id"),
            status_code=status,  # 403 per backend mapping
        )

    if backend_code == "BUDGET_RECHECK_FAILED":
        # H6 / 2026-08-12 audit: dedicated typed dispatch so callers
        # can branch on the post-approval recheck failure (NR-B006)
        # vs a fresh /gate block (NR-B004). The dispatcher surfaces
        # ``current_spend_cents`` / ``budget_cents`` from the wire
        # envelope so callers can compute the remaining cap and
        # decide whether to retry after re-/gate.
        return NullRunBudgetRecheckFailedError(
            full_message,
            current_spend_cents=details.get("current_spend_cents"),
            budget_cents=details.get("budget_cents"),
            status_code=status,  # 402 per backend mapping
        )

    if backend_code in (
        "APPROVAL_NOT_YET_APPROVED",
        "APPROVAL_DENIED",
        "APPROVAL_EXPIRED",
        "APPROVAL_DIGEST_MISMATCH",
        "APPROVAL_TOOL_DIGEST_MISMATCH",
        "APPROVAL_REPLAY_REJECTED",
    ):
        # v3.53 / 2026-08-13 audit, A-1+A-2 bundle: dedicated typed
        # dispatch so callers can branch on the precise grant-consume
        # outcome. Pre-v3.53 the SDK fell through to the catalog
        # fallback path which called ``catalog(full_message, **details)``
        # — NullRunBlockedException subclasses reject that signature
        # (they need workflow_id as positional arg) so the catch-all
        # path raised TypeError instead of the typed exception.
        # Post-v3.53 each of the six codes maps to its own NR-Axxx
        # subclass (NR-A010..NR-A015). Wire details carry the
        # approval_id and the typed exception's NR-Axxx catalog
        # value (via the class attribute) so cookbook recipes can
        # ``except NullRunApprovalDeniedError:`` for terminal
        # surface-to-user, ``except
        # NullRunApprovalNotYetApprovedError:`` for wait/poll,
        # ``except NullRunApprovalReplayRejectedError:`` for
        # retry-loop detection, etc.
        catalog = _V3_ERROR_CODE_MAP[backend_code]
        return catalog(  # type: ignore[call-arg]
            workflow_id=str(details.get("workflow_id") or "unknown"),
            reason=full_message,
            status_code=status,  # 403 per backend mapping
            approval_id=details.get("approval_id"),
        )

    if backend_code == "RATE_LIMIT_REDIS_UNAVAILABLE":
        # NullRunRateLimitRedisError → NullRunInfrastructureError
        # → NullRunError base. Base constructor accepts only
        # message + (error_code, user_action, retryable, docs_url
        # cause) — NOT a generic ``details=``. The catalog value
        # already encodes error_code + retryable, so we just pass
        # the message.
        return NullRunRateLimitRedisError(full_message)

    if backend_code == "RATE_LIMIT_EXCEEDED":
        retry_after = retry_after_ms / 1000.0 if retry_after_ms else None
        return RateLimitError(
            full_message,
            source=TransportErrorSource.GATEWAY_ERROR,
            endpoint=endpoint,
            retry_after=retry_after,
            body=body,
        )

    # Catalog codes that map to NullRunBudgetError / NullRunBackendError
    # via the fallback shape (no special signature).
    catalog = _V3_ERROR_CODE_MAP.get(backend_code)
    if catalog is not None:
        # Special-case each constructor signature — the NullRun
        # hierarchy has heterogeneous constructors (workflow_id +
        # reason for NullRunBlockedException, endpoint + status_code
        # for NullRunBackendError, error_code/user_action for
        # NullRunError base). Universal ``catalog(message, details=)``
        # would trip one of them every time.
        if catalog is NullRunBackendError:
            return NullRunBackendError(
                full_message,
                endpoint=endpoint,
                status_code=status,
            )
        if catalog is NullRunBudgetError:
            # NullRunBudgetError → NullRunBlockedException → requires
            return NullRunBudgetError(
                workflow_id=str(details.get("workflow_id") or "unknown"),
                reason=full_message,
                status_code=status,
            )
        if catalog is NullRunRateLimitRedisError:
            # NullRunError base takes (message, error_code=, user_action=
            # retryable=, docs_url=, cause=). The catalog value here
            # already encodes error_code + retryable, so we pass
            # the message only.
            return catalog(full_message)
        if catalog is NullRunProtocolError:
            return catalog(full_message)
        # NullRunAuthError — surface the wire error_code (one of
        # v3.38's API_KEY_REVOKED / API_KEY_EXPIRED / API_KEY_DISABLED
        # / API_KEY_INVALID / API_KEY_MISSING / API_KEY_MALFORMED) on
        # ``self.wire_code`` so callers can branch on granular
        # lifecycle state without clobbering the SDK-side
        # ``error_code`` taxonomy (NR-A003). Mirrors the
        # ``NullRunChainError.backend_code`` pattern.
        #
        # Filter ``details`` to the kwargs the base NullRunError
        # constructor accepts — the envelope's ``details`` dict can
        # carry arbitrary keys (``expires_at``, ``ttl_seconds``, ...)
        # and the base class rejects unknown kwargs with TypeError.
        # Unknown fields are stored on ``self.details`` for caller
        # introspection instead.
        if catalog is NullRunAuthError:
            allowed = {"error_code", "user_action", "retryable", "docs_url", "cause"}
            forwarded = {k: v for k, v in details.items() if k in allowed}
            extra = {k: v for k, v in details.items() if k not in allowed}
            instance = NullRunAuthError(
                full_message,
                wire_code=backend_code,
                **forwarded,
            )
            if extra:
                instance.details = extra  # type: ignore[attr-defined]
            return cast(Exception, instance)
        # Final fallback for catalog classes with a generic
        # (message, **details) signature (NullRunWorkflowInactiveError
        # and any future addition).
        # The details payload is forwarded as a positional kwarg
        # via **details (typed as Any to satisfy mypy since
        # type[BaseException] does not expose the kwargs the
        # catalog subclasses actually accept).
        #
        # The catalog lookup produces type[BaseException] (the
        # union of all class objects), but every entry in
        # _V3_ERROR_CODE_MAP is a real Exception subclass. Cast
        # to Exception so mypy stops flagging the return value
        # as BaseException (the helper declares -> Exception).
        allowed = {"error_code", "user_action", "retryable", "docs_url", "cause"}
        forwarded = {k: v for k, v in details.items() if k in allowed}
        instance = catalog(full_message, **forwarded)  # type: ignore[call-arg]
        return cast(Exception, instance)

    # Fallback — use HTTP status. The catalog may not yet cover
    # every backend code, so we surface a typed backend error
    # that exposes status_code + error_code for the caller.
    if status in (401, 403):
        return NullRunAuthenticationError(
            f"Auth failed on {endpoint} (status {status}, error_code={backend_code!r}): {message}"
        )
    if status == 429:
        retry_after = retry_after_ms / 1000.0 if retry_after_ms else None
        return RateLimitError(
            f"Rate limited on {endpoint} (status 429, error_code={backend_code!r}): {message}",
            source=TransportErrorSource.GATEWAY_ERROR,
            endpoint=endpoint,
            retry_after=retry_after,
            body=body,
        )
    if 500 <= status < 600:
        return NullRunBackendError(
            f"{endpoint}: {message} (status {status}, error_code={backend_code!r})",
            endpoint=endpoint,
            status_code=status,
        )
    return NullRunBackendError(
        f"{endpoint}: {message} (status {status}, error_code={backend_code!r})",
        endpoint=endpoint,
        status_code=status,
    )


# Lazy import to avoid a hard dependency at module import time.
# `_parse_v3_error_envelope` is a module-level helper; the exception
# classes live in `nullrun.breaker.exceptions`. Importing here
# (rather than at the top of transport.py) keeps the legacy import
# graph identical and avoids breaking the frozen
# ``_parse_error_envelope`` test contract.
def _build_v3_error_code_map() -> dict[str, type[BaseException]]:
    """Construct the v3 error_code → exception class mapping.

    Imported lazily because the exception classes import the
    transport types (TransportErrorSource), which would create a
    circular import if loaded eagerly at the top of transport.py.
    """
    from nullrun.breaker.exceptions import (
        NullRunApprovalDeniedError,
        NullRunApprovalDigestMismatchError,
        NullRunApprovalExpiredError,
        NullRunApprovalNotYetApprovedError,
        NullRunApprovalReplayRejectedError,
        NullRunApprovalToolDigestMismatchError,
        NullRunAuthError,
        NullRunBackendError,
        NullRunBlockedException,
        NullRunBudgetError,
        NullRunChainError,
        NullRunConsumeOverbudgetError,
        NullRunProtocolError,
        NullRunRateLimitRedisError,
        NullRunWorkflowInactiveError,
        RateLimitError,
    )

    return {
        # 400 — protocol mismatch
        "PROTOCOL_TOO_OLD": NullRunProtocolError,
        "PROTOCOL_TOO_NEW": NullRunProtocolError,
        # 402 — budget family
        "BUDGET_HARD_BLOCKED": NullRunBudgetError,
        "BUDGET_SOFT_BLOCKED": NullRunBudgetError,
        "BUDGET_OVERDRAFT_EXCEEDED": NullRunBudgetError,
        "BUDGET_PERIOD_NOT_STARTED": NullRunBudgetError,
        "REDIS_UNAVAILABLE": NullRunBudgetError,
        # 402 — chain family (separate class for diagnostic clarity)
        "CHAIN_MAX_DURATION_EXCEEDED": NullRunChainError,
        # 403 — chain security + workflow state
        "CHAIN_CROSS_ORG": NullRunChainError,
        "CHAIN_ORG_MISMATCH": NullRunChainError,
        # 403 — Execution Graph v0 (2026-08-06, backend). Sub-agent
        "PARENT_EXECUTION_NOT_FOUND": NullRunChainError,
        "PARENT_EXECUTION_ORG_MISMATCH": NullRunChainError,
        "PARENT_EXECUTION_KEY_MISMATCH": NullRunChainError,
        "WORKFLOW_INACTIVE": NullRunWorkflowInactiveError,
        # 401/403 — auth (v3.38 distinct lifecycle states).
        # The backend splits the v3.36 ``API_KEY_REVOKED`` bucket into
        # five distinct wire codes so SDKs can branch on each state
        # (e.g. surface "rotate this key" vs "this key was admin-
        # disabled" vs "no Authorization header was sent"). All map
        # to NullRunAuthError — diagnostic class is preserved; the
        # granular codes live in ``details.error_code`` and are
        # surfaced via NullRunAuthError.code for handler dispatch.
        "API_KEY_REVOKED": NullRunAuthError,
        "API_KEY_EXPIRED": NullRunAuthError,
        "API_KEY_DISABLED": NullRunAuthError,
        "API_KEY_INVALID": NullRunAuthError,
        "API_KEY_MISSING": NullRunAuthError,
        "API_KEY_MALFORMED": NullRunAuthError,
        # 422 — consume invariant violation
        "CONSUME_OVERBUDGET": NullRunConsumeOverbudgetError,
        # 429 — rate limit
        "RATE_LIMIT_EXCEEDED": RateLimitError,
        # 503 — backend availability
        "RATE_LIMIT_REDIS_UNAVAILABLE": NullRunRateLimitRedisError,
        "BUDGET_DATA_UNAVAILABLE": NullRunBackendError,
        # 402 — approval-create failure family (DEF-ARFLOW-TOOLNAME-01,
        "APPROVAL_DB_UNAVAILABLE": NullRunBlockedException,
        "APPROVAL_PERSISTENCE_FAILED": NullRunBlockedException,
        "APPROVAL_VALIDATION_FAILED": NullRunBlockedException,
        "APPROVAL_CONFLICT": NullRunBlockedException,
        "APPROVAL_NOT_FOUND": NullRunBlockedException,
        "APPROVAL_CREATE_FAILED": NullRunBlockedException,
        # 403 — approval grant-consume outcomes (v3.53 / 2026-08-13
        # audit, A-1+A-2 bundle). Distinct from the /gate
        # create-failure family above: these are the seven
        # distinct outcomes that the backend's
        # `gate_internal()` returns on /execute post-approval
        # grant-consume (see
        # `backend/src/proxy/http/gate/internal.rs:3059-3108,
        # 3115-3138`). Pre-v3.53 the SDK collapsed all six
        # into NullRunBlockedException — bilateral wire gap.
        # Post-v3.53 each maps to a typed exception
        # (NR-A010..NR-A015) so cookbook recipes can branch
        # on the precise outcome (e.g. ``except
        # NullRunApprovalNotYetApprovedError:`` for wait/poll,
        # ``except NullRunApprovalDeniedError:`` for terminal
        # surface-to-user, ``except
        # NullRunApprovalReplayRejectedError:`` for retry-loop
        # detection).
        "APPROVAL_NOT_YET_APPROVED": NullRunApprovalNotYetApprovedError,
        "APPROVAL_DENIED": NullRunApprovalDeniedError,
        "APPROVAL_EXPIRED": NullRunApprovalExpiredError,
        "APPROVAL_DIGEST_MISMATCH": NullRunApprovalDigestMismatchError,
        "APPROVAL_TOOL_DIGEST_MISMATCH": NullRunApprovalToolDigestMismatchError,
        "APPROVAL_REPLAY_REJECTED": NullRunApprovalReplayRejectedError,
        # 402 — post-approval budget recheck (H6 / 2026-08-12 audit).
        # Distinct from BUDGET_HARD_BLOCKED: the operator explicitly
        # approved the grant at /gate, but the period-bound counter
        # moved between /gate and /execute (another concurrent
        # execution spent the budget). Caller should re-/gate to
        # refresh the reservation envelope and retry /execute.
        # Backed by GateErrorCode::BudgetRecheckFailed in the
        # backend (error_codes.rs).
        "BUDGET_RECHECK_FAILED": NullRunBudgetError,
    }


_V3_ERROR_CODE_MAP: dict[str, type[BaseException]] = _build_v3_error_code_map()


# ADR (2026-06-28, audit P2.2 close): ``_parse_error_envelope`` below
def _parse_error_envelope(
    response: httpx.Response,
    endpoint: str,
) -> Exception:
    """Translate a non-2xx ``httpx.Response`` into the right exception
    subclass per the canonical ``contracts/errors.ts`` envelope.

    4xx/5xx/429 are mapped to distinct ``RateLimitError`` /
    ``NullRunAuthenticationError`` / ``NullRunTransportError(GATEWAY_ERROR)``
    so callers branch on type instead of string-matching ``str(exc)``.

    Module-level helper (not a Transport method) so it can be called
    from background threads that do not carry a Transport instance.

    **Audit F-R2-13 (2026-06-22):** no live wire path uses this. It
    exists for tests only. See the comment block above.
    """
    status = response.status_code
    try:
        body = response.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        body = {}
    error_slug: str = body.get("error", "") or ""
    message: str = body.get("message") or response.text or f"HTTP {status}"

    if status in (401, 403):
        return NullRunAuthenticationError(
            f"Auth failed on {endpoint} (status {status}, error={error_slug!r}): {message}"
        )

    if status == 429:
        retry_after: float | None = None
        ra_header = response.headers.get("Retry-After")
        if ra_header:
            try:
                retry_after = float(ra_header)
            except ValueError:
                try:
                    from datetime import datetime, timezone
                    from email.utils import parsedate_to_datetime

                    dt = parsedate_to_datetime(ra_header)
                    retry_after = (dt - datetime.now(timezone.utc)).total_seconds()
                except Exception:
                    retry_after = None
        upgrade_url = body.get("upgrade_url") if isinstance(body, dict) else None
        return RateLimitError(
            f"Rate limited on {endpoint} (status 429, error={error_slug!r}): {message}",
            source=TransportErrorSource.GATEWAY_ERROR,
            endpoint=endpoint,
            retry_after=retry_after,
            upgrade_url=upgrade_url,
            body=body,
        )

    if 500 <= status < 600:
        return NullRunTransportError(
            f"Gateway error on {endpoint} (status {status}, error={error_slug!r}): {message}",
            source=TransportErrorSource.GATEWAY_ERROR,
            endpoint=endpoint,
            status_code=status,
            error_slug=error_slug,
        )

    return NullRunTransportError(
        f"Client error on {endpoint} (status {status}, error={error_slug!r}): {message}",
        source=TransportErrorSource.GATEWAY_ERROR,
        endpoint=endpoint,
        status_code=status,
        error_slug=error_slug,
    )


# Public surface for `from nullrun.transport import X` consumers
# (notably runtime.py). Without this list, mypy treats every
# submodule attribute as private and rejects cross-module imports
# under `--strict`. The list mirrors the symbols runtime.py
# actually consumes plus the convenience constructors / constants
# documented in the README.
__all__ = [
    "HEADER_PROTOCOL",
    "NULLRUN_PROTOCOL_VERSION",
    "DecisionSource",
    "FallbackMode",
    "FlushConfig",
    "ExecuteConfig",
    "Transport",
    "TransportErrorSource",
    "_retry_with_backoff",
    "generate_hmac_signature",
    "verify_hmac_signature",
    "_signed_request_body",
    "RateLimitError",
    "InsecureTransportError",
]
