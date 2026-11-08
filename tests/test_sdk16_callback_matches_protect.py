"""SDK-16 — the callback and the `@protect` boundary must AGREE.

The finding
-----------
The SDK has one authoritative fail-OPEN/CLOSED table
(`nullrun/runtime.py`'s module docstring, ADR-063 §2.4). Its row for
`check_workflow_budget` says the gate fails OPEN for TRANSPORT errors
ONLY and CLOSED for everything else. `runtime.check_workflow_budget`
honours that: it RAISES on a 401, on an unreadable answer, on an
unclassifiable refusal and on a rate-limit backend that is down.

The `@protect` boundary honoured it too — it re-raises every
`NullRunInfrastructureError`.

The LangChain callback did NOT. Its broad arm swallowed exactly the
classes the runtime raises on purpose, so the SAME refusal killed the
call through one path and vanished through the other. On the callback
path the LLM call then ran with no reservation and its cost event was
dropped: the agent got the call for free.

Why this file is separate from `test_langchain_enforcement.py`
---------------------------------------------------------------
That file drives the callback with a DOUBLE — it proves the callback's
own classification. This file drives the REAL runtime against a mocked
backend, and asks the question the ledger row actually asked: does the
same refusal produce the same OUTCOME on both paths?

Both halves matter. A double proves the callback's arms; a real
runtime proves that the classes the callback now re-raises are the
classes the runtime actually produces — that the 401 in the table is
the 401 the wire produces, and that the unclassifiable refusal is the
one `resolve_refusal_category` raises.

The counter-test is not optional: `test_a_transport_failure_still_`
lets_the_agent_through` proves the widening did not turn "the backend
is down" into a refusal.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from nullrun.breaker.categories import NullRunUnclassifiedRefusalError
from nullrun.breaker.exceptions import (
    NullRunAuthError,
    NullRunBackendError,
    NullRunMalformedGateResponseError,
)
from nullrun.decorators import protect
from nullrun.instrumentation.langgraph import (
    NullRunCallback,
    drain_deferred_enforcement,
)

BASE_URL = "https://api.test.nullrun.io"
GATE_URL = f"{BASE_URL}/api/v1/gate"


def _gate_unauthorized() -> httpx.Response:
    """A 401 from the gate: a CREDENTIAL failure, not a transport one."""
    return httpx.Response(
        401,
        json={"error_code": "UNAUTHORIZED", "message": "invalid api key"},
    )


def _gate_unclassified_refusal() -> httpx.Response:
    """A refusal with NO category.

    ADR-062 §2.2: the gate does not send a bare "no", it says why. A
    refusal whose category the SDK does not trust must RAISE, never be
    guessed.
    """
    return httpx.Response(
        403,
        json={
            "decision": "block",
            "decision_source": "gateway",
            "explanation": "refused",
            "error_code": "NR-UNKNOWN",
        },
    )


def _gate_malformed() -> httpx.Response:
    """A body that is not a verdict: no `decision_source`, no provenance."""
    return httpx.Response(200, json={"decision": "allow"})


# The gate answer, and the class the SDK's OWN table says it produces.
_CASES = {
    "unauthorized": (_gate_unauthorized, NullRunAuthError),
    "unclassified_refusal": (_gate_unclassified_refusal, NullRunUnclassifiedRefusalError),
    "malformed_body": (_gate_malformed, NullRunMalformedGateResponseError),
}


@pytest.fixture
def ran() -> list[str]:
    """Records whether the decorated body executed."""
    return []


@pytest.fixture(autouse=True)
def _no_deferred_residue():
    """Leave no residue for the next test on this thread.

    The callback stashes a refusal for the `@protect` boundary
    (`record_deferred_enforcement`) AND re-raises it. The stash is a
    thread-local, so a test that drives the callback directly leaves
    the refusal behind. The next `@protect` call on the same thread
    would then drain it and refuse a call the stash has nothing to do
    with. That is the designed fallback, but it is the CALLER's job to
    consume it — so this file consumes it.
    """
    yield
    while drain_deferred_enforcement() is not None:
        pass


def _charge(ran: list[str]) -> str:
    @protect
    def charge_card() -> str:
        ran.append("body")
        return "charged"

    return charge_card()


def _mock_gate(builder) -> None:
    respx.post(GATE_URL).mock(return_value=builder())


class TestTheSameRefusalOneOutcome:
    """One refusal, two paths, one outcome."""

    @pytest.mark.parametrize("case", list(_CASES), ids=list(_CASES))
    def test_the_refusal_stops_the_protect_body(self, case, make_runtime, mock_api, ran):
        """The `@protect` half — the boundary that already refused."""
        _mock_gate(_CASES[case][0])
        make_runtime()

        with pytest.raises(_CASES[case][1]):
            _charge(ran)

        assert ran == [], (
            "the decorated body ran on a refused call — the @protect "
            "boundary is the reference behaviour the callback must match"
        )

    @pytest.mark.parametrize("case", list(_CASES), ids=list(_CASES))
    def test_the_refusal_reaches_the_callback_caller(self, case, make_runtime, mock_api):
        """The callback half — the path that used to swallow it.

        Pre-fix, the refusal was logged at debug and the LLM call
        proceeded with no reservation: the cost event was dropped and
        the agent got the call for free.
        """
        _mock_gate(_CASES[case][0])
        rt = make_runtime()
        cb = NullRunCallback(runtime=rt)

        with pytest.raises(_CASES[case][1]):
            cb.on_llm_start({}, ["hi"], run_id="run-sdk16")

        # The refusal is ALSO stashed for the `@protect` boundary, which
        # is the fallback for a host framework that ignores `raise_error`.
        # A refused call must not lose its refusal on the way out.
        stashed = drain_deferred_enforcement()
        assert isinstance(stashed, _CASES[case][1]), (
            "the refusal was raised but not stashed — a host framework "
            "that swallows callback exceptions would lose it"
        )


class TestTheCounterTest:
    """The widening must not freeze the agent when the backend is down."""

    def test_a_transport_failure_still_lets_the_agent_through(
        self, make_runtime, mock_api, monkeypatch
    ):
        """ADR-008: a dead backend must not freeze the agent.

        A transport failure is an infrastructure error TOO — the
        callback's new infrastructure arm would catch it and refuse —
        unless the transport arm comes first. The order is pinned in
        `test_langchain_enforcement.py`; this test pins the BEHAVIOUR
        it exists to protect.
        """
        make_runtime()

        def _boom(*_a, **_k):
            raise NullRunBackendError(
                "the backend is down", endpoint="gate", status_code=503
            )

        monkeypatch.setattr(
            "nullrun.runtime.NullRunRuntime.check_workflow_budget", _boom
        )

        cb = NullRunCallback(runtime=make_runtime())
        cb.on_llm_start({}, ["hi"], run_id="run-sdk16")  # must not raise
