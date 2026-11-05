"""tests/test_mcp_action_binding.py — SDK-7, the MCP approval binding.

The MCP path is the SDK's SECOND enforcement path (the first is the
``@protect``-decorated local function). Before ADR-125 the adapter
sent NO ``business_impact`` and NO ``action_digest`` on ``/execute``,
and the backend refused the post-approval re-entry outright:

* ``/execute`` with an ``approval_id`` runs Step 9.5, which
  SERVER-DERIVES the action digest from the live
  ``gate_req.business_impact``
  (``backend/src/proxy/http/gate/payload_binding.rs:163``). With no
  envelope the derive returns ``Ok(None)`` and the orchestrator fails
  CLOSED with ``BUSINESS_IMPACT_INVALID`` (HTTP 400, NR-010) —
  ``backend/src/proxy/http/gate/orchestrator.rs:2325``. So an
  operator approval for an MCP tool could NEVER be spent: the row was
  created, the operator approved it, and the re-execute was refused.
* Where the digest arm did run, ``server_validate_tool_argument_binding``
  compares ``BusinessImpact::ToolCall.params`` with the live
  ``gate_req.tool_arguments`` — with no envelope it is a no-op, so the
  approval bound the tool NAME but not the ARGUMENTS.

ADR-125 closes both: the adapter derives ONE envelope from the tool
call it is about to make — the tool name plus the EXACT argument bag
it forwards — and its digest, and puts both on the wire.

The doubles here are deliberately shallow: a mock MCP client and a
fake runtime that records the two wire hops (``check_workflow_budget``
stands for ``/gate``, ``execute`` for ``/execute``). Nothing between
the adapter and the fake runtime is faked, so a change that drops the
envelope turns these red rather than being stubbed around.
"""

from __future__ import annotations

from typing import Any

import pytest

from nullrun.business_impact import BusinessImpact, compute_action_digest
from nullrun.context import (
    clear_server_minted_execution_id,
    get_call_impact,
    set_call_impact,
    set_mcp_tool_context,
)
from nullrun.toolbox.mcp import MCPAdapter

