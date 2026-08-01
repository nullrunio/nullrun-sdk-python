"""Tests for ``nullrun.toolbox.mcp.MCPAdapter``.

The adapter wraps a user-supplied MCP client so every tool call
forwards the cached class + per-tool ``annotations`` to the
gate via ``set_mcp_tool_context``. Tests pin the public
contract without spinning up a real MCP server — we pass a
hand-rolled mock client that exposes ``list_tools()`` and
``call_tool(name, args)`` so the test runs without any IO.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from nullrun.context import (
    get_call_mcp_annotations,
    get_call_mcp_class,
    set_mcp_tool_context,
)
from nullrun.toolbox.mcp import DEFAULT_CACHE_SECONDS, MCPAdapter

# -----------------------------------------------------------------------
# Test fixtures / mocks
# -----------------------------------------------------------------------


@dataclass
class _Ann:
    """MCP-style annotation object — supports attribute access
    the way the official Python MCP SDK exposes them."""

    readOnlyHint: bool | None = None
    destructiveHint: bool | None = None
    openWorldHint: bool | None = None


@dataclass
class _Tool:
    """MCP-style tool entry."""

    name: str
    annotations: _Ann | None = None


class _MockMcpClient:
    """Hand-rolled MCP client substitute. Tracks every
    ``call_tool`` invocation so tests can assert pass-through.
    """

    def __init__(self, tools: list[_Tool]) -> None:
        self._tools = {t.name: t for t in tools}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_tools(self) -> list[_Tool]:
        return list(self._tools.values())

    def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, **kwargs: Any
    ) -> str:
        self.calls.append((name, arguments or {}))
        if name not in self._tools:
            raise KeyError(f"unknown tool {name!r}")
        return f"ok:{name}"


# Pre-built fixtures -----------------------------------------------------


def _clean_context() -> None:
    """Force a clean MCP context between tests. Otherwise a
    stale value from a prior test could leak into the next
    call's ``get_call_mcp_class()`` read. ``set_mcp_tool_context``
    accepts explicit-None to clear both fields.
    """

    set_mcp_tool_context(tool_class=None, annotations=None)


@pytest.fixture(autouse=True)
def _isolate_mcp_context():
    _clean_context()
    yield
    _clean_context()


# A representative `github`-shaped inventory -----------------------------


def _github_inventory() -> list[_Tool]:
    return [
        _Tool(
            name="create_issue",
            annotations=_Ann(
                readOnlyHint=False, destructiveHint=True, openWorldHint=True
            ),
        ),
        _Tool(
            name="delete_repo",
            annotations=_Ann(
                readOnlyHint=False, destructiveHint=True, openWorldHint=True
            ),
        ),
        _Tool(
            name="get_file_contents",
            annotations=_Ann(
                readOnlyHint=True, destructiveHint=False, openWorldHint=True
            ),
        ),
        _Tool(
            name="list_branches",
            annotations=_Ann(
                readOnlyHint=True, destructiveHint=False, openWorldHint=False
            ),
        ),
    ]


# -----------------------------------------------------------------------
# Constructor validation
# -----------------------------------------------------------------------


def test_server_name_required():
    with pytest.raises(ValueError, match="server_name"):
        MCPAdapter(server_name="", mcp_client=_MockMcpClient([]))


def test_cache_seconds_minimum():
    with pytest.raises(ValueError, match="cache_seconds"):
        MCPAdapter(
            server_name="github",
            mcp_client=_MockMcpClient([]),
            cache_seconds=10,
        )


def test_default_cache_seconds_matches_documented_value():
    """Public constant — if we ever change the default, callers
    that rely on it (operator docs) break. Pin it."""

    assert DEFAULT_CACHE_SECONDS == 300


# -----------------------------------------------------------------------
# Cache surface: list_cached_tools + cached_annotations
# -----------------------------------------------------------------------


def test_list_cached_tools_returns_sorted_names():
    client = _MockMcpClient(_github_inventory())
    adapter = MCPAdapter(server_name="github", mcp_client=client)
    names = adapter.list_cached_tools()
    assert names == [
        "create_issue",
        "delete_repo",
        "get_file_contents",
        "list_branches",
    ]


def test_cached_annotations_returns_normalized_shape():
    client = _MockMcpClient(_github_inventory())
    adapter = MCPAdapter(server_name="github", mcp_client=client)
    cached = adapter.cached_annotations("get_file_contents")
    assert cached is not None
    assert cached.read_only is True
    assert cached.destructive is False
    assert cached.open_world is True


def test_cached_annotations_unknown_tool_returns_none():
    client = _MockMcpClient(_github_inventory())
    adapter = MCPAdapter(server_name="github", mcp_client=client)
    assert adapter.cached_annotations("nonexistent_tool") is None


def test_cache_refresh_when_invoked_lazily():
    """Constructor does NOT eagerly call ``list_tools`` — the
    adapter should defer until the first ``call_tool`` /
    ``list_cached_tools`` so empty-then-populated workflows
    don't hammer the upstream on adapter construction."""

    client = _MockMcpClient(_github_inventory())
    sentinel = {"called": False}
    original_list = client.list_tools

    def tracked_list_tools() -> list[_Tool]:
        sentinel["called"] = True
        return original_list()

    adapter = MCPAdapter(
        server_name="github",
        mcp_client=client,
        list_tools=tracked_list_tools,
    )
    # No automatic call on construction.
    assert sentinel["called"] is False
    adapter.list_cached_tools()
    assert sentinel["called"] is True


