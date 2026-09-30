"""tests/test_mcp_adapter_gate_closed.py — MCPAdapter is never ungated.

B2, 2026-09-30 (ADR-037). Continues the cluster started by
DEF-MP-TS12-ENF-01 (QA cycle RUN_ID 20260929T1338).

The bypass
----------
v3.53 added ``runtime.execute(...)`` to ``MCPAdapter.call_tool``, but
made it conditional::

    if self._runtime is not None:
        execute_result = self._runtime.execute(...)
    # ... otherwise: call the MCP server directly

``runtime`` defaulted to ``None``, so a default-constructed adapter
called the MCP server with no gate at all. The only thing the
operator got was a contextvar (``set_mcp_tool_context``) that a
*later* ``@protect`` wrapper might read on its *next* ``/check`` —
post-hoc annotation, not enforcement. The module's own documented
example took exactly that path::

    adapter = MCPAdapter(server_name="github", mcp_client=conn)
    result = adapter.call_tool("create_issue", {"repo": "acme/api"})

So the operator's ``mcp_destructive_policy`` /
``mcp_readonly_policy`` applied to a locally-declared function but
not to a remote MCP call — same agent, same loop, different
enforcement. And nothing in the return value, the log, or the audit
trail distinguished the two.

The fix makes ``runtime=None`` mean "resolve the global runtime",
resolved on the same terms ``@protect`` resolves it. Resolution is
lazy (at ``call_tool``, not at construction) so the adapter stays
constructible in fixtures and doc snippets without ``nullrun.init()``
— the original and legitimate reason for the decoupling.

This module deliberately has NO autouse gate-runtime fixture, so it
observes the real resolution order.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from typing import Any

import pytest

from nullrun._registry import get_registry
from nullrun.breaker.exceptions import (
    NullRunAuthenticationError,
    NullRunBlockedException,
)
from nullrun.toolbox.mcp import MCPAdapter

# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _Ann:
    def __init__(self, read=None, destructive=None, open_world=None):
        self.readOnlyHint = read
        self.destructiveHint = destructive
        self.openWorldHint = open_world


class _Tool:
    def __init__(self, name, annotations=None):
        self.name = name
        self.annotations = annotations


class _MockMcpClient:
    def __init__(self, tools):
        self._tools = {t.name: t for t in tools}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_tools(self):
        return list(self._tools.values())

    def call_tool(self, name, arguments=None, **kwargs):
        self.calls.append((name, arguments or {}))
        if name not in self._tools:
            raise KeyError(f"unknown tool {name!r}")
        return f"ok:{name}"


class _RecordingRuntime:
    """Allow-all runtime that records what it was asked."""

    def __init__(self, payload=None):
        self.calls: list[dict[str, Any]] = []
        self._payload = payload or {
            "decision": "allow",
            "decision_source": "gateway",
            "explanation": "allow",
        }

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return dict(self._payload)


@pytest.fixture(autouse=True)
def _clean_registry():
    """No runtime bound, and no API key in the environment.

    Both matter: a bound runtime would make the resolution-order
    assertions pass for the wrong reason, and an ambient
    ``NULLRUN_API_KEY`` would make the fail-loud assertion pass for
    the wrong reason.
    """
    import os

    registry = get_registry()
    previous = registry.get()
    registry.clear()
    had_key = "NULLRUN_API_KEY" in os.environ
    old_key = os.environ.pop("NULLRUN_API_KEY", None)
    try:
        yield
    finally:
        if had_key and old_key is not None:
            os.environ["NULLRUN_API_KEY"] = old_key
        if previous is not None:
            registry.set(previous)
        else:
            registry.clear()


def _inventory():
    return [
        _Tool(
            "create_issue",
            _Ann(read=False, destructive=True, open_world=True),
        ),
        _Tool("get_file_contents", _Ann(read=True, destructive=False)),
    ]


def _adapter(**kw) -> tuple[MCPAdapter, _MockMcpClient]:
    client = _MockMcpClient(_inventory())
    return MCPAdapter(server_name="github", mcp_client=client, **kw), client


# ---------------------------------------------------------------------------
# The bypass
# ---------------------------------------------------------------------------


class TestDefaultAdapterIsGated:
    def test_call_tool_consults_the_gate_by_default(self):
        """The core of B2: no ``runtime=`` no longer means no gate."""
        runtime = _RecordingRuntime()
        get_registry().set(runtime)
        adapter, client = _adapter()
        adapter.call_tool("get_file_contents", {"path": "README.md"})
        assert len(runtime.calls) == 1, (
            "a default-constructed MCPAdapter called the MCP server "
            "without consulting /execute — that is the bypass B2 closes"
        )
        assert runtime.calls[0]["tool_name"] == "get_file_contents"
        assert runtime.calls[0]["input_data"] == {"path": "README.md"}

    def test_gate_runs_before_the_mcp_client(self):
        """Order is the whole point — a gate consulted afterwards is
        a receipt, not a gate."""
        order: list[str] = []

        class _OrderRuntime(_RecordingRuntime):
            def execute(self, **kw):
                order.append("gate")
                return super().execute(**kw)

        get_registry().set(_OrderRuntime())

        client = _MockMcpClient(_inventory())
        original = client.call_tool

        def _tracked(name, arguments=None, **kw):
            order.append("mcp")
            return original(name, arguments, **kw)

        client.call_tool = _tracked
        MCPAdapter(server_name="github", mcp_client=client).call_tool("get_file_contents", {})
        assert order == ["gate", "mcp"], f"/execute must precede the MCP call; got {order}"

    def test_explicit_runtime_beats_the_registry(self):
        """A caller who passes ``runtime=`` gets that one."""
        registry_runtime = _RecordingRuntime()
        explicit = _RecordingRuntime()
        get_registry().set(registry_runtime)
        adapter, _ = _adapter(runtime=explicit)
        adapter.call_tool("get_file_contents", {})
        assert len(explicit.calls) == 1
        assert registry_runtime.calls == [], "an explicit runtime= must win over the registry"


class TestFailLoudNotFailOpen:
    def test_missing_api_key_raises_instead_of_calling_mcp(self):
        """No API key is a configuration error, not permission to
        run ungated.

        This is the invariant ``@protect`` already holds (see
        ``decorators._get_or_create_runtime``: "a missing API key must
        be a hard error not a silent allow-all"). MCPAdapter must not
        be a quieter door into the same state.
        """
        adapter, client = _adapter()
        with pytest.raises(NullRunAuthenticationError):
            adapter.call_tool("get_file_contents", {})
        assert client.calls == [], "the MCP server was called with no gate and no API key"

    def test_the_error_says_how_to_fix_it(self):
        adapter, _ = _adapter()
        with pytest.raises(NullRunAuthenticationError) as exc:
            adapter.call_tool("get_file_contents", {})
        assert "API_KEY" in str(exc.value), "the error must name the missing setting, not just fail"


class TestBlockAndApprovalStillShortCircuit:
    def test_block_never_reaches_the_mcp_server(self):
        runtime = _RecordingRuntime(
            {
                "decision": "block",
                "decision_source": "gateway",
                "explanation": "destructive tool blocked",
                "workflow_id": "wf-1",
            }
        )
        get_registry().set(runtime)
        adapter, client = _adapter()
        with pytest.raises(NullRunBlockedException):
            adapter.call_tool("create_issue", {"repo": "acme/api"})
        assert client.calls == [], "a blocked MCP call reached the server"

    def test_require_approval_never_reaches_the_mcp_server(self):
        runtime = _RecordingRuntime(
            {
                "decision": "require_approval",
                "decision_source": "gateway",
                "explanation": "needs approval",
                "workflow_id": "wf-1",
                "approval_id": "apr-1",
            }
        )
        get_registry().set(runtime)
        adapter, client = _adapter()
        with pytest.raises(NullRunBlockedException) as exc:
            adapter.call_tool("create_issue", {"repo": "acme/api"})
        assert client.calls == []
        # NR-A001 is the "approval exists, route the user through the
        # flow" code; NR-A010 is the "no approval row" variant.
        assert exc.value.error_code == "NR-A001"


class TestContextvarStampingSurvived:
    def test_annotations_are_still_stamped(self):
        """B2 changed when the gate runs, not what is forwarded.

        ``set_mcp_tool_context`` is what makes the v3.31 umbrella
        policies (``mcp_destructive_policy``) fire at all — no SDK on
        the planet calls it otherwise. Dropping it while closing the
        bypass would have traded one hole for another.
        """
        from nullrun.context import get_call_mcp_annotations, get_call_mcp_class

        get_registry().set(_RecordingRuntime())
        adapter, _ = _adapter()
        adapter.call_tool("get_file_contents", {"path": "README.md"})
        assert get_call_mcp_class() == "mcp"
        ann = get_call_mcp_annotations()
        assert ann["read_only"] is True
        assert ann["destructive"] is False

    def test_unknown_tool_stamps_invalid_and_still_gates(self):
        from nullrun.context import get_call_mcp_class

        runtime = _RecordingRuntime()
        get_registry().set(runtime)
        adapter, _ = _adapter()
        with pytest.raises(KeyError):
            adapter.call_tool("phantom_tool", {})
        assert get_call_mcp_class() == "invalid"
        assert len(runtime.calls) == 1, (
            "an unknown tool must still be gated before the server's "
            "KeyError — a permissive server could otherwise invent "
            "tool names that skip the cache"
        )


class TestNoConditionalGateInTheCode:
    """The bypass must be gone from the CODE, not merely unreachable."""

    def test_call_tool_has_no_conditional_runtime_gate(self):
        """Parsed, not grepped — the comment explaining the removal
        must not be able to satisfy the pin.
        """
        src = inspect.getsource(MCPAdapter.call_tool)
        tree = ast.parse(inspect.cleandoc(src))
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and "runtime" in ast.unparse(node):
                pytest.fail(
                    f"call_tool line {node.lineno} still branches on the "
                    f"runtime: {ast.unparse(node)!r}. The gate is "
                    "unconditional — a conditional is the bypass."
                )

    def test_legacy_wording_is_gone_from_the_module(self):
        """No docstring may still advertise the ungated path.

        A stale docstring is how a bypass gets reintroduced: someone
        reads the docs, believes ``runtime=`` is optional, and wires
        around the gate.
        """
        path = pathlib.Path(inspect.getfile(MCPAdapter))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            text = node.value
            if "contextvar-only path" in text or "legacy contextvar" in text:
                offenders.append(f"line {node.lineno}")
        assert not offenders, (
            "mcp.py still documents an ungated contextvar-only path:\n  " + "\n  ".join(offenders)
        )

    def test_no_module_offers_an_ungated_adapter_constructor(self):
        """No second, ungated entry point elsewhere in the toolbox."""
        root = pathlib.Path(__file__).resolve().parent.parent
        toolbox = root / "src" / "nullrun" / "toolbox"
        offenders = []
        for path in toolbox.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "MCPAdapter" not in text:
                continue
            # A module that constructs an adapter and then calls
            # call_tool without a runtime, outside a test.
            for node in ast.walk(ast.parse(text)):
                if (
                    isinstance(node, ast.Call)
                    and ast.unparse(node.func).endswith("MCPAdapter")
                    and not any(kw.arg == "runtime" for kw in node.keywords)
                    and "test" not in str(path).lower()
                ):
                    offenders.append(f"{path.name}:{node.lineno}")
        assert not offenders, "a module constructs MCPAdapter without runtime=:\n  " + "\n  ".join(
            offenders
        )
