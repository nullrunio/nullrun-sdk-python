"""tests/test_auth_fail_closed.py — 401 must not read as "allowed".

DEF-MP-TS12-ENF-01 (RUN_ID 20260929T1338, 2026-09-29).

`check_workflow_budget` re-read a 401 as permission to proceed:

  1. `_retry_with_backoff` raises `NullRunAuthError` on any 401 and
     re-raises it WITHOUT retrying.
  2. Both `except` arms in `check_workflow_budget` swallowed it and
     returned None, which the caller reads as "no block".
  3. `TransportErrorSource.AUTH_ERROR` was also in the synthetic
     fail-OPEN set, so a 401 arriving as a returned response rather
     than a raise was reclassified as a transport error.

The agent therefore executed a call the backend had refused.

The fix classifies by TYPE (`NullRunAuthenticationError`), never by
inspecting the message, and preserves ADR-008's transport fail-OPEN
policy unchanged. The three-branch contract these tests pin:

    connection/transport failure -> fail-OPEN (proceed)
    401 authentication failure   -> raise
    enforcement 4xx              -> enforcement exception
"""

from __future__ import annotations

import httpx
import pytest
import respx

from nullrun.breaker.exceptions import (
    NullRunAuthError,
    NullRunBudgetError,
)

BASE_URL = "https://api.test.nullrun.io"
GATE_URL = f"{BASE_URL}/api/v1/gate"


class TestAuthFailClosed:
    def test_401_raises_instead_of_failing_open(self, make_runtime, mock_api):
        """The Blocker itself: a refused key must not become "allowed"."""
        respx.post(GATE_URL).mock(
            return_value=httpx.Response(401, json={"error_code": "HMAC_REPLAY"})
        )
        rt = make_runtime()
        with pytest.raises(NullRunAuthError):
            rt.check_workflow_budget()

    def test_401_in_cache_enabled_path_also_raises(
        self, make_runtime, mock_api
    ):
        """The cached branch has its own except arm — pin it too.

        Pre-fix both arms swallowed independently, so fixing only the
        non-cached path would have left the chain-mode hole open.
        `chain_id` is read from a contextvar, so the cached path is
        reached by entering a chain scope.
        """
        import uuid

        from nullrun.context import chain

        respx.post(GATE_URL).mock(
            return_value=httpx.Response(401, json={"error_code": "HMAC_REPLAY"})
        )
        rt = make_runtime()
        with chain(str(uuid.uuid4())):
            with pytest.raises(NullRunAuthError):
                rt.check_workflow_budget()

    def test_transport_failure_still_fails_open(self, make_runtime, mock_api):
        """ADR-008 preserved: a dead backend must not freeze the agent.

        This is the counter-test to the two above. If a future change
        widens the fail-CLOSED arm to all exceptions, this fails —
        which is the point: the fix must narrow auth, not close the
        gate.
        """
        respx.post(GATE_URL).mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        rt = make_runtime()
        # No exception: fail-OPEN, the call proceeds.
        result = rt.check_workflow_budget()
        assert result is None, "transport failure must still fail open"

    def test_enforcement_4xx_still_raises_budget_error(
        self, make_runtime, mock_api
    ):
        """A real enforcement block keeps raising the budget exception.

        This is the case the backend half of DEF-MP-TS12-ENF-01
        produces: with the status-mapping fix,
        BUDGET_WORKFLOW_BLOCKED arrives as 402 and must surface as
        NullRunBudgetError, not be swallowed.
        """
        respx.post(GATE_URL).mock(
            return_value=httpx.Response(
                402,
                json={
                    "decision": "block",
                    "decision_source": "gateway",
                    "explanation": "BUDGET_WORKFLOW_BLOCKED",
                    "error_code": "BUDGET_WORKFLOW_BLOCKED",
                    # ADR-062 §2.2 — a real refusal always carries a
                    # category; the backend classifies this one
                    # ``budget``. Omitting it made the SDK raise
                    # NullRunUnclassifiedRefusalError instead of
                    # reaching the budget arm this test pins.
                    "category": "budget",
                    "user_message": "raise the workflow budget",
                    "details": {"max_budget_cents": 100},
                },
            )
        )
        rt = make_runtime()
        with pytest.raises(NullRunBudgetError):
            rt.check_workflow_budget()
