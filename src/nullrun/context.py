"""
Context management for NullRun SDK.

Provides workflow and trace context for automatic event correlation.
"""

import uuid
import warnings
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

# Context variables for tenant isolation and workflow/trace propagation
_organization_id_var: ContextVar[str | None] = ContextVar("organization_id", default=None)
_api_key_id_var: ContextVar[str | None] = ContextVar("api_key_id", default=None)
_workflow_id_var: ContextVar[str | None] = ContextVar("workflow_id", default=None)
_trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)
_span_id_var: ContextVar[str | None] = ContextVar("span_id", default=None)
_agent_id_var: ContextVar[str | None] = ContextVar("agent_id", default=None)
_attempt_index_var: ContextVar[int] = ContextVar("attempt_index", default=0)


# =============================================================================
# Tenant Context Getters/Setters (for structured logging isolation)
# =============================================================================


def get_org_id() -> str | None:
    """Get current organization ID from context."""
    warnings.warn(
        "get_org_id() is deprecated, use get_organization_id() instead",
        DeprecationWarning,
        stacklevel=2,
    )
    return _organization_id_var.get()


def get_organization_id() -> str | None:
    """Get current organization ID from context."""
    return _organization_id_var.get()


def get_api_key_id() -> str | None:
    """Get current API key ID from context."""
    return _api_key_id_var.get()


def set_tenant_context(organization_id: str | None = None, api_key_id: str | None = None) -> None:
    """Set tenant context for logging isolation.

    Args:
        organization_id: Organization ID (replaces workspace_id)
        api_key_id: API key ID
    """
    if organization_id is not None:
        _organization_id_var.set(organization_id)
    if api_key_id is not None:
        _api_key_id_var.set(api_key_id)


@contextmanager
def tenant_context(organization_id: str, api_key_id: str | None = None) -> Generator[str, None, None]:
    """
    Context manager for tenant scope (for structured logging isolation).

    All SDK log records within this context automatically include tenant fields.

    Usage:
        from nullrun.context import tenant_context

        with tenant_context("org-123", "key-789"):
            # All logs here include organization_id, api_key_id
            logger.info("Processing event")
            track({"type": "llm_call", ...})

    Args:
        organization_id: Organization ID
        api_key_id: Optional API key ID

    Yields:
        The organization ID
    """
    token_org_id = _organization_id_var.set(organization_id)
    token_key = _api_key_id_var.set(api_key_id) if api_key_id else None

    try:
        yield organization_id
    finally:
        _organization_id_var.reset(token_org_id)
        if token_key is not None:
            _api_key_id_var.reset(token_key)


def get_workflow_id() -> str | None:
    """Get current workflow ID from context."""
    return _workflow_id_var.get()


def get_trace_id() -> str | None:
    """Get current trace ID from context."""
    return _trace_id_var.get()


def get_span_id() -> str | None:
    """Get current span ID from context."""
    return _span_id_var.get()


def get_agent_id() -> str | None:
    """Get current agent ID from context."""
    return _agent_id_var.get()


def get_attempt_index() -> int:
    """Get current attempt index from context (for retry correlation)."""
    return _attempt_index_var.get()


def set_attempt_index(index: int) -> None:
    """Set current attempt index for retry correlation."""
    _attempt_index_var.set(index)


def generate_trace_id() -> str:
    """Generate a new trace ID.

    Returns a real UUID4 (e.g. ``95ca7c0b-8334-478a-af23-2788803ef3b8``).
    The backend's `cost_events.trace_id` is uuid-typed, so the wire
    value has to parse as a UUID — earlier we shipped
    ``f"trace-{hex[:16]}"`` which silently dropped to NULL on insert
    (the handler's `Uuid::parse_str(...).ok()` returned None).
    """
    return str(uuid.uuid4())


def generate_span_id() -> str:
    """Generate a new span ID. Real UUID4 — see generate_trace_id."""
    return str(uuid.uuid4())


@contextmanager
def workflow(name: str | None = None) -> Generator[str, None, None]:
    """
    Context manager for workflow scope.

    Sets up a new workflow context with auto-generated or provided workflow_id.
    All track() calls within this context automatically use this workflow_id.

    Usage:
        from nullrun import workflow

        with workflow("my-agent"):
            # All events here auto-tagged with workflow_id
            track({"type": "llm_call", ...})
            agent.invoke(...)

    Args:
        name: Optional workflow name. Auto-generated if not provided.

    Yields:
        The workflow_id string
    """
    workflow_id = name or f"wf-{uuid.uuid4().hex}"
    trace_id = generate_trace_id()

    # Save current values
    wf_token = _workflow_id_var.set(workflow_id)
    trace_token = _trace_id_var.set(trace_id)

    try:
        yield workflow_id
    finally:
        # Restore previous values
        _workflow_id_var.reset(wf_token)
        _trace_id_var.reset(trace_token)


@contextmanager
def span(name: str | None = None) -> Generator[str, None, None]:
    """
    Context manager for a span within a workflow.

    Usage:
        with workflow("my-agent"):
            with span("llm-call"):
                result = llm.invoke(prompt)
                track({"type": "llm_call", ...})
    """
    span_id = name or generate_span_id()
    token = _span_id_var.set(span_id)

    try:
        yield span_id
    finally:
        _span_id_var.reset(token)


@contextmanager
def agent(name: str | None = None) -> Generator[str, None, None]:
    """
    Context manager for agent scope within a workflow.

    Sets up an agent context with auto-generated or provided agent_id.
    All track() calls within this context automatically use this agent_id
    for per-agent cost attribution.

    Usage:
        from nullrun import workflow, agent, track

        with workflow("my-workflow"):
            with agent("my-agent"):
                # All events here auto-tagged with agent_id
                track({"type": "llm_call", ...})
                agent.invoke(...)

    Args:
        name: Optional agent name/ID. Auto-generated if not provided.

    Yields:
        The agent_id string
    """
    agent_id = name or f"agent-{uuid.uuid4().hex}"
    token = _agent_id_var.set(agent_id)

    try:
        yield agent_id
    finally:
        _agent_id_var.reset(token)


@contextmanager
def attempt(attempt_index: int) -> Generator[int, None, None]:
    """
    Context manager for attempt scope within a workflow (retry correlation).

    Sets up an attempt context for correlating retries in execution attempts.
    All track() calls within this context automatically include the attempt_index
    for linking retries to the same ExecutionAttempt in the backend.

    Usage:
        from nullrun import workflow, attempt, track

        with workflow("my-workflow"):
            for attempt_index in range(retries):
                with attempt(attempt_index):
                    track({"type": "llm_call", ...})
                    llm.invoke(prompt)

    Args:
        attempt_index: The attempt index (0 = first attempt, 1 = first retry, etc.)

    Yields:
        The attempt_index
    """
    token = _attempt_index_var.set(attempt_index)
    try:
        yield attempt_index
    finally:
        _attempt_index_var.reset(token)


class WorkflowContext:
    """
    Manual workflow context manager (alternative to `with workflow()`).

    Useful when you need to manage lifecycle explicitly.
    """

    def __init__(self, name: str | None = None):
        self.workflow_id = name or f"wf-{uuid.uuid4().hex}"
        self._token = None

    def __enter__(self) -> "WorkflowContext":
        self._token = _workflow_id_var.set(self.workflow_id)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._token is not None:
            _workflow_id_var.reset(self._token)
        return False
