"""`@protect`: /gate and /execute must carry the SAME envelope.

ADR-065 decision step 4 and verification bullet 2.

The backend does not trust the `action_digest` the SDK sends. At
`/execute` it RECOMPUTES the digest from the `business_impact` in
that request and compares it to the digest STORED on the approval row
at `/gate` time (`payload_binding.rs:163`, `orchestrator.rs:1511`).
So the two requests have to carry the same envelope — which is why
`@protect` builds it once, before the pre-flight, and both calls read
it off the call context.

Until ADR-065 both carried a constant `{"kind": "none"}`. That is
DEF-TC14-002: the operator approves, and the agent still cannot run
the tool — and even if it could, the grant was bound to nothing, so
it was replayable as any other action.

These assert the CAPTURED request bodies of both endpoints. A digest
is opaque on its own, so the round-trip is checked the way the server
checks it: recompute from the envelope the body actually carried and
compare to the digest the same body carried.
"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest
import respx

import nullrun
from nullrun.business_impact import DIGEST_PREFIX, BusinessImpact

BASE_URL = "https://api.test.nullrun.io"


def _server_recompute(envelope: dict) -> str:
    """What the backend derives from an envelope it received.

    Deliberately NOT the SDK's own helper: if both sides called
    `compute_action_digest`, the test would pass even with the wrong
    envelope on the wire.
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
def captured():
    """Capture the bodies of both gate endpoints. Both answer allow."""
    bodies: dict[str, list[dict]] = {"gate": [], "execute": []}

    def _gate(request: httpx.Request) -> httpx.Response:
        bodies["gate"].append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "decision": "allow",
                "actions": [],
                "local_cost_cents": 0,
                "policy_id": "policy-test",
                "decision_source": "gateway",
            },
        )

    def _execute(request: httpx.Request) -> httpx.Response:
        bodies["execute"].append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "decision": "allow",
                "decision_source": "gateway",
                "explanation": "allowed",
                "policy_version": 1,
            },
        )

    respx.post(f"{BASE_URL}/api/v1/gate").mock(side_effect=_gate)
    respx.post(f"{BASE_URL}/api/v1/execute").mock(side_effect=_execute)
    return bodies


def _last(captured, endpoint):
    assert captured[endpoint], f"no /{endpoint} call was captured"
    return captured[endpoint][-1]


class TestBothEndpointsCarryOneEnvelope:
    def test_envelopes_are_identical(self, make_runtime, mock_api, captured):
        @nullrun.protect
        def refund_customer(amount: int, currency: str = "EUR") -> str:
            return "ok"

        make_runtime()
        assert refund_customer(amount=500) == "ok"

        gate = _last(captured, "gate")
        execute = _last(captured, "execute")
        assert gate["business_impact"] == execute["business_impact"]

    def test_digests_are_identical(self, make_runtime, mock_api, captured):
        @nullrun.protect
        def refund_customer(amount: int) -> str:
            return "ok"

        make_runtime()
        assert refund_customer(amount=500) == "ok"
        assert _last(captured, "gate")["action_digest"] == _last(captured, "execute")[
            "action_digest"
        ]

    def test_server_can_recompute_the_gate_digest(self, make_runtime, mock_api, captured):
        # What the backend does when it stores the approval row.
        @nullrun.protect
        def refund_customer(amount: int) -> str:
            return "ok"

        make_runtime()
        refund_customer(amount=500)
        gate = _last(captured, "gate")
        assert _server_recompute(gate["business_impact"]) == gate["action_digest"]

    def test_server_can_recompute_the_execute_digest(self, make_runtime, mock_api, captured):
        # What the backend does at /execute, which is where DEF-TC14-002
        # failed: 400 BUSINESS_IMPACT_INVALID when the envelope was
        # absent, 422 VALIDATION_ERROR when it was the `none` sentinel.
        @nullrun.protect
        def refund_customer(amount: int) -> str:
            return "ok"

        make_runtime()
        refund_customer(amount=500)
        execute = _last(captured, "execute")
        assert execute["business_impact"] is not None
        assert _server_recompute(execute["business_impact"]) == execute["action_digest"]

    def test_stored_and_recomputed_digests_agree(self, make_runtime, mock_api, captured):
        # The end-to-end property, stated as the server states it.
        @nullrun.protect
        def refund_customer(amount: int) -> str:
            return "ok"

        make_runtime()
        refund_customer(amount=500)
        stored = _last(captured, "gate")
        recomputed = _last(captured, "execute")
        assert _server_recompute(recomputed["business_impact"]) == stored["action_digest"]


