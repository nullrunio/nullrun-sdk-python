"""The enforcement properties, proven through `@protect`.

Every other file in this cluster tests the refusal handling by calling
`runtime.check_workflow_budget()` directly. That is the wrong level for
the question users actually care about, because `@protect` is what they
call — and `@protect` is a separate code path with its own `try`,
its own `except BaseException`, and its own two wrappers (sync and
async).

Reading `decorators.py` says the refusals propagate: the
`except BaseException` arm only re-wraps kill/pause and then re-raises.
But "reading the code says so" is exactly the reasoning that let
DEF-MP-TS12-ENF-01 ship — `check_workflow_budget`'s `except Exception`
looked harmless in isolation too, until it was reached by a real 401.

So this file runs the same properties through the decorator and asserts
the only thing that matters to a caller: **did the function body run?**

The counter-test is not optional. Every assertion here is "the body did
not run", and a suite of only those would pass if `@protect` refused
everything, including calls it should have allowed. `test_a_real_allow
_still_runs_the_body` is what stops that reading.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from nullrun.breaker.categories import NullRunUnclassifiedRefusalError
from nullrun.breaker.exceptions import (
    NullRunBlockedException,
    NullRunBudgetError,
    NullRunDeniedError,
    NullRunError,
    NullRunMalformedGateResponseError,
)
from nullrun.decorators import protect

BASE_URL = "https://api.test.nullrun.io"
GATE_URL = f"{BASE_URL}/api/v1/gate"
EXECUTE_URL = f"{BASE_URL}/api/v1/execute"


def _gate_allow(**extra) -> httpx.Response:
    """A real `/gate` allow, per the live-captured envelope.

    `decision_source`, `explanation` and `policy_version` are the
    non-`Option` fields on the backend's `GateResponse`
    (`gate/internal.rs:637-641`); all three are present on every real
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


def _gate_refusal(code: str, category: str | None, *, status: int = 403) -> httpx.Response:
    body = {
        "decision": "block",
        "decision_source": "gateway",
        "explanation": f"refused: {code}",
        "error_code": code,
    }
    if category is not None:
        body["category"] = category
        if category == "denied":
            body["agent_message"] = "The operator has not allowed this tool."
    return httpx.Response(status, json=body)


@pytest.fixture
def ran() -> list[str]:
    """Records whether the decorated body executed.

    A list rather than a bool so a test failure can say how many times
    it ran, which distinguishes "ran once then refused" from "ran for
    every attempt".
    """
    return []


def _charge(ran: list[str], amount: int = 100) -> str:
    @protect
    def charge_card(amount: int) -> str:
        ran.append("body")
        return f"charged:{amount}"

    return charge_card(amount)


