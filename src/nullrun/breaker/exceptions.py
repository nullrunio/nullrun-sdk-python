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
#   * ``error_code``   — stable, grep-able identifier (e.g. ``"NR-A001"``).
#                        Documented in ``docs/errors/<code>.md`` and
#                        available to telemetry / Sentry / dashboards.
#   * ``user_action``  — short, imperative sentence telling the user what
#                        to do next ("Set NULLRUN_API_KEY env var",
#                        "Verify API key at https://app.nullrun.io/...",
#                        "Retry in 30s, backend is down"). Empty when
#                        there is no actionable step.
#   * ``retryable``    — ``True`` when a retry after a backoff is the
#                        correct response (5xx, network blip, transient
#                        auth). ``False`` for config / permission /
#                        budget-exhausted — retrying without changing
#                        something will just hit the same wall.
#   * ``docs_url``     — link to the per-code docs page. Always set; falls
#                        back to ``https://docs.nullrun.io/errors`` when
#                        the per-code page does not exist yet.
#
# Existing ``except`` clauses keep working: every existing public class
# (``NullRunAuthenticationError``, ``NullRunBlockedException``,
# ``NullRunTransportError``, ``WorkflowKilledException``,
# ``WorkflowPausedException``) inherits from ``NullRunError`` now, so
# ``except NullRunError:`` catches them all — but the narrower clauses
# keep matching too.
#
# New specialized classes (``NullRunConfigError``, ``NullRunAuthError``,
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

    * :class:`NullRunDecision` — expected policy outcomes (budget
      cap, tool block, rate limit, loop detection, workflow pause).
      The enforcement layer is doing its job; the UX is "what
      happened" + (where applicable) "how to proceed".
    * :class:`NullRunInfrastructureError` — system failures (network,
      backend 5xx, auth rejection, config error). The SDK could not
      reach or query the policy engine; the UX is a generic
      "service unavailable" with operator triage info.

    Both inherit from :class:`NullRunError`, so existing
    ``except NullRunError:`` clauses keep matching — the split is a
    strict refinement, not a breaking change. ``WorkflowKilledInterrupt``
    is **not** in either category: it remains a ``BaseException``
    subclass so kill signals bypass any ``except Exception:`` that
    might otherwise swallow them.
    """

    #: Default error code when a subclass does not override it.
    #: Real codes are ``"NR-LETTERNNN"`` — see the catalog at the top
    #: of the docstring above.
    error_code: str = "NR-0000"

    #: Short imperative next-step hint shown in tracebacks and
    #: surfaced by the cookbook example. Empty string means "no
    #: actionable step beyond what the message says".
    user_action: str = ""

    #: ``True`` only when a retry after a backoff is the correct
    #: response (5xx, network blip, transient auth). Default is
    #: ``False`` because the common case is "user must change
    #: something before retrying makes sense".
    retryable: bool = False

    #: Per-code docs page. Fallback to the index when the per-code
    #: page does not exist yet — the docs site is responsible for
    #: the 404 page, not the SDK.
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
            # Mirror Python's `raise ... from` behaviour so ``str(exc)``
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
# event the exception represents. They are pure markers — no new fields,
# no constructor changes. Host code can use them as the catch-all for
# a category without enumerating individual codes:
#
#     try:
#         ...
#     except NullRunDecision as d:
#         # Budget, tool block, rate limit, loop, pause — expected
#         return d.user_action_or_message()
#     except NullRunInfrastructureError as e:
#         # Network, 5xx, auth, config — system failure
#         sentry.capture_exception(e)
#         return "service unavailable"
#
# Both inherit from NullRunError so ``except NullRunError:`` keeps
# matching existing handlers — the split is additive.
class NullRunDecision(NullRunError):
    """Marker for expected policy outcomes.

    Includes budget caps, tool blocks, rate limits, loop detection,
    workflow pause, and the generic block fallback. These are NOT
    system failures — the enforcement layer reached a deliberate
    decision. UX should explain the decision and (where applicable)
    offer an upgrade or alternative action.

    End-user messaging for these exceptions is stable per ``error_code``
    (see :mod:`nullrun.messages`) and rarely needs to mention the
    decision mechanism.
    """


class NullRunInfrastructureError(NullRunError):
    """Marker for system failures (operator-facing).

    Includes network errors reaching the policy engine, gateway 5xx,
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

    Inherits from :class:`NullRunError` (Layer 1) so every transport
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

    Subclass of :class:`NullRunTransportError` so existing
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

    Phase 4: subclass of ``NullRunTransportError`` so
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

    Inherits from :class:`NullRunError` (Layer 1) so callers can do
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

    Subclass of :class:`NullRunAuthenticationError` so existing
    ``except NullRunAuthenticationError`` clauses keep matching.
    """

    error_code = "NR-A003"
    user_action = (
        "The API key was rejected by the NullRun backend (401). "
        "Verify the key at https://app.nullrun.io/settings/api-keys "
        "and rotate it if it has been revoked."
    )
    retryable = False


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

    Subclasses (:class:`NullRunBudgetError`, :class:`NullRunToolBlockedError`)
    carry the specific ``error_code`` and ``user_action`` for each
    block reason. ``except NullRunBlockedException`` continues to
    match all of them — back-compat.

    Attributes:
        workflow_id: Workflow that was blocked (may be a sentinel like
            "<unknown>" when the block fires outside a workflow context,
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
        **details: Any,
    ) -> None:
        self.workflow_id = workflow_id
        self.reason = reason
        self.action = action
        self.tool_name = tool_name
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
            f"(action={action}{tool_suffix}, details={details})",
            error_code=error_code,
            user_action=user_action,
            retryable=retryable,
        )


class NullRunBudgetError(NullRunBlockedException):
    """Budget exhausted — every cost-bearing call will be rejected.

    Subclass of :class:`NullRunBlockedException` so the existing
    ``except NullRunBlockedException:`` pattern keeps matching.
    """

    error_code = "NR-B004"
    user_action = (
        "Workflow budget is exhausted. Increase the budget at "
        "https://app.nullrun.io/billing or wait for the next billing "
        "cycle. Until then, every @protect call will be rejected."
    )
    retryable = False


class NullRunToolBlockedError(NullRunBlockedException):
    """The tool is in the workflow's block list.

    Subclass of :class:`NullRunBlockedException` so the existing
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