# The backend's golden hex for ``BusinessImpact::tool_call`` with the
# tool ``stripe.charge`` and the params ``{"region": "EU",
# "amount": 500}`` (backend/src/proxy/gate/business_impact.rs::
# DIGEST_FIXTURE_HEX_TOOL_CALL, mirrored by
# tests/test_business_impact_tool_call.py). The adapter derives its
# envelope with the SAME production canonicaliser, so the digest it
# puts on the wire for that call IS the backend's digest — the two
# repos cannot drift apart here without this going red.
GOLDEN_HEX_TOOL_CALL = (
    "9975a8b75a436fb78b9d141b9e0c0a90838c1243d78119b304ae6ed0526966a6"
)


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
    the extra kwargs — the gate fields must never travel into the MCP
    server.

    Unlike the double in ``tests/test_mcp_approval_wire.py`` this one
    accepts ANY tool name: these tests call tools that are not in the
    cached inventory (``stripe.charge``, ``delete_file``) to prove
    properties of the ENVELOPE, and the adapter's own
    unknown-tool path (``class='invalid'``) is not what is under test
    here.
    """

    def __init__(self, tools):
        self._tools = {t.name: t for t in tools}
        self.calls: list[dict[str, Any]] = []

    def list_tools(self):
        return list(self._tools.values())

    def call_tool(self, name, arguments=None, **kwargs):
        record = {"name": name, "arguments": arguments or {}, "kwargs": kwargs}
        self.calls.append(record)
        return f"ok:{name}"


class _RecordingRuntime:
    """Fake runtime recording both wire hops.

    ``check_workflow_budget`` stands for the ``/gate`` pre-flight and
    captures the envelope that is in scope at that moment — the
    property under test is that the ``/gate`` and the ``/execute`` hash
    the SAME bytes (ADR-065), so the capture has to happen inside the
    gate hop, not after the call.
    """

    def __init__(self, payload=None):
        self.execute_calls: list[dict[str, Any]] = []
        self.gate_calls = 0
        self.gate_impacts: list[Any] = []
        self._payload = payload or {
            "decision": "allow",
            "decision_source": "gateway",
            "explanation": "allow",
        }

    def check_workflow_budget(self):
        self.gate_calls += 1
        self.gate_impacts.append(get_call_impact())

    def execute(self, **kwargs):
        self.execute_calls.append(kwargs)
        return dict(self._payload)


def _inventory():
    return [
        _Tool("create_issue", _Ann(read=False, destructive=True, open_world=True)),
        _Tool("get_file_contents", _Ann(read=True, destructive=False)),
    ]


def _adapter(runtime, tools=None):
    client = _MockMcpClient(tools if tools is not None else _inventory())
    adapter = MCPAdapter(server_name="github", mcp_client=client, runtime=runtime)
    return adapter, client


@pytest.fixture(autouse=True)
def _isolate_context():
    """The three contextvars the adapter touches are process-wide, so a
    stale value from a previous test would make a test pass for the
    wrong reason."""

    clear_server_minted_execution_id()
    set_mcp_tool_context(tool_class=None, annotations=None)
    saved_impact = get_call_impact()
    yield
    clear_server_minted_execution_id()
    set_mcp_tool_context(tool_class=None, annotations=None)
    set_call_impact(saved_impact)


# ---------------------------------------------------------------------------
# T1 — the derived envelope reaches the wire
# ---------------------------------------------------------------------------


class TestTheDerivedEnvelopeReachesTheWire:
    def test_the_envelope_names_the_tool_and_carries_the_arguments(self):
        runtime = _RecordingRuntime()
        adapter, _ = _adapter(runtime)

        adapter.call_tool("create_issue", {"repo": "acme/api"})

        body = runtime.execute_calls[0]
        envelope = body["business_impact"]
        assert envelope["kind"] == "tool_call"
        assert envelope["tool_name"] == "create_issue"
        assert envelope["params"] == {"repo": "acme/api"}

    def test_the_envelope_params_are_the_canonical_bag(self):
        """The backend compares ``BusinessImpact::ToolCall.params``
        with the live ``tool_arguments`` CANONICALLY
        (``server_validate_tool_argument_binding``). Equal dicts are
        canonical-equal by construction; a masked or reshaped copy is
        not, and would bind the approval to arguments nobody
        approved."""

        runtime = _RecordingRuntime()
        adapter, _ = _adapter(runtime)

        adapter.call_tool("create_issue", {"repo": "acme/api"})

        body = runtime.execute_calls[0]
        assert body["business_impact"]["params"] == body["tool_arguments"]

    def test_the_digest_is_the_digest_of_the_envelope_on_the_wire(self):
        """Not a made-up hex: the digest on the wire is the digest OF
        the envelope on the wire. The backend re-derives it from the
        live payload and compares it with the value stored on the
        approval row."""

        runtime = _RecordingRuntime()
        adapter, _ = _adapter(runtime)

        adapter.call_tool("create_issue", {"repo": "acme/api"})

        body = runtime.execute_calls[0]
        expected = compute_action_digest(
            BusinessImpact.tool_call("create_issue", {"repo": "acme/api"})
        )
        assert body["action_digest"] == expected

    def test_the_digest_is_the_backend_golden_hex(self):
        """The cross-repo half: the adapter's digest for a known call
        is the BACKEND's digest, byte for byte."""

        runtime = _RecordingRuntime()
        adapter, _ = _adapter(runtime)

        adapter.call_tool("stripe.charge", {"region": "EU", "amount": 500})

        assert runtime.execute_calls[0]["action_digest"] == GOLDEN_HEX_TOOL_CALL

    def test_a_tool_call_without_arguments_still_carries_an_envelope(self):
        runtime = _RecordingRuntime()
        adapter, _ = _adapter(runtime)

        adapter.call_tool("get_file_contents")

        body = runtime.execute_calls[0]
        assert body["business_impact"]["params"] == {}
        assert body["action_digest"] == compute_action_digest(
            BusinessImpact.tool_call("get_file_contents")
        )


# ---------------------------------------------------------------------------
# T2 — the digest binds the ARGUMENTS, not just the tool name
# ---------------------------------------------------------------------------


class TestTheDigestBindsTheArguments:
    def test_changing_an_argument_changes_the_digest(self):
        """The hole ADR-125 closes: an approval for
        ``delete_file(path=/tmp/x)`` must not be spendable on
        ``delete_file(path=/etc/passwd)``."""

        runtime = _RecordingRuntime()
        adapter, _ = _adapter(runtime)

        adapter.call_tool("delete_file", {"path": "/tmp/x"})
        adapter.call_tool("delete_file", {"path": "/etc/passwd"})

        first, second = (call["action_digest"] for call in runtime.execute_calls)
        assert first != second, "the approval would bind the tool name only"

    def test_changing_the_tool_changes_the_digest(self):
        """The tool name is inside the hashed bytes, so a grant for one
        tool does not validate a replay of another."""

        runtime = _RecordingRuntime()
        adapter, _ = _adapter(runtime)

        adapter.call_tool("create_issue", {"repo": "acme/api"})
        adapter.call_tool("delete_issue", {"repo": "acme/api"})

        first, second = (call["action_digest"] for call in runtime.execute_calls)
        assert first != second


