"""
Transport layer for NullRun SDK.

Handles HTTP communication with batching and background flush.
Includes fallback modes for Gateway unavailability.
"""

import asyncio
import atexit
import hashlib
import hmac
import json
import logging
import os
import random
import signal
import sys
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from nullrun.actions import handle_action
from nullrun.breaker.circuit_breaker import CircuitBreaker
from nullrun.breaker.exceptions import BreakerTransportError, InsecureTransportError, NullRunAuthenticationError
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


# =============================================================================
# Pool Configuration & Adaptive Pool
# =============================================================================

@dataclass
class PoolConfig:
    """Configuration for adaptive connection pool.

    Args:
        initial_connections: Starting number of connections (default: 5)
        max_connections: Maximum concurrent connections (default: 100)
        max_keepalive: Max keepalive connections (default: 20)
        acquire_timeout: Timeout for acquiring a connection (default: 30s)
        idle_timeout: Keepalive expiry (default: 60s)
        scale_up_threshold: Scale up when waiting > active * threshold (default: 2.0)
        scale_down_idle: Scale down if idle > this fraction of active (default: 0.3)
    """
    initial_connections: int = 5
    max_connections: int = 100
    max_keepalive: int = 20
    acquire_timeout: float = 30.0
    idle_timeout: float = 60.0
    scale_up_threshold: float = 2.0
    scale_down_idle: float = 0.3


class AdaptivePool:
    """Connection pool that scales based on demand.

    Uses a semaphore to limit concurrent connections. Provides backpressure
    signaling when pool is exhausted via the pool_exhausted metric.
    """

    def __init__(self, config: PoolConfig):
        self._config = config
        self._semaphore = asyncio.Semaphore(config.max_connections)
        self._active_connections = 0
        self._waiting_tasks = 0
        self._total_acquired = 0
        self._total_released = 0
        self._exhausted_count = 0
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        """Acquire connection with backpressure.

        Returns True if acquired, False if timeout (pool exhausted).
        """
        async with self._lock:
            self._waiting_tasks += 1

        try:
            acquired = await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self._config.acquire_timeout
            )
            async with self._lock:
                self._active_connections += 1
                self._total_acquired += 1
                self._waiting_tasks -= 1
            return True

        except asyncio.TimeoutError:
            async with self._lock:
                self._waiting_tasks -= 1
                self._exhausted_count += 1
            metrics.inc_transport("pool_exhausted")
            logger.warning(
                f"Pool exhausted: {self._active_connections} active, "
                f"{self._waiting_tasks} waiting, {self._exhausted_count} total exhaustions"
            )
            return False

    def release(self) -> None:
        """Release a connection back to the pool."""
        self._active_connections -= 1
        self._total_released += 1
        self._semaphore.release()

    async def scale_up_if_needed(self) -> None:
        """Increase pool size if demand is high.

        Called periodically to check if we should allow more concurrent connections.
        Scales up when waiting tasks > active connections * threshold.
        """
        async with self._lock:
            if self._waiting_tasks > self._active_connections * self._config.scale_up_threshold:
                if self._active_connections < self._config.max_connections:
                    self._semaphore.release()
                    self._active_connections += 1
                    metrics.inc_transport("pool_scaled_up")
                    logger.debug(
                        f"Scaled up pool: active={self._active_connections}, "
                        f"waiting={self._waiting_tasks}"
                    )

    async def scale_down_if_needed(self) -> None:
        """Decrease pool size if we have excess idle capacity.

        Scales down when active connections < max_connections and
        we haven't used the full pool recently.
        """
        async with self._lock:
            if self._active_connections > self._config.initial_connections:
                usage_ratio = self._active_connections / self._config.max_connections
                if usage_ratio < self._config.scale_down_idle:
                    pass  # Conservative - don't auto-scale down aggressively

    def get_stats(self) -> dict:
        """Get current pool statistics."""
        return {
            "active": self._active_connections,
            "waiting": self._waiting_tasks,
            "max": self._config.max_connections,
            "total_acquired": self._total_acquired,
            "total_released": self._total_released,
            "exhausted_count": self._exhausted_count,
        }


