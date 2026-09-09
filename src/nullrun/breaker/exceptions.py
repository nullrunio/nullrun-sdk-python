from enum import Enum
from typing import Any


class BreakerError(Exception):
    """Base exception for Breaker SDK."""

    pass


# ---------------------------------------------------------------------------
# Structured error base (Layer 1 of the "give the user a chance" design)
# ---------------------------------------------------------------------------
# Pre-Layer-1: every SDK exception was a plain ``Exception`` with a free-form
# ``message``. Users got the same string for "you forgot api_key" and
# "backend is on fire" — no machine-readable code, no next-step hint, no
# retryable flag. Cookbook examples had to grep the message for keywords.
#
# Post-Layer-1: every public SDK exception inherits from ``NullRunError``
# and carries four structured fields:
#
# * ``error_code`` — stable, grep-able identifier (e.g. ``"NR-A001"``).
# Documented in ``docs/errors/<code>.md`` and
# available to telemetry / Sentry / dashboards.
# * ``user_action`` — short, imperative sentence telling the user what
# to do next ("Set NULLRUN_API_KEY env var"
# "Verify API key at https:/app.nullrun.io/..."
# "Retry in 30s, backend is down"). Empty when
# there is no actionable step.
# * ``retryable`` — ``True`` when a retry after a backoff is the
# correct response (5xx, network blip, transient
# auth). ``False`` for config / permission /
# budget-exhausted — retrying without changing
# something will just hit the same wall.
# * ``docs_url`` — link to the per-code docs page. Always set; falls
# back to ``https:/docs.nullrun.io/errors`` when
# the per-code page does not exist yet.
#
# Existing ``except`` clauses keep working: every existing public class
# (``NullRunAuthenticationError``, ``NullRunBlockedException``
# ``NullRunTransportError``, ``WorkflowKilledException``
# ``WorkflowPausedException``) inherits from ``NullRunError`` now, so
# ``except NullRunError:`` catches them all — but the narrower clauses
# keep matching too.
#
# New specialized classes (``NullRunConfigError``, ``NullRunAuthError``
# ``NullRunBackendError``, ``NullRunBudgetError``, ``NullRunToolBlockedError``)
# are added below. They are subclasses of the existing user-facing
# classes where it makes sense (e.g. ``NullRunBudgetError`` is a subclass
# of ``NullRunBlockedException``) so existing handlers still match.
class NullRunError(BreakerError):
    """Structured base for every user-facing SDK exception.

    Carries the four fields that make an exception actionable
    (``error_code``, ``user_action``, ``retryable``, ``docs_url``)
    plus the optional ``cause`` (chained original exception). Every
    subclass populates at least ``error_code``; ``user_action`` is
    empty only when there is genuinely nothing to suggest (e.g. an
    internal sanity check).

    Two intermediate marker subclasses split the public hierarchy by
    category so host code can ``except`` on the category without
    enumerating individual codes:

    *:class:`NullRunDecision` — expected policy outcomes (budget
      cap, tool block, rate limit, loop detection, workflow pause).
      The enforcement layer is doing its job; the UX is "what
      happened" + (where applicable) "how to proceed".
    *:class:`NullRunInfrastructureError` — system failures (network
      backend 5xx, auth rejection, config error). The SDK could not
      reach or query the policy engine; the UX is a generic
      "service unavailable" with operator triage info.

    Both inherit from:class:`NullRunError`, so existing
    ``except NullRunError:`` clauses keep matching — the split is a
    strict refinement, not a breaking change. ``WorkflowKilledInterrupt``
    is **not** in either category: it remains a ``BaseException``
    subclass so kill signals bypass any ``except Exception:`` that
    might otherwise swallow them.
    """

    # Default error code when a subclass does not override it.
    # Real codes are ``"NR-LETTERNNN"`` — see the catalog at the top
    # of the docstring above.
    error_code: str = "NR-0000"

    # Short imperative next-step hint shown in tracebacks and
    # surfaced by the cookbook example. Empty string means "no
    # actionable step beyond what the message says".
    user_action: str = ""

    # ``True`` only when a retry after a backoff is the correct
    # response (5xx, network blip, transient auth). Default is
    # ``False`` because the common case is "user must change
    # something before retrying makes sense".
    retryable: bool = False

    # Per-code docs page. Fallback to the index when the per-code
    # page does not exist yet — the docs site is responsible for
    # the 404 page, not the SDK.
    docs_url: str = "https://docs.nullrun.io/errors"

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        user_action: str | None = None,
        retryable: bool | None = None,
        docs_url: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        # Apply per-instance overrides, falling back to the class
        # attribute. We intentionally do NOT mutate the class attribute
        # — each instance must own its own fields so a subclass
        # override (e.g. ``NullRunBackendError.retryable = True``)
        # does not leak across other subclasses.
        if error_code is not None:
            self.error_code = error_code
        if user_action is not None:
            self.user_action = user_action
        if retryable is not None:
            self.retryable = retryable
        if docs_url is not None:
            self.docs_url = docs_url
        # ``cause`` is the chained original exception, mirroring
        # ``raise X from e``. We store it on the instance so the
        # cookbook ``except`` handlers and the on_error hook
        # (Layer 2) can introspect it without parsing ``__cause__``.
        if cause is not None:
            self.cause = cause
            # Mirror Python's `raise... from` behaviour so ``str(exc)``
            # shows the chain ("The above exception was the direct
            # cause of the following exception"). Skipped when the
            # caller already chained via `from` — ``__cause__`` is
            # then set automatically and we just stash the reference
            # for structured access.
            if getattr(self, "__cause__", None) is None:
                self.__cause__ = cause
        super().__init__(message)


# ---------------------------------------------------------------------------
# Category marker classes
# ---------------------------------------------------------------------------
# These two classes split the NullRunError hierarchy by what kind of
# event the exception represents. They are pure markers — no new fields
# no constructor changes. Host code can use them as the catch-all for
# a category without enumerating individual codes:
#
# try:
#     ...
# except NullRunDecision as d:
#     # Budget, tool block, rate limit, loop, pause — expected
#     return d.user_action_or_message()
# except NullRunInfrastructureError as e:
#     # Network, 5xx, auth, config — system failure
#     sentry.capture_exception(e)
#     return "service unavailable"
#
# Both inherit from NullRunError so ``except NullRunError:`` keeps
# matching existing handlers — the split is additive.
class NullRunDecision(NullRunError):
    """Marker for expected policy outcomes.

    Includes budget caps, tool blocks, rate limits, loop detection
    workflow pause, and the generic block fallback. These are NOT
    system failures — the enforcement layer reached a deliberate
    decision. UX should explain the decision and (where applicable)
    offer an upgrade or alternative action.

    End-user messaging for these exceptions is stable per ``error_code``
    (see:mod:`nullrun.messages`) and rarely needs to mention the
    decision mechanism.
    """


