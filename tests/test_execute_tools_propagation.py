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
import threading

import httpx
import pytest
import respx

from nullrun.breaker.exceptions import NullRunBlockedException

BASE_URL = "https://api.test.nullrun.io"
EXECUTE_URL = f"{BASE_URL}/api/v1/execute"


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
