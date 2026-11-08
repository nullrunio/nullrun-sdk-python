"""The MCP annotation cache is a POLICY INPUT, not a convenience.

The gate's ``mcp_destructive_policy`` / ``mcp_readonly_policy``
umbrella policies are evaluated against the ``destructive`` /
``read_only`` hints the adapter stamps on ``/check``. A cached hint
the server has since changed is a stale policy input, so two
properties are pinned here:

1. The window is short (30 s by default) and an operator can remove
   it entirely with ``cache_seconds=0``.
2. A FAILED refresh never presents the stale snapshot to the gate.
   The gate gets the unknown shape and falls back to its own
   classifier.

The snapshot stays readable for DIAGNOSTICS after a failure -- it is
a diagnostic view of the last successful answer, not a claim about
the current one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from nullrun.context import set_mcp_tool_context
from nullrun.toolbox.mcp import DEFAULT_CACHE_SECONDS, MCPAdapter

# -----------------------------------------------------------------------
# Test fixtures / mocks
# -----------------------------------------------------------------------


@dataclass
class _Ann:
    """MCP-style annotation object."""

    readOnlyHint: bool | None = None
    destructiveHint: bool | None = None
    openWorldHint: bool | None = None


@dataclass
class _Tool:
    """MCP-style tool entry."""

    name: str
    annotations: _Ann | None = None


class _FlakyClient:
    """MCP client whose ``tools/list`` can be made to fail."""

    def __init__(self, tools: list[_Tool]) -> None:
        self._tools = {t.name: t for t in tools}
        self.list_calls = 0
        self.fail_list = False

    def list_tools(self) -> list[_Tool]:
        self.list_calls += 1
        if self.fail_list:
            raise RuntimeError("tools/list is down")
        return list(self._tools.values())

    def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, **kwargs: Any
    ) -> str:
        return f"ok:{name}"


class _StubRuntime:
    """Allow-all runtime stub."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def check_workflow_budget(self) -> None:
        return None

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "decision": "allow",
            "decision_source": "gateway",
            "explanation": "stub allow",
            "policy_version": 1,
        }


@pytest.fixture(autouse=True)
def _isolate_mcp_context():
    set_mcp_tool_context(tool_class=None, annotations=None)
    yield
    set_mcp_tool_context(tool_class=None, annotations=None)


@pytest.fixture(autouse=True)
def _default_gate_runtime():
    """``MCPAdapter(runtime=None)`` resolves the global runtime."""
    from nullrun._registry import get_registry

    registry = get_registry()
    previous = registry.get()
    registry.set(_StubRuntime())
    yield
    registry.set(previous) if previous is not None else registry.clear()


def _inventory() -> list[_Tool]:
    return [
        _Tool(
            name="read_notes",
            annotations=_Ann(
                readOnlyHint=True, destructiveHint=False, openWorldHint=False
            ),
        ),
    ]


def _expire(adapter: MCPAdapter) -> None:
    """Force the TTL to have elapsed. The adapter exposes no
    public invalidate method; the existing suite pokes the private
    attribute the same way."""
    adapter._cached_at = 0.0


# -----------------------------------------------------------------------
# The window itself
# -----------------------------------------------------------------------


def test_the_default_window_is_thirty_seconds():
    """The cached annotations are a policy input. A five-minute
    window is a five-minute window in which the gate can be told a
    stale ``destructive=False``."""
    assert DEFAULT_CACHE_SECONDS == 30


def test_a_window_below_thirty_seconds_is_refused():
    """An accidental 10 would hammer the upstream and skew the
    drift detector. Still refused."""
    with pytest.raises(ValueError, match="cache_seconds"):
        MCPAdapter(
            server_name="notes",
            mcp_client=_FlakyClient([]),
            cache_seconds=10,
        )


def test_zero_is_accepted_and_removes_the_window():
    """``cache_seconds=0`` is the operator's way to pay one
    ``tools/list`` round trip per call and hold no window at all."""
    client = _FlakyClient(_inventory())
    adapter = MCPAdapter(
        server_name="notes", mcp_client=client, cache_seconds=0
    )
    adapter.call_tool("read_notes")
    adapter.call_tool("read_notes")
    assert client.list_calls == 2


# -----------------------------------------------------------------------
# The failed refresh
# -----------------------------------------------------------------------


def test_a_failed_refresh_stamps_the_unknown_shape_not_the_stale_annotations():
    """The gate must not be told a stale ``destructive=False`` as if
    it were the server's current answer."""
    client = _FlakyClient(_inventory())
    adapter = MCPAdapter(server_name="notes", mcp_client=client)

    # The first call populates the cache with destructive=False.
    adapter.call_tool("read_notes")
    assert client.list_calls == 1

    # The upstream goes down and the window elapses.
    client.fail_list = True
    _expire(adapter)
    adapter.call_tool("read_notes")

    # The gate was handed the unknown shape, not the cached hint.
    from nullrun.context import get_call_mcp_annotations, get_call_mcp_class

    assert get_call_mcp_class() == "invalid"
    annotations = get_call_mcp_annotations()
    assert annotations is not None
    assert annotations["destructive"] is None
    assert annotations["read_only"] is None
    assert annotations["open_world"] is None


def test_a_failed_refresh_keeps_the_snapshot_for_diagnostics():
    """The snapshot is the last successful answer. It stays
    readable for diagnostics; it is not a claim about the current
    one."""
    client = _FlakyClient(_inventory())
    adapter = MCPAdapter(server_name="notes", mcp_client=client)
    adapter.call_tool("read_notes")

    client.fail_list = True
    _expire(adapter)
    adapter.call_tool("read_notes")

    assert adapter.list_cached_tools() == ["read_notes"]
    cached = adapter.cached_annotations("read_notes")
    assert cached is not None
    assert cached.destructive is False


def test_a_successful_refresh_restores_the_annotations():
    """The unknown shape is a statement about a failed refresh, not
    a new mode."""
    client = _FlakyClient(_inventory())
    adapter = MCPAdapter(server_name="notes", mcp_client=client)
    adapter.call_tool("read_notes")

    client.fail_list = True
    _expire(adapter)
    adapter.call_tool("read_notes")

    client.fail_list = False
    _expire(adapter)
    adapter.call_tool("read_notes")

    from nullrun.context import get_call_mcp_annotations, get_call_mcp_class

    assert get_call_mcp_class() == "mcp"
    annotations = get_call_mcp_annotations()
    assert annotations is not None
    assert annotations["destructive"] is False
    assert annotations["read_only"] is True


def test_a_fresh_cache_does_not_re_list():
    """The window still buys what it bought: one ``tools/list`` per
    window, not one per call."""
    client = _FlakyClient(_inventory())
    adapter = MCPAdapter(server_name="notes", mcp_client=client)
    adapter.call_tool("read_notes")
    adapter.call_tool("read_notes")
    adapter.call_tool("read_notes")
    assert client.list_calls == 1
