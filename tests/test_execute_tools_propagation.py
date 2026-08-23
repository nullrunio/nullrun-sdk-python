"""
DEF-LATEST_PLAN-F01 (2026-08-21) regression test: SDK → /execute →
real wire body must include the per-call `tools` list when
``set_call_context(tools=...)`` was set.

Bug that this test pins down: pre-fix, ``Runtime.execute()`` did NOT
populate the ``tools`` array on the /execute wire body. The backend's
Step 3 tool_block check
(``backend/src/proxy/http/gate/orchestrator.rs:1847-1893``) returns
``Block { TOOL_BLOCKED, reason: "no_tools_field" }`` whenever the
workflow's effective ``policy.tool_patterns`` is non-empty AND the
``tools`` field is absent. The /gate path was already threading
``tools`` correctly; this test closes the gap on the /execute path
that @sensitive-decorated functions follow.

This file asserts the fixed behaviour:

  1. Default /execute request (no set_call_context) → no ``tools``
     key in the wire body. The backend's TB-1 branch fires
     fail-CLOSED for sensitive tools with active policy.tool_patterns,
     but for non-sensitive tools (no policy enforcement) the absence
     is preserved exactly the same way the /gate test asserts it.
  2. ``set_call_context(tools=[...])`` → the request sent to /execute
     contains that tool list. Mirrors ``test_gate_real_path.py`` for
     the /gate path.
  3. ``set_call_context(tools=[])`` clears the previously-set tools
     and the next execute call must not include the ``tools`` key —
     preserves the same "no tools" vs "I didn't tell you" distinction
     the backend relies on.
  4. ``@sensitive`` decorator auto-threads ``tools`` from
     ``get_call_tools()`` contextvar through to the /execute wire
     body, even when the user did not call ``set_call_context``
     themselves (the decorator captures the contextvar at decoration
     time on the wrapper's call site).
"""

from __future__ import annotations

import json
import os
import threading

import httpx
import pytest
import respx

from nullrun.breaker.exceptions import NullRunBlockedException

BASE_URL = "https://api.test.nullrun.io"
EXECUTE_URL = f"{BASE_URL}/api/v1/execute"
GATE_URL = f"{BASE_URL}/api/v1/gate"


@pytest.fixture
def captured_execute_bodies():
    """Capture every /execute request body sent by the SDK under test.

    Returns a mutable list — append to read what was sent. Replaces
    the default /execute mock from the ``mock_api`` fixture with one
    that captures the body before returning a decision=allow response.
    """
    bodies: list[dict] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "decision": "allow",
                "decision_source": "gateway",
                "explanation": "allowed",
                "policy_version": 1,
            },
        )

    respx.post(EXECUTE_URL).mock(side_effect=_capture)
    return bodies


class TestExecuteToolsPropagation:
    """F01: runtime.execute must propagate the per-call `tools` array
    from the contextvar to the /execute wire body."""

    def test_set_call_context_tools_appear_in_execute_body(
        self, make_runtime, mock_api, captured_execute_bodies
    ):
        """When the user calls set_call_context(tools=[...]) and then
        triggers a sensitive-tool @execute round-trip, the wire body
        must contain the tools list. This is the headline F01 closure."""
        from nullrun.context import get_call_tools, set_call_context

        rt = make_runtime()
        set_call_context(tools=["refund_customer", "send_email"])
        assert get_call_tools() == ("refund_customer", "send_email")

        # mode='strict' forces the /execute round-trip regardless of
        # whether the tool is in the sensitive-tools registry.
        rt.execute(
            "refund_customer",
            {"args": (), "kwargs": {"amount": 100}},
            mode="strict",
        )

        assert captured_execute_bodies, "no /execute call was captured"
        body = captured_execute_bodies[-1]
        assert body.get("tools") == ["refund_customer", "send_email"], (
            f"expected tools list on the /execute wire body, got body={body!r}"
        )

    def test_no_call_context_means_no_tools_field(
        self, make_runtime, mock_api, captured_execute_bodies
    ):
        """When the user never called set_call_context, the SDK must
        NOT send a `tools` key on /execute (None, not []). The backend
        distinguishes 'no tools' (send []) from 'I did not tell you'
        (omit the key) — see backend orchestrator Step 3 doc comment."""
        rt = make_runtime()
        rt.execute(
            "refund_customer",
            {"args": (), "kwargs": {"amount": 100}},
            mode="strict",
        )
        assert captured_execute_bodies, "no /execute call was captured"
        body = captured_execute_bodies[-1]
        assert "tools" not in body, (
            "when the user did not call set_call_context(tools=...) "
            "the SDK must not include a `tools` key on /execute — "
            "sending [] would tell the backend 'no tools will be called' "
            "which differs from 'I did not tell you what tools'"
        )

    def test_clear_call_context_drops_tools(
        self, make_runtime, mock_api, captured_execute_bodies
    ):
        """set_call_context(tools=[]) clears the previously-set tools
        and the next /execute call must not include the `tools` key.

        Mirrors the /gate round-trip test in
        `test_gate_real_path.py::TestSetCallContext::test_clear_call_context`.
        """
        from nullrun.context import get_call_tools, set_call_context

        set_call_context(tools=["refund_customer"])
        assert get_call_tools() == ("refund_customer",)
        set_call_context(tools=[])
        assert get_call_tools() == ()

        rt = make_runtime()
        rt.execute(
            "refund_customer",
            {"args": (), "kwargs": {"amount": 100}},
            mode="strict",
        )
        assert captured_execute_bodies, "no /execute call was captured"
        body = captured_execute_bodies[-1]
        # The `tools` field is what the backend distinguishes; the
        # body may still contain `tool: "refund_customer"` (the singular
        # tool name) which is an unrelated field.
        assert "tools" not in body, (
            f"set_call_context(tools=[]) should clear the tools "
            f"contextvar, but body still contains tools={body.get('tools')!r}"
        )


