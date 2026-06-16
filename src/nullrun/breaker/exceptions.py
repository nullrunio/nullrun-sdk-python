from enum import Enum
from typing import Any


class BreakerError(Exception):
    """Base exception for Breaker SDK."""
    pass


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


class NullRunTransportError(BreakerError):
    """Raised by transport layer when the policy engine is unreachable.

    The exception carries a `source` (TransportErrorSource) and the
    `endpoint` that failed, so callers can implement endpoint-specific
    recovery (e.g. fail-CLOSED for sensitive tools, fail-OPEN for
    budget pre-checks) per ADR-008.

    Replaces the previous behavior of swallowing the failure and
    returning a synthetic `allow` / `block` response — that hid
    the policy-engine outage from operators and was the root cause
    of bug #1 / #2 fixed in ADR-008.
    """
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
        super().__init__(
            f"Transport error on {endpoint}: {message} "
            f"(source={source.value}, details={details})"
        )


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

    Applications should implement retry logic or alerting mechanism when this exception
    is raised, as budget protection may be compromised.
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


class NullRunAuthenticationError(BreakerError):
    """
    Raised when authentication fails and safe mode is required.

    This exception indicates that the SDK could not authenticate with
    the NullRun backend and will not operate in unprotected mode.
    Applications should handle this exception and provide valid credentials.
    """
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class CostLimitExceeded(BreakerError):
    """Raised when workflow cost exceeds limit."""

    def __init__(self, workflow_id: str, cost: float, limit: float):
        self.workflow_id = workflow_id
        self.cost = cost
        self.limit = limit
        super().__init__(f"Workflow {workflow_id} cost ${cost:.2f} exceeds limit ${limit:.2f}")


class ApprovalRequired(BreakerError):
    """Raised when destructive action requires human approval."""

    def __init__(self, workflow_id: str, action: str, request_id: str):
        self.workflow_id = workflow_id
        self.action = action
        self.request_id = request_id
        super().__init__(
            f"Workflow {workflow_id} requires approval for {action}. "
            f"Request ID: {request_id}"
        )


class BreakerTimeout(BreakerError):
    """Raised when request times out."""
    pass


class NullRunBlockedException(BreakerError):
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
            `None` when the block is workflow-scoped rather than
            tool-scoped.
        details: Free-form structured payload forwarded by the caller.
    """
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
        super().__init__(
            f"Workflow {workflow_id} blocked: {reason} "
            f"(action={action}{tool_suffix}, details={details})"
        )


class LoopDetectedException(NullRunBlockedException):
    """Raised when infinite loop is detected."""

    def __init__(self, workflow_id: str, tool_name: str, count: int):
        super().__init__(
            workflow_id=workflow_id,
            reason=f"Loop detected: {tool_name} called {count}x",
            action="kill",
            tool_name=tool_name,
            count=count,
        )


class RetryStormException(NullRunBlockedException):
    """Raised when excessive retries are detected."""

    def __init__(self, workflow_id: str, count: int):
        super().__init__(
            workflow_id=workflow_id,
            reason=f"Retry storm detected: {count} retries",
            action="kill",
            count=count,
        )


class RateLimitExceededException(NullRunBlockedException):
    """Raised when rate limit is exceeded."""

    def __init__(self, workflow_id: str, rate: float, limit: float):
        super().__init__(
            workflow_id=workflow_id,
            reason=f"Rate limit exceeded: {rate}/min > {limit}/min",
            action="pause",
            rate=rate,
            limit=limit,
        )


class WorkflowPausedException(BreakerError):
    """
    Raised when workflow is paused by NullRun.

    This allows the workflow to be resumed later after
    human approval or automatic cooldown.
    """

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
    ``except WorkflowKilledInterrupt``, or, if recovery is impossible,
    let the exception propagate to the top of the loop.

    This class is **not** an ``Exception`` subclass — kill is a
    non-recoverable signal and should not be caught by generic
    ``except Exception`` clauses. Only ``except BaseException`` or the
    explicit ``except WorkflowKilledInterrupt`` reliably stops the work.
    See ``docs/kill-contract.md`` §6 for the full rationale.
    """

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

    See ``docs/kill-contract.md`` §6 for the full rationale, including
    the four-level coverage model and the decision tree for users.

    Fields:
        workflow_id:  The workflow that was killed.
        reason:       Server-supplied reason (e.g. "killed via API",
                      "budget exhausted", "circuit-breaker tripped").
    """

    def __init__(self, workflow_id: str, reason: str) -> None:
        # Bypass the parent's __init__ so constructing the canonical
        # class does NOT trigger the parent's DeprecationWarning. The
        # deprecation is about using the old *name* — not the
        # BaseException-based hierarchy.
        self.workflow_id = workflow_id
        self.reason = reason
        BaseException.__init__(self, f"Workflow {workflow_id} killed: {reason}")
