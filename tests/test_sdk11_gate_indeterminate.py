"""SDK-11 — one implementation, one handler: an indeterminate gate
decision is a REFUSAL, not a permission.

The audit's subject
-------------------
``RELIABILITY_AUDIT_2026-11-02_EVIDENCE.md`` §SDK-11, verbatim:

    *Location:* ``src/nullrun/runtime.py`` ≈L2664–2672 (the chain arm
    catches narrowly) vs ≈L2713–2717 (the single-shot arm catches
    ``Exception`` and **fails open**). *Mechanism:* the same check is
    implemented twice with different handlers. *Failure:* a single-shot
    call gets a fail-open, a chain-call gets a fail-closed — and the
    fail-open path is the simpler one, so it is the one users hit
    first. *Verify:* an exception inside the gate ⇒ the single-shot
    path must refuse. *Fix:* one implementation, one handler.

What the audit's line numbers actually point at
-----------------------------------------------
They match the tree at ``d71bade`` EXACTLY — and the two arms are NOT
"chain" and "single-shot". They are the **cached** branch's narrow arm
(``except (httpx.HTTPError, NullRunError)``) and the **uncached**
branch's broad arm (``except Exception``). The audit read the two
halves of ONE handler as two implementations.

The state at HEAD is sharper than the audit's
---------------------------------------------
SDK-1 (``87a4c2b``) deleted the cache, and with it the narrow twin.
So at HEAD there is ONE implementation and ONE handler — and it is the
BROAD one. The audit's fix ("one implementation, one handler") is
half-done: the duplication is gone, the broad catch-all remains, and
every fault inside the gate call is laundered into "allowed".

Why "allowed" is the wrong answer
----------------------------------
ADR-008's fail-OPEN covers ONE thing: the gate could not be REACHED,
so there is no answer to honour and a dead backend must not freeze the
agent. Everything else is a different fact:

* the gate NAMED a refusal → honour it (block);
* the gate answered with something unusable → refuse (SDK-10);
* the SDK's own code faulted while asking → the decision is
  INDETERMINATE, and an unanswered question is not a yes.

The broad arm could not tell the third apart from the first. It
logged, counted ``gate_fail_open_total``, and returned ``None`` —
which every caller reads as "no decision, carry on". The call then ran
with NO reservation: the backend booked nothing, the cost event was
dropped, and the agent got the call for free. The same shape as the
SDK-10 hole, one class away.

What this file proves
---------------------
1. A non-transport fault inside the gate REFUSES, and the refusal is
   typed (``NullRunGateIndeterminateError``) and counted.
2. The refusal REACHES the caller through the LangChain callback —
   otherwise it is raised and then swallowed one layer up, and the LLM
   call proceeds anyway (the SDK-10 hole, re-opened).
3. The provider request never leaves, and the ``@protect`` body never
   runs — the money.
4. A genuine transport failure STILL fails OPEN (ADR-008 preserved),
   and a refusal the gate NAMED is STILL honoured.

The last class is the counter-test. A file full of "the SDK refuses"
assertions passes if the SDK refuses everything, including the calls
it should have allowed.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from nullrun.breaker.exceptions import (
    ENFORCEMENT_EXCEPTIONS,
    NullRunBackendError,
    NullRunGateIndeterminateError,
    NullRunTransportError,
    RateLimitError,
)
from nullrun.observability import metrics
from nullrun.transport import TransportErrorSource

BASE_URL = "https://api.test.nullrun.io"
GATE_URL = f"{BASE_URL}/api/v1/gate"

# A real server-minted id: the backend's ``mint_execution_id`` is
# ``Uuid::now_v7()``, and the ``/gate`` Allow arm threads that id into
# ``GateResponse.reservation_id`` (the Step-9 Lua reserve's id).
SERVER_MINTED_UUID = "0195f0c0-0000-7000-8000-000000000001"


def _gate_allow(**extra) -> httpx.Response:
    """A real ``/gate`` allow.

    ``decision_source``, ``explanation`` and ``policy_version`` are the
    non-``Option`` fields on the backend's ``GateResponse``; all three
    are present on every real answer, and omitting any of them is what
    makes a body stop being a verdict.
    """
    body = {
        "decision": "allow",
        "decision_source": "gateway",
        "explanation": "within budget",
        "policy_version": 1,
    }
    body.update(extra)
    return httpx.Response(200, json=body)


def _break_the_gate(rt, exc: BaseException) -> None:
    """Make the transport's ``check`` raise, as an SDK bug would.

    The seam is deliberate: ``Transport.check`` is the SDK's own code,
    and the handler under test is exactly the ``try`` that wraps it.
    A fault injected here is what an SDK-internal bug looks like to
    the runtime — and it is NOT reachable from the wire today, which
    is the point: this class of failure only ever arrives as a bug,
    and the guard exists for the day one does.
    """
    def _boom(_check_request):
        raise exc

    rt._transport.check = _boom  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _no_global_auto_instrumentation(monkeypatch):
    """This file must not install the SDK's process-global patches.

    ``@protect`` lazily calls ``auto_instrument(runtime)`` the first time
    it resolves a runtime. ``auto_instrument`` patches
    ``langchain_core.callbacks.BaseCallbackManager.__init__`` for the
    WHOLE PROCESS, and the patch captures the runtime it was given.
    Every ``CallbackManager`` built after that — including ones built
    by unrelated test files — gets a ``NullRunCallback`` bound to the
    captured runtime.

    This file breaks a runtime on purpose (its ``check`` raises), so
    if it were the file that installed the patch, the captured runtime
    would be the broken one and the NEXT test file to build a
    ``CallbackManager`` would explode with this file's injected
    ``RuntimeError`` instead of its own. That is a real
    order-dependence: the suite's own
    ``test_langchain_swallows_callback_exceptions`` fails when this
    file runs first.

    Pinning the flag makes this file a bystander: it neither installs
    the patch nor tears down one an earlier file installed. The flag is
    restored by ``monkeypatch`` afterwards, so the process ends the file
    exactly as it started it.

    Nothing in this file needs auto-instrumentation: the transport
    under test is built explicitly, and the enforcement path is
    exercised directly.
    """
    import nullrun.decorators as _dec

    monkeypatch.setattr(_dec, "_auto_instrument_triggered", True, raising=False)


@pytest.fixture(autouse=True)
def _clear_deferred():
    """The deferred-enforcement stash is thread-local and outlives a test.

    It is the SDK-4 secondary channel: a refusal that a framework
    callback could not abort is stashed, and the ``@protect`` boundary
    drains it. A stash left behind by one test is drained by the NEXT
    test's ``@protect`` — which would make an unrelated test fail with
    a refusal it never caused.
    """
    from nullrun.instrumentation.langgraph import drain_deferred_enforcement

    drain_deferred_enforcement()
    yield
    drain_deferred_enforcement()


# ---------------------------------------------------------------------------
# The refusal
# ---------------------------------------------------------------------------


class TestAnIndeterminateDecisionRefuses:
    """The audit's Verify clause: an exception inside the gate ⇒ refuse."""

    def test_a_non_transport_fault_refuses(self, make_runtime, mock_api):
        rt = make_runtime()
        _break_the_gate(rt, RuntimeError("the SDK faulted"))

        with pytest.raises(NullRunGateIndeterminateError):
            rt.check_workflow_budget()

    def test_it_is_not_a_silent_none(self, make_runtime, mock_api):
        """The pre-fix answer was ``None`` — "no decision, carry on".

        A caller that ignores exceptions would see an allowed call.
        The refusal must be an exception, not a value.
        """
        rt = make_runtime()
        _break_the_gate(rt, RuntimeError("the SDK faulted"))

        with pytest.raises(NullRunGateIndeterminateError):
            rt.check_workflow_budget()

    def test_the_refusal_carries_the_cause(self, make_runtime, mock_api):
        """The message says the decision was indeterminate; the cause
        says WHY. Without it the operator has a refusal and no lead."""
        boom = RuntimeError("the SDK faulted")
        rt = make_runtime()
        _break_the_gate(rt, boom)

        with pytest.raises(NullRunGateIndeterminateError) as caught:
            rt.check_workflow_budget()

        assert caught.value.cause is boom
        assert caught.value.__cause__ is boom

    def test_the_refusal_is_counted(self, make_runtime, mock_api):
        """A refusal nobody can see is a wish, not a control."""
        before = getattr(metrics.runtime, "gate_indeterminate_total", 0)
        rt = make_runtime()
        _break_the_gate(rt, RuntimeError("the SDK faulted"))

        with pytest.raises(NullRunGateIndeterminateError):
            rt.check_workflow_budget()

        after = getattr(metrics.runtime, "gate_indeterminate_total", 0)
        assert after == before + 1, (
            "the refusal must be counted — it is the only signal an "
            "operator gets that the SDK is refusing calls"
        )

    def test_it_is_not_counted_as_a_fail_open(self, make_runtime, mock_api):
        """Two different facts, two different counters.

        ``gate_fail_open_total`` is the ADR-008 posture: enforcement
        was ADVISORY and the call ran. An indeterminate decision does
        the opposite — the call is stopped. Counting it as a fail-open
        would send an operator to the backend dashboard when the fault
        is in the SDK.
        """
        before = getattr(metrics.runtime, "gate_fail_open_total", 0)
        rt = make_runtime()
        _break_the_gate(rt, RuntimeError("the SDK faulted"))

        with pytest.raises(NullRunGateIndeterminateError):
            rt.check_workflow_budget()

        after = getattr(metrics.runtime, "gate_fail_open_total", 0)
        assert after == before, (
            "an indeterminate decision is a refusal, not a fail-OPEN — "
            "the two counters answer different operator questions"
        )


