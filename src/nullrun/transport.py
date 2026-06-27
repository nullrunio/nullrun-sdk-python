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
from typing import Any

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


def _emit_for_transport_error(
    err: BaseException,
    stage: str,
    correlation_id: str | None,
    *,
    status_code: int | None = None,
) -> None:
    """Layer 2: fire the on_error hook for transport-level raises.

    The transport module is stateless (no `self` carrying the
    runtime's api_key / workflow_id), so the context is minimal
    — just ``stage`` + ``correlation_id`` + ``status_code``. The
    hook receives ``api_key_prefix=None`` and ``workflow_id=None``
    because the transport layer does not have them.

    Best-effort: never raises. ``emit_error`` swallows hook
    exceptions internally.
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
    body: str,
) -> str:
    """
    Generate HMAC-SHA256 signature for request authentication.

    Signature = HMAC-SHA256(secret_key, timestamp + ":" + api_key + ":" + body_hash)
    Body hash = SHA256(request_body)

    This provides:
    - Authentication: API key identifies the client
    - Integrity: Body hash ensures request hasn't been tampered with
    - Freshness: Timestamp prevents replay attacks

    Args:
        api_key: Client's API key (identifier)
        secret_key: Client's secret key (used for HMAC)
        timestamp: Unix timestamp in seconds
        body: Request body as JSON string

    Returns:
        Hex-encoded HMAC-SHA256 signature
    """
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    message = f"{timestamp}:{api_key}:{body_hash}"

    signature = hmac.new(
        secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    return signature


def verify_hmac_signature(
    api_key: str,
    secret_key: str,
    timestamp: int,
    body: str,
    signature: str,
    max_age_seconds: int = 300,
) -> bool:
    """
    Verify HMAC signature from request.

    Args:
        api_key: Client's API key
        secret_key: Client's secret key
        timestamp: Unix timestamp from request
        body: Request body as JSON string
        signature: HMAC signature to verify
        max_age_seconds: Maximum allowed age of request (default 5 min)

    Returns:
        True if signature is valid and request is fresh
    """
    # Check timestamp freshness
    current_time = int(time.time())
    if abs(current_time - timestamp) > max_age_seconds:
        # §7.2 #6: separate counter so SRE can distinguish
        # "our clock drifted" from "someone is forging packets".
        # The two cases need different runbooks — NTP sync
        # vs. incident response.
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
    """Serialise a JSON payload to the canonical bytes the HMAC
    signature is computed over.

    All three signed POST call sites (``_send_batch_with_retry_info``,
    ``Transport.execute``, ``Transport.check``) MUST serialise via this
    helper and pass the result with ``content=body`` to
    ``httpx.Client.post``. Sending via ``json=...`` lets httpx
    re-serialise with its default compact separators, which produces
    a body that does NOT match the body the HMAC signature was
    computed over. The Rust server at
    ``backend/src/auth/hmac.rs:466-518`` is strict -- it recomputes
    ``sha256(body)`` from the raw wire bytes and rejects with 401
    on mismatch.
    """
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


# =============================================================================
# Retry with exponential backoff + jitter
# =============================================================================

"""
Retry with exponential backoff + jitter + Retry-After header support
"""


def _retry_with_backoff(
    func: Callable[[], Any],
    max_retries: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
    jitter: float = 0.1,
    last_retry_after_seconds: float = 0.0,
    on_transport_error: str | Callable[[Exception], dict[str, Any]] | None = None,
) -> Any:
    """
    Retry with exponential backoff and jitter, honoring Retry-After header.

    When Retry-After is provided (from backend 429 response), use it directly
    instead of exponential backoff to prevent retry storms.

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
                    # Round 3 (Phase 0.4.0): 5xx is a classified
                    # GATEWAY_ERROR. Don't retry -- this is a server
                    # bug, not a network blip. Only raise when the
                    # caller has opted into the typed-error contract
                    # via on_transport_error="raise".
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
            # Sprint 3 follow-up (B24): bump ``last_error`` so the
            # operator can read the most recent failure type without
            # grepping logs. The string is the exception class
            # name plus the message — short, searchable, and
            # doesn't leak request bodies.
            metrics.set_transport("last_error", f"{type(exc).__name__}: {exc}")
            # ``timeouts`` is a specific subcategory of retry
            # trigger — distinguished so an SRE can alert on
            # ``timeouts > N per minute`` separately from
            # generic 5xx retries.
            if isinstance(exc, (httpx.TimeoutException, httpx.ConnectTimeout, httpx.ReadTimeout)):
                metrics.inc_transport("timeouts")

            if attempt >= max_retries:
                break

            # Bump ``retries_total`` for every retry attempt
            # (not for the final failure). The counter is
            # distinct from the final BreakerTransportError —
            # it measures how often the SDK had to retry
            # because the backend was flaky.
            metrics.inc_transport("retries_total")

            # Honor Retry-After from backend if present (from 429 response)
            if last_retry_after_seconds > 0:
                actual_delay = min(last_retry_after_seconds, max_delay)
                # Reset after use so next retry uses exponential backoff
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
                # Standard jitter for retry delay -- not crypto-sensitive
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
# Fallback Modes (Phase 1 - SDK Resilience)
# =============================================================================


