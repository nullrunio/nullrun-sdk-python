"""The per-action BusinessImpact contextvar (ADR-065 step 2).

``/gate`` and ``/execute`` are two HTTP calls issued from two
different places, and the backend compares the digest it RECOMPUTES
at ``/execute`` against the one it STORED at ``/gate``
(``payload_binding.rs:163``, ``orchestrator.rs:1511``). The two must
therefore read the SAME envelope. This contextvar is what makes that
possible without threading an argument through both call sites.

The pre-0.21 SDK sent a constant ``{"kind": "none"}`` sentinel from
both places instead. A constant hashes to a constant, so the digests
always agreed — while binding the approval to nothing at all. That
is DEF-TC14-002: the operator approves, and the agent still cannot
run the tool.
"""

from __future__ import annotations

import pytest

from nullrun.business_impact import BusinessImpact, compute_action_digest
from nullrun.context import (
    get_call_impact,
    reset_call_impact,
    set_call_impact,
)


@pytest.fixture(autouse=True)
def _clear_impact():
    """Never leak an envelope into another test."""
    yield
    set_call_impact(None)


class TestEnvelopeLivesOnTheContext:
    def test_default_is_none(self):
        # "no envelope in scope" is distinct from "an envelope that
        # says no impact". Collapsing the two is what produced the
        # sentinel.
        assert get_call_impact() is None

    def test_set_then_get_returns_the_same_object(self):
        impact = BusinessImpact.tool_call("refund_customer", {"amount": 500})
        set_call_impact(impact)
        assert get_call_impact() is impact

    def test_set_none_clears(self):
        set_call_impact(BusinessImpact.tool_call("t"))
        set_call_impact(None)
        assert get_call_impact() is None

    def test_reset_restores_the_previous_envelope(self):
        first = BusinessImpact.tool_call("first")
        second = BusinessImpact.tool_call("second")
        set_call_impact(first)
        inner = set_call_impact(second)
        reset_call_impact(inner)
        assert get_call_impact() is first

    def test_reset_of_the_outermost_set_returns_to_none(self):
        # The token from the FIRST set restores the value that was in
        # scope before it, which is None by default. Nesting is what
        # the inner token above is for.
        token = set_call_impact(BusinessImpact.tool_call("t"))
        reset_call_impact(token)
        assert get_call_impact() is None


class TestTheTwoCallsHashTheSameBytes:
    """The property the contextvar exists to provide."""

    def test_gate_and_execute_read_one_envelope(self):
        # Simulates the two call sites: the /gate pre-flight reads the
        # context, then @protect reads the same context later. The
        # digests the backend stores and recomputes are these two.
        set_call_impact(BusinessImpact.tool_call("refund_customer", {"amount": 500}))
        stored_at_gate = compute_action_digest(get_call_impact())
        recomputed_at_execute = compute_action_digest(get_call_impact())
        assert stored_at_gate == recomputed_at_execute

    def test_a_different_tool_produces_a_different_digest(self):
        # The property the constant sentinel destroyed: an approval
        # granted for one tool must not be replayable as another.
        set_call_impact(BusinessImpact.tool_call("refund_customer", {"amount": 500}))
        approved = compute_action_digest(get_call_impact())
        set_call_impact(BusinessImpact.tool_call("charge_card", {"amount": 500}))
        assert compute_action_digest(get_call_impact()) != approved

    def test_a_tampered_argument_bag_produces_a_different_digest(self):
        set_call_impact(BusinessImpact.tool_call("refund_customer", {"amount": 500}))
        approved = compute_action_digest(get_call_impact())
        set_call_impact(BusinessImpact.tool_call("refund_customer", {"amount": 500_000}))
        assert compute_action_digest(get_call_impact()) != approved

    def test_no_impact_is_not_equivalent_to_any_tool_call(self):
        set_call_impact(BusinessImpact.no_impact())
        sentinel = compute_action_digest(get_call_impact())
        set_call_impact(BusinessImpact.tool_call("refund_customer"))
        assert compute_action_digest(get_call_impact()) != sentinel