# ---------------------------------------------------------------------------
# The money — the LLM-span path
# ---------------------------------------------------------------------------


class _RecordingTransport(httpx.BaseTransport):
    """An inner transport that records the requests it was asked to send."""

    def __init__(self, body: bytes = b"{}", status: int = 200) -> None:
        self.body = body
        self.status = status
        self.sent: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        return httpx.Response(
            self.status,
            content=self.body,
            headers={"content-type": "application/json"},
        )


class TestTheProviderRequestNeverLeaves:
    """The hole, at the level the agent sees it.

    The LLM-span pre-flight has NO ``/execute`` behind it, so nothing
    recovers a lost verdict. Pre-fix the answer was "warn and
    continue": the provider call went out, and its cost event was
    dropped — the agent got the call for free.
    """

    def test_the_provider_request_never_leaves(self, make_runtime, mock_api):
        from nullrun.instrumentation.auto import NullRunSyncTransport

        rt = make_runtime()
        _break_the_gate(rt, RuntimeError("the SDK faulted"))

        inner = _RecordingTransport(
            body=b'{"usage":{"prompt_tokens":10,"completion_tokens":5}}'
        )
        client = httpx.Client(transport=NullRunSyncTransport(inner, rt))

        with pytest.raises(NullRunGateIndeterminateError):
            client.post(
                "https://api.openai.com/v1/chat/completions",
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )

        assert inner.sent == [], (
            "the provider request went out with no reservation: the "
            "spend was never booked, and the agent got the call for free"
        )