class NullRunInfrastructureError(NullRunError):
    """Marker for system failures (operator-facing).

    Includes network errors reaching the policy engine, gateway 5xx
    authentication rejections, and configuration errors. End users see
    a generic "service unavailable" message; operators see the
    structured fields for triage (``error_code``, ``retryable``, and
    for transport errors, ``source`` / ``endpoint``).

    Host integrations (FastAPI middleware, Slack handler, etc.)
    typically map these to HTTP 503 / 502 / 500 — NOT to 4xx, because
    the failure is on our side, not the user's.
    """


# ---------------------------------------------------------------------------
# Transport / network failures
# ---------------------------------------------------------------------------
class TransportErrorSource(str, Enum):
    """Where a transport failure originated.

    Surfaces the failure classification up to the caller so the
    `decision_source` audit trail can distinguish "server said
    block" from "server did not respond" — see ADR-008 for the full
    rationale.

    These values also flow through `decision_source` on
    `execute` / `check` return dicts when the transport layer
    degrades to a fallback instead of raising.
    """

    NETWORK_ERROR = "NETWORK_ERROR"  # httpx.ConnectError, timeout, DNS
    GATEWAY_ERROR = "GATEWAY_ERROR"  # 5xx from the gateway
    BREAKER_OPEN = "BREAKER_OPEN"  # circuit breaker tripped
    AUTH_ERROR = "AUTH_ERROR"  # 401 / 403 from the gateway


class NullRunTransportError(NullRunInfrastructureError):
    """Raised by transport layer when the policy engine is unreachable.

    The exception carries a `source` (TransportErrorSource) and the
    `endpoint` that failed, so callers can implement endpoint-specific
    recovery (e.g. fail-CLOSED for sensitive tools, fail-OPEN for
    budget pre-checks) per ADR-008.

    Replaces the previous behavior of swallowing the failure and
    returning a synthetic `allow` / `block` response — that hid
    the policy-engine outage from operators and was the root cause
    of bug #1 / #2 fixed in ADR-008.

    Inherits from:class:`NullRunError` (Layer 1) so every transport
    failure carries an ``error_code`` and ``user_action`` — see
:class:`NullRunBackendError` for the most common 5xx case.
    """

    error_code = "NR-B001"  # default; subclasses override
    user_action = (
        "Check connectivity to the NullRun backend. If the backend is "
        "up, retry the request — transport errors are usually transient."
    )
    retryable = True

    def __init__(
        self,
        message: str,
        source: TransportErrorSource,
        endpoint: str,
        **details: Any,
    ) -> None:
        self.source = source
        self.endpoint = endpoint
        self.details = details
        # Map the transport-source classification to a per-class
        # ``error_code`` when the caller does not override it via
        # ``**details``. NETWORK_ERROR / GATEWAY_ERROR are the two
        # common paths; the others (BREAKER_OPEN, AUTH_ERROR) are
        # kept as the default ``NR-B001`` because they signal SDK-
        # internal state, not the backend.
        _CODE_BY_SOURCE = {
            TransportErrorSource.NETWORK_ERROR: "NR-B001",
            TransportErrorSource.GATEWAY_ERROR: "NR-B002",
            TransportErrorSource.AUTH_ERROR: "NR-A003",
            TransportErrorSource.BREAKER_OPEN: "NR-B005",
        }
        # Precedence: explicit ``error_code=`` in details wins, then
        # the class's own ``error_code`` (which subclasses like
        # ``RateLimitError`` override to opt out of the source
        # mapping — 429 is not a gateway error), then the source
        # mapping (which only applies when the class still uses the
        # parent's ``"NR-B001"`` default).
        _PARENT_DEFAULT_CODE = "NR-B001"
        if type(self).error_code != _PARENT_DEFAULT_CODE:
            # Subclass overrode the default — honor it.
            code = details.pop("error_code", None) or type(self).error_code
        else:
            code = details.pop("error_code", None) or _CODE_BY_SOURCE.get(
                source, _PARENT_DEFAULT_CODE
            )
        # Only forward the structured fields the base class accepts —
        # arbitrary ``**details`` like ``status_code`` must NOT leak
        # into ``NullRunError.__init__`` (which has a fixed kwarg
        # signature). Non-structured details stay on ``self.details``
        # for the message string and for inspection.
        super().__init__(
            f"Transport error on {endpoint}: {message} (source={source.value}, details={details})",
            error_code=code,
        )


class NullRunBackendError(NullRunTransportError):
    """5xx from the NullRun backend. Retryable.

    Subclass of:class:`NullRunTransportError` so existing
    ``except NullRunTransportError:`` handlers keep matching.
    Adds a specific ``error_code`` and a retry hint.
    """

    error_code = "NR-B002"
    user_action = (
        "The NullRun backend returned a server error. This is usually "
        "transient — retry after a few seconds. If it persists for more "
        "than a minute, check https://status.nullrun.io or contact support."
    )
    retryable = True

    def __init__(
        self,
        message: str,
        endpoint: str,
        status_code: int | None = None,
        **details: Any,
    ) -> None:
        details.setdefault("status_code", status_code)
        super().__init__(
            message,
            source=TransportErrorSource.GATEWAY_ERROR,
            endpoint=endpoint,
            **details,
        )


class RateLimitError(NullRunTransportError):
    """Raised when the gateway returns HTTP 429 with a ``Retry-After``
    header (or JSON body field).

    Subclass of ``NullRunTransportError`` so
    ``except NullRunTransportError`` keeps catching it. Surfaces
    ``retry_after`` (seconds) and ``upgrade_url`` so callers can
    schedule a retry or surface a billing upgrade prompt.

    Attributes:
        retry_after: Seconds the server asks the client to wait
            before retrying. ``None`` when no ``Retry-After`` header.
        upgrade_url: Plan-upgrade URL from the 429 body. ``None``
            when the response did not include one.
        body: Parsed JSON body (gateway's ``error`` / ``message``).
    """

    error_code = "NR-R001"
    user_action = (
        "The NullRun backend rate-limited this API key. Wait "
        "``retry_after`` seconds (or upgrade the plan) before retrying."
    )
    retryable = True

    def __init__(
        self,
        message: str,
        source: TransportErrorSource,
        endpoint: str,
        retry_after: float | None = None,
        upgrade_url: str | None = None,
        body: dict[str, Any] | None = None,
        **details: Any,
    ) -> None:
        self.retry_after = retry_after
        self.upgrade_url = upgrade_url
        self.body = body or {}
        if retry_after is not None:
            details.setdefault("retry_after", retry_after)
        if upgrade_url is not None:
            details.setdefault("upgrade_url", upgrade_url)
        super().__init__(message, source, endpoint, **details)


