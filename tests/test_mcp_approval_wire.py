"""tests/test_mcp_approval_wire.py — NR-058 / NR-015, the SDK half.

The MCP path is the SECOND enforcement path (the first is the
``@protect``-decorated local function). Before this module the
adapter could not complete an approval round trip at all:

* ``MCPAdapter.call_tool`` had no ``approval_id`` parameter, so a
  retry after the operator approved landed in ``**passthrough`` and
  was forwarded to the MCP SERVER instead of the gate. The gate never
  saw the grant.
* The adapter sent neither ``business_impact`` nor ``action_digest``,
  so the approval row was never digest-bound and the backend could not
  detect a tampered payload when the grant was consumed.
* The tool arguments reached the wire only as the legacy ``input``
  field. The backend's canonical argument bag (ADR-060 §4) is
  ``tool_arguments`` → ``tool_params`` → Null, so with
  ``tool_arguments`` absent every params-based rule saw "no bag" and
  the approval decision had no argument snapshot to bind to.
* The adapter called ``runtime.execute`` with NO pre-flight. The
  backend's ``/api/v1/execute`` runs an existence check on the Redis
  binding ``execution:{id}`` and answers 404 EXECUTION_NOT_FOUND when
  no prior ``/gate`` minted it — so a DIRECT
  ``adapter.call_tool(...)`` never reached the MCP server at all.

The doubles here are deliberately shallow — a mock MCP client and a
fake runtime that records the two wire hops (``check_workflow_budget``
stands for ``/gate``, ``execute`` for ``/execute``). Nothing between
the adapter and the fake runtime is faked, so a change that drops the
gate kwargs or the pre-flight turns these red rather than being
stubbed around.

Every test below was run against the PRE-FIX code and failed there;
the failure output is the evidence that the property was not already
satisfied.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from nullrun.breaker.exceptions import NullRunBlockedException
from nullrun.context import (
    clear_server_minted_execution_id,
    set_mcp_tool_context,
    set_server_minted_execution_id,
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
    """Mock MCP client that records the tool name, the arguments AND
    the extra kwargs.

    The kwargs are recorded separately from the arguments on purpose:
    the whole point of T1 is that a gate-only kwarg (``approval_id``)
    must not travel with the tool call into the MCP server.
    """

    def __init__(self, tools):
        self._tools = {t.name: t for t in tools}
        self.calls: list[dict[str, Any]] = []

    def list_tools(self):
        return list(self._tools.values())

    def call_tool(self, name, arguments=None, **kwargs):
        record = {"name": name, "arguments": arguments or {}, "kwargs": kwargs}
        self.calls.append(record)
        if name not in self._tools:
            raise KeyError(f"unknown tool {name!r}")
        return f"ok:{name}"


class _RecordingRuntime:
    """Fake runtime recording both wire hops.

    ``check_workflow_budget`` stands for the ``/gate`` pre-flight;
    ``execute`` stands for ``/execute``. Both append to ``order`` so a
    test can assert the SEQUENCE, not just the counts.
    """

    def __init__(self, payload=None, order=None):
        self.execute_calls: list[dict[str, Any]] = []
        self.gate_calls = 0
        self.order: list[str] = order if order is not None else []
        self._payload = payload or {
            "decision": "allow",
            "decision_source": "gateway",
            "explanation": "allow",
        }

    def check_workflow_budget(self):
        self.gate_calls += 1
        self.order.append("gate")

    def execute(self, **kwargs):
        self.execute_calls.append(kwargs)
        self.order.append("execute")
        return dict(self._payload)


def _inventory():
    return [
        _Tool(
            "create_issue",
            _Ann(read=False, destructive=True, open_world=True),
        ),
        _Tool("get_file_contents", _Ann(read=True, destructive=False)),
    ]


def _adapter(runtime, tools=None):
    client = _MockMcpClient(tools if tools is not None else _inventory())
    adapter = MCPAdapter(
        server_name="github",
        mcp_client=client,
        runtime=runtime,
    )
    return adapter, client


@pytest.fixture(autouse=True)
def _isolate_context():
    """Both contextvars the adapter reads are process-wide, so a stale
    value from a previous test would make a test pass for the wrong
    reason.

    ``server_minted_execution_id`` is the marker the adapter uses to
    decide whether a ``/gate`` pre-flight is needed — a leaked id from
    an earlier test would silently skip the pre-flight.
    """

    clear_server_minted_execution_id()
    set_mcp_tool_context(tool_class=None, annotations=None)
    yield
    clear_server_minted_execution_id()
    set_mcp_tool_context(tool_class=None, annotations=None)


# ---------------------------------------------------------------------------
# T1 — approval_id reaches the GATE and never the MCP client
# ---------------------------------------------------------------------------


class TestApprovalIdReachesTheGate:
    def test_approval_id_is_forwarded_to_the_execute_body(self):
        runtime = _RecordingRuntime()
        adapter, _ = _adapter(runtime)

        adapter.call_tool("create_issue", {"repo": "acme/api"}, approval_id="apr-123")

        assert len(runtime.execute_calls) == 1
        body = runtime.execute_calls[0]
        assert body["approval_id"] == "apr-123", "the grant must reach the gate"

    def test_approval_id_never_reaches_the_mcp_client(self):
        runtime = _RecordingRuntime()
        adapter, client = _adapter(runtime)

        adapter.call_tool("create_issue", {"repo": "acme/api"}, approval_id="apr-123")

        assert len(client.calls) == 1
        kwargs = client.calls[0]["kwargs"]
        assert "approval_id" not in kwargs, "a gate field leaked to the server"

    def test_impact_kwargs_never_reach_the_mcp_client(self):
        runtime = _RecordingRuntime()
        adapter, client = _adapter(runtime)

        adapter.call_tool(
            "create_issue",
            {"repo": "acme/api"},
            business_impact={"kind": "tool_call", "params": {}},
            action_digest="ab" * 32,
        )

        kwargs = client.calls[0]["kwargs"]
        assert "business_impact" not in kwargs
        assert "action_digest" not in kwargs

    def test_the_three_gate_kwargs_are_named_parameters(self):
        """Structural pin for D1: the three kwargs must be NAMED
        parameters of ``call_tool``, not part of ``**passthrough``.

        A named parameter is what makes the property above structural
        rather than a matter of what the current implementation
        happens to forward.
        """

        params = inspect.signature(MCPAdapter.call_tool).parameters
        for name in ("approval_id", "business_impact", "action_digest"):
            assert name in params, f"{name} must be a named parameter"
            kind = params[name].kind
            assert kind is inspect.Parameter.KEYWORD_ONLY, f"{name} is not keyword-only"

        assert params["passthrough"].kind is inspect.Parameter.VAR_KEYWORD


# ---------------------------------------------------------------------------
# T2 — business_impact / action_digest reach the gate body
# ---------------------------------------------------------------------------


class TestImpactReachesTheGate:
    def test_business_impact_is_forwarded_to_the_execute_body(self):
        runtime = _RecordingRuntime()
        adapter, _ = _adapter(runtime)
        impact = {
            "kind": "tool_call",
            "tool": "create_issue",
            "params": {"repo": "acme/api"},
        }

        adapter.call_tool(
            "create_issue",
            {"repo": "acme/api"},
            business_impact=impact,
        )

        assert runtime.execute_calls[0]["business_impact"] == impact

    def test_action_digest_is_forwarded_to_the_execute_body(self):
        runtime = _RecordingRuntime()
        adapter, _ = _adapter(runtime)

        adapter.call_tool(
            "create_issue",
            {"repo": "acme/api"},
            action_digest="cd" * 32,
        )

        assert runtime.execute_calls[0]["action_digest"] == "cd" * 32

    def test_absent_impact_is_not_invented_on_the_wire(self):
        """The adapter must not fabricate a digest the caller did not
        supply — a made-up one would be a claim the backend cannot
        verify."""

        runtime = _RecordingRuntime()
        adapter, _ = _adapter(runtime)

        adapter.call_tool("create_issue", {"repo": "acme/api"})

        body = runtime.execute_calls[0]
        assert "business_impact" not in body
        assert "action_digest" not in body


# ---------------------------------------------------------------------------
# T3 — the arguments reach the gate as the canonical bag
# ---------------------------------------------------------------------------


class TestArgumentsReachTheGate:
    def test_arguments_are_forwarded_as_tool_arguments(self):
        runtime = _RecordingRuntime()
        adapter, _ = _adapter(runtime)

        adapter.call_tool("create_issue", {"repo": "acme/api"})

        body = runtime.execute_calls[0]
        assert body["tool_arguments"] == {"repo": "acme/api"}, "no canonical bag"

    def test_the_legacy_input_payload_is_unchanged(self):
        """``input`` is the tool payload and stays exactly as it was —
        the canonical bag is an ADDITIONAL field, not a replacement."""

        runtime = _RecordingRuntime()
        adapter, client = _adapter(runtime)

        adapter.call_tool("create_issue", {"repo": "acme/api"})

        assert runtime.execute_calls[0]["input_data"] == {"repo": "acme/api"}
        assert client.calls[0]["arguments"] == {"repo": "acme/api"}

    def test_no_arguments_still_yields_an_empty_bag(self):
        runtime = _RecordingRuntime()
        adapter, _ = _adapter(runtime)

        adapter.call_tool("get_file_contents")

        assert runtime.execute_calls[0]["tool_arguments"] == {}


# ---------------------------------------------------------------------------
# T4 — the /gate pre-flight
# ---------------------------------------------------------------------------


class TestGatePreflight:
    def test_gate_runs_before_execute_when_no_execution_id_is_in_scope(self):
        """The backend's ``/execute`` 404s (EXECUTION_NOT_FOUND) unless
        a prior ``/gate`` minted the ``execution:{id}`` binding. A
        direct ``adapter.call_tool(...)`` has no such binding, so the
        adapter must run the pre-flight itself."""

        order: list[str] = []
        runtime = _RecordingRuntime(order=order)
        adapter, _ = _adapter(runtime)

        adapter.call_tool("create_issue", {"repo": "acme/api"})

        assert runtime.gate_calls == 1, "no /gate ran"
        assert order == ["gate", "execute"]

    def test_the_preflight_is_skipped_when_a_gate_already_ran(self):
        """The pre-flight RESERVES money at the gate. Running it twice
        for one logical call would charge the org twice."""

        set_server_minted_execution_id("0192f0c1-1111-7000-8000-000000000001")

        order: list[str] = []
        runtime = _RecordingRuntime(order=order)
        adapter, _ = _adapter(runtime)

        adapter.call_tool("create_issue", {"repo": "acme/api"})

        assert runtime.gate_calls == 0, "a second /gate would charge twice"
        assert len(runtime.execute_calls) == 1
        assert order == ["execute"]

    def test_a_blocked_preflight_never_reaches_the_mcp_client(self):
        """The pre-flight is a gate, not a formality: a block there
        stops the call before /execute and before the MCP server."""

        class _BlockingRuntime(_RecordingRuntime):
            def check_workflow_budget(self):
                super().check_workflow_budget()
                raise NullRunBlockedException(
                    workflow_id="wf-1",
                    reason="budget exhausted",
                    tool_name="create_issue",
                )

        runtime = _BlockingRuntime()
        adapter, client = _adapter(runtime)

        with pytest.raises(NullRunBlockedException):
            adapter.call_tool("create_issue", {"repo": "acme/api"})

        assert runtime.execute_calls == []
        assert client.calls == []


# ---------------------------------------------------------------------------
# T5 — regression: a blocked gate decision stops the MCP call
# ---------------------------------------------------------------------------


class TestBlockedGateStillShortCircuits:
    def test_a_block_decision_never_reaches_the_mcp_client(self):
        runtime = _RecordingRuntime(
            payload={
                "decision": "block",
                "decision_source": "gateway",
                "explanation": "destructive tool blocked",
                "workflow_id": "wf-1",
            }
        )
        adapter, client = _adapter(runtime)

        with pytest.raises(NullRunBlockedException):
            adapter.call_tool("create_issue", {"repo": "acme/api"})

        assert client.calls == [], "a blocked MCP call reached the server"

    def test_a_require_approval_decision_never_reaches_the_mcp_client(self):
        runtime = _RecordingRuntime(
            payload={
                "decision": "require_approval",
                "decision_source": "gateway",
                "explanation": "needs approval",
                "workflow_id": "wf-1",
                "approval_id": "apr-1",
            }
        )
        adapter, client = _adapter(runtime)

        with pytest.raises(NullRunBlockedException):
            adapter.call_tool("create_issue", {"repo": "acme/api"})

        assert client.calls == []