class TestTheProtectBoundaryHolds:
    def test_the_tool_body_does_not_run(self, make_runtime, mock_api):
        """``@protect`` is what users call, so it is the level that counts."""
        from nullrun.decorators import protect

        rt = make_runtime()
        _break_the_gate(rt, RuntimeError("the SDK faulted"))
        ran: list[str] = []

        @protect
        def charge() -> str:
            ran.append("body")
            return "charged"

        with pytest.raises(NullRunGateIndeterminateError):
            charge()

        assert ran == [], "the decorated body ran on an indeterminate decision"


# ---------------------------------------------------------------------------
# The callback must not swallow it
# ---------------------------------------------------------------------------


class TestTheRefusalReachesTheCaller:
    """Without this, the refusal is raised and then swallowed one layer
    up — and the LLM call proceeds anyway.

    ``instrumentation/langgraph.py`` re-raises only the enforcement
    family and swallows everything else at debug level. A refusal class
    that is not in the family is a refusal nobody sees.
    """

    def test_it_escapes_a_real_callback_manager(self, make_runtime, mock_api):
        pytest.importorskip("langchain_core")
        from langchain_core.callbacks.manager import CallbackManager

        from nullrun.instrumentation.langgraph import NullRunCallback

        rt = make_runtime()
        _break_the_gate(rt, RuntimeError("the SDK faulted"))
        cb = NullRunCallback(runtime=rt)

        with pytest.raises(NullRunGateIndeterminateError):
            CallbackManager([cb]).on_llm_start({}, ["hi"])

    def test_it_escapes_the_async_dispatch(self, make_runtime, mock_api):
        """The async dispatch is a DIFFERENT path in langchain-core."""
        pytest.importorskip("langchain_core")
        from langchain_core.callbacks.manager import AsyncCallbackManager

        from nullrun.instrumentation.langgraph import NullRunCallback

        rt = make_runtime()
        _break_the_gate(rt, RuntimeError("the SDK faulted"))
        cb = NullRunCallback(runtime=rt)

        with pytest.raises(NullRunGateIndeterminateError):
            asyncio.run(AsyncCallbackManager([cb]).on_llm_start({}, ["hi"]))

    def test_it_is_recorded_for_the_protect_boundary(self, make_runtime, mock_api):
        """The secondary channel stays: a host framework that ignores
        ``raise_error`` still gets the decision at the ``@protect``
        boundary.
        """
        from nullrun.instrumentation.langgraph import (
            NullRunCallback,
            drain_deferred_enforcement,
        )

        rt = make_runtime()
        _break_the_gate(rt, RuntimeError("the SDK faulted"))
        cb = NullRunCallback(runtime=rt)

        with pytest.raises(NullRunGateIndeterminateError):
            cb.on_llm_start({}, ["hi"], run_id="run-1")

        deferred = drain_deferred_enforcement()
        assert deferred is not None, (
            "the refusal must also be recorded — it is the only channel "
            "that can abort on the @protect boundary's own thread"
        )
        assert isinstance(deferred, NullRunGateIndeterminateError)


