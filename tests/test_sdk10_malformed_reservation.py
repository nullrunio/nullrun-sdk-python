"""SDK-10 — a reservation id the SDK cannot bind is a REFUSAL, not a warning.

The audit's subject
-------------------
``_capture_server_minted_execution_id`` dropped a ``reservation_id``
that is not a UUID with a ``logger.warning`` and returned ``None``.
The audit's own Verify clause: *"a server response with
``reservation_id = "abc"`` ⇒ the SDK must fail closed, not warn and
continue."*

What the audit got wrong about the CONSEQUENCE
---------------------------------------------
The audit said the chain cache loses the server-minted
``execution_id`` and later steps reuse the WRONG reservation.
Neither is what happens. The chain cache was deleted by the SDK-1/2/3
fix (commit 87a4c2b), six hours AFTER the audit document was written,
and the capture's miss branch calls ``clear_server_minted_execution_id()``,
which wipes ALL the capture slots. So a later call finds NOTHING — it
does not find a wrong reservation.

What IS reachable, and it is worse, is this: the drop read "the server
reserved nothing" when the truth was "the server reserved something I
cannot bind". The call then proceeded with NO reservation:

* the ``/track`` event had no ``execution_id`` to carry, so the cost
  event was DROPPED (``dropped_llm_call_no_reservation``) — the spend
  was never booked, and the backend's reserve sat on the period counter
  until its TTL ran out;
* the agent got the call for FREE.

And the shape that reaches it is the LLM-span pre-flight
(``on_llm_start`` → ``check_workflow_budget``), which has no
``/execute`` behind it to recover the verdict.

Two fixes, because one is not enough
------------------------------------
1. The capture REFUSES a present-but-unusable id
   (``NullRunMalformedGateResponseError`` — the class the SDK already
   uses for "something answered, and what it said was not a verdict").
2. That refusal must REACH the caller. The instrumentation's blanket
   arms re-raise the enforcement family and swallow everything else,
   so the malformed-answer class joins ``ENFORCEMENT_EXCEPTIONS``.
   WITHOUT (2) the refusal is raised and then swallowed one layer up,
   and the LLM call proceeds anyway — which is the hole.

The ABSENT id is a different fact and must NOT be refused: a block
reserves nothing, so a body with no ``reservation_id`` is a fact, not
a fault. Every test below says which of the two it is.

The counter-test is not optional. A file full of "the SDK refuses"
assertions passes if the SDK refuses EVERYTHING, including the calls
it should have allowed. The last class is that counter-test.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from nullrun.breaker.exceptions import (
    NullRunMalformedGateResponseError,
)
from nullrun.context import (
    clear_server_minted_execution_id,
    get_server_minted_execution_id,
    set_server_minted_execution_id,
)
from nullrun.observability import metrics
from nullrun.runtime import _capture_server_minted_execution_id

BASE_URL = "https://api.test.nullrun.io"
GATE_URL = f"{BASE_URL}/api/v1/gate"

# A real server-minted id: the backend's ``mint_execution_id`` is
# ``Uuid::now_v7()``, and the ``/gate`` Allow arm threads that id into
# ``GateResponse.reservation_id`` (the Step-9 Lua reserve's id). The
# ``/execute`` arm reuses the SAME id (ADR-073).
SERVER_MINTED_UUID = "0195f0c0-0000-7000-8000-000000000001"


def _gate_allow(**extra) -> httpx.Response:
    """A real ``/gate`` allow.

    ``decision_source``, ``explanation`` and ``policy_version`` are the
    non-``Option`` fields on the backend's ``GateResponse``
    (``gate/internal.rs:637-641``); all three are present on every real
    answer, and omitting any of them is what makes a body stop being a
    verdict.
    """
    body = {
        "decision": "allow",
        "decision_source": "gateway",
        "explanation": "within budget",
        "policy_version": 1,
    }
    body.update(extra)
    return httpx.Response(200, json=body)


@pytest.fixture(autouse=True)
def _clean_capture():
    """The capture writes to contextvars; they outlive a test."""
    clear_server_minted_execution_id()
    yield
    clear_server_minted_execution_id()


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
# The capture itself — the audit's Verify clause
# ---------------------------------------------------------------------------


class TestTheCaptureRefusesAnUnusableReservation:
    def test_a_non_uuid_reservation_id_is_refused(self):
        """The audit's Verify clause, verbatim.

        "a server response with ``reservation_id = "abc"`` ⇒ the SDK
        must fail closed, not warn and continue".
        """
        with pytest.raises(NullRunMalformedGateResponseError) as caught:
            _capture_server_minted_execution_id({"reservation_id": "abc"})

        assert "reservation_id" in str(caught.value)

    def test_a_non_string_reservation_id_is_refused(self):
        with pytest.raises(NullRunMalformedGateResponseError):
            _capture_server_minted_execution_id({"reservation_id": 123456})

    def test_an_empty_reservation_id_is_refused(self):
        """Empty is PRESENT and unusable.

        The old code folded the empty string into the same arm as an
        absent id (``if not raw``), so an empty string was read as
        "the server reserved nothing". It is the opposite: the server
        answered with a reservation id, and that id names nothing.
        """
        with pytest.raises(NullRunMalformedGateResponseError):
            _capture_server_minted_execution_id({"reservation_id": ""})

    def test_the_refusal_is_counted(self):
        """A refusal nobody can see is a wish, not a control."""
        before = getattr(
            metrics.runtime, "gate_malformed_reservation_total", 0
        )
        with pytest.raises(NullRunMalformedGateResponseError):
            _capture_server_minted_execution_id({"reservation_id": "abc"})
        after = getattr(
            metrics.runtime, "gate_malformed_reservation_total", 0
        )
        assert after == before + 1, (
            "the refusal must be counted — it is the only signal an "
            "operator gets that the gate answered with something the "
            "SDK could not bind"
        )


class TestAnAbsentReservationIsNotARefusal:
    """The other half, and the half a block depends on.

    A BLOCK reserves nothing. A body with no ``reservation_id`` says
    "the server held no money for this call" — a fact. Refusing it
    would turn every block into a protocol error and hide the block.
    """

    def test_a_missing_reservation_id_is_not_a_refusal(self):
        assert _capture_server_minted_execution_id({"decision": "block"}) is None

    def test_a_null_reservation_id_is_not_a_refusal(self):
        """The backend sometimes sends ``reservation_id: null``."""
        assert _capture_server_minted_execution_id({"reservation_id": None}) is None

    def test_a_non_dict_response_is_not_a_refusal(self):
        assert _capture_server_minted_execution_id("not a dict") is None  # type: ignore[arg-type]

    def test_an_absent_id_clears_the_previous_one(self):
        """The old value must not leak into the next call."""
        set_server_minted_execution_id(SERVER_MINTED_UUID)
        assert _capture_server_minted_execution_id({"decision": "block"}) is None
        assert get_server_minted_execution_id() is None

    def test_a_real_uuid_is_still_captured(self):
        out = _capture_server_minted_execution_id(
            {"reservation_id": SERVER_MINTED_UUID}
        )
        assert out == SERVER_MINTED_UUID
        assert get_server_minted_execution_id() == SERVER_MINTED_UUID


# ---------------------------------------------------------------------------
# The whole /gate path, over the wire
# ---------------------------------------------------------------------------


class TestTheGatePathRefuses:
    def test_the_gate_answer_with_a_malformed_reservation_id_is_refused(
        self, make_runtime, mock_api
    ):
        """End to end over the wire, with the real runtime."""
        respx.post(GATE_URL).mock(
            return_value=_gate_allow(reservation_id="abc")
        )
        rt = make_runtime()

        with pytest.raises(NullRunMalformedGateResponseError):
            rt.check_workflow_budget()

    def test_a_gate_allow_with_no_reservation_id_is_not_a_refusal(
        self, make_runtime, mock_api
    ):
        """The counter-test for the class above.

        A real backend on a path with no reserve (a soft pass, a
        workflow with no budget policy) answers without a reservation
        id. The SDK must accept that answer, or the gate would refuse
        every unbudgeted call in the fleet.
        """
        respx.post(GATE_URL).mock(return_value=_gate_allow())
        rt = make_runtime()

        assert rt.check_workflow_budget() is None

    def test_a_gate_allow_with_a_real_reservation_id_still_works(
        self, make_runtime, mock_api
    ):
        respx.post(GATE_URL).mock(
            return_value=_gate_allow(reservation_id=SERVER_MINTED_UUID)
        )
        rt = make_runtime()

        assert rt.check_workflow_budget() == SERVER_MINTED_UUID


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


class TestTheLlmCallNeverRuns:
    """The hole, at the level the agent sees it.

    The LLM-span pre-flight (``on_llm_start`` → ``check_workflow_budget``)
    has NO ``/execute`` behind it, so nothing recovers a lost verdict.
    Pre-fix the answer was "warn and continue": the provider call went
    out, and its cost event was dropped — the agent got the call for
    free.
    """

    def test_the_refusal_reaches_the_caller_through_a_real_callback_manager(
        self, make_runtime, mock_api
    ):
        """A source-pin cannot prove the framework PROPAGATES what the
        handler raises — that is langchain-core's contract. So this
        drives the real ``CallbackManager``.
        """
        pytest.importorskip("langchain_core")
        from langchain_core.callbacks.manager import CallbackManager

        from nullrun.instrumentation.langgraph import NullRunCallback

        respx.post(GATE_URL).mock(
            return_value=_gate_allow(reservation_id="abc")
        )
        rt = make_runtime()
        cb = NullRunCallback(runtime=rt)

        with pytest.raises(NullRunMalformedGateResponseError):
            CallbackManager([cb]).on_llm_start({}, ["hi"])

    def test_the_refusal_reaches_the_caller_on_the_async_path(
        self, make_runtime, mock_api
    ):
        """The async dispatch is a DIFFERENT path in langchain-core."""
        pytest.importorskip("langchain_core")
        from langchain_core.callbacks.manager import AsyncCallbackManager

        from nullrun.instrumentation.langgraph import NullRunCallback

        respx.post(GATE_URL).mock(
            return_value=_gate_allow(reservation_id="abc")
        )
        rt = make_runtime()
        cb = NullRunCallback(runtime=rt)

        with pytest.raises(NullRunMalformedGateResponseError):
            asyncio.run(AsyncCallbackManager([cb]).on_llm_start({}, ["hi"]))

    def test_the_refusal_is_recorded_for_the_protect_boundary(
        self, make_runtime, mock_api
    ):
        """The secondary channel stays: a host framework that ignores
        ``raise_error`` still gets the decision at the ``@protect``
        boundary.
        """
        from nullrun.instrumentation.langgraph import (
            NullRunCallback,
            drain_deferred_enforcement,
        )

        respx.post(GATE_URL).mock(
            return_value=_gate_allow(reservation_id="abc")
        )
        rt = make_runtime()
        cb = NullRunCallback(runtime=rt)

        with pytest.raises(NullRunMalformedGateResponseError):
            cb.on_llm_start({}, ["hi"], run_id="run-1")

        deferred = drain_deferred_enforcement()
        assert deferred is not None, (
            "the refusal must also be recorded — it is the only channel "
            "that can abort on the @protect boundary's own thread"
        )
        assert isinstance(deferred, NullRunMalformedGateResponseError)


class TestTheProviderRequestNeverLeaves:
    """The money, on the zero-code path.

    The httpx transport reserves BEFORE the provider request goes out.
    Pre-fix the reservation was dropped, so the request went out
    unbilled. Post-fix the request must not go out at all.
    """

    def test_the_provider_request_never_leaves(
        self, make_runtime, mock_api
    ):
        from nullrun.instrumentation.auto import NullRunSyncTransport

        respx.post(GATE_URL).mock(
            return_value=_gate_allow(reservation_id="abc")
        )
        rt = make_runtime()

        inner = _RecordingTransport(
            body=b'{"usage":{"prompt_tokens":10,"completion_tokens":5}}'
        )
        client = httpx.Client(transport=NullRunSyncTransport(inner, rt))

        with pytest.raises(NullRunMalformedGateResponseError):
            client.post(
                "https://api.openai.com/v1/chat/completions",
                json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
            )

        assert inner.sent == [], (
            "the provider request went out with no reservation: the "
            "spend was never booked, and the agent got the call for free"
        )


class TestTheProtectBoundaryHolds:
    def test_the_tool_body_does_not_run(self, make_runtime, mock_api):
        """``@protect`` is what users call, so it is the level that counts."""
        from nullrun.decorators import protect

        respx.post(GATE_URL).mock(
            return_value=_gate_allow(reservation_id="abc")
        )
        make_runtime()
        ran: list[str] = []

        @protect
        def charge() -> str:
            ran.append("body")
            return "charged"

        with pytest.raises(NullRunMalformedGateResponseError):
            charge()

        assert ran == [], "the decorated body ran on an unbindable answer"


# ---------------------------------------------------------------------------
# The fail-OPEN policy is untouched
# ---------------------------------------------------------------------------


class TestATransportFailureStillFailsOpen:
    """ADR-008: a dead backend must not freeze the agent.

    The refusal above is an ANSWER the SDK cannot use. A dead backend
    is the opposite case — no answer at all — and it keeps the
    documented fail-OPEN posture. Conflating the two would take every
    agent down during an outage.
    """

    def test_the_llm_span_path_still_proceeds_when_the_gate_is_unreachable(
        self, make_runtime, mock_api
    ):
        pytest.importorskip("langchain_core")
        from langchain_core.callbacks.manager import CallbackManager

        from nullrun.instrumentation.langgraph import NullRunCallback

        respx.post(GATE_URL).mock(return_value=httpx.Response(503, text="down"))
        rt = make_runtime()
        cb = NullRunCallback(runtime=rt)

        CallbackManager([cb]).on_llm_start({}, ["hi"])  # must not raise

    def test_the_provider_request_still_leaves_when_the_gate_is_unreachable(
        self, make_runtime, mock_api
    ):
        from nullrun.instrumentation.auto import NullRunSyncTransport

        respx.post(GATE_URL).mock(return_value=httpx.Response(503, text="down"))
        rt = make_runtime()

        inner = _RecordingTransport(
            body=b'{"usage":{"prompt_tokens":10,"completion_tokens":5}}'
        )
        client = httpx.Client(transport=NullRunSyncTransport(inner, rt))

        response = client.post(
            "https://api.openai.com/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert response.status_code == 200
        assert len(inner.sent) == 1, (
            "a dead backend must not stop the call — ADR-008's fail-OPEN "
            "posture is the documented policy for an unreachable gate"
        )


class TestTheSiblingHoleIsClosed:
    """The same family membership closes a second hole.

    A body that is not JSON at all (a proxy error page, a captive
    portal, a TLS-interception box) ALREADY raised
    ``NullRunMalformedGateResponseError`` in the runtime. But on the
    LLM-span path the instrumentation's blanket arm swallowed it, so
    the LLM call proceeded on a non-NULLRUN answer — the same free call,
    the same hole, one class away.
    """

    def test_a_non_json_gate_body_is_refused_on_the_llm_span_path(
        self, make_runtime, mock_api
    ):
        pytest.importorskip("langchain_core")
        from langchain_core.callbacks.manager import CallbackManager

        from nullrun.instrumentation.langgraph import NullRunCallback

        respx.post(GATE_URL).mock(
            return_value=httpx.Response(200, text="<html>captive portal</html>")
        )
        rt = make_runtime()
        cb = NullRunCallback(runtime=rt)

        with pytest.raises(NullRunMalformedGateResponseError):
            CallbackManager([cb]).on_llm_start({}, ["hi"])


# ---------------------------------------------------------------------------
# The counter-test: the SDK must not refuse what it can use
# ---------------------------------------------------------------------------


class TestAGoodAnswerStillWorks:
    def test_a_real_allow_still_captures_its_reservation(
        self, make_runtime, mock_api
    ):
        respx.post(GATE_URL).mock(
            return_value=_gate_allow(reservation_id=SERVER_MINTED_UUID)
        )
        rt = make_runtime()

        assert rt.check_workflow_budget() == SERVER_MINTED_UUID
        assert get_server_minted_execution_id() == SERVER_MINTED_UUID

    def test_a_real_allow_still_runs_the_provider_request(
        self, make_runtime, mock_api
    ):
        from nullrun.instrumentation.auto import NullRunSyncTransport

        respx.post(GATE_URL).mock(
            return_value=_gate_allow(reservation_id=SERVER_MINTED_UUID)
        )
        rt = make_runtime()

        inner = _RecordingTransport(
            body=b'{"usage":{"prompt_tokens":10,"completion_tokens":5}}'
        )
        client = httpx.Client(transport=NullRunSyncTransport(inner, rt))

        response = client.post(
            "https://api.openai.com/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert response.status_code == 200
        assert len(inner.sent) == 1

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


class TestTheFamilyHoldsTheClass:
    """The refusal class must be in the enforcement family.

    Without the membership, the instrumentation's blanket arm downgrades
    it to a fail-OPEN transport error — the refusal is raised and then
    swallowed one layer up.
    """

    def test_the_malformed_answer_class_is_an_enforcement_exception(self):
        from nullrun.breaker.exceptions import ENFORCEMENT_EXCEPTIONS

        assert issubclass(
            NullRunMalformedGateResponseError, ENFORCEMENT_EXCEPTIONS
        ), (
            "the class must be caught by the enforcement arm BEFORE the "
            "broad transport arm, or the refusal is downgraded to a "
            "fail-OPEN transport error and the call proceeds unbilled"
        )

    def test_a_transport_failure_is_still_not_in_the_family(self):
        """ADR-008: the gate could not be reached → fail OPEN."""
        from nullrun.breaker.exceptions import (
            ENFORCEMENT_EXCEPTIONS,
            NullRunBackendError,
        )

        assert not isinstance(
            NullRunBackendError("boom", endpoint="gate", status_code=500),
            ENFORCEMENT_EXCEPTIONS,
        )
