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


# =============================================================================

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

    def clear(self) -> None:
        """Drop every cached decision. Counters (hits/misses) are
        preserved so observability dashboards can still see the
        lifetime aggregate after a clear.
        """
        self._cache.clear()


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

    Re-raises the original exception on retry exhaustion so callers can
    inspect the concrete cause (httpx.ConnectError for network, an
    HTTPStatusError for 5xx, etc.) and produce a classified
    `decision_source`. The legacy `BreakerTransportError` wrapper
    that used to be raised here conflated "CB OPEN" and "retries
    exhausted" — ADR-008 requires the caller to tell the two apart.
    """
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            result = func()

            if hasattr(result, "status_code"):
                if result.status_code == 401:
                    raise NullRunAuthenticationError("Invalid API key")
                # Do NOT raise on 4xx/5xx here — the caller wants to
                # inspect the response (decide on_transport_error,
                # fall back, etc.) without the retry helper converting
                # a 5xx into an HTTPStatusError that masks the
                # status code. 4xx non-auth responses are real
                # gateway decisions and should NOT be retried either,
                # so the caller short-circuits on the first such
                # response — the retry loop is only useful for
                # network-level flakes.

            return result

        except (BreakerTransportError, NullRunAuthenticationError):
            # CB OPEN or auth — do not retry. Re-raise so the caller
            # classifies.
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

    # Retries exhausted. Re-raise the original exception so the caller
    # can inspect the concrete cause. If the helper was somehow
    # called with no exception (defensive), fall back to a generic
    # BreakerTransportError.
    if last_exc is not None:
        raise last_exc
    raise BreakerTransportError(
        f"Request failed after {max_retries + 1} attempts"
    )


# =============================================================================
# Transport-error routing (ADR-008)
# =============================================================================
# When the policy engine is unreachable, the caller has to decide
# between fail-OPEN ("let the call through with a flagged decision
# so the dashboard can show it") and fail-CLOSED ("block the call
# so a denied `charge_card()` cannot run during an outage"). The
# contract is declared per-call via `on_transport_error`:
#
#   "raise"   → raise NullRunTransportError so the calling gate
#               (e.g. `_enforce_sensitive_tool`) can apply its own
#               fail-OPEN/CLOSED rule. Default for /execute.
#   "open"    → return synthetic allow with `decision_source` set
#               to the classified source. Used for fail-OPEN callers
#               that want the dict shape instead of an exception.
#   "closed"  → return synthetic block with `decision_source` set
#               to the classified source. Used for fail-CLOSED
#               callers that want the dict shape.
#   "legacy"  → use the historical `fallback_mode` (STRICT / CACHED /
#               PERMISSIVE) to decide. Preserved for backward compat.

_TRANSPORT_ERROR_RESULTS = {
    "open": {
        "decision": "allow",
        "decision_source": None,  # filled in by the handler with the source
        "explanation": "Gateway unavailable, fail-OPEN",
        "policy_version": 0,
    },
    "closed": {
        "decision": "block",
        "decision_source": None,  # filled in by the handler with the source
        "explanation": "Gateway unavailable, fail-CLOSED",
        "policy_version": 0,
    },
}


def _handle_transport_error(
    mode: str,
    source: "TransportErrorSource",
    endpoint: str,
    detail: str,
) -> dict[str, Any] | None:
    """
    Route a transport failure to the declared caller policy. See the
    module-level ADR-008 contract for the four `mode` values.

    Returns a dict ONLY when the caller asked for a dict (open /
    closed). For `mode == "raise"` this raises
    `NullRunTransportError` and does not return. For
    `mode == "legacy"` the caller is expected to apply its own
    `fallback_mode` logic, so this returns None as a sentinel —
    the public method's post-handler fallback block is what actually
    runs in that case.
    """
    if mode == "raise":
        raise NullRunTransportError(
            detail,
            source=source,
            endpoint=endpoint,
        )
    if mode == "open":
        return {
            "decision": "allow",
            "decision_source": source,
            "explanation": (
                f"Gateway unavailable ({source.value}), fail-OPEN: {detail}"
            ),
            "policy_version": 0,
        }
    if mode == "closed":
        return {
            "decision": "block",
            "decision_source": source,
            "explanation": (
                f"Gateway unavailable ({source.value}), fail-CLOSED: {detail}"
            ),
            "policy_version": 0,
        }
    if mode == "legacy":
        return None  # caller applies fallback_mode itself
    # Unknown mode: fail-OPEN by default. Wrong is better than silent.
    logger.warning(
        "Unknown on_transport_error=%r; falling back to raise", mode
    )
    raise NullRunTransportError(detail, source=source, endpoint=endpoint)


def _parse_error_envelope(
    response: httpx.Response,
    endpoint: str,
) -> Exception:
    """Translate a non-2xx ``httpx.Response`` into the right exception
    subclass per the canonical ``contracts/errors.ts`` envelope.

    Phase 4 of the production-readiness plan: previously every
    4xx / 5xx was raised as a generic ``NullRunTransportError``,
    which lost the ``error`` slug (``rate_limit_exceeded`` /
    ``unauthorized`` / …) the operator needs to classify the
    failure. We map the most common slugs to distinct
    ``RateLimitError`` / ``NullRunAuthenticationError`` /
    ``NullRunTransportError(GATEWAY_ERROR)`` so callers can
    branch on the type instead of string-matching ``str(exc)``.

    Args:
        response: The non-2xx ``httpx.Response`` from the gateway.
        endpoint: A short string naming the endpoint the request
            targeted (``"track"``, ``"gate"``, ``"evaluate"``,
            ``"status"``). Embedded in the raised exception so
            callers can implement endpoint-specific retry policy.

    Returns:
        A concrete exception instance — always a subclass of
        ``BreakerError``. The caller is expected to ``raise`` it.
    """
    status = response.status_code
    # Best-effort parse of the JSON envelope. Some endpoints
    # (e.g. NGINX 502 pages) don't return JSON; we tolerate
    # that and fall back to the raw status code.
    try:
        body = response.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        body = {}
    error_slug: str = body.get("error", "") or ""
    message: str = (
        body.get("message")
        or response.text
        or f"HTTP {status}"
    )

    # 401 / 403 — auth-class. Per ADR-008 these are NEVER silenced
    # by ``on_transport_error``; the SDK propagates the failure
    # so the runtime can re-run ``auth/verify`` and retry once
    # (Phase 4: ``_authenticate`` does this transparently for
    # direct runtime calls; transport.execute() / transport.check()
    # leave the exception for the caller).
    if status in (401, 403):
        return NullRunAuthenticationError(
            f"Auth failed on {endpoint} (status {status}, "
            f"error={error_slug!r}): {message}"
        )

    # 429 — rate-limit. The gateway sends ``Retry-After`` as a
    # standard HTTP header (preferred) and may also include
    # ``retry_after`` / ``upgrade_url`` in the JSON body. We
    # honour both and surface them on the raised exception.
    if status == 429:
        # ``_extract_retry_after`` is a method on ``Transport``,
        # not a module-level helper. We replicate the parser
        # inline so the envelope helper can be called without a
        # transport instance (the body parsing in particular
        # runs from background threads that don't carry the
        # full transport state).
        retry_after: float | None = None
        ra_header = response.headers.get("Retry-After")
        if ra_header:
            try:
                retry_after = float(ra_header)
            except ValueError:
                try:
                    from email.utils import parsedate_to_datetime
                    from datetime import datetime, timezone
                    dt = parsedate_to_datetime(ra_header)
                    retry_after = (
                        dt - datetime.now(timezone.utc)
                    ).total_seconds()
                except Exception:
                    retry_after = None
        upgrade_url = body.get("upgrade_url") if isinstance(body, dict) else None
        return RateLimitError(
            f"Rate limited on {endpoint} (status 429, error={error_slug!r}): "
            f"{message}",
            source=TransportErrorSource.GATEWAY_ERROR,
            endpoint=endpoint,
            retry_after=retry_after,
            upgrade_url=upgrade_url,
            body=body,
        )

    # 5xx — server-side. Distinct exception type so callers can
    # branch on it (e.g. trigger a circuit-breaker backoff vs.
    # 4xx which is a permanent client error).
    if 500 <= status < 600:
        return NullRunTransportError(
            f"Gateway error on {endpoint} (status {status}, "
            f"error={error_slug!r}): {message}",
            source=TransportErrorSource.GATEWAY_ERROR,
            endpoint=endpoint,
            status_code=status,
            error_slug=error_slug,
        )

    # 4xx (non-auth) — the gateway explicitly rejected the
    # request. We surface as ``NullRunTransportError`` with the
    # slug embedded so the caller can decide whether to retry.
    return NullRunTransportError(
        f"Client error on {endpoint} (status {status}, "
        f"error={error_slug!r}): {message}",
        source=TransportErrorSource.GATEWAY_ERROR,
        endpoint=endpoint,
        status_code=status,
        error_slug=error_slug,
    )

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

        # mTLS client certificate support.
        #
        # Phase 6 of the production-readiness plan: the three env
        # vars ``NULLRUN_TLS_CLIENT_CERT``, ``NULLRUN_TLS_CLIENT_KEY``,
        # and ``NULLRUN_TLS_CA_CERT`` are read here and wired into
        # the underlying ``httpx.Client``. The contract is
        # documented in ``audits/new_audit_ux.md:876-887`` and is
        # the SDK-facing surface for the platform's opt-in mTLS
        # mode (server-side flag ``TLS_CLIENT_AUTH_ENABLED=true``).
        #
        # When BOTH ``NULLRUN_TLS_CLIENT_CERT`` and
        # ``NULLRUN_TLS_CLIENT_KEY`` are set, the SDK presents a
        # client certificate during the TLS handshake (mutual
        # auth). When only ``NULLRUN_TLS_CA_CERT`` is set, the SDK
        # uses it as the trust anchor for verifying the server
        # certificate (one-way TLS with a private CA, common in
        # staging). When NONE are set, the platform's public CA
        # chain is used.
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

        # Register a weakref-based atexit handler. The closure holds
        # a weakref to self; if the transport has been GC'd by the
        # time the process exits, the atexit becomes a no-op. This
        # replaces the previous signal-handler-and-atexit pair, which
        # (a) overwrote the application's global SIGTERM/SIGINT
        # handlers on every Transport() construction, (b) called
        # sys.exit(0) from a signal context, and (c) did file I/O
        # from a signal context — all of which are unsafe in
        # long-lived services.
        #
        # Callers are now responsible for shutdown in long-lived
        # services: either call `transport.stop()` explicitly, use
        # `transport` as a context manager, or rely on
        # `weakref.finalize` (registered below) to fire on GC.
        weakref.finalize(
            self,
            self._atexit_flush_safe,
            id(self),  # bind for debug log
        )

    def _atexit_flush_safe(self, instance_id: int) -> None:
        """Final-flush callable used by `weakref.finalize` / `atexit`.

        Wraps `_atexit_flush` so an exception in the flush does NOT
        propagate to the interpreter's atexit machinery (which would
        silently swallow the next atexit handler — a real footgun
        in multi-Transport processes).
        """
        try:
            self._atexit_flush()
        except Exception as exc:  # noqa: BLE001 — last-chance hook
            logger.warning(
                "atexit flush failed for transport id=%s: %s", instance_id, exc
            )

    def _persist_to_wal(self) -> None:
        """Persist unflushed events to WAL file for replay on restart.

        Location precedence:
          1. `NULLRUN_WAL_PATH` env var (operator override).
          2. `<tmp>/.nullrun.wal` (per-user, OS-appropriate temp dir
             — `/tmp` on Linux, `C:\\Users\\<u>\\AppData\\Local\\Temp`
             on Windows, etc.). This replaced the previous
             `os.getcwd()/.nullrun.wal` location, which was unsafe in
             long-lived production services (WAL would land in
             whatever directory the SDK was started from).
        """
        if not self._buffer:
            return
        event_count = len(self._buffer)
        wal_path = self._wal_path()
        with open(wal_path, "a") as f:
            for event in self._buffer:
                f.write(json.dumps(event) + "\n")
        self._buffer.clear()
        logger.debug(f"Persisted {event_count} events to WAL at {wal_path}")

    def _replay_from_wal(self) -> None:
        """Replay events from WAL file on startup."""
        wal_path = self._wal_path()
        if not os.path.exists(wal_path):
            return
        events = []
        with open(wal_path) as f:
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

    @staticmethod
    def _wal_path() -> str:
        """Resolve the WAL file path. See `_persist_to_wal` for the
        precedence rules."""
        configured = os.environ.get("NULLRUN_WAL_PATH")
        if configured:
            return configured
        import tempfile
        return os.path.join(tempfile.gettempdir(), ".nullrun.wal")

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
        """Stop background flush thread and flush remaining events.

        Callers in long-lived services MUST call this explicitly
        (or use the `Transport` as a context manager) — the SDK
        no longer installs process-wide signal handlers. See the
        class docstring for the recommended lifecycle.
        """
        self._running = False
        self._stopped = True  # Mark as stopped to prevent double flush
        if self._flush_thread:
            self._flush_thread.join(timeout=timeout)
        self._do_flush()  # Final flush
        self._persist_to_wal()  # WAL any remaining events
        self._client.close()
        logger.info("Transport stopped")

    def __enter__(self) -> "Transport":
        """Context manager entry. Starts the background flush thread.

        Usage:
            with Transport(api_url=..., api_key=...) as t:
                t.track(...)
            # Final flush + WAL written; weakref.finalize fires on GC.
        """
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        """Context manager exit. Stops the background flush thread."""
        self.stop()

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

    def _drain_batch(self) -> list[dict[str, Any]] | None:
        """Atomically snapshot + clear the in-memory buffer.

        Returns the batch to send (or None if the buffer is empty).
        Must be called with `self._lock` held. This is the only
        code path that mutates `_buffer` outside of `_buffer.append`
        in `track()` — the previous contract had two distinct bugs:

        1. `self._buffer[:]` + `self._buffer.clear()` was not atomic
           against concurrent `track()` calls, which could lose
           events appended between the snapshot and the clear.
        2. The CB-OPEN re-queue path did `self._buffer = self._buffer[overflow:]`,
           which re-bound the attribute to a new list — any
           concurrent `track()` call that captured a reference to
           the old list would silently drop into dead memory.

        Both are fixed by centralizing all drain/clear operations
        through this single helper.
        """
        if not self._buffer:
            return None
        # In-place slice is essential: it mutates the existing list
        # in place rather than rebinding the attribute. Any code
        # that holds a reference to the list (e.g. an in-flight
        # `track()` call) sees the post-drain state, not stale data.
        batch = self._buffer[:]
        del self._buffer[:]
        return batch

    def _do_flush_locked(self) -> None:
        """Flush under lock. Must be called with _lock held."""
        batch = self._drain_batch()
        if batch is None:
            logger.debug("Buffer empty, skipping flush")
            return
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
            # Enforce max buffer size BEFORE re-queue. We check the
            # batch's own size against the configured ceiling, not
            # the current buffer length (the buffer is empty after
            # `_drain_batch` — checking it would be a no-op). If the
            # batch alone is larger than max_buffer_size, drop the
            # oldest events from the batch before re-queuing.
            if len(batch) > self.config.max_buffer_size:
                overflow = len(batch) - self.config.max_buffer_size
                logger.warning(
                    f"Batch of {len(batch)} exceeds max_buffer_size="
                    f"{self.config.max_buffer_size}; dropping {overflow} oldest"
                )
                batch = batch[overflow:]
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

    def _build_signed_headers(
        self,
        body: str | None,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """
        Build the canonical header set for a gateway request.

        Includes:
          - Content-Type: application/json
          - X-API-Version: __api_version__
          - X-API-Key: <api_key> (when set)
          - X-Signature + X-Signature-Timestamp (when both api_key and
            secret_key are set; signature is computed over the exact
            `body` bytes the client will transmit)
          - W3C trace context (when opentelemetry is installed)

        The `extra` dict is merged in last so callers can override
        defaults (e.g. add `Authorization: Bearer …` if needed).

        Returns a new dict; the caller's `extra` is not mutated.
        """
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "X-API-Version": __api_version__,
        }
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if body is not None:
            self._add_hmac_headers(headers, body)
        self._inject_trace_context(headers)
        if extra:
            for k, v in extra.items():
                headers[k] = v
        return headers

    def _signed_post(
        self,
        path: str,
        payload: dict[str, Any],
        extra_headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """
        POST to the gateway with HMAC signing, trace context, and
        the canonical header set applied automatically.

        Args:
            path: URL path (e.g. ``/api/v1/track/batch``). Joined
                onto ``self.api_url`` with a single ``/`` separator.
            payload: JSON-serialisable body. The exact bytes
                produced by ``json.dumps(payload)`` are signed.
            extra_headers: Optional extra headers merged on top
                of the defaults (X-API-Key, X-Signature, etc.).
            timeout: Per-request timeout in seconds. When ``None``
                the shared client's default is used.

        Returns:
            The ``httpx.Response`` — the caller is responsible
            for inspecting the status code and body. The transport
            does NOT raise on 4xx/5xx (per HTTP semantics); the
            caller routes the result through ``parse_error_envelope``
            for typed error handling.
        """
        body = json.dumps(payload)
        headers = self._build_signed_headers(body, extra_headers)
        url = f"{self.api_url}{path}" if not path.startswith("/") else f"{self.api_url}{path}"
        kwargs: dict[str, Any] = {"headers": headers, "content": body}
        if timeout is not None:
            kwargs["timeout"] = timeout
        return self._client.post(url, **kwargs)

    def _signed_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """
        Generic signed request to the gateway. Used for GET (no body
        → no signature) and for non-JSON bodies.

        Args:
            method: HTTP verb (``GET``, ``POST``, …). Case-insensitive.
            path: URL path joined onto ``self.api_url``.
            payload: JSON-serialisable body. When provided, the
                exact bytes produced by ``json.dumps(payload)`` are
                signed. When ``None`` (e.g. GET), no signature is
                added — the server treats unsigned GETs as
                authenticated via the ``X-API-Key`` header.
            extra_headers: Optional extra headers merged on top of
                the defaults.
            timeout: Per-request timeout in seconds. When ``None``
                the shared client's default is used.
        """
        body: str | None = None
        if payload is not None:
            body = json.dumps(payload)
        headers = self._build_signed_headers(body, extra_headers)
        url = f"{self.api_url}{path}" if not path.startswith("/") else f"{self.api_url}{path}"
        kwargs: dict[str, Any] = {"headers": headers}
        if body is not None:
            kwargs["content"] = body
        if timeout is not None:
            kwargs["timeout"] = timeout
        return self._client.request(method.upper(), url, **kwargs)

    def post_signed_with_401_retry(
        self,
        path: str,
        payload: dict[str, Any],
        reauth_callback: Callable[[], bool] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """
        POST to the gateway with HMAC signing AND a one-shot
        re-authentication retry on HTTP 401.

        Phase 4: the server can rotate an API key out from under
        the SDK (via the dashboard's "rotate" button, the WS
        ``KeyRotated`` event, or the cron job). The first request
        after the rotation returns 401; the SDK re-calls
        ``auth/verify`` to pick up the new ``secret_key``, then
        retries the original request. A second 401 propagates as
        ``NullRunAuthenticationError``.

        Args:
            path: URL path joined onto ``self.api_url``.
            payload: JSON-serialisable body. Signed with the
                current ``self.secret_key`` (which may be the
                freshly-rotated key after the first 401).
            reauth_callback: A no-arg callable that re-fetches
                credentials. The SDK does not know about the
                runtime directly; the runtime wires this in
                (typically ``lambda: self._authenticate()``).
                When ``None``, the first 401 propagates as-is.
            timeout: Per-request timeout in seconds.

        Returns:
            The ``httpx.Response`` (after at most one re-auth +
            retry). The caller is responsible for inspecting
            the status code; success is 2xx, anything else is
            routed through ``_parse_error_envelope`` for typed
            exception raising.
        """
        response = self._signed_post(path, payload, timeout=timeout)
        if response.status_code == 401 and reauth_callback is not None:
            try:
                reauthenticated = reauth_callback()
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    f"401 retry: reauth_callback raised: {exc}; "
                    "propagating the original 401"
                )
                return response
            if reauthenticated:
                # Re-sign the body with the freshly-rotated key.
                response = self._signed_post(path, payload, timeout=timeout)
        return response

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
        # Route through _signed_post so HMAC + W3C trace context +
        # canonical headers (X-API-Key, X-API-Version) are applied
        # in one place. Phase 1 of the production-readiness plan:
        # HMAC always-on when secret_key is present.
        response = self._signed_post(
            "/api/v1/track/batch",
            {"events": batch},
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

        # Phase 4: handle 429 with a typed ``RateLimitError`` so the
        # caller (background flush thread, runtime.execute, …) can
        # branch on the exception type instead of parsing ``str(exc)``.
        # We still store the parsed ``Retry-After`` on the transport
        # so the existing backoff machinery in the flush thread
        # keeps working.
        if response.status_code == 429:
            retry_after = self._extract_retry_after(response)
            if retry_after:
                self._last_retry_after_seconds = retry_after
            # _parse_error_envelope returns a concrete RateLimitError
            # instance; raising it surfaces the structured
            # ``retry_after`` / ``upgrade_url`` to the caller.
            raise _parse_error_envelope(response, "track")
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
        on_transport_error: str = "raise",
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
            fallback_mode: What to do if Gateway unavailable. Used only
                when `on_transport_error="legacy"`.
            operation_id: Optional idempotency key
            on_transport_error: How to react when the transport cannot
                reach the gateway. One of:
                  - "raise"   → raise `NullRunTransportError` with a
                    classified `source` (NETWORK_ERROR / GATEWAY_ERROR /
                    BREAKER_OPEN). Default per ADR-008.
                  - "open"    → return a synthetic allow with
                    `decision_source = NETWORK_ERROR` (or GATEWAY_ERROR
                    on 5xx). Use for fail-OPEN callers.
                  - "closed"  → return a synthetic block with
                    `decision_source = NETWORK_ERROR`. Use for fail-CLOSED
                    callers that want the dict shape instead of an
                    exception.
                  - "legacy"  → use the historical `fallback_mode` to
                    decide what to do. Kept for backward compatibility.

        Returns:
            Dict with:
                - decision: "allow" | "block" | "flag" | "pause" | "require_approval"
                - decision_source: "gateway" | "cached" | "fallback"
                - explanation: Human-readable explanation
                - policy_version: Policy version used
                - decision_context: Context for replay (if available)

        Raises:
            NullRunTransportError: When the transport fails AND
                `on_transport_error="raise"`. Carries `source` and
                `endpoint` for the calling gate to apply its declared
                fail-OPEN/CLOSED policy.
            NullRunAuthenticationError: On 401/403 from the gateway
                regardless of `on_transport_error` (never silenced).
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

        def do_gate_request() -> httpx.Response:
            # Route through _signed_post so HMAC + W3C trace context
            # are applied automatically (Phase 1).
            return self._signed_post(
                "/api/v1/gate",
                gate_request,
                timeout=5.0,
            )

        # Try Gateway with retry on network + 5xx. We use a custom
        # loop (rather than `_retry_with_backoff`) so we can treat
        # 5xx as a retryable transport error without losing the
        # status code on exhaustion.
        last_status: int | None = None
        last_exc: BaseException | None = None
        for _attempt in range(3):
            try:
                response = do_gate_request()
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout, httpx.RequestError) as exc:
                last_exc = exc
                time.sleep(0.1)
                continue
            if 200 <= response.status_code < 300:
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
            if 500 <= response.status_code < 600:
                last_status = response.status_code
                time.sleep(0.1)
                continue
            if response.status_code == 401:
                # Auth errors are NEVER silenced by on_transport_error —
                # they indicate a real credential problem and must
                # propagate so the SDK re-checks the key on next init.
                raise NullRunAuthenticationError(
                    f"Auth failed with status {response.status_code}. "
                    f"API key may be invalid or expired."
                )
            # 4xx (non-auth): a real gateway decision, not a
            # transport failure. Return a block so the caller sees
            # the gateway's verdict.
            return {
                "decision": "block",
                "decision_source": DecisionSource.FALLBACK,
                "explanation": f"Gateway returned {response.status_code}",
                "policy_version": 0,
            }

        # All 3 attempts exhausted. Classify and route.
        if last_status is not None and 500 <= last_status < 600:
            result = _handle_transport_error(
                on_transport_error,
                TransportErrorSource.GATEWAY_ERROR,
                "execute",
                f"gateway returned {last_status} after 3 attempts",
            )
            if result is not None:
                return result
        elif last_exc is not None:
            result = _handle_transport_error(
                on_transport_error,
                TransportErrorSource.NETWORK_ERROR,
                "execute",
                f"{type(last_exc).__name__}: {last_exc}",
            )
            if result is not None:
                return result

        # The "legacy" branch falls through to the historical
        # fallback_mode handling. Only reachable when the caller
        # passes on_transport_error="legacy".
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

    def check(
        self,
        check_request: dict[str, Any],
        on_transport_error: str = "raise",
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
            on_transport_error: Same as `Transport.execute()`. Default
                is "raise" per ADR-008 — the calling gate (e.g.
                `check_workflow_budget`) is expected to wrap the
                call in a `try/except NullRunTransportError` to
                implement its own fail-OPEN/CLOSED policy. Use
                "open" / "closed" to get a dict shape with the
                classified `decision_source` instead.

        Returns:
            Dict with:
                - decision: "allow" | "block" | "throttle"
                - reservation_id: Optional reservation ID
                - remaining_budget_cents: Remaining budget
                - projected_cost_cents: Projected cost for this operation
                - explanations: List of explanation strings
                - suggestions: List of suggestion strings

        Raises:
            NullRunTransportError: On transport failure AND
                `on_transport_error="raise"`. Carries `source` and
                `endpoint` for the calling gate to apply its declared
                fail-OPEN/CLOSED policy.
            NullRunAuthenticationError: On 401 (never silenced).
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

        # Custom retry loop: retry on network + 5xx. Unlike
        # `_retry_with_backoff` we keep the status code on
        # exhaustion so the caller can route through
        # `on_transport_error` with a classified source.
        last_status: int | None = None
        last_exc: BaseException | None = None
        for _attempt in range(3):
            try:
                # Route through _signed_post so HMAC + W3C trace
                # context are applied automatically (Phase 1).
                response = self._signed_post(
                    "/api/v1/gate",
                    gate_request,
                    timeout=5.0,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout, httpx.RequestError) as exc:
                last_exc = exc
                time.sleep(0.1)
                continue
            if 200 <= response.status_code < 300:
                return response.json()  # type: ignore[no-any-return]
            if 500 <= response.status_code < 600:
                last_status = response.status_code
                time.sleep(0.1)
                continue
            if response.status_code == 401:
                raise NullRunAuthenticationError(
                    f"Auth failed with status {response.status_code}. "
                    f"API key may be invalid or expired."
                )
            # 4xx (non-auth): a real gateway decision, not a
            # transport failure.
            return {
                "decision": "block",
                "reservation_id": None,
                "remaining_budget_cents": 0,
                "projected_cost_cents": 0,
                "explanations": [f"Gate endpoint returned {response.status_code}"],
                "suggestions": ["Check API availability"],
            }

        # All 3 attempts exhausted. Classify and route.
        if last_status is not None and 500 <= last_status < 600:
            result = _handle_transport_error(
                on_transport_error,
                TransportErrorSource.GATEWAY_ERROR,
                "check",
                f"gateway returned {last_status} after 3 attempts",
            )
            if result is not None:
                return _shape_check_result(result)
        elif last_exc is not None:
            result = _handle_transport_error(
                on_transport_error,
                TransportErrorSource.NETWORK_ERROR,
                "check",
                f"{type(last_exc).__name__}: {last_exc}",
            )
            if result is not None:
                return _shape_check_result(result)
        # Legacy path: transport failure with mode="legacy" — fall
        # through to the historical fail-CLOSED block return so
        # callers that haven't opted into the new contract still
        # see the same shape they did before.
        return {
            "decision": "block",
            "reservation_id": None,
            "remaining_budget_cents": 0,
            "projected_cost_cents": 0,
            "explanations": ["Gateway unavailable (legacy mode)"],
            "suggestions": ["Check API availability"],
        }


def _shape_check_result(result: dict[str, Any]) -> dict[str, Any]:
    """
    Adapt a result dict from `execute()` semantics (`decision` /
    `decision_source` / `explanation` / `policy_version`) to the
    `check()` shape (`decision` / `reservation_id` /
    `remaining_budget_cents` / `projected_cost_cents` /
    `explanations` / `suggestions`).

    Used when `check()` is called with `on_transport_error="open"`
    or `"closed"` so the result still matches the calling gate's
    expectations.
    """
    explanation = result.get("explanation", "Gateway unavailable")
    return {
        "decision": result.get("decision", "block"),
        "reservation_id": None,
        "remaining_budget_cents": 0,
        "projected_cost_cents": 0,
        "explanations": [explanation],
        "suggestions": ["Check API availability"],
    }

    # =============================================================================
    # Evaluate — pre-validation / "what if" (no execution)
    # =============================================================================

    def evaluate(
        self,
        organization_id: str,
        execution_id: str | None,
        trace_id: str,
        tool: str,
        context: dict[str, Any] | None = None,
        on_transport_error: str = "raise",
    ) -> dict[str, Any]:
        """
        Dry-run / pre-validation against the gateway.

        POSTs to `/api/v1/evaluate` with the same envelope as
        `execute()` so the call goes through the SDK's own
        connection pool, HMAC headers, circuit breaker, and retry
        policy. The gateway returns a decision + matched-rule report
        without any side effect (no execution, no state change).

        The previous implementation in `runtime.evaluate()` reached
        into `self._client` directly, which silently bypassed the
        circuit breaker — a production hazard on a long-lived
        runtime. This method is the public surface for that call.

        Args:
            organization_id: Organization identifier.
            execution_id: Optional workflow id. May be None when
                the user has not opened a `with workflow(...)` block
                — the gateway tolerates null.
            trace_id: Trace id for cross-system correlation.
            tool: Tool name to evaluate.
            context: Optional per-tool context dict forwarded to
                the gateway as `context` (kept separate from the
                `input` field used by `execute()`).
            on_transport_error: Same contract as `execute()` /
                `check()`. Default is `"raise"`.

        Returns:
            Dict with the gateway's evaluate response (decision,
            decision_source, explanation, policy_version,
            matched_rules, scores, …). Shape is gateway-defined.

        Raises:
            NullRunTransportError: When the transport fails AND
                `on_transport_error="raise"`.
            NullRunAuthenticationError: On 401 (never silenced).
        """
        eval_request = {
            "organization_id": organization_id,
            "execution_id": execution_id,
            "trace_id": trace_id,
            "tool": tool,
            "context": context or {},
        }

        try:
            # Route through _signed_post so HMAC + W3C trace context
            # are applied automatically (Phase 1).
            response = self._signed_post(
                "/api/v1/evaluate",
                eval_request,
                timeout=5.0,
            )
        except NullRunAuthenticationError:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout, httpx.RequestError) as exc:
            result = _handle_transport_error(
                on_transport_error,
                TransportErrorSource.NETWORK_ERROR,
                "evaluate",
                str(exc),
            )
            if result is not None:
                return result
            # legacy: fall through to the historical block fallback
            return {
                "decision": "block",
                "decision_source": DecisionSource.FALLBACK,
                "explanation": "Evaluation endpoint unavailable (legacy mode)",
                "policy_version": 0,
                "matched_rules": [],
                "scores": {},
            }

        if response.status_code == 200:
            return response.json()  # type: ignore[no-any-return]
        if response.status_code == 401:
            raise NullRunAuthenticationError(
                f"Auth failed with status {response.status_code}. "
                f"API key may be invalid or expired."
            )
        if 500 <= response.status_code < 600:
            result = _handle_transport_error(
                on_transport_error,
                TransportErrorSource.GATEWAY_ERROR,
                "evaluate",
                f"gateway returned {response.status_code}",
            )
            if result is not None:
                # The result is shaped for `execute()`. Re-shape to
                # the evaluate dict (matched_rules / scores).
                return {
                    "decision": result.get("decision", "block"),
                    "decision_source": result.get(
                        "decision_source", DecisionSource.FALLBACK
                    ),
                    "explanation": result.get(
                        "explanation", "Evaluation endpoint unavailable"
                    ),
                    "policy_version": result.get("policy_version", 0),
                    "matched_rules": [],
                    "scores": {},
                }
        # 4xx (non-auth): treat as a real gateway decision (block).
        return {
            "decision": "block",
            "decision_source": DecisionSource.FALLBACK,
            "explanation": f"Evaluation endpoint returned {response.status_code}",
            "policy_version": 0,
            "matched_rules": [],
            "scores": {},
        }

    # =============================================================================
    # WebSocket Connection (Task 6 - WebSocket Push)
    # =============================================================================

    def clear_policy_cache(self) -> None:
        """Clear the policy cache, forcing next gate/execute to fetch fresh policy."""
        if hasattr(self, '_policy_cache'):
            self._policy_cache.clear()
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

        Uses the SDK's own httpx client (already pooled, mTLS-aware)
        so we don't add a `requests` dependency for a single call.
        """
        try:
            response = self._client.post(
                f"{self.api_url}/auth/verify",
                json={"api_key": self.api_key},
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