# ---------------------------------------------------------------------------


class NullRunProtocolError(NullRunInfrastructureError):
    """Wire-protocol version mismatch.

    Raised when the backend rejects the SDK's ``X-NULLRUN-PROTOCOL``
    header as either too old (``PROTOCOL_TOO_OLD`` — server is newer
    than the SDK) or too new (``PROTOCOL_TOO_NEW`` — SDK is newer
    than the server). The actionable fix is to upgrade the SDK
    (too old) or wait for the backend to roll out the new wire
    version (too new).
    """

    error_code = "NR-P001"
    user_action = (
        "The NullRun backend rejected the SDK's wire-protocol version. "
        "Upgrade the SDK to a version that supports protocol "
        "X-NULLRUN-PROTOCOL: 4 — see "
        "https://docs.nullrun.io/reference/wire-protocol for the "
        "current compatibility matrix."
    )
    retryable = False


class NullRunChainError(NullRunDecision):
    """Chain-related failure.

    Covers backend codes: ``CHAIN_MAX_DURATION_EXCEEDED`` (402),
    ``CHAIN_CROSS_ORG`` (403), ``CHAIN_ORG_MISMATCH`` (403),
    ``CHAIN_NOT_FOUND`` / ``CHAIN_EXPIRED`` (404), and the
    Execution Graph v0 (2026-08-06) trio:
    ``PARENT_EXECUTION_NOT_FOUND`` / ``PARENT_EXECUTION_ORG_MISMATCH``
    / ``PARENT_EXECUTION_KEY_MISMATCH`` (all 403). Splitting the
    chain-and-lineage codes into their own class (rather than reusing
    NullRunBlockedException) gives cookbook code a clean way to
    distinguish "you forgot to start a chain" from "your tool is
    blocked" from "your sub-agent references an execution you do not
    own" without string-matching the message.

    Attributes:
        chain_id: Chain that triggered the error (may be None on a
            cross-org collision).
        parent_execution_id: Execution Graph v0 (2026-08-06) — the
            parent execution_id from the rejected sub-agent call.
            Distinct from chain_id (lifecycle of one SDK run) — the
            Execution Graph tracks spawn topology across runs.
    """

    error_code = "NR-CH001"
    user_action = (
        "The chain context is invalid. Verify chain_id is a UUID v4 "
        "you started with chain_op='start', that it belongs to the "
        "same org as the API key, and that it has not exceeded its "
        "max_duration. See https://docs.nullrun.io/concepts/chains."
    )
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        chain_id: str | None = None,
        parent_execution_id: str | None = None,
        backend_code: str | None = None,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
        **kwargs: Any,
    ) -> None:
        self.chain_id = chain_id
        # Execution Graph v0 (2026-08-06): when the backend rejects
        self.parent_execution_id = parent_execution_id
        self.backend_code = backend_code or self.error_code
        self.details = details or {}
        # 2026-07-04: preserve the wire HTTP
        self.status_code = status_code
        super().__init__(message, **kwargs)


class NullRunConsumeOverbudgetError(NullRunDecision):
    """``actual_cost > reserved + epsilon_cents``.

    The CONSUME_SCRIPT v3 invariant fires when the per-call actual
    cost exceeds the per-execution reservation by more than the
    configured ``epsilon_cents`` (default 1 cent). The reservation
    is NOT silently re-reserved — the caller MUST reconcile the
    delta manually before retrying. This is the fix to a class
    of "implicit re-reserve = bypass enforcement" attacks where a
    malicious SDK would reserve 1 cent, then report 1000 cents on
    the consume path.

    Attributes:
        execution_id: Server-minted id from the matching /check.
        reserved_cents: What the gate reserved (the binding ceiling).
        max_allowed_cents: ``reserved + epsilon_cents`` — the actual
            hard ceiling that was violated.
        actual_cost_cents: What the caller tried to consume (the
            rejected value).
        epsilon_cents: The configured tolerance (default 1).
    """

    error_code = "NR-O001"
    user_action = (
        "The actual cost exceeded the reservation by more than the "
        "epsilon_cents tolerance. The reservation was NOT silently "
        "re-reserved. Either reduce the call's "
        "expected cost before /check (model downgrade, fewer tokens) "
        "or increase the per-policy ``epsilon_cents`` after manual "
        "review — never bypass the invariant by retrying."
    )
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        execution_id: str | None = None,
        reserved_cents: int | None = None,
        max_allowed_cents: int | None = None,
        actual_cost_cents: int | None = None,
        epsilon_cents: int | None = None,
        status_code: int | None = None,
        **kwargs: Any,
    ) -> None:
        self.execution_id = execution_id
        self.reserved_cents = reserved_cents
        self.max_allowed_cents = max_allowed_cents
        self.actual_cost_cents = actual_cost_cents
        self.epsilon_cents = epsilon_cents
        # 2026-07-04: CONSUME_OVERBUDGET maps to
        self.status_code = status_code
        super().__init__(message, **kwargs)


class NullRunWorkflowInactiveError(NullRunDecision):
    """Workflow soft-deleted; gate blocks per-key traffic.

    Raised when the workflow's ``is_active`` flag is false (soft
    delete + ``killed_at`` not null) AND an active API key still
    tries to drive traffic against it. Per the fail-CLOSED contract,
    the SDK must not let the agent body run in
    this state — a soft-deleted workflow implies the operator
    intentionally revoked it.
    """

    error_code = "NR-W004"
    user_action = (
        "The workflow is soft-deleted or killed on the server. "
        "Stop sending traffic against this workflow — restore it "
        "via the dashboard at https://app.nullrun.io/workflows/ "
        "before retrying. Existing reservations are returned to "
        "the org's available budget via the /cancel path or by "
        "the per-execution reservation TTL (300s)."
    )
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        workflow_id: str | None = None,
        status_code: int | None = None,
        **kwargs: Any,
    ) -> None:
        self.workflow_id = workflow_id
        # 2026-07-04: WORKFLOW_INACTIVE maps to
        self.status_code = status_code
        super().__init__(message, **kwargs)


class NullRunRateLimitRedisError(NullRunInfrastructureError):
    """Redis unavailable for the aggregate per-org rate limit
.

    Fail-CLOSED per the enforcement table — aggregate rate
    limiting is the authoritative gate, so a Redis outage maps to
    503, not to a silent allow. Per-key rate limits stay
    fail-OPEN because budget enforcement is the authoritative
    backstop there.
    """

    error_code = "NR-R002"
    user_action = (
        "The NullRun backend cannot reach Redis for the aggregate "
        "rate limit. The request was rejected (fail-CLOSED) because "
        "the rate limit is the authoritative gate, not a soft "
        "advisory. Retry after the operator confirms Redis is "
        "healthy — check status.nullrun.io."
    )
    retryable = True


