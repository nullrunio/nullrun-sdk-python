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

If a future use case appears (e.g. per-API-key rate isolation)
re-introduce the contextvars AND a setter API (token-based like
``set_attempt_index``) AND wire them in ``NullRunRuntime.__init__``
from the ``_authenticate`` response.
"""

import uuid
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar, Token

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

# 2026-07-02 (v0.11.0): chain_id contextvar for soft-mode gate
#.
#
# Soft-mode budget enforcement ONLY allows overdrafts when an
# active chain is registered against the org. The SDK must forward
# the active chain_id on every /check request so the backend can
# find the chain in Redis. Storing the chain_id as a contextvar
# (rather than threading it through every @protect call) means
# user code does not have to manage the chain lifecycle explicitly
# — the ``with chain("agent-loop")`` contextmanager below handles
# set + reset.
_chain_id_var: ContextVar[str | None] = ContextVar("chain_id", default=None)
_chain_op_var: ContextVar[str] = ContextVar(
    "chain_op", default="auto"
)  # "auto" | "start" | "continue" | "end"


# =============================================================================
# Workflow / trace getters
# =============================================================================


def get_workflow_id() -> str | None:
    """Get current workflow ID from context."""
    return _workflow_id_var.get()


def get_trace_id() -> str | None:
    """Get current trace ID from context."""
    return _trace_id_var.get()


def set_trace_id(trace_id: str | None) -> object:
    """Pin the current trace_id on the context.

    Used by ``@protect`` blocks and by the langgraph callback
    during ``on_chain_start`` to give downstream cost events a
    stable parent-trace reference. Returns a token that the caller
    passes to :func:`reset_trace_id` to restore the previous value
    — this is the ``ContextVar`` contract, see
    https://docs.python.org/3/library/contextvars.html#contextvars.ContextVar.set.

    Passing ``None`` clears the field. Tests should pair this with
    a try/finally ``reset_trace_id`` to avoid bleeding state into
    the next test (we observed this as the root cause of the
    2026-07-11 cross-test WAL-replay flake).
    """
    return _trace_id_var.set(trace_id)


def reset_trace_id(token: object) -> None:
    """Restore the previous trace_id state from a ``set_trace_id``
    token. See :func:`set_trace_id`."""
    _trace_id_var.reset(token)  # type: ignore[arg-type]


def clear_trace_id() -> None:
    """Clear the trace_id contextvar to its default (None).

    Convenience for tests + teardown paths that do not need to
    capture the previous value. Equivalent to
    ``set_trace_id(None)`` but with no return token to manage.
    """
    _trace_id_var.set(None)


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


# ---------------------------------------------------------------------------
# Chain context (v0.11.0 — )
# ---------------------------------------------------------------------------
def get_chain_id() -> str | None:
    """Return the active chain_id, or ``None`` when no chain is in
    scope.

    Read by ``Transport.check_v3`` (and the legacy ``check`` /
    ``check_workflow_budget`` paths) so the backend can decide
    whether to allow soft-mode budget overdrafts. ``None`` means
    single-shot Hard mode — the gate is binary (budget or no).
    """
    return _chain_id_var.get()


def get_chain_op() -> str:
    """Return the chain operation for the next /check call.

    One of ``"auto"`` (default — auto-register if chain_id present
    else no-op), ``"start"``, ``"continue"``, ``"end"``. Maps to the
    backend's ``chain_op`` field on ``/api/v1/check``.
    """
    return _chain_op_var.get()


def set_chain_id(chain_id: str | None) -> None:
    """Manually set the active chain_id (advanced; prefer ``with chain(...)``).

    Setting ``None`` clears the chain context — subsequent /check
    calls become single-shot Hard. The setter does NOT issue a
    /chain/end — call ``nullrun.chain_end(chain_id)`` explicitly
    when you want to close the chain on the server.
    """
    _chain_id_var.set(chain_id)


def set_chain_op(op: str) -> None:
    """Manually set the chain_op for the next /check call.

    Valid values: ``"auto"`` (default), ``"start"``, ``"continue"``
    ``"end"``. Mirrors the wire-contract enum in 
    decision matrix. Use ``"start"`` to force REGISTERED-state
    semantics on the next call (no auto-register); use ``"end"``
    on a /check to close the chain in the same atomic operation
    as the gate (avoids the extra round-trip).
    """
    _chain_op_var.set(op)


# ---------------------------------------------------------------------------
# Server-minted execution_id (2026-07-04 — )
# ---------------------------------------------------------------------------
#
# Pre-0.12.0 the SDK sent a client-supplied ``execution_id`` (usually
# ``workflow_id``) in /check requests and IGNORED the server's response.
# This left two problems:
#
# 1. ownership — the backend's `gate_reserve_v3`
# generates a uuidv7 internally, persists
# ``execution:{execution_id}`` (24h TTL) and creates
# ``reservation:{execution_id}`` (300s TTL). The client-minted
# id never matched, so on the v3 path the gate rejected /track
# with 503 RESERVATION_NOT_FOUND — fail-CLOSED.
#
# 2. idempotency — /track's ``idempotency_key``
# contract depends on the server-minted UUID being reused
# on retry. Without picking it up at /check the SDK has no
# way to compute a stable key.
#
# Fix: capture the ``reservation_id`` field from the /check
# response into this contextvar. The runtime sets it on every
# successful /check; the runtime's ``_enrich_event`` reads it on
# the way out and tags the /track payload with ``execution_id``.
#
# Lifetime: scoped automatically by ``with workflow(...)`` /
# ``with chain(...)`` — the runtime resets the contextvar on
# block exit so a /check in one block never leaks into a /track
# in a sibling block. Tests can drive it manually with
# ``set_/reset_server_minted_execution_id`` (Token-based API
# mirrors the user-facing audit spec; ``clear_`` is a
# no-token convenience for the runtime's ``_enrich_event``
# after a /track has been issued).
#
# The reservation TTL (300s) is shorter than the chain id's 24h
# binding TTL, so we also record the capture timestamp —
# ``get_server_minted_reservation_at`` returns ``time.monotonic ``
# at the moment /check returned 200. The runtime ignores the
# contextvar when the age exceeds 295s (5s margin below the
# 300s backend reservation TTL) so an exceptionally long LLM
# call never ships a doomed ``execution_id``.
_server_minted_execution_id_var: ContextVar[str | None] = ContextVar(
    "server_minted_execution_id", default=None
)
_server_minted_reservation_at_var: ContextVar[float] = ContextVar(
    "server_minted_reservation_at", default=0.0
)
# 2026-07-04: /track idempotency anchor.
# The /check request carries ``idempotency_key = operation_id`` (UUID v4)
# the backend's /track handler (handlers.rs:4654-4725) accepts the same
# key and replays the original response on hit (200 + ``idempotent_replay:
# true``). Without forwarding the key from /check onto the /track payload
# a transport-level retry on the SAME event either re-runs CONSUME_SCRIPT
# (→ 503 RESERVATION_NOT_FOUND, since the reservation key was DEL'ed by
# the first successful consume per) or double-bills.
#
# Captured into a contextvar at the same instant as
# ``server_minted_execution_id`` so the two values always refer to the
# same /check. ``None`` when the /check didn't supply one (legacy or
# capability-disabled backend) — the /track payload then omits the field.
_server_minted_idempotency_key_var: ContextVar[str | None] = ContextVar(
    "server_minted_idempotency_key", default=None
)


def get_server_minted_execution_id() -> str | None:
    """Return the server-minted execution_id from the last /check, or
    ``None`` if none captured in scope.

    Read by ``NullRunRuntime._enrich_event`` to tag the /track
    payload. ``None`` is the legacy / v1-v2 path — the wire spec
    allows the field to be omitted when the backend has not
    minted one (capability ``server_minted_execution_id=False``).
    """
    return _server_minted_execution_id_var.get()


def get_server_minted_reservation_at() -> float:
    """Return ``time.monotonic `` at the moment of /check capture
    or ``0.0`` if no capture in scope.

    Used by ``NullRunRuntime._enrich_event`` to refuse a /track
    whose /check has aged past the v3 reservation TTL (300s —
    ). The runtime captures the timestamp at the
    same instant the id is captured, so the two values always
    refer to the same /check.
    """
    return _server_minted_reservation_at_var.get()


def get_server_minted_idempotency_key() -> str | None:
    """Return the /check ``idempotency_key`` for the in-scope
    reservation, or ``None`` if none captured.

    Read by ``NullRunRuntime._enrich_event`` to tag the /track
    v3 single-event payload. The /check request sets
    ``idempotency_key = operation_id`` (a UUID v4) at
    runtime.py:1260; the /track handler honors it for replay
