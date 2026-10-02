"""`/gate` must carry the envelope its `action_digest` was computed over.

The backend stores `action_digest` on the approval row at `/gate` and,
at `/execute`, RECOMPUTES it from the envelope in that request and
compares (`payload_binding.rs:163`, `orchestrator.rs:1511`). A digest
without its envelope is unusable: the server has no way to reproduce
what it stored, and the re-entry fails CLOSED with
``APPROVAL_DIGEST_MISMATCH``.

These tests assert the CAPTURED POST BODY, not the return value.
`Transport.check` is an allowlist BUILDER, not a pass-through — it
rebuilds the body from an explicit key list — so a field computed
upstream can be dropped in transit without any error surfacing. That
is exactly what happened to `approval_id` (TC-14) and to
`tool_class` / `mcp_annotations` (DEF-TC29-001, commit b64dc8a), and
a source pin would have kept passing through any refactor that
reintroduced the drop at a different line.
"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest
import respx

from nullrun.business_impact import BusinessImpact, DIGEST_PREFIX
from nullrun.context import set_call_impact
from nullrun.transport import Transport

GATE_URL = "https://api.test.nullrun.io/api/v1/gate"
ORG_ID = "org-1"


def _sent_body(route) -> dict:
    return json.loads(route.calls[0].request.content.decode("utf-8"))


def _server_recompute(envelope: dict) -> str:
    """What the backend derives from an envelope it receives.

    Mirrors `server_derive_action_digest` (payload_binding.rs:163)
    plus `canonicalize_json` (business_impact.rs:411-439): sort keys
    recursively, compact separators, SHA-256 over prefix + bytes.
    Recomputing it here rather than calling the SDK's own helper is
    the point -- if both sides used the same function, the test would
    pass even when the envelope on the wire was wrong.
    """

    def sort_keys(value):
        if isinstance(value, dict):
            return {k: sort_keys(v) for k, v in sorted(value.items())}
        if isinstance(value, list):
            return [sort_keys(v) for v in value]
        return value

    canonical = json.dumps(
        sort_keys(envelope), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(DIGEST_PREFIX + canonical).hexdigest()


@pytest.fixture
def transport():
    t = Transport(api_url="https://api.test.nullrun.io", api_key="test-key-12345678")
    yield t
    t.stop()


@pytest.fixture(autouse=True)
def _clear_impact():
    yield
    set_call_impact(None)


def _check(transport, **overrides) -> dict:
    check_request = {
        "organization_id": ORG_ID,
        "execution_id": "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0001",
        "check_type": "llm",
        "model": "claude-sonnet-4-6",
        "estimated_tokens": 1,
        "stream": False,
        "operation_id": "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0002",
    }
    check_request.update(overrides)
    with respx.mock:
        route = respx.post(GATE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "decision": "allow",
                    "execution_id": check_request["execution_id"],
                    "remaining_budget_cents": 10000,
                },
            )
        )
        transport.check(check_request)
        assert route.called, "no /gate request was made"
        return _sent_body(route)


class TestEnvelopeReachesTheWire:
    def test_tool_call_envelope_is_forwarded(self, transport):
        impact = BusinessImpact.tool_call("refund_customer", {"amount": 500})
        body = _check(transport, business_impact=impact.to_wire_dict())
        assert body["business_impact"] == {
            "kind": "tool_call",
            "tool_name": "refund_customer",
            "params": {"amount": 500},
            "extractor_id": "nullrun.tool_call.path",
            "extractor_version": "1",
        }

    def test_none_envelope_is_forwarded(self, transport):
        # `{"kind": "none"}` is a real value, not an absence. An LLM
        # check with no tool to name sends it, and the backend tells
        # it apart from an omitted envelope.
        body = _check(transport, business_impact=BusinessImpact.no_impact().to_wire_dict())
        assert body["business_impact"] == {"kind": "none"}

    def test_envelope_is_omitted_when_absent(self, transport):
        # Pre-0.21 callers send neither envelope nor digest. The
        # backend pins the negative case too (`internal.rs:8291-8295`
        # for the sibling fields) -- `null` is a different value
        # carrying a different meaning.
        body = _check(transport)
        assert "business_impact" not in body


class TestTheServerCanReproduceTheDigest:
    """The property that makes the approval re-entry reachable."""

    def test_server_recompute_matches_the_digest_sent(self, transport):
        impact = BusinessImpact.tool_call("refund_customer", {"amount": 500})
        from nullrun.business_impact import compute_action_digest

        body = _check(
            transport,
            business_impact=impact.to_wire_dict(),
            action_digest=compute_action_digest(impact),
        )
        assert _server_recompute(body["business_impact"]) == body["action_digest"]

    def test_server_recompute_matches_for_a_non_ascii_argument_bag(self, transport):
        from nullrun.business_impact import compute_action_digest

        impact = BusinessImpact.tool_call("refund", {"note": "возврат"})
        body = _check(
            transport,
            business_impact=impact.to_wire_dict(),
            action_digest=compute_action_digest(impact),
        )
        assert _server_recompute(body["business_impact"]) == body["action_digest"]

    def test_a_tampered_envelope_does_not_reproduce_the_digest(self, transport):
        # The test that would have caught DEF-TC14-002's successor:
        # an attacker who swaps the envelope between /gate and
        # /execute must not land on the stored digest.
        from nullrun.business_impact import compute_action_digest

        impact = BusinessImpact.tool_call("refund_customer", {"amount": 500})
        tampered = BusinessImpact.tool_call("refund_customer", {"amount": 500_000})
        body = _check(
            transport,
            business_impact=tampered.to_wire_dict(),
            action_digest=compute_action_digest(impact),
        )
        assert _server_recompute(body["business_impact"]) != body["action_digest"]


class TestCheckWorkflowBudgetPopulatesTheEnvelope:
    """The real call site, not a hand-built request.

    The tests above drive `Transport.check` with an envelope supplied
    directly, so they prove the builder forwards it but say nothing
    about whether `check_workflow_budget` puts one there. A mutation
    that restores the `no_impact()` sentinel in `runtime.py` passes
    all six of them. These go through the runtime.
    """

    @pytest.fixture
    def captured_gate_bodies(self):
        bodies: list[dict] = []

        def _capture(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "decision": "allow",
                    "decision_source": "gateway",
                    "explanation": "",
                    "policy_version": 1,
                    "explanations": [],
                },
            )

        respx.post(GATE_URL).mock(side_effect=_capture)
        return bodies

    def test_context_envelope_reaches_gate(self, make_runtime, mock_api, captured_gate_bodies):
        set_call_impact(BusinessImpact.tool_call("refund_customer", {"amount": 500}))
        make_runtime().check_workflow_budget()
        assert captured_gate_bodies, "no /gate call was captured"
        body = captured_gate_bodies[-1]
        assert body["business_impact"]["kind"] == "tool_call"
        assert body["business_impact"]["tool_name"] == "refund_customer"
        assert body["business_impact"]["params"] == {"amount": 500}

    def test_gate_digest_matches_its_own_envelope(self, make_runtime, mock_api, captured_gate_bodies):
        # The two must agree ON THE WIRE. A digest the server cannot
        # recompute from the body it was sent alongside is the whole
        # of DEF-TC14-002.
        set_call_impact(BusinessImpact.tool_call("refund_customer", {"amount": 500}))
        make_runtime().check_workflow_budget()
        body = captured_gate_bodies[-1]
        assert _server_recompute(body["business_impact"]) == body["action_digest"]

    def test_no_context_envelope_sends_none(self, make_runtime, mock_api, captured_gate_bodies):
        # An LLM check with no tool to name is legitimately
        # impact-free. It must still send an envelope, because the
        # backend needs one to recompute the stored digest.
        make_runtime().check_workflow_budget()
        body = captured_gate_bodies[-1]
        assert body["business_impact"] == {"kind": "none"}
        assert _server_recompute(body["business_impact"]) == body["action_digest"]

    def test_two_different_tools_produce_two_different_gate_digests(
        self, make_runtime, mock_api, captured_gate_bodies
    ):
        rt = make_runtime()
        set_call_impact(BusinessImpact.tool_call("refund_customer", {"amount": 500}))
        rt.check_workflow_budget()
        set_call_impact(BusinessImpact.tool_call("charge_card", {"amount": 500}))
        rt.check_workflow_budget()
        first, second = captured_gate_bodies[-2], captured_gate_bodies[-1]
        assert first["action_digest"] != second["action_digest"]
        assert first["business_impact"] != second["business_impact"]
