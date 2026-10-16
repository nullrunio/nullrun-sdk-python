"""The declared output cap reaches the wire — and its absence stays absent.

ADR-016 §2.1: the backend prices an output reserve from TWO independent
signals — the caller's declared `max_output_tokens` and its own per-model
catalog cap — and refuses to reserve from either one alone. That makes
the SDK's half of the contract narrow and specific:

  1. When the caller set a cap, it is sent.
  2. When the caller did NOT, the key is ABSENT from the body. Not
     present-and-null: a null is not "I don't know", it is a value the
     backend has to defend against, and sending one turns an honest gap
     into a half-answer.
  3. A nonsensical cap (zero, negative) is not forwarded — it would let
     a caller declare "no output" and reserve nothing.

The bug this closes is silent and one-directional. The pre-flight has
always sent `estimated_tokens=1`, which prices at the INPUT rate and
covers no output at all. Output bills at the completion rate — 5× input
on the live catalog — so a real 20k-token Opus response costs 50¢
against a 1¢ reserve. /track then rejects the row with
CONSUME_OVERBUDGET and the period counter under-counts the spend. Nothing
in that path raises; the org just sees less spend than it made.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from nullrun.context import get_call_max_output_tokens, set_call_context
from nullrun.edge import token_split

BASE_URL = "https://api.test.nullrun.io"
GATE_URL = f"{BASE_URL}/api/v1/gate"


@pytest.fixture
def captured_bodies():
    """Capture every /gate body and answer `allow`, so the assertion is
    about what was SENT rather than about a decision."""
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


class TestMaxOutputTokensReachesTheWire:
    def test_a_declared_cap_is_sent(self, make_runtime, mock_api, captured_bodies):
        """The whole feature: a cap the caller declared must be visible
        to the backend, otherwise `Unavailable` holds and the reserve
        stays at the old 1¢ that under-reserves output by ~50×."""
        rt = make_runtime()
        set_call_context(model="claude-opus-4.8", max_output_tokens=20_000)
        rt.check_workflow_budget()

        assert captured_bodies, "no /gate call was captured"
        assert captured_bodies[-1].get("max_output_tokens") == 20_000, (
            "the declared output cap must reach /gate — without it the "
            "backend has one signal where it requires two and refuses "
            "to reserve any output at all"
        )

    def test_no_cap_means_the_key_is_absent_not_null(
        self, make_runtime, mock_api, captured_bodies
    ):
        """Absence is the load-bearing case: 76 of the 111 catalog rows
        have no cap either, and the default pre-flight must not
        manufacture a number to pair with them."""
        rt = make_runtime()
        set_call_context(model="claude-opus-4.8")
        rt.check_workflow_budget()

        body = captured_bodies[-1]
        assert "max_output_tokens" not in body, (
            "with no declared cap the key must be OMITTED, not sent as "
            "null. A null reads as 'the caller said zero output', which "
            "is a claim the backend would have to reject rather than an "
            "honest 'unknown', and it silently disables the reserve on "
            "every model whose catalog cap IS backfilled"
        )

    @pytest.mark.parametrize("bogus", [0, -1, -4096])
    def test_a_nonsense_cap_is_treated_as_not_declared(
        self, make_runtime, mock_api, captured_bodies, bogus
    ):
        """A non-positive cap is not a cap. Forwarding it would be a
        free way to reserve nothing on a call that does emit output —
        the exact under-reservation this exists to close, reachable
        through the new field."""
        rt = make_runtime()
        set_call_context(model="claude-opus-4.8", max_output_tokens=bogus)
        rt.check_workflow_budget()

        assert "max_output_tokens" not in captured_bodies[-1], (
            f"{bogus=} was forwarded. A cap of zero or less would let a "
            "caller reserve nothing while still making a real LLM call"
        )


class TestContextVarItself:
    """The plumbing under the wire path: a cap that never reaches the
    getter cannot reach /gate, and the getter is a public symbol users
    can read back."""

    def test_the_getter_round_trips_what_was_set(self):
        assert get_call_max_output_tokens() is None
        set_call_context(max_output_tokens=8_192)
        assert get_call_max_output_tokens() == 8_192

    def test_omitting_the_argument_leaves_the_previous_value_alone(self):
        """Same contract as `model` and `tools`: a later
        `set_call_context(tools=...)` that says nothing about the cap
        must not silently clear it. Dropping the reserve because an
        unrelated field was set would be an invisible regression for
        anyone configuring this per-tool."""
        set_call_context(max_output_tokens=8_192)
        set_call_context(tools=["read_file"])
        assert get_call_max_output_tokens() == 8_192


class TestEdgeTokenSplit:
    """The via-edge box cannot check any number it is given — it
    believes it. So every fallback in `token_split` must move TOWARD a
    larger output estimate, never toward a smaller one."""

    def test_explicit_edge_output_still_wins(self):
        """An explicitly reported output size is a better answer than
        any upper bound, so it keeps priority."""
        _, out = token_split(
            {"edge_output_tokens": 512, "max_output_tokens": 20_000, "estimated_tokens": 1}
        )
        assert out == 512

    def test_the_declared_cap_beats_the_one_token_estimate(self):
        _, out = token_split({"max_output_tokens": 20_000, "estimated_tokens": 1})
        assert out == 20_000, (
            "with a declared cap available, falling back to "
            "estimated_tokens=1 re-creates the cloud path's "
            "under-reservation on the edge path"
        )

    def test_nothing_declared_still_falls_back_to_the_estimate(self):
        """Status quo preserved for callers who set no cap — the edge
        path must not start refusing or erroring on the old shape."""
        _, out = token_split({"estimated_tokens": 1})
        assert out == 1