class BreakerTransportError(BreakerError):
    """
    Raised when transport layer fails and events cannot be delivered.

    This exception indicates a critical failure in the transport layer where
    events are being dropped after exceeding retry limits. The caller must
    handle this exception - events are NOT silently lost.

    Use cases:
    - After max_retries consecutive flush failures
    - Transport buffer full and circuit breaker triggered
    - Network connectivity issues preventing delivery

    Applications should implement retry logic or alerting mechanism when this
    exception is raised, as budget protection may be compromised.

    NOTE: NOT inheriting from ``NullRunError`` because this exception
    signals a loss of the audit pipeline itself, not a structured
    SDK error. Surface to the operator; do not treat like a regular
    NullRun failure.
    """

    def __init__(
        self,
        message: str,
        events_lost: int = 0,
        buffer_size: int = 0,
        **details: Any,
    ) -> None:
        self.events_lost = events_lost
        self.buffer_size = buffer_size
        self.details = details
        super().__init__(
            f"Transport error: {message} "
            f"(events_lost={events_lost}, buffer_size={buffer_size}, details={details})"
        )


class InsecureTransportError(BreakerTransportError):
    """Raised when SDK is configured with insecure HTTP (non-localhost)."""

    pass


# ---------------------------------------------------------------------------
# Configuration / authentication
# ---------------------------------------------------------------------------
class NullRunConfigError(NullRunInfrastructureError):
    """Raised when the SDK is misconfigured: missing api_key, bad
    key format, workflow not registered, etc.

    These are NEVER retryable — retrying with the same configuration
    will hit the same wall. The fix is always outside the loop.
    """

    error_code = "NR-C000"  # subclasses override
    user_action = (
        "Review your NullRun configuration. The SDK cannot recover "
        "from configuration errors on its own — see the error_code "
        "link in the exception for the specific fix."
    )
    retryable = False


class NullRunAuthenticationError(NullRunInfrastructureError):
    """
    Raised when authentication fails and safe mode is required.

    This exception indicates that the SDK could not authenticate with
    the NullRun backend and will not operate in unprotected mode.
    Applications should handle this exception and provide valid credentials.

    Inherits from:class:`NullRunError` (Layer 1) so callers can do
    ``except NullRunError`` to catch every user-facing SDK failure
    with structured fields. Existing ``except NullRunAuthenticationError``
    clauses keep matching.
    """

    error_code = "NR-A001"  # default; ``NullRunAuthError`` overrides per status
    user_action = (
        "The NullRun backend rejected the request. Verify the API "
        "key at https://app.nullrun.io/settings/api-keys and ensure "
        "it has not been revoked."
    )
    retryable = False

    def __init__(self, message: str, **kwargs: Any) -> None:
        # Preserve the historical ``self.message`` attribute — some
        # user code reads ``exc.message`` instead of ``str(exc)``.
        self.message = message
        super().__init__(message, **kwargs)


class NullRunAuthError(NullRunAuthenticationError):
    """401 from the backend — key was rejected.

    Subclass of:class:`NullRunAuthenticationError` so existing
    ``except NullRunAuthenticationError`` clauses keep matching.

    The wire error code (one of ``API_KEY_REVOKED`` /
    ``API_KEY_EXPIRED`` / ``API_KEY_DISABLED`` / ``API_KEY_INVALID``
    / ``API_KEY_MISSING`` / ``API_KEY_MALFORMED`` per v3.38) is
    stored on ``self.wire_code`` so callers can branch on the
    granular lifecycle state without clobbering the SDK-side
    ``error_code`` taxonomy (``NR-A003``). Pattern mirrors
    :class:`NullRunChainError.backend_code`.
    """

    error_code = "NR-A003"
    user_action = (
        "The API key was rejected by the NullRun backend (401). "
        "Verify the key at https://app.nullrun.io/settings/api-keys "
        "and rotate it if it has been revoked."
    )
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        wire_code: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.wire_code = wire_code or "API_KEY_REVOKED"
        # Preserve the historical ``self.message`` attribute — some
        # user code reads ``exc.message`` instead of ``str(exc)``.
        self.message = message
        super().__init__(message, **kwargs)


# ---------------------------------------------------------------------------
# Block decisions (budget, loop, rate, tool-block)
# ---------------------------------------------------------------------------
class NullRunBlockedException(NullRunDecision):
    """
    Raised when NullRun circuit breaker trips.

    This is the client-side enforcement exception that
    immediately stops runaway agents without waiting for
    network roundtrip to the backend.

    Use cases:
    - Budget exceeded
    - Loop detected (>6 same tool calls)
    - Retry storm (>5 retries)
    - Rate limit exceeded

    Subclasses (:class:`NullRunBudgetError`,:class:`NullRunToolBlockedError`)
    carry the specific ``error_code`` and ``user_action`` for each
    block reason. ``except NullRunBlockedException`` continues to
    match all of them — back-compat.

    Attributes:
        workflow_id: Workflow that was blocked (may be a sentinel like
            "<unknown>" when the block fires outside a workflow context
            e.g. the sensitive-tool pre-check).
        reason: Human-readable explanation of why the block fired.
        action: One of "block" / "kill" / "pause" — the suggested
            downstream action.
        tool_name: Optional name of the tool that triggered the block.
            Surfaced as a first-class attribute (not just `details`) so
            cookbook examples and audit pipelines can read
            `exc.tool_name` without indexing into `**details`.
            ``None`` when the block is workflow-scoped rather than
            tool-scoped.
        details: Free-form structured payload forwarded by the caller.
        status_code: HTTP status code the backend sent on the wire
            when the block was derived from a server response (e.g.
            402 for ``BUDGET_HARD_BLOCKED``, 403 for
            ``TOOL_BLOCKED`` / ``WORKFLOW_INACTIVE``, 429 for
            ``RATE_LIMIT_EXCEEDED``). ``None`` for client-side
            blocks (sensitive-tool pre-check, loop detection, retry
            storm) where there was no wire response. Lets FastAPI /
            Starlette exception handlers map to the correct HTTP
            status without re-deriving it from
            ``type(exc).__name__``.
    """

    error_code = "NR-X001"  # generic block; subclasses override
    user_action = (
        "NullRun blocked this call. The body did not run. See the "
        "error_code link in the exception for the specific reason "
        "and the fix."
    )
    retryable = False

    def __init__(
        self,
        workflow_id: str,
        reason: str,
        action: str = "block",
        tool_name: str | None = None,
        status_code: int | None = None,
        **details: Any,
    ) -> None:
        self.workflow_id = workflow_id
        self.reason = reason
        self.action = action
        self.tool_name = tool_name
        # 2026-07-04: wire HTTP status preserved
        self.status_code = status_code
        self.details = details
        tool_suffix = f", tool={tool_name}" if tool_name else ""
        # ``code`` / ``user_action`` / ``retryable`` can be overridden
        # by the caller via ``details`` — useful when the same call
        # site raises for multiple block reasons and wants the
        # catalog value to be exact (e.g. loop vs. retry storm).
        error_code = details.pop("error_code", None) or self.error_code
        user_action = details.pop("user_action", None) or self.user_action
        retryable = details.pop("retryable", None)
        if retryable is None:
            retryable = self.retryable
        super().__init__(
            f"Workflow {workflow_id} blocked: {reason} "
            f"(action={action}{tool_suffix}, status_code={status_code}, details={details})",
            error_code=error_code,
            user_action=user_action,
            retryable=retryable,
        )