.

    Pairs with:func:`get_server_minted_execution_id` and shares
    the same capture token; ``None`` on the legacy v1/v2 path.
    """
    return _server_minted_idempotency_key_var.get()


def set_server_minted_execution_id(value: str | None) -> Token[str | None]:
    """Capture the server-minted execution_id returned by /check.

    Returns the ``Token`` so the caller can restore the previous
    value via:func:`reset_server_minted_execution_id`. The
    runtime drives the lifetime explicitly (it owns the
    capture/reset cycle around the user-function call) — user
    code does not need to call this directly.

    Args:
        value: UUID v7 string returned on ``GateResponse.
            reservation_id`` (server-minted per). Pass
            ``None`` to clear (e.g. on a hard block response
            which carries no reservation_id).
    """
    return _server_minted_execution_id_var.set(value)


def set_server_minted_reservation_at(value: float) -> Token[float]:
    """Capture the ``time.monotonic `` instant corresponding to
    ``set_server_minted_execution_id``.

    Called by the runtime immediately after:func:`set_server_minted_execution_id`
    so the two timestamps stay in lockstep. Returns the matching
    Token for symmetric:func:`reset_server_minted_reservation_at`.
    """
    return _server_minted_reservation_at_var.set(value)


def set_server_minted_idempotency_key(value: str | None) -> Token[str | None]:
    """Capture the /check ``idempotency_key`` (the operation_id UUID v4
    on the v3 path) alongside the matching execution_id.

    Lifetime is symmetric with