__api_version__ = "1.0"


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
    body_hash = hashlib.sha256(body.encode('utf-8')).hexdigest()
    message = f"{timestamp}:{api_key}:{body_hash}"

    signature = hmac.new(
        secret_key.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
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
        logger.warning(f"Request timestamp too old: {timestamp} vs current {current_time}")
        return False

    # Recompute expected signature
    expected = generate_hmac_signature(api_key, secret_key, timestamp, body)

    # Constant-time comparison to prevent timing attacks
    return hmac.compare_digest(expected, signature)


# =============================================================================
# Policy Cache for CACHED fallback mode
# =============================================================================

class CachedDecision:
    """Represents a cached execute decision."""

    def __init__(self, decision: str, policy_id: str = None, ttl_seconds: float = 300.0):
        self.decision = decision
        self.policy_id = policy_id
        self.cached_at = time.monotonic()
        self.ttl_seconds = ttl_seconds

    def is_expired(self) -> bool:
        return time.monotonic() - self.cached_at > self.ttl_seconds


class PolicyCache:
    """
    LRU cache for execute decisions. Used in CACHED fallback mode.

    Cache key is (organization_id, policy_version) to prevent cache thrashing.
    At 1000+ users with unique workflow_ids, keying by tool caused constant eviction.
    Now we key by organization + policy version, so all tools in an organization share
    the same policy cached entry until the policy version changes.
    """

    def __init__(self, maxsize: int = 1000, ttl_seconds: float = 300.0):
        self._cache: OrderedDict[str, CachedDecision] = OrderedDict()
        self._maxsize = maxsize
        self._ttl = ttl_seconds
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> CachedDecision | None:
        decision = self._cache.get(key)
        if decision is None:
            self._misses += 1
            return None
        if decision.is_expired():
            del self._cache[key]
            self._misses += 1
            return None
        self._cache.move_to_end(key)
        self._hits += 1
        return decision

    def set(self, key: str, decision: str, policy_id: str = None, policy_version: int = None) -> None:
        if key in self._cache:
            self._cache.move_to_end(key)
        elif len(self._cache) >= self._maxsize:
            self._cache.popitem(last=False)
        # Store policy_version in the decision for cache key generation
        self._cache[key] = CachedDecision(decision, policy_id, self._ttl)
        # Store policy_version as ttl_seconds field (repurposed) for reference
        if policy_version is not None:
            self._cache[key].ttl_seconds = float(policy_version)  # type: ignore[attr-defined]

    def make_key(self, organization_id: str, policy_version: int = None) -> str:
        """Generate cache key from organization_id and policy_version."""
        if policy_version is not None:
            return f"{organization_id}:{policy_version}"
        return f"{organization_id}:0"  # Default to version 0 if not provided

    def get_stats(self) -> dict:
        """Get cache statistics for observability."""
        total = self._hits + self._misses
        hit_rate = self._hits / total if total > 0 else 0.0
        return {
            "size": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": hit_rate,
        }

    def __len__(self) -> int:
        return len(self._cache)


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
                    raise NullRunAuthenticationError("Invalid API key")
                if result.status_code >= 400:
                    result.raise_for_status()

            return result

        except (BreakerTransportError, NullRunAuthenticationError):
            raise

        except Exception as exc:
            last_exc = exc

            if attempt >= max_retries:
                break

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
                delay = min(base_delay * (backoff_factor ** attempt), max_delay)
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

    raise BreakerTransportError(
        f"Request failed after {max_retries + 1} attempts"
    ) from last_exc

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
    # Use cached decision if Gateway unavailable
    CACHED = "cached"


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

        # TLS enforcement: reject non-localhost HTTP URLs
        if self.api_url.startswith('http://') and not self.api_url.startswith('http://localhost') and not self.api_url.startswith('http://127.0.0.1'):
            raise InsecureTransportError(
                f"Insecure URL detected: {self.api_url}. "
                f"HTTP is only allowed for localhost. Use https:// for production."
            )

        self.api_key = api_key
        self.secret_key = secret_key  # HMAC signing key
        self.config = config or FlushConfig()
        self._buffer: list[dict[str, Any]] = []
        self._in_flight: dict[str, dict[str, Any]] = {}  # event_id -> event for retry dedup
        self._lock = threading.Lock()
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
        self._policy_cache = PolicyCache(
            maxsize=1000,
            ttl_seconds=300.0,
        )
        _masked = api_key[:8] + "***" if api_key and len(api_key) >= 8 else "***"
        logger.debug(f"Transport initialized: api_url={self.api_url}, api_key={_masked}")

        # OpenTelemetry tracer initialization (lazy - only if opentelemetry is installed)
        self._tracer = None
        self._propagator = None
        if _OTEL_AVAILABLE:
            self._tracer = trace.get_tracer("nullrun.transport")
            self._propagator = TraceContextTextMapPropagator()

        # Register atexit handler for final flush
        atexit.register(self._atexit_flush)

        # Register signal handler for graceful shutdown
        self._signal_handler_registered = False
        self._register_signal_handlers()

    def _register_signal_handlers(self) -> None:
        """Register signal handlers for SIGTERM/SIGINT."""
        if self._signal_handler_registered:
            return

        def _handle_shutdown(signum, frame):
            logger.info(f"Received signal {signum}, initiating graceful shutdown")
            self._running = False
            self._do_flush()  # Sync flush
            self._persist_to_wal()  # Persist unflushed events to WAL
            self._client.close()
            sys.exit(0)

        signal.signal(signal.SIGTERM, _handle_shutdown)
        signal.signal(signal.SIGINT, _handle_shutdown)
        self._signal_handler_registered = True

    def _persist_to_wal(self) -> None:
        """Persist unflushed events to WAL file for replay on restart."""
        if not self._buffer:
            return
        event_count = len(self._buffer)
        wal_path = os.path.join(os.getcwd(), ".nullrun.wal")
        with open(wal_path, "a") as f:
            for event in self._buffer:
                f.write(json.dumps(event) + "\n")
        self._buffer.clear()
        logger.debug(f"Persisted {event_count} events to WAL at {wal_path}")

    def _replay_from_wal(self) -> None:
        """Replay events from WAL file on startup."""
        wal_path = os.path.join(os.getcwd(), ".nullrun.wal")
        if not os.path.exists(wal_path):
            return
        events = []
        with open(wal_path, "r") as f:
            for line in f:
                try:
                    events.append(json.loads(line.strip()))
                except json.JSONDecodeError:
                    continue
        if events:
            self._buffer.extend(events)
            self._do_flush()
        os.remove(wal_path)  # Clean up WAL after successful replay
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

    def stop(self, timeout: float = 10.0) -> None:
        """Stop background flush thread and flush remaining events."""
        self._running = False
        self._stopped = True  # Mark as stopped to prevent double flush
        if self._flush_thread:
            self._flush_thread.join(timeout=timeout)
        self._do_flush()  # Final flush
        self._persist_to_wal()  # WAL any remaining events
        self._client.close()
        # Unregister atexit to avoid double flush
        atexit.unregister(self._atexit_flush)
        logger.info("Transport stopped")

    def _atexit_flush(self) -> None:
        """Final flush on process exit. Guaranteed by atexit registration."""
        if self._stopped:
            return
        try:
            logger.debug("atexit: performing final flush")
            self._do_flush()
        except Exception as exc:
            logger.warning("atexit flush failed: %s", exc)

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
            logger.warning(
                f"Circuit breaker OPEN. Batch of {len(batch)} events will be re-queued."
            )
            # Enforce max buffer size BEFORE re-queue to prevent unbounded growth
            # Drop oldest events first to make room for new batch
            available_space = self.config.max_buffer_size - len(self._buffer)
            if available_space < len(batch):
                overflow = len(batch) - available_space
                if overflow > 0:
                    # Drop oldest from front (batch) since it hasn't been sent yet
                    logger.warning(f"Buffer overflow on CB OPEN: dropping {overflow} oldest events from pending batch")
                    batch = batch[overflow:]  # type: ignore[assignment]
                    metrics.inc_transport("events_dropped", overflow)
            # Append to END (not front) so oldest events are retried first
            self._buffer.extend(batch)
            # Update metrics on failure (thread-safe)
            metrics.inc_transport("batches_failed")

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

    def _send_batch_with_retry_info(self, batch: list[dict[str, Any]]) -> 'SendResult':
        """Send batch to server using batch endpoint. Returns SendResult with retry info."""
        logger.debug(f"Sending batch of {len(batch)} events to {self.api_url}/api/v1/track/batch")
        headers = {"Content-Type": "application/json", "X-API-Version": __api_version__}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        # Add HMAC signature headers
        body = json.dumps({"events": batch})
        self._add_hmac_headers(headers, body)

        # Inject trace context for distributed tracing (W3C Trace Context)
        self._inject_trace_context(headers)

        # Use batch endpoint for efficiency - single request for all events
        response = self._client.post(
            f"{self.api_url}/api/v1/track/batch",
            json={"events": batch},
            headers=headers,
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
            if 'rejected' in data and data['rejected']:
                rejected_info = data['rejected']
                if isinstance(rejected_info, dict):
                    if 'retry_after_ms' in rejected_info:
                        retry_after_ms = rejected_info['retry_after_ms']
                    if 'reason' in rejected_info and rejected_info['reason'] == 'policy_limit':
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

        # Process actions_taken from server response
        try:
            data = response.json()
            actions = data.get("actions_taken", [])
            for action in actions:
                action_type = action.get("type", "")
                workflow_id = action.get("workflow_id", "unknown")
                reason = action.get("reason", "")
                if action_type:
                    handle_action(action_type, workflow_id, reason)
        except Exception as e:
            logger.warning(f"Failed to process actions_taken: {e}")

        # Return accepted event_ids for retry dedup
        accepted_event_ids = data.get("accepted_event_ids", []) if 'data' in locals() else []
        logger.debug(f"Batch track: sent {len(batch)} events")
        return self.SendResult(
            accepted_event_ids=accepted_event_ids,
            retry_after_ms=retry_after_ms,
            is_policy_limit=is_policy_limit
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
    ) -> dict[str, Any]:
        """
        Pre-execution policy evaluation via unified gate endpoint.

        This is the PRIMARY enforcement point - decision is made BEFORE execution.
        Uses /api/v1/gate endpoint for unified execute + check functionality.

        Args:
            organization_id: Organization identifier
            execution_id: Execution identifier
            trace_id: Distributed trace ID
            tool: Tool to execute
            input_data: Tool input
            mode: Execution mode ("auto", "inline", "strict")
            fallback_mode: What to do if Gateway unavailable
            operation_id: Optional idempotency key

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
            "mode": mode,
            "operation_id": operation_id or str(uuid.uuid4()),
        }

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        # Add HMAC signature headers
        body = json.dumps(gate_request)
        self._add_hmac_headers(headers, body)

        # Inject trace context for distributed tracing (W3C Trace Context)
        self._inject_trace_context(headers)

        def do_gate_request() -> httpx.Response:
            return self._client.post(
                f"{self.api_url}/api/v1/gate",
                json=gate_request,
                headers=headers,
                timeout=5.0,
            )

        # Try Gateway with retry backoff
        try:
            response = _retry_with_backoff(
                do_gate_request,
                max_retries=2,
                base_delay=0.5,
            )

            if response.status_code == 200:
                data = response.json()
                data["decision_source"] = DecisionSource.GATEWAY
                # Cache successful decision for CACHED mode
                cache_key = self._policy_cache.make_key(
                    organization_id,
                    data.get("policy_version")
                )
                self._policy_cache.set(
                    cache_key,
                    data.get("decision", "allow"),
                    data.get("policy_id"),
                    data.get("policy_version")
                )
                return data  # type: ignore[no-any-return]
            elif response.status_code >= 400:
                # 4xx - don't retry, return block
                return {
                    "decision": "block",
                    "decision_source": DecisionSource.FALLBACK,
                    "explanation": f"Gateway returned {response.status_code}",
                    "policy_version": 0,
                }

        except BreakerTransportError:
            pass  # Will fall through to fallback mode
        except NullRunAuthenticationError:
            raise  # Don't fall back on auth errors

        # All attempts failed - apply fallback mode
        if fallback_mode == FallbackMode.STRICT:
            return {
                "decision": "block",
                "decision_source": DecisionSource.FALLBACK,
                "explanation": "Gateway unavailable, fallback=STRICT",
                "policy_version": 0,
            }
        elif fallback_mode == FallbackMode.CACHED:
            # Use cached decision if available
            cache_key = self._policy_cache.make_key(organization_id)
            cached = self._policy_cache.get(cache_key)
            if cached:
                logger.warning("Gateway unreachable, using cached decision for %s", tool)
                return {
                    "decision": cached.decision,
                    "decision_source": DecisionSource.CACHED,
                    "explanation": "Gateway unavailable, using cached decision",
                    "policy_version": int(cached.ttl_seconds) if cached.ttl_seconds > 0 else 0,
                }
            else:
                logger.warning(
                    "Gateway unreachable, no cache for %s, "
                    "falling back to PERMISSIVE",
                    tool
                )
                return {
                    "decision": "allow",
                    "decision_source": DecisionSource.FALLBACK,
                    "explanation": "Gateway unavailable, no cache available",
                    "policy_version": 0,
                }
        else:  # PERMISSIVE (default)
            return {
                "decision": "allow",
                "decision_source": DecisionSource.FALLBACK,
                "explanation": "Gateway unavailable, fallback=PERMISSIVE",
                "policy_version": 0,
            }

    def check(self, check_request: dict[str, Any]) -> dict[str, Any]:
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
        headers["X-API-Version"] = __api_version__

        # Add HMAC signature headers
        body = json.dumps(gate_request)
        self._add_hmac_headers(headers, body)

        # Inject trace context for distributed tracing (W3C Trace Context)
        self._inject_trace_context(headers)

        try:
            response = self._client.post(
                f"{self.api_url}/api/v1/gate",
                json=gate_request,
                headers=headers,
                timeout=5.0,
            )

            if response.status_code == 200:
                return response.json()  # type: ignore[no-any-return]
            else:
                # Return block decision on error
                return {
                    "decision": "block",
                    "reservation_id": None,
                    "remaining_budget_cents": 0,
                    "projected_cost_cents": 0,
                    "explanations": [f"Gate endpoint returned {response.status_code}"],
                    "suggestions": ["Check API availability"],
                }
        except Exception as e:
            logger.warning(f"Gate request failed: {e}")
            return {
                "decision": "block",
                "reservation_id": None,
                "remaining_budget_cents": 0,
                "projected_cost_cents": 0,
                "explanations": [f"Gate request failed: {e}"],
                "suggestions": ["Check API availability"],
            }

    # =============================================================================
    # WebSocket Connection (Task 6 - WebSocket Push)
    # =============================================================================

    def clear_policy_cache(self) -> None:
        """Clear the policy cache, forcing next gate/execute to fetch fresh policy."""
        if hasattr(self, '_policy_cache'):
            self._policy_cache._cache.clear()
            logger.debug("Policy cache cleared")

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
        from nullrun.transport_websocket import WebSocketConnection

        ws_url = self.api_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/ws/control/{organization_id}"

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        # Wrap the policy invalidated callback to clear local cache
        async def wrapped_policy_invalidated(ws_id: str, policy_id: str, new_version: int) -> None:
            logger.info(f"Policy {policy_id} invalidated (v{new_version}), clearing policy cache")
            self.clear_policy_cache()
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
        """
        try:
            import requests
            response = requests.post(
                f"{self.api_url}/auth/verify",
                json={"api_key": self.api_key},
                timeout=10,
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


class AsyncTransport:
    """
    Async HTTP transport with batching support.

    For use with asyncio-based applications.
    """

    def __init__(
        self,
        api_url: str,
        api_key: str | None = None,
        secret_key: str | None = None,
        config: FlushConfig | None = None,
        redis_client: Any = None,
        pool_config: PoolConfig | None = None,
    ):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.secret_key = secret_key  # HMAC signing key
        self.config = config or FlushConfig()
        self._pool_config = pool_config or PoolConfig()
        self._pool = AdaptivePool(self._pool_config)
        self._buffer: list[dict[str, Any]] = []
        self._in_flight: dict[str, dict[str, Any]] = {}  # event_id -> event for retry dedup
        self._lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None
        self._flush_task: asyncio.Task | None = None
        self._running = False
        self._redis_client = redis_client
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=self.config.max_failed_flush,
            recovery_timeout=30.0,
            redis_client=redis_client,
            name="async_transport",
        )
        self._last_retry_after_ms = 0.0  # P0: Store last retry_after for smart backoff
        self._last_failure_policy_limit = False  # P0: Track if last failure was policy limit
        self._last_retry_after_seconds = 0.0  # Honor Retry-After from backend (429 response)
        self._policy_cache = PolicyCache(
            maxsize=1000,
            ttl_seconds=300.0,
        )

        # OpenTelemetry tracer initialization (lazy - only if opentelemetry is installed)
        self._tracer = None
        self._propagator = None
        if _OTEL_AVAILABLE:
            self._tracer = trace.get_tracer("nullrun.async_transport")
            self._propagator = TraceContextTextMapPropagator()

    def _persist_to_wal(self) -> None:
        """Persist unflushed events to WAL file for replay on restart."""
        if not self._buffer:
            return
        event_count = len(self._buffer)
        wal_path = os.path.join(os.getcwd(), ".nullrun.wal")
        with open(wal_path, "a") as f:
            for event in self._buffer:
                f.write(json.dumps(event) + "\n")
        self._buffer.clear()
        logger.debug(f"Persisted {event_count} events to WAL at {wal_path}")

    async def _replay_from_wal_async(self) -> None:
        """Replay events from WAL file on startup (async version)."""
        wal_path = os.path.join(os.getcwd(), ".nullrun.wal")
        if not os.path.exists(wal_path):
            return
        events = []
        with open(wal_path, "r") as f:
            for line in f:
                try:
                    events.append(json.loads(line.strip()))
                except json.JSONDecodeError:
                    continue
        if events:
            self._buffer.extend(events)
            await self._flush()
        os.remove(wal_path)  # Clean up WAL after successful replay
        logger.info(f"Replayed {len(events)} events from WAL")

    async def track(self, event: dict[str, Any]) -> None:
        """Add event to buffer. Non-blocking."""
        async with self._lock:
            # Generate event_id if not provided
            if "event_id" not in event or not event["event_id"]:
                event["event_id"] = str(uuid.uuid4())

            # Store in-flight for retry dedup
            self._in_flight[event["event_id"]] = event

            self._buffer.append(event)
            metrics.inc_transport("events_enqueued")
            if len(self._buffer) >= self.config.batch_size:
                await self._flush_locked()

    async def start(self) -> None:
        """Start background flush task."""
        if self._running:
            return
        # Replay any events from WAL that were persisted due to previous crash
        await self._replay_from_wal_async()
        self._running = True
        # Configure httpx.AsyncClient with adaptive pool limits
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=5.0,
                read=30.0,
                write=10.0,
                pool=self._pool_config.acquire_timeout,
            ),
            verify=True,
            limits=httpx.Limits(
                max_connections=self._pool_config.max_connections,
                max_keepalive_connections=self._pool_config.max_keepalive,
                keepalive_expiry=self._pool_config.idle_timeout,
            ),
        )
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.info(
            f"AsyncTransport started with pool config: "
            f"max_connections={self._pool_config.max_connections}, "
            f"max_keepalive={self._pool_config.max_keepalive}"
        )

    async def stop(self, timeout: float = 10.0) -> None:
        """Stop background flush task and flush remaining events."""
        self._running = False
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await asyncio.wait_for(self._flush_task, timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("Flush task did not complete within timeout, proceeding with shutdown")
            except asyncio.CancelledError:
                pass
        await self._flush()
        self._persist_to_wal()  # WAL any remaining events
        if self._client:
            await self._client.aclose()
        logger.info("AsyncTransport stopped")

    async def _flush_loop(self) -> None:
        """Background loop that periodically flushes."""
        while self._running:
            await asyncio.sleep(self.config.flush_interval)
            if self._running:
                # Check if we should scale up the pool based on demand
                await self._pool.scale_up_if_needed()
                await self._flush()

    async def _flush(self) -> None:
        """Perform the actual flush."""
        async with self._lock:
            await self._flush_locked()

    async def _flush_locked(self) -> None:
        """Flush under lock. Must be called with _lock held."""
        if not self._buffer:
            return

        batch = self._buffer[:]
        self._buffer.clear()

        # Circuit breaker wrapped async send with pool backpressure
        async def send_batch():
            # Acquire from adaptive pool with backpressure
            acquired = await self._pool.acquire()
            if not acquired:
                # Pool exhausted - apply backpressure
                backoff = self._calculate_backoff()
                logger.warning(
                    f"Pool exhausted during flush, backing off {backoff:.2f}s "
                    f"for batch of {len(batch)} events"
                )
                # Re-add entire batch to buffer for retry
                self._buffer.extend(batch)
                metrics.inc_transport("pool_backpressure_events", len(batch))
                # Return a mock response that will trigger circuit breaker to re-queue
                raise BreakerTransportError(f"Pool exhausted, batch of {len(batch)} re-queued")

            try:
                headers = {"Content-Type": "application/json"}
                if self.api_key:
                    headers["X-API-Key"] = self.api_key
                headers["X-API-Version"] = __api_version__

                # Add HMAC signature headers
                body = json.dumps({"events": batch})
                if self.secret_key and self.api_key:
                    timestamp = int(time.time())
                    signature = generate_hmac_signature(
                        self.api_key,
                        self.secret_key,
                        timestamp,
                        body,
                    )
                    headers["X-Signature-Timestamp"] = str(timestamp)
                    headers["X-Signature"] = signature

                # Inject trace context for distributed tracing (W3C Trace Context)
                await self._inject_trace_context(headers)

                response = await self._client.post(
                    f"{self.api_url}/api/v1/track/batch",
                    json={"events": batch},
                    headers=headers,
                )

                # Extract retry info
                retry_after_seconds = self._extract_retry_after(response)
                is_policy_limit = self._is_policy_limit_response(response)
                self._last_retry_after_seconds = retry_after_seconds or 0.0
                self._last_failure_policy_limit = is_policy_limit

                # Process actions_taken from server response
                try:
                    data = response.json()
                    actions = data.get("actions_taken", [])
                    for action in actions:
                        action_type = action.get("type", "")
                        workflow_id = action.get("workflow_id", "unknown")
                        reason = action.get("reason", "")
                        if action_type:
                            handle_action(action_type, workflow_id, reason)

                    # Remove accepted events from in-flight
                    accepted_event_ids = data.get("accepted_event_ids", [])
                    for event in batch:
                        if event.get("event_id") in accepted_event_ids:
                            self._in_flight.pop(event.get("event_id"), None)
                except Exception as e:
                    logger.warning(f"Failed to process actions_taken: {e}")

                logger.debug(f"Batch track: sent {len(batch)} events")
                # Update metrics on successful flush (thread-safe)
                metrics.inc_transport("batches_sent")
                metrics.inc_transport("events_sent", len(batch))
                metrics.set_transport("last_flush_at", time.monotonic())
                return response
            finally:
                self._pool.release()

        try:
            await self._circuit_breaker.call(send_batch)
        except BreakerTransportError:
            # Circuit breaker is open - re-add batch to buffer for retry later
            logger.warning(
                f"Circuit breaker OPEN. Batch of {len(batch)} events will be re-queued."
            )
            # Enforce max buffer size BEFORE re-queue to prevent unbounded growth
            # Drop oldest events first to make room for new batch
            available_space = self.config.max_buffer_size - len(self._buffer)
            if available_space < len(batch):
                overflow = len(batch) - available_space
                if overflow > 0:
                    # Drop oldest from front (batch) since it hasn't been sent yet
                    logger.warning(f"Buffer overflow on CB OPEN: dropping {overflow} oldest events from pending batch")
                    batch = batch[overflow:]  # type: ignore[assignment]
                    metrics.inc_transport("events_dropped", overflow)
            # Append to END (not front) so oldest events are retried first
            self._buffer.extend(batch)
            # Update metrics on failure (thread-safe)
            metrics.inc_transport("batches_failed")

        # Enforce max buffer size for any remaining overflow
        if len(self._buffer) > self.config.max_buffer_size:
            overflow = len(self._buffer) - self.config.max_buffer_size
            logger.warning(f"Buffer overflow: dropping {overflow} oldest events")
            self._buffer = self._buffer[overflow:]  # type: ignore[assignment]
            metrics.inc_transport("events_dropped", overflow)

    def _extract_retry_after(self, response: httpx.Response) -> float | None:
        """Extract Retry-After header value as seconds.

        Handles both:
        - Integer seconds (e.g., "30")
        - HTTP-date format (e.g., "Wed, 21 Oct 2015 07:28:00 GMT")

        Returns seconds (not ms) to align with _last_retry_after_seconds.
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

    def _is_policy_limit_response(self, response: httpx.Response) -> bool:
        """Check if response indicates policy limit failure."""
        if response.status_code == 429:
            try:
                data = response.json()
                if 'rejected' in data and data['rejected']:
                    rejected_info = data['rejected']
                    if (
                        isinstance(rejected_info, dict) and
                        rejected_info.get('reason') == 'policy_limit'
                    ):
                        return True
            except Exception:
                logger.debug("Non-JSON response, skipping parse")
        return False

    def _calculate_backoff(self) -> float:
        """Calculate backoff delay based on retry info and jitter.

        Uses exponential backoff with jitter for retry handling.
        Honors Retry-After header from backend (in seconds) when available.
        """
        base_delay = 0.5
        max_delay = 30.0
        backoff_factor = 2.0
        jitter = 0.1

        # Honor Retry-After from backend if present (from 429 response)
        if self._last_retry_after_seconds > 0:
            delay = min(self._last_retry_after_seconds, max_delay)
            # Add small jitter to prevent thundering herd when many clients
            # have the same Retry-After value
            jitter_amount = delay * jitter
            delay = delay + random.uniform(-jitter_amount, jitter_amount)
            delay = max(0.0, delay)
            # Reset after use - next retry uses exponential backoff
            self._last_retry_after_seconds = 0.0
        else:
            delay = base_delay

        return delay

    async def _inject_trace_context(self, headers: dict[str, str]) -> None:
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

    async def flush_now(self) -> None:
        """Force immediate flush."""
        await self._flush()

    # =============================================================================
    # Execute (Strict Mode) - Phase 1
    # =============================================================================

    async def execute(
        self,
        organization_id: str,
        execution_id: str,
        trace_id: str,
        tool: str,
        input_data: dict[str, Any],
        mode: str = "auto",
        fallback_mode: str = FallbackMode.PERMISSIVE,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Pre-execution policy evaluation via unified gate endpoint.

        Uses /api/v1/gate endpoint for unified execute + check functionality.

        Args:
            organization_id: Organization identifier
            execution_id: Execution identifier
            trace_id: Distributed trace ID
            tool: Tool to execute
            input_data: Tool input
            mode: Execution mode ("auto", "inline", "strict")
            fallback_mode: What to do if Gateway unavailable
            operation_id: Optional idempotency key

        Returns:
            Dict with:
                - decision: "allow" | "block" | "flag" | "pause" | "require_approval"
                - decision_source: "gateway" | "cached" | "fallback"
                - explanation: Human-readable explanation
                - policy_version: Policy version used
                - decision_context: Context for replay (if available)
        """
        if not self._client:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=5.0,
                    read=30.0,
                    write=10.0,
                    pool=self._pool_config.acquire_timeout,
                ),
                verify=True,
                limits=httpx.Limits(
                    max_connections=self._pool_config.max_connections,
                    max_keepalive_connections=self._pool_config.max_keepalive,
                    keepalive_expiry=self._pool_config.idle_timeout,
                ),
            )

        gate_request = {
            "organization_id": organization_id,
            "execution_id": execution_id,
            "trace_id": trace_id,
            "tool": tool,
            "input": input_data,
            "mode": mode,
            "operation_id": operation_id or str(uuid.uuid4()),
        }

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        headers["X-API-Version"] = __api_version__

        # Add HMAC signature headers
        body = json.dumps(gate_request)
        if self.secret_key and self.api_key:
            timestamp = int(time.time())
            signature = generate_hmac_signature(
                self.api_key,
                self.secret_key,
                timestamp,
                body,
            )
            headers["X-Signature-Timestamp"] = str(timestamp)
            headers["X-Signature"] = signature

        # Inject trace context for distributed tracing (W3C Trace Context)
        await self._inject_trace_context(headers)

        # Try Gateway
        for attempt in range(2):
            try:
                response = await self._client.post(
                    f"{self.api_url}/api/v1/gate",
                    json=gate_request,
                    headers=headers,
                    timeout=5.0,
                )

                if response.status_code == 200:
                    data = response.json()
                    data["decision_source"] = DecisionSource.GATEWAY
                    # Cache successful decision for CACHED mode
                    cache_key = self._policy_cache.make_key(
                        organization_id,
                        data.get("policy_version")
                    )
                    self._policy_cache.set(
                        cache_key,
                        data.get("decision", "allow"),
                        data.get("policy_id"),
                        data.get("policy_version")
                    )
                    return data  # type: ignore[no-any-return]
                elif response.status_code >= 500:
                    # Gateway error - try fallback
                    logger.warning(f"Gateway returned {response.status_code}, trying fallback")
                    continue
                else:
                    # 4xx - don't retry, return block
                    return {
                        "decision": "block",
                        "decision_source": DecisionSource.FALLBACK,
                        "explanation": f"Gateway returned {response.status_code}",
                        "policy_version": 0,
                    }
            except Exception as e:
                logger.warning(f"Execute attempt {attempt + 1} failed: {e}")
                if attempt < 1:
                    await asyncio.sleep(0.5)

        # All attempts failed - apply fallback mode
        if fallback_mode == FallbackMode.STRICT:
            return {
                "decision": "block",
                "decision_source": DecisionSource.FALLBACK,
                "explanation": "Gateway unavailable, fallback=STRICT",
                "policy_version": 0,
            }
        elif fallback_mode == FallbackMode.CACHED:
            # Use cached decision if available
            cache_key = self._policy_cache.make_key(organization_id)
            cached = self._policy_cache.get(cache_key)
            if cached:
                logger.warning("Gateway unreachable, using cached decision for %s", tool)
                return {
                    "decision": cached.decision,
                    "decision_source": DecisionSource.CACHED,
                    "explanation": "Gateway unavailable, using cached decision",
                    "policy_version": int(cached.ttl_seconds) if cached.ttl_seconds > 0 else 0,
                }
            else:
                logger.warning(
                    "Gateway unreachable, no cache for %s, "
                    "falling back to PERMISSIVE",
                    tool
                )
                return {
                    "decision": "allow",
                    "decision_source": DecisionSource.FALLBACK,
                    "explanation": "Gateway unavailable, no cache available",
                    "policy_version": 0,
                }
        else:  # PERMISSIVE (default)
            return {
                "decision": "allow",
                "decision_source": DecisionSource.FALLBACK,
                "explanation": "Gateway unavailable, fallback=PERMISSIVE",
                "policy_version": 0,
            }

    async def check(self, check_request: dict[str, Any]) -> dict[str, Any]:
        """
        Call /api/v1/gate endpoint for pre-execution budget checking.

        Uses the unified gate endpoint with check_type for budget validation.
        Async version for asyncio-based applications.

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
        if not self._client:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=5.0,
                    read=30.0,
                    write=10.0,
                    pool=self._pool_config.acquire_timeout,
                ),
                verify=True,
                limits=httpx.Limits(
                    max_connections=self._pool_config.max_connections,
                    max_keepalive_connections=self._pool_config.max_keepalive,
                    keepalive_expiry=self._pool_config.idle_timeout,
                ),
            )

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
        headers["X-API-Version"] = __api_version__

        # Add HMAC signature headers
        body = json.dumps(gate_request)
        if self.secret_key and self.api_key:
            timestamp = int(time.time())
            signature = generate_hmac_signature(
                self.api_key,
                self.secret_key,
                timestamp,
                body,
            )
            headers["X-Signature-Timestamp"] = str(timestamp)
            headers["X-Signature"] = signature

        # Inject trace context for distributed tracing (W3C Trace Context)
        await self._inject_trace_context(headers)

        try:
            response = await self._client.post(
                f"{self.api_url}/api/v1/gate",
                json=gate_request,
                headers=headers,
                timeout=5.0,
            )

            if response.status_code == 200:
                return response.json()  # type: ignore[no-any-return]
            else:
                return {
                    "decision": "block",
                    "reservation_id": None,
                    "remaining_budget_cents": 0,
                    "projected_cost_cents": 0,
                    "explanations": [f"Gate endpoint returned {response.status_code}"],
                    "suggestions": ["Check API availability"],
                }
        except Exception as e:
            logger.warning(f"Gate request failed: {e}")
            return {
                "decision": "block",
                "reservation_id": None,
                "remaining_budget_cents": 0,
                "projected_cost_cents": 0,
                "explanations": [f"Gate request failed: {e}"],
                "suggestions": ["Check API availability"],
            }