class NullRunBudgetError(NullRunBlockedException):
    """Budget exhausted — every cost-bearing call will be rejected.

    Subclass of:class:`NullRunBlockedException` so the existing
    ``except NullRunBlockedException:`` pattern keeps matching.
    """

    error_code = "NR-B004"
    user_action = (
        "Workflow budget is exhausted. Increase the budget at "
        "https://app.nullrun.io/billing or wait for the next billing "
        "cycle. Until then, every @protect call will be rejected."
    )
    retryable = False


class NullRunBudgetRecheckFailedError(NullRunBudgetError):
    """Budget authorization failed during the post-approval re-check on /execute.

    Distinct from :class:`NullRunBudgetError` (which is raised when /gate
    itself blocks) — this is raised on the SECOND authorization decision:
    the operator approved the grant at /gate, but the period-bound
    budget counter moved between /gate reserve and /execute (typically
    another concurrent execution spent the budget). Wire code
    ``BUDGET_RECHECK_FAILED`` from `GateErrorCode::BudgetRecheckFailed`
    on the backend (error_codes.rs).

    Carries ``current_spend_cents`` and ``budget_cents`` (from the
    backend ``details`` envelope) so callers can compute the remaining
    cap and decide whether to retry after re-``/gate``.

    Subclass of :class:`NullRunBudgetError` so the existing
    ``except NullRunBudgetError:`` pattern keeps matching. New
    ``except NullRunBudgetRecheckFailedError:`` branches on the typed
    shape (recommended: re-/gate then re-/execute).

    Audit: H6 (2026-08-12). Pre-fix SDK 0.14.x collapsed this code
    into a generic ``NullRunBudgetError("Budget authorization failed")``
    with no introspection on the running counter.
    """

    error_code = "NR-B006"
    user_action = (
        "Post-approval budget re-check failed — another execution "
        "spent the budget between /gate and /execute. Call /gate "
        "again to refresh the reservation, then retry /execute."
    )
    retryable = True

    def __init__(
        self,
        message: str,
        *,
        current_spend_cents: int | None = None,
        budget_cents: int | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(
            workflow_id="<recheck>",
            reason=message,
            status_code=status_code,
        )
        # First-class attributes so callers can read the running
        # counter without indexing into ``details``.
        self.current_spend_cents: int | None = current_spend_cents
        self.budget_cents: int | None = budget_cents
        self.recheck_retryable: bool = True


class NullRunBudgetThrottleError(NullRunBudgetError):
    """Backend returned ``decision == "throttle"`` — soft budget signal.

    Distinct from :class:`NullRunBudgetError` (NR-B004, the hard-block
    case raised when ``decision == "block"``). Throttle means
    "rate-limit this workflow but don't fully block it" — a temporary
    pacing signal that the SDK surfaces as a typed exception so
    cookbook code can back off and retry, vs. the hard block where
    the same parameters would fail again.

    Added 2026-09-08 to retire the generic ``WorkflowKilledInterrupt``
    raise on the throttle path. Cookbook pattern: catch this
    specifically (``except NullRunBudgetThrottleError``), sleep for
    the cooldown window, and retry — distinct from the hard block
    where retrying with the same budget tier is futile.
    """

    error_code = "NR-B007"
    user_action = (
        "Backend throttled this workflow (soft budget signal). Wait "
        "for the cooldown window shown in the response and retry — "
        "do NOT request a budget increase for a throttle (that is "
        "the wrong remediation; the issue is pacing, not cap)."
    )
    retryable = True


class NullRunExecutionNotFoundError(NullRunBackendError):
    """``/execute`` or ``/cancel`` was called with an ``execution_id`` that
    has no live server-side binding.

    Wire code ``EXECUTION_NOT_FOUND`` (HTTP 404) from backend
    `GateErrorCode::ExecutionNotFound` (`error_codes.rs`). Two emission
    sites:
      - ``backend/src/proxy/http/gate/execute.rs:194`` — when /execute
        fires before /gate (or after the binding TTL expired)
      - ``backend/src/proxy/http/cancel.rs:303`` — same condition on
        the cancel path

    Cookbook pattern: do NOT retry the same ``execution_id``; the
    server never minted it (or its binding has expired and the
    reservation has been released). Re-issue ``/api/v1/gate`` to mint
    a fresh ``execution_id``, then retry /execute.

    Subclass of :class:`NullRunBackendError` (NR-GEN) so the existing
    ``except NullRunBackendError:`` cookbook pattern keeps matching;
    callers that want to handle this specific case can ``except
    NullRunExecutionNotFoundError`` for a clearer intent.

    Audit: 2026-09-09 SDK-drift audit — pre-fix SDK 0.15.x collapsed
    this code into a generic ``NullRunBackendError("Execution binding
    not found")`` with no introspection on whether /gate was missed.
    """

    error_code = "NR-EX01"
    user_action = (
        "/execute (or /cancel) was called without a prior /gate that "
        "minted this execution_id — or the binding TTL expired. "
        "Re-issue /api/v1/gate to get a fresh execution_id, then retry."
    )
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        execution_id: str | None = None,
        endpoint: str | None = None,
        status_code: int | None = None,
    ) -> None:
        # Wire detail envelope carries execution_id + endpoint;
        # promote them to first-class kwargs on the exception so
        # cookbook code can introspect without indexing into
        # ``details``. The parent (NullRunBackendError) accepts
        # ``endpoint`` as a named param and ``**details`` for
        # everything else, so we route execution_id through
        # details to avoid colliding with the parent's signature.
        details: dict[str, Any] = {}
        if execution_id is not None:
            details["execution_id"] = execution_id
        super().__init__(
            message=message,
            endpoint=endpoint or "/api/v1/execute",
            status_code=status_code,
            **details,
        )
        # First-class attributes so cookbook code can introspect
        # which execution_id and which endpoint surfaced the 404
        # without indexing into ``details``.
        self.execution_id: str | None = execution_id
        # ``endpoint`` is always set after ``super().__init__`` (the
        # parent constructor receives ``endpoint or "/api/v1/execute"``,
        # never None). Override the inherited ``str`` annotation with the
        # same type so mypy is happy — we are narrowing the parent's
        # declared type by subclass attribute re-assignment here, not
        # widening it.
        self.endpoint: str = endpoint or "/api/v1/execute"
        # Re-issue /gate is the only path forward.
        self.regate_required: bool = True