# ---------------------------------------------------------------------------
# T3 — the approval retry carries the SAME envelope
# ---------------------------------------------------------------------------


class TestTheApprovalRetryCarriesTheSameEnvelope:
    def test_the_retry_derives_the_same_digest_as_the_first_hop(self):
        """The operator approves, the caller retries with
        ``approval_id=``. The backend compares the digest it
        re-derives from the live payload with the digest STORED on the
        approval row — which was stamped from the FIRST hop. A retry
        that derives a different digest is refused with
        APPROVAL_DIGEST_MISMATCH, so the two hops must agree."""

        runtime = _RecordingRuntime()
        adapter, _ = _adapter(runtime)

        adapter.call_tool("create_issue", {"repo": "acme/api"})
        adapter.call_tool(
            "create_issue", {"repo": "acme/api"}, approval_id="apr-123"
        )

        first, second = (call["action_digest"] for call in runtime.execute_calls)
        assert first == second
        assert second == runtime.execute_calls[1]["action_digest"]

    def test_the_retry_carries_the_envelope_too(self):
        runtime = _RecordingRuntime()
        adapter, _ = _adapter(runtime)

        adapter.call_tool(
            "create_issue", {"repo": "acme/api"}, approval_id="apr-123"
        )

        body = runtime.execute_calls[0]
        assert body["business_impact"]["params"] == {"repo": "acme/api"}
        assert body["approval_id"] == "apr-123"


# ---------------------------------------------------------------------------
# T4 — ONE envelope per logical action (ADR-065)
# ---------------------------------------------------------------------------


class TestOneEnvelopePerLogicalAction:
    def test_the_gate_and_the_execute_carry_the_same_bytes(self):
        """The ``/gate`` pre-flight and the ``/execute`` must hash the
        SAME envelope. The adapter installs the derived envelope on the
        call context before the pre-flight runs, so the pre-flight
        reads exactly what the ``/execute`` carries."""

        runtime = _RecordingRuntime()
        adapter, _ = _adapter(runtime)

        adapter.call_tool("create_issue", {"repo": "acme/api"})

        assert runtime.gate_calls == 1
        gate_impact = runtime.gate_impacts[0]
        assert gate_impact is not None, "the /gate saw no envelope at all"
        body = runtime.execute_calls[0]
        assert gate_impact.to_wire_dict() == body["business_impact"]
        assert compute_action_digest(gate_impact) == body["action_digest"]

    def test_the_envelope_is_released_after_the_call(self):
        """A leaked envelope would bind the NEXT action's approval to
        this call's tool and arguments."""

        runtime = _RecordingRuntime()
        adapter, _ = _adapter(runtime)

        adapter.call_tool("create_issue", {"repo": "acme/api"})

        assert get_call_impact() is None

    def test_the_envelope_is_released_even_when_the_call_is_refused(self):
        runtime = _RecordingRuntime(
            payload={
                "decision": "block",
                "decision_source": "gateway",
                "explanation": "destructive tool blocked",
                "workflow_id": "wf-1",
            }
        )
        adapter, _ = _adapter(runtime)

        from nullrun.breaker.exceptions import NullRunBlockedException

        with pytest.raises(NullRunBlockedException):
            adapter.call_tool("create_issue", {"repo": "acme/api"})

        assert get_call_impact() is None

    def test_an_envelope_already_in_scope_is_not_overwritten(self):
        """The ``@protect`` path owns its own envelope and its own
        ``/gate``/``/execute`` pair. Overwriting it would make the two
        hops disagree — the exact drift ADR-065 exists to prevent."""

        runtime = _RecordingRuntime()
        adapter, _ = _adapter(runtime)
        existing = BusinessImpact.tool_call("protected_fn", {"a": 1})
        set_call_impact(existing)

        adapter.call_tool("create_issue", {"repo": "acme/api"})

        assert runtime.gate_impacts[0] is existing
        assert get_call_impact() is existing


