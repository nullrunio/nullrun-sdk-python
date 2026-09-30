"""ADR-063 §1.3(f): what the SDK does with an `infra` refusal.

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
  (`retry_on_5xx=True`, `max_retries=3`), then the transport
  synthesises `decision="block"` with `decision_source=FALLBACK`.
  `check_workflow_budget` calls `is_fallback_decision_source(...)` on
  that and **returns without raising** — a fail-OPEN, per ADR-008's
  documented "dead backend must not freeze the agent" rule.

So on the current SDK a tripped breaker stops the agent and a failed
state read does not. Both are correct *for their category*: the
breaker genuinely stopped the workflow, the read failure did not stop
anything. But it means the pause work must NOT rely on a 5xx to halt
an agent — a `WORKFLOW_PAUSED` shipped as 503 would be an allow on
every SDK released to date.

These tests pin that asymmetry so it cannot change silently, in either
direction. If a future SDK makes 5xx fail-CLOSED, the 503 tests go
red and the ADR gets amended; if one accidentally lets a 403 through
as a fallback, the 403 test goes red.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from nullrun.breaker.exceptions import NullRunBudgetError

BASE_URL = "https://api.test.nullrun.io"
GATE_URL = f"{BASE_URL}/api/v1/gate"


def _infra_503() -> httpx.Response:
    """The exact shape the backend ships for a failed state read."""
    return httpx.Response(
        503,
        json={
            "decision": "block",
            "decision_source": "gateway",
            "error_code": "WORKFLOW_INACTIVE_LOOKUP_FAILED",
            "category": "infra",
            "user_message": "The gate could not read this workflow's state ...",
            "explanation": (
                "The authorization check could not read this workflow's "
                "state, so it failed closed. The workflow was not stopped. "
                "Wait briefly and try again."
            ),
            "explanations": [],
            "policy_version": 0,
        },
    )


def _breaker_trip_403() -> httpx.Response:
    return httpx.Response(
        403,
        json={
            "decision": "block",
            "decision_source": "gateway",
            "error_code": "CIRCUIT_BREAKER_TRIPPED",
            "category": "halt",
            "explanation": (
                "This workflow was stopped by its circuit breaker and will "
                "not run again on its own. Do not retry this call and do not "
                "try a different tool or approach."
            ),
            "explanations": [],
            "policy_version": 0,
        },
    )


class TestInfraRefusalIsNotFailClosed:
    """503 must NOT be read as "allowed" — but it currently is.

    The test asserts the *actual* behaviour, fail-OPEN included, and
    says so in the failure message. Asserting the desired behaviour
    here would ship a red test; asserting "it raises" would be a lie.
    The point is that the asymmetry is now a pinned, visible fact
    rather than something discovered during an incident.
    """

    def test_503_state_read_failure_fails_open_today(self, make_runtime, mock_api):
        """A failed state read currently lets the call through.

        Pinned deliberately. ADR-008's fail-OPEN on transport error is
        the documented policy and is not being changed here, but the
        consequence — `infra` refusals do not stop an agent — must be
        a known quantity before the pause work builds on 5xx.
        """
        respx.post(GATE_URL).mock(return_value=_infra_503())
        rt = make_runtime()

        # Must NOT raise. If a future release makes this fail-CLOSED
        # this test goes red, and ADR-008 + ADR-063 §1.3(f) get
        # amended in the same commit.
        rt.check_workflow_budget()  # noqa: B018 - the absence of a raise IS the assertion

    def test_503_is_retried_before_failing_open(self, make_runtime, mock_api):
        """The 503 is not a first-try fail-open.

        `_retry_with_backoff(retry_on_5xx=True, max_retries=3)` means a
        transient 503 gets three more attempts, which is what makes the
        fail-OPEN tolerable for a rolling deploy. Pinning the attempt
        count stops a future "reduce retries on 5xx" change from
        quietly making gate calls flakier under load.
        """
        calls: list[httpx.Request] = []

        def _count(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return _infra_503()

        respx.post(GATE_URL).mock(side_effect=_count)
        rt = make_runtime()
        rt.check_workflow_budget()

        assert len(calls) > 1, (
            "a 503 must be retried, not failed open on the first "
            f"response — only {len(calls)} attempt(s) were made"
        )

    def test_403_breaker_trip_stops_the_agent(self, make_runtime, mock_api):
        """The inverse case, and the one the whole fix is for.

        A tripped breaker is a real gateway decision, so the runtime
        honours it and raises. Before the registry change this shipped
        500, which is also `>= 500` — so it took the same retry-then-
        fail-OPEN path and the agent was told to retry a workflow an
        operator had just stopped.
        """
        respx.post(GATE_URL).mock(return_value=_breaker_trip_403())
        rt = make_runtime()

        with pytest.raises(NullRunBudgetError) as exc_info:
            rt.check_workflow_budget()

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
        with pytest.raises(NullRunBudgetError):
            rt.check_workflow_budget()

        assert len(calls) == 1, (
            f"a 403 must be answered once; {len(calls)} attempts were made"
        )