class NullRunToolBlockedError(NullRunBlockedException):
    """The tool is in the workflow's block list.

    Subclass of:class:`NullRunBlockedException` so the existing
    ``except NullRunBlockedException:`` pattern keeps matching.
    Carries ``tool_name`` (set by the raise site) so the user knows
    which tool is the offender.
    """

    error_code = "NR-T001"
    user_action = (
        "This tool is in the workflow's block list. Remove it from the "
        "block list at https://app.nullrun.io/policies/<workflow> or "
        "use a different tool."
    )
    retryable = False


# ---------------------------------------------------------------------------
# Approval grant-consume outcomes (v3.53 / 2026-08-13 audit, A-1/A-2)
# ---------------------------------------------------------------------------
# These six typed exceptions wire-up the /execute grant-consume outcomes
# that backend `backend/src/proxy/http/gate/internal.rs:3059-3108, 3115-3138`
# surfaces as distinct §13 wire codes. Pre-v3.53 the SDK collapsed all six
# into a generic ``NullRunBlockedException`` because the codes were missing
# from ``_V3_ERROR_CODE_MAP`` (transport.py:2427-2484) — bilateral wire
# gap. Post-v3.53 each outcome maps to its own typed class so cookbook
# recipes can ``except NullRunApprovalDeniedError:`` / ``except
# NullRunApprovalExpiredError:`` / ``except NullRunDigestMismatchError:``
# instead of string-matching the ``error_message``.
#
# All six subclass :class:`NullRunBlockedException` so the legacy
# ``except NullRunBlockedException:`` pattern keeps matching — back-compat
# invariant preserved.
class NullRunApprovalNotYetApprovedError(NullRunBlockedException):
    """The approval row exists but the operator has not yet decided.

    Wire code ``APPROVAL_NOT_YET_APPROVED`` (HTTP 403). SDK cookbook
    pattern: poll the approval via the WS push channel or sleep +
    retry, NOT surface as terminal error.

    Distinct from :class:`NullRunApprovalDeniedError` (operator said
    no — terminal) and from :class:`NullRunApprovalExpiredError`
    (operator said yes but grant TTL elapsed). All three share the
    HTTP 403 envelope; the wire code is the discriminator.

    Also raised client-side (NOT just wire path) on the /execute
    "approval_id missing in response" malformed-payload case — see
    ``NullRunApprovalResponseMissingError`` for the precise semantic
    distinction (NR-A004 is the wire-bug code, NR-A010 is "operator
    has not decided yet").
    """

    error_code = "NR-A010"
    user_action = (
        "Approval is pending — the operator has not yet decided. Wait "
        "for the approval_resolved WebSocket frame or poll the "
        "approval row; do NOT raise this to the user as terminal."
    )
    retryable = True

    def __init__(
        self,
        workflow_id: str,
        reason: str,
        action: str = "block",
        tool_name: str | None = None,
        status_code: int | None = None,
        *,
        approval_id: str | None = None,
        **details: Any,
    ) -> None:
        super().__init__(
            workflow_id=workflow_id,
            reason=reason,
            action=action,
            tool_name=tool_name,
            status_code=status_code,
            **details,
        )
        # First-class attribute so cookbook code can introspect the
        # pending approval row without parsing the message string.
        self.approval_id = approval_id


class NullRunApprovalResponseMissingError(NullRunBlockedException):
    """``/execute`` returned ``require_approval`` but the response body
    did not include an ``approval_id`` — wire-bug / server drift.

    Wire code ``NR-A004`` (was previously set inline on a generic
    ``NullRunBlockedException`` at runtime.py:2888, 2914, 2929 — promoted
    to a typed class for parity with the six approval exceptions above).
    This is distinct from ``NullRunApprovalNotYetApprovedError`` (NR-A010)
    which is "the operator has not yet decided". Here the operator never
    had a chance — the wire envelope was incomplete.

    Cookbook pattern: do NOT retry the same execution_id; the backend
    needs a fix or the wire-shape contract needs re-reading. Log the
    full response body and report to NULLRUN support.
    """

    error_code = "NR-A004"
    user_action = (
        "Server returned require_approval without an approval_id — "
        "this is a wire-contract bug, NOT a transient failure. Inspect "
        "the full response body and report to NullRun support; do not "
        "retry the same execution_id."
    )
    retryable = False


class NullRunApprovalDeniedError(NullRunBlockedException):
    """Operator explicitly denied the approval.

    Wire code ``APPROVAL_DENIED`` (HTTP 403). Terminal — re-running
    with the same approval_id will keep failing. Cookbook pattern:
    surface denial to the user and request a fresh approval row
    (different parameters / intent).

    Now raised client-side (NOT just wire path) on the WS push "denied"
    outcome at ``check_workflow_budget`` and on the /execute "outcome
    != approved" branch.
    """

    error_code = "NR-A011"
    user_action = (
        "Operator denied the approval. Surface the denial to the "
        "user, request a fresh approval row with revised parameters. "
        "Re-running with the same approval_id will fail again."
    )
    retryable = False

    def __init__(
        self,
        workflow_id: str,
        reason: str,
        action: str = "block",
        tool_name: str | None = None,
        status_code: int | None = None,
        *,
        approval_id: str | None = None,
        denial_note: str | None = None,
        **details: Any,
    ) -> None:
        super().__init__(
            workflow_id=workflow_id,
            reason=reason,
            action=action,
            tool_name=tool_name,
            status_code=status_code,
            **details,
        )
        self.approval_id = approval_id
        self.denial_note = denial_note


