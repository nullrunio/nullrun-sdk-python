"""P2-18 — every MCP tool call must run its OWN ``/gate`` pre-flight.

The adapter ran ``runtime.check_workflow_budget()`` only when
``get_server_minted_execution_id()`` was ``None``. That contextvar is
set by ``/gate`` and by ``/execute`` and is NEVER reset in ``src/``, so
the second and later MCP tool calls in one scope ran NO ``/gate``:
no budget check, no reservation, no spend reserved for that call.

The pre-flight still has to be SKIPPED in two cases, and this file pins
both — a fix that simply always pre-flights would double-charge:

* the approval re-entry — the backend's require_approval arm echoes the
  approval row's execution id into ``reservation_id``
  (``enforcement/gate_wire_adapter.rs``), so the ambient id IS the
  approval's execution. A second ``/gate`` would mint a different id and
  the approval's consume (``WHERE execution_id = $3``) would miss its
  row (APPROVAL_REPLAY_REJECTED).
* the ``@protect`` scope — one reserve for the whole scope (NR-059). A
  second reserve inside the scope orphans the first.

The cells drive the REAL runtime over respx-mocked wire, so the
assertions are about what went ON THE WIRE, not about a fake's counters.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

import nullrun
from nullrun.business_impact import BusinessImpact
from nullrun.context import (
    reset_call_impact,
    set_call_impact,
    set_server_minted_execution_id,
)
from nullrun.toolbox.mcp import MCPAdapter

BASE_URL = "https://api.test.nullrun.io"

# The ambient id the approval re-entry carries. A UUIDv7 shape, because
# the backend mints v7 (ADR-073); the SDK only requires it to parse.
AMBIENT_ID = "0192f0c1-1111-7000-8000-000000000001"


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
    """Mock MCP client. Records the tool name and the arguments."""

    def __init__(self, tools):
        self._tools = {t.name: t for t in tools}
        self.calls: list[dict[str, Any]] = []

    def list_tools(self):
        return list(self._tools.values())

    def call_tool(self, name, arguments=None, **kwargs):
        self.calls.append({"name": name, "arguments": arguments or {}})
        return f"ok:{name}"


@pytest.fixture
def wire(mock_api):
    """The two gate hops, captured off the wire.

    The ``/gate`` answer carries a ``reservation_id`` because the
    backend's Allow arm stamps the server-minted uuid there
    (``gate/internal.rs``). Without it the SDK's capture CLEARS the
    ambient id, and the defect this file pins would be invisible.
    The id is a UUIDv7 because the backend mints v7 (ADR-073).

    The ``/execute`` answer echoes the request's ``execution_id`` back,
    because the ``/execute`` arm reuses the ``/gate`` id (ADR-073) and
    the SDK reads that echo.
    """
    from nullrun.uuid7 import uuid7_str

    bodies: dict[str, list[Any]] = {"gate": [], "execute": [], "order": []}

    def _gate(request: httpx.Request) -> httpx.Response:
        bodies["gate"].append(json.loads(request.content.decode("utf-8")))
        bodies["order"].append("gate")
        return httpx.Response(
            200,
            json={
                "decision": "allow",
                "actions": [],
                "local_cost_cents": 0,
                "policy_id": "policy-test",
                "decision_source": "gateway",
                "reservation_id": uuid7_str(),
            },
        )

    def _execute(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        bodies["execute"].append(body)
        bodies["order"].append("execute")
        return httpx.Response(
            200,
            json={
                "decision": "allow",
                "decision_source": "gateway",
                "explanation": "allowed",
                "policy_version": 1,
                "reservation_id": body.get("execution_id"),
            },
        )

    respx.post(f"{BASE_URL}/api/v1/gate").mock(side_effect=_gate)
    respx.post(f"{BASE_URL}/api/v1/execute").mock(side_effect=_execute)
    return bodies


@pytest.fixture(autouse=True)
def _isolate_context():
    """No ambient execution id or envelope leaks between cells.

    The server-minted execution id is never reset in ``src/``, so a
    cell that installs one would change the NEXT cell's pre-flight
    decision. Every cell starts from a clean scope.
    """
    from nullrun.context import clear_server_minted_execution_id

    clear_server_minted_execution_id()
    yield
    clear_server_minted_execution_id()


def _adapter(runtime):
    client = _MockMcpClient(
        [_Tool("create_issue", _Ann(read=False, destructive=True, open_world=True))]
    )
    adapter = MCPAdapter(
        server_name="github",
        mcp_client=client,
        runtime=runtime,
    )
    return adapter, client


class _CountingRuntime:
    """A runtime double whose transport MAY carry an edge box.

    The adapter's condition reads ``runtime._transport.edge``: the
    presence of a box is what makes the pre-flight unconditional. The
    double counts the pre-flight calls; the box itself is not the
    subject of the edge cell.
    """

    def __init__(self, *, edge: bool):
        self.gate_calls = 0
        self.execute_calls: list[dict[str, Any]] = []
        self._transport = SimpleNamespace(edge=object() if edge else None)

    def check_workflow_budget(self):
        self.gate_calls += 1

    def execute(self, **kwargs):
        self.execute_calls.append(kwargs)
        return {
            "decision": "allow",
            "decision_source": "gateway",
            "explanation": "allowed",
        }


class TestEveryToolCallIsGated:
    def test_two_tool_calls_run_two_gates(self, make_runtime, wire):
        """The defect, stated as a fact about the wire.

        Pre-fix the second call issued no ``/gate`` at all: its budget
        was never checked and its spend was never reserved.
        """
        rt = make_runtime()
        adapter, client = _adapter(rt)

        adapter.call_tool("create_issue", {"repo": "acme/api"})
        adapter.call_tool("create_issue", {"repo": "acme/api"})

        assert len(wire["gate"]) == 2, (
            "the second tool call ran no /gate — its budget was never "
            "checked and its spend was never reserved"
        )
        assert len(client.calls) == 2, "the MCP server was called twice"

    def test_each_tool_call_carries_its_own_execution_id(self, make_runtime, wire):
        """One reservation per tool call.

        Pre-fix both ``/execute`` bodies carried the FIRST call's
        execution id, so the second call settled against a reservation
        that was already spent.
        """
        rt = make_runtime()
        adapter, _ = _adapter(rt)

        adapter.call_tool("create_issue", {"repo": "acme/api"})
        adapter.call_tool("create_issue", {"repo": "acme/api"})

        ids = [body["execution_id"] for body in wire["execute"]]
        assert len(set(ids)) == 2, (
            f"both /execute calls carried the same execution id {ids}: the "
            "second tool call reused the first call's reservation"
        )

    def test_every_execute_is_preceded_by_its_own_gate(self, make_runtime, wire):
        """The order is the contract: gate, execute, gate, execute.

        A ``/execute`` with no ``/gate`` of its own is refused by the
        backend with 404 EXECUTION_NOT_FOUND — the ``execution:{id}``
        binding does not exist.
        """
        rt = make_runtime()
        adapter, _ = _adapter(rt)

        adapter.call_tool("create_issue", {"repo": "acme/api"})
        adapter.call_tool("create_issue", {"repo": "acme/api"})

        assert wire["order"] == ["gate", "execute", "gate", "execute"]


class TestTheSkipsThatMustStay:
    def test_the_approval_re_entry_does_not_run_a_second_gate(self, make_runtime, wire):
        """The approval's own execution id must survive the re-fire.

        The backend's require_approval arm echoes the approval row's
        execution id into ``reservation_id``, so the ambient id IS the
        approval's execution. A second ``/gate`` would mint a different
        id and the approval's consume would miss its row.
        """
        rt = make_runtime()
        adapter, _ = _adapter(rt)
        set_server_minted_execution_id(AMBIENT_ID)

        adapter.call_tool("create_issue", {"repo": "acme/api"}, approval_id="appr-1")

        assert wire["gate"] == [], "the approval re-entry ran a second /gate"
        assert len(wire["execute"]) == 1
        assert wire["execute"][0]["execution_id"] == AMBIENT_ID, (
            "the approval re-entry must carry the approval's execution id"
        )

    def test_the_protect_scope_reserves_once(self, make_runtime, wire):
        """One reserve for the whole protected scope (NR-059).

        A second reserve inside the scope orphans the first, and the
        scope's spend settles against the wrong envelope.
        """
        rt = make_runtime()

        @nullrun.protect
        def step():
            adapter, _ = _adapter(rt)
            adapter.call_tool("create_issue", {"repo": "acme/api"})
            return "ok"

        assert step() == "ok"
        assert len(wire["gate"]) == 1, (
            "the protected scope ran more than one /gate — the second "
            "reserve orphans the first"
        )


class TestTheEdgeCarveOut:
    def test_the_edge_asks_the_box_even_when_the_scope_is_covered(self):
        """Via-edge asks the box per tool call, covered scope or not.

        The edge has no cloud ``/track`` to book the call through, so
        the box ask IS the charge, and the ``/execute`` reads back the
        box's answer for ITS event id. A scope that is covered for the
        cloud (one reserve for the whole scope) is NOT covered for the
        edge: skipping the ask leaves the box unconsulted and the
        ``/execute`` then fails CLOSED (SDK-2).
        """
        token = set_call_impact(BusinessImpact.no_impact())
        set_server_minted_execution_id(AMBIENT_ID)
        try:
            cloud = _CountingRuntime(edge=False)
            edge_rt = _CountingRuntime(edge=True)

            _adapter(cloud)[0].call_tool("create_issue", {"repo": "acme/api"})
            _adapter(edge_rt)[0].call_tool("create_issue", {"repo": "acme/api"})

            assert cloud.gate_calls == 0, (
                "a covered cloud scope must NOT pre-flight again — a second "
                "/gate would charge the org twice for one logical call"
            )
            assert edge_rt.gate_calls == 1, (
                "the edge must ask the box for EVERY tool call — the box "
                "is the enforcement surface there"
            )
        finally:
            reset_call_impact(token)
