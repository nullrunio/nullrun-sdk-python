"""MCP (Model Context Protocol) toolbox helper for NullRun.

Wraps a connected MCP server so every tool invocation forwards
the cached canonical class + per-tool `annotations` to the gate
on `/check`. The v3.31 gate honors
`mcp_destructive_policy` / `mcp_readonly_policy` against these
annotations — without an adapter, no SDK on the planet calls
``set_mcp_tool_context()`` and the umbrella policies stay
dormant on real traffic.

The adapter follows the ``toolbox/langgraph.py`` convention:
thin convenience layer over the lower-level MCP wire plumbing
that the user's MCP client library already exposes. We do NOT
reimplement JSON-RPC framing, transports (stdio / Streamable
HTTP / SSE / WebSocket), or `initialize` / `tools/list` discovery;
we expect the user to pass an already-connected client that
exposes ``list_tools()`` / ``call_tool(name, args)``.

Scope (kept deliberately small):
  * Cache `tools/list` for ``MCP_ADAPTER_CACHE_SECONDS``
    (default 300s, matches the gate's ``heartbeat`` cadence).
  * On every ``call_tool(name, args)``, set
    ``call_mcp_class='mcp'`` + ``call_mcp_annotations=...`` via
    the public ``context`` helpers so the runtime's
    ``check_workflow_budget`` forwarding picks them up on the
    next ``/check`` call.
  * Map the MCP spec's
    [`Tool.annotations`](https://modelcontextprotocol.io/specification/2025-06-18/schema#tool)
    object (with hints ``readOnlyHint`` / ``destructiveHint`` /
    ``openWorldHint`` — note the casing the spec uses) onto the
    lowercase ``read_only`` / ``destructive`` / ``open_world``
    fields the gate expects.
  * Pass-through for unknown tool names — surface the same
    exception the underlying client raises, but stamp the
    ``class='invalid'`` context first so the audit log
    records the misshape.

Out of scope (deferred):
  * Negotiating JSON-RPC frames. The user brings their own
    MCP client (e.g. ``mcp`` PyPI, or the official
    ``modelcontextprotocol/python-sdk``).
  * Server-side discovery polling. That's NULLRUN's cron
    worker responsibility (table migration 239 already exists,
    the worker itself is a follow-up PR).
  * Tools / Resources / Prompts distinction — only ``tools``
    is forwarded. Resources (``mcp://server/resource/...``)
    and Prompts (``mcp://server/prompt/...``) are MCP
    primitives we don't model on the wire yet; v3.31 still
    classifies them by string shape.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from contextvars import Token
from dataclasses import dataclass
from typing import Any

from nullrun.business_impact import BusinessImpact, compute_action_digest
from nullrun.context import (
    get_call_impact,
    reset_call_impact,
    set_call_impact,
    set_mcp_tool_context,
)

logger = logging.getLogger(__name__)


# Cache TTL for the ``tools/list`` discovery response. Matches
# the v3.31 gate's ``heartbeat`` cadence so the adapter and the
# gate see roughly the same version of the server's tool inventory
# over time. Operators who need a tighter or looser TTL can
# override it via the constructor.
DEFAULT_CACHE_SECONDS = 300


@dataclass(frozen=True)
class _CachedTool:
    """Lightweight snapshot of an MCP tool entry. We keep only
    the fields the gate cares about — name and annotations —
    so the cache stays small even for servers that expose 100+
    tools."""

    name: str
    read_only: bool | None
    destructive: bool | None
    open_world: bool | None


def _normalize_annotation(tool: Any) -> _CachedTool:
    """Read the MCP ``Tool.annotations`` object the user's
    client library already parsed, and project it onto the
    ``_CachedTool`` shape the gate expects.

    The MCP spec (2025-06-18) defines
    ``annotations.readOnlyHint`` etc. with PascalCase keys.
    Third-party clients surface this as either attribute access
    (``tool.annotations.readOnlyHint``), dict access
    (``tool.annotations["readOnlyHint"]``), or pydantic-style
    attributes. We accept all three.
    """

    def read_ann(hint: str) -> bool | None:
        ann = getattr(tool, "annotations", None)
        if ann is None:
            return None
        val: Any
        if hasattr(ann, hint):
            val = getattr(ann, hint)
        elif isinstance(ann, dict) and hint in ann:
            val = ann[hint]
        else:
            return None
        if val is None:
            return None
        # Treat ONLY literal ``True`` / ``False`` as a value;
        # coerce truthy non-bools (some clients return
        # ``None`` to mean "I don't know" — not None here,
        # already handled — or non-boolean objects) back to
        # ``None`` so the gate treats them as "unknown".
        return bool(val) if isinstance(val, bool) else None

    name = getattr(tool, "name", None) or getattr(tool, "tool_name", None)
    if not name:
        raise ValueError(
            f"MCPAdapter: tool object has no readable name: {tool!r}"
        )
    return _CachedTool(
        name=str(name),
        read_only=read_ann("readOnlyHint"),
        destructive=read_ann("destructiveHint"),
        open_world=read_ann("openWorldHint"),
    )


def _derive_mcp_impact(
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[BusinessImpact, dict[str, Any], str] | None:
    """Derive the ONE envelope that describes one MCP tool call.

    ADR-125. The envelope is a ``tool_call`` impact carrying the tool
    name and the EXACT argument dict the adapter forwards as the
    canonical bag (``tool_arguments``). The backend re-derives the
    digest from the live payload, compares it with the digest stored
    on the approval row, and then compares the envelope's ``params``
    with the live bag — so the params MUST be that same dict, not a
    masked or reshaped copy of it. A reshaped copy would bind the
    operator's approval to arguments nobody approved.

    Returns ``(impact, wire_dict, digest)``, or ``None`` when the
    envelope cannot be built: the digest layer cannot round-trip the
    arguments (an f64, a non-ASCII tool name, an over-long parameter
    name). The caller then sends the ``no_impact`` digest with NO
    envelope, which the backend refuses at consume time — the
    fail-CLOSED arm, not the legacy "no digest at all" arm that binds
    an approval to nothing.
    """
    if not isinstance(arguments, dict):
        # A non-mapping argument bag is not a tool payload this
        # envelope can describe.
        return None
    try:
        impact = BusinessImpact.tool_call(tool_name, arguments)
    except (ValueError, TypeError, AttributeError) as exc:
        logger.warning(
            "MCPAdapter: could not derive a tool_call BusinessImpact for %r (%s); "
            "the call goes on the wire with the no_impact digest and NO envelope, "
            "so an operator approval for it cannot be consumed.",
            tool_name,
            exc,
        )
        return None
    return impact, impact.to_wire_dict(), compute_action_digest(impact)


class MCPAdapter:
    """Forward MCP-aware metadata (``tool_class`` +
    ``mcp_annotations``) for every tool call from a connected
    MCP server.

    Usage:

        from mcp import Client  # your MCP client of choice
        from nullrun.toolbox.mcp import MCPAdapter

        conn = Client.connect(...)  # user-owned connection
        adapter = MCPAdapter(server_name="github", mcp_client=conn)

        # Now every `adapter.call_tool` stamps the gate with
        # the canonical class + the cached annotations AND is
        # gated by /api/v1/execute before the MCP server is
        # called. The runtime is resolved from the global
        # registry (or NULLRUN_API_KEY) on the same terms
        # `@protect` resolves it — pass `runtime=` to override.
        result = adapter.call_tool("create_issue", {"repo": "acme/api"})
    """

    def __init__(
        self,
        server_name: str,
        mcp_client: Any,
        cache_seconds: int = DEFAULT_CACHE_SECONDS,
        list_tools: Callable[[], Iterable[Any]] | None = None,
        # When provided, ``call_tool`` routes the invocation through
        # ``runtime.execute(...)`` (the /api/v1/execute gate endpoint)
        # BEFORE the underlying MCP client is called, so the operator's
        # tool-block / budget / approval policies apply to MCP tool
        # calls just like they do to local functions decorated with
        # ``@protect``.
        #
        # When ``runtime`` is None the adapter resolves the global
        # runtime itself, on the same terms ``@protect`` does
        # (``get_active_runtime()`` then ``NullRunRuntime.get_instance()``).
        # B2 (2026-09-30 / ADR-061) removed the previous behaviour,
        # which was: ``runtime=None`` meant "no /execute round-trip at
        # all". The MCP call went straight through and the only thing
        # the operator got was a contextvar that a *later* ``@protect``
        # wrapper might or might not read — a post-hoc annotation, not
        # enforcement, and the module's own documented example
        # (``MCPAdapter(server_name=..., mcp_client=conn)``) took
        # exactly that ungated path.
        #
        # Resolution is lazy (at ``call_tool``, not at construction)
        # so the adapter stays importable and constructible in test
        # fixtures and documentation snippets without forcing
        # ``nullrun.init()`` — that was the original reason for the
        # decoupling, and it is preserved. A configuration error at
        # call time (no API key) propagates loudly rather than
        # degrading to an ungated call, matching ``@protect``'s
        # fail-loud invariant: a missing API key is a hard error, not
        # a silent allow-all.
        runtime: Any | None = None,
    ) -> None:
        if not server_name:
            raise ValueError("MCPAdapter: server_name is required")
        if cache_seconds < 30:
            # Less than 30s would hammer the upstream and
            # skew the v3.31 ``mcp_observed_tools`` drift
            # detector's notion of "stable tool inventory".
            raise ValueError(
                f"MCPAdapter: cache_seconds must be >= 30 (got {cache_seconds})"
            )
        self.server_name = server_name
        self._mcp_client = mcp_client
        self._cache_seconds = cache_seconds
        # ``list_tools_fn`` lets the caller wire up a custom
        # discovery path (e.g. an already-async MCP client that
        # exposes ``await client.list_tools()``). Default:
        # call ``mcp_client.list_tools()`` synchronously and
        # assume it returned an iterable of tool objects.
        self._list_tools_fn = list_tools or self._default_list_tools
        # (name -> _CachedTool) populated lazily on the first
        # call_tool, refreshed every ``cache_seconds``.
        self._cache: dict[str, _CachedTool] = {}
        self._cached_at: float = 0.0
        # every ``call_tool``. When provided, ``call_tool`` blocks on
        # ``runtime.execute(...)`` returning decision="block" so a
        # permissive MCP server cannot bypass the operator's
        # tool-block / budget / approval policies. When None, the
        # global runtime is resolved lazily at ``call_tool`` time —
        # see the constructor docstring.
        self._runtime = runtime

    def _resolve_runtime(self) -> Any:
        """The runtime this adapter gates through.

        B2 (2026-09-30): the explicit ``runtime=`` wins; otherwise the
        global runtime is resolved on the same terms ``@protect``
        resolves it. Never returns None — there is no ungated path.

        ``@protect``'s own resolver is not imported: it also triggers
        ``auto_instrument()``, which is the decorator's job and would
        be a surprising side effect for a caller who handed us an MCP
        client. The two lines of resolution order are what matter, and
        they are pinned by a test.
        """
        if self._runtime is not None:
            return self._runtime
        from nullrun._registry import get_active_runtime
        from nullrun.runtime import NullRunRuntime

        active = get_active_runtime()
        if active is not None:
            return active
        # Propagates NullRunAuthenticationError when NULLRUN_API_KEY is
        # unset. Deliberate — see the constructor docstring.
        return NullRunRuntime.get_instance()

    def _default_list_tools(self) -> Iterable[Any]:
        tools = self._mcp_client.list_tools()
        # Accept any iterable — caller might return a list,
        # a generator, an async iterable wrapped in asyncio
        # .run, etc.
        try:
            return list(tools)
        except TypeError as exc:
            raise TypeError(
                "MCPAdapter: mcp_client.list_tools() must return an "
                "iterable. Pass a custom ``list_tools`` callable if "
                "the underlying client is async or returns a "
                "different shape."
            ) from exc

    def _refresh_cache(self) -> None:
        """Re-query ``tools/list`` and rebuild the lookup
        cache. Called automatically on cache expiry AND on
        the first call_tool after construction."""
        try:
            tools = self._list_tools_fn()
            fresh: dict[str, _CachedTool] = {}
            for tool in tools:
                try:
                    cached = _normalize_annotation(tool)
                except ValueError as exc:
                    logger.debug(
                        "MCPAdapter: skipping unparseable tool entry: %s",
                        exc,
                    )
                    continue
                fresh[cached.name] = cached
            self._cache = fresh
            self._cached_at = time.monotonic()
            logger.debug(
                "MCPAdapter: refreshed tool cache for %s (%d tools)",
                self.server_name,
                len(fresh),
            )
        except Exception as exc:  # noqa: BLE001
            # Cache refresh is best-effort. If the upstream is
            # down we'll keep using the previous cache rather
            # than failing every call. Operators see stale
            # data in the audit log; the next call will retry.
            logger.warning(
                "MCPAdapter: %s tools/list refresh failed: %s (keeping %d cached)",
                self.server_name,
                exc,
                len(self._cache),
            )

    def _maybe_refresh(self) -> None:
        if (
            not self._cache
            or (time.monotonic() - self._cached_at) > self._cache_seconds
        ):
            self._refresh_cache()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_cached_tools(self) -> list[str]:
        """Return the cached tool names. Useful for diagnostics
        / dashboard rendering. Triggers a refresh if the
        cache is empty or stale."""
        self._maybe_refresh()
        return sorted(self._cache.keys())

    def cached_annotations(self, tool_name: str) -> _CachedTool | None:
        """Inspect the cached annotations for a specific tool.
        Triggers a refresh if the cache is empty or stale."""
        self._maybe_refresh()
        return self._cache.get(tool_name)

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        approval_id: str | None = None,
        business_impact: dict[str, Any] | None = None,
        action_digest: str | None = None,
        **passthrough: Any,
    ) -> Any:
        """Call a tool on the underlying MCP client, stamping
        the cached class + annotations onto the gate's
        `/check` via the public context helpers so the
        upstream ``check_workflow_budget`` picks them up.

        ``arguments`` is forwarded to the underlying client
        verbatim; ``**passthrough`` lets callers expose
        client-specific kwargs without changing the public
        surface.

        The three GATE kwargs — ``approval_id``, ``business_impact``
        and ``action_digest`` — are NAMED keyword-only parameters, so
        they can never fall into ``**passthrough`` and be forwarded to
        the MCP server. They are consumed here and sent to the GATE:

        * ``approval_id`` — the operator-approved grant. Without it
          the backend had no way to learn that the call was approved,
          so the approval round trip could not be completed
          (NR-058, SDK half).
        * ``business_impact`` / ``action_digest`` — the typed impact
          and its SHA-256 digest. The backend stamps the approval row
          with the digest and re-derives it from the live payload at
          consume time, so a tampered payload is refused (NR-015,
          SDK half).

          **When the caller supplies NEITHER, the adapter derives
          both from the tool call itself** (ADR-125): a ``tool_call``
          envelope carrying the tool name and the exact argument bag
          it forwards, plus that envelope's digest. Without them the
          approval row carries no digest at all, and the backend's
          post-approval ``/execute`` re-entry cannot server-derive
          one — it refuses the consume with ``BUSINESS_IMPACT_INVALID``
          (NR-010), so an operator approval for an MCP tool could
          never be spent, and even where it could, the approval would
          bind the tool NAME but not the ARGUMENTS.

        Gate enforcement: ``call_tool`` ALWAYS routes the invocation
        through ``runtime.execute(...)`` (the /api/v1/execute gate
        endpoint) BEFORE the underlying MCP client is called. The
        runtime is the one passed to the constructor, or the global
        runtime resolved on the same terms ``@protect`` resolves it.
        ``decision="block"`` raises ``NullRunBlockedException`` and the
        MCP client is NOT called. ``decision="allow"`` proceeds to the
        MCP client. ``decision="require_approval"`` raises
        ``NullRunBlockedException`` with the approval_id attached so the
        caller can route the user through the approval flow and retry
        with ``approval_id=``.

        PRE-FLIGHT: ``/execute`` is bound to an execution id that a
        prior ``/gate`` minted — the backend answers 404
        EXECUTION_NOT_FOUND when the ``execution:{id}`` binding is
        missing. The ``@protect`` path gets that ``/gate`` from its own
        pre-flight; a DIRECT ``adapter.call_tool(...)`` has no such
        wrapper, so the adapter runs the pre-flight ITSELF.

        The pre-flight runs for EVERY tool call, because every tool
        call is its own action with its own budget. Two cases skip it,
        and only those two:

        * the approval re-entry — the caller carries an
          ``approval_id``, so the ambient server-minted id IS the
          approval row's execution id, and a second ``/gate`` would
          mint a different one;
        * the ``@protect`` scope — the scope already reserved for
          itself, and a second ``/gate`` would reserve twice and
          orphan the first (NR-059).

        A second tool call in the same scope is NOT one of those two.
        It gets its own ``/gate``, its own reservation, its own
        execution id. The backend settles a reservation once (SDK-1).

        B2 (2026-09-30): there is no ungated path. ``runtime=None``
        used to mean "skip the round-trip entirely"; it now means
        "resolve the global runtime". A configuration error at this
        point (no API key) propagates rather than degrading to an
        ungated call.

        Returns the underlying client's result (when allowed).
        Raises ``NullRunBlockedException`` on gate block; raises the
        underlying client's exceptions untouched on MCP transport
        failure so the SDK caller sees the same errors as if it
        called the client directly.
        """
        self._maybe_refresh()
        cached = self._cache.get(tool_name)
        if cached is None:
            # Unknown tool. Be honest with the gate: this is
            # not a recognised MCP server's tool. The gate
            # will fall through to its ``classify_tool``
            # parser (which would have classified this as
            # ``invalid`` anyway given the cached inventory).
            annotations: dict[str, Any] | None = {
                "read_only": None,
                "destructive": None,
                "open_world": None,
            }
            tool_class = "invalid"
            logger.debug(
                "MCPAdapter: tool %r not found in %s's inventory; "
                "stamping class=invalid on /check",
                tool_name,
                self.server_name,
            )
        else:
            annotations = {
                "read_only": cached.read_only,
                "destructive": cached.destructive,
                "open_world": cached.open_world,
            }
            tool_class = "mcp"

        # Stamp the context. ``set_mcp_tool_context`` is a
        # non-blocking ContextVar set — it stays in scope for
        # whatever code path wraps this call (typically the
        # user's agentic loop with @protect-decorated
        # functions). The runtime reads it via
        # ``get_call_mcp_class`` / ``get_call_mcp_annotations``
        # when assembling the next /check request.
        set_mcp_tool_context(tool_class=tool_class, annotations=annotations)

        # synchronously BEFORE invoking the MCP client. This closes
        # the silent bypass where the agentic loop called
        # ``adapter.call_tool`` directly without a ``@protect``
        # wrapper. ``runtime.execute`` raises ``NullRunBlockedException``
        # on ``decision="block"`` (and on ``decision="require_approval"``
        # unless an ``approval_id`` is supplied) — both short-circuit
        # to the call site without touching ``self._mcp_client``.
        #
        # B2 (2026-09-30): this is no longer conditional. The previous
        # ``if self._runtime is not None:`` meant a default-constructed
        # adapter — including the one in this module's own docstring —
        # called the MCP server with no gate at all.
        runtime = self._resolve_runtime()
        execute_input = arguments if arguments is not None else {}

        # Is a scope-level envelope ALREADY in scope? Read BEFORE the
        # adapter resolves its own envelope below, because the adapter
        # installs one of its own and that would make this answer
        # "yes" for every call.
        #
        # The ``@protect`` path is the only thing in the SDK that
        # installs one, and it installs it for the WHOLE scope — after
        # its own ``/gate`` reserved for that scope. So "an envelope
        # was already here" is the marker of a scope that already
        # gated itself.
        scope_already_gated = get_call_impact() is not None

        # ── The ONE envelope for this logical action (ADR-125) ─────
        #
        # The adapter derives the envelope from the tool call it is
        # about to make: the tool name and the EXACT argument dict it
        # forwards as the canonical bag. Without it the approval row
        # carries NO digest, and the backend's post-approval
        # /execute re-entry cannot server-derive the digest at all —
        # it refuses the consume with BUSINESS_IMPACT_INVALID (NR-010),
        # so an operator approval for an MCP tool could never be
        # spent. See ADR-125 §1.
        business_impact, action_digest, impact_token = self._resolve_impact(
            tool_name=tool_name,
            arguments=execute_input,
            business_impact=business_impact,
            action_digest=action_digest,
        )
        try:
            return self._call_tool_gated(
                runtime=runtime,
                tool_name=tool_name,
                arguments=arguments,
                execute_input=execute_input,
                approval_id=approval_id,
                business_impact=business_impact,
                action_digest=action_digest,
                passthrough=passthrough,
                scope_already_gated=scope_already_gated,
            )
        finally:
            if impact_token is not None:
                reset_call_impact(impact_token)

    def _resolve_impact(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        business_impact: dict[str, Any] | None,
        action_digest: str | None,
    ) -> tuple[dict[str, Any] | None, str | None, Token[BusinessImpact | None] | None]:
        """Resolve the envelope this call goes on the wire with.

        Returns ``(business_impact, action_digest, impact_token)``.

        * A caller that brought its OWN envelope (or its own digest)
          keeps it: the SDK cannot reconstruct the caller's envelope,
          so inventing a digest for it would be a claim the backend
          cannot verify. Forward it verbatim.
        * Otherwise the adapter derives the envelope from the call
          itself and installs it on the call context, so the
          pre-flight ``/gate`` hashes the SAME bytes this ``/execute``
          carries (ADR-065: one envelope per logical action).
        * An envelope already in scope (the ``@protect`` path) is NOT
          overwritten — that path's own ``/gate`` and ``/execute`` must
          keep agreeing with each other.
        * When no envelope can be built at all, the digest still goes
          on the wire (over the ``no_impact`` sentinel) and the
          envelope stays ABSENT. The backend then takes its
          fail-CLOSED arm ("cannot recompute") instead of the legacy
          "no digest at all" arm, which is a grant bound to nothing.
        """
        if business_impact is not None or action_digest is not None:
            return business_impact, action_digest, None

        derived = _derive_mcp_impact(tool_name, arguments)
        if derived is None:
            return (
                None,
                compute_action_digest(BusinessImpact.no_impact()),
                None,
            )

        impact, wire, digest = derived
        token: Token[BusinessImpact | None] | None = None
        if get_call_impact() is None:
            token = set_call_impact(impact)
        return wire, digest, token

    def _call_tool_gated(
        self,
        *,
        runtime: Any,
        tool_name: str,
        arguments: dict[str, Any] | None,
        execute_input: dict[str, Any],
        approval_id: str | None,
        business_impact: dict[str, Any] | None,
        action_digest: str | None,
        passthrough: dict[str, Any],
        scope_already_gated: bool,
    ) -> Any:
        """The gated half of :meth:`call_tool`.

        Split out so the caller can hold ONE envelope for the whole
        logical action: the envelope is installed on the call context
        before the pre-flight ``/gate`` runs and released after this
        method returns, whatever it returns.
        """
        # ── Pre-flight: /execute needs a prior /gate ───────────────
        #
        # The backend's ``/api/v1/execute`` runs an existence check on
        # the Redis binding ``execution:{id}`` and answers 404
        # EXECUTION_NOT_FOUND when no prior ``/gate`` minted it
        # (``backend/src/proxy/http/gate/execute.rs``). The
        # ``@protect`` path runs ``check_workflow_budget()`` BEFORE
        # ``runtime.execute()``; a DIRECT ``adapter.call_tool(...)``
        # has no such wrapper, so without this pre-flight the /execute
        # round trip was refused and the MCP server was never called.
        #
        # The pre-flight runs for EVERY tool call. A tool call is its
        # own action with its own budget, so it gets its own ``/gate``,
        # its own reservation and its own execution id.
        #
        # The condition used to be "no server-minted execution id in
        # scope". That id is set by ``/gate`` and by ``/execute`` and is
        # NEVER reset in ``src/``, so the second and later tool calls
        # in one scope ran NO ``/gate``: no budget check, no
        # reservation, no spend reserved for those calls. The id says
        # "some action was gated", not "THIS action was gated".
        #
        # Two cases are genuinely covered by an ambient reservation,
        # and both are about THIS action:
        #
        # * the approval re-entry — the caller carries an
        #   ``approval_id``, and the backend's require_approval arm
        #   echoes the approval row's execution id into
        #   ``reservation_id``. The ambient id IS the approval's
        #   execution, so a second ``/gate`` would mint a DIFFERENT
        #   id and the approval's consume (``WHERE execution_id =
        #   $3``) would miss its row (APPROVAL_REPLAY_REJECTED).
        # * the ``@protect`` scope — the scope reserved for itself,
        #   and a second ``/gate`` inside it would reserve twice and
        #   orphan the first (NR-059).
        #
        # A refusal from the pre-flight propagates: the MCP client is
        # NOT called. Same ordering and same error handling as the
        # ``@protect`` wrapper (``decorators._protect_body``: gate
        # pre-flight → /execute → body).
        from nullrun.context import get_server_minted_execution_id

        # Via-edge. The BOX is the enforcement surface there: its
        # ``/enforce`` call is what prices AND charges the call, and
        # there is no cloud ``/track`` to book it through afterwards.
        # So every tool call needs its own ask — one box call, one
        # charge, per tool call.
        #
        # The "a /gate already ran for this action" short-circuit is
        # correct for the cloud (one /gate reserves, one /execute
        # reuses that reservation, one /track settles it) and wrong
        # for the edge (one /enforce per tool call). Skipping the ask
        # on the second tool call would leave the box unconsulted, and
        # with the SDK-2 fix the /execute then fails CLOSED because
        # nobody asked — so the second and later MCP calls in an edge
        # scope must ask the box themselves.
        _transport = getattr(runtime, "_transport", None)
        edge_mode = getattr(_transport, "edge", None) is not None

        ambient_id = get_server_minted_execution_id()
        covered = (approval_id is not None) or scope_already_gated

        if edge_mode or ambient_id is None or not covered:
            runtime.check_workflow_budget()

        # Every MCP tool call routed through the runtime contacts
        # /api/v1/execute unconditionally — there is no
        # ``mode=`` opt-out for audit bypass.
        #
        # ``tool_arguments`` is the canonical argument bag
        # (ADR-060 §4): the backend resolves the bag as
        # ``tool_arguments`` → ``tool_params`` → Null, so a payload
        # that only travels as the legacy ``input`` field leaves every
        # params-based rule with "no bag" and leaves the approval
        # decision with no argument snapshot to bind to. The SAME dict
        # is sent as both — ``input`` is the tool payload and is
        # unchanged; ``tool_arguments`` is the canonical view of it.
        execute_kwargs: dict[str, Any] = {
            "tool_name": tool_name,
            "input_data": execute_input,
            "tool_arguments": execute_input,
        }
        # The three gate kwargs are forwarded ONLY when supplied —
        # an absent field means "no grant" / "no envelope", which is
        # not the same wire statement as a null.
        if approval_id is not None:
            execute_kwargs["approval_id"] = approval_id
        if business_impact is not None:
            execute_kwargs["business_impact"] = business_impact
        if action_digest is not None:
            execute_kwargs["action_digest"] = action_digest
        execute_result = runtime.execute(**execute_kwargs)
        decision = execute_result.get("decision")
        if decision == "block":
            # ``NullRunBlockedException`` is raised by
            # ``runtime.execute`` internally; this guard is for
            # defense-in-depth in case the runtime returns a
            # synthetic block (e.g. PERMISSIVE fallback in
            # tests) and the exception path was bypassed.
            from nullrun.breaker.exceptions import NullRunBlockedException

            raise NullRunBlockedException(
                workflow_id=execute_result.get("workflow_id") or "unknown",
                reason=execute_result.get(
                    "explanation",
                    "MCP gate blocked call",
                ),
                tool_name=tool_name,
                error_code="NR-T003",
                user_action=(
                    f"MCPAdapter.call_tool({tool_name!r}) was blocked "
                    "by the NullRun gate. The MCP client was NOT "
                    "invoked. Inspect the operator's tool-block / "
                    "budget / approval policy to allow this call."
                ),
            )
        if decision == "require_approval":
            from nullrun.breaker.exceptions import NullRunBlockedException

            approval_id = execute_result.get("approval_id") or ""
            raise NullRunBlockedException(
                workflow_id=execute_result.get("workflow_id") or "unknown",
                reason=execute_result.get(
                    "explanation",
                    "MCP gate requires operator approval",
                ),
                tool_name=tool_name,
                error_code="NR-A010" if not approval_id else "NR-A001",
                user_action=(
                    f"MCPAdapter.call_tool({tool_name!r}) requires "
                    "operator approval before the MCP client is "
                    "invoked. Route the user through the approval "
                    f"flow and retry with approval_id={approval_id!r}."
                ),
            )

        # Call through. We deliberately do NOT catch the
        # underlying client's exceptions — the SDK caller
        # needs to see them exactly as they would have from
        # a direct call. The contextvar remains set so the
        # post-call track / audit lineage still tags the
        # call as MCP-shaped.
        if arguments is None:
            return self._mcp_client.call_tool(
                tool_name, **{}, **passthrough
            )
        return self._mcp_client.call_tool(
            tool_name, arguments, **passthrough
        )


__all__ = ["MCPAdapter", "DEFAULT_CACHE_SECONDS"]
