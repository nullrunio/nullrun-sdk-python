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
from dataclasses import dataclass
from typing import Any

from nullrun.context import (
    get_call_mcp_annotations,
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
        # the canonical class + the cached annotations.
        result = adapter.call_tool("create_issue", {"repo": "acme/api"})
    """

    def __init__(
        self,
        server_name: str,
        mcp_client: Any,
        cache_seconds: int = DEFAULT_CACHE_SECONDS,
        list_tools: Callable[[], Iterable[Any]] | None = None,
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

        Returns the underlying client's result. Raises the
        underlying client's exceptions untouched so the SDK
        caller sees the same errors as if it called the
        client directly.
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