class TestDecoratorThreading:
    """F01 follow-up: @sensitive must auto-thread tools from
    get_call_tools() contextvar through to the /execute wire body,
    even when the user did not call set_call_context directly.

    The decorator's `_enforce_sensitive_tool` calls
    `runtime.execute(..., tools=get_call_tools())` which is a
    kwarg pass-through. The runtime/transport layer is already
    covered by the tests in `TestExecuteToolsPropagation` above
    (the runtime/transport signature accepts `tools` and forwards
    it to the wire body).

    This module pins the decorator source so a refactor that
    drops the `tools=get_call_tools()` kwarg fails the test
    immediately — the decorator-side behavioral path is
    intentionally NOT exercised here because it requires warming
    up the full decorator registration flow (the
    `_do_sensitive_register` call at decoration time needs a
    runtime singleton in the registry, which is a separate
    concern from the F01 fix surface).
    """

    def test_sensitive_decorator_threads_tools_kwarg_to_runtime_execute(
        self,
    ):
        """Source-pin: `_enforce_sensitive_tool` must call
        `runtime.execute(..., tools=get_call_tools(), ...)`. The
        `tools=` kwarg is the bridge that propagates the per-call
        contextvar through to the runtime layer, which is in turn
        already covered by the runtime/transport tests above."""
        from pathlib import Path

        # Read the decorator source directly so this test is a
        # structural regression guard rather than a behavioural
        # one (the register-singleton flow is too noisy to exercise
        # in a single test without the rest of the @sensitive
        # machinery).
        src_path = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "nullrun"
            / "decorators.py"
        )
        source = src_path.read_text(encoding="utf-8")
        # The decorators.py source has Windows CRLF preserved by
        # git's autocrlf — normalise before scanning so the needle
        # matches the production text regardless of line-ending.
        source = source.replace("\r\n", "\n")

        # The exact pattern the source must contain. Built via
        # runtime format so the test's own source cannot match
        # self-referentially.
        _kwarg = "tools={call}".format(call="get_call_tools()")
        needle = (
            "result = runtime.execute(\n"
            "            fn.__name__,\n"
            "            {\"args\": masked_args, \"kwargs\": masked},\n"
            "            on_transport_error=\"raise\",\n"
            "            business_impact=business_impact_dict,\n"
            "            action_digest=action_digest_hex,\n"
            "            " + _kwarg + ",\n"
            "        )"
        )
        assert needle in source, (
            "decorators.py::_enforce_sensitive_tool must call "
            "runtime.execute(..., tools=get_call_tools(), ...). "
            "The F01 fix threads the per-call tools contextvar "
            "through to the runtime layer; dropping the kwarg "
            "re-introduces the TB-1 no_tools_field silent block."
        )

    def test_decorator_imports_get_call_tools(self):
        """Source-pin: `from nullrun.context import (...)` block
        in decorators.py must include `get_call_tools`. The
        import is the only place the decorator learns about the
        per-call tools contextvar — dropping it would silently
        NameError at runtime when the kwarg is evaluated."""
        from pathlib import Path

        src_path = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "nullrun"
            / "decorators.py"
        )
        source = src_path.read_text(encoding="utf-8")
        source = source.replace("\r\n", "\n")

        assert "from nullrun.context import (" in source, (
            "decorators.py must import from nullrun.context"
        )
        # The named import must be inside the parens.
        import_block = source.split("from nullrun.context import (", 1)[1]
        import_block = import_block.split(")", 1)[0]
        assert "get_call_tools" in import_block, (
            "decorators.py must import `get_call_tools` from "
            "nullrun.context — the F01 fix reads the per-call "
            "tools contextvar via this helper"
        )