# ---------------------------------------------------------------------------
# T5 — the fail-CLOSED fallback
# ---------------------------------------------------------------------------


class TestTheUnbuildableEnvelopeFailsClosed:
    def test_a_float_argument_leaves_the_envelope_absent(self):
        """The digest layer cannot round-trip an f64 — serde and Python
        serialise it differently, so the two repos would never agree on
        the hashed bytes. The backend's own ``ToolCallParams::validate``
        rejects it too."""

        runtime = _RecordingRuntime()
        adapter, _ = _adapter(runtime)

        adapter.call_tool("charge_card", {"amount": 1.5})

        body = runtime.execute_calls[0]
        assert "business_impact" not in body, "an unround-trippable envelope went out"

    def test_a_float_argument_still_carries_a_digest(self):
        """The digest is still sent — over the ``no_impact`` sentinel.
        The backend's post-approval re-entry then takes its fail-CLOSED
        arm ("cannot recompute") instead of the legacy "no digest at
        all" arm, which is a grant bound to nothing."""

        runtime = _RecordingRuntime()
        adapter, _ = _adapter(runtime)

        adapter.call_tool("charge_card", {"amount": 1.5})

        body = runtime.execute_calls[0]
        assert body["action_digest"] == compute_action_digest(
            BusinessImpact.no_impact()
        )

    def test_a_non_ascii_tool_name_leaves_the_envelope_absent(self):
        runtime = _RecordingRuntime()
        adapter, _ = _adapter(runtime)

        adapter.call_tool("создать_задачу", {"repo": "acme/api"})

        body = runtime.execute_calls[0]
        assert "business_impact" not in body
        assert "action_digest" in body


# ---------------------------------------------------------------------------
# T6 — a caller-supplied envelope wins
# ---------------------------------------------------------------------------


class TestACallerEnvelopeIsForwardedVerbatim:
    def test_a_caller_envelope_is_not_replaced_by_a_derived_one(self):
        runtime = _RecordingRuntime()
        adapter, _ = _adapter(runtime)
        envelope = {
            "kind": "tool_call",
            "tool_name": "create_issue",
            "params": {"repo": "acme/api"},
            "extractor_id": "nullrun.tool_call.path",
            "extractor_version": "1",
        }
        digest = compute_action_digest(
            BusinessImpact.tool_call("create_issue", {"repo": "acme/api"})
        )

        adapter.call_tool(
            "create_issue",
            {"repo": "acme/api"},
            business_impact=envelope,
            action_digest=digest,
        )

        body = runtime.execute_calls[0]
        assert body["business_impact"] == envelope
        assert body["action_digest"] == digest

    def test_the_gate_never_sees_a_caller_envelope_it_did_not_carry(self):
        """A caller envelope is NOT installed on the call context: the
        SDK cannot know that the caller's envelope describes THIS
        call. The contextvar keeps carrying whatever the caller's own
        integration put there."""

        runtime = _RecordingRuntime()
        adapter, _ = _adapter(runtime)

        adapter.call_tool(
            "create_issue",
            {"repo": "acme/api"},
            business_impact={
                "kind": "tool_call",
                "tool_name": "create_issue",
                "params": {},
                "extractor_id": "nullrun.tool_call.path",
                "extractor_version": "1",
            },
            action_digest="ab" * 32,
        )

        assert runtime.gate_impacts[0] is None


# ---------------------------------------------------------------------------
# T7 — the gate fields never reach the MCP server
# ---------------------------------------------------------------------------


class TestTheEnvelopeNeverReachesTheMcpServer:
    def test_the_derived_envelope_stays_on_the_gate_side(self):
        runtime = _RecordingRuntime()
        adapter, client = _adapter(runtime)

        adapter.call_tool("create_issue", {"repo": "acme/api"})

        kwargs = client.calls[0]["kwargs"]
        assert "business_impact" not in kwargs
        assert "action_digest" not in kwargs
        assert "approval_id" not in kwargs

    def test_the_tool_payload_is_unchanged(self):
        """The envelope is an ADDITIONAL gate field — the MCP server
        still receives exactly the arguments it was given."""

        runtime = _RecordingRuntime()
        adapter, client = _adapter(runtime)

        adapter.call_tool("create_issue", {"repo": "acme/api"})

        assert client.calls[0]["arguments"] == {"repo": "acme/api"}
        assert client.calls[0]["name"] == "create_issue"