# NOTE (Sprint 2.2): the following six exception classes were removed
# in 0.4.0 because they had no callers in the SDK or in any
# test. They were zombie public surface — defined but never raised.
# If a real use case emerges in the future, they should be re-added
# with at least one in-tree caller and a regression test that
# exercises the raise path:
#   - CostLimitExceeded
#   - ApprovalRequired
#   - BreakerTimeout
#   - LoopDetectedException
#   - RetryStormException
#   - RateLimitExceededException


class WorkflowPausedException(NullRunDecision):
    """
    Raised when workflow is paused by NullRun.

    This allows the workflow to be resumed later after
    human approval or automatic cooldown.

    Inherits from :class:`NullRunError` (Layer 1) so it carries
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
    DEPRECATED. Use :class:`WorkflowKilledInterrupt` instead.

    Kept for backward compatibility: this class is the *parent* of
    :class:`WorkflowKilledInterrupt`, so user code that does
    ``except WorkflowKilledException`` will still catch the new raises
    (``except X`` matches subclasses of ``X`` — and the new class is
    a subclass of this one).

    A ``DeprecationWarning`` is emitted on construction. The class will
    be removed in a future major release; migrate new code to
    :class:`WorkflowKilledInterrupt` and update existing
    ``except WorkflowKilledException`` clauses to
    ``except WorkflowKilledInterrupt`, or, if recovery is impossible,
    let the exception propagate to the top of the loop.

    This class is **not** an ``Exception`` subclass — kill is a
    non-recoverable signal and should not be caught by generic
    ``except Exception`` clauses. Only ``except BaseException`` or the
    explicit ``except WorkflowKilledInterrupt`` reliably stops the work.
    See ``docs/kill-contract.md`` §6 for the full rationale.

    NOTE: NOT inheriting from :class:`NullRunError` because
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


class WorkflowKilledInterrupt(WorkflowKilledException):
    """
    Raised when a workflow is killed by the NullRun control plane.

    Inherits from the deprecated :class:`WorkflowKilledException`
    (which is itself a ``BaseException`` subclass, not ``Exception``)
    so that:

      * ``except WorkflowKilledInterrupt`` (new code) catches new raises
        and only new raises.
      * ``except WorkflowKilledException`` (legacy user code) still
        catches new raises — back-compat.
      * ``except Exception`` does **not** catch this signal — kill is
        not a recoverable error. Mirrors the ``KeyboardInterrupt`` /
        ``SystemExit`` pattern from the standard library: user code
        that catches ``except Exception`` and re-runs the work will
        silently bypass the kill.
      * ``except BaseException`` catches it, like the stdlib interrupts.

    See ``docs/kill-contract.md` §6 for the full rationale, including
    the four-level coverage model and the decision tree for users.

    Fields:
        workflow_id:  The workflow that was killed.
        reason:       Server-supplied reason (e.g. "killed via API",
                      "budget exhausted", "circuit-breaker tripped").

    Catching in production
    ----------------------
    ``WorkflowKilledInterrupt`` is a ``BaseException`` subclass
    (NOT ``Exception``), so a user-agent ``try / except Exception``
    will not catch it. This is intentional — the kill signal
    must reach the top of the loop. It does mean, however, that
    Sentry / OpenTelemetry default error handlers (which filter
    on ``Exception``) will not record the kill event unless the
    user's code re-raises it under an ``except BaseException``:

        from sentry_sdk import capture_exception
        try:
            agent.run()
        except BaseException:
            capture_exception()  # records kill, ctrl-c, system-exit
            raise

    ``except Exception`` will swallow non-kill errors but let the
    kill through. ``except BaseException`` captures everything
    including the kill — recommended for the top of an agent loop.
    """

    def __init__(self, workflow_id: str, reason: str) -> None:
        # Bypass the parent's __init__ so constructing the canonical
        # class does NOT trigger the parent's DeprecationWarning. The
        # deprecation is about using the old *name* — not the
        # BaseException-based hierarchy.
        self.workflow_id = workflow_id
        self.reason = reason
        BaseException.__init__(self, f"Workflow {workflow_id} killed: {reason}")