class TestProtectHonoursRefusals:
    """A refusal through the decorator stops the call."""

    def test_denied_refusal_does_not_run_the_body(self, make_runtime, mock_api, ran):
        respx.post(GATE_URL).mock(
            return_value=_gate_refusal("TOOL_BLOCKED", "denied", status=403)
        )
        make_runtime()
        with pytest.raises(NullRunError):
            _charge(ran)
        assert ran == [], "the decorated body ran on a refused call"

    def test_budget_refusal_does_not_run_the_body(self, make_runtime, mock_api, ran):
        respx.post(GATE_URL).mock(
            return_value=_gate_refusal("BUDGET_HARD_BLOCKED", "budget", status=402)
        )
        make_runtime()
        with pytest.raises(NullRunBudgetError):
            _charge(ran)
        assert ran == [], "the decorated body ran on an exhausted budget"

    def test_forged_allow_without_provenance_does_not_run_the_body(
        self, make_runtime, mock_api, ran
    ):
        """Limitation 4's hole, at the level a user is actually exposed at.

        The body is `{"decision": "allow"}` and nothing else. Reaching
        `@protect` matters here: a caller who never calls
        `check_workflow_budget` directly — the normal case — is
        protected only if the decorator's own `try` does not swallow
        the malformed-response error on its way out.
        """
        respx.post(GATE_URL).mock(return_value=httpx.Response(200, json={"decision": "allow"}))
        make_runtime()
        with pytest.raises(NullRunMalformedGateResponseError):
            _charge(ran)
        assert ran == [], (
            "a body with no decision_source authorised a real call through "
            "the decorator — the whole point of the provenance check"
        )

    def test_unclassifiable_refusal_does_not_run_the_body(
        self, make_runtime, mock_api, ran
    ):
        """A refusal the SDK cannot read must not be laundered into a call.

        `NullRunUnclassifiedRefusalError` is an
        `NullRunInfrastructureError`, and `@protect` wraps its body in a
        bare `except BaseException`. The two facts are compatible —
        that handler re-raises — but compatibility is not evidence, and
        this is the assertion that makes it evidence.
        """
        respx.post(GATE_URL).mock(
            return_value=_gate_refusal("BUDGET_HARD_BLOCKED", None, status=402)
        )
        make_runtime()
        with pytest.raises(NullRunUnclassifiedRefusalError):
            _charge(ran)
        assert ran == [], "an unreadable refusal became a call"


class TestProtectFailOpenStillHolds:
    """The counter-tests. A dead backend must not freeze the agent."""

    def test_a_real_allow_runs_the_body(self, make_runtime, mock_api, ran):
        respx.post(GATE_URL).mock(return_value=_gate_allow())
        respx.post(EXECUTE_URL).mock(return_value=httpx.Response(200, json={}))
        make_runtime()
        assert _charge(ran) == "charged:100"
        assert ran == ["body"], (
            "a real allow must still execute — a suite of 'body did not "
            "run' assertions passes just as well against a decorator "
            "that refuses everything"
        )

    def test_connection_failure_runs_the_body(self, make_runtime, mock_api, ran):
        """ADR-008's promise, through the decorator.

        `on_denied="message"` is set deliberately: this is the most
        permissive handling the host can ask for, and it still must not
        convert an unreachable gate into a block.
        """
        respx.post(GATE_URL).mock(side_effect=httpx.ConnectError("connection refused"))
        make_runtime(on_denied="message")
        _charge(ran)
        assert ran == ["body"], "ADR-008 fail-open does not survive @protect"

    def test_a_502_with_no_refusal_body_runs_the_body(self, make_runtime, mock_api, ran):
        """A 5xx that is not a gate answer is an outage, not a decision."""
        respx.post(GATE_URL).mock(
            return_value=httpx.Response(502, text="<html>Bad Gateway</html>")
        )
        make_runtime()
        _charge(ran)
        assert ran == ["body"]


class TestOnDeniedReachesProtect:
    """The flag has to mean something at the decorator level too."""

    def test_denied_with_message_enabled_carries_the_server_text(
        self, make_runtime, mock_api, ran
    ):
        respx.post(GATE_URL).mock(
            return_value=_gate_refusal("TOOL_BLOCKED", "denied", status=403)
        )
        make_runtime(on_denied="message")
        with pytest.raises(NullRunDeniedError) as exc:
            _charge(ran)
        assert exc.value.model_safe_text() == "The operator has not allowed this tool."
        assert isinstance(exc.value, NullRunBlockedException)
        assert ran == []

    @pytest.mark.parametrize(
        "code,category,status,expected",
        [
            ("BUDGET_HARD_BLOCKED", "budget", 402, NullRunBudgetError),
            ("WORKFLOW_PAUSED", "halt", 403, NullRunBlockedException),
        ],
    )
    def test_non_denied_categories_never_become_a_message(
        self, make_runtime, mock_api, ran, code, category, status, expected
    ):
        """The negative set, at the decorator level.

        The same reasoning as one level down, and it matters more for a
        decorator: the caller's `except NullRunBlockedException` is
        around a whole function, so a `budget` wall surfacing as
        "that tool is not allowed" is a message an agent will act on by
        trying something else.
        """
        respx.post(GATE_URL).mock(
            return_value=_gate_refusal(code, category, status=status)
        )
        make_runtime(on_denied="message")
        with pytest.raises(expected) as exc:
            _charge(ran)
        assert not isinstance(exc.value, NullRunDeniedError), (
            f"{code} is category={category!r}; on_denied must not reach it"
        )
        assert ran == []

    def test_absent_category_raises_even_with_message_enabled(
        self, make_runtime, mock_api, ran
    ):
        """The opt-in is a statement about `denied`, not permission to guess."""
        respx.post(GATE_URL).mock(
            return_value=_gate_refusal("TOOL_BLOCKED", None, status=403)
        )
        make_runtime(on_denied="message")
        with pytest.raises(NullRunUnclassifiedRefusalError):
            _charge(ran)
        assert ran == []


