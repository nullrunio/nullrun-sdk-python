"""DEF-TC4-001 (QA RUN_ID 20261002T0826, 2026-10-02, prod e8d811a0):
a ``/gate`` rate-limit refusal must surface as the TYPED rate-limit
exception, not as ``NullRunBudgetError``.

## What was observed

``rate_limit_demo.py`` (TC-4) against production, policy
``policy_type=RateLimit`` / ``rate_limit_per_minute=5``:

```
[0..4] ALLOW
[5] OTHER-EXC type=NullRunBudgetError code=NR-B004
     msg="Workflow ... blocked: RATE_LIMIT_EXCEEDED (action=block,
          status_code=None, details={'decision_source': 'gateway', …})"
[6,7] same
```

The GATE is correct: 5 allows, then block at index 5, reason
``RATE_LIMIT_EXCEEDED``. The probe's own parser only accepts
``NR-R001`` / ``NR-R002`` / ``NR-W002/RATE_LIMIT_EXCEEDED``, so it
reports REVIEW.

## Root cause — two independent defects, both required

1. **Backend omits ``details.error_code`` on the rate-limit block.**
   ``orchestrator.rs:641-654`` builds the Block ``details`` with
   only ``scope`` / ``limit_per_minute`` / ``current_count`` /
   ``retry_after_seconds`` / ``retry_after_ms``. The code is
   carried in the 2nd tuple element (``reason_code`` → binds to
   ``GateResponse.explanation``), which is what makes the HTTP
   status resolve correctly (429 via the two-candidate lookup in
   ``gate.rs:88-104``). But ``error_code`` never reaches
   ``details``, so a client reading the machine-readable field
   (CLAUDE.md §13) sees nothing.

2. **SDK ``check_workflow_budget`` does no typed dispatch at all.**
   ``runtime.py`` raises ``NullRunBudgetError`` unconditionally in
   its ``decision == "block"`` arm, so EVERY refusal — rate-limit,
   tool-block, workflow-inactive — surfaces as NR-B004. The
   typed dispatcher ``_build_block_exception`` (which reads
   ``details["error_code"]`` and instantiates the catalog class)
   exists and is wired into ``Runtime.execute``; the ``/gate``
   pre-flight never calls it.

Defect 1 alone leaves the SDK with no code to dispatch on; defect 2
alone leaves the SDK dispatching on a field nobody sets. Both ship.

## Why ``RateLimitError`` is the right answer here

``transport._parse_v3_error_envelope`` already maps
``RATE_LIMIT_EXCEEDED → RateLimitError`` and
``RATE_LIMIT_REDIS_UNAVAILABLE → NullRunRateLimitRedisError``, and
both are documented in ``integrations/fastapi.py:39`` as the codes a
caller branches on. The probe, the cookbook, and the SDK's own
catalog all agree; only the pre-flight raise site disagrees.

## The tests

Each models the real wire body captured from production 429 (a body
whose ``details`` carries NO ``error_code``, plus the
``category``/``user_message``/``agent_message`` surface ADR-062
attaches), and asserts the TYPED class. A body that still only
reaches the keyword-on-explanation fallback (no ``error_code``) is
covered too — it must NOT claim to be a typed rate-limit refusal.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from nullrun.breaker.exceptions import (
    NullRunBudgetError,
    NullRunRateLimitRedisError,
    RateLimitError,
)

BASE_URL = "https://api.test.nullrun.io"
GATE_URL = f"{BASE_URL}/api/v1/gate"


def _rate_limit_body(*, with_error_code: bool) -> dict:
    """The production 429 body for a per-workflow rate-limit block.

    ``with_error_code=False`` reproduces prod BEFORE the fix
    (orchestrator.rs built ``details`` without the key). ``True`` is
    the post-fix shape. Both carry ``explanation`` = the reason code
    because the Block dispatcher puts ``reason_code`` in that slot.
    """
    details: dict = {
        "decision_source": "gateway",
        "reasons": "RATE_LIMIT_EXCEEDED",
        "scope": "api_key",
        "limit_per_minute": 5,
        "current_count": 6,
        "retry_after_seconds": 42,
        "retry_after_ms": 42000,
        "enforcement_path": "rate_limit",
    }
    if with_error_code:
        details["error_code"] = "RATE_LIMIT_EXCEEDED"
    return {
        "decision": "block",
        "decision_source": "gateway",
        "explanation": "RATE_LIMIT_EXCEEDED",
        "policy_version": 1,
        "explanations": ["RATE_LIMIT_EXCEEDED"],
        # ADR-062 surface as attached by the backend. `budget` is NOT
        # model-message-safe, so `agent_message` is absent by design.
        "category": "budget",
        "user_message": "Rate limit exceeded for this workflow.",
        "details": details,
    }


def _mock_gate(body: dict, status: int) -> None:
    respx.post(GATE_URL).mock(
        return_value=httpx.Response(status, json=body)
    )


class TestRateLimitBlockIsTyped:
    """The headline regression: a rate-limit refusal is a RateLimitError."""

    def test_rate_limit_exceeded_raises_rate_limit_error(
        self, make_runtime, mock_api
    ):
        _mock_gate(_rate_limit_body(with_error_code=True), 429)
        rt = make_runtime()
        with pytest.raises(RateLimitError) as exc_info:
            rt.check_workflow_budget()
        assert exc_info.value.error_code == "NR-R001"

    def test_rate_limit_redis_unavailable_raises_typed_redis_error(
        self, make_runtime, mock_api
    ):
        """NR-R002 is the fail-CLOSED infra sibling. It must NOT be
        reported as a budget exhaustion either — an operator reading
        NR-B004 would go raise a cap when Redis is what is down."""
        body = _rate_limit_body(with_error_code=True)
        body["explanation"] = "RATE_LIMIT_REDIS_UNAVAILABLE"
        body["explanations"] = ["RATE_LIMIT_REDIS_UNAVAILABLE"]
        body["category"] = "infra"
        body["details"]["error_code"] = "RATE_LIMIT_REDIS_UNAVAILABLE"
        _mock_gate(body, 503)
        rt = make_runtime()
        with pytest.raises(NullRunRateLimitRedisError):
            rt.check_workflow_budget()

    def test_rate_limit_redis_unavailable_does_not_raise_budget_error(
        self, make_runtime, mock_api
    ):
        """The negative half of the same property, asserted
        separately so a future refactor cannot pass the test above by
        raising the right thing for the wrong reason."""
        body = _rate_limit_body(with_error_code=True)
        body["explanation"] = "RATE_LIMIT_REDIS_UNAVAILABLE"
        body["explanations"] = ["RATE_LIMIT_REDIS_UNAVAILABLE"]
        body["category"] = "infra"
        body["details"]["error_code"] = "RATE_LIMIT_REDIS_UNAVAILABLE"
        _mock_gate(body, 503)
        rt = make_runtime()
        with pytest.raises(NullRunRateLimitRedisError):
            try:
                rt.check_workflow_budget()
            except NullRunBudgetError as exc:  # pragma: no cover
                pytest.fail(
                    f"infra refusal reported as budget exhaustion: {exc!r}"
                )
            raise  # re-raise the RateLimitRedis error for the outer assert


class TestToolBlockIsNotABudgetError:
    """TC-2's observation, generalised: the same hardcoded raise made
    a policy tool-block look like budget exhaustion."""

    def test_tool_blocked_raises_tool_blocked_error(
        self, make_runtime, mock_api
    ):
        body = {
            "decision": "block",
            "decision_source": "gateway",
            "explanation": "TOOL_BLOCKED",
            "policy_version": 1,
            "explanations": ["TOOL_BLOCKED"],
            "category": "denied",
            "user_message": "Tool 'bash' is blocked by policy.",
            # Denied IS model-message-safe, so ADR-062 populates this.
            "agent_message": "I can't run that tool.",
            "details": {"error_code": "TOOL_BLOCKED"},
        }
        _mock_gate(body, 403)
        rt = make_runtime()
        with pytest.raises(Exception) as exc_info:
            rt.check_workflow_budget()
        assert not isinstance(exc_info.value, NullRunBudgetError), (
            "a policy tool-block surfaced as NullRunBudgetError / NR-B004 — "
            "the pre-flight raise site hardcodes the budget type for every "
            "refusal instead of dispatching on details.error_code"
        )
        assert exc_info.value.error_code == "NR-T001"


class TestBudgetBlockStillBudget:
    """The dispatch must not regress the case that motivated the
    hardcoded raise in the first place (test_gate_real_path.py)."""

    def test_budget_block_still_raises_budget_error(
        self, make_runtime, mock_api
    ):
        body = {
            "decision": "block",
            "decision_source": "gateway",
            "explanation": "BUDGET_HARD_BLOCKED",
            "policy_version": 1,
            "explanations": ["Budget exhausted: need 5 cents, 0 available"],
            "category": "budget",
            "user_message": "Budget exhausted.",
            "details": {"error_code": "BUDGET_HARD_BLOCKED"},
        }
        _mock_gate(body, 402)
        rt = make_runtime()
        with pytest.raises(NullRunBudgetError) as exc_info:
            rt.check_workflow_budget()
        assert exc_info.value.error_code == "NR-B004"

    def test_legacy_explanation_only_block_keeps_its_pinned_class(
        self, make_runtime, mock_api
    ):
        """A pre-structured backend that sends no ``error_code`` must
        not start raising something new.

        Two contracts meet here and both are preserved:

        * the SHARED dispatcher stays on the base
          ``NullRunBlockedException`` for a type guessed from English
          — pinned by ``test_2026_09_10_runtime_block_typed_dispatch.py::
          test_legacy_keyword_path_budget``, because a subclass chosen
          by substring-matching claims more confidence than the wire
          gave;
        * the ``/gate`` PRE-FLIGHT is a budget pre-flight and its
          caller contract is ``NullRunBudgetError`` — pinned by
          ``test_gate_real_path.py::test_real_block_still_honored``.

        So the widening happens at the caller that promises it, not in
        the shared dispatcher. This test pins the observable result of
        that split, since both pins alone would pass with either half
        missing.

        Status 200 + ``decision="block"`` rather than 402: a 4xx
        refusal is required to carry ``category`` (ADR-062 §2.2) and a
        pre-structured backend predates that field."""
        body = {
            "decision": "block",
            "decision_source": "gateway",
            "explanation": "Budget exhausted: need 5 cents, 0 available",
            "policy_version": 1,
            "explanations": [],
        }
        _mock_gate(body, 200)
        rt = make_runtime()
        with pytest.raises(NullRunBudgetError) as exc_info:
            rt.check_workflow_budget()
        assert "Budget exhausted" in exc_info.value.reason
        assert exc_info.value.error_code == "NR-B004"
        # The dispatcher's guess is still visible, so an operator can
        # tell a guessed budget classification from a wire-stated one.
        # The pre-flight wraps the dispatcher's exception, so the
        # shim sits at whatever depth the constructor nested it —
        # search rather than hardcode the level.
        def _find_mapped_class(details: dict) -> str | None:
            if "mapped_class" in details:
                return details["mapped_class"]
            for value in details.values():
                if isinstance(value, dict):
                    found = _find_mapped_class(value)
                    if found:
                        return found
            return None

        assert _find_mapped_class(exc_info.value.details) == "NullRunBudgetError"