class NullRunApprovalExpiredError(NullRunBlockedException):
    """Approval grant aged out — operator said yes but ``expires_at`` is past.

    Wire code ``APPROVAL_EXPIRED`` (HTTP 403). Two raise paths:

    1. **Wire path** — backend returns APPROVAL_EXPIRED on /execute
       because the operator's grant TTL elapsed between /gate and
       /execute.
    2. **Client-side timeout path** (added 2026-09-08, the trigger for
       this typed exception migration) — WS push went silent for
       ``approval_timeout_seconds`` (default 300s) without an operator
       decision. The SDK raises this exception instead of the generic
       ``WorkflowKilledInterrupt`` so cookbook code can catch it
       (`except NullRunApprovalExpiredError`) and react with a fresh
       approval request.

    Cookbook pattern: do NOT retry the same approval_id — request a
    fresh row and re-/gate.
    """

    error_code = "NR-A012"
    user_action = (
        "Approval expired — no operator decision within the configured "
        "timeout window (WS push silent past approval_timeout_seconds). "
        "Request a fresh approval row and retry /gate; the previous "
        "approval_id cannot be revived."
    )
    retryable = False

    def __init__(
        self,
        workflow_id: str,
        reason: str,
        action: str = "block",
        tool_name: str | None = None,
        status_code: int | None = None,
        *,
        approval_id: str | None = None,
        timeout_seconds: float | None = None,
        local_timeout: bool = False,
        **details: Any,
    ) -> None:
        super().__init__(
            workflow_id=workflow_id,
            reason=reason,
            action=action,
            tool_name=tool_name,
            status_code=status_code,
            **details,
        )
        self.approval_id = approval_id
        # Server-authoritative timeout the SDK waited for. ``None`` when
        # the exception came from the wire path (where the backend
        # already closed the grant; the SDK never started a wait).
        self.timeout_seconds = timeout_seconds
        # True when raised by the SDK on local WS-silent timeout (path 2
        # above); False when raised by the wire path (path 1). Lets
        # cookbook code distinguish "operator never saw the request"
        # (local timeout — maybe the request never propagated) from
        # "operator approved but grant TTL elapsed" (wire path).
        self.local_timeout = local_timeout


class NullRunApprovalReplayRejectedError(NullRunBlockedException):
    """Approval grant was already consumed by a prior /execute call.

    Wire code ``APPROVAL_REPLAY_REJECTED`` (HTTP 403). Each grant
    is single-use per ``consume_approved`` atomic check-and-set.
    Cookbook pattern: do NOT retry the same approval_id; treat as
    idempotency violation (likely a client retry loop).

    Also raised client-side on the /execute "post-approval re-check
    returned require_approval again" race (the operator approved but
    the same approval_id was already consumed by a concurrent /execute).
    """

    error_code = "NR-A015"
    user_action = (
        "Approval grant was already consumed by a prior /execute "
        "call — this is a replay/retry-loop signal, NOT a transient "
        "failure. Inspect your retry logic; the same approval_id "
        "will never succeed twice."
    )
    retryable = False

    def __init__(
        self,
        workflow_id: str,
        reason: str,
        action: str = "block",
        tool_name: str | None = None,
        status_code: int | None = None,
        *,
        approval_id: str | None = None,
        **details: Any,
    ) -> None:
        super().__init__(
            workflow_id=workflow_id,
            reason=reason,
            action=action,
            tool_name=tool_name,
            status_code=status_code,
            **details,
        )
        self.approval_id = approval_id


class NullRunApprovalDigestMismatchError(NullRunBlockedException):
    """Business-impact digest drifted since operator approval (ADR-006).

    Wire code ``APPROVAL_DIGEST_MISMATCH` (HTTP 403). The operator
    approved action A; SDK /execute requests action B (different
    business impact). Defense against prompt-injection-driven silent
    capability drift. Cookbook pattern: request fresh approval with
    the actual impact the SDK intends to execute.
    """

    error_code = "NR-A013"
    user_action = (
        "Business-impact digest mismatch — the operator approved a "
        "different action than the one currently bound to this "
        "execution. Request fresh approval with the intended impact "
        "and retry /execute."
    )
    retryable = False


class NullRunApprovalToolDigestMismatchError(NullRunBlockedException):
    """Tool capability digest drifted since operator approval (T8 / ADR-008).

    Wire code ``APPROVAL_TOOL_DIGEST_MISMATCH`` (HTTP 403). The
    operator approved the tool at /gate-create; the MCP server's
    current capability surface differs at /execute (added destructive
    flag, expanded schema, etc.). Cookbook pattern: re-pull the
    current MCP ``tools/list`` and re-run /gate-create with the new
    capability digest, OR roll back the server.
    """

    error_code = "NR-A014"
    user_action = (
        "Tool capability digest mismatch — the operator approved a "
        "different tool capability than the one currently bound. "
        "Re-pull MCP tools/list and re-run /gate-create with the "
        "current capability surface, or roll back the server."
    )
    retryable = False


# NOTE: NullRunApprovalReplayRejectedError was moved earlier in this
# module (alongside the other five approval exceptions) so all six
# typed approval exceptions are co-located. The earlier definition
# also adds an explicit ``__init__`` accepting ``approval_id`` as a
# first-class attribute. See the block just below the
# ``NullRunApprovalNotYetApprovedError`` docstring for the canonical
# definition.


# NOTE: the following six exception classes were removed in 0.4.0
# because they had no callers in the SDK or in any test. They were
# zombie public surface — defined but never raised. If a real use
# case emerges in the future, they should be re-added with at least
# one in-tree caller and a regression test that exercises the raise
# path:
# - CostLimitExceeded
# - ApprovalRequired
# - BreakerTimeout
# - LoopDetectedException
# - RetryStormException
# - RateLimitExceededException


class WorkflowPausedException(NullRunDecision):
    """
    Raised when workflow is paused by NullRun.

    This allows the workflow to be resumed later after
    human approval or automatic cooldown.

    Inherits from:class:`NullRunError` (Layer 1) so it carries
    ``error_code`` (``NR-W003``) and a ``user_action`` hint pointing
    at the workflow page on the dashboard.
    """

    error_code = "NR-W003"
    user_action = (
        "The workflow is paused. Resume it at "
        "https://app.nullrun.io/workflows/<workflow_id> or wait for "
        "the cooldown to expire."
    )
    retryable = False

    def __init__(self, workflow_id: str, reason: str, resume_after: float | None = None) -> None:
        self.workflow_id = workflow_id
        self.reason = reason
        self.resume_after = resume_after
        msg = f"Workflow {workflow_id} paused: {reason}"
        if resume_after:
            msg += f" (resume after {resume_after}s)"
        super().__init__(msg)


