"""
Context management for NullRun SDK.

Provides workflow and trace context for automatic event correlation.

Sprint 2.7 (B27): the previously-defined ``_organization_id_var`` /
``_api_key_id_var`` contextvars and the ``get_organization_id`` /
``get_api_key_id`` getters were removed because:
  1. No code path ever wrote to them — both getters always
     returned ``None``.
  2. ``observability.TenantFilter`` (the only consumer) was
     removed in 0.3.1.
  3. The structured-logging tenant-isolation feature moved to
     the backend in the same release.

If a future use case appears (e.g. per-API-key rate isolation),
re-introduce the contextvars AND a setter API (token-based like
``set_attempt_index``) AND wire them in ``NullRunRuntime.__init__``
from the ``_authenticate`` response.
"""

import uuid
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

# Context variables for workflow/trace propagation.
_workflow_id_var: ContextVar[str | None] = ContextVar("workflow_id", default=None)
_trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)
_span_id_var: ContextVar[str | None] = ContextVar("span_id", default=None)
_agent_id_var: ContextVar[str | None] = ContextVar("agent_id", default=None)
_attempt_index_var: ContextVar[int] = ContextVar("attempt_index", default=0)

# T4 (2026-06-27): per-call context that flows into the /gate pre-flight
# request so the backend can compute projected_cost and tool_block
# decisions from real data instead of the previous fake "budget-precheck"
# sentinel. Both default to None/empty; users opt in by calling
# ``set_call_context(model=..., tools=[...])`` inside a ``with workflow(...)``
# block. When unset, the backend falls back to its default pricing and
# skips tool-block enforcement on /gate (per-key tool_block is enforced
# on /track only — see gate/internal.rs T3).
_call_model_var: ContextVar[str | None] = ContextVar("call_model", default=None)
_call_tools_var: ContextVar[tuple[str, ...]] = ContextVar("call_tools", default=())


# =============================================================================
# Workflow / trace getters
# =============================================================================


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


def get_call_model() -> str | None:
    """Get the LLM model name set via ``set_call_context``.

    Used by ``check_workflow_budget`` to send the real model to the
    backend's /gate endpoint instead of the previous fake
    ``"budget-precheck"`` placeholder (which forced the backend's
    pricing model to fall through to the default rate and broke any
    future per-model budget tiers).
    """
    return _call_model_var.get()


def get_call_tools() -> tuple[str, ...]:
    """Get the tool names set via ``set_call_context``.

    Used by ``check_workflow_budget`` so the backend's tool_block
    enforcement (when added in T3) can match against the workflow's
    configured ``blocked_tools`` aggregate.
    """
    return _call_tools_var.get()


def set_attempt_index(index: int) -> None:
    """Set current attempt index for retry correlation."""
    _attempt_index_var.set(index)


def set_call_context(
    model: str | None = None,
    tools: list[str] | tuple[str, ...] | None = None,
) -> None:
    """Set per-call context (model name, tool list) for the next /gate
    pre-flight check.

    T4 (2026-06-27): replaces the previous fake ``model="budget-precheck"``
    and ``estimated_tokens=1`` always-default / always-empty pre-flight.
    Call inside a ``with workflow(...)`` block before ``@protect`` to
    give the backend real data.

    Args:
        model: LLM model name (e.g. ``"claude-sonnet-4-6"``). Backend
            uses this to look up the per-model rate from
            ``tool_pricing`` (Postgres) so projected_cost matches what
            /track will compute from real token counts.
        tools: List of tool names the call intends to use. Backend
            matches each against the workflow's effective
            ``blocked_tools`` aggregate (T3 in backend) and returns
            block on any match. Pass ``None`` to leave whatever was
            previously set, ``[]`` to clear.
    """
    if model is not None:
        _call_model_var.set(model)
    if tools is not None:
        _call_tools_var.set(tuple(tools))


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
    # Phase 5 #5.6: emit a real UUID4 with dashes (matching
    # ``generate_trace_id``). The previous ``wf-{hex32}`` format
    # was inconsistent with the rest of the SDK's id generation.
    workflow_id = name or str(uuid.uuid4())
    trace_id = generate_trace_id()
    # §7.2 #16: a new workflow gets a fresh span_id too. The
    # pre-fix code only reset workflow_id and trace_id, so a
    # ``with span("inner"); with workflow("outer")`` block would
    # leave the inner span_id visible inside the workflow scope —
    # the span emitted by the workflow would carry the wrong
    # parent. We set a new span_id here so the audit log can
    # correctly nest the workflow's own span_start under the
    # workflow_id (rather than under some earlier span that
    # happened to be on the contextvar stack).
    span_id = generate_span_id()

    # Save current values
    wf_token = _workflow_id_var.set(workflow_id)
    trace_token = _trace_id_var.set(trace_id)
    span_token = _span_id_var.set(span_id)

    try:
        yield workflow_id
    finally:
        # Restore previous values
        _workflow_id_var.reset(wf_token)
        _trace_id_var.reset(trace_token)
        _span_id_var.reset(span_token)


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
    # P2-4 / S-8: emit a real UUID4 with dashes (matching
    # ``generate_trace_id`` / ``generate_span_id``). The previous
    # ``f"agent-{uuid.uuid4().hex}"`` format was 32 hex chars
    # without dashes; backend UUID-typed columns (cost_events.
    # agent_id, audit_log) silently dropped these to NULL on insert
    # (``Uuid::parse_str(...).ok()`` returned None). User-supplied
    # ``name`` is preserved verbatim so existing dashboards continue
    # to work for already-allocated agent ids.
    agent_id = name or str(uuid.uuid4())
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