:func:`set_server_minted_execution_id` — the runtime captures
    both at the same instant and resets both at the matching
    /track emission (or workflow/chain block exit). Returns the
    matching Token.
    """
    return _server_minted_idempotency_key_var.set(value)


def reset_server_minted_execution_id(token: Token[str | None]) -> None:
    """Restore the previous server-minted execution_id value.

    Pair with:func:`set_server_minted_execution_id`. The runtime
    stores the token at capture time and resets it on the matching
    /track emission (or at workflow/chain block exit, whichever
    comes first).
    """
    _server_minted_execution_id_var.reset(token)


def reset_server_minted_reservation_at(token: Token[float]) -> None:
    """Restore the previous reservation capture timestamp.

    Pair with:func:`set_server_minted_reservation_at`.
    """
    _server_minted_reservation_at_var.reset(token)


def reset_server_minted_idempotency_key(token: Token[str | None]) -> None:
    """Restore the previous /check idempotency_key value.

    Pair with:func:`set_server_minted_idempotency_key`.
    """
    _server_minted_idempotency_key_var.reset(token)


def clear_server_minted_execution_id() -> None:
    """Erase the captured server-minted execution_id + timestamp.

    No-token convenience for the runtime's "block exited, drop the
    capture" code path. Equivalent to::

        _server_minted_execution_id_var.set(None)
        _server_minted_reservation_at_var.set(0.0)
        _server_minted_idempotency_key_var.set(None)

    Use:func:`reset_server_minted_execution_id` instead when you
    have a Token to consume — that path restores the previous
    scope's value, ``clear_`` strictly forgets it.
    """
    _server_minted_execution_id_var.set(None)
    _server_minted_reservation_at_var.set(0.0)
    _server_minted_idempotency_key_var.set(None)


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
    (the handler's `Uuid::parse_str(...).ok ` returned None).
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
    All track calls within this context automatically use this workflow_id.

    Usage:
        from nullrun import workflow

        with workflow("my-agent"):
            # All events here auto-tagged with workflow_id
            track({"type": "llm_call",...})
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
    # a new workflow gets a fresh span_id too. The
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
                track({"type": "llm_call",...})
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
    All track calls within this context automatically use this agent_id
    for per-agent cost attribution.

    Usage:
        from nullrun import workflow, agent, track

        with workflow("my-workflow"):
            with agent("my-agent"):
                # All events here auto-tagged with agent_id
                track({"type": "llm_call",...})
                agent.invoke(...)

    Args:
        name: Optional agent name/ID. Auto-generated if not provided.

    Yields:
        The agent_id string
    """
    # P2-4 / S-8: emit a real UUID4 with dashes (matching
    # ``generate_trace_id`` / ``generate_span_id``). The previous
    # ``f"agent-{uuid.uuid4.hex}"`` format was 32 hex chars
    # without dashes; backend UUID-typed columns (cost_events.
    # agent_id, audit_log) silently dropped these to NULL on insert
    # (``Uuid::parse_str(...).ok `` returned None). User-supplied
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
    All track calls within this context automatically include the attempt_index
    for linking retries to the same ExecutionAttempt in the backend.

    Usage:
        from nullrun import workflow, attempt, track

        with workflow("my-workflow"):
            for attempt_index in range(retries):
                with attempt(attempt_index):
                    track({"type": "llm_call",...})
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


