"""
Context management for NullRun SDK.

Provides workflow and trace context for automatic event correlation.

The previously-defined ``_organization_id_var`` / ``_api_key_id_var``
contextvars and the ``get_organization_id`` / ``get_api_key_id``
getters were removed (B27) because:
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

# 2026-08-14 (F-19 fix): ``nullrun.tracing`` provides the structured
# SpanContext that models the parent/child hierarchy a trace timeline
# needs. ``nullrun.context`` previously owned loose ``_trace_id`` /
# ``_span_id`` contextvars and now keeps them in lockstep via the
# ``_mirror_to_span_context`` / ``_mirror_to_legacy_span`` helpers
# below; ``@protect`` (decorators.py:441) and any other writer must
# call BOTH sides so runtime readers (``get_trace_id`` /
# ``get_span_id``) and SpanContext readers (``get_current_span``) see
# the same trace id. See audit_ui/UI-UX-AUDIT-REPORT.md F-19.
from .tracing import (
    SpanContext,
    _current_span,
    reset_span,
    set_span,
)

# Context variables for workflow/trace propagation.
_workflow_id_var: ContextVar[str | None] = ContextVar("workflow_id", default=None)
_trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)
_span_id_var: ContextVar[str | None] = ContextVar("span_id", default=None)
_agent_id_var: ContextVar[str | None] = ContextVar("agent_id", default=None)
_attempt_index_var: ContextVar[int] = ContextVar("attempt_index", default=0)

# Per-call context that flows into the /gate pre-flight request so
# the backend can compute projected_cost and tool_block decisions
# from real data instead of the previous fake "budget-precheck"
# sentinel. Both default to None/empty; users opt in by calling
# ``set_call_context(model=..., tools=[...])`` inside a ``with workflow(...)``
# block. When unset, the backend falls back to its default pricing and
# skips tool-block enforcement on /gate (per-key tool_block is
# enforced on /track only).
_call_model_var: ContextVar[str | None] = ContextVar("call_model", default=None)
_call_tools_var: ContextVar[tuple[str, ...]] = ContextVar("call_tools", default=())
# Per-call MCP tool class + annotations. Set via the
# ``set_mcp_tool_context`` helper when the SDK recognises an MCP
# server. The gate honors `tool_class` over its own
# `classify_tool(tool_name)` parse, and uses `mcp_annotations` to
# evaluate `mcp_destructive_policy` / `mcp_readonly_policy`.
# ``None`` means "I don't know" — the gate treats absent values
# as unknown (NOT as false), so a server that forgets to set
# annotations cannot accidentally get a read-only bypass.
_call_mcp_class_var: ContextVar[str | None] = ContextVar("call_mcp_class", default=None)
_call_mcp_annotations_var: ContextVar[dict[str, bool | None] | None] = ContextVar(
    "call_mcp_annotations", default=None
)

# 2026-07-02 (v0.11.0): chain_id contextvar for soft-mode gate
# .
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


def get_call_mcp_class() -> str | None:
    """Canonical tool class for the next ``/check`` call.

    One of ``"builtin" | "mcp" | "custom" | "invalid"``. Set via
    ``set_mcp_tool_context`` when the SDK recognises an MCP server
    (curl `tools/list` once per cache window, then forward on every
    ``/check``). ``None`` means "I don't know — derive from the
    raw ``tool`` string on the server".
    """
    return _call_mcp_class_var.get()


def get_call_mcp_annotations() -> dict[str, bool | None] | None:
    """Per-tool MCP annotations for the next ``/check`` call.

    Mirrors the MCP spec's ``tools/list`` ``annotations`` object —
    keys ``read_only``, ``destructive``, ``open_world``, each
    optional ``bool`` or ``None``. ``None`` means "I have no
    opinion" — the gate treats the value as unknown.
    """
    return _call_mcp_annotations_var.get()


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

    Per CLAUDE.md §6 the chain_id field MUST be a UUID v4. The
    setter validates the format (length, canonical UUID
    structure, version=4) and raises ``ValueError`` on
    malformed input. ``None`` is accepted (clears the context).
    """
    if chain_id is not None:
        _validate_chain_id(chain_id)
    _chain_id_var.set(chain_id)