class WorkflowKilledException(BaseException):
    """
    DEPRECATED. Use:class:`WorkflowKilledInterrupt` instead.

    Kept for backward compatibility: this class is the *parent* of
:class:`WorkflowKilledInterrupt`, so user code that does
    ``except WorkflowKilledException`` will still catch the new raises
    (``except X`` matches subclasses of ``X`` — and the new class is
    a subclass of this one).

    A ``DeprecationWarning`` is emitted on construction. The class will
    be removed in a future major release; migrate new code to
:class:`WorkflowKilledInterrupt` and update existing
    ``except WorkflowKilledException`` clauses to
    ``except WorkflowKilledInterrupt`, or, if recovery is impossible
    let the exception propagate to the top of the loop.

    This class is **not** an ``Exception`` subclass — kill is a
    non-recoverable signal and should not be caught by generic
    ``except Exception`` clauses. Only ``except BaseException`` or the
    explicit ``except WorkflowKilledInterrupt`` reliably stops the work.
    See ``docs/kill-contract.md`` for the full rationale.

    NOTE: NOT inheriting from:class:`NullRunError` because
    ``NullRunError`` is an ``Exception`` subclass — and the kill
    contract deliberately excludes ``except Exception`` from catching
    this signal. The structured fields are attached at construction
    time as instance attributes (not class attributes) so the kill
    site can still stamp ``error_code`` / ``user_action`` without
    breaking the BaseException contract.
    """

    error_code = "NR-W002"
    user_action = (
        "The workflow was killed. The body did not run and the kill "
        "is non-recoverable from inside the agent loop. Inspect the "
        "reason and, if appropriate, resume the workflow at "
        "https://app.nullrun.io/workflows/<workflow_id>."
    )
    retryable = False

    def __init__(self, workflow_id: str, reason: str) -> None:
        import warnings as _w

        _w.warn(
            "WorkflowKilledException is deprecated. Catch "
            "WorkflowKilledInterrupt (BaseException) instead. The class "
            "is preserved for backward-compatible `except` clauses but "
            "will be removed in a future major release.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.workflow_id = workflow_id
        self.reason = reason
        super().__init__(f"Workflow {workflow_id} killed: {reason}")


class WorkflowKilledInterrupt(NullRunError):
    """
    Raised when a workflow is killed by the NullRun control plane.

    **2026-09-08 migration**: this class is now an ``Exception``
    subclass (``NullRunError`` parent) — formerly ``BaseException``.
    The user override: agent recovery code needs to catch the kill
    signal via ``except WorkflowKilledInterrupt`` or
    ``except NullRunWorkflowKilledError`` to surface a structured
    error to the user with ``error_code=NR-W002`` and ``user_action``.

    Migration back-compat guarantees (all three hold):

      * ``except WorkflowKilledInterrupt`` (new code) — still matches,
        including legacy raises that haven't been updated.
      * ``except NullRunError`` — now matches (was NO match before
        migration; this is the new ability the user wanted).
      * ``except NullRunWorkflowKilledError`` — matches (preferred
        typed name for new cookbook code).

    Migration BREAK (acceptable, documented in CHANGELOG):

      * ``except WorkflowKilledException`` (the deprecated parent
        class) — no longer matches. The parent class remains
        BaseException and emits DeprecationWarning on construction,
        but is no longer in the ``WorkflowKilledInterrupt`` MRO. Code
        that catches the deprecated name must migrate to either
        ``WorkflowKilledInterrupt`` (keep current name) or
        ``NullRunWorkflowKilledError`` (preferred typed name).

    Fields:
        workflow_id: The workflow that was killed.
        reason: Server-supplied reason (e.g. "killed via API"
                      "budget exhausted", "circuit-breaker tripped").

    Catching in production
    ----------------------
    ``WorkflowKilledInterrupt`` is now an ``Exception`` subclass.
    Cookbook code can do::

        try:
            agent.run()
        except NullRunWorkflowKilledError as exc:
            surface_to_user(
                f"Workflow {exc.workflow_id} was killed: {exc.reason}. "
                f"{exc.user_action}"
            )

    or for broader catch::

        try:
            agent.run()
        except Exception as exc:
            # Now catches kill signals too (the new contract).
            sentry_sdk.capture_exception(exc)
            raise

    Sentry / OpenTelemetry handlers that filter on ``Exception`` will
    now record kill events — this is the intended new behavior. Code
    that relies on kill being un-catchable by ``except Exception`` is
    a regression candidate; see ``docs/kill-contract-migration-2026-09-08.md``.
    """

    error_code = "NR-W002"
    user_action = (
        "The workflow was killed by the NullRun control plane. The "
        "body did not run. Inspect the reason (killed via dashboard, "
        "killed via API, circuit-breaker tripped, etc.) and, if "
        "appropriate, resume the workflow at "
        "https://app.nullrun.io/workflows/<workflow_id>."
    )
    retryable = False

    def __init__(
        self,
        workflow_id: str,
        reason: str,
        *,
        kill_source: str | None = None,
        **details: Any,
    ) -> None:
        # Skip NullRunError.__init__'s kwargs-by-key path — we want
        # the structured fields attached as instance attrs (matches
        # the pre-migration shape) AND surfaced through the NullRunError
        # fields too, so cookbook introspection works either way.
        self.workflow_id = workflow_id
        self.reason = reason
        # First-class attribute distinguishing operator kill from
        # circuit-breaker kill, etc. None when the source is ambiguous.
        self.kill_source = kill_source
        NullRunError.__init__(
            self,
            f"Workflow {workflow_id} killed: {reason}",
            error_code=self.error_code,
            user_action=self.user_action,
            **details,
        )


class NullRunWorkflowKilledError(WorkflowKilledInterrupt):
    """Typed public name for the kill signal.

    Subclass of :class:`WorkflowKilledInterrupt` (which remains the
    legacy canonical name) so ``except WorkflowKilledInterrupt``
    clauses continue to match. New cookbook code should prefer this
    name (``except NullRunWorkflowKilledError``) for typed dispatch.

    Wire code ``NR-W002`` (same as parent). Distinct from
    :class:`NullRunBlockedException` family — kill is a control-plane
    signal (operator or circuit-breaker), not a gate-decision block.

    Cookbook pattern (2026-09-08 migration):

        try:
            agent.run()
        except NullRunWorkflowKilledError as exc:
            # Structured fields ready for the LLM:
            # exc.workflow_id, exc.reason, exc.kill_source,
            # exc.error_code ("NR-W002"), exc.user_action
            surface_to_user(
                f"Workflow {exc.workflow_id} was killed "
                f"(source={exc.kill_source}): {exc.user_action}"
            )
    """

    error_code = "NR-W002"
    user_action = (
        "Workflow was killed by the NullRun control plane (operator "
        "action or circuit-breaker). The body did not run. Resume "
        "the workflow at https://app.nullrun.io/workflows/<workflow_id> "
        "or inspect the reason before retrying."
    )
    retryable = False