class TestTheEnvelopeNamesTheAction:
    def test_tool_name_is_the_wrapped_function(self, make_runtime, mock_api, captured):
        @nullrun.protect
        def charge_card(amount: int) -> str:
            return "ok"

        make_runtime()
        charge_card(amount=500)
        assert _last(captured, "execute")["business_impact"]["tool_name"] == "charge_card"

    def test_params_carry_the_masked_kwargs(self, make_runtime, mock_api, captured):
        @nullrun.protect
        def charge_card(amount: int) -> str:
            return "ok"

        make_runtime()
        charge_card(amount=500)
        params = _last(captured, "execute")["business_impact"]["params"]
        assert params["kwargs"]["amount"] == "500"
        assert params["args"] == []

    def test_sensitive_kwargs_are_masked_in_the_digested_params(
        self, make_runtime, mock_api, captured
    ):
        # The digest must cover what the OPERATOR saw on the approval
        # card, which is the masked bag — never the raw secret.
        @nullrun.protect
        def charge_card(credit_card_number: str, amount: int) -> str:
            return "ok"

        make_runtime()
        charge_card(credit_card_number="4111111111111111", amount=500)
        params = _last(captured, "execute")["business_impact"]["params"]
        assert params["kwargs"]["credit_card_number"] == "***"
        assert "4111111111111111" not in json.dumps(params)

    def test_positional_arguments_are_covered(self, make_runtime, mock_api, captured):
        # A tool called purely positionally must still bind its
        # arguments, or charge_card("x", 50) and charge_card("x", 5000)
        # would be the same action.
        @nullrun.protect
        def charge_card(credit_card_number: str, amount: int) -> str:
            return "ok"

        make_runtime()
        charge_card("4111111111111111", 50)
        assert _last(captured, "execute")["business_impact"]["params"]["args"] == [
            "***",
            "50",
        ]


class TestTheBindingActuallyBinds:
    """The property NR-010 was raised to protect."""

    def _digest_for(self, make_runtime, captured, call):
        make_runtime()
        call()
        return _last(captured, "gate")["action_digest"]

    def test_a_different_amount_is_a_different_action(self, make_runtime, mock_api, captured):
        @nullrun.protect
        def refund_customer(amount: int) -> str:
            return "ok"

        make_runtime()
        refund_customer(amount=500)
        approved = _last(captured, "gate")["action_digest"]
        refund_customer(amount=500_000)
        tampered = _last(captured, "gate")["action_digest"]
        assert approved != tampered

    def test_a_different_tool_is_a_different_action(self, make_runtime, mock_api, captured):
        @nullrun.protect
        def refund_customer(amount: int) -> str:
            return "ok"

        @nullrun.protect
        def charge_card(amount: int) -> str:
            return "ok"

        make_runtime()
        refund_customer(amount=500)
        approved = _last(captured, "gate")["action_digest"]
        charge_card(amount=500)
        assert _last(captured, "gate")["action_digest"] != approved

    def test_a_tampered_replay_does_not_land_on_the_approved_digest(
        self, make_runtime, mock_api, captured
    ):
        # The exact shape of the attack ADR-065 closes: reuse the
        # approval_id/digest granted for amount=500 to run amount=5000.
        @nullrun.protect
        def refund_customer(amount: int) -> str:
            return "ok"

        make_runtime()
        refund_customer(amount=500)
        approved = _last(captured, "gate")["action_digest"]
        refund_customer(amount=5000)
        replay = _last(captured, "execute")
        assert _server_recompute(replay["business_impact"]) != approved

    def test_a_tool_call_is_not_the_none_sentinel(self, make_runtime, mock_api, captured):
        from nullrun.business_impact import compute_action_digest

        sentinel = compute_action_digest(BusinessImpact.no_impact())

        @nullrun.protect
        def refund_customer(amount: int) -> str:
            return "ok"

        make_runtime()
        refund_customer(amount=500)
        assert _last(captured, "gate")["action_digest"] != sentinel
        assert _last(captured, "execute")["action_digest"] != sentinel