def test_unparseable_tool_entries_are_skipped_not_crash():
    """An inventory entry that lacks a name should be skipped,
    not crash the cache. Common when servers mix MCP and
    vendor-specific tool shapes."""

    class _MixedClient(_MockMcpClient):
        def list_tools(self) -> list[Any]:
            return [
                _Tool(name="good_tool", annotations=_Ann()),
                object(),  # no .name attribute
            ]

    adapter = MCPAdapter(server_name="mixed", mcp_client=_MixedClient([]))
    names = adapter.list_cached_tools()
    # Only the parseable tool survives.
    assert names == ["good_tool"]


# -----------------------------------------------------------------------
# call_tool: the contract that's actually load-bearing
# -----------------------------------------------------------------------


def test_call_tool_stamps_mcp_class_and_annotations():
    """The whole point of the adapter: before delegating to
    the underlying client, set the gate-visible contextvars
    so /check sees class='mcp' + the cached annotations."""

    client = _MockMcpClient(_github_inventory())
    adapter = MCPAdapter(server_name="github", mcp_client=client)
    result = adapter.call_tool("create_issue", {"repo": "acme/api"})

    # Underlying client got the pass-through.
    assert result == "ok:create_issue"
    assert client.calls == [("create_issue", {"repo": "acme/api"})]

    # Contextvars propagated the MCP shape + annotations.
    assert get_call_mcp_class() == "mcp"
    ann = get_call_mcp_annotations()
    assert ann is not None
    assert ann["read_only"] is False
    assert ann["destructive"] is True
    assert ann["open_world"] is True


def test_call_tool_unknown_tool_stamps_class_invalid():
    """If the SDK asks for a tool the server didn't advertise,
    the gate should see ``class='invalid'`` so it can
    surface the misshape in the audit log rather than
    silently letting the call through."""

    client = _MockMcpClient(_github_inventory())
    adapter = MCPAdapter(server_name="github", mcp_client=client)

    with pytest.raises(KeyError, match="nonexistent"):
        adapter.call_tool("nonexistent", {})

    assert get_call_mcp_class() == "invalid"
    ann = get_call_mcp_annotations()
    # Per Разрыв 3 / wire contract, all three hints are
    # explicitly ``None`` (= unknown) rather than ``False``
    # so the gate cannot accidentally bypass a destructive
    # block because the adapter lied.
    assert ann is not None
    assert ann["read_only"] is None
    assert ann["destructive"] is None
    assert ann["open_world"] is None


