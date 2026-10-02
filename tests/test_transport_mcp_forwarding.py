"""DEF-TC29-001 — MCP tool class + annotations must reach ``/gate``.

Found 2026-10-02 under QA ``RUN_ID 20261002T0826`` (suite TS-12,
TC-29). SDK 0.20.0 against prod ``e8d811a0``.

## The defect

``NullRunRuntime.check_workflow_budget`` computes the MCP forwarding
fields correctly (``runtime.py:2383-2388``)::

    mcp_class = get_call_mcp_class()
    if mcp_class is not None:
        check_req["tool_class"] = mcp_class
    mcp_annotations = get_call_mcp_annotations()
    if mcp_annotations is not None:
        check_req["mcp_annotations"] = mcp_annotations

...and then hands ``check_req`` to ``Transport.check``, which does not
send ``check_req``. It **rebuilds** the body from an explicit
allowlist (``transport.py:1524-1542`` plus the conditional forwards at
``:1545-1575``) and neither field is on it. The values are dropped
without a word.

Observed on the wire — ``set_mcp_tool_context(tool_class="mcp",
annotations={"read_only": False, "destructive": True, "open_world":
False})`` followed by one ``check_workflow_budget()``:

    GATE BODY KEYS: ['action_digest', 'check_type', 'estimated_tokens',
      'execution_id', 'idempotency_key', 'input', 'mode', 'model',
      'operation_id', 'organization_id', 'stream', 'tool', 'tools',
      'trace_id']
      tool_class      = <ABSENT>
      mcp_annotations = <ABSENT>

The public ``set_mcp_tool_context`` API and the whole
``nullrun.toolbox.mcp`` auto-classification path are therefore
non-functional end to end: the SDK can never tell the gate that a tool
is ``destructive`` or ``read_only``.

## Why it matters even while ADR-013 is dormant

The backend is complete and explicitly documents the contract it
expects (``backend/src/proxy/http/gate/internal.rs:318-341``): *"SDKs
that recognise an MCP server cache the ``tools/list`` entry and pass
the canonical ``Mcp`` class plus the corresponding ``McpAnnotations``"*.
``effective_tool_class()`` falls back to ``classify_tool`` on the raw
string when the field is absent, so today this is a **dead feature
with a misleading API** rather than a live bypass — the day the
server-side flag flips, destructive MCP tools will silently fall back
to name-based classification.

## Test shape

These are wire-shape assertions, not source pins: they capture the
actual POST body. A source pin would keep passing through any future
refactor that reintroduces the drop at a different line.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from nullrun.transport import Transport

GATE_URL = "https://api.test.nullrun.io/api/v1/gate"

# The backend's `McpAnnotations` keys (tool_canonical.rs:229-249).
# NOT the MCP wire names `destructiveHint` / `readonlyHint` — the
# SDK's `set_mcp_tool_context` docstring pins these three.
MCP_ANNOTATIONS = {"read_only": False, "destructive": True, "open_world": False}


@pytest.fixture
def transport():
    t = Transport(api_url="https://api.test.nullrun.io", api_key="test-key-12345678")
    yield t
    t.stop()


def _base_check_request(**overrides) -> dict:
    req = {
        "organization_id": "org-1",
        "execution_id": "0198aaaa-0001-7000-8000-000000000001",
        "operation_id": "op-1",
        "check_type": "tool",
        "tool": "mcp__github__delete_branch",
        "estimated_tokens": 1,
        "action_digest": "digest-1",
    }
    req.update(overrides)
    return req


def _sent_body(route) -> dict:
    return json.loads(route.calls[0].request.content.decode("utf-8"))


class TestMcpForwardingReachesTheWire:
    """The two fields `check_workflow_budget` computes must survive."""

    @respx.mock
    def test_tool_class_is_forwarded(self, transport):
        route = respx.post(GATE_URL).mock(
            return_value=httpx.Response(200, json={"decision": "allow"})
        )
        transport.check(_base_check_request(tool_class="mcp"))

        assert route.called
        assert _sent_body(route)["tool_class"] == "mcp", (
            "DEF-TC29-001: Transport.check rebuilds the /gate body from an "
            "explicit allowlist (transport.py:1524-1542) that has no "
            "`tool_class` key, so the value runtime.py:2385 sets is "
            "silently dropped before the wire"
        )

    @respx.mock
    def test_mcp_annotations_are_forwarded(self, transport):
        route = respx.post(GATE_URL).mock(
            return_value=httpx.Response(200, json={"decision": "allow"})
        )
        transport.check(_base_check_request(mcp_annotations=dict(MCP_ANNOTATIONS)))

        assert route.called
        assert _sent_body(route)["mcp_annotations"] == MCP_ANNOTATIONS, (
            "DEF-TC29-001: `mcp_annotations` is not on Transport.check's "
            "allowlist, so a tool declared `destructive` reaches the gate "
            "as unknown and mcp_destructive_policy can never fire"
        )

    @respx.mock
    def test_both_fields_survive_together(self, transport):
        route = respx.post(GATE_URL).mock(
            return_value=httpx.Response(200, json={"decision": "allow"})
        )
        transport.check(
            _base_check_request(tool_class="mcp", mcp_annotations=dict(MCP_ANNOTATIONS))
        )

        body = _sent_body(route)
        assert body["tool_class"] == "mcp"
        assert body["mcp_annotations"] == MCP_ANNOTATIONS


class TestAbsentFieldsStayAbsent:
    """Forwarding is conditional — the backend pins this on its side.

    `internal.rs:8291-8295` asserts `tool_class=None` and
    `mcp_annotations=None` must NOT appear in the JSON. A `"key" in
    check_request` guard would send them as null and break that pin
    plus every pre-MCP SDK's wire shape, so the fix must test for
    `is not None`, not for presence.
    """

    @respx.mock
    def test_no_keys_when_sdk_has_no_opinion(self, transport):
        route = respx.post(GATE_URL).mock(
            return_value=httpx.Response(200, json={"decision": "allow"})
        )
        transport.check(_base_check_request())

        body = _sent_body(route)
        assert "tool_class" not in body
        assert "mcp_annotations" not in body

    @respx.mock
    def test_explicit_none_is_not_serialised(self, transport):
        """`None` means "I don't know" and must stay off the wire.

        The backend treats an absent annotation as *unknown*, not as
        false (`internal.rs:334-339`). Serialising `null` would be a
        different value with a different meaning.
        """
        route = respx.post(GATE_URL).mock(
            return_value=httpx.Response(200, json={"decision": "allow"})
        )
        transport.check(
            _base_check_request(tool_class=None, mcp_annotations=None)
        )

        body = _sent_body(route)
        assert "tool_class" not in body
        assert "mcp_annotations" not in body