class TestEnvelopeIsScopedToTheCall:
    def test_envelope_is_cleared_after_the_call(self, make_runtime, mock_api, captured):
        from nullrun.context import get_call_impact

        @nullrun.protect
        def refund_customer(amount: int) -> str:
            return "ok"

        make_runtime()
        refund_customer(amount=500)
        # Leaving it set would make an unrelated LLM check report a
        # tool_call impact for a call that has no tool in it.
        assert get_call_impact() is None

    def test_nested_protect_restores_the_outer_envelope(self, make_runtime, mock_api, captured):
        from nullrun.context import get_call_impact

        @nullrun.protect
        def inner(amount: int) -> str:
            return "ok"

        @nullrun.protect
        def outer(amount: int) -> str:
            return inner(amount=amount)

        make_runtime()
        outer(amount=500)
        assert get_call_impact() is None
        envelopes = [b["business_impact"]["tool_name"] for b in captured["execute"]]
        assert envelopes == ["outer", "inner"]


class TestUnbuildableEnvelopeDegradesLoudly:
    """`_build_call_impact` can fall back to no_impact().

    ADR-065 retires the `none` sentinel FROM THE TOOL PATH, which is
    only true modulo this branch: a tool name the backend's validator
    rejects (non-ASCII, over 128 bytes) or an argument the digest layer
    cannot round-trip falls back to `no_impact()` and logs. The
    backend then stores a digest that binds nothing and refuses the
    re-entry — fail-CLOSED on the server, fail-OPEN in the SDK's own
    metadata. That trade is deliberate and is pinned here so it stays
    deliberate rather than becoming an unnoticed hole.
    """

    def test_non_ascii_tool_name_degrades_instead_of_raising(self, make_runtime, mock_api, captured):
        # The backend's `ToolCallParams::validate` requires printable
        # ASCII (business_impact.rs:320-323). A tool named in Cyrillic
        # cannot be described by a valid envelope at all.
        from nullrun.decorators import _build_call_impact

        def refund_клиент():
            pass

        impact = _build_call_impact(refund_клиент, (), {})
        assert impact.kind == "none"

    def test_overlong_tool_name_degrades(self, make_runtime, mock_api, captured):
        from nullrun.decorators import _build_call_impact

        def tool():
            pass

        tool.__name__ = "t" * 129
        assert _build_call_impact(tool, (), {}).kind == "none"

    def test_a_valid_tool_never_degrades(self, make_runtime, mock_api, captured):
        from nullrun.decorators import _build_call_impact

        def refund_customer(amount: int):
            pass

        assert _build_call_impact(refund_customer, (), {"amount": 500}).kind == "tool_call"

    def test_degraded_tool_still_runs_but_binds_nothing(self, make_runtime, mock_api, captured):
        # Documented consequence: the body is NOT blocked, the approval
        # simply carries no trust binding, so /execute is refused.
        @nullrun.protect
        def refund_клиент(amount: int) -> str:
            return "ok"

        make_runtime()
        assert refund_клиент(amount=500) == "ok"
        from nullrun.business_impact import compute_action_digest

        sentinel = compute_action_digest(BusinessImpact.no_impact())
        assert _last(captured, "gate")["action_digest"] == sentinel