# 2026-07-02 (v0.11.0): chain context manager for soft-mode budget
# enforcement.
#
# Usage:
#
# import nullrun
# import uuid
#
# chain_id = str(uuid.uuid4 )
# with nullrun.chain(chain_id, op="start"):
# # The first @protect call inside this block issues
# # /api/v1/check with chain_id + chain_op="start".
# # Subsequent calls extend the chain's TTL on the server.
# agent.run_long_loop
# # On exit, the SDK does NOT issue /chain/end automatically —
# # the server's idle TTL (300s) cleans up if no /check lands.
# # To close explicitly: nullrun.chain_end(chain_id).
#
# Pair with ``runtime.ping_chain(chain_id, interval=30.0)`` for
# long-running streams where you want to extend the TTL faster than
# the natural /check cadence.
@contextmanager
def chain(
    chain_id: str,
    op: str = "start",
) -> Generator[str, None, None]:
    """Context manager for chain scope.

    Args:
        chain_id: UUID v4 (or any unique string) identifying this
            chain. Persists in Redis with idle TTL 300s; auto-extended
            by every /check inside the block.
        op: Chain operation for the FIRST /check call inside the
            block. ``"start"`` creates REGISTERED-state, ``"continue"``
            extends TTL (auto-recover if the chain was lost)
            ``"end"`` closes the chain on the same call. Subsequent
            calls inside the block always send ``op="continue"``.

    Yields:
        The chain_id (so callers can ``as cid`` for symmetry with
        ``workflow ``).
    """
    if op not in ("start", "continue", "end", "auto"):
        raise ValueError(
            f"chain() op must be one of start/continue/end/auto, got {op!r}"
        )
    chain_token = _chain_id_var.set(chain_id)
    op_token = _chain_op_var.set(op)
    try:
        yield chain_id
    finally:
        _chain_id_var.reset(chain_token)
        _chain_op_var.reset(op_token)