# ---------------------------------------------------------------------------
# ADR-008 is preserved
# ---------------------------------------------------------------------------


class TestATransportFailureStillFailsOpen:
    """A dead backend must not freeze the agent.

    The refusal above is a fault in the SDK's own code. A dead backend
    is the opposite case — no answer at all — and it keeps the
    documented fail-OPEN posture. Conflating the two would take every
    agent down during an outage.
    """

    def test_a_network_failure_still_fails_open(self, make_runtime, mock_api):
        rt = make_runtime()
        _break_the_gate(rt, httpx.ConnectError("connection refused"))

        assert rt.check_workflow_budget() is None

    def test_a_transport_error_still_fails_open(self, make_runtime, mock_api):
        rt = make_runtime()
        _break_the_gate(
            rt,
            NullRunTransportError(
                "gateway down",
                source=TransportErrorSource.NETWORK_ERROR,
                endpoint="check",
            ),
        )

        assert rt.check_workflow_budget() is None

    def test_a_gateway_5xx_still_fails_open(self, make_runtime, mock_api):
        rt = make_runtime()
        _break_the_gate(
            rt,
            NullRunBackendError("boom", endpoint="check", status_code=502),
        )

        assert rt.check_workflow_budget() is None

    def test_the_llm_span_path_still_proceeds_when_the_gate_is_unreachable(
        self, make_runtime, mock_api
    ):
        pytest.importorskip("langchain_core")
        from langchain_core.callbacks.manager import CallbackManager

        from nullrun.instrumentation.langgraph import NullRunCallback

        rt = make_runtime()
        _break_the_gate(rt, httpx.ConnectError("connection refused"))
        cb = NullRunCallback(runtime=rt)

        CallbackManager([cb]).on_llm_start({}, ["hi"])  # must not raise

    def test_the_provider_request_still_leaves_when_the_gate_is_unreachable(
        self, make_runtime, mock_api
    ):
        from nullrun.instrumentation.auto import NullRunSyncTransport

        rt = make_runtime()
        _break_the_gate(rt, httpx.ConnectError("connection refused"))

        inner = _RecordingTransport(
            body=b'{"usage":{"prompt_tokens":10,"completion_tokens":5}}'
        )
        client = httpx.Client(transport=NullRunSyncTransport(inner, rt))

        response = client.post(
            "https://api.openai.com/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert response.status_code == 200
        assert len(inner.sent) == 1, (
            "a dead backend must not stop the call — ADR-008's fail-OPEN "
            "posture is the documented policy for an unreachable gate"
        )


# ---------------------------------------------------------------------------
# A refusal the gate NAMED is still honoured
# ---------------------------------------------------------------------------


class TestANamedRefusalIsNotDowngraded:
    """The family arm comes FIRST, and the ordering is load-bearing.

    ``RateLimitError`` is a ``NullRunTransportError``. If the transport
    arm came first, a 429 would be swallowed by it and fail OPEN — the
    exact hole ADR-062 and SDK-4 closed.
    """

    def test_a_rate_limit_refusal_is_not_downgraded_to_a_fail_open(
        self, make_runtime, mock_api
    ):
        rt = make_runtime()
        _break_the_gate(
            rt,
            RateLimitError(
                "too many calls",
                source=TransportErrorSource.NETWORK_ERROR,
                endpoint="check",
            ),
        )

        with pytest.raises(RateLimitError):
            rt.check_workflow_budget()

    def test_a_budget_refusal_is_not_downgraded_to_a_fail_open(
        self, make_runtime, mock_api
    ):
        from nullrun.breaker.exceptions import NullRunBudgetError

        rt = make_runtime()
        _break_the_gate(
            rt,
            NullRunBudgetError(workflow_id="w1", reason="budget exhausted"),
        )

        with pytest.raises(NullRunBudgetError):
            rt.check_workflow_budget()


# ---------------------------------------------------------------------------
# The counter-test: the SDK must not refuse what it can use
# ---------------------------------------------------------------------------


class TestAGoodAnswerStillWorks:
    def test_a_real_allow_still_works(self, make_runtime, mock_api):
        respx.post(GATE_URL).mock(
            return_value=_gate_allow(reservation_id=SERVER_MINTED_UUID)
        )
        rt = make_runtime()

        assert rt.check_workflow_budget() == SERVER_MINTED_UUID

    def test_a_real_allow_still_runs_the_tool_body(self, make_runtime, mock_api):
        from nullrun.decorators import protect

        respx.post(GATE_URL).mock(
            return_value=_gate_allow(reservation_id=SERVER_MINTED_UUID)
        )
        make_runtime()
        ran: list[str] = []

        @protect
        def charge() -> str:
            ran.append("body")
            return "charged"

        assert charge() == "charged"
        assert ran == ["body"], (
            "the SDK refused a call the gate allowed — a suite of "
            "refusal assertions passes if the SDK refuses everything"
        )


# ---------------------------------------------------------------------------
# The family holds the class
# ---------------------------------------------------------------------------


class TestTheFamilyHoldsTheClass:
    """Without the membership, the instrumentation's blanket arm
    downgrades the refusal to a swallowed debug line — the refusal is
    raised and then thrown away one layer up.
    """

    def test_the_indeterminate_class_is_an_enforcement_exception(self):
        assert issubclass(NullRunGateIndeterminateError, ENFORCEMENT_EXCEPTIONS), (
            "the class must be caught by the enforcement arm BEFORE the "
            "transport arm, or the refusal is downgraded to a fail-OPEN "
            "transport error and the call proceeds unbilled"
        )

    def test_it_is_not_a_transport_error(self):
        """ADR-126's two-step opt-in opens TRANSPORT errors.

        An indeterminate decision must not be convertible into "the
        body runs" by flipping a switch. Deriving from
        ``NullRunTransportError`` would hand it to the ``@protect``
        boundary's transport arm, which that switch can open.
        """
        assert not issubclass(NullRunGateIndeterminateError, NullRunTransportError), (
            "the indeterminate refusal must not be a transport error — "
            "that is the one class ADR-126's opt-in can open"
        )