def _validate_chain_id(chain_id: str) -> None:
    """Validate ``chain_id`` is a UUID v4 string per CLAUDE.md §6.

    The backend owns the race guard (``HGET chain_key 'org_id'`` per
    §6 Q2) but does NOT validate the chain_id format — non-UUID or
    malformed chain_ids silently auto-register as new ACTIVE
    chains. The SDK is the authoritative client-side validator;
    failing fast here surfaces typos and predictable-UUID attacks
    before they hit the network.

    Args:
        chain_id: The candidate chain_id string.

    Raises:
        ValueError: If ``chain_id`` is not a syntactically valid
            UUID v4 string. The error message includes the
            offending value (truncated for readability) and the
            specific reason (parse failure / non-v4 version).

    Why UUID v4 and not v7 / v1: per CLAUDE.md §6 the chain_id is
    server-generated and sent back to the SDK for hash-chain
    integrity (the chain_id is the second key in the
    `chain:{org_id}:{chain_id}` Redis hash). UUID v4 has the
    lowest collision probability at 2^122 bits of randomness and
    is the canonical format the backend has used since v0.11.0.
    Future versions MAY migrate to v7 (time-ordered) but require
    a wire-contract bump + cross-SDK migration.
    """
    try:
        parsed = uuid.UUID(chain_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(
            f"chain_id must be a syntactically valid UUID v4 string per "
            f"CLAUDE.md §6; got {chain_id!r:.80} (parse error: {exc}). "
            f"Generate one via uuid.uuid4() or pass chain_id=None to "
            f"clear the chain context."
        ) from exc
    if parsed.version != 4:
        raise ValueError(
            f"chain_id must be a UUID v4 (version=4) per CLAUDE.md §6; "
            f"got version={parsed.version} from {chain_id!r:.80}. "
            f"The backend's chain race guard relies on UUID v4 entropy "
            f"and will silently auto-register non-v4 chain_ids as new "
            f"ACTIVE chains without format validation."
        )


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
# ADR-037 Slice B (2026-08-31, protocol v4): wire-evidence echo
# from /gate response. Both fields are ADR-009 governance columns
# that the backend now echoes on the /gate response (additive —
# pre-v4 backends omit the keys entirely via skip_serializing_if).
#
# `action_digest` is the SDK-supplied SHA-256 hex of the canonical
# `business_impact` payload, re-verified server-side by
# `payload_binding::server_derive_action_digest` and echoed back
# on the wire so the SDK can confirm what the gate saw matches
# what it intended. The architectural invariant
# `GateResponse.action_digest == AuditEvent.action_digest` holds
# trivially because both sides flow from the SDK's input.
#
# `policy_hash` is reserved for future Slice D wiring (per
# ADR-037 §3 deferral — gate doesn't compute per-request hash
# today; in-memory KeyPolicy cache carries no hash and the hot
# path cannot load PolicyRow). Today this field is always None
# on the wire; the audit row stores `policy_hash = None` for the
# same reason (audit_drain.rs:301), so the invariant
# `GateResponse.policy_hash == AuditEvent.policy_hash` holds
# trivially.
#
# Both default to None; clear_ functions reset to None.
_last_gate_action_digest_var: ContextVar[str | None] = ContextVar(
    "last_gate_action_digest", default=None
)
_last_gate_policy_hash_var: ContextVar[str | None] = ContextVar(
    "last_gate_policy_hash", default=None
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
        # Also drops the v4 wire-evidence echo slots so the next
        # /check in scope doesn't read a stale echo from a prior block.
        _last_gate_action_digest_var.set(None)
        _last_gate_policy_hash_var.set(None)

    Use:func:`reset_server_minted_execution_id` instead when you
    have a Token to consume — that path restores the previous
    scope's value, ``clear_`` strictly forgets it.
    """
    _server_minted_execution_id_var.set(None)
    _server_minted_reservation_at_var.set(0.0)
    _server_minted_idempotency_key_var.set(None)
    # ADR-037 Slice B (2026-08-31, protocol v4): also drop the
    # wire-evidence echo slots so a /check in one block never leaks
    # a stale echo into a /track in a sibling block.
    _last_gate_action_digest_var.set(None)
    _last_gate_policy_hash_var.set(None)


def set_attempt_index(index: int) -> None:
    """Set current attempt index for retry correlation."""
    _attempt_index_var.set(index)


# ---------------------------------------------------------------------------
# ADR-037 Slice B (2026-08-31, protocol v4): wire-evidence echo
# ---------------------------------------------------------------------------
# Read by tests + operators to confirm the gate saw the same
# `action_digest` the SDK sent (and to surface the architectural
# invariant `GateResponse.action_digest == AuditEvent.action_digest`
# from the SDK side). `policy_hash` is informational only today;
# Slice D will populate it per-request.


def get_last_gate_action_digest() -> str | None:
    """Return the `action_digest` echoed by the last /gate response, or
    ``None`` if no echo captured in scope (legacy backend, or a /check
    that didn't carry a typed business impact).

    Wire-additive — pre-v4 backends omit the field entirely
    (``skip_serializing_if = "Option::is_none"`` on the backend); a
    v4 SDK connecting to a v3 backend reads None and behaves like
    pre-Slice-B. No false positive.

    See ADR-037 Slice B (2026-08-31) for the wire contract.
    """
    return _last_gate_action_digest_var.get()


def get_last_gate_policy_hash() -> str | None:
    """Return the `policy_hash` echoed by the last /gate response, or
    ``None`` if no echo captured in scope.

    Slot reserved for future Slice D wiring (per ADR-037 §3
    deferral — gate doesn't compute per-request hash today; the
    audit row stores `policy_hash = None` for the same reason at
    `audit_drain.rs:301`). Today this field is always None on the
    wire, so this getter is informational only.

    See ADR-037 Slice B (2026-08-31) for the wire contract.
    """
    return _last_gate_policy_hash_var.get()


def set_last_gate_action_digest(value: str | None) -> None:
    """Capture the `action_digest` echoed by a /gate response.

    Called by ``runtime._capture_wire_evidence`` immediately after
    ``_capture_server_minted_execution_id`` — the two captures share
    the same lifetime (one /check → one execution_id + one
    action_digest). See ADR-037 Slice B (2026-08-31).
    """
    _last_gate_action_digest_var.set(value)


def set_last_gate_policy_hash(value: str | None) -> None:
    """Capture the `policy_hash` echoed by a /gate response.

    Slot reserved for Slice D (per ADR-037 §3 deferral). Today
    this is always set to None on the wire; this setter is the
    forward-compatible hook for Slice D.

    See ADR-037 Slice B (2026-08-31).
    """
    _last_gate_policy_hash_var.set(value)


# ---------------------------------------------------------------------------
# F-19 (2026-08-14): legacy _trace_id / _span_id token-based setters
# ---------------------------------------------------------------------------
#
# ``nullrun.tracing.SpanContext`` is the canonical source-of-truth at
# write time (audit F-19: ``@protect`` derives a SpanContext, then
# emits ``span_start``/``span_end`` with ``ctx.trace_id``). The runtime
# still reads ``get_trace_id`` / ``get_span_id`` for cost-event
# enrichment (``runtime.py:2679``, ``2903-2907``, ``2967-2972``) and
# for ``parent_trace_id`` derivation; without a mirror, those readers
# see ``None`` and fall back to ``generate_trace_id`` — different
# uuid from the SpanContext's trace_id, so the dashboard sees two
# trace rows for a single ``@protect`` call.
#
# These setters let ``decorators._protect_body`` mirror the new
# SpanContext back to legacy AFTER ``set_span``. Token-based (PEP 567)
# so a nested ``@protect`` inside an outer ``@protect`` (or inside
# ``with workflow``) restores the outer trace on reset — same shape
# as ``reset_server_minted_execution_id`` and ``reset_span``.
def set_trace_id(value: str) -> Token[str | None]:
    """Mirror a SpanContext's trace_id into the legacy ``_trace_id_var``.

    Token-based (matches ``reset_span`` / ``reset_server_minted_*``
    helpers). Returns the matching Token so the caller can restore
    the previous value via :func:`reset_trace_id`. Read by
    ``runtime._enrich_event`` and the ``parent_trace_id`` enrichment
    branch; without this mirror the dashboard's span tree is
    detached from the cost events the runtime emits.
    """
    return _trace_id_var.set(value)


def reset_trace_id(token: Token[str | None]) -> None:
    """Restore the previous ``_trace_id_var`` value (paired with
    :func:`set_trace_id`).
    """
    _trace_id_var.reset(token)


def set_span_id(value: str) -> Token[str | None]:
    """Mirror a SpanContext's span_id into the legacy ``_span_id_var``.

    Token-based; pairs with :func:`reset_span_id`. Same audit
    motivation as :func:`set_trace_id` (F-19, 2026-08-14).
    """
    return _span_id_var.set(value)


def reset_span_id(token: Token[str | None]) -> None:
    """Restore the previous ``_span_id_var`` value."""
    _span_id_var.reset(token)


# ---------------------------------------------------------------------------
# F-19 (2026-08-14): helpers used by ``with workflow`` / ``with span``
# ---------------------------------------------------------------------------
#
# ``with workflow`` writes a fresh root ``SpanContext``; ``with span``
# derives a child SpanContext from whatever ``_current_span`` already
# has (or no-ops if no span is active, preserving bare-``with span``
# corner-case behavior for legacy readers).
def _set_workflow_root_span(trace_id: str, span_id: str) -> Token[SpanContext | None]:
    """Push a fresh root ``SpanContext`` onto ``_current_span``.

    Called from ``with workflow`` once the legacy
    ``_workflow_id_var`` / ``_trace_id_var`` / ``_span_id_var`` tokens
    are minted. Returns the matching Token; the caller MUST pair it
    with :func:`reset_span` in a ``finally`` block (the wrapping
    ``with workflow`` does).
    """
    return set_span(
        SpanContext(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=None,
            depth=0,
        )
    )


def _set_child_span_context(span_id: str) -> Token[SpanContext | None] | None:
    """Push a child ``SpanContext`` derived from the active parent.

    Called from ``with span`` only when a parent ``SpanContext`` is
    active (i.e. we're inside a workflow / ``@protect`` block). If
    no parent is set, returns ``None`` and the caller does NOT push
    anything onto ``_current_span`` — preserving the legacy
    corner-case behavior of bare ``with span(...)`` (the runtime's
    ``_enrich_event`` falls back to ``generate_trace_id()`` for
    legacy readers; that path was correct pre-F-19 and stays so).

    Returns the matching Token; ``with span`` pairs it with
    :func:`reset_span` in its ``finally``.
    """
    parent = _current_span.get()
    if parent is None:
        return None
    return set_span(create_child_span_with_id(parent, span_id))


def create_child_span_with_id(parent: SpanContext, span_id: str) -> SpanContext:
    """Build a child ``SpanContext`` reusing a caller-supplied span_id.

    Same semantics as ``tracing.create_child_span`` (inherits
    ``trace_id`` + ``parent_span_id``; ``depth = parent.depth + 1``),
    but takes the ``span_id`` verbatim rather than generating a new
    one. Used by ``with span`` so its externally-observable
    ``span_id`` stays in lockstep with the legacy ``_span_id_var``
    it sets.

    Why not just ``create_child_span(parent)``: that path mints a
    fresh span_id, so the legacy ``_span_id_var`` (set by
    ``with span`` to ``name or generate_span_id()``) and the new
    SpanContext.span_id would diverge — defeating the F-19 fix.
    """
    return SpanContext(
        trace_id=parent.trace_id,
        span_id=span_id,
        parent_span_id=parent.span_id,
        depth=parent.depth + 1,
    )


def set_call_context(
    model: str | None = None,
    tools: list[str] | tuple[str, ...] | None = None,
) -> None:
    """Set per-call context (model name, tool list) for the next /gate
    pre-flight check.

    Replaces the previous fake ``model="budget-precheck"`` and
    ``estimated_tokens=1`` always-default / always-empty pre-flight.
    Call inside a ``with workflow(...)`` block before ``@protect`` to
    give the backend real data.

    Args:
        model: LLM model name (e.g. ``"claude-sonnet-4-6"``). Backend
            uses this to look up the per-model rate from
            ``tool_pricing`` (Postgres) so projected_cost matches what
            /track will compute from real token counts.
        tools: List of tool names the call intends to use. Backend
            matches each against the workflow's effective
            ``blocked_tools`` aggregate and returns block on any
            match. Pass ``None`` to leave whatever was previously
            set, ``[]`` to clear.
    """
    if model is not None:
        _call_model_var.set(model)
    if tools is not None:
        _call_tools_var.set(tuple(tools))


def set_mcp_tool_context(
    tool_class: str | None = None,
    annotations: dict[str, bool | None] | None = None,
) -> None:
    """Forward the cached MCP tool class + annotations to the next
    ``/check`` call.

    Use after fetching ``tools/list`` from an MCP server — the SDK
    caches the response and on each subsequent ``/check`` should
    call this with the matching class and annotations for the
    tool being invoked.

    Args:
        tool_class: One of ``"builtin" | "mcp" | "custom" | "invalid"``.
            When ``None`` the SDK stays quiet and the backend falls
            back to ``classify_tool(raw_tool_name)``.
        annotations: Per-tool MCP annotations dict (keys
            ``read_only``, ``destructive``, ``open_world`` —
            each ``bool | None``). When ``None`` the SDK has no
            opinion and the gate treats the value as unknown.
    """
    if tool_class is not None:
        _call_mcp_class_var.set(tool_class)
    if annotations is not None:
        _call_mcp_annotations_var.set(annotations)


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
    # Emit a real UUID4 with dashes (matching
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
    # F-19 (2026-08-14): dual-write a root SpanContext onto
    # ``_current_span`` so an inner ``@protect`` (or nested
    # ``with span``) derives child spans from THIS workflow's
    # trace_id rather than minting a fresh disconnected root.
    # Before this bridge the two contextvar systems diverged:
    # span_start events carried SpanContext.trace_id while cost
    # events read legacy ``_trace_id_var`` — the dashboard saw
    # two trace rows per ``@protect`` call inside a workflow.
    # ``reset_span(span_ctx_token)`` in the ``finally`` restores
    # the previous SpanContext (could be ``None`` or an outer
    # workflow's root).
    span_ctx_token = _set_workflow_root_span(trace_id, span_id)

    try:
        yield workflow_id
    finally:
        # Restore previous values
        _workflow_id_var.reset(wf_token)
        _trace_id_var.reset(trace_token)
        _span_id_var.reset(span_token)
        # Restore the previous SpanContext (mirrors the legacy
        # token resets above). Resetting before yielding was lost
        # the parent chain — reordering doesn't matter here since
        # finally runs after the body exits and the body has
        # already finished emitting events.
        reset_span(span_ctx_token)


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
    # F-19 (2026-08-14): when a SpanContext is already active
    # (e.g. we're inside ``with workflow(...)`` or ``@protect``),
    # push a child SpanContext onto ``_current_span`` so that nested
    # ``@protect`` calls and the runtime's
    # ``_enrich_event → parent_trace_id`` path both see this span
    # as a real parent. ``_set_child_span_context`` returns
    # ``None`` if no parent is active — bare ``with span(...)``
    # outside any workflow/protect block keeps the legacy behavior
    # (legacy readers fall through to ``generate_trace_id()``
    # enrichment, which was correct pre-F-19 and stays so).
    span_ctx_token = _set_child_span_context(span_id)

    try:
        yield span_id
    finally:
        _span_id_var.reset(token)
        if span_ctx_token is not None:
            reset_span(span_ctx_token)


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
    # Emit a real UUID4 with dashes (matching
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
        chain_id: UUID v4 string identifying this chain. Persists
            in Redis with idle TTL 300s; auto-extended by every
            /check inside the block. Per CLAUDE.md §6 the chain_id
            MUST be a UUID v4 — the context manager validates the
            format (length, canonical UUID structure, version=4)
            and raises ``ValueError`` on malformed input. Generate
            one via ``uuid.uuid4()``.
        op: Chain operation for the FIRST /check call inside the
            block.

    Yields:
        The chain_id (so callers can ``as cid`` for symmetry with
        ``workflow ``).
    """
    if op not in ("start", "continue", "end", "auto"):
        raise ValueError(f"chain() op must be one of start/continue/end/auto, got {op!r}")
    # Per CLAUDE.md §6 the chain_id field MUST be a UUID v4. The
    # backend owns the race guard (HGET chain_key 'org_id') but does
    # NOT validate the chain_id format — non-UUID chain_ids silently
    # auto-register as new ACTIVE chains. SDK validates client-side
    # so typos and predictable-UUID attacks surface before they hit
    # the network. See _validate_chain_id for the version=4 check.
    _validate_chain_id(chain_id)
    chain_token = _chain_id_var.set(chain_id)
    op_token = _chain_op_var.set(op)
    try:
        yield chain_id
    finally:
        _chain_id_var.reset(chain_token)
        _chain_op_var.reset(op_token)
