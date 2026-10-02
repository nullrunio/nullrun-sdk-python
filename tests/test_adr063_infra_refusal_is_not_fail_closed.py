"""ADR-063 §1.3(f): what the SDK does with an `infra` refusal.

SUPERSEDED IN PART — the rule now lives in ADR-064. ADR-063 §4.7
records the correction and points at it.

The first version of this file pinned an asymmetry that was, on
inspection, not a property of the categories but an accident of the
status code: a 403 refusal stopped the agent and a 503 refusal did
not, purely because ``400 <= status < 500`` happened to be the branch
that produced a gateway decision. The product owner ruled on
2026-09-30 that the split should follow the failure's MEANING, not
its number:

    ordinary unavailability stays fail-open; a failure OF THE CHECK
    ITSELF gets a marker the new SDK treats as a block. SDKs predating
    the category work keep failing open.

The marker is on the wire: ``category: "infra"`` alongside
``decision: "block"``, both always serialised by ``GateResponse``.
An earlier draft of this docstring said the backend "distinguishes the
two 503 groups internally via ``GateErrorCode::is_fail_closed()``" —
that was wrong. ``is_fail_closed`` is an ordinary Rust method and is
never serialised; ``GateErrorCode`` has no flag field and no response
struct carries one, so a client had nothing to read. The discriminator
that does survive the wire is ``decision``, which is what the code
below and ``categories.py:168`` actually use. This was a client-side
mapping change, not a re-architecture: a 5xx body that is a genuine
gate refusal (``decision == "block"``) is now classified and blocks,
while a 5xx that is a real outage still fails open.

The backend now answers a failed workflow-state read with **503**
(`WORKFLOW_INACTIVE_LOOKUP_FAILED` / `CIRCUIT_BREAKER_STATE_LOOKUP_FAILED`,
category `infra`) rather than the 500 it used to ship, and answers a
tripped breaker with **403** (`CIRCUIT_BREAKER_TRIPPED`, category `halt`).

Backend fail-CLOSED is not the same as user-visible fail-CLOSED. The
gate blocks in both cases, but what the SDK *does* with that block
differs, and the difference is the whole reason this file exists:

* **403** lands in `transport.py`'s `400 <= status < 500` branch and
  becomes a real `decision="block"` with `decision_source=GATEWAY`.
  `check_workflow_budget` honours it and raises. The agent stops.
* **503** is `>= 500`, so `_retry_with_backoff` retries it
  (`retry_on_5xx=True`, `max_retries=3`). The transport then asks
  whether the body is a genuine refusal. If it is, the refusal is
  classified and returned with `decision_source=GATEWAY`, and the
  agent stops. If it is not — a proxy 502, a gateway that never
  reached the gate — the synthetic `decision_source=FALLBACK` block
  still applies and `check_workflow_budget` fails OPEN, per ADR-008's
  documented "dead backend must not freeze the agent" rule.

So a tripped breaker stops the agent, a failed state read stops the
agent, and an outage does not. The last one is the load-bearing
distinction: the gate's own answer is always honoured, and only the
absence of an answer fails open.

These tests pin that so it cannot change silently, in either
direction. If a future SDK fails open on a 503 refusal, the 503 tests
go red; if one lets a 403 through as a fallback, the 403 test goes
red; if the fail-OPEN on a genuine outage is lost, the outage test
goes red.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from nullrun.breaker.categories import NullRunUnclassifiedRefusalError
from nullrun.breaker.exceptions import (
    NullRunBlockedException,
    NullRunBudgetError,
    NullRunError,
)

BASE_URL = "https://api.test.nullrun.io"
GATE_URL = f"{BASE_URL}/api/v1/gate"


def _infra_503() -> httpx.Response:
    """The exact shape the backend ships for a failed state read.

    Captured from a live gate run, not hand-assembled: the top-level
    keys are `category` / `decision` / `decision_source` / `details` /
    `explanation` / `policy_id` / `policy_version` /
    `projected_cost_cents` / `remaining_budget_cents` / `reservation_id`
    / `staleness_ms` / `user_message`.

    **`error_code` is NOT top-level.** It lives at `details.error_code`
    (`internal.rs:716`), which is where the backend's own status mapper
    reads it from (`gate.rs:88-90`). `category` and `user_message` are
    top-level fields on `GateResponse`, set by `attach_refusal_surface`
    (`gate.rs:163-172`).

    ADR-064 §Correction records an earlier version of these fixtures
    that put `error_code` at the top level; they were built from a
    hand-assembled sample rather than a captured response. The mistake
    is load-bearing, not cosmetic: a client written against the wrong
    nesting classifies every refusal as unparseable -- which is
    fail-OPEN.
    """
    return httpx.Response(
        503,
        json={
            "decision": "block",
            "decision_source": "gateway",
            "category": "infra",
            "user_message": "The gate could not read this workflow's state ...",
            "details": {"error_code": "WORKFLOW_INACTIVE_LOOKUP_FAILED"},
            "explanation": (
                "The authorization check could not read this workflow's "
                "state, so it failed closed. The workflow was not stopped. "
                "Wait briefly and try again."
            ),
            "explanations": [],
            "policy_id": None,
            "policy_version": 0,
            "projected_cost_cents": None,
            "remaining_budget_cents": None,
            "reservation_id": None,
            "staleness_ms": None,
        },
    )


def _breaker_trip_403() -> httpx.Response:
    return httpx.Response(
        403,
        json={
            "decision": "block",
            "decision_source": "gateway",
            "category": "halt",
            "details": {"error_code": "CIRCUIT_BREAKER_TRIPPED"},
            "explanation": (
                "This workflow was stopped by its circuit breaker and will "
                "not run again on its own. Do not retry this call and do not "
                "try a different tool or approach."
            ),
            "explanations": [],
            "policy_id": None,
            "policy_version": 0,
            "projected_cost_cents": None,
            "remaining_budget_cents": None,
            "reservation_id": None,
            "staleness_ms": None,
        },
    )


class TestInfraRefusalIsNotFailClosed:
    """A 503 the GATE answered is not an outage.

    ADR-064. The distinction is whether an answer exists: a refusal
    body means the gate made a decision and it stands; a body-less 5xx
    means it never got to one, and ADR-008's fail-OPEN applies.
    """

    def test_503_state_read_failure_blocks(self, make_runtime, mock_api):
        """A failed state read is the gate's own answer — honour it.

        Before ADR-064 this returned without raising, so a fail-CLOSED
        503 from the backend was converted into "allowed" by the
        status code alone. The gate blocks in both 503 groups
        (fail-CLOSED, CLAUDE.md §4); the SDK now stops too.
        """
        respx.post(GATE_URL).mock(return_value=_infra_503())
        rt = make_runtime()

        with pytest.raises(NullRunError) as exc_info:
            rt.check_workflow_budget()
        assert not isinstance(exc_info.value, NullRunUnclassifiedRefusalError), (
            "the body carries category=infra, so it is classifiable — an "
            "unclassified-refusal here would mean the category was not read"
        )

    def test_503_outage_with_no_refusal_still_fails_open(self, make_runtime, mock_api):
        """The counter-test, and the reason the rule is narrow.

        A 5xx that is NOT a gate refusal — no `decision` field, the
        shape a proxy or an unreachable gateway produces — must still
        fail open. Widening "honour the answer" to "raise on any
        5xx" would freeze every agent on every deploy.
        """
        respx.post(GATE_URL).mock(
            return_value=httpx.Response(502, text="<html>Bad Gateway</html>")
        )
        rt = make_runtime()
        assert rt.check_workflow_budget() is None

    def test_503_is_retried_before_the_decision(self, make_runtime, mock_api):
        """The 503 is not answered on the first try.

        `_retry_with_backoff(retry_on_5xx=True, max_retries=3)` means a
        transient 503 gets three more attempts, which is what makes
        fail-OPEN tolerable for a rolling deploy. Pinning the attempt
        count stops a future "reduce retries on 5xx" change from
        quietly making gate calls flakier under load — and stops a
        "skip the retry, block immediately" change from turning a blip
        into a stopped agent.
        """
        calls: list[httpx.Request] = []

        def _count(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return _infra_503()

        respx.post(GATE_URL).mock(side_effect=_count)
        rt = make_runtime()
        with pytest.raises(NullRunError):
            rt.check_workflow_budget()

        assert len(calls) > 1, (
            f"a 503 must be retried before the block stands — only "
            f"{len(calls)} attempt(s) were made"
        )

    def test_403_breaker_trip_stops_the_agent(self, make_runtime, mock_api):
        """The inverse case, and the one the whole fix is for.

        A tripped breaker is a real gateway decision, so the runtime
        honours it and raises. Before the registry change this shipped
        500, which is also `>= 500` — so it took the same retry-then-
        fail-OPEN path and the agent was told to retry a workflow an
        operator had just stopped.

        DEF-TC4-001 (2026-10-02) changed the expected CLASS, not the
        behaviour. The pre-flight used to raise `NullRunBudgetError`
        for every refusal, so this test passed on the budget type by
        accident. `CIRCUIT_BREAKER_TRIPPED` is category `halt`, is
        absent from the SDK catalog, and correctly resolves to the
        base `NullRunBlockedException` with the wire code preserved on
        `error_code` — a breaker trip is not budget exhaustion, and an
        operator reading NR-B004 would go look at spend caps.
        """
        respx.post(GATE_URL).mock(return_value=_breaker_trip_403())
        rt = make_runtime()

        with pytest.raises(NullRunBlockedException) as exc_info:
            rt.check_workflow_budget()

        assert not isinstance(exc_info.value, NullRunBudgetError), (
            "a circuit-breaker trip must not surface as budget "
            "exhaustion — it is a `halt`, and the two send an operator "
            "to completely different screens"
        )
        assert exc_info.value.error_code == "CIRCUIT_BREAKER_TRIPPED"
        assert "stopped by its circuit breaker" in exc_info.value.reason
        assert "Do not retry" in exc_info.value.reason, (
            "the agent-facing instruction must survive the round trip — "
            "it is the only channel a model sees, since the backend "
            "ships no agent_message for a `halt` category "
            "(ADR-063 §1.3e)"
        )

    def test_403_is_not_retried(self, make_runtime, mock_api):
        """A 4xx is final. One attempt, no backoff.

        This is the property that distinguishes the fixed trip from
        the 500 it replaced: the old shape made three attempts against
        an already-OPEN breaker.
        """
        calls: list[httpx.Request] = []

        def _count(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return _breaker_trip_403()

        respx.post(GATE_URL).mock(side_effect=_count)
        rt = make_runtime()
        with pytest.raises(NullRunBlockedException):
            rt.check_workflow_budget()

        assert len(calls) == 1, (
            f"a 403 must be answered once; {len(calls)} attempts were made"
        )