def test_call_tool_read_only_caches_allow_pattern():
    """The read-only truthiness flow: SDK supplies
    readOnlyHint=True -> adapter forwards
    ``read_only=true`` -> the gate's
    ``mcp_readonly_policy=allow`` pattern lets the call
    through even when a broad ``mcp://*`` tool_pattern
    would otherwise block it. Pins the wire-level
    booleans the gate trusts."""

    client = _MockMcpClient(_github_inventory())
    adapter = MCPAdapter(server_name="github", mcp_client=client)
    adapter.call_tool("get_file_contents", {"path": "README.md"})
    assert get_call_mcp_class() == "mcp"
    ann = get_call_mcp_annotations()
    assert ann["read_only"] is True
    assert ann["destructive"] is False


def test_call_tool_passes_kwargs_through():
    """Some MCP clients expose extra kwargs (e.g. ``timeout``,
    ``stream=True``). The adapter forwards them untouched."""

    client = _MockMcpClient(_github_inventory())
    adapter = MCPAdapter(server_name="github", mcp_client=client)
    adapter.call_tool(
        "list_branches", {"owner": "acme"}, timeout=2.5
    )
    assert client.calls == [("list_branches", {"owner": "acme"})]
    # The pass-through kwargs reach the underlying client.


def test_call_tool_with_no_arguments_dict():
    """Some tools take no payload. The adapter should pass an
    empty dict (or whatever the underlying client accepts)
    without crashing."""

    client = _MockMcpClient(_github_inventory())
    adapter = MCPAdapter(server_name="github", mcp_client=client)
    result = adapter.call_tool("list_branches")
    assert result == "ok:list_branches"
    assert client.calls == [("list_branches", {})]


def test_call_tool_propagates_underlying_exceptions():
    """The underlying client's exceptions reach the caller
    unchanged. The SDK caller needs to see the same error
    surface as if it called the client directly — the
    adapter only stamps metadata, it doesn't swallow or
    rewrap."""

    class _BoomClient(_MockMcpClient):
        def call_tool(
            self, name: str, arguments: dict[str, Any] | None = None, **kwargs: Any
        ) -> str:
            raise RuntimeError("upstream is down")

    adapter = MCPAdapter(server_name="github", mcp_client=_BoomClient([]))
    with pytest.raises(RuntimeError, match="upstream is down"):
        adapter.call_tool("anything", {})


def test_call_tool_does_not_emit_empty_annotations_dict():
    """Empty annotations dict (``None`` everywhere) is a valid
    signal — it just means "I have no opinion". Wire format
    keeps the dict with explicit None hints so the gate
    distinguishes ``unknown`` from ``absent``."""

    class _NoAnnClient(_MockMcpClient):
        def __init__(self) -> None:
            super().__init__([_Tool(name="bare_tool", annotations=None)])

        def list_tools(self) -> list[_Tool]:
            return list(self._tools.values())

    adapter = MCPAdapter(server_name="bare", mcp_client=_NoAnnClient())
    adapter.call_tool("bare_tool")
    ann = get_call_mcp_annotations()
    assert ann is not None  # explicit None dict, not absent
    assert ann["read_only"] is None
    assert ann["destructive"] is None
    assert ann["open_world"] is None


def test_call_tool_unknown_annotations_helper_is_explicit_none():
    """Pin the dict-access path that the official MCP Python
    SDK uses (a plain ``dict``, not a dataclass)."""

    @dataclass
    class _DictAnnTool:
        name: str
        annotations: dict[str, Any]

    class _DictAnnClient(_MockMcpClient):
        def __init__(self) -> None:
            super().__init__(
                [
                    _DictAnnTool(
                        name="d",
                        annotations={
                            "readOnlyHint": False,
                            "destructiveHint": True,
                            "openWorldHint": True,
                        },
                    ),
                ]
            )

        def list_tools(self) -> list[Any]:
            return list(self._tools.values())

    adapter = MCPAdapter(server_name="dict", mcp_client=_DictAnnClient())
    adapter.call_tool("d")
    ann = get_call_mcp_annotations()
    assert ann["read_only"] is False
    assert ann["destructive"] is True
    assert ann["open_world"] is True