class TestAsyncProtectIsNotASeparatePath:
    """`protect` returns two different wrappers. Both must hold.

    The sync and async bodies are separate implementations in
    `decorators.py`, and the async one has its own `except Exception`
    around the call. A property proven only on the sync path is a
    property of half the decorator.
    """

    @pytest.mark.asyncio
    async def test_refusal_does_not_run_the_async_body(
        self, make_runtime, mock_api, ran
    ):
        respx.post(GATE_URL).mock(
            return_value=_gate_refusal("TOOL_BLOCKED", "denied", status=403)
        )
        make_runtime()

        @protect
        async def charge_card(amount: int) -> str:
            ran.append("body")
            return "charged"

        with pytest.raises(NullRunError):
            await charge_card(100)
        assert ran == []

    @pytest.mark.asyncio
    async def test_forged_allow_does_not_run_the_async_body(
        self, make_runtime, mock_api, ran
    ):
        respx.post(GATE_URL).mock(return_value=httpx.Response(200, json={"decision": "allow"}))
        make_runtime()

        @protect
        async def charge_card(amount: int) -> str:
            ran.append("body")
            return "charged"

        with pytest.raises(NullRunMalformedGateResponseError):
            await charge_card(100)
        assert ran == []

    @pytest.mark.asyncio
    async def test_a_real_allow_runs_the_async_body(self, make_runtime, mock_api, ran):
        respx.post(GATE_URL).mock(return_value=_gate_allow())
        respx.post(EXECUTE_URL).mock(return_value=httpx.Response(200, json={}))
        make_runtime()

        @protect
        async def charge_card(amount: int) -> str:
            ran.append("body")
            return "charged"

        assert await charge_card(100) == "charged"
        assert ran == ["body"], (
            "the async counter-test matters as much as the sync one: "
            "without it, 'body did not run' passes against an async "
            "wrapper that refuses everything"
        )


