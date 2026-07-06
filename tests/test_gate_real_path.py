"""
T5 (2026-06-27) regression test: SDK → /gate → real decision.

Bug that this test pins down: pre-T1, every SDK `/gate` call for any
workflow with a budget was hard-blocked with
  "Tool 'llm' was blocked because policy 'Rule 1 (cost_limit)' (score 70.00) matched"
because the backend's `PolicyEvaluationGraph.evaluate ` stub
returned `Block` for any synthetic `cost_limit` rule with score > 0.8
(see `backend/src/policy/graph.rs:448-462`
 `backend/src/proxy/http/gate/internal.rs:619-628` pre-T1).

This file asserts the fixed behaviour:

  1. Default /gate request (no `set_call_context`) → allow.
     The body runs. Pre-T1 this would have been a hard block.
  2. `set_call_context(model=...)` → the request sent to /gate
     contains that model name (NOT the old `budget-precheck`
     sentinel).
  3. `set_call_context(tools=[...])` → the request sent to /gate
     contains that tool list. Backend's tool_block check can then
     match against the workflow's blocked_tools aggregate.
  4. SDK does NOT send `model="budget-precheck"` anywhere.
  5. The runtime's pre-flight (`check_workflow_budget`) does NOT
     raise on a real `decision="allow"` response.
  6. The runtime's pre-flight DOES raise `WorkflowKilledInterrupt`
     on a real `decision="block"` response (so the fix didn't
     accidentally remove the real-block path).
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

import nullrun
from nullrun.breaker.exceptions import WorkflowKilledInterrupt

BASE_URL = "https://api.test.nullrun.io"
GATE_URL = f"{BASE_URL}/api/v1/gate"


@pytest.fixture
def captured_bodies():
    """Replace the default /gate mock with one that captures every
    request body and returns allow. Returns a mutable list — append
    to read what was sent.
    """
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


class TestGateRealPathRegression:
    """The original customer bug: budget_cents > 0 must NOT auto-block."""

    def test_default_request_allows_clean_workflow(
        self, make_runtime, mock_api, captured_bodies
    ):
        """A workflow with `max_budget_cents > 0` and a simple LLM
        call must return `allow` from /gate (NOT the old blanket
        block on the synthetic `cost_limit` rule)."""
        rt = make_runtime()
        # No set_call_context — uses defaults (model=None, tools= )
        rt.check_workflow_budget()
        # If we got here without WorkflowKilledInterrupt, the gate
        # path returned allow. Inspect the captured request body.
        assert captured_bodies, "no /gate call was captured"
        body = captured_bodies[-1]
        # SDK must not send the old fake `model=budget-precheck` sentinel.
        assert body.get("model") != "budget-precheck", (
            "SDK must not send the old fake `model=budget-precheck` "
            "sentinel — it forced backend pricing into the default "
            "rate and blocked per-model budget tiers"
        )

    def test_real_block_still_honored(self, make_runtime, mock_api):
        """T1 must NOT have accidentally removed the real-block path.
        Backend returning decision=block (with a real reason, NOT a
        FALLBACK_* synthetic) must still raise WorkflowKilledInterrupt.
        """
        respx.post(GATE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "decision": "block",
                    "decision_source": "gateway",
                    "explanation": "Budget exhausted: need 5 cents, 0 available",
                    "policy_version": 1,
                    "explanations": [],
                },
            )
        )
        rt = make_runtime()
        with pytest.raises(WorkflowKilledInterrupt) as exc_info:
            rt.check_workflow_budget()
        assert "Budget exhausted" in exc_info.value.reason

    def test_no_policy_graph_in_request(
        self, make_runtime, mock_api, captured_bodies
    ):
        """The wire payload must not contain any score/graph residue
        from the old `PolicyEvaluationGraph` code path."""
        rt = make_runtime()
        rt.check_workflow_budget()
        assert captured_bodies, "no /gate call was captured"
        body = captured_bodies[-1]
        for key in body:
            assert not key.startswith("policy-"), (
                f"request body should not contain policy-N keys "
                f"from the old graph plumbing, but got: {key!r}"
            )


class TestSetCallContext:
    """T4: per-call context flows into the /gate pre-flight."""

    def test_set_call_context_model_is_sent(
        self, make_runtime, mock_api, captured_bodies
    ):
        from nullrun.context import get_call_model, set_call_context

        rt = make_runtime()
        set_call_context(model="claude-sonnet-4-6")
        assert get_call_model() == "claude-sonnet-4-6"

        rt.check_workflow_budget()
        assert captured_bodies, "no /gate call was captured"
        body = captured_bodies[-1]
        # Real model name on the wire, not the old sentinel.
        assert body.get("model") == "claude-sonnet-4-6"
        assert body.get("model") != "budget-precheck"

    def test_set_call_context_tools_are_sent(
        self, make_runtime, mock_api, captured_bodies
    ):
        from nullrun.context import get_call_tools, set_call_context

        rt = make_runtime()
        set_call_context(tools=["shell.run", "code.eval"])
        assert get_call_tools() == ("shell.run", "code.eval")

        rt.check_workflow_budget()
        assert captured_bodies, "no /gate call was captured"
        body = captured_bodies[-1]
        assert body.get("tools") == ["shell.run", "code.eval"], (
            f"expected tools list on the wire, got body={body!r}"
        )

    def test_no_call_context_means_no_tools_field(
        self, make_runtime, mock_api, captured_bodies
    ):
        """When the user never called set_call_context, the SDK must
        NOT send a `tools` key at all (None, not []). The backend
        treats the two differently — see
        `gate/internal.rs::check_tool_block` doc-comment."""
        rt = make_runtime()
        rt.check_workflow_budget()
        assert captured_bodies, "no /gate call was captured"
        body = captured_bodies[-1]
        assert "tools" not in body, (
            "when the user did not call set_call_context(tools=...) "
            "the SDK must not include a `tools` key at all — sending "
            "[] would tell the backend 'no tools will be called' which "
            "is different from 'I did not tell you what tools'"
        )

    def test_clear_call_context(
        self, make_runtime, mock_api, captured_bodies
    ):
        """set_call_context(tools=[]) clears the previously-set tools
        and the next gate call must not include the `tools` key.
        Distinguishing "no tools" from "I didn't tell you" is
        important for backend tool_block enforcement."""
        from nullrun.context import get_call_tools, set_call_context

        set_call_context(tools=["shell.run"])
        assert get_call_tools() == ("shell.run",)
        set_call_context(tools=[])
        assert get_call_tools() == ()

        rt = make_runtime()
        rt.check_workflow_budget()
        assert captured_bodies, "no /gate call was captured"
        body = captured_bodies[-1]
        assert "tools" not in body
        assert "shell.run" not in json.dumps(body)


class TestPackageExports:
    """The new T4 helpers are reachable from `nullrun.*`."""

    def test_set_call_context_exported(self):
        from nullrun import get_call_model, get_call_tools, set_call_context

        # Smoke: each is callable and idempotent
        set_call_context(model="claude-opus-4-7", tools=["x"])
        try:
            assert get_call_model() == "claude-opus-4-7"
            assert get_call_tools() == ("x",)
        finally:
            # Clean up the contextvar so it doesn't leak to other tests.
            from nullrun.context import (
                _call_model_var,
                _call_tools_var,
            )

            _call_model_var.set(None)
            _call_tools_var.set(())