class FallbackMode:
    """
    SDK behavior when Gateway is unavailable.

    This is CRITICAL for production - Gateway unavailability should NOT
    block agent execution, but behavior must be defined and logged.
    """

    # Block if Gateway unavailable (for critical tools)
    STRICT = "strict"
    # Allow if Gateway unavailable, log locally (DEFAULT)
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
    max_retries: int = 3
    retry_delay: float = 1.0  # seconds
    max_buffer_size: int = 1000  # Max events before dropping oldest
    max_failed_flush: int = 10  # Circuit breaker: stop trying after this many failures


@dataclass
class ExecuteConfig:
    """Configuration for execute (strict mode) behavior."""

    # Fallback mode when Gateway is unavailable
    fallback_mode: str = FallbackMode.PERMISSIVE
    # Gateway timeout in seconds
    timeout: float = 5.0
    # Max retries for execute calls
    max_retries: int = 2
    # Cache TTL for CACHED mode (seconds)
    cache_ttl: float = 60.0
    # Cache max size
    cache_max_size: int = 10000


class Transport:
    """
    HTTP transport with batching support.

    Features:
    - Non-blocking track() calls (append to buffer)
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

        # TLS enforcement: reject non-localhost HTTP URLs. The check
        # must NOT be a startswith chain — that allowed homograph
        # attacks (http://127.0.0.1.attacker.com, http://localhost.evil.com)
        # and rejected legitimate inputs (http://[::1]:8080, http://LOCALHOST).
        # We use urllib.parse.urlparse to extract the canonical hostname,
        # then check the host against a small allow-list that includes the
        # full IPv4 loopback range (127.0.0.0/8) and IPv6 loopback (::1).
        # For IPv4 we use ``ipaddress.ip_address`` so that
        # ``127.0.0.1.attacker.com`` (a string that happens to start
        # with "127.") is NOT mistakenly treated as a loopback IP.
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
        # Phase 8 #8.4: allow env-var override of batch size and
        # flush interval. Useful for tuning high-throughput agents
        # without subclassing.
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
        self._lock = threading.RLock()  # RLock so re-entrant acquisition (e.g.
        # test fixtures that hold the lock
        # while calling lock-acquiring
        # methods) doesn't deadlock.
        self._flush_thread: threading.Thread | None = None
        self._running = False

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
        self._stopped = False  # Track if stop() was called
        # 0.7.0 thin client: no local policy cache. The backend is
        # authoritative on every gate/execute call.
        _masked = api_key[:8] + "***" if api_key and len(api_key) >= 8 else "***"
        logger.debug(f"Transport initialized: api_url={self.api_url}, api_key={_masked}")

        # OpenTelemetry tracer initialization (lazy - only if opentelemetry is installed)
        self._tracer = None
        self._propagator = None
        if _OTEL_AVAILABLE:
            self._tracer = trace.get_tracer("nullrun.transport")
            self._propagator = TraceContextTextMapPropagator()

        # Register final-flush hook via weakref.finalize so the
        # callback only fires if this Transport instance is still
        # alive at process exit. Replaces the previous
        # ``atexit.register`` (which accumulated one handler per
        # Transport in long-running deployments) and the previous
        # ``signal.signal`` handler (which hijacked SIGTERM/SIGINT
        # process-wide and called ``sys.exit(0)`` from inside the
        # signal context). The fix contract is pinned by
        # tests/test_signal_safety.py.
        self._finalizer = weakref.finalize(self, self._atexit_flush_safe)

    @staticmethod
    def _atexit_flush_safe(_self_id: int | None = None) -> None:
        """Weakref finalizer entry point.

        ``weakref.finalize`` calls this with no arguments (the
        reference to ``self`` has been dropped by the time the
        callback fires). We cannot reach into the transport from
        here — the buffer, the httpx client, and the lock are all
        gone. The recommended lifecycle is to call ``stop()``
        explicitly (or use ``Transport`` as a context manager).
        If the caller did neither, we log a one-time DEBUG line
        and return.

        The staticmethod signature accepts an optional positional
        arg so that ``weakref.finalize`` succeeds and so that
        tests can call ``_atexit_flush_safe(id(t))`` to assert
        the wrapper swallows exceptions raised by a patched
        ``_atexit_flush``.
        """
        logger.debug(
            "Transport finalizer fired without explicit stop(); "
            "remaining events may be lost. Use Transport as a context "
            "manager or call stop() explicitly."
        )

    # P1-5b: rotate the WAL when it grows past this many bytes.
    # Default 64 MB — large enough to absorb a multi-minute
    # backend outage on a busy agent, small enough that one
    # rotated file plus the active WAL never exceeds the typical
    # K8s emptyDir limit. Operators can override via
    # ``NULLRUN_WAL_MAX_BYTES``.
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
        """Resolve WAL path.

        Honours ``NULLRUN_WAL_PATH`` so crash-recovery lands on a
        writable mount in containers with
        ``readOnlyRootFilesystem: true``. Default lands in the
        platform temp dir (``tempfile.gettempdir()`` — typically
        ``/tmp`` on Linux, ``/var/folders/...`` on macOS,
        ``%TEMP%`` on Windows). Using the platform helper rather
        than a hardcoded ``/tmp`` keeps us off S108's insecure
        path list and lets the SDK work on Windows out of the
        box.
        """
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
                    f.write(json.dumps(event) + "\n")
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
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()
        logger.info("Transport flush thread started")

    def __enter__(self) -> "Transport":
        """Context-manager entry: start the flush thread and return self.

        Pairs with ``__exit__`` so callers can write
        ``with Transport(...) as t:`` and rely on ``stop()`` running
        on the way out. Replaces the manual ``start() / stop()`` pair
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

    def stop(self, timeout: float = 10.0) -> None:
        """Stop background flush thread and flush remaining events."""
        self._running = False
        self._stopped = True  # Mark as stopped to prevent double flush
        if self._flush_thread:
            self._flush_thread.join(timeout=timeout)
        self._do_flush()  # Final flush
        self._persist_to_wal()  # WAL any remaining events
        self._client.close()
        # Detach the weakref finalizer — stop() is the canonical
        # "I am done" path. After this point the finalizer will
        # silently no-op even if the interpreter is still alive.
        if getattr(self, "_finalizer", None) is not None and self._finalizer.alive:
            self._finalizer.detach()
        logger.info("Transport stopped")

    def _flush_loop(self) -> None:
        """Background loop that periodically flushes."""
        while self._running:
            time.sleep(self.config.flush_interval)
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
            # Circuit breaker is open - re-add batch to buffer for retry later
            logger.warning(f"Circuit breaker OPEN. Batch of {len(batch)} events will be re-queued.")
            # P0-4 (plan §10): drop NEWEST non-critical events instead of
            # oldest. For cost-audit the oldest events are the
            # most valuable (incident start, billing-period start) —
            # losing them would silently break per-customer monthly
            # rollups. Critical control-plane events
            # (state_change / kill_received / policy_invalidated /
            # key_rotated) are preserved unconditionally because the
            # dashboard's KILL switch has to land even under
            # sustained backend outage.
            available_space = self.config.max_buffer_size - len(self._buffer)
            if available_space < len(batch):
                overflow = len(batch) - available_space
                if overflow > 0:
                    batch = self._drop_newest_with_priority(batch, overflow)
            # Append to END (not front) so oldest events are retried first
            self._buffer.extend(batch)
            # Update metrics on failure (thread-safe)
            metrics.inc_transport("batches_failed")

    def _drain_batch(self) -> list[dict[str, Any]] | None:
        """Round 2 (Phase 0.4.0): public, lock-acquiring snapshot of
        the current buffer. Returns ``None`` when empty.

        Used by ``tests/test_buffer_invariants.py``. The full flush
        logic (CB, re-queue, metrics) lives in ``_do_flush_locked``;
        this method is the read-only counterpart.
        """
        with self._lock:
            if not self._buffer:
                return None
            batch = list(self._buffer)
            del self._buffer[:]
            return batch

    # Event types that MUST NOT be dropped on buffer overflow.
    # These are control-plane events: the dashboard's KILL/PAUSE has
    # to land even under sustained backend outage, otherwise the
    # kill-switch promise is broken (plan §11.4 P0-4 recommendation).
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
        """Drop the ``overflow`` newest NON-CRITICAL events from
        ``batch``, preserving critical events (state_change etc.)
        even when they happen to be the newest.

        Cost-audit invariant (plan §10 P0-4): under overflow we keep
        the OLDEST events because the start of an incident / start of
        the billing period is exactly what a billing investigator
        will look up first. Dropping oldest silently breaks
        monthly rollups; dropping newest does not.

        Caller invariant: ``overflow`` is the number of events that
        must be dropped to fit the buffer. We assume callers compute
        this against ``max_buffer_size - len(self._buffer)``. We
        never drop critical events even if that means slightly
        exceeding the configured limit (defensive: a brief
        transient overshoot of a few KB is cheaper than losing the
        KILL).
        """
        if overflow <= 0:
            return batch
        # Walk from the newest backwards, drop non-critical until
        # we've dropped `overflow` items. Critical events are kept in
        # place (they keep their relative order — newest critical
        # event comes after older critical events).
        kept: list[dict[str, Any]] = []
        dropped = 0
        # Reverse so we can pop from the "newest" end first while
        # rebuilding in original order.
        for event in reversed(batch):
            if dropped < overflow and event.get("type") not in self._CRITICAL_EVENT_TYPES:
                dropped += 1
                continue
            kept.append(event)
        if dropped > 0:
            logger.warning(
                f"P0-4 buffer overflow: dropped {dropped} newest non-critical "
                f"events (kept {len(kept)}, preserved {len(batch) - len(kept) - dropped} critical)"
            )
            metrics.inc_transport("events_dropped", dropped)
        # Restore original order (we iterated in reverse above).
        kept.reverse()
        return kept

    @dataclass
    class SendResult:
        accepted_event_ids: list
        retry_after_ms: float | None = None
        is_policy_limit: bool = False

    def _add_hmac_headers(self, headers: dict[str, str], body: str) -> None:
        """
        Add HMAC signing headers to request.

        Adds:
        - X-Signature-Timestamp: Unix timestamp for freshness
        - X-Signature: HMAC-SHA256(api_key, secret, timestamp, body_hash)

        Only adds signature if secret_key is configured.
        """
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
        """Build the canonical signed-headers dict for a request.

        Round 2 (Phase 0.4.0): the canonical one-call helper used
        by every signed POST. Mirrors the contract the test
        framework in ``tests/test_hmac_signing.py`` expects.

        Always includes:
        - Content-Type: application/json
        - X-API-Version: __api_version__
        - X-API-Key: when api_key is set

        Adds HMAC signature headers when secret_key is set and a
        body is provided.

        ``extra`` is merged ON TOP of the defaults so callers can
        override Content-Type or add custom headers.
        """
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "X-API-Version": __api_version__,
        }
        if self.api_key:
            headers["X-API-Key"] = self.api_key
            # FIX-F3 (counterpart of backend csrf.rs has_bearer_auth):
            # The backend's CSRF middleware bypasses cookie-based
            # double-submit checks whenever the request carries any
            # non-empty Authorization header (see
            # backend/src/auth/csrf.rs::has_bearer_auth). Without this
            # header the SDK POSTs hit the "state-changing request
            # without session cookie" branch and get 403 — which the
            # SDK's try/except in /gate, /track, /check, /execute
            # silently swallowed, so every SDK-side enforcement was
            # effectively fail-OPEN on production traffic.
            #
            # We use the user-facing api_key as the Bearer value so the
            # bypass header is meaningful for debugging; the actual
            # SDK auth path is still X-API-Key (+ HMAC when configured).
            # Bearer-style bypass is documented as safe in csrf.rs:80-95
            # because browsers never auto-attach Authorization to
            # cross-site requests, so this is not a CSRF regression.
            headers["Authorization"] = f"Bearer {self.api_key}"
        if body is not None and self.secret_key and self.api_key:
            body_str = body if isinstance(body, str) else body.decode("utf-8")
            timestamp = int(time.time())
            signature = generate_hmac_signature(self.api_key, self.secret_key, timestamp, body_str)
            headers["X-Signature-Timestamp"] = str(timestamp)
            headers["X-Signature"] = signature
        if extra:
            headers.update(extra)
        # Inject trace context (W3C) as well — matches the
        # end-to-end behaviour of every signed POST.
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
        """Send batch to server using batch endpoint. Returns SendResult with retry info.

        P0 #2: the post() call below is wrapped with _retry_with_backoff so a
        transient backend 5xx no longer drops the entire batch. Pre-fix the
        call was a single self._client.post(...) followed by raise_for_status;
        a 500 raised out of the flush path, the buffer was cleared at the
        call site, and every event in the batch was lost. See
        audit_result.md §16.B (P0 #2).
        """
        logger.debug(f"Sending batch of {len(batch)} events to {self.api_url}/api/v1/track/batch")
        headers = {"Content-Type": "application/json", "X-API-Version": __api_version__}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
            # FIX-F3: Bearer header for CSRF bypass (see _build_signed_headers).
            headers["Authorization"] = f"Bearer {self.api_key}"

        # Add HMAC signature headers
        # 2026-06-27: route through _signed_request_body for canonical
        # compact separators ((",", ":")) — matches the wire form used by
        # /execute and /gate and the docstring invariant of
        # _signed_request_body (which says "All three signed POST call
        # sites MUST serialise via this helper"). HMAC itself is unaffected
        # (it hashes the bytes either way), but consistent serialization
        # means future audits / contract tests don't have to special-case
        # this endpoint.
        # NOTE: _signed_request_body is a MODULE-LEVEL helper, not a
        # method on Transport. The two siblings in this file
        # (``execute`` and ``check``) call it without ``self.``; calling
        # ``self._signed_request_body`` here raised AttributeError on
        # every batch flush and broke 15 tests across test_transport.py
        # / test_track_batch_retry.py / test_integration_contract.py.
        body = _signed_request_body({"events": batch})
        self._add_hmac_headers(headers, body)

        # Inject trace context for distributed tracing (W3C Trace Context)
        self._inject_trace_context(headers)

        # Use batch endpoint for efficiency - single request for all events.
        # We send ``content=body`` (the exact bytes that were HMAC-signed
        # above) rather than ``json=...`` — the latter re-serialises the
        # payload with httpx defaults (compact separators) and produces
        # a body that does not match the body the HMAC signature was
        # computed over. See plan B6.
        # The inner function is the unit of retry:
        # * 5xx → raise_for_status() raises HTTPStatusError → retry helper backs off
        #   and re-attempts. 429 is included in this category (the helper honors
        #   Retry-After when present).
        # * 4xx (other than 429) → return as-is, the outer raise_for_status()
        #   surfaces it. These are real client bugs (auth, payload) and must
        #   NOT be retried — retrying a 401 just wastes the user's budget.
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

        response = _retry_with_backoff(
            _post_batch,
            max_retries=3,
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

        # Process actions from server response.
        #
        # 2026-06-27: Backend renamed BatchTrackResponse.actions_taken (Vec<String>
        # of debug names) → BatchTrackResponse.actions (Vec<ActionTaken>) with
        # human-readable strings moved to `messages`. Single /track still uses
        # TrackResponse.actions_taken (Vec<ActionTaken>). We read both for forward
        # compat, and per-element try/except so one malformed entry doesn't abort
        # the whole loop.
        try:
            data = response.json()
            actions = data.get("actions")
            if actions is None:
                actions = data.get("actions_taken", [])
            for action in actions:
                try:
                    if not isinstance(action, dict):
                        # Backend sent a legacy string or unexpected shape —
                        # log and skip, don't dispatch.
                        logger.warning(
                            "Skipping non-dict action from /track/batch: %r",
                            action,
                        )
                        continue
                    action_type = action.get("type", "")
                    workflow_id = action.get("workflow_id", "unknown")
                    reason = action.get("reason", "")
                    if action_type:
                        handle_action(action_type, workflow_id, reason)
                except Exception as item_err:
                    logger.warning(
                        "Skipping malformed action %r: %s", action, item_err
                    )
            # Display-only backend messages (renamed from `actions_taken: Vec<String>`).
            for msg in data.get("messages", []) or []:
                logger.info("Backend message: %s", msg)
        except Exception as e:
            logger.warning(f"Failed to process actions_taken: {e}")

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
    # Execute (Strict Mode) - Phase 1
    # =============================================================================

    def execute(
        self,
        organization_id: str,
        execution_id: str,
        trace_id: str,
        tool: str,
        input_data: dict[str, Any],
        mode: str = "auto",
        fallback_mode: str = FallbackMode.PERMISSIVE,
        operation_id: str | None = None,
        on_transport_error: Callable[[Exception], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """
        Pre-execution policy evaluation via the /api/v1/execute endpoint.

        This is the PRIMARY enforcement point - decision is made BEFORE execution.
        Per audit F-R2-01 (2026-06-22): the SDK MUST call /api/v1/execute (which
        checks the ``execute`` scope on the API key) rather than /api/v1/gate
        (advisory, no scope check). Calling /gate here would let an API key
        with only ``read``/``write`` scopes drive a sensitive-tool decision --
        scope gate would be skipped entirely.

        /api/v1/gate is reserved for budget pre-flight (``Transport.check``);
        see CLAUDE.md ``fail-CLOSED`` table for sensitive tools.

        Args:
            organization_id: Organization identifier
            execution_id: Execution identifier
            trace_id: Distributed trace ID
            tool: Tool to execute
            input_data: Tool input
            mode: Execution mode ("auto", "inline", "strict")
            fallback_mode: What to do if Gateway unavailable
            operation_id: Optional idempotency key
            on_transport_error: Optional callback invoked on
                ``BreakerTransportError`` (Phase 5 #5.10). When set, the
                callback's return value is returned verbatim; otherwise
                the request falls through to the ``fallback_mode``
                default. The decorator's ``_enforce_sensitive_tool``
                sets this to a closure that converts the error into a
                ``NullRunBlockedException`` (fail-CLOSED).

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
            # Audit F-R2-19 (2026-06-22): `mode` field is wire-present
            # but never read by the backend
            # (`backend/src/proxy/http/gate/internal.rs:42-54`). The
            # backend's `EnforcementMode` is selected by the route
            # handler (`gate.rs:33`, `check.rs:?`, `execute.rs:59`),
            # NOT by this string. We keep the field for now to avoid a
            # breaking change for any third-party proxies that mirror
            # the wire shape, but the SDK does NOT honour this value
            # for any local decision.
            "mode": mode,
            "operation_id": operation_id or str(uuid.uuid4()),
        }

        headers = {"Content-Type": "application/json", "X-API-Version": __api_version__}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
            # FIX-F3: Bearer header for CSRF bypass (see _build_signed_headers).
            headers["Authorization"] = f"Bearer {self.api_key}"

        # HMAC fix: serialise via the canonical-bytes helper and send
        # via content=body so the wire bytes match the signed bytes.
        # See ``_signed_request_body`` for the rationale.
        body = _signed_request_body(gate_request)
        self._add_hmac_headers(headers, body.decode("utf-8"))

        # Inject trace context for distributed tracing (W3C Trace Context)
        self._inject_trace_context(headers)

        def do_execute_request() -> httpx.Response:
            return self._client.post(
                f"{self.api_url}/api/v1/execute",
                content=body,
                headers=headers,
                timeout=5.0,
            )

        # Try Gateway with retry backoff
        try:
            response = _retry_with_backoff(
                do_execute_request,
                max_retries=2,
                base_delay=0.5,
                on_transport_error=on_transport_error,
            )

            if response.status_code == 200:
                data = response.json()
                data["decision_source"] = DecisionSource.GATEWAY
                # 0.7.0 thin client: no local policy cache. The next
                # /gate call re-reads from the backend, which is
                # authoritative.
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
            # Phase 5 #5.10: ADR-008 lets callers opt into a
            # classified-error handler. Round 3 (Phase 0.4.0):
            # on_transport_error accepts both callables AND strings:
            #   "raise"  -> raise NullRunTransportError (classified)
            #   "open"    -> return synthetic allow with FALLBACK_* source
            #   "closed"  -> return synthetic block with FALLBACK_* source
            #   callable  -> call with the breaker error, return the result
            #   None      -> fall through to the legacy fallback-mode default
            if on_transport_error == "raise":
                # Re-raise as a classified transport error.
                raise NullRunTransportError(
                    f"Gateway unreachable on /execute: {exc}",
                    source=TransportErrorSource.NETWORK_ERROR,
                    endpoint="execute",
                ) from exc
            if callable(on_transport_error):
                return on_transport_error(exc)
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
            # Round 3: classify httpx network errors at the call site.
            if on_transport_error == "raise":
                raise NullRunTransportError(
                    f"Network error on /execute: {exc}",
                    source=TransportErrorSource.NETWORK_ERROR,
                    endpoint="execute",
                ) from exc
            raise
        except NullRunAuthenticationError:
            raise  # Don't fall back on auth errors

        # All attempts failed - apply fallback mode
        # Sprint 3 follow-up (B24): bump ``fallback_mode_activations``
        # every time we reach this branch (gateway unreachable).
        # The operator alerts on a spike here as a proxy for
        # backend unavailability.
        metrics.inc_transport("fallback_mode_activations")
        if fallback_mode == FallbackMode.STRICT:
            return {
                "decision": "block",
                "decision_source": DecisionSource.FALLBACK,
                "explanation": "Gateway unavailable, fallback=STRICT",
                "policy_version": 0,
            }
        else:  # PERMISSIVE (default)
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
        }

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
            # FIX-F3: Bearer header for CSRF bypass (see _build_signed_headers).
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers["X-API-Version"] = __api_version__

        # HMAC fix: serialise via the canonical-bytes helper and send
        # via content=body so the wire bytes match the signed bytes.
        body = _signed_request_body(gate_request)
        self._add_hmac_headers(headers, body.decode("utf-8"))

        # Inject trace context for distributed tracing (W3C Trace Context)
        self._inject_trace_context(headers)

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
            # Round 3: classify network errors. By default fall
            # through to synthetic block (legacy); raise only when
            # the caller opted in via on_transport_error="raise".
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
    # WebSocket Connection (Task 6 - WebSocket Push)
    # =============================================================================

    async def connect_websocket(
        self,
        organization_id: str,
        on_state_change: Callable[[dict[str, Any]], None] | None = None,
        on_policy_invalidated: Callable[[str, str, int], None] | None = None,
        on_key_rotated: Callable[[str, str, int], None] | None = None,
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
        # Phase 6 #6.6: build the WS URL via urllib.parse instead of
        # string replace. Reject unknown schemes with a clear error.
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

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
            # FIX-F3: Bearer header for CSRF bypass (see _build_signed_headers).
            headers["Authorization"] = f"Bearer {self.api_key}"

        # Policy invalidation: 0.7.0 thin client. There is no local
        # policy cache to clear -- the next /gate or /execute call
        # re-reads from the backend. Just forward the notification
        # to the caller if one was provided.
        async def wrapped_policy_invalidated(ws_id: str, policy_id: str, new_version: int) -> None:
            logger.info(f"Policy {policy_id} invalidated (v{new_version})")
            if on_policy_invalidated:
                on_policy_invalidated(ws_id, policy_id, new_version)

        # Wrap the key rotated callback to re-fetch credentials
        async def wrapped_key_rotated(ws_id: str, key_id: str, new_version: int) -> None:
            logger.info(f"Key {key_id} rotated (v{new_version}), re-fetching credentials")
            await self._refetch_credentials()
            if on_key_rotated:
                on_key_rotated(ws_id, key_id, new_version)

        conn = WebSocketConnection(
            url=ws_url,
            headers=headers,
            api_key=self.api_key,
            secret_key=self.secret_key,
            on_state_change=on_state_change,
            on_policy_invalidated=wrapped_policy_invalidated,
            on_key_rotated=wrapped_key_rotated,
        )
        await conn.connect()
        return conn

    async def _refetch_credentials(self) -> None:
        """
        Re-fetch credentials from /auth/verify after key rotation.

        This is called when the server notifies us via WebSocket that
        our HMAC secret_key has been rotated. We need to get the new
        secret_key from the /auth/verify endpoint.

        Sprint 2.4 (B20): the previous implementation used
        ``import requests`` and bypassed every transport-layer
        invariant — the shared ``httpx.Client`` (mTLS, connection
        pool), the circuit breaker, the HMAC body signature, and
        the retry policy. It also pulled in ``requests`` as a new
        dependency that is not in ``pyproject.toml`` (a runtime
        ImportError waiting to happen on any environment where
        ``requests`` is not installed transitively).

        Post-fix: route through ``self._client`` so the same TLS
        configuration, connection pool, and HMAC signing path
        apply. Body is serialised via ``_signed_request_body`` so
        the wire bytes match the signed bytes.
        """
        try:
            payload = {"api_key": self.api_key}
            body = _signed_request_body(payload)
            headers: dict[str, str] = {
                "Content-Type": "application/json",
                "X-API-Key": self.api_key or "",
                # FIX-F3: Bearer header for CSRF bypass (see _build_signed_headers).
                "Authorization": f"Bearer {self.api_key}" if self.api_key else "",
            }
            # Re-use the same HMAC headers as /gate and /track so
            # the server's auth-verify path is consistent.
            self._add_hmac_headers(headers, body.decode("utf-8"))

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


# Audit F-R2-13 (2026-06-22): the module-level ``_parse_error_envelope``
# helper below is documented as "canonical" but is NOT called from any
# live wire path — every endpoint does its own ad-hoc
# ``response.raise_for_status()`` or status-code branch.
#
# The audit's recommendation was "either delete the helper (it's
# misleading), OR wire it up everywhere". We chose "keep but mark
# test-only" because:
#
#   1. ``tests/test_error_envelope.py`` and
#      ``tests/test_transport_branches.py`` import this helper as a
#      pure-function reference for the canonical envelope→exception
#      mapping (the test fixtures encode the contract that a future
#      refactor will need to match).
#   2. Tests are documentation. Deleting it forces the tests to
#      duplicate the mapping table, which is exactly the kind of
#      drift the helper exists to prevent.
#
# DO NOT add a new caller that uses this helper from the SDK wire
# path until every endpoint is refactored to route through it. The
# helper is currently a frozen contract test, not a live translator.
# If you wire it up everywhere, delete this comment and rename to a
# non-underscored name (it's no longer private).
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