class TestOnDeniedThroughRealLangChainTool:
    """`on_denied="message"` must work INSIDE a real `@tool`.

    `TestOnDeniedReachesProtect` above proves the flag reaches
    `@protect`. It does not prove it survives the framework layer an
    agent actually calls through — and that layer is where the mode is
    most likely to break:

      * LangChain's `ToolException` arm is its own error path, and
        `handle_tool_error=True` stringifies whatever it catches;
      * `@tool`/`@protect` ordering determines whether the wrapper is
        even in the call chain;
      * async and sync are separate wrappers.

    `NullRunDeniedError` is what an operator uses to hand an agent a
    written "you may not do this" without ending the run. If the tool
    layer turns it into a string or drops it, the agent either loops or
    crashes, and both are worse than the refusal.
    """

    def test_denied_message_reaches_the_agent_through_a_sync_tool(
        self, make_runtime, mock_api, ran
    ):
        pytest.importorskip("langchain_core")
        from langchain_core.tools import tool

        respx.post(GATE_URL).mock(
            return_value=_gate_refusal("TOOL_BLOCKED", "denied", status=403)
        )
        make_runtime(on_denied="message")

        @protect
        @tool
        def charge(amount: int) -> str:
            """Charge a card."""
            ran.append("body")
            return f"charged:{amount}"

        with pytest.raises(NullRunDeniedError) as exc:
            charge.invoke({"amount": 100})
        assert exc.value.model_safe_text() == "The operator has not allowed this tool."
        assert ran == []

    def test_denied_message_reaches_the_agent_through_the_other_order(
        self, make_runtime, mock_api, ran
    ):
        """`@tool` inside `@protect` — both orders must behave the same."""
        pytest.importorskip("langchain_core")
        from langchain_core.tools import tool

        respx.post(GATE_URL).mock(
            return_value=_gate_refusal("TOOL_BLOCKED", "denied", status=403)
        )
        make_runtime(on_denied="message")

        @tool
        @protect
        def charge(amount: int) -> str:
            """Charge a card."""
            ran.append("body")
            return f"charged:{amount}"

        with pytest.raises(NullRunDeniedError):
            charge.invoke({"amount": 100})
        assert ran == []

    @pytest.mark.asyncio
    async def test_denied_message_reaches_the_agent_through_an_async_tool(
        self, make_runtime, mock_api, ran
    ):
        pytest.importorskip("langchain_core")
        from langchain_core.tools import tool

        respx.post(GATE_URL).mock(
            return_value=_gate_refusal("TOOL_BLOCKED", "denied", status=403)
        )
        make_runtime(on_denied="message")

        @protect
        @tool
        async def charge(amount: int) -> str:
            """Charge a card."""
            ran.append("body")
            return f"charged:{amount}"

        with pytest.raises(NullRunDeniedError) as exc:
            await charge.ainvoke({"amount": 100})
        assert exc.value.model_safe_text() == "The operator has not allowed this tool."
        assert ran == []

    @pytest.mark.parametrize(
        "code,category,status,expected",
        [
            ("BUDGET_HARD_BLOCKED", "budget", 402, NullRunBudgetError),
            ("WORKFLOW_PAUSED", "halt", 403, NullRunBlockedException),
        ],
    )
    def test_budget_and_halt_still_stop_the_tool_under_the_flag(
        self, make_runtime, mock_api, ran, code, category, status, expected
    ):
        """The safety half, at the framework layer.

        `on_denied="message"` is an operator convenience for a POLICY
        denial. Letting it also stringify a budget wall would tell the
        agent "that tool is not allowed" when the truth is "you are out
        of money" — and its obvious next move is to find another way to
        spend.
        """
        pytest.importorskip("langchain_core")
        from langchain_core.tools import tool

        respx.post(GATE_URL).mock(
            return_value=_gate_refusal(code, category, status=status)
        )
        make_runtime(on_denied="message")

        @protect
        @tool
        def charge(amount: int) -> str:
            """Charge a card."""
            ran.append("body")
            return f"charged:{amount}"

        with pytest.raises(expected) as exc:
            charge.invoke({"amount": 100})
        assert not isinstance(exc.value, NullRunDeniedError)
        assert ran == []

    def test_allow_still_runs_the_tool_body(
        self, make_runtime, mock_api, ran
    ):
        """Counter-test: the tool layer is not simply refusing everything."""
        pytest.importorskip("langchain_core")
        from langchain_core.tools import tool

        respx.post(GATE_URL).mock(return_value=_gate_allow())
        respx.post(EXECUTE_URL).mock(return_value=httpx.Response(200, json={}))
        make_runtime(on_denied="message")

        @protect
        @tool
        def charge(amount: int) -> str:
            """Charge a card."""
            ran.append("body")
            return f"charged:{amount}"

        assert charge.invoke({"amount": 100}) == "charged:100"
        assert ran == ["body"]