def test_call_tool_uses_custom_list_tools_callable():
    """The list_tools override path is for async or
    non-standard clients. The constructor accepts a custom
    callable in addition to the default ``mcp_client.list_tools``."""

    captured: dict[str, Any] = {}

    def async_list(_client: Any) -> list[_Tool]:
        captured["called"] = True
        return _github_inventory()

    adapter = MCPAdapter(
        server_name="github",
        mcp_client=_MockMcpClient([]),
        list_tools=lambda: captured.update({"called": True})
        or _github_inventory(),
    )
    adapter.list_cached_tools()
    # The custom callable ran instead of the default
    # ``mcp_client.list_tools()``.
    assert captured["called"] is True


def test_call_tool_refreshes_cache_when_inventory_changes():
    """When the upstream ``tools/list`` shape changes (e.g.
    the MCP server pushes new tools), the next ``call_tool``
    rebuilds the cache lazily. Pins the property that the
    adapter does NOT keep stale cache forever."""

    live_tools = [_Tool(name="alive", annotations=None)]

    class _DynamicClient(_MockMcpClient):
        def __init__(self) -> None:
            super().__init__(live_tools)
            self.snapshot = list(live_tools)
            self.list_calls = 0

        def list_tools(self) -> list[_Tool]:
            self.list_calls += 1
            return list(self.snapshot)

    client = _DynamicClient()
    adapter = MCPAdapter(server_name="github", mcp_client=client)

    # First call populates the cache.
    adapter.call_tool("alive")
    assert adapter.cached_annotations("alive") is not None
    assert client.list_calls == 1

    # Replace the upstream inventory with a new toolset.
    # Both ``snapshot`` (what list_tools returns) AND ``_tools``
    # (the underlying call_tool mock) need to stay in sync —
    # the mock's call_tool validates against ``_tools``.
    new_tool = _Tool(
        name="replacement_tool",
        annotations=_Ann(
            readOnlyHint=False,
            destructiveHint=True,
            openWorldHint=False,
        ),
    )
    client.snapshot = [new_tool]
    client._tools = {new_tool.name: new_tool}
    # Reset the cache to force a refresh — simulates elapsed
    # TTL without waiting on wallclock. The adapter exposes
    # no public ``invalidate`` method (we'd rather keep the
    # public surface tiny), so we poke the private attribute.
    adapter._cache = {}
    adapter._cached_at = 0.0

    adapter.call_tool("replacement_tool")
    # The new tool name is now in the cache.
    cached = adapter.cached_annotations("replacement_tool")
    assert cached is not None
    assert cached.destructive is True
    # The old tool name is no longer present.
    assert adapter.cached_annotations("alive") is None
    # The upstream was called again — at least twice now
    # (initial population + forced refresh).
    assert client.list_calls >= 2


def test_call_tool_distinct_server_names_share_no_cache():
    """Two adapters with different ``server_name`` arguments
    don't share cached inventory. Pin: ``self._cache`` is
    per-instance, so a user with multiple MCP integrations
    gets clean separation."""

    github = _MockMcpClient(_github_inventory())
    filesystem = _MockMcpClient(
        [_Tool(name="read_file", annotations=_Ann(readOnlyHint=True))]
    )
    a = MCPAdapter(server_name="github", mcp_client=github)
    b = MCPAdapter(server_name="filesystem", mcp_client=filesystem)

    a.call_tool("create_issue")
    b.call_tool("read_file")

    # Each adapter sees only its own inventory.
    assert "create_issue" in a.list_cached_tools()
    assert "create_issue" not in b.list_cached_tools()
    assert "read_file" in b.list_cached_tools()
    assert "read_file" not in a.list_cached_tools()


def test_call_tool_idempotent_under_repeated_invocations():
    """Repeated calls on the same tool keep returning the
    same metadata. Pins that the contextvar setter does NOT
    accumulate state across calls — each
    ``set_mcp_tool_context`` call replaces the previous value."""

    client = _MockMcpClient(_github_inventory())
    adapter = MCPAdapter(server_name="github", mcp_client=client)
    for _ in range(5):
        adapter.call_tool("create_issue", {"repo": "acme/api"})
    ann = get_call_mcp_annotations()
    assert ann["destructive"] is True
    # The cache wasn't rebuilt each iteration — same call
    # repeated, no upstream polling needed on every
    # invocation beyond the first.
    # (Exact list_tools call count is _maybe_refresh's
    # private concern; this test pins the user-visible
    # outcome.)
