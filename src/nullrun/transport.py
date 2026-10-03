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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import httpx

from nullrun.actions import handle_action
from nullrun.breaker.categories import is_gate_refusal, resolve_refusal_category
from nullrun.breaker.circuit_breaker import CircuitBreaker
from nullrun.breaker.exceptions import (
    BreakerTransportError,
    InsecureTransportError,
    NullRunApprovalDbUnavailableError,
    NullRunApprovalReplayRejectedError,
    NullRunAuthenticationError,
    NullRunBackendError,
    NullRunBlockedException,
    NullRunConfigError,
    NullRunDecision,
    NullRunExecutionNotFoundError,
    NullRunInfrastructureError,
    NullRunMcpApprovalRequiredError,
    NullRunMcpDestructiveBlockedError,
    NullRunMcpReadonlyBypassBlockedError,
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
    trace = None  # type: ignore[assignment, misc]
    TraceContextTextMapPropagator = None  # type: ignore[assignment, misc]

logger = logging.getLogger(__name__)

__api_version__ = "1.0"

# Wire-protocol version handshake. Backend rejects signed POSTs without
# `X-NULLRUN-PROTOCOL: <n>` with 400. Bump must be coordinated with backend
# `proxy::http::gate::protocol` and `/api/v1/capabilities`.
#
# the SDK-supplied `action_digest` and a `policy_hash` slot (always None
# today; Slice D wires per-request computation). Wire-additive: v3 SDKs
# parsing the response simply ignore the new fields; v4 SDKs parsing a
# v3 backend response see `None` on both (skip_serializing_if on the
# backend means the JSON keys are absent, not `null`). No new
# hashing/computation introduced on either side — both fields echo
# already-computed values.
NULLRUN_PROTOCOL_VERSION: int = 4
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
# Backend error classification
# =============================================================================

# Backend `error_code` values that name a DETERMINISTIC refusal: the request
# can never succeed as sent, no matter how long we wait or how often we resend
# it. Classifying on these by name — rather than by HTTP status — is what keeps
# a healthy backend from being treated as broken.
#
# EXECUTION_NOT_BOUND is the motivating case. The backend served it as 503
# (handlers.rs:10016-10017 before the status correction) while naming the code
# in the body, so by status alone it was indistinguishable from an outage. It
# is not one: the execution binding lives in Redis under a 24h TTL
# (EXECUTION_BINDING_TTL_SECONDS = 86_400) and only the server can mint it. Once
# it is gone, every retry of that event hits the same wall. Left on the transient
# path it would be resent until the attempt budget died AND, because it is a
# 5xx, each attempt would be counted as a transport failure — ten of them open
# the circuit breaker on a backend that is answering every other request
# perfectly. Same bug class as "any 4xx is permanent", one status code over.
#
# The backend now answers 422 for this code. That is necessary but not
# sufficient: deployed SDKs outlive the backend release that fixed it, so an
# instance pinned to an older build keeps talking to a newer server, and
# clients in the field are not upgraded in lockstep. Classifying on the code
# is what makes this correct against BOTH statuses — relying on the server
# having been upgraded first would turn a code-classification fix into a
# fleet-coordination problem and leave the laggard instances quietly
# mis-classifying.
#
# Every entry here must satisfy: (a) the backend names it explicitly, (b) no
# client-side action on the SAME request can change the answer, (c) retrying
# wastes an attempt without a chance of success. Anything that fails (b) or (c)
# belongs in the transient path even if it looks permanent from the outside.
_DETERMINISTIC_ERROR_CODES = frozenset(
    {
        "EXECUTION_NOT_BOUND",
        "CONSUME_OVERBUDGET",
    }
)

# Per-item reasons on a 200 response that retrying cannot fix.
#
# `/track/batch` answers 200 even when it refuses individual events, naming
# each one in `rejection_details`. Those reasons split three ways, and the
# wire tells us which: the backend sends `retry_after_ms` on a refusal it
# expects to be retried, and omits it on one it does not.
#
# The set below is the "omits it AND we have checked" list — reasons we have
# looked at and know are permanent:
#
#   reservation_not_found — the reservation the consume needed is gone.
#     Reservations are minted by /api/v1/gate and live under a TTL; nothing
#     the client can send re-mints one for a past event. The agent would have
#     to re-issue /gate, which produces a NEW execution and would double-count
#     against the budget. Same class as EXECUTION_NOT_BOUND one layer down.
#
# `budget_exceeded` is NOT here even though the spend has already happened,
# because the period-bound counter will not clear until the period rolls —
# retrying within the period is provably pointless — but the decision of
# whether such a row belongs in the ledger at all (spend as fact, enforcement
# separately) is open, and parking the event in the DLQ is the only response
# that preserves the fact without asserting one.
#
# The compatibility rule matters more than the list. Absence of
# `retry_after_ms` alone is NOT enough to call something terminal: an older
# backend omits the field entirely, so "no retry hint" would silently mean
# "drop" for every reason a pre-field backend can produce. Unknown reasons
# therefore take the bounded-retry path and land in the DLQ afterwards —
# a delay, never a silent drop. Adding a reason here is safe; forgetting one
# is not.
_TERMINAL_REJECTION_REASONS = frozenset(
    {
        "reservation_not_found",
    }
)

# DLQ row schema version. Bump when the record gains or changes a field.
#
# v1 (implicit — the field did not exist): `{"error": str, "event": {...}}`
# v2: adds `reason` (machine-readable, distinct from the v1 `error` string),
# `first_failed_at` (epoch seconds — an operator replaying a DLQ needs to
# know whether the refusal is still current or whether the world moved on),
# `attempts` (how many send cycles this event survived), and `event_type` so
# a DLQ can be triaged without opening every payload.
#
# Readers must accept v1 rows: a DLQ written before an upgrade stays on disk
# across it, and a reader that requires `v == 2` would make the whole file
# unreadable — turning a data-recovery file into data loss.
_DLQ_ROW_VERSION = 2

# ---------------------------------------------------------------------------
# Durability primitives: what this platform can actually promise.
#
# The WAL is the SDK's only copy of an event between `track()` and the
# backend accepting it. Three things protect it, and none of them is
# available everywhere:
#
#   0600 on every file  — the WAL holds full event payloads, which for an
#                         agent means prompts, completions and tool arguments.
#                         Default umask 022 leaves them world-readable in
#                         /tmp, on a shared volume, and in a container.
#   advisory flock     — a WAL is a SHARED path by default (one per tempdir,
#                         not one per process), so a second process appending
#                         the read-copy-append of the DLQ can interleave a
#                         line into the middle of another.
#   directory fsync    — `os.replace` is atomic, but without fsyncing the
#                         containing directory the new NAME can be absent
#                         after a power cut even though the data blocks
#                         landed.
#
# All three are probed, not imported. A platform lacking one is a supported
# configuration, so the probe result becomes observable state (metrics
# `wal_dir_fsync` / `wal_lock`) and a single warning, rather than an
# ImportError at module load or a silent downgrade nobody can see.
#
# The absence of `flock` is the sharp one. It means a multi-process
# deployment (gunicorn -w N) shares a WAL with no protection against
# interleaved appends, and the correct configuration is a per-process
# NULLRUN_WAL_PATH. The lock does not merge per-process buffers either way;
# re-delivery is absorbed by the backend's dedup on `event_id`. What the
# lock buys is that the FILES stay parseable.
# ---------------------------------------------------------------------------

try:  # POSIX only. Absence is a configuration, not an error.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    _fcntl = None

# Typed as `Any` deliberately. A bare `fcntl = None` makes the type of the
# name depend on which platform the type checker is running on, and mypy
# resolves `fcntl` to a stub without `flock` on win32 — so the same line is a
# clean type on Linux CI and five errors on a Windows dev box.
fcntl: Any = _fcntl
# DLQ size cap (default 64 MB). Override via NULLRUN_DLQ_MAX_BYTES.
_DLQ_MAX_BYTES_DEFAULT: int = 64 * 1024 * 1024

# Max refused events tracked in memory while the DLQ is full. Override via
# NULLRUN_DLQ_HOLDOVER_MAX_EVENTS. The rows are durable in `<wal>.holdover`
# either way; this bounds the index, not the data.
_DLQ_HOLDOVER_INDEX_MAX_DEFAULT: int = 10_000

# Longest server-stated `Retry-After` the SDK will sit through. Past this it
# stops retrying for the cycle rather than retrying early — see
# `_next_retry_delay` for why shortening the wait is the wrong cap.
# Override via NULLRUN_RETRY_AFTER_CEILING.
_RETRY_AFTER_CEILING_DEFAULT: float = 60.0

# How long a writer waits for the cross-process WAL lock before skipping its
# write. The critical section is a local file copy plus an fsync, so the
# uncontended cost is microseconds; the timeout exists for a descheduled
# holder, not for a busy one.
_WAL_LOCK_TIMEOUT_DEFAULT: float = 5.0

_DEGRADATION_WARNING = (
    "WAL durability degraded on this platform: %s. The events are still "
    "written and still replayed, but the guarantee they rest on is weaker. "
    "See metrics `wal_dir_fsync` / `wal_lock` for the live state."
)


class UnconfirmedBatchResponse(Exception):
    """A 2xx whose body did not say which events landed.

    Status said success; the body said nothing we can read — a proxy HTML
    page, an empty response, or a backend predating ``accepted_event_ids``.
    The request may well have been fully processed, and it may not have been
    delivered at all, and the difference decides whether the caller retries
    or stops. Guessing either way loses something, so the batch keeps its
    attempt budget and only reaches the DLQ once that budget is spent.
    """

    def __init__(self, event_count: int) -> None:
        self.event_count = event_count
        super().__init__(f"{event_count} events got a 2xx with no readable batch confirmation")


class UnknownRejectionReason(Exception):
    """The backend refused one event and named a reason we have no rule for.

    Not a reason to drop it. An SDK built before a reason existed would
    otherwise be the only fleet that discards those events, and the discard
    is invisible — the operator sees a shorter ledger and no error. So the
    event keeps its attempt budget and lands in the DLQ when it runs out,
    with the reason preserved verbatim for whoever triages it.
    """

    def __init__(self, reason: str, event_id: str | None) -> None:
        self.reason = reason
        self.event_id = event_id
        super().__init__(f"unhandled rejection reason {reason!r} for event {event_id}")


# How a caller asks a transport failure to be surfaced (ADR-008). A callable
# receives the exception and returns a decision dict; the three strings select
# a built-in arm. ``None`` means "use ``fallback_mode``", which is fail-CLOSED
# under ``mode="strict"``. Named because the union appears at several call
# sites and the narrower callback-only annotation that used to stand here
# made the documented ``"raise"`` arm un-passable under a type checker.
TransportErrorHandler = str | Callable[[Exception], dict[str, Any]]


class DeterministicBackendRefusal(Exception):
    """The backend named a refusal that retrying cannot fix.

    Carries the wire ``error_code`` and status separately because the status is
    not the signal — several of these arrive as 5xx, which is exactly why
    status-based classification mis-routes them. Handlers of this exception
    must NOT count it as a transport failure: the transport layer is healthy,
    the answer is a decision.
    """

    def __init__(self, error_code: str, status_code: int | None, message: str = "") -> None:
        self.error_code = error_code
        self.status_code = status_code
        self.detail = message
        super().__init__(f"{error_code} (HTTP {status_code}): {message}" if message else error_code)


def _retry_after_seconds_from(response: Any) -> float | None:
    """``Retry-After`` in seconds, from either wire form. ``None`` if absent/unparseable.

    Module-level because the retry loop needs it and has no Transport: it
    reads the header off the ``HTTPStatusError`` the response produced. Both
    RFC 7231 forms are accepted — delta-seconds and an HTTP-date — because
    nginx and a hand-rolled rate limiter in front of the gate will each emit
    a different one.

    An HTTP-date already in the past yields a NEGATIVE number. That is
    returned rather than clamped, so the caller can tell "the server said
    wait zero" from "the server said wait, and that moment has passed" — the
    retry loop treats non-positive as "no floor stated" and falls back to
    exponential backoff, which is the safe reading of a stale date.
    """
    if response is None:
        return None
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        retry_after = headers.get("Retry-After")
    except Exception:
        return None
    if not retry_after:
        return None

    try:
        return float(retry_after)
    except (TypeError, ValueError):
        pass

    try:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(retry_after)
        return (dt - datetime.now(timezone.utc)).total_seconds()
    except Exception:
        return None


def _extract_backend_error_code(response: Any) -> str | None:
    """Pull ``error_code`` out of a backend error envelope, if there is one.

    The envelope is ``{"error_code", "error_message", "details", "retry_after_ms"}``
    (see the TrackError::WithBody sites in handlers.rs). Returns ``None`` for a
    body that is absent, unparseable, or a plain proxy HTML page — a missing
    code is NOT evidence of a deterministic refusal, so the caller keeps the
    request on the transient path.
    """
    if response is None:
        return None
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 — non-JSON body (proxy error page, empty)
        return None
    if not isinstance(payload, dict):
        return None
    code = payload.get("error_code")
    if isinstance(code, str) and code:
        return code
    return None


def _extract_error_message(response: Any) -> str:
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(payload, dict):
        message = payload.get("error_message")
        if isinstance(message, str):
            return message
    return ""


# =============================================================================
# Retry with exponential backoff + jitter
# =============================================================================


def _next_retry_delay(
    *,
    attempt: int,
    max_retries: int,
    base_delay: float,
    backoff_factor: float,
    jitter: float,
    max_delay: float,
    server_wait: float | None,
    error: Exception,
    retry_after_ceiling: float = _RETRY_AFTER_CEILING_DEFAULT,
) -> float | None:
    """How long to wait before the next attempt — or ``None`` to not retry.

    Two regimes, and the choice between them is the whole function.

    With a server-stated ``server_wait`` the value is a FLOOR, so jitter is
    one-sided — the spread is ``[wait, wait * (1 + jitter)]``. Spreading
    downward would retry before the rate limit permitted it, which is how a
    429 becomes a sustained 429; and spreading symmetrically is what makes a
    fleet that got limited together retry together.

    Without one there is no floor, so jitter is symmetric around the
    exponential curve, which is the standard defence against a thundering
    herd on a shared failure.

    Returns ``None`` when the server's floor is beyond ``retry_after_ceiling``.
    This is the part that used to be wrong, and it was wrong in the
    direction that looks like a safety feature.

    The previous code clamped with ``min(server_wait, max_delay)``. That
    bounds the wait, so ``Retry-After: 86400`` cannot hang a flush — and it
    also retries 60 minutes early, which is precisely what the floor
    semantics forbid. A server that says "not for an hour" and gets a client
    back in 30 seconds gets the same 429, spends its whole retry budget
    re-tripping the limit it was told to respect, and the operator sees a
    client that ignores rate limits rather than one that honours a long one.

    The fix is not a shorter wait, it is no wait. The events are already
    durable — ``.inflight`` is retained and the WAL is the recovery path —
    so declining to retry in this cycle costs nothing and loses nothing. The
    flush ends, and the next cycle tries again. So the ceiling produces an
    honest "not now" instead of a dishonest "yes, sooner".
    """
    if server_wait is not None and server_wait > 0:
        if server_wait > retry_after_ceiling:
            metrics.inc_transport("retry_after_deferred")
            logger.warning(
                "Request failed (attempt %d/%d), Retry-After %.2fs exceeds the "
                "%.2fs ceiling — not retrying in this cycle rather than "
                "retrying before the server permits it. The events stay in the "
                "WAL; the next flush cycle tries again. Raise "
                "NULLRUN_RETRY_AFTER_CEILING to wait longer: %s",
                attempt + 1,
                max_retries + 1,
                server_wait,
                retry_after_ceiling,
                type(error).__name__,
            )
            return None
        base = server_wait
        delay = base * (1.0 + random.uniform(0.0, jitter))  # noqa: S311
        logger.warning(
            "Request failed (attempt %d/%d), honoring Retry-After %.2fs "
            "(+jitter -> %.2fs): %s",
            attempt + 1,
            max_retries + 1,
            base,
            delay,
            type(error).__name__,
        )
        return delay

    backoff = min(base_delay * (backoff_factor**attempt), max_delay)
    spread = backoff * jitter
    delay = max(0.0, backoff + random.uniform(-spread, spread))  # noqa: S311
    logger.warning(
        "Request failed (attempt %d/%d), retrying in %.2fs: %s",
        attempt + 1,
        max_retries + 1,
        delay,
        type(error).__name__,
    )
    return delay


def _retry_after_ceiling() -> float:
    """Effective ``Retry-After`` ceiling. Non-positive env values are ignored."""
    raw = os.environ.get("NULLRUN_RETRY_AFTER_CEILING", "").strip()
    if not raw:
        return _RETRY_AFTER_CEILING_DEFAULT
    try:
        value = float(raw)
        return value if value > 0 else _RETRY_AFTER_CEILING_DEFAULT
    except ValueError:
        return _RETRY_AFTER_CEILING_DEFAULT


def _wait_on(cancel: threading.Event, seconds: float) -> bool:
    """Wait on ``cancel`` for up to ``seconds``. The clock seam for tests.

    A module-level function, not an inline ``cancel.wait``, so the test
    fixture can stub the CLOCK without stubbing the decision. The
    alternative — patching ``threading.Event.wait`` globally — changes the
    semantics of every other ``Event.wait`` in the suite, including the
    approval-wait tests that legitimately assert on real elapsed time, and
    turns them into load-sensitive flakes to make the retry path fast.
    Stubbing the seam keeps the full retry path — the ceiling check, the
    one-sided jitter, the shutdown branch — under test, and only removes
    the wall clock from it.
    """
    return not cancel.wait(seconds)


def _interruptible_sleep(seconds: float, cancel: threading.Event | None) -> bool:
    """Sleep, but wake early when the SDK is shutting down.

    Returns True if the full delay elapsed, False if it was cut short by
    ``cancel``.

    ``time.sleep`` is the wrong primitive on the flush path for one reason:
    it cannot be woken. ``Transport.stop()`` sets an event that the flush
    loop honours between cycles, but a retry sleeping on ``Retry-After:
    45`` inside a cycle does not check that event, so a shutdown that arrives
    during a rate-limit wait blocks for the remainder of the wait. For a
    process that is exiting, that is the difference between a prompt stop
    and one that appears hung — and it is worst exactly when the backend is
    misbehaving, which is when someone is most likely to be killing it.
    """
    if cancel is None:
        time.sleep(seconds)
        return True
    return _wait_on(cancel, seconds)


def _retry_with_backoff(
    func: Callable[[], Any],
    max_retries: int = 10,
    base_delay: float = 0.5,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
    jitter: float = 0.1,
    last_retry_after_seconds: float = 0.0,
    on_transport_error: TransportErrorHandler | None = None,
    retry_on_5xx: bool = False,
    cancel: threading.Event | None = None,
    retry_after_ceiling: float | None = None,
) -> Any:
    """Retry with exponential backoff + jitter; honors Retry-After (429) header.

    Formula (without Retry-After): delay = min(base_delay * backoff_factor^attempt, max_delay)
                                    delay += random.uniform(-jitter * delay, jitter * delay)
    Formula (with Retry-After): actual_delay = min(last_retry_after_seconds, max_delay)
                                    delay  *= (1 + random.uniform(0, jitter))
    The Retry-After jitter is one-sided on purpose: a rate limit states a
    FLOOR, so spreading downward would retry before the server permitted it.
    Read off the response the exception carries; see
    ``_retry_after_seconds_from``.

    NR-006: when ``retry_on_5xx=True`` a 5xx response is treated as
    transient infrastructure failure and retried via the same
    backoff path as network errors. After the retry budget is
    exhausted the LAST 5xx response is returned (not raised) so
    the caller can produce a deterministic fail-CLOSED fallback —
    the audit's "fail-NO-CHECK" violation happens when a 5xx
    short-circuits to a synthetic block without any retry. Default
    ``retry_on_5xx=False`` preserves the /track and /execute
    semantics where 5xx is a classified GATEWAY_ERROR that raises
    immediately.
    """
    # Eager imports for the exception classes that the ``except``
    # branch below references. Lazy imports inside the ``try`` body
    # shadow the name in this scope (Python treats any assignment
    # to the name as a local binding), which raises
    # ``UnboundLocalError`` when the except branch tries to
    # pattern-match before the lazy import has fired.
    from nullrun.breaker.exceptions import (
        NullRunAuthError,
        NullRunBackendError,
    )

    if retry_after_ceiling is None:
        retry_after_ceiling = _retry_after_ceiling()

    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            result = func()

            if hasattr(result, "status_code"):
                if result.status_code == 401:
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
                if result.status_code >= 500 and retry_on_5xx and attempt < max_retries:
                    # Convert to HTTPStatusError so the except branch catches
                    # it as a retryable condition. After retry exhaustion
                    # the helper returns the last response (see below).
                    result.raise_for_status()
                elif result.status_code >= 500 and not retry_on_5xx:
                    # raises HTTPStatusError so the caller (e.g.
                    # ``Transport.execute``) can run its fallback logic
                    # after retry exhaustion produces BreakerTransportError.
                    # ``retry_on_5xx=True`` (the /gate path) takes the
                    # branch above instead and returns the last response.
                    result.raise_for_status()
                # 4xx is a real gate decision — return the response so
                # the caller can synthesize the appropriate fallback
                # (Transport.check returns a synthetic block; Transport.execute
                # returns a synthetic block; /track batch inspects status
                # directly). Calling ``raise_for_status()`` here would force
                # every caller into the except path and retry a permanent
                # contract.

            return result

        except (
            BreakerTransportError,
            NullRunAuthenticationError,
            NullRunTransportError,
            NullRunBackendError,
            # A refusal the backend NAMED as deterministic. Retrying it is
            # guaranteed waste, and — since several of these arrive as 5xx —
            # letting it fall through the generic handler would also count it
            # as infrastructure failure on a perfectly healthy backend.
            DeterministicBackendRefusal,
        ):
            raise

        except httpx.HTTPStatusError as exc:
            # 5xx HTTPStatusError from the retry_on_5xx branch above, and
            # also the 429 that `_post_batch` turns into one. Treat as
            # retryable transient infra failure.
            #
            # This branch used to set `last_exc` and fall straight through
            # to the next attempt with NO delay: no backoff, no sleep, and
            # the `Retry-After` header the server had just sent ignored
            # entirely. A rate-limited client therefore spent its whole
            # retry budget in milliseconds, hammering the limit that had
            # just answered it, and then gave up — the opposite of what a
            # 429 asks for. The suite stayed green because the existing
            # 429 test asserted the CALL COUNT, which does not depend on
            # how long you wait between calls.
            last_exc = exc
            if attempt >= max_retries:
                break
            metrics.inc_transport("retries_total")
            actual_delay = _next_retry_delay(
                attempt=attempt,
                max_retries=max_retries,
                base_delay=base_delay,
                backoff_factor=backoff_factor,
                jitter=jitter,
                max_delay=max_delay,
                server_wait=_retry_after_seconds_from(exc.response),
                error=exc,
                retry_after_ceiling=retry_after_ceiling,
            )
            if actual_delay is None:
                # The server's floor is past our ceiling. Stop this cycle
                # rather than retry early; the caller keeps the events.
                break

        except Exception as exc:
            last_exc = exc
            metrics.set_transport("last_error", f"{type(exc).__name__}: {exc}")
            if isinstance(exc, (httpx.TimeoutException, httpx.ConnectTimeout, httpx.ReadTimeout)):
                metrics.inc_transport("timeouts")

            if attempt >= max_retries:
                break

            metrics.inc_transport("retries_total")

            actual_delay = _next_retry_delay(
                attempt=attempt,
                max_retries=max_retries,
                base_delay=base_delay,
                backoff_factor=backoff_factor,
                jitter=jitter,
                max_delay=max_delay,
                server_wait=last_retry_after_seconds,
                error=exc,
                retry_after_ceiling=retry_after_ceiling,
            )
            last_retry_after_seconds = 0.0
            if actual_delay is None:
                break

        # Every path that reaches here has decided to retry. One sleep, one
        # place: the two except branches used to carry their own, and the
        # status branch had none, which is how the two drifted apart.
        if not _interruptible_sleep(actual_delay, cancel):
            # Shutdown arrived mid-wait. Stop retrying now; the caller still
            # holds the events, and `.inflight` is retained because the send
            # did not complete.
            logger.info("Retry wait interrupted by shutdown; stopping this cycle")
            metrics.inc_transport("retries_interrupted_by_shutdown")
            break

    # ``retry_on_5xx`` and the failure mode was 5xx, return the
    # last response so the caller can synthesize a fallback
    # (e.g. ``Transport.check`` returns the legacy synthetic-block
    # shape). Other exhaustion paths (network errors, timeouts)
    # still raise ``BreakerTransportError`` — pre-existing
    # behaviour, unchanged.
    if (
        retry_on_5xx
        and last_exc is not None
        and isinstance(last_exc, httpx.HTTPStatusError)
        and last_exc.response is not None
    ):
        return last_exc.response
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


def is_fallback_decision_source(source: object) -> bool:
    """True when ``source`` marks a SYNTHETIC decision, not a real one.

    A decision is synthetic when the transport degraded instead of
    reaching the gateway. ADR-008's fail-OPEN/CLOSED rules then apply;
    a real ``gateway`` decision is always honoured regardless.

    Two shapes exist, both produced by this module:

    * ``DecisionSource.FALLBACK`` (``"fallback"``) — the generic
      degradation, e.g. ``fallback_mode=STRICT`` or a gateway response
      that could not be parsed into a decision.
    * a ``TransportErrorSource`` member (``"NETWORK_ERROR"``,
      ``"GATEWAY_ERROR"``, ``"BREAKER_OPEN"``) — returned only when the
      caller passed ``on_transport_error="open"`` or ``"closed"``.
      ``TransportErrorSource`` is a ``str`` Enum, so these compare
      equal to their uppercase string form.

    ``AUTH_ERROR`` is deliberately NOT a fallback. It is a credential
    failure, not an unreachable gate: the transport re-raises
    ``NullRunAuthenticationError`` rather than degrading, and treating
    it as a transport error would let a bad API key read as "engine
    unavailable, carry on". DEF-MP-TS12-ENF-01.

    This function exists because the predicate was previously written
    out twice — in ``runtime.check_workflow_budget`` and in
    ``decorators._run_tool_policy_gate`` — and the copies had already
    drifted: one matched lowercase ``"fallback"`` and the other
    uppercase ``"FALLBACK_"`` (a prefix no code path produces, so that
    copy's first clause could never fire), and only one had
    ``AUTH_ERROR`` removed. Two hand-maintained copies of a
    security-relevant predicate is one copy too many.
    """
    if not isinstance(source, str):
        return False
    return source == DecisionSource.FALLBACK or source in {
        TransportErrorSource.NETWORK_ERROR.value,
        TransportErrorSource.GATEWAY_ERROR.value,
        TransportErrorSource.BREAKER_OPEN.value,
    }


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

    # Fallback mode when Gateway is unavailable. Default is STRICT
    # (fail-CLOSED on enforcement) per CLAUDE.md §4.
    fallback_mode: FallbackMode = FallbackMode.STRICT
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
        # Per-batch re-queue budget. A failure we cannot classify as transient
        # (a payload that will not serialize, a shape the signer rejects) repeats
        # identically on every cycle and would pin the batch ahead of the whole
        # buffer forever. Count attempts, and dead-letter once spent.
        self._batch_attempts: dict[str, int] = {}  # batch signature -> attempts
        # event_id -> epoch seconds of its first observed failure, so a DLQ
        # record says how long the event was failing rather than only that it
        # was. In-memory only: after a restart the clock restarts, and the
        # durable history is the DLQ file's own row order (each re-park
        # appends). That is a deliberate trade — reading the whole DLQ to
        # recover a timestamp would make the write path O(file), and the
        # value is diagnostic, not load-bearing for recovery.
        self._first_failed_at: dict[str, float] = {}
        # Durability state. Probed on first use against the real filesystem,
        # reported in metrics, warned about once. See the "Durability
        # primitives" block above for why each of these is optional-but-named.
        self._degradation_warned: set[str] = set()
        self._dir_fsync_ok: bool | None = None
        self._max_batch_attempts = int(os.environ.get("NULLRUN_MAX_BATCH_ATTEMPTS", "10"))
        self._bisect_depth = 0  # recursion guard for 400/422 batch splitting
        # Request budget for one bisect cascade. The depth guard alone bounds
        # the RECURSION, not the wire: halving to depth 24 is 2^24 sends. A
        # batch that is genuinely all-bad therefore spends an unbounded amount
        # of the operator's rate limit rediscovering that fact. This counts
        # sends instead, so the cost is a number a human chose.
        self._bisect_budget = int(os.environ.get("NULLRUN_MAX_BISECT_REQUESTS", "64"))
        # Terminal refusals the DLQ could not take, held in memory until it
        # has room. See `_hold_for_dlq` for why they are not re-sent.
        self._dlq_overflow: list[dict[str, Any]] = []
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

        # Tighten anything a previous run left behind, BEFORE the replay
        # path reads it. A file created by an older SDK, or by a run under a
        # different umask, keeps its old mode through every `os.replace`,
        # so hardening only new writes would leave the existing events — the
        # ones most likely to hold full prompts and tool arguments — readable
        # by everyone with access to the volume.
        self._harden_wal_permissions()

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

    def _wal_inflight_path(self) -> str:
        """Path of the in-flight batch file (the batch currently on the wire)."""
        return f"{self._wal_path()}.inflight"

    def _wal_dlq_path(self) -> str:
        """Path of the dead-letter file for permanently-rejected batches."""
        return f"{self._wal_path()}.dlq"

    def _wal_holdover_path(self) -> str:
        """Path of the holdover file: refused events the DLQ had no room for.

        Deliberately NOT `.wal` and NOT `.wal.inflight`. `.wal` is rewritten
        wholesale by `_persist_to_wal` on every buffer persist, so held rows
        appended there are erased by the next unsent event; `.inflight` is
        overwritten with whatever batch is about to go on the wire, which is
        precisely the loss this file exists to prevent. A separate file has
        one writer (the hold) and one eraser (the drain), and nothing else
        touches it.
        """
        return f"{self._wal_path()}.holdover"

    def _wal_lock_path(self) -> str:
        """Path of the advisory lock guarding mutations of the WAL files."""
        return f"{self._wal_path()}.lock"

    @property
    def _dlq_max_bytes(self) -> int:
        """Effective DLQ size cap. Same shape as ``_wal_max_bytes``."""
        raw = os.environ.get("NULLRUN_DLQ_MAX_BYTES", "").strip()
        if not raw:
            return _DLQ_MAX_BYTES_DEFAULT
        try:
            value = int(raw)
            return value if value > 0 else _DLQ_MAX_BYTES_DEFAULT
        except ValueError:
            return _DLQ_MAX_BYTES_DEFAULT

    @property
    def _holdover_index_max(self) -> int:
        """Max tracked holdover entries. Bounded on COUNT, not bytes.

        Count is the unit the metric reports and the unit an operator alerts
        on; a byte bound here would be a second cap for the same property and
        would fail at a threshold nobody chose deliberately. The cap trims
        the in-memory index only — the holdover file keeps every row, so a
        saturated index under-reports `dlq_holdover` rather than losing an
        event.
        """
        raw = os.environ.get("NULLRUN_DLQ_HOLDOVER_MAX_EVENTS", "").strip()
        if not raw:
            return _DLQ_HOLDOVER_INDEX_MAX_DEFAULT
        try:
            value = int(raw)
            return value if value > 0 else _DLQ_HOLDOVER_INDEX_MAX_DEFAULT
        except ValueError:
            return _DLQ_HOLDOVER_INDEX_MAX_DEFAULT

    # -- durability probes ------------------------------------------------

    def _warn_degraded_once(self, aspect: str, state: str, detail: str) -> None:
        """Say it once per aspect per Transport, and record the state.

        The metric gets a short enum value and the log gets the sentence.
        A metric field an operator has to substring-match to alert on is a
        metric field nobody alerts on.

        Once, because a degraded platform is a standing condition, not an
        event: warning on every flush would bury the log under a fact that
        never changes and never resolves itself.
        """
        metrics.set_transport(f"wal_{aspect}", state)
        if aspect in self._degradation_warned:
            return
        self._degradation_warned.add(aspect)
        logger.warning(_DEGRADATION_WARNING % detail)

    def _dir_fsync_supported(self, directory: str) -> bool:
        """Whether this platform and filesystem can fsync a DIRECTORY.

        Probed against the real directory rather than inferred from the OS:
        Windows has no directory fsync at all, and on Linux some mounts
        (overlayfs upper dirs, certain network and FUSE filesystems) accept
        the open and then refuse the fsync. Both are indistinguishable from
        the outside and both mean the same thing, so both report False.
        """
        if self._dir_fsync_ok is not None:
            return self._dir_fsync_ok
        ok = True
        fd = -1
        try:
            fd = os.open(directory, os.O_RDONLY)
            os.fsync(fd)
        except OSError:
            ok = False
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._dir_fsync_ok = ok
        if ok:
            metrics.set_transport("wal_dir_fsync", "enabled")
        else:
            self._warn_degraded_once(
                "dir_fsync", "unavailable", f"directory fsync unavailable on {directory}"
            )
        return ok

    @property
    def _wal_lock_timeout(self) -> float:
        """Seconds to wait for the cross-process WAL lock before giving up."""
        raw = os.environ.get("NULLRUN_WAL_LOCK_TIMEOUT_MS", "").strip()
        if not raw:
            return _WAL_LOCK_TIMEOUT_DEFAULT
        try:
            value = float(raw) / 1000.0
            return value if value > 0 else _WAL_LOCK_TIMEOUT_DEFAULT
        except ValueError:
            return _WAL_LOCK_TIMEOUT_DEFAULT

    @contextmanager
    def _wal_file_lock(self) -> Iterator[bool]:
        """Hold the cross-process WAL lock. Yields False if it could not be had.

        A NEW descriptor is opened per acquisition, and that is load-bearing,
        not incidental. `flock` locks the open file DESCRIPTION, so two
        threads of one process each opening their own descriptor DO conflict —
        which is why this needs no companion `threading.Lock` and why adding
        one would be redundant. Caching the descriptor to save an `open()`
        would invert this: the kernel would see one description, and the
        second `LOCK_EX` would succeed immediately against the first holder's
        own lock. Cross-process safety survives that; in-process safety does
        not. `test_flock_excludes_two_threads_of_one_process` is the tripwire.

        Advisory (`flock`), exclusive, and **bounded-wait, not
        non-blocking**. Non-blocking is the obvious choice and it is wrong:
        it was tried here, and on a 4-worker deployment the losing writer was
        refused on every single attempt, forever, because the winner was
        always mid-append. Its events were never lost — the caller re-queues
        them — and never landed either, so a DLQ nobody could ever write to
        while the other process was busy. A wait costs the losing writer
        microseconds, because the critical section is a local file copy and
        fsync; starvation is not a cheaper option than waiting.

        Bounded, because "wait forever" turns a descheduled writer into a
        hung flush thread. On expiry the caller skips its write and keeps the
        events — the same fail-safe as any other reason the write did not
        happen. The lock is released by the kernel if a holder dies, so a
        crashed process cannot hold it.

        The scope is the FILE MUTATION only — never the network send.
        Holding it across a request would serialise every worker behind one
        another's latency, which is the opposite of what it is for.
        """
        if fcntl is None:
            # No flock here (Windows). The files still get 0600 and the
            # platform's own sharing semantics; what is missing is the guard
            # against two processes appending at once, which is a real
            # multi-process hazard and is why the warning says so.
            self._warn_degraded_once(
                "lock",
                "unavailable",
                "no fcntl.flock — one writer per WAL path, set NULLRUN_WAL_PATH per worker",
            )
            yield True
            return

        lock_path = self._wal_lock_path()
        directory = os.path.dirname(lock_path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as e:
            logger.warning(f"Cannot create WAL directory {directory}: {e}")
            yield False
            return

        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as e:
            # A lock we cannot open is a lock we do not have. Say so once
            # and proceed unlocked rather than refusing to write at all —
            # the alternative is losing every event on a permissions
            # problem in a directory the WAL itself already lives in.
            self._warn_degraded_once(
                "lock", "unavailable", f"cannot open {lock_path}: {e}"
            )
            yield True
            return

        acquired = False
        deadline_step = 0.02
        # Measured, not counted. A budget expressed as "50 naps of 20ms" is
        # a budget of 50 *naps*, and anything that returns from sleep early —
        # a signal, a test harness that stubs sleep, a coarse scheduler —
        # spends the whole 5s in milliseconds and gives up while the lock is
        # still held. The timeout is a duration, so it is compared against a
        # clock.
        start = time.monotonic()
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() - start >= self._wal_lock_timeout:
                        break
                    time.sleep(deadline_step)
            waited = time.monotonic() - start
            if not acquired:
                metrics.set_transport("wal_lock", "contended")
                metrics.inc_transport("wal_lock_timeouts_total")
                logger.warning(
                    f"WAL lock {lock_path} still held after {waited:.2f}s; "
                    f"skipping this write. The events stay on the retry path."
                )
                yield False
                return
            if waited > deadline_step:
                metrics.set_transport("wal_lock", "contended")
                logger.debug(
                    f"WAL lock {lock_path} waited {waited:.2f}s before being granted"
                )
            metrics.set_transport("wal_lock", "enabled")
            yield True
        finally:
            if acquired:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                os.close(fd)
            except OSError:
                pass

    def _harden_wal_permissions(self) -> None:
        """Force 0600 on every WAL file, including ones a previous run left.

        A file written by an older SDK, or by a run under a different umask,
        keeps its old mode: `os.replace` carries the mode of the file it
        renamed, so tightening only the tmp file tightens new writes and
        leaves every existing file as it was. Events are payloads, so this is
        a data-exposure fix, not a tidiness one.
        """
        base = self._wal_path()
        for candidate in (
            base,
            f"{base}.1",
            f"{base}.inflight",
            f"{base}.holdover",
            f"{base}.dlq",
            f"{base}.lock",
        ):
            try:
                os.chmod(candidate, 0o600)
            except FileNotFoundError:
                continue
            except OSError as e:
                # Best-effort by design. Refusing to run because a mode
                # could not be tightened would trade a permissions warning
                # for total data loss.
                logger.warning(f"Could not restrict permissions on {candidate}: {e}")

    def _dlq_size(self) -> int:
        try:
            return os.path.getsize(self._wal_dlq_path())
        except OSError:
            return 0

    def _dlq_over_cap(self, incoming_bytes: int) -> bool:
        """True when appending would take the DLQ past its cap.

        The cap NEVER deletes anything. The alternative — rotating or
        trimming the oldest rows — destroys the only copy of an event the SDK
        could not deliver, silently, with no trace beyond a short file. A
        DLQ that has stopped growing is a visible, alertable condition; a
        DLQ that quietly dropped the oldest refusals is neither. So the cap
        stalls the write and the caller re-queues the event on the retry path
        instead, where a further crash still holds it and the next attempt
        reports the same refusal.

        `incoming_bytes` is part of the test, not an optimisation: a cap
        checked only against the current size admits any single write, so an
        operator who set 1 GB would get a 40 GB file the first time a large
        batch was quarantined.
        """
        size = self._dlq_size()
        metrics.set_transport("dlq_bytes", size)
        if size < self._dlq_max_bytes and size + incoming_bytes <= self._dlq_max_bytes:
            return False
        metrics.inc_transport("dlq_overflow_total")
        metrics.set_transport("dlq_overflow_reason", "size_cap")
        logger.error(
            f"DLQ {self._wal_dlq_path()} would exceed its cap "
            f"({size} bytes + {incoming_bytes} incoming > {self._dlq_max_bytes}). "
            f"Refusing to append: the cap never deletes rows, so the event "
            f"stays on the retry path. Raise NULLRUN_DLQ_MAX_BYTES, or triage "
            f"and archive the file with `nullrun-wal`."
        )
        return True

    def _write_events_atomic(
        self, path: str, events: list[dict[str, Any]], mode: str = "w"
    ) -> bool:
        """Write ``events`` as JSON lines to ``path`` atomically.

        tmp + fsync + os.replace: a reader either sees the previous file or
        the complete new one, never a half-written batch. ``mode="a"`` copies
        the existing file first and appends, so successive writes accumulate
        instead of clobbering — that is what the DLQ needs, where losing an
        earlier quarantine to a later one would itself be data loss.

        Two crash hazards the copy-and-append path has to survive:

        * A **torn tail**. If the process died mid-write, the last line of the
          existing file has no terminating ``\\n``. Copying it verbatim glues
          the next row onto the truncated JSON, producing one corrupt line
          that takes a valid event with it. The unterminated tail is dropped
          (it was never durable) before appending.
        * A **stale tmp**. The name is pid-scoped and opened with ``"w"``, so a
          leftover from a dead process is truncated rather than appended to.

        The directory is fsynced after the rename: without it the new name can
        be absent after a power cut even though the data blocks landed.

        The tmp file is created 0600 rather than through ``open(..., "w")``,
        because the mode of the file that ``os.replace`` renames is the mode
        the WAL ends up with, and ``open`` applies the process umask to
        whatever 0666 it asks for. On a default umask that leaves every event
        payload world-readable in the tempdir, on a shared volume, and inside
        a container that several services mount.

        The whole read-copy-append runs under the cross-process WAL lock:
        it is a read followed by a write of the same file, and a second
        process doing the same thing concurrently interleaves its lines into
        the middle of the other's, which no reader can then parse.

        Returns True when the data is durably on disk.
        """
        wal_dir = os.path.dirname(path) or "."
        try:
            os.makedirs(wal_dir, exist_ok=True)
        except OSError as e:
            logger.warning(f"Cannot create WAL directory {wal_dir}: {e}")
            return False
        tmp_path = f"{path}.tmp.{os.getpid()}"
        with self._wal_file_lock() as acquired:
            if not acquired:
                # Another process owns these files. Our events are still in
                # memory / still covered by `.inflight`, and the caller is
                # told "not written" so it keeps the recovery file.
                return False
            try:
                fd = os.open(
                    tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
                )
                with os.fdopen(fd, "w") as f:
                    if mode == "a" and os.path.exists(path):
                        # Existing contents FIRST, so the file stays in
                        # chronological order. Writing the new rows first and
                        # folding the old ones in after would reverse the DLQ.
                        #
                        # A crash can leave the last line unterminated. Copying it
                        # verbatim would glue the next row onto truncated JSON and
                        # corrupt an event that was perfectly valid, so the torn
                        # tail is dropped — it was never durable anyway.
                        with open(path) as prev:
                            previous = prev.read()
                        if previous and not previous.endswith("\n"):
                            keep = previous.rfind("\n") + 1  # 0 when there is none
                            logger.warning(
                                f"Dropping {len(previous) - keep}B torn tail of {path} before append"
                            )
                            previous = previous[:keep]
                        f.write(previous)
                    for event in events:
                        f.write(json.dumps(event, default=str) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, path)
                self._fsync_dir(wal_dir)
                return True
            except OSError as e:
                logger.warning(
                    f"Failed to persist {len(events)} events to {path}: {e}"
                )
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                return False

    def _fsync_dir(self, path: str) -> None:
        """Flush a directory entry so a rename survives a power cut.

        Best-effort: a failure here costs durability of the NAME, not the
        data, which is already fsynced. What it must not cost is
        *visibility* — a platform that cannot do this is probed once by
        ``_dir_fsync_supported``, reported in metrics, and warned about, so
        "we lose the rename on power cut here" is a known state rather than
        an assumption nobody checked.
        """
        if not self._dir_fsync_supported(path):
            return
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _persist_to_wal(self) -> bool:
        """Persist unflushed events to WAL file for replay on restart.

        Every event is guaranteed an ``event_id`` before it reaches disk.
        Replay is at-least-once — a crash mid-send can re-deliver an event —
        and the backend dedups on ``event_id`` (cost_event_id_dedup PRIMARY
        KEY), so a stable id is exactly what makes re-delivery safe.

        Returns True when the buffer is durably on disk (and therefore
        cleared), False when the write failed and the caller must NOT
        discard any recovery file.
        """
        if not self._buffer:
            return True
        for event in self._buffer:
            if not event.get("event_id"):
                event["event_id"] = str(uuid.uuid4())
        event_count = len(self._buffer)
        wal_path = self._wal_path()
        self._rotate_wal_if_needed()
        if not self._write_events_atomic(wal_path, list(self._buffer)):
            return False
        self._buffer.clear()
        logger.debug(f"Persisted {event_count} events to WAL at {wal_path}")
        return True

    def _persist_inflight(self, batch: list[dict[str, Any]]) -> None:
        """Record the batch that is about to go on the wire.

        The batch has already been removed from ``_buffer`` at this point, so
        a crash during the send would otherwise lose it outright. Cleared once
        the send is accepted; deliberately RETAINED when the send fails,
        because then the batch is only in memory. A later successful flush
        overwrites this file with its own batch and removes it, so a stale
        copy self-heals rather than accumulating.
        """
        if batch:
            self._write_events_atomic(self._wal_inflight_path(), batch)

    def _clear_inflight(self) -> None:
        try:
            os.remove(self._wal_inflight_path())
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning(f"Failed to remove in-flight WAL: {e}")

    def _replay_from_wal(self) -> None:
        """Recover events from every durable WAL source and flush them.

        Sources, oldest first: the rotated ``.wal.1``, the active ``.wal``,
        and ``.wal.inflight`` — the batch that was on the wire when the
        process died.

        Crash-safety invariant: no recovery file is unlinked before its
        events are provably safe, i.e. either accepted by the backend or
        rewritten into ``.wal`` by ``_persist_to_wal``. Recovery is
        at-least-once; the backend dedups on ``event_id``.

        The read phase runs under the cross-process WAL lock; the send does
        not. That leaves a window in which two processes have read the same
        file and both replay it — a duplicate, absorbed by the backend's dedup
        — and no window in which one of them can remove a file the other
        still needs, because removal only ever happens after the events are
        delivered or re-persisted. Holding the lock across the send instead
        would serialise every worker in a multi-process deployment behind one
        another's latency to remove a duplication the dedup key already
        handles.
        """
        wal_path = self._wal_path()
        sources = [f"{wal_path}.1", wal_path, self._wal_inflight_path()]
        events: list[dict[str, Any]] = []
        found: list[str] = []
        empty: list[str] = []
        with self._wal_file_lock() as acquired:
            if not acquired:
                logger.warning(
                    "WAL replay skipped: another process holds the WAL lock. "
                    "Its replay covers the same files."
                )
                return
            for candidate in sources:
                try:
                    with open(candidate) as f:
                        lines = f.readlines()
                except FileNotFoundError:
                    continue
                except OSError as e:
                    logger.warning(f"Failed to read WAL {candidate}: {e}")
                    continue
                if not lines:
                    empty.append(candidate)
                    continue
                found.append(candidate)
                for line in lines:
                    try:
                        events.append(json.loads(line.strip()))
                    except json.JSONDecodeError:
                        continue

            if not events:
                # Nothing recovered. Drop leftovers so the next flush starts clean.
                for candidate in empty:
                    try:
                        os.remove(candidate)
                    except OSError as e:
                        logger.warning(f"Failed to remove empty WAL {candidate}: {e}")
                return

        self._buffer.extend(events)
        try:
            self._do_flush()
        except BreakerTransportError as e:
            # `_do_flush` normally swallows this internally (it re-queues the
            # batch), but a transport subclass or a future refactor may not.
            # Recovery must never abort `start()` — the events are already in
            # the buffer, and the fall-through below persists them.
            logger.warning(f"WAL replay flush failed: {e}")

        repersisted = False
        if self._buffer:
            # The flush did not deliver everything. Rewrite the survivors into
            # `.wal` so they survive another crash, and only then discard the
            # older sources. If that write fails the survivors are in memory
            # only — keep every source so the next start can try again.
            if not self._persist_to_wal():
                logger.warning(
                    f"WAL replay: {len(self._buffer)} events still buffered and could not be "
                    f"re-persisted; keeping {found} for the next start"
                )
                return
            repersisted = True

        # Everything recovered is either delivered or — when `repersisted` —
        # sitting in a FRESH `.wal` that `_persist_to_wal` just wrote. That
        # file must be spared: unlinking it here would discard exactly what we
        # persisted. The other sources are now strictly redundant.
        for candidate in found:
            if repersisted and candidate == wal_path:
                continue
            try:
                os.remove(candidate)
            except OSError as e:
                logger.warning(f"Failed to remove WAL {candidate}: {e}")
        logger.info(
            f"Replayed {len(events)} events from WAL"
            + (" (unflushed remainder re-persisted)" if repersisted else "")
        )

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
        # Held refusals are recovered separately and are NOT replayed onto the
        # send path. `_replay_from_wal` recovers undelivered events, which is
        # the right thing for them; these were already refused, so re-sending
        # produces the same refusal while blocking everything queued behind
        # them. They go to the DLQ on the first flush that finds room.
        self._recover_holdover()
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
            if self._persist_to_wal():
                # The buffer is durably in `.wal` (or was already accepted),
                # so any retained `.inflight` copy is now a subset of it.
                # Clear it, or the next replay would load the same events twice.
                self._clear_inflight()
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
        # Space may have freed up since the last refusal (the operator raised
        # the cap, or archived the file with `nullrun-wal`). Draining first
        # means the held refusals land before new traffic, not after an
        # unbounded wait for the next one to fail.
        self._drain_dlq_overflow()

        if not self._buffer:
            logger.debug("Buffer empty, skipping flush")
            return

        batch = self._buffer[:]
        self._buffer.clear()
        # Refilled as the cascade below splits it, so one flush is one budget.
        self._bisect_budget = int(os.environ.get("NULLRUN_MAX_BISECT_REQUESTS", "64"))
        logger.debug(f"Sending batch of {len(batch)} events")

        # Durability barrier: the batch has left `_buffer` and the process
        # can now die at any point. Record it on disk before the send so a
        # crash mid-flight is recoverable on the next start.
        self._persist_inflight(batch)

        # Circuit breaker wrapped send - uses proper 3-state circuit breaker.
        #
        # Only genuine backend UNREACHABILITY may count as a breaker failure.
        # A 4xx is the server answering, which proves the transport is healthy;
        # a TypeError from local serialization is our own bug. Counting either
        # would let a batch of malformed payloads open the circuit on a backend
        # that is plainly up and would keep taking traffic, and then every
        # buffered event sits blocked for the whole recovery window. Those are
        # trapped here and handled after the call.
        #
        # httpx.TransportError (connect refused, DNS, read timeout) and
        # BreakerTransportError (5xx exhaustion) still propagate, so the
        # breaker keeps doing its actual job.
        deferred: list[Exception] = []

        def send_batch():
            try:
                result = self._send_batch_with_retry_info(batch)
            except (BreakerTransportError, httpx.TransportError):
                raise
            except Exception as exc:  # noqa: BLE001
                deferred.append(exc)
                return None
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
            result = self._circuit_breaker.call(send_batch)
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
            # `.inflight` is intentionally NOT cleared here: the batch is
            # back in memory only, and a crash before the next `_persist_to_wal`
            # would lose it. A later successful flush overwrites and clears it.
            metrics.inc_transport("batches_failed")
            return
        except Exception as e:  # noqa: BLE001
            self._retry_or_dlq(batch, e, dead_letter=True)
            return

        if deferred:
            self._route_deferred(batch, deferred[0])
            return

        # The batch was on the wire and came back 2xx. A 2xx is not
        # delivery: settle each event against the per-item answer before
        # clearing `.inflight`.
        if not self._settle_batch_outcome(batch, result):
            # Something is still live. `.inflight` stays — it is the durable
            # copy of those events, and a crash before the next
            # `_persist_to_wal` would otherwise lose them.
            return

        self._batch_attempts.pop(self._failure_signature(batch), None)  # landed
        self._clear_inflight()  # every event is durably accounted for

    def _settle_batch_outcome(self, batch: list[dict[str, Any]], result: Any) -> bool:
        """Apply a 2xx per-item outcome. Returns True when nothing is still live.

        The load-bearing property is the ORDER, not the classification. Every
        event must be durable — written to the DLQ, or still covered by a
        retained ``.inflight`` — BEFORE ``.inflight`` is cleared. Cleared in
        between and a crash loses whatever was only in memory. The other order
        (durable first, crash after) costs a duplicate, which is safe: the
        backend dedups on ``event_id`` and an ``IdempotentReplay`` consume
        comes back accepted, so the replay lands instead of looping. A
        duplicate is recoverable; a silent gap is not.

        That is also why this method writes the DLQ itself and returns rather
        than calling ``_quarantine_to_dlq``: the latter clears ``.inflight``,
        which would be wrong whenever anything in the same batch is being
        re-queued rather than parked.
        """
        if not getattr(result, "body_confirmed", True):
            # We cannot read the answer. Bounded retry, then the DLQ — the
            # budget is what stops an unreadable 200 from looping forever,
            # and the DLQ is what stops it from ending in a silent drop.
            #
            # Negated on the way out: `_retry_or_dlq` answers "is it live",
            # this method answers "is anything settled". Getting that backwards
            # pops the attempt counter on every cycle, so the budget never
            # depletes and the event retries forever.
            return not self._retry_or_dlq(
                batch, UnconfirmedBatchResponse(len(batch)), dead_letter=True
            )

        rejected_by_id = {
            item["event_id"]: item for item in getattr(result, "rejected_items", []) or []
        }
        accepted = set(getattr(result, "accepted_event_ids", []) or [])

        still_live = False
        requeue: list[dict[str, Any]] = []
        unknown: list[tuple[dict[str, Any], str]] = []
        refused = 0

        for event in batch:
            eid = event.get("event_id")
            if eid in accepted:
                self._in_flight.pop(eid, None)
                continue
            refused += 1
            detail = rejected_by_id.get(eid)
            if detail is None:
                # In neither list. The server's answer does not account for
                # this event at all, so we cannot claim it landed. Treated as
                # an unknown refusal: bounded retry, then the DLQ. Guessing
                # "accepted" here is how a partition bug turns into silent
                # data loss, and guessing "rejected" is how a partition bug
                # turns into an infinite retry.
                unknown.append((event, "unaccounted_in_response"))
                continue
            reason = detail.get("reason") or "unspecified"
            if reason in _TERMINAL_REJECTION_REASONS:
                # Durably parked by the time this returns, so it does not keep
                # `.inflight` alive — but the write had to happen first, which
                # is why the DLQ row is written here rather than in the tail.
                if self._dead_letter_row(event, reason):
                    still_live = True

            elif detail.get("retry_after_ms") is not None:
                # The backend gave a retry hint: it expects this to succeed
                # later. Bounded by the per-event attempt counter anyway, so
                # a hint that never materialises cannot pin the buffer.
                requeue.append(event)
                still_live = True
            else:
                unknown.append((event, reason))

        for event, reason in unknown:
            if self._retry_or_dlq(
                [event], UnknownRejectionReason(reason, event.get("event_id")), dead_letter=True
            ):
                still_live = True

        if requeue:
            # Head, not tail: a refusal the backend expects to clear should
            # not sit behind a steady stream of new events until the buffer
            # drains.
            self._requeue_at_head(requeue)
        if refused:
            metrics.inc_transport("events_partial_refused", refused)
        return not still_live

    # 4xx classes that MUST NOT be quarantined. Quarantining these would
    # discard good data at exactly the moment it is recoverable.
    #
    # 408 Request Timeout / 429 Too Many Requests — transient. After an
    # outage every SDK replays its WAL at once, the rate limiter answers
    # 429, and a naive "any 4xx is permanent" rule would quarantine every
    # recovering fleet in the first second of the window.
    #
    # 401 Unauthorized / 403 Forbidden — the key was rotated or revoked.
    # Once the operator fixes the key the batch must still deliver, so it
    # belongs back in the WAL, not on disk as DLQ litter.
    #
    # 413 Payload Too Large — the BATCH is too big, not the events. Halve
    # and resend; every event is individually acceptable.
    _TRANSIENT_4XX = frozenset({408, 429})
    _AUTH_4XX = frozenset({401, 403})
    _RESPLIT_4XX = frozenset({413})
    # Cap on distinct batch signatures tracked for the attempt budget.
    _MAX_TRACKED_BATCHES = 1024
    # Ceiling on bisection. Each level halves the batch, so this bounds the
    # worst case (an all-bad batch) to ~2n sends while still isolating a
    # single offender in any realistic batch.
    _MAX_BISECT_DEPTH = 24

    def _handle_http_rejection(self, batch: list[dict[str, Any]], error: Exception) -> None:
        """Route a 4xx to re-queue, split-and-resend, or DLQ by status class."""
        status = getattr(getattr(error, "response", None), "status_code", None)

        if status in self._TRANSIENT_4XX:
            # Never dead-letter a rate limit or a timeout, no matter how many
            # attempts: the batch is good data, the server is busy. It stays in
            # the WAL until the window opens.
            self._retry_or_dlq(batch, error, dead_letter=False)
            return

        if status in self._AUTH_4XX:
            # Recoverable by operator action. Hold the data, keep the WAL,
            # and say loudly that delivery is blocked on a key problem.
            logger.error(
                f"Backend rejected the batch with {status} (auth). The events stay "
                f"in the WAL and WILL be delivered once the API key is fixed — "
                f"rotate/verify it at https://app.nullrun.io/settings/api-keys. "
                f"Quarantining is deliberately NOT done here."
            )
            self._requeue_at_head(batch)
            metrics.inc_transport("batches_auth_blocked")
            return

        # 413 (batch too big) and 400/422 (one malformed event rejected the
        # whole request) are both resolved the same way: halve and resend. The
        # halves are sent as SEPARATE batches rather than pushed back onto the
        # buffer — the buffer is flushed wholesale, so re-queuing both halves
        # would re-form the batch we just proved the server will not accept,
        # and the bisect would never make progress.
        if len(batch) > 1:
            half = len(batch) // 2
            logger.warning(
                f"Batch of {len(batch)} rejected with {status} — splitting into "
                f"{half} + {len(batch) - half} and resending the halves separately."
            )
            metrics.inc_transport(
                "batches_resplit" if status in self._RESPLIT_4XX else "batches_bisected"
            )
            self._send_subbatches(batch[:half], batch[half:], error)
            return

        # A single event the server will never accept.
        self._quarantine_to_dlq(batch, error)

    def _send_subbatches(
        self, left: list[dict[str, Any]], right: list[dict[str, Any]], error: Exception
    ) -> None:
        """Send both halves as independent batches, isolating a single offender.

        Recursion depth is log2(len(batch)), and `_MAX_BISECT_DEPTH` caps it,
        so an entire bad batch cannot spin here. Whatever is still undeliverable
        when the cap is hit falls back to the whole-batch quarantine.
        """
        if self._bisect_depth >= self._MAX_BISECT_DEPTH:
            logger.error(f"Bisect depth cap reached, quarantining {len(left) + len(right)} events")
            self._quarantine_to_dlq(left + right, error)
            return

        if not self._bisect_request_available(len(left) + len(right)):
            self._quarantine_to_dlq(left + right, error)
            return

        self._bisect_depth += 1
        try:
            for half in (left, right):
                if not half:
                    continue
                self._attempt_half(half, error)
        finally:
            self._bisect_depth -= 1

    def _bisect_request_available(self, remaining: int) -> bool:
        """Whether the cascade may afford to split, and charge it if so.

        A split costs two sends (one per half), so the budget is charged here
        rather than at the send sites: two call sites spending independently
        is how one of them ends up not spending at all.

        Depth alone does not bound the wire: a full halving cascade is 2^depth
        sends, and `_MAX_BISECT_DEPTH` is 24. When the budget runs out, stop
        splitting and park what is left rather than spend the operator's rate
        limit proving once more that a batch is bad — a refusal is exactly
        what a rate limiter starts answering, so the cascade would be feeding
        the condition it is trying to diagnose. Events already delivered in
        the healthy halves stay delivered.
        """
        if self._bisect_budget < 2:
            logger.error(
                f"Bisect request budget ({max(self._bisect_budget, 0)} remaining) "
                f"exhausted — parking the remaining {remaining} event(s) without "
                f"splitting further. Raise NULLRUN_MAX_BISECT_REQUESTS if these "
                f"batches deserve isolating."
            )
            metrics.inc_transport("batches_bisect_budget_exhausted")
            return False
        self._bisect_budget -= 2
        return True

    def _attempt_half(self, half: list[dict[str, Any]], original: Exception) -> None:
        """Send one half, routing the result exactly as a top-level batch would."""
        deferred: list[Exception] = []

        def send():
            try:
                result = self._send_batch_with_retry_info(half)
            except (BreakerTransportError, httpx.TransportError):
                raise
            except Exception as exc:  # noqa: BLE001
                deferred.append(exc)
                return None
            if result.accepted_event_ids:
                for event in half:
                    if event.get("event_id") in result.accepted_event_ids:
                        self._in_flight.pop(event.get("event_id"), None)
            metrics.inc_transport("batches_sent")
            metrics.inc_transport("events_sent", len(half))
            metrics.set_transport("last_flush_at", time.monotonic())
            return result

        try:
            self._circuit_breaker.call(send)
        except BreakerTransportError:
            self._requeue_at_head(half)
            metrics.inc_transport("batches_failed")
            return
        except Exception as exc:  # noqa: BLE001
            self._retry_or_dlq(half, exc, dead_letter=True)
            return

        if deferred:
            self._route_deferred(half, deferred[0])
            return

        self._batch_attempts.pop(self._failure_signature(half), None)

    def _route_deferred(self, batch: list[dict[str, Any]], error: Exception) -> None:
        """Classify a failure that was kept away from the circuit breaker."""
        if isinstance(error, DeterministicBackendRefusal):
            # The backend named the refusal. It named it for the BATCH, and a
            # whole-batch refusal is what an unbatched per-event rejection
            # looks like from out here: one event the backend cannot accept
            # (an unbound execution, a policy limit) makes the request carrying
            # it fail as a unit. Quarantining the batch on that evidence
            # discards every healthy event sharing it — the 49 good events are
            # recorded as refused for a reason that never applied to them.
            #
            # So bisect FIRST, exactly as the 400/422 path does, and only
            # quarantine what survives as a singleton. The bisect is bounded by
            # `_MAX_BISECT_DEPTH` and by a request budget, so an all-bad batch
            # cannot turn this into a request storm.
            if len(batch) > 1 and self._bisect_depth < self._MAX_BISECT_DEPTH:
                if not self._bisect_request_available(len(batch)):
                    metrics.inc_transport("batches_deterministic_refusal")
                    self._quarantine_to_dlq(batch, error)
                    return
                half = len(batch) // 2
                logger.warning(
                    f"Backend refused a batch of {len(batch)} as "
                    f"{error.error_code} — a whole-batch refusal is not evidence "
                    f"about each event, so splitting into {half} + "
                    f"{len(batch) - half} to isolate the offender."
                )
                metrics.inc_transport("batches_bisected")
                self._bisect_depth += 1
                try:
                    self._send_subbatches(batch[:half], batch[half:], error)
                finally:
                    self._bisect_depth -= 1
                return
            logger.error(
                f"Backend refused the batch as {error.error_code} "
                f"(HTTP {error.status_code}) — deterministic, quarantining "
                f"{len(batch)} event(s). {error.detail}"
            )
            metrics.inc_transport("batches_deterministic_refusal")
            self._quarantine_to_dlq(batch, error)
            return
        if isinstance(error, httpx.HTTPStatusError):
            self._handle_http_rejection(batch, error)
        else:
            # Not obviously transient and not an HTTP status either. It may
            # still be deterministic (an event that cannot be serialized will
            # fail identically every cycle), so count attempts and dead-letter
            # once the budget is spent rather than re-queuing forever.
            self._retry_or_dlq(batch, error, dead_letter=True)

    def _requeue_at_head(self, batch: list[dict[str, Any]]) -> None:
        """Put a batch back at the FRONT of the buffer.

        The bisect sends halves directly rather than re-queuing them, so any
        event that comes back has to resume its place: appending would let a
        steady stream of new events push a stalled batch to the back of the
        queue indefinitely. ``.inflight`` stays — the batch is in memory only,
        and a later successful flush overwrites and clears it.
        """
        self._buffer[0:0] = batch

    def _retry_or_dlq(
        self, batch: list[dict[str, Any]], error: Exception, *, dead_letter: bool
    ) -> bool:
        """Re-queue a possibly-transient failure, with a bounded attempt budget.

        Deterministic failures (a payload that will never serialize) repeat
        identically on every cycle and would pin the batch ahead of the whole
        buffer forever. So every re-queue costs an attempt; once the budget is
        spent a ``dead_letter``-eligible batch is quarantined rather than
        blocking delivery forever.

        ``dead_letter=False`` is for genuinely transient statuses (408/429).
        Those are counted for observability but NEVER quarantined: the batch is
        valid data and the server is merely busy, so it waits in the WAL.

        Returns True when the batch is back on the retry path, False when it
        was quarantined. The caller needs the distinction to decide whether
        anything from this send is still live, which is what governs whether
        ``.inflight`` may be cleared.
        """
        key = self._failure_signature(batch)
        attempts = self._batch_attempts.get(key, 0) + 1
        if len(self._batch_attempts) >= self._MAX_TRACKED_BATCHES:
            # A long outage produces an unbounded stream of distinct batch
            # signatures. Drop the map rather than grow it: a reset only costs
            # one extra retry cycle, never correctness.
            logger.warning(
                f"Failure-attempt map hit {self._MAX_TRACKED_BATCHES} entries, resetting"
            )
            self._batch_attempts.clear()
        self._batch_attempts[key] = attempts

        if dead_letter and attempts > self._max_batch_attempts:
            logger.error(
                f"Batch failed {attempts} times without landing: {error}. "
                f"Quarantining {len(batch)} events."
            )
            self._quarantine_to_dlq(batch, error)
            return False

        if not dead_letter:
            logger.warning(
                f"Transient rejection (attempt {attempts}) — holding the batch: {error}"
            )
        else:
            logger.warning(
                f"Batch re-queued (attempt {attempts}/{self._max_batch_attempts}): {error}"
            )
        self._buffer.extend(batch)
        metrics.inc_transport("batches_retried")
        return True

    def _failure_signature(self, batch: list[dict[str, Any]]) -> str:
        """Stable identity for a batch, used as the attempt-counter key."""
        ids = sorted(str(e.get("event_id", "")) for e in batch)
        return hashlib.sha256("|".join(ids).encode()).hexdigest()[:16]

    def _dlq_reason(self, error: Exception) -> str:
        """The machine-readable reason a batch is being parked.

        Prefers what the wire named over how it was wrapped. Several
        deterministic refusals arrive as 5xx (see
        ``_DETERMINISTIC_ERROR_CODES``), and a DLQ row that only says "503"
        tells an operator replaying it nothing about why the event was
        parked. The two reasons this transport raises itself carry their
        reason as a field rather than in a formatted message, for the same
        reason — a message is for a log, a field is for a record.
        """
        if isinstance(error, DeterministicBackendRefusal):
            return f"{error.error_code}:{error.status_code}"
        if isinstance(error, UnknownRejectionReason):
            return f"{error.reason}:unknown"
        if isinstance(error, UnconfirmedBatchResponse):
            return "unconfirmed_response:no_accepted_event_ids"
        status = getattr(getattr(error, "response", None), "status_code", None)
        return f"{type(error).__name__}:{status}"

    def _dlq_row(self, event: dict[str, Any], reason: str) -> dict[str, Any]:
        """Build one versioned DLQ record. See ``_DLQ_ROW_VERSION`` for the schema.

        ``error`` is kept alongside ``reason`` so a v1 reader — anything
        written before this field existed and still on disk across an
        upgrade — keeps working. Dropping it would make a recovery file
        unreadable, which is the one thing a recovery file must not be.
        """
        eid = str(event.get("event_id") or "")
        first_failed = self._first_failed_at.get(eid)
        if first_failed is None:
            first_failed = time.time()
            if eid:
                self._first_failed_at[eid] = first_failed
        signature = self._failure_signature([event])
        return {
            "v": _DLQ_ROW_VERSION,
            "event": event,
            "error": reason,
            "reason": reason,
            "event_type": event.get("type"),
            "first_failed_at": first_failed,
            # Sends this event took to end up here, including the current one.
            # A terminal refusal is parked on its first response and never
            # touches `_retry_or_dlq`, so reading the counter alone would record
            # 0 — "never attempted" — for an event that was in fact refused on
            # the first send. The two are different stories for whoever triages
            # the DLQ, and only this one is true.
            "attempts": self._batch_attempts.get(signature, 0) + 1,
        }

    def _write_dlq_rows(self, rows: list[dict[str, Any]]) -> bool:
        """Append DLQ records durably. Does NOT touch ``.inflight``.

        Kept separate from ``_quarantine_to_dlq`` because that method clears
        ``.inflight``, which is only correct when the WHOLE batch is parked.
        A partial refusal parks some events and re-queues others from the same
        send, and clearing there would drop the re-queued ones on the next
        crash.

        Returns False — and writes nothing — when the DLQ is at its size cap.
        The caller treats False as "not parked", which re-queues the event on
        the retry path. That is the whole design of the cap: a full DLQ
        stalls parking rather than dropping the oldest rows, so the operator
        gets a loud, alertable condition instead of a file that quietly lost
        the events from three hours ago.
        """
        if not rows:
            return True
        # Measured, not estimated from `len(row)`: `len` of a dict is its
        # number of keys, which is off by a factor of an order of magnitude
        # and would make the cap meaningless in the only direction that
        # matters — under-counting.
        incoming = sum(len(json.dumps(r, default=str)) + 1 for r in rows)
        if self._dlq_over_cap(incoming):
            return False
        if not self._write_events_atomic(self._wal_dlq_path(), rows, mode="a"):
            return False
        metrics.set_transport("dlq_bytes", self._dlq_size())
        return True

    def _dead_letter_row(self, event: dict[str, Any], reason: str) -> bool:
        """Park one event, durably, before anything clears ``.inflight``.

        Returns True when the event could NOT be parked and is still live, so
        `.inflight` must be retained.
        """
        if not self._write_dlq_rows([self._dlq_row(event, reason)]):
            # The DLQ could not take it. Re-queueing would put a batch the
            # backend has already refused terminally back on the send path,
            # where it produces the identical refusal on every cycle forever
            # AND sits at the head of the buffer ahead of every healthy event
            # behind it. That is the "one bad event stops the whole stream"
            # failure, and it is caused by parking, not by the refusal.
            #
            # So the event is HELD, not re-sent: a terminal refusal cannot
            # become a success, so another request is pure waste, and holding
            # keeps the head of the buffer clear. `_drain_dlq_overflow` retries
            # the write whenever the DLQ may have room again, and the holdover
            # is bounded — a holdover that grows without limit is the same
            # memory blowup the cap exists to prevent, only invisible.
            self._hold_for_dlq([self._dlq_row(event, reason)], reason)
            return True
        metrics.inc_transport("events_dead_lettered")
        metrics.set_transport("last_dlq_error", reason)
        return False

    def _hold_for_dlq(self, rows: list[dict[str, Any]], reason: str) -> None:
        """Park refused events durably until the DLQ has room.

        The alternative — leaving them on the send path — is what turns a full
        DLQ into an outage: the events are re-sent every cycle, they are
        refused every cycle, and they occupy the head of the buffer the whole
        time, so the healthy events queued behind them are never delivered.
        The refusal is terminal, so re-sending cannot help; only the recording
        can, and that is what the holdover defers.

        They are written to `.wal.holdover`, not just kept in memory. A
        memory-only holdover is a `kill -9` away from total loss: the next
        `_persist_inflight` overwrites `.inflight` with the batch it is about
        to send, so once the process restarts the events are in the DLQ
        nowhere, in `.wal` nowhere, and in `.inflight` overwritten. Keeping
        them in RAM looked like the fix and was a new loss path — the same
        shape of bug as the one that put `replay` in the tree, where the
        report described a durability the code did not have.

        Recovery reads the holdover file back into `_dlq_overflow` at
        startup, so the drain resumes without re-deriving the refusal by
        re-sending. That matters for a terminal reason: re-sending is the one
        thing the holdover exists to avoid, and a restart that recovers the
        events by re-sending them would reintroduce the head-of-buffer
        starvation the hold was built to stop.

        The in-memory index is bounded, and the bound is on COUNT, not bytes.
        Count is what the metric reports and what an operator alerts on; a
        byte bound would need a second cap for the same property and would
        fail silently on a different threshold. Past the bound the oldest
        entries stop being *tracked* — they are not lost, they are in the
        file and `_drain_dlq_overflow` reads the file as well as the index —
        so the only cost is an under-reported `dlq_holdover`, which is loud
        and named in the log rather than silent.
        """
        self._dlq_overflow.extend(rows)
        metrics.inc_transport("dlq_holdover_total", len(rows))
        self._persist_holdover(rows)
        self._trim_holdover_index()
        metrics.set_transport("dlq_holdover", len(self._dlq_overflow))
        metrics.set_transport("last_dlq_error", reason)
        logger.error(
            f"DLQ at its cap — holding {len(self._dlq_overflow)} refused event(s) "
            f"({reason}). They are NOT being re-sent: a terminal refusal cannot "
            f"succeed on retry, and re-sending would block every healthy event "
            f"behind them. They are durably in {self._wal_holdover_path()}, so a "
            f"kill -9 does not lose them. Raise NULLRUN_DLQ_MAX_BYTES, or triage "
            f"and archive the DLQ with `nullrun-wal`, and they drain on the next "
            f"flush."
        )

    def _trim_holdover_index(self) -> None:
        """Bound the in-memory holdover index; the file is the record of truth.

        Only the index is trimmed. Trimming the file would be the cap deleting
        the only copy of an event the SDK could not deliver — the same thing
        `_dlq_over_cap` refuses to do, for the same reason.
        """
        cap = self._holdover_index_max
        if not cap or len(self._dlq_overflow) <= cap:
            return
        dropped = len(self._dlq_overflow) - cap
        del self._dlq_overflow[:-cap]
        metrics.inc_transport("dlq_holdover_index_truncated", dropped)
        logger.error(
            f"Holdover index passed NULLRUN_DLQ_HOLDOVER_MAX_EVENTS ({cap}); "
            f"stopped tracking the {dropped} oldest held event(s). They are NOT "
            f"lost — {self._wal_holdover_path()} still holds them and the drain "
            f"reads it — but `dlq_holdover` now under-reports by {dropped}. Raise "
            f"the env var to keep the count exact."
        )

    def _persist_holdover(self, rows: list[dict[str, Any]]) -> None:
        """Append the held rows to `.wal.holdover`.

        Append, and the rows are the same DLQ-shaped dicts the drain already
        knows how to write — a `event_id` plus the `event` — so the drain does
        not need a second format, and `nullrun-wal` can archive the holdover
        file with the same reader it uses for the DLQ.

        Failure to persist is loud but not fatal. The events remain in the
        index and the caller has already been told the DLQ refused them, so
        raising here would abort a flush over a bookkeeping failure on a path
        whose whole purpose is to not lose the event. The log says the one
        true thing: a kill before a later attempt loses them.
        """
        if not rows:
            return
        if not self._write_events_atomic(self._wal_holdover_path(), rows, mode="a"):
            logger.error(
                f"Could not persist {len(rows)} held refusal(s) to "
                f"{self._wal_holdover_path()} (lock held elsewhere, or I/O "
                f"failed). They are in memory only, and a kill before a later "
                f"attempt loses them."
            )
            metrics.inc_transport("dlq_holdover_persist_failures", len(rows))

    def _recover_holdover(self) -> None:
        """Load rows from `.wal.holdover` into the index at startup.

        Appends rather than replaces, so a second process that finds the file
        already drained — or empty — does not erase an index it does not own.
        The lock is not taken: read-only, and the only writer appends whole
        lines atomically, so the worst case is one incomplete final line,
        which is dropped exactly as `_replay_from_wal` drops it.
        """
        path = self._wal_holdover_path()
        try:
            with open(path) as f:
                lines = f.readlines()
        except FileNotFoundError:
            return
        except OSError as e:
            logger.warning(f"Failed to read holdover file {path}: {e}")
            return
        recovered: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # A torn tail from a kill mid-append. The event is not
                # recoverable from this line, and the refusal will simply be
                # re-derived if the event is still in another source.
                continue
            if isinstance(row, dict):
                recovered.append(row)
        if recovered:
            self._dlq_overflow.extend(recovered)
            logger.warning(
                f"Recovered {len(recovered)} held refusal(s) from {path}. They "
                f"were refused and the DLQ was full when the process died; they "
                f"are NOT re-sent, they go to the DLQ as soon as it has room."
            )
        self._trim_holdover_index()

    def _drain_dlq_overflow(self) -> int:
        """Try to move held refusals into the DLQ now that space may exist.

        Called at the top of every flush. Returns how many rows landed. A
        refused write is a no-op — the holdover is unchanged and the operator
        still sees the same alert — so this is safe to attempt unconditionally
        and cheap when there is nothing held.

        The holdover FILE is released by `event_id`, not deleted wholesale.
        The index can be shorter than the file: `_trim_holdover_index` drops
        the oldest entries from memory to bound it, and those rows are still
        owed a DLQ. Removing the file after a partial drain would delete them.
        Deleting by id also means a crash between the DLQ write and the file
        release replays into the DLQ a second time rather than losing
        anything — at-least-once, which is the same trade the WAL already
        makes and which the `event_id` dedup absorbs.
        """
        if not self._dlq_overflow:
            return 0
        pending, self._dlq_overflow = self._dlq_overflow, []
        written = self._write_dlq_rows(pending)
        if not written:
            self._dlq_overflow = pending  # unchanged; try again next cycle
            return 0
        if not self._release_holdover(
            {
                str(r["event"]["event_id"])
                for r in pending
                if isinstance(r.get("event"), dict) and r["event"].get("event_id")
            }
        ):
            # The rows are in the DLQ. The file still names them, so a restart
            # re-drains them into the DLQ a second time — a duplicate the
            # `event_id` dedup absorbs, which is strictly better than the
            # alternative of not knowing whether the release happened.
            logger.warning(
                f"Could not release {self._wal_holdover_path()} after draining "
                f"{len(pending)} refusal(s) into the DLQ. The rows are safely in "
                f"the DLQ; the stale holdover file will re-drain them on the next "
                f"start, which duplicates rather than loses."
            )
        metrics.inc_transport("events_dead_lettered", len(pending))
        metrics.set_transport("dlq_holdover", len(self._dlq_overflow))
        logger.info(f"Drained {len(pending)} held refusal(s) into the DLQ")
        return len(pending)

    def _release_holdover(self, event_ids: set[str]) -> bool:
        """Rewrite `.wal.holdover` without the drained `event_id`s.

        The id is read from ``row["event"]["event_id"]`` because that is where
        `_dlq_row` puts it. Matching a top-level ``event_id`` — which the row
        schema does not have — would release nothing while reporting success,
        which is the failure mode this whole file exists to remove.

        Returns True when the file now names none of them (including the
        not-there case, which is success), False when the rewrite failed and
        the caller must assume the rows are still listed.
        """
        if not event_ids:
            return True
        path = self._wal_holdover_path()
        try:
            with open(path) as f:
                lines = f.readlines()
        except FileNotFoundError:
            return True
        except OSError as e:
            logger.warning(f"Failed to read holdover file {path} to release: {e}")
            return False
        kept: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # Unparseable: we cannot prove which event it is, so it stays.
                # Dropping it would be deleting the only copy of an unknown row.
                kept.append({"raw": line})
                continue
            event = row.get("event") if isinstance(row, dict) else None
            eid = str(event.get("event_id")) if isinstance(event, dict) else None
            if eid not in event_ids:
                kept.append(row)
        try:
            if not kept:
                os.remove(path)
                return True
            return self._write_events_atomic(path, kept)
        except OSError as e:
            logger.warning(f"Failed to release holdover file {path}: {e}")
            return False

    def _quarantine_to_dlq(self, batch: list[dict[str, Any]], error: Exception) -> None:
        """Move a permanently-rejected batch out of the retry path.

        A 4xx will never succeed on retry, so the events go to ``.dlq`` with
        the reason attached and `.inflight` is cleared — otherwise every
        subsequent start replays a batch that is guaranteed to fail and the
        poison pill blocks the whole WAL. Nothing is silently discarded: the
        file is inspectable and re-submittable after the caller is fixed.

        The whole batch is parked here, so clearing ``.inflight`` afterwards
        is correct — but ONLY once the DLQ write is confirmed. A failed
        write (full cap, read-only volume, lost lock) leaves the batch on the
        retry path and retains ``.inflight``, because the alternative is
        clearing the only durable copy of events that were not parked
        anywhere. The events will be refused again on the next send, which is
        the correct outcome for a permanent rejection we could not record.
        """
        reason = self._dlq_reason(error)
        logger.error(
            f"Permanent rejection ({reason}) on /track/batch — quarantining "
            f"{len(batch)} events to DLQ. They will NOT be retried."
        )
        rows = [self._dlq_row(e, reason) for e in batch]
        if not self._write_dlq_rows(rows):
            # Same reasoning as `_dead_letter_row`: the batch is terminally
            # refused, so re-sending it produces the same refusal forever
            # while blocking everything behind it. Hold instead.
            self._hold_for_dlq(rows, reason)
            return
        self._clear_inflight()
        metrics.inc_transport("events_dead_lettered", len(batch))
        metrics.set_transport("last_dlq_error", reason)

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
        # Per-item refusals from a 200 body: `{"event_id", "reason",
        # "retry_after_ms"}`. Absent `retry_after_ms` (None) means the backend
        # gave no retry hint for that event.
        rejected_items: list[dict[str, Any]] = field(default_factory=list)
        # False when the response was a success status but the body could not
        # be read as a confirmation (proxy HTML page, empty body, a backend
        # that predates `accepted_event_ids`). A 200 we cannot interpret is
        # NOT proof of delivery — treating it as one is the failure this
        # field exists to prevent.
        body_confirmed: bool = True

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

        Thin wrapper over the module-level helper, which the retry loop also
        needs and which cannot reach a Transport instance.
        """
        return _retry_after_seconds_from(response)

    def _send_batch_with_retry_info(self, batch: list[dict[str, Any]]) -> "SendResult":
        """Send batch to server. Returns SendResult with retry info. Wrapped by _retry_with_backoff."""
        logger.debug(f"Sending batch of {len(batch)} events to {self.api_url}/api/v1/track/batch")
        body = _signed_request_body({"events": batch})

        # S008: re-sign per attempt — see the long note at
        # `do_execute_request` (same defect, same fix).
        #
        # Inner function is the unit of retry:
        # * 5xx → retry helper backs off. 429 honors Retry-After.
        # * 4xx (other than 429) → return as-is; these are real client bugs
        #   (auth, payload) and must NOT be retried.
        def _post_batch() -> httpx.Response:
            resp = self._client.post(
                f"{self.api_url}/api/v1/track/batch",
                content=body,
                headers=self._build_signed_headers(body=body),
            )
            # A deterministic refusal is checked BEFORE the status test, and on
            # any status. The backend sent EXECUTION_NOT_BOUND as 503 until the
            # status correction (now 422), and a status-first test files the
            # old shape under "the server is down": it retries, and counts
            # every attempt as a transport failure. Testing the code first is
            # what makes this correct against a server we may not control.
            code = _extract_backend_error_code(resp)
            if code in _DETERMINISTIC_ERROR_CODES and resp.status_code >= 400:
                raise DeterministicBackendRefusal(
                    code, resp.status_code, _extract_error_message(resp)
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
            cancel=self._stop_event,
        )

        # P0: Extract retry_after from response headers or body
        retry_after_seconds: float | None = None
        retry_after_ms: float | None = None
        is_policy_limit = False

        # Check Retry-After header (may be seconds or HTTP-date)
        retry_after_seconds = self._extract_retry_after(response)

        # Parse the body ONCE. Three consumers below need it — the batch-level
        # `rejected` block, the per-item `rejection_details`, and the actions
        # loop — and each used to call `response.json()` independently, so a
        # non-JSON body was re-raised and re-caught once per consumer.
        data: dict[str, Any] | None = None
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                data = parsed
        except Exception:  # noqa: S110 — non-JSON body; handled below per status
            data = None

        # Check response body for batch-level retry info
        if data is not None:
            rejected_info = data.get("rejected")
            if isinstance(rejected_info, dict):
                if "retry_after_ms" in rejected_info:
                    retry_after_ms = rejected_info["retry_after_ms"]
                if rejected_info.get("reason") == "policy_limit":
                    is_policy_limit = True

        # ---- Per-item outcome of a 200 ------------------------------------
        # A 200 from /track/batch is NOT proof of delivery. The endpoint
        # refuses individual events and still answers 200, naming the
        # survivors in `accepted_event_ids` and the casualties in
        # `rejection_details`. The pre-fix code read the status, saw 200,
        # treated the whole batch as delivered, cleared `.inflight` and
        # dropped the refused events with no trace in the buffer and none in
        # the DLQ. That is the largest hole in the "data is not lost" claim,
        # which until now held only for transport failures.
        #
        # `accepted_event_ids` is read as a partition, not a hint: anything
        # absent from it is NOT delivered, whatever the status said. That is
        # the only reading an SDK can act on, and it is why the backend has
        # to report events it persisted through other paths (the span /
        # org_action_catalog writes) — see the `accepted_event_ids` partition
        # fix in backend handlers.rs.
        accepted_event_ids: list[str] = []
        rejected_items: list[dict[str, Any]] = []
        body_confirmed = True
        if response.status_code < 400:
            raw_accepted = (data or {}).get("accepted_event_ids")
            if data is None or not isinstance(raw_accepted, list):
                # 200 we cannot read as a confirmation. Either something in
                # front of the backend answered with an HTML error page and a
                # 200 status, or the backend predates `accepted_event_ids`.
                # Neither is evidence that anything landed. Treating this as
                # delivery is precisely the loss we are preventing, so the
                # batch stays on the retry path under a bounded budget.
                body_confirmed = False
                logger.error(
                    f"/track/batch returned {response.status_code} with a body "
                    f"that is not a batch confirmation (parseable dict with an "
                    f"accepted_event_ids list: {data is not None}). Treating "
                    f"{len(batch)} events as NOT delivered. A reverse proxy "
                    f"answering 200, or a backend older than the "
                    f"accepted_event_ids field, produces this."
                )
            else:
                accepted_event_ids = [i for i in raw_accepted if isinstance(i, str)]
                raw_details = (data or {}).get("rejection_details")
                if isinstance(raw_details, list):
                    for detail in raw_details:
                        if not isinstance(detail, dict):
                            continue
                        eid = detail.get("event_id")
                        reason = detail.get("reason")
                        if not isinstance(eid, str) or not isinstance(reason, str):
                            continue
                        hint = detail.get("retry_after_ms")
                        rejected_items.append(
                            {
                                "event_id": eid,
                                "reason": reason,
                                "retry_after_ms": (
                                    float(hint) if isinstance(hint, (int, float)) else None
                                ),
                            }
                        )

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
            actions = (data or {}).get("actions") or []
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
            for msg in (data or {}).get("messages", []) or []:
                logger.info("Backend message: %s", msg)
        except Exception as e:
            logger.warning(f"Failed to process actions: {e}")

        logger.debug(f"Batch track: sent {len(batch)} events")
        return self.SendResult(
            accepted_event_ids=accepted_event_ids,
            retry_after_ms=retry_after_ms,
            is_policy_limit=is_policy_limit,
            rejected_items=rejected_items,
            body_confirmed=body_confirmed,
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
        # to match CLAUDE.md §4 ("DEFAULT: fail-CLOSED для всех
        # enforcement путей"). /execute is the primary enforcement
        # point (see docstring) — when the gateway is unreachable the
        # body MUST NOT run on a silent local pass. Callers that
        # intentionally want fail-OPEN on this path (dev / test
        # harnesses without a live engine) must opt in by passing
        # ``fallback_mode=FallbackMode.PERMISSIVE`` explicitly.
        fallback_mode: FallbackMode = FallbackMode.STRICT,
        operation_id: str | None = None,
        approval_id: str | None = None,
        # Typed-impact + digest-bound approval. Forwarded when the
        # gate built them so the backend can stamp the approval row
        # with the digest.
        business_impact: dict[str, Any] | None = None,
        action_digest: str | None = None,
        # Tool-call argument bag forwarded on /execute so the gate can compute
        # a schema fingerprint and write it to mcp_tool_signatures.
        tool_arguments: dict[str, Any] | None = None,
        # Per-call `tools` list forwarded on /execute so the backend's
        # Step 3 tool_block check (`backend/src/proxy/http/gate/orchestrator.rs:1847-1893`)
        # can match each tool against the workflow's effective `tool_patterns`
        # aggregate. Without this, TB-1 fails closed with `no_tools_field`
        # whenever the workflow has an active `policy.tool_patterns` block.
        # Populated by `runtime.execute` from the `get_call_tools()` contextvar
        # when the caller invoked `set_call_context(tools=...)`.
        tools: tuple[str, ...] | None = None,
        on_transport_error: TransportErrorHandler | None = None,
    ) -> dict[str, Any]:
        """Pre-execution policy evaluation via /api/v1/execute (PRIMARY enforcement point).

        Wire contract: /execute requires a prior /gate call that minted the
        same ``execution_id`` and registered the ``execution:{id}``
        binding in Redis. Backend enforcement:
        ``backend/src/proxy/http/gate/execute.rs``
        runs ``HGET execution:{id} ORG_FIELD`` on entry; a miss
        returns 404 EXECUTION_NOT_FOUND (fail-CLOSED). The SDK
        therefore MUST thread the execution_id captured by
        ``runtime.check_workflow_budget`` (which calls ``Transport.check``,
        i.e. /gate) into the body of this /execute call. See
        ``runtime.execute()`` (line ~2820) for the reuse path; this
        method's caller is the single source of truth for
        ``execution_id`` selection.

        be called rather than /gate" — that contract was the legacy
        preceded by /gate for the same execution_id" — the budget
        pre-flight (Transport.check, /api/v1/gate) is the binding
        registrar; /execute is the policy decision that re-uses it.

        Args:
            organization_id: Organization identifier
            execution_id: Execution identifier
            trace_id: Distributed trace ID
            tool: Tool to execute
            input_data: Tool input
            mode: Execution mode ("auto", "inline", "strict")
            fallback_mode: :class:`FallbackMode` enum (STRICT or
                PERMISSIVE). Default STRICT (fail-CLOSED on transport
                failure per CLAUDE.md §4).
            operation_id: Optional idempotency key
            on_transport_error: Optional callback invoked on BreakerTransportError.
                When set, the callback's return value is returned verbatim; otherwise
                the request falls through to fallback_mode. The gate sets this
                to convert the error into a NullRunBlockedException (fail-CLOSED).

        Returns:
            Dict with:
                - decision: "allow" | "block" | "flag" | "pause" | "require_approval"
                - decision_source: "gateway" | "cached" | "fallback"
                - explanation: Human-readable explanation
                - policy_hash: Server-side SHA-256 of the policy applied
                  (v4 wire field; null on pre-v4 backends). NOT a
                  sequential `policy_version` number — wire v3/v4 backends
                  emit only `policy_hash`. Synthetic fallback dicts ship
                  `policy_version: 0` for legacy compatibility; real
                  responses populate `policy_hash` only.
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
        if tools is not None:
            gate_request["tools"] = list(tools)

        body = _signed_request_body(gate_request)

        # S008 / DEF-MP-TS12-ENF-01 (2026-09-29): sign INSIDE the
        # retry closure. Pre-fix `headers` was built once here, so
        # every one of the (up to 10) retries replayed a byte-identical
        # signature. The backend's S008 replay guard
        # (`hmac:replay:{key_fp}:{sig_hash}`, `hmac_verify.rs`) marks
        # the first occurrence and rejects the rest as HMAC_REPLAY —
        # so a single transient 5xx turned the whole retry budget into
        # a wall of replay rejections, and the 401 that came back was
        # indistinguishable from a genuinely invalid key.
        #
        # `_build_signed_headers` recomputes `int(time.time())` and the
        # HMAC on every call, so each attempt now carries a distinct
        # `sig_hash` and is not a replay. The backend deferred exactly
        # this fix ("the Python SDK builds its signed headers ONCE
        # outside the retry closure ... Tracked as S008 v2") pending
        # this change. A true single-use `X-Nonce` remains a protocol
        # change and is deliberately still out of scope.
        def do_execute_request() -> httpx.Response:
            return self._client.post(
                f"{self.api_url}/api/v1/execute",
                content=body,
                headers=self._build_signed_headers(body=body),
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
                cancel=self._stop_event,
            )

            if response.status_code == 200:
                data = response.json()
                data["decision_source"] = DecisionSource.GATEWAY
                # 0.7.0 thin client: no local policy cache. The next
                return data  # type: ignore[no-any-return]
            elif response.status_code >= 400:
                # 4xx — don't retry.
                #
                # this branch dropped the wire envelope on the floor
                # and synthesised a generic ``{"decision": "block",
                # "explanation": "Gateway returned 409"}`` dict. That
                # hid every wire-coded reason (`APPROVAL_REPLAY_REJECTED`,
                # `APPROVAL_DENIED`, `BUDGET_HARD_BLOCKED`, etc.) behind
                # a single string, so the runtime block dispatch fell
                # through to ``NR-X001`` and `format_user_message`
                # produced the catalogue fallback ("Something went
                # wrong. Please try again.") instead of the typed
                # `NR-A015` message. Cookbook callers had no way to
                # branch on the precise cause.
                #
                # Post-fix: parse the envelope via the existing
                # `_parse_v3_error_envelope` helper — it covers the
                # v3 wire envelope for every /execute reject reason,
                # including the six typed approval grant-consume
                # outcomes (`APPROVAL_NOT_YET_APPROVED` →
                # ``NullRunApprovalNotYetApprovedError`` (NR-A010),
                # `APPROVAL_DENIED` → NR-A011,
                # `APPROVAL_EXPIRED` → NR-A012,
                # `APPROVAL_DIGEST_MISMATCH` → NR-A013,
                # `APPROVAL_TOOL_DIGEST_MISMATCH` → NR-A014,
                # `APPROVAL_REPLAY_REJECTED` → NR-A015 / ``
                # NullRunApprovalReplayRejectedError``) — and raise
                # the typed exception so the @protect /
                # runtime.execute() exception arms propagate the
                # right class up to the caller.
                #
                # Fall through to the synthetic block shape if the
                # envelope is unrecognised (plaintext body, malformed
                # JSON, unknown wire code). `_parse_v3_error_envelope`
                # always returns an Exception — it never silently
                # swallows a 4xx.
                try:
                    raise _parse_v3_error_envelope(response, "execute")
                except NullRunApprovalReplayRejectedError:
                    # The exact case the user reported: the operator
                    # approved, the SDK polled /execute again, and
                    # the backend's atomic consume_approved UPDATE
                    # returned zero rows (replay race — UI approve
                    # vs SDK re-check). Surface the typed exception
                    # so `format_user_message` yields the NR-A015
                    # catalogue line ("Your request couldn't be
                    # completed because the approval has already
                    # been used. Please start a new request.")
                    # instead of the fallback.
                    metrics.inc_transport("execute_block_replay_rejected")
                    raise
                except NullRunBlockedException:
                    # All other typed blocks from the dispatch —
                    # budget, rate, tool, approval-deny, etc.
                    # Re-raise for the @protect / runtime.execute
                    # arms to handle.
                    metrics.inc_transport("execute_block_typed")
                    raise
                except NullRunBackendError:
                    # 5xx-classified envelope parsed as a typed
                    # backend error (shouldn't normally land here
                    # because the helper maps 5xx to GATEWAY_ERROR
                    # via NullRunTransportError, but stays
                    # defensive). Re-raise.
                    raise
                except NullRunAuthenticationError:
                    # 401 envelope parsed as auth error — surface
                    # directly so the caller can react.
                    raise
                except NullRunTransportError:
                    # Transport-classified (network, breaker) — not
                    # a real 4xx, but helper may return one if the
                    # envelope shape is ambiguous. Re-raise so the
                    # on_transport_error arm sees it.
                    raise
                except NullRunDecision:
                    # umbrella pass-through for typed Decision
                    # subclasses NOT in the NullRunBlockedException
                    # MRO. Specifically:
                    #   - NullRunChainError (NR-CH001) — chain
                    #     lifetime / cross-org / Execution Graph
                    #     parent-lineage rejections
                    #   - NullRunWorkflowInactiveError (NR-W004) —
                    #     soft-deleted workflow
                    #   - NullRunConsumeOverbudgetError (NR-O001) —
                    #     CONSUME > RESERVE + epsilon_cents invariant
                    #   - WorkflowPausedException (NR-W003)
                    # Pre-fix these fell through to `except
                    # Exception: pass` below and got silently
                    # swallowed into the synthetic block shape
                    # (`{"decision": "block", "decision_source":
                    # FALLBACK, "explanation": f"Gateway returned
                    # {response.status_code}"}`) — losing
                    # exc.chain_id / exc.parent_execution_id (Chain),
                    # exc.workflow_id (WorkflowInactive),
                    # exc.execution_id / exc.reserved_cents /
                    # exc.actual_cost_cents / exc.epsilon_cents
                    # (ConsumeOverbudget), and every typed
                    # `error_code`/user-action. MUST come AFTER the
                    # NullRunBlockedException arm above so the typed
                    # approval / budget / tool-block path still
                    # matches by MRO specificity.
                    metrics.inc_transport("execute_block_decision_typed")
                    raise
                except NullRunInfrastructureError:
                    # umbrella pass-through for typed
                    # Infrastructure subclasses NOT in the
                    # NullRunBackendError / NullRunAuthenticationError /
                    # NullRunTransportError MRO branches above.
                    # Specifically:
                    #   - NullRunProtocolError (NR-P001) —
                    #     PROTOCOL_TOO_OLD / PROTOCOL_TOO_NEW /
                    #     PROTOCOL_HEADER_INVALID /
                    #     PROTOCOL_HEADER_REQUIRED
                    #   - NullRunRateLimitRedisError (NR-R002) —
                    #     RATE_LIMIT_REDIS_UNAVAILABLE
                    #   - NullRunConfigError (NR-Cxxx) — when raised
                    #     from a wire envelope (rare; mostly SDK-side)
                    # NullRunAuthError (NR-A003) IS in the
                    # NullRunAuthenticationError arm above (parent
                    # class match), but listing here for completeness
                    # preserves the documented recovery contract
                    # even if a future refactor reorders the prior
                    # arms.
                    # Pre-fix these fell through to `except Exception:
                    # pass` below — same synthetic-block loss as the
                    # Decision path. MUST come AFTER the three
                    # specific parent arms above (Backend, Auth,
                    # Transport) so the wire-classified exceptions
                    # still match by MRO specificity.
                    metrics.inc_transport("execute_block_infra_typed")
                    raise
                except Exception:
                    # Unrecognised envelope (plaintext body, legacy
                    # slug, malformed JSON). Fall through to the
                    # synthetic block shape so old / non-v3 backends
                    # keep working and ``on_transport_error="raise"``
                    # callers still see a usable dict. The retry
                    # helper has already given up; emitting a typed
                    # exception here would mask unknown wire codes
                    # the user hasn't yet catalogued.
                    pass
                return {
                    "decision": "block",
                    "decision_source": DecisionSource.FALLBACK,
                    "explanation": f"Gateway returned {response.status_code}",
                    "policy_hash": None,
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
                    "policy_hash": None,
                }
            if on_transport_error == "closed":
                return {
                    "decision": "block",
                    "decision_source": TransportErrorSource.NETWORK_ERROR,
                    "explanation": f"Gateway unreachable: {exc}",
                    "policy_hash": None,
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
        if fallback_mode == FallbackMode.STRICT:  # type: ignore[comparison-overlap]
            return {
                "decision": "block",
                "decision_source": DecisionSource.FALLBACK,
                "explanation": "Gateway unavailable, fallback=STRICT",
                "policy_version": 0,
            }
        else:  # PERMISSIVE (opt-in)
            # Default is STRICT (fail-CLOSED); PERMISSIVE requires the
            # caller to pass ``fallback_mode=FallbackMode.PERMISSIVE``
            # explicitly. Synthesizes an allow + decision_source=FALLBACK
            # so the caller / @protect decorator can still observe that
            # the engine was unreachable.
            return {
                "decision": "allow",
                "decision_source": DecisionSource.FALLBACK,
                "explanation": "Gateway unavailable, fallback=PERMISSIVE",
                "policy_version": 0,
            }

    def check(
        self,
        check_request: dict[str, Any],
        on_transport_error: TransportErrorHandler | None = None,
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
        # v0.16.1 (Phase-1+ wire-shape fix): runtime.check_workflow_budget
        # always sets `action_digest` so the gate's
        # `if req.action_digest.is_none()` version-gate passes
        # Pre-v0.16.1 / Phase-0 callers can still omit it (forwarded
        # only when truthy) without triggering a "field present
        # but None" wire-shape drift.
        if check_request.get("action_digest"):
            gate_request["action_digest"] = check_request["action_digest"]
        # The BusinessImpact envelope the digest was computed over.
        # The backend stores `action_digest` on the approval row and,
        # at /execute, recomputes it from the envelope in THAT request
        # and compares (payload_binding.rs:163, orchestrator.rs:1511),
        # so both endpoints have to carry the same envelope. Sending
        # the digest without the envelope leaves the server unable to
        # reproduce what it stored -- the /execute re-entry then fails
        # CLOSED with APPROVAL_DIGEST_MISMATCH.
        #
        # `internal.rs:216` declares it on GateRequest and
        # `internal.rs:6947` round-trips it through serde.
        #
        # Forwarded whenever present. `{"kind": "none"}` is a real
        # value here, not an absence: it is what an LLM check with no
        # tool to name sends, and the backend distinguishes it from
        # an omitted envelope.
        if check_request.get("business_impact") is not None:
            gate_request["business_impact"] = check_request["business_impact"]
        # Forward the `tool_arguments` bag alongside `tool` so
        # the gate can hash it via `signature::compute_schema_hash`
        # and write the fingerprint into `mcp_tool_signatures`.
        # Legacy SDKs never set this; the backend's gate falls
        # back to a derived signature when the field is missing,
        # so legacy callers do not regress. The shape is
        # `Optional[dict[str, Any]]` -- the backend
        # canonicalises the JSON before hashing, so field
        # ordering inside the dict does not affect the
        # fingerprint.
        if "tool_arguments" in check_request and check_request["tool_arguments"] is not None:
            gate_request["tool_arguments"] = check_request["tool_arguments"]
        # DEF-TC29-001 (2026-10-02, QA RUN_ID 20261002T0826): forward
        # the MCP tool class + per-tool annotations.
        #
        # `check_workflow_budget` has computed both since the MCP
        # integration landed — it reads `get_call_mcp_class()` /
        # `get_call_mcp_annotations()` off the call context and sets
        # them on `check_req` (`runtime.py:2383-2388`) — but this
        # method never sent `check_req`. It rebuilds the body from the
        # allowlist above, and neither key was on it, so both values
        # were discarded here without a word. Confirmed on the wire
        # against prod: `set_mcp_tool_context(tool_class="mcp",
        # annotations={"read_only": False, "destructive": True,
        # "open_world": False})` produced a `/gate` body with
        # neither field. The public `set_mcp_tool_context` API and the
        # `toolbox.mcp` auto-classification path were dead end to end.
        #
        # The backend already accepts and honours both
        # (`gate/internal.rs:318-341` states the forwarding contract;
        # `gate/tool_canonical.rs:229-249` defines `McpAnnotations` as
        # `read_only` / `destructive` / `open_world`).
        #
        # Guarded on `is not None`, NOT on key presence. The backend
        # pins the negative case too — `internal.rs:8291-8295` asserts
        # `tool_class=None` / `mcp_annotations=None` must not appear in
        # the JSON — and an absent annotation means "unknown", not
        # "false" (`internal.rs:334-339`). Serialising `null` would be a
        # different value carrying a different meaning.
        if check_request.get("tool_class") is not None:
            gate_request["tool_class"] = check_request["tool_class"]
        if check_request.get("mcp_annotations") is not None:
            gate_request["mcp_annotations"] = check_request["mcp_annotations"]
        _parent_execution_id = check_request.get("parent_execution_id", parent_execution_id)
        if _parent_execution_id is not None:
            gate_request["parent_execution_id"] = _parent_execution_id

        body = _signed_request_body(gate_request)

        # S008 / DEF-MP-TS12-ENF-01 (2026-09-29): sign INSIDE the
        # retry closure. Pre-fix `headers` was built once, so each of
        # the 3 retries replayed a byte-identical signature and the
        # backend's S008 guard rejected the retry as HMAC_REPLAY. That
        # 401 is what surfaced to `check_workflow_budget` as a
        # credential error during the TS-12 cycle (prod x684). See the
        # long note at `do_execute_request` for the full rationale.
        #
        # ``_retry_with_backoff`` with ``retry_on_5xx=True`` and
        # ``max_retries=3`` (per audit recommendation: "less than
        # 10 — /gate is critical and too many retries amplify
        # load"). Pre-fix this code path returned a synthetic block
        # on the FIRST 5xx — the agent caller never received a real
        # gate decision, violating CLAUDE.md §4 "fail-CLOSED ≠
        # fail-NO-CHECK". A transient 503 from a rolling deploy
        # would silently flip every agent to "budget blocked" even
        # though the budget was fine.
        def _do_gate_post() -> httpx.Response:
            return self._client.post(
                f"{self.api_url}/api/v1/gate",
                content=body,
                headers=self._build_signed_headers(body=body),
                timeout=5.0,
            )

        try:
            response = _retry_with_backoff(
                _do_gate_post,
                max_retries=3,
                base_delay=0.5,
                max_delay=10.0,
                backoff_factor=2.0,
                jitter=0.1,
                retry_on_5xx=True,
                on_transport_error=on_transport_error,
                cancel=self._stop_event,
            )

            if response.status_code == 200:
                return response.json()  # type: ignore[no-any-return]
            # 4xx is a REAL gate decision — surface it through the
            # existing block / throttle / soft_pass dispatch in
            # runtime.check_workflow_budget. The runtime's
            # ``decision_source != fallback`` check honours the wire
            # decision and raises ``NullRunBudgetError`` via its
            # existing ``decision=="block"`` arm. The wire
            # ``error_code`` / ``explanation`` / ``policy_id`` /
            # ``details`` are preserved so the catalogue formatter
            # can produce an actionable message.
            #
            # A gate refusal is a refusal whatever the status.
            # ADR-064 owns this rule; §4.7 of ADR-063 records the
            # correction and points here.
            #
            # The distinction is not the status and not a store
            # name — it is whether the gate produced an ANSWER:
            #
            #   * the check could not be performed, and the gate
            #     said so (CIRCUIT_BREAKER_STATE_LOOKUP_FAILED,
            #     WORKFLOW_INACTIVE_LOOKUP_FAILED,
            #     RATE_LIMIT_PLAN_LOOKUP_FAILED — category "infra",
            #     status 503) — arrives as 503 WITH
            #     decision="block", and the decision stands.
            #   * nothing reached the gate — a proxy 502, a gateway
            #     that never answered, `/budget/approximate`'s
            #     `BudgetUnavailableResponse` (budget.rs:246, which
            #     carries no `decision` field at all) — arrives 5xx
            #     with no refusal body, and ADR-008's fail-OPEN
            #     applies.
            #
            # So the discriminator below is `decision == "block"`,
            # which `GateResponse` always serialises. Do not look
            # for a fail-closed marker: `GateErrorCode::is_fail_closed`
            # is an in-process Rust method that is never written to
            # the wire, and a client written against it could not
            # have found it. ADR-064 §Correction is the full
            # history.
            #
            # Pre-fix the entire 5xx band fell through to the
            # synthetic FALLBACK block below, which the runtime
            # reads as a transport error and fails OPEN — so a 503
            # refusal the gate had actually made was silently
            # converted into "allowed". That is DEF-MP-TS12-ENF-01's
            # exact shape with a different trigger, and it is why a
            # 5xx body that is a genuine refusal is handled here
            # instead.
            #
            # Old SDKs are unaffected by definition: they never
            # looked at `category`, and they read the 503 as a
            # transport error. They keep failing open, which is the
            # documented pre-0.19.0 behaviour.
            try:
                wire_body = response.json()
            except Exception:
                wire_body = {}
            # ADR-062 §2.2. Classify the refusal BEFORE building
            # the synthetic response dict, and let an
            # unclassifiable one escape as
            # ``NullRunUnclassifiedRefusalError`` rather than
            # being flattened into the generic block below.
            #
            # The dict this branch returns hardcodes
            # ``decision="block"`` for every 4xx, including ones
            # that were never gate refusals (a 400 protocol
            # mismatch has no ``decision`` field at all).
            # ``resolve_refusal_category`` discriminates on the
            # WIRE body, not on the status, so those return
            # ``None`` and keep their pre-existing handling.
            is_refusal = is_gate_refusal(wire_body)
            if 400 <= response.status_code < 500 or (
                response.status_code >= 500 and is_refusal
            ):
                category = resolve_refusal_category(wire_body)
                explanations = wire_body.get("explanations") or []
                if not explanations:
                    single = (
                        wire_body.get("explanation")
                        or wire_body.get("error_message")
                    )
                    if single:
                        explanations = [single]
                if not explanations:
                    explanations = [f"Gate endpoint returned {response.status_code}"]
                return {
                    "decision": "block",
                    "decision_source": DecisionSource.GATEWAY,
                    "explanation": explanations[0],
                    "explanations": explanations,
                    # ADR-062 §2.2 — ``None`` for a 4xx that was
                    # never a gate refusal, a real
                    # ``DecisionCategory`` for one that was. Carried
                    # rather than re-derived so the runtime's raise
                    # site branches on the server's own classification
                    # instead of inferring one from the status code.
                    "category": category,
                    # Server-authored text. ``agent_message`` is
                    # populated by the backend only for ``denied``;
                    # its absence on the other three is the server
                    # stating "this is not the model's to read".
                    "agent_message": wire_body.get("agent_message"),
                    "user_message": wire_body.get("user_message"),
                    "reservation_id": wire_body.get("reservation_id"),
                    "remaining_budget_cents": wire_body.get("remaining_budget_cents") or 0,
                    "projected_cost_cents": wire_body.get("projected_cost_cents") or 0,
                    "policy_id": wire_body.get("policy_id"),
                    "policy_version": wire_body.get("policy_version"),
                    "operation_id": wire_body.get("operation_id"),
                    "details": wire_body.get("details") or {},
                    "error_code": wire_body.get("error_code"),
                    "status_code": response.status_code,
                }
            # 5xx after retry exhaustion -> synthetic block (legacy
            # fallback path preserved).
            if response.status_code >= 500 and on_transport_error == "raise":
                # Defence-in-depth: the helper raises 5xx-with-raise
                # inside the retry loop, but if a path slips through
                # (e.g. operator passes on_transport_error after
                # exhaustion), we still surface the typed error
                # rather than the silent synthetic block.
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
            # after retry exhaustion as ``BreakerTransportError``, but
            # ``httpx.RequestError`` can still surface when the helper
            # raises mid-loop on a non-retryable path (e.g. caller
            # passes ``max_retries=0``). Translate to either a
            # typed ``NullRunTransportError`` (opt-in) or a synthetic
            # block (legacy).
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
        except BreakerTransportError as e:
            # errors and re-raised as ``BreakerTransportError``. Apply
            # the same translation rule as ``httpx.RequestError``
            # above so the legacy ``on_transport_error`` opt-in
            # contract is preserved — opt-in → typed error, default
            # → synthetic block.
            if on_transport_error == "raise":
                raise NullRunTransportError(
                    f"Network error on /check after retry exhaustion: {e}",
                    source=TransportErrorSource.NETWORK_ERROR,
                    endpoint="check",
                ) from e
            logger.warning(f"Gate request failed after retries: {e}")
            return {
                "decision": "block",
                "decision_source": DecisionSource.FALLBACK,
                "reservation_id": None,
                "remaining_budget_cents": 0,
                "projected_cost_cents": 0,
                "explanations": [f"Gate request failed after retries: {e}"],
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
    # The v3 wire contract adds six endpoints that the /gate +
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
        on_transport_error: TransportErrorHandler | None = None,
    ) -> dict[str, Any]:
        """Pre-execution gate — wire-protocol v3.

        Targets ``/api/v1/gate`` and forwards every v3 wire field —
        ``chain_id`` ``chain_op``, ``idempotency_key``, ``stream``. This method
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
        return self.check(request, on_transport_error=on_transport_error)

    def track_single(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """POST /api/v1/track — wire-protocol v3 single-event consume.

        Runs the CONSUME_SCRIPT invariant
        ``actual_cost <= reserved_cents + epsilon_cents`` (ADR-005)
        and rejects with 422 CONSUME_OVERBUDGET on violation. The
        reserved binding is the one created by the matching
        ``/check`` call (same ``reservation_id``).

                The wire shape is built by ``runtime._build_v3_track_payload``
                (see ``runtime.py:2679-2776``); this method just forwards
                whatever dict the caller hands it. The schema is:

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
                    Parsed JSON dict from the backend's TrackResponse.
                    NOTE: there is NO top-level ``status`` field on the
                    wire — backends emit
                    ``{snapshot, actions_taken, processing_mode,
                    cost_source, confidence, event_id,
                    idempotent_replay, stored_response?}``. SDK callers
                    branch on the HTTP status (200 vs 4xx/5xx) and on
                    ``idempotent_replay`` (bool) for replay detection —
                    do NOT read ``data["status"]`` (KeyError on every
                    backend >= 3.66.2).

                Raises:
                    NullRunConsumeOverbudgetError: 422 CONSUME_OVERBUDGET —
                        ``actual_cost > reserved + epsilon_cents``. The
                        reservation is NOT silently re-reserved.
                    NullRunBackendError: 503 RESERVATION_NOT_FOUND /
                        EXECUTION_NOT_BOUND.
                    NullRunAuthenticationError: 401/403.

                 Wire contract: ``TrackRequestRaw`` is
                ``{workflow_id, tokens, cost_cents,...}``; ``execution_id``
                is replaced by ``reservation_id``, ``actual_cost_cents`` is
                replaced by ``cost_cents`` (the SDK always sends 0 — see
                ``_WIRE_STRIP_FIELDS``), and ``api_key_id`` is derived
                server-side from the request auth, not supplied by the SDK.
        """
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
                    Parsed JSON dict from the backend's CancelResponse.
                    NOTE: there is NO top-level ``status`` field on the
                    wire — backends emit
                    ``{execution_id, canceled_at, reservation_released_cents,
                    already_canceled}``. SDK callers branch on the HTTP
                    status only — do NOT read ``data["status"]``.
        """
        request: dict[str, Any] = {"execution_id": execution_id}
        if reason:
            request["reason"] = reason

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

    def consume_approval(
        self,
        approval_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """POST /api/v1/approvals/{approval_id}/consume — mark an approved
        approval row as executed.

        Closes the orphan class on the SDK success path: ``consume_approved``
        SQL is reachable from the orchestrator's Step 6 inline at
        backend/src/proxy/http/gate/orchestrator.rs:713, but
        mode="inline" tools bypass /execute entirely — leaving the
        approval row at status=APPROVED past expires_at. This
        endpoint is structurally distinct (no execution_id binding per
        ADR-046).

        The body is built by Runtime.consume_approval — it always
        carries ``organization_id`` (C2 closure) and optionally
        ``execution_id`` for audit emit only (no binding on the wire).

        Returns:
            Parsed JSON dict from the backend's ApprovalConsumeResponse
            (status ∈ {"consumed", "already_consumed", "not_approved"}).
            Idempotent on retries: already-CONSUMED rows return
            already_consumed, PENDING/DENIED/EXPIRED rows return
            not_approved.
        """
        body_bytes = _signed_request_body(body)
        headers = self._build_signed_headers(body=body_bytes)

        try:
            response = self._client.post(
                f"{self.api_url}/api/v1/approvals/{approval_id}/consume",
                content=body_bytes,
                headers=headers,
                timeout=5.0,
            )
        except httpx.RequestError as e:
            raise NullRunTransportError(
                f"Network error on /approvals/.../consume: {e}",
                source=TransportErrorSource.NETWORK_ERROR,
                endpoint="consume_approval",
            ) from e

        if response.status_code == 200:
            return response.json()  # type: ignore[no-any-return]

        raise _parse_v3_error_envelope(response, "consume_approval")

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
        *,
        organization_id: str | None = None,
        trace_id: str | None = None,
        operation_id: str | None = None,
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
                    organization_id: Organization identifier (REQUIRED on the
                        wire — ``GateRequest`` deserialization fails with
                        422 ``missing field 'organization_id'`` without it,
                        see ``backend/src/proxy/http/gate/internal.rs:156``).
                        ``Runtime.chain_end`` always passes
                        ``self.organization_id`` from ``_authenticate``.
                    trace_id: Distributed trace ID. Auto-generated UUIDv4 if
                        not provided.
                    operation_id: Idempotency key — set explicitly when the
                        caller wants the backend's per-``operation_id``
                        dedup (matches ``runtime.check_workflow_budget``
                        behaviour). Auto-generated UUIDv4 if not provided.

                Returns:
                    Parsed JSON dict (typically ``{"decision": "allow"
                    "chain_id":...}``).
        """
        # POSTed only ``{chain_id, chain_op, execution_id}`` to /gate. The
        # backend's ``GateRequest`` struct
        # (backend/src/proxy/http/gate/internal.rs:156) marks
        # ``organization_id``, ``execution_id``, ``trace_id``, ``mode`` as
        # REQUIRED — the deserializer validates them BEFORE chain_op-specific
        # dispatch, so even the ``chain_op=end`` control-plane path
        # returns 422 ``missing field 'organization_id'`` on the old
        # body. Live wire trace against api.nullrun.io confirms the
        # 422. Fix: build the same full GateRequest body every other
        # /gate caller builds (``check`` at transport.py:1481, the
        # capture site at runtime.py:1953).
        if organization_id is None:
            raise NullRunConfigError(
                "Transport.chain_end requires organization_id — "
                "NullRunRuntime.chain_end always passes it from "
                "_authenticate; a direct Transport.chain_end call must "
                "pass organization_id explicitly. The backend's GateRequest "
                "struct (backend/src/proxy/http/gate/internal.rs:156) "
                "rejects requests without organization_id with 422 "
                "VALIDATION_ERROR."
            )
        # v0.16.1 (Phase-1+ wire-shape): the backend's
        # ``backend/src/proxy/http/gate/gate.rs:148`` version-gate
        # fail-CLOSED-rejects any proto>=3 /gate call that omits
        # ``action_digest``. ``chain_end`` is a control-plane op with
        # no business_impact, so we emit the same NoImpact sentinel
        # digest that ``runtime.check_workflow_budget`` produces at
        # runtime.py:1978.
        from nullrun.business_impact import (
            BusinessImpact as _BusinessImpact,
        )
        from nullrun.business_impact import (
            compute_action_digest as _compute_action_digest,
        )

        request = {
            "organization_id": organization_id,
            # Fresh UUIDv4 per call (canonical hyphenated form so it
            # parses through the backend's ``Uuid::parse_str`` if it
            # ever reads it on this path).
            "execution_id": str(uuid.uuid4()),
            "trace_id": trace_id or str(uuid.uuid4()),
            "tool": None,
            "input": None,
            # ``mode="auto"`` — chain_end is a control-plane operation,
            # not a budget-consuming call. The orchestrator's
            # ``gate_reserve_v3`` Lua call receives ``chain_op=End``
            # and skips the budget reserve path, so the mode string
            # only needs to satisfy GateRequest's required-field check.
            "mode": "auto",
            "operation_id": operation_id or str(uuid.uuid4()),
            "chain_id": chain_id,
            "chain_op": "end",
            "action_digest": _compute_action_digest(_BusinessImpact.no_impact()),
        }
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
    # of endpoints to SDK consumers. Callers invoke
    # ``runtime.audit.list(...)`` etc. and get typed dataclasses back
    # without writing JSON parsing glue.
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
        url = f"{self.api_url}/api/v1/orgs/{organization_id}/audit-log/export/{job_id}/status"
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


def _extract_error_envelope(
    body: Any,
    raw_text: str,
) -> tuple[str, str, dict[str, Any]]:
    """Pull ``(error_code, message, details)`` from any error envelope.

    The backend emits three distinct shapes for non-2xx responses.
    This helper normalises them into the
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
    #
    # ``error_code`` is read from the TOP LEVEL first and from
    # ``details.error_code`` second. The second is not a fallback for
    # a malformed envelope — it is where a real gate refusal puts it.
    # ``GateResponse`` serialises ``error_code`` inside
    # ``details`` (``internal.rs:716``) and the backend's own status
    # mapper reads it from there (``gate.rs:88-90``), so every
    # refusal that reaches ``/execute`` or ``/gate`` carries it at
    # that nesting. Reading only the top level left ``code`` empty
    # for the whole refusal family, and the dispatcher then fell
    # through to its status-only branch — where a 403 becomes
    # ``NullRunAuthenticationError``. The observable effect was a
    # ``TOOL_BLOCKED`` policy refusal on the MCP path reported to the
    # host as a bad API key: wrong exception class, wrong
    # ``format_user_message``, and an operator action ("check your
    # credentials") that cannot possibly fix a policy decision.
    #
    # This is the same class of defect as DEF-MP-TS12-ENF-01 — a
    # decision the backend made being reported as something else —
    # and it is why ADR-064 records the nesting as load-bearing.
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

    # Shape 2: a gate refusal envelope. Same fields as the v3
    # envelope, but ``error_code`` nested under ``details`` — see the
    # note on shape 1 for why that nesting is the norm rather than an
    # edge case. Handled BEFORE the legacy slug because a refusal body
    # carries no ``error`` key, so the two cannot collide; it is
    # ordered here so a refusal is never misread as a legacy slug.
    details_raw = body.get("details")
    if isinstance(details_raw, dict) and "error_code" in details_raw:
        code = str(details_raw.get("error_code", "") or "")
        message = str(
            body.get("explanation")
            or body.get("error_message")
            or body.get("message")
            or raw_text
            or ""
        )
        details = dict(details_raw)
        for key, value in body.items():
            if key in (
                "error_code",
                "error_message",
                "explanation",
                "message",
                "details",
                "retry_after_ms",
            ):
                continue
            details.setdefault(key, value)
        return (code, message, details)

    # Shape 3: legacy slug. ``error`` is the slug,
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

    The parse failure is wrapped in NullRunTransportError with a
    stable ``error_code`` so callers can ``except`` cleanly and the
    user sees a short NullRun-family message instead of a Python
    traceback.

    ``body_preview`` is intentionally truncated to 200 chars and the
    raw ``JSONDecodeError.lineno/colno`` are NOT included in the
    surfaced message -- both are info-leak surface (line numbers
    hint at response shape; partial body may carry PII like
    organization_id fragments).
    """
    try:
        return response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise NullRunTransportError(
            f"Received malformed JSON from {endpoint} "
            f"(status={response.status_code}): {type(exc).__name__}",
            source=TransportErrorSource.GATEWAY_ERROR,
            endpoint=endpoint,
            # NR-T001 collides with NullRunToolBlockedError's
            # canonical code (breaker/exceptions.py:955); using
            # NR-T-PARSE here so a cookbook handler that branches
            # on `exc.error_code == "NR-T001"` does not mis-classify
            # a JSON parse failure as a tool block. See
            # tests/test_2026_08_11_fixes.py for the pin.
            error_code="NR-T-PARSE",
        ) from exc


def _parse_v3_error_envelope(
    response: httpx.Response,
    endpoint: str,
) -> Exception:
    """Translate a non-2xx response, stamping the refusal category on.

    Thin wrapper around [`_parse_v3_error_envelope_uncategorised`]
    that attaches ``wire_category`` to whatever exception comes back.
    The wrapper exists because the function has ~15 return points and
    threading the attribute through each one would be a change with
    no behaviour in it — the property being added is "every exception
    built from a gate refusal knows what kind of refusal it was".
    """
    exc = _parse_v3_error_envelope_uncategorised(response, endpoint)
    try:
        body = response.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        # ADR-062 §2.2. A refusal whose category is ABSENT is left
        # absent, not defaulted: the runtime's strict rule reads the
        # missing value as unclassifiable, which is the honest
        # reading. Defaulting to `infra` here would quietly convert a
        # missing field into a confident answer.
        category = body.get("category")
        if category is not None:
            exc.wire_category = category  # type: ignore[attr-defined]
        exc.wire_error_code = (  # type: ignore[attr-defined]
            (body.get("details") or {}).get("error_code")
            if isinstance(body.get("details"), dict)
            else body.get("error_code")
        )
        # Server-authored text, carried the same way ``check()``
        # carries it on the dict it returns. ADR-063 §1.3(e):
        # ``agent_message`` is the ONLY text the backend certifies
        # as safe for a model — no store name, no schema, no wire
        # code, no policy vocabulary. Inventing a substitute when it
        # is absent would put exactly the content the leak guard
        # exists to keep off the model's plate onto it, so an
        # absent ``agent_message`` is left absent and the raise
        # site's own catalogue text applies.
        exc.agent_message = (  # type: ignore[attr-defined]
            body.get("agent_message")
        )
        exc.user_message = body.get("user_message")  # type: ignore[attr-defined]
    return exc


def _parse_v3_error_envelope_uncategorised(
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

    DEF-NR-TOOLBLOCKED-PARSER: the ``catalog is
    NullRunToolBlockedError or catalog is NullRunBlockedException``
    arm further down is the dedicated branch for the typed v3
    envelope. It used to be documented by a comment inside
    ``Transport.check``, which never had such a branch — the tag was
    filed against the wrong function for as long as it existed.
    Re-filed here, on the function that actually holds the code.
    """
    # Lazy imports: the exception classes import the transport
    # types (TransportErrorSource), so a top-level import here
    # would create a cycle. The price is one extra import
    # non-2xx response — irrelevant for the failure path.
    from nullrun.breaker.exceptions import (
        NullRunAuthError,
        NullRunBackendError,
        NullRunBlockedException,
        NullRunBudgetError,
        NullRunBudgetRecheckFailedError,
        NullRunChainError,
        NullRunConsumeOverbudgetError,
        NullRunProtocolError,
        NullRunRateLimitRedisError,
        NullRunToolBlockedError,
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
        if catalog is NullRunExecutionNotFoundError:
            # read ``execution_id`` / ``endpoint`` / ``regate_required``
            # off the exception without indexing into ``details``.
            # Mirrors the ``NullRunBackendError`` branch above (the
            # parent class) but also forwards ``execution_id`` from
            # the wire envelope. Without this branch the generic
            # catalog fallback at line ~2615 would discard the
            # ``execution_id`` field (it filters ``**details`` to
            # the base NullRunError kwargs only).
            return NullRunExecutionNotFoundError(
                full_message,
                execution_id=details.get("execution_id"),
                endpoint=details.get("endpoint") or endpoint,
                status_code=status,  # 404 per backend mapping
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
        if (
            catalog is NullRunToolBlockedError
            or catalog is NullRunBlockedException
        ):
            # subclasses require positional ``workflow_id`` + ``reason``
            # (no defaults), so the generic ``catalog(full_message, ...)``
            # fallback below raises TypeError when given a string for
            # ``workflow_id``. Affects 7 catalog entries: TOOL_BLOCKED,
            # LOOP_DETECTED, MODEL_REQUIRED, POLICY_UNCONFIGURED,
            # TOO_MANY_PENDING_APPROVALS, BUSINESS_IMPACT_INVALID,
            # VALIDATION_FAILED. Pre-fix the TypeError escaped the parser
            # and got swallowed by the catch-all ``except Exception: pass``
            # in Transport.execute, surfacing the synthetic-block dict
            # ``{"decision": "block", "explanation": "Gateway returned
            # 403"}`` instead of the typed NR-T001 / NR-Lxxx catalog line.
            # ``tool_name`` is forwarded for NullRunToolBlockedError
            # (the only BlockedException subclass that surfaces it on the
            # wire envelope); the parent constructor drops it for plain
            # NullRunBlockedException so it's a no-op there. ``forwarded``
            # (error_code / user_action / retryable / docs_url / cause) is
            # passed through so the catalog value's defaults win.
            instance = catalog(  # type: ignore[call-arg]
                workflow_id=str(details.get("workflow_id") or "unknown"),
                reason=full_message,
                status_code=status,
                tool_name=details.get("tool_name"),
                **forwarded,
            )
            return cast(Exception, instance)
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
# (rather than at the top of transport.py) avoids breaking the
# frozen ``_parse_error_envelope`` test contract.
def _build_v3_error_code_map() -> dict[str, type[Exception]]:
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
        NullRunBudgetRecheckFailedError,
        NullRunChainError,
        NullRunConsumeOverbudgetError,
        NullRunExecutionNotFoundError,
        NullRunProtocolError,
        NullRunRateLimitRedisError,
        NullRunToolBlockedError,
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
        # DEF-TC4-001 (2026-10-02). These two are registered in the
        # backend's `GateErrorCode::all()` (error_codes.rs:666-667,
        # `BudgetWorkflowBlocked` / `BudgetCacheExceeded`, both 402)
        # and the backend logged `BUDGET_WORKFLOW_BLOCKED` ×389 in
        # production before they were registered at all. The SDK
        # catalog was never updated to match, so the typed dispatcher
        # could not classify them: `BUDGET_WORKFLOW_BLOCKED` fell to
        # the base-class drift tier and a caller branching on
        # `NullRunBudgetError` to mean "stop spending" saw an
        # untyped block instead.
        #
        # Drift between the two registries is exactly what
        # `test_unknown_wire_code_falls_back_to_base` exists to
        # surface — this pair is the reason that test matters, and
        # the reason the catalog has to be checked when a code is
        # registered backend-side.
        "BUDGET_WORKFLOW_BLOCKED": NullRunBudgetError,
        "BUDGET_CACHE_EXCEEDED": NullRunBudgetError,
        # Note: BUDGET_REDIS_UNAVAILABLE and RATE_LIMIT_REDIS_UNAVAILABLE
        # because the backend never emits it (it is absent from
        # ``GateErrorCode::all()`` in error_codes.rs). A cookbook that
        # extends this map with the legacy slug risks silently matching
        # nothing, so the slot stays unoccupied by design.
        # 402 — chain family (separate class for diagnostic clarity)
        "CHAIN_MAX_DURATION_EXCEEDED": NullRunChainError,
        # 403 — chain security + workflow state
        "CHAIN_CROSS_ORG": NullRunChainError,
        "CHAIN_ORG_MISMATCH": NullRunChainError,
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
        # typed ``NullRunApprovalDbUnavailableError`` (NR-A016) so
        # cookbook code can branch on the typed class instead of
        # falling through to the base NullRunBlockedException).
        "APPROVAL_DB_UNAVAILABLE": NullRunApprovalDbUnavailableError,
        "APPROVAL_PERSISTENCE_FAILED": NullRunApprovalDbUnavailableError,
        "APPROVAL_VALIDATION_FAILED": NullRunApprovalDbUnavailableError,
        "APPROVAL_CONFLICT": NullRunApprovalDbUnavailableError,
        "APPROVAL_NOT_FOUND": NullRunApprovalDbUnavailableError,
        "APPROVAL_CREATE_FAILED": NullRunApprovalDbUnavailableError,
        # A-1+A-2 bundle. Distinct from the /gate create-failure
        # family above: these are the seven distinct outcomes that
        # the backend's ``gate_internal()`` returns on /execute
        # post-approval grant-consume (see
        # ``backend/src/proxy/http/gate/internal.rs:3059-3108,
        # 3115-3138``). Each maps to a typed exception
        # (NR-A010..NR-A015) so cookbook recipes can branch on the
        # precise outcome (e.g. ``except
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
        # Distinct from BUDGET_HARD_BLOCKED: the operator explicitly
        # approved the grant at /gate, but the period-bound counter
        # moved between /gate and /execute (another concurrent
        # execution spent the budget). Caller should re-/gate to
        # refresh the reservation envelope and retry /execute.
        # Backed by GateErrorCode::BudgetRecheckFailed in the
        # backend (error_codes.rs).
        # ``_v3_error_dispatch`` (line ~2477) already routes this to
        # ``NullRunBudgetRecheckFailedError`` (NR-B006) before the
        # catalog fallback — defense-in-depth, this catalog entry
        # now matches the dispatcher.
        "BUDGET_RECHECK_FAILED": NullRunBudgetRecheckFailedError,
        # from the SDK map and caused cookbook recipes that branch on
        # ``error_code`` to fall through to ``NullRunBackendError``.
        # block grouped so the parity CI test
        # ``backend/tests/nr007_sdk_error_code_parity.rs`` has a
        # single regression pin surface. Family mapping rationale
        # per code:
        #   - budget family: NullRunBudgetError
        #   - chain family: NullRunChainError
        #   - auth binding: NullRunAuthError
        #   - protocol / wire validation: NullRunProtocolError /
        #     NullRunBackendError
        #   - gate decision: NullRunBlockedException /
        #     NullRunToolBlockedError (TOOL_BLOCKED MUST use the
        #     dedicated class per CLAUDE.md §8 — operators expect
        #     ``except NullRunToolBlockedError:`` for tool-name
        #     branch recipes).
        "BUDGET_ANTI_DOS_RESERVED_CAP": NullRunBudgetError,
        "BUDGET_REDIS_UNAVAILABLE": NullRunBudgetError,
        "CHAIN_ID_INVALID": NullRunChainError,
        "EXECUTION_KEY_MISMATCH": NullRunAuthError,
        "EXECUTION_ORG_MISMATCH": NullRunAuthError,
        "ORG_MISMATCH": NullRunAuthError,
        "PROTOCOL_HEADER_INVALID": NullRunProtocolError,
        "PROTOCOL_HEADER_REQUIRED": NullRunProtocolError,
        "TOOL_BLOCKED": NullRunToolBlockedError,
        "LOOP_DETECTED": NullRunBlockedException,
        "MODEL_REQUIRED": NullRunBlockedException,
        "POLICY_UNCONFIGURED": NullRunBlockedException,
        "TOO_MANY_PENDING_APPROVALS": NullRunBlockedException,
        "BUSINESS_IMPACT_INVALID": NullRunBlockedException,
        "VALIDATION_FAILED": NullRunBlockedException,
        # ``NullRunMcp*Error`` subclasses so cookbook code can branch
        # on the precise umbrella path. Pre-B.1 these collapsed to
        # the generic NullRunBlockedException / NR-X001 fallback —
        # operators couldn't distinguish the destructive-MCP block
        # from the readonly-bypass block from the approval-required
        # path. ADR-013 marked the umbrella frozen-dormant: wire
        # codes are reserved and the SDK must round-trip them, but
        # the underlying mechanisms aren't wired in production yet.
        "MCP_DESTRUCTIVE_BLOCKED": NullRunMcpDestructiveBlockedError,
        "MCP_READONLY_BYPASS_BLOCKED": NullRunMcpReadonlyBypassBlockedError,
        "MCP_APPROVAL_REQUIRED": NullRunMcpApprovalRequiredError,
        # Wire-level parsing failures (missing / malformed fields).
        # Map to ``NullRunBackendError`` because the SDK treats them
        # as infrastructure-side issues — the server should have
        # returned a structured 4xx envelope, and a fall-through
        # here indicates a wire-shape drift between client and server.
        "EXECUTION_ID_MALFORMED": NullRunBackendError,
        "EXECUTION_ID_REQUIRED": NullRunBackendError,
        # emitted by the backend as a typed envelope at
        # ``cancel.rs:142-149`` and ``orchestrator.rs:1327-1334`` —
        # round-trips through the canonical ``v3_error_envelope``
        # helper, so the wire string is canonical. Map to
        # ``NullRunBackendError`` (sibling to the EXECUTION_ID_*
        # siblings above) — wire-shape drift guard.
        "INVALID_EXECUTION_ID": NullRunBackendError,
        # emitted by the backend as a typed envelope at
        # routing through ``v3_error_envelope`` + the new
        # ``GateErrorCode::ExecutionNotFound`` variant). Map to the
        # dedicated ``NullRunExecutionNotFoundError`` (NR-EX01) so
        # cookbook code can ``except
        # NullRunExecutionNotFoundError`` to distinguish a missed
        # /gate (re-issue /gate then retry /execute) from generic
        # wire-shape drift.
        "EXECUTION_NOT_FOUND": NullRunExecutionNotFoundError,
        # ``From<JsonRejection> for ApiError`` impl routes the two
        # parse-level rejections to distinct wire codes:
        # - ``INVALID_FIELD`` (422 + ``invalid_field`` slug via
        #   ``ErrorSlug::ValidationFailed``) for axum
        #   ``JsonDataError`` — body parsed but a field failed
        #   schema validation.
        # - ``INVALID_JSON`` (400 + ``invalid_json`` slug via
        #   ``ErrorSlug::InvalidJson``) for axum
        #   ``JsonSyntaxError`` — the body isn't parseable as
        #   JSON at all (truncated, malformed braces, unescaped
        #   control chars).
        # Both are emitted on /gate, /execute, and /track (the
        # /track side has always used the typed 3-way split
        # via ``TrackError::WithBody``). Both map to
        # ``NullRunBackendError`` because the SDK treats
        # parse-level rejections as "the server couldn't make
        # sense of your body" infrastructure-side issues —
        # cookbook recipes that branch on these codes (vs the
        # generic ``VALIDATION_FAILED`` collapse) get the
        "INVALID_FIELD": NullRunBackendError,
        "INVALID_JSON": NullRunBackendError,
        # Rate-limit plan lookup failure (Postgres / Redis adjacent).
        # Tied to ``NullRunRateLimitRedisError`` because the failure
        # mode is rate-limit-specific infrastructure unavailability
        # rather than generic backend error.
        "RATE_LIMIT_PLAN_LOOKUP_FAILED": NullRunRateLimitRedisError,
        # Idempotency layer Redis unavailability. Map to generic
        # ``NullRunBackendError`` — the wire class is infrastructure
        # availability, not a typed subclass (mirrors
        # ``RATE_LIMIT_REDIS_UNAVAILABLE`` -> ``NullRunRateLimitRedisError``
        # family pattern at wire level).
        "IDEMPOTENCY_REDIS_UNAVAILABLE": NullRunBackendError,
        # Execution Graph / ADR-036 (sub-agent spawn topology). Backend
        # error_codes.rs:107-382 covers six codes in this family — three
        # 422 semantic rejects (cycle / depth / parent-binding) and three
        # 503 infrastructure failures (depth lookup / invoke persist /
        # subworkflow disabled). Map to ``NullRunChainError`` because
        # the existing class already carries `parent_execution_id` per
        # Execution Graph v0 docstring at `exceptions.py:388-410`. Adding
        # them under a fresh ``NullRunSubworkflowError`` would force
        # cookbook code to import a new exception class for the same
        # lineage concept; consolidate under ChainError instead.
        "WORKFLOW_CYCLE_DETECTED": NullRunChainError,
        "WORKFLOW_DEPTH_EXCEEDED": NullRunChainError,
        "WORKFLOW_PARENT_BINDING_EXPIRED": NullRunChainError,
        "WORKFLOW_DEPTH_LOOKUP_FAILED": NullRunChainError,
        "INVOKE_PERSIST_FAILED": NullRunBackendError,
        "SUBWORKFLOW_INVOKE_DISABLED": NullRunChainError,
        # ADR-023 (post-approval re-check race): a second operator
        # already decided on the same approval row before this call's
        # re-check landed. Map to ``NullRunApprovalReplayRejectedError``
        # because semantically the agent caller has the same retry-loop
        # concern as a replay-rejected approval (CLAUDE.md §34c).
        "APPROVAL_ALREADY_DECIDED": NullRunApprovalReplayRejectedError,
        # ADR-023 (Phase-1+ wire-shape fail-CLOSED): a v3+ SDK hit /gate
        # without ``action_digest`` (legacy anchor attempt). Map to
        # ``NullRunBlockedException`` because the wire shape is a true
        # block decision, not an infrastructure error — cookbook code
        # branches on the action_digest missing path with the same
        # `except NullRunBlockedException:` flow as TOOL_BLOCKED.
        "LEGACY_GRANT_REJECTED": NullRunBlockedException,
    }


_V3_ERROR_CODE_MAP: dict[str, type[Exception]] = _build_v3_error_code_map()


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

    **Test-only helper:** no live wire path uses this. See the
    comment block above.
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
    "is_fallback_decision_source",
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