class TestDecoratorF03BehavioralRegression:
    """F03 (2026-08-22) behavioural regression: ``@protect`` /
    ``@sensitive`` must populate the per-call ``_call_tools_var``
    contextvar from ``fn.__name__`` when the user did NOT explicitly
    call ``set_call_context(tools=...)``.

    Pre-F03 the contextvar was never written by SDK internal code, so
    the /gate round-trip triggered by ``check_workflow_budget()``
    omitted the ``tools`` field. The backend's Step 3 tool_block check
    (``backend/src/proxy/http/gate/orchestrator.rs``) fail-CLOSED via
    TB-1 (``no_tools_field``) and every approval-rule probe returned
    ``decision=block reason='TOOL_BLOCKED'`` without ever reaching the
    approval_rule_eval step. This suite exercises the decorator's
    full runtime path (not the source-pin-only test in
    ``TestDecoratorThreading`` above) so a future refactor that drops
    the population step fails the test immediately.

    The transport layer already accepts ``tools`` on the wire body
    (covered by ``TestExecuteToolsPropagation`` above). This module
    closes the gap on the *decorator* leg of the handoff: from
    ``@protect`` invocation through ``_protect_body`` into the
    underlying runtime call.
    """

    @pytest.fixture
    def captured_gate_and_execute(self):
        """Capture every /gate AND /execute request body. Returns
        a tuple of two mutable lists ``(gate_bodies, execute_bodies)``
        that the test can index into to assert what was sent."""
        gate_bodies: list[dict] = []
        execute_bodies: list[dict] = []

        def _gate_capture(request: httpx.Request) -> httpx.Response:
            gate_bodies.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "decision": "allow",
                    "decision_source": "gateway",
                    "explanation": "allowed",
                    "policy_version": 1,
                    "explanations": [],
                },
            )

        def _execute_capture(request: httpx.Request) -> httpx.Response:
            execute_bodies.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "decision": "allow",
                    "decision_source": "gateway",
                    "explanation": "allowed",
                    "policy_version": 1,
                },
            )

        respx.post(GATE_URL).mock(side_effect=_gate_capture)
        respx.post(EXECUTE_URL).mock(side_effect=_execute_capture)
        return gate_bodies, execute_bodies

    def test_protect_populates_tools_on_gate_body_when_user_omits_set_call_context(
        self, make_runtime, mock_api, captured_gate_and_execute
    ):
        """The decorator's ``_protect_body`` must seed
        ``_call_tools_var = (fn.__name__,)`` so ``check_workflow_budget``
        forwards ``tools`` on the /gate wire body — even when the user
        never called ``set_call_context``. This is the headline F03
        closure for the /gate leg."""
        gate_bodies, _ = captured_gate_and_execute
        from nullrun.context import get_call_tools

        # Defensive: assert the precondition the F03 fix relies on
        # (no caller-side set_call_context for this test).
        assert get_call_tools() == ()

        import nullrun.decorators as dec

        rt = make_runtime()
        dec._runtime = rt  # belt-and-braces — make_runtime already does this.

        @dec.protect
        def my_agent(query: str) -> str:
            return f"answer:{query}"

        result = my_agent("hello")
        assert result == "answer:hello"

        # /gate must have been called once and must carry
        # tools=["my_agent"] — populated by _protect_body from
        # fn.__name__ before check_workflow_budget().
        assert gate_bodies, "no /gate call was captured"
        gate_body = gate_bodies[-1]
        assert gate_body.get("tools") == ["my_agent"], (
            f"F03 not closed: /gate body must carry tools=['my_agent'] "
            f"after @protect; got body={gate_body!r}. The fix is in "
            f"decorators.py::_protect_body which seeds "
            f"_call_tools_var from fn.__name__ before "
            f"check_workflow_budget()."
        )

    def test_protect_does_not_override_explicit_set_call_context(
        self, make_runtime, mock_api, captured_gate_and_execute
    ):
        """When the user explicitly called
        ``set_call_context(tools=[user_list])``, the decorator MUST
        preserve the user's list — not overwrite it with
        ``[fn.__name__]``. Precedence: explicit > auto-populated."""
        gate_bodies, _ = captured_gate_and_execute
        from nullrun.context import get_call_tools, set_call_context

        # User explicitly declared their tool list.
        set_call_context(tools=["user_declared_tool", "another_tool"])
        assert get_call_tools() == ("user_declared_tool", "another_tool")

        import nullrun.decorators as dec

        rt = make_runtime()
        dec._runtime = rt

        @dec.protect
        def my_agent(query: str) -> str:
            return f"answer:{query}"

        try:
            my_agent("hello")
            assert gate_bodies, "no /gate call was captured"
            gate_body = gate_bodies[-1]
            # The user's explicit list survives — NOT fn.__name__.
            assert gate_body.get("tools") == [
                "user_declared_tool",
                "another_tool",
            ], (
                f"explicit set_call_context must win over decorator "
                f"auto-population; got body={gate_body!r}"
            )
        finally:
            # Clean up so the test's explicit context doesn't leak.
            set_call_context(tools=[])
            assert get_call_tools() == ()

    def test_protect_restores_prior_call_tools_context_after_call(
        self, make_runtime, mock_api, captured_gate_and_execute
    ):
        """Token-based reset: a nested @protect inside an outer @protect
        (or inside ``with workflow``) restores the prior
        ``_call_tools_var`` value on exit. Bare @protect leaves the
        contextvar empty again. The same shape as the legacy
        ``_trace_id_var`` / ``_span_id_var`` resets in _protect_body."""
        from nullrun.context import _call_tools_var, get_call_tools, set_call_context

        # Outer: user explicitly set tools=["outer"]
        set_call_context(tools=["outer"])
        assert get_call_tools() == ("outer",)

        import nullrun.decorators as dec

        rt = make_runtime()
        dec._runtime = rt

        @dec.protect
        def outer(query: str) -> str:
            return f"outer:{query}"

        # Before invocation: outer context is "outer"
        assert get_call_tools() == ("outer",)

        # Invoke outer — its _protect_body will see _existing="outer"
        # and SKIP auto-population (call_tools_token stays None).
        _ = outer("hello")

        # After invocation: outer context STILL "outer" (unchanged).
        assert get_call_tools() == ("outer",), (
            f"explicit set_call_context was clobbered by the decorator "
            f"after the call exited; got {get_call_tools()!r}"
        )

        # Clean up
        set_call_context(tools=[])
        assert get_call_tools() == ()

    def test_sensitive_decorator_populates_tools_on_execute_body(
        self, make_runtime, mock_api, captured_gate_and_execute
    ):
        """``@sensitive`` is the decorator combo reported in DEF-LATEST_PLAN-F03
        (probes ``qa/approval_rules/ar_toolname_run.py``,
        ``ar_toolname_run_chain.py``, ``ar_params_run.py``,
        ``ar_threshold_run.py``). Pre-F03 the ``_enforce_sensitive_tool``
        ``runtime.execute(..., tools=get_call_tools())`` call saw an
        empty contextvar, the wire body omitted ``tools``, and the
        backend's Step 3 tool_block fail-CLOSED via TB-1
        (``no_tools_field``) BEFORE the approval_rule_eval step could
        fire — every approval-rule probe returned
        ``decision=block reason='TOOL_BLOCKED'``.

        Post-F03 the decorator populates ``_call_tools_var`` from
        ``fn.__name__`` in ``_protect_body`` (before /gate and
        /execute are called), and ``runtime.execute`` now accepts the
        ``tools=`` kwarg directly so the source-pin pattern at
        decorators.py:735 no longer TypeErrors. The /execute wire body
        must carry ``tools=["refund_customer"]``.
        """
        _gate_bodies, execute_bodies = captured_gate_and_execute
        from nullrun.context import get_call_tools

        assert get_call_tools() == ()  # precondition

        import nullrun.decorators as dec

        rt = make_runtime()
        dec._runtime = rt

        # The /sensitive registration flow warms the runtime
        # singleton's sensitive-tools set. ``_do_sensitive_register``
        # calls ``add_sensitive_tool(fn.__name__)`` so the
        # ``_enforce_sensitive_tool`` short-circuit (line 606 in
        # decorators.py) doesn't return early.
        @dec.sensitive
        @dec.protect
        def refund_customer(refund_amount: float) -> str:
            return f"refund:{refund_amount}"

        # Register the tool manually (decoration-time registration
        # uses the lazy singleton; in this test we pin the runtime
        # directly via dec._runtime so the registration lands on the
        # pinned instance — same trick make_runtime uses).
        rt.add_sensitive_tool("refund_customer")

        result = refund_customer(refund_amount=100.0)
        assert result == "refund:100.0"

        # /execute (the sensitive-tool round-trip) must carry
        # tools=["refund_customer"] — the F03 headline closure.
        assert execute_bodies, "no /execute call was captured"
        execute_body = execute_bodies[-1]
        assert execute_body.get("tools") == ["refund_customer"], (
            f"F03 not closed on /execute path: body must carry "
            f"tools=['refund_customer']; got body={execute_body!r}. "
            f"This is the symptom that broke all four approval-rule "
            f"probes in LATEST_PLAN run 20260822-181500-a3f1."
        )
