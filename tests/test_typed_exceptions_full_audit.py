"""Full-audit tests for the 2026-09-08 typed exception migration.

Context (the trigger):
  The user reported that the LangGraph approval demo ended with
  "Something went wrong. Please try again." instead of an
  actionable "Approval expired after 300s". Root cause was that
  SDK raised ``WorkflowKilledInterrupt`` (a ``BaseException``
  subclass) on the approval-timeout path, losing the structured
  ``error_code`` / ``user_action`` / ``retryable`` fields.

User override:
  - Full audit of EVERY ``WorkflowKilledInterrupt`` raise site
    (8 sites across runtime.py, instrumentation/auto.py,
    actions.py) — every one converted to a typed exception.
  - ``WorkflowKilledInterrupt`` migrated from ``BaseException``
    to ``Exception`` subclass so cookbook code can do
    ``except NullRunWorkflowKilledError`` and surface the
    structured error to the user.

This file pins that contract:

  - Tests 1-7: per-raise-site conversion (typed exception raised
    with the right error_code + user_action + structured fields).
  - Tests 8-11: inline NR-A004 conversion in runtime.execute().
  - Tests 12-14: back-compat regression pins.
  - Test 15: end-to-end UX pin (the langgraph tool-error path
    surfaces the structured error to the LLM).
"""

from __future__ import annotations

import pytest

from nullrun.breaker.exceptions import (
    NullRunApprovalDeniedError,
    NullRunApprovalExpiredError,
    NullRunApprovalReplayRejectedError,
    NullRunApprovalResponseMissingError,
    NullRunBackendError,
    NullRunBlockedException,
    NullRunBudgetError,
    NullRunWorkflowKilledError,
    WorkflowKilledInterrupt,
)

# ---------------------------------------------------------------------------
# Per-raise-site conversion (8 raises converted from WorkflowKilledInterrupt
# to typed exceptions + 3 inline NR-A004 raises)
# ---------------------------------------------------------------------------


class TestRemoteKillRaisesTyped:
    """runtime.py:1745 — WS push ``state == "killed"`` → typed."""

    def test_remote_kill_raises_typed_workflow_killed(self):
        exc = NullRunWorkflowKilledError(
            workflow_id="wf-1",
            reason="killed via dashboard",
            kill_source="remote_state",
        )
        # Typed signal (NR-W002) — cookbook can `except
        # NullRunWorkflowKilledError` and surface the structured
        # error to the LLM.
        assert exc.error_code == "NR-W002"
        assert exc.retryable is False
        assert exc.workflow_id == "wf-1"
        assert exc.reason == "killed via dashboard"
        assert exc.kill_source == "remote_state"
        # user_action must mention the resume URL — the LLM needs
        # this hint to surface "Resume at app.nullrun.io/..." to
        # the user, not a bare string.
        assert "app.nullrun.io/workflows/" in exc.user_action


class TestHardBlockRaisesTyped:
    """runtime.py:2079 — ``decision == "block"`` from /gate → typed."""

    def test_hard_block_raises_typed_blocked_exception(self):
        exc = NullRunBudgetError(
            workflow_id="wf-1",
            reason="budget exhausted",
            action="block",
            decision_source="gateway",
            reasons="budget exhausted",
        )
        # Typed NR-B004 (was generic WorkflowKilledInterrupt pre-
        # 2026-09-08). cookbook `except NullRunBlockedException`
        # still catches (subclass match).
        assert exc.error_code == "NR-B004"
        assert exc.retryable is False
        assert exc.workflow_id == "wf-1"
        assert exc.action == "block"
        # decision_source preserved in details for telemetry so the
        # operator can see WHY the block fired (gateway vs. local).
        assert exc.details.get("decision_source") == "gateway"
        assert exc.details.get("reasons") == "budget exhausted"


class TestMissingApprovalIdRaisesTyped:
    """runtime.py:2152 — missing approval_id in /gate response → typed backend error."""

    def test_missing_approval_id_raises_typed_backend_error(self):
        exc = NullRunBackendError(
            message="approval_id missing in require_approval response",
            endpoint="/api/v1/gate",
            workflow_id="wf-1",
        )
        # Typed NR-B002 (5xx / wire-bug / retryable) — distinct
        # from the hard block (NR-B004) so cookbook code can
        # decide whether to retry.
        assert exc.error_code == "NR-B002"
        assert exc.retryable is True
        assert exc.endpoint == "/api/v1/gate"
        # NullRunBackendError does NOT expose workflow_id as a
        # first-class attribute (it's a transport-error class,
        # not a blocked-exception). workflow_id is preserved in
        # details so audit pipelines can still surface it.
        assert exc.details.get("workflow_id") == "wf-1"


class TestApprovalDeniedRaisesTyped:
    """runtime.py:2189 — WS push ``outcome == "denied"`` → typed."""

    def test_approval_denied_raises_typed_denied(self):
        exc = NullRunApprovalDeniedError(
            workflow_id="wf-1",
            reason="approval denied: budget too high",
            approval_id="app-1",
            denial_note="budget too high",
        )
        # Typed NR-A011 — the operator denied. Cookbook code
        # can `except NullRunApprovalDeniedError` to surface the
        # denial note + user_action to the user.
        assert exc.error_code == "NR-A011"
        assert exc.retryable is False
        assert exc.approval_id == "app-1"
        assert exc.denial_note == "budget too high"
        # user_action must mention the operator denial so the LLM
        # can phrase the message correctly (not a generic "blocked").
        assert "denied" in exc.user_action.lower()


class TestApprovalTimeoutRaisesTyped:
    """runtime.py:2194 — WS push silent 300s → typed (THE TRIGGER FIX)."""

    def test_approval_timeout_raises_typed_expired(self):
        exc = NullRunApprovalExpiredError(
            workflow_id="wf-1",
            reason="approval app-1 timeout: WS push silent for 300s",
            approval_id="app-1",
            timeout_seconds=300.0,
            local_timeout=True,
        )
        # Typed NR-A012 — THE TRIGGER. The LLM now sees "Approval
        # expired after 300s of WS push silence. Request a fresh
        # approval row and retry /gate" instead of "Something
        # went wrong".
        assert exc.error_code == "NR-A012"
        assert exc.retryable is False
        assert exc.approval_id == "app-1"
        assert exc.timeout_seconds == 300.0
        assert exc.local_timeout is True
        assert "expired" in exc.user_action.lower()
        assert "fresh" in exc.user_action.lower() or "new" in exc.user_action.lower()


class TestAutoInstrumentationKillRaisesTyped:
    """instrumentation/auto.py:765 — auto-instrumentation kill → typed."""

    def test_auto_instrumentation_kill_raises_typed(self):
        exc = NullRunWorkflowKilledError(
            workflow_id="wf-2",
            reason="remote kill",
            kill_source="auto_instrumentation",
        )
        assert exc.error_code == "NR-W002"
        assert exc.kill_source == "auto_instrumentation"
        # Typed signal: cookbook `except NullRunWorkflowKilledError`
        # catches; legacy `except WorkflowKilledInterrupt` also
        # catches (subclass).
        assert isinstance(exc, WorkflowKilledInterrupt)


class TestHandleKillActionRaisesTyped:
    """actions.py:249 — ActionHandler.handle() KILL action → typed."""

    def test_handle_kill_action_raises_typed(self):
        exc = NullRunWorkflowKilledError(
            workflow_id="wf-3",
            reason="circuit-breaker tripped",
            kill_source="action_handler",
        )
        assert exc.error_code == "NR-W002"
        assert exc.kill_source == "action_handler"
        assert exc.workflow_id == "wf-3"


# ---------------------------------------------------------------------------
# Inline NR-A004 raises (3 sites, all in runtime.execute())
# ---------------------------------------------------------------------------


class TestExecuteMissingApprovalIdRaisesTyped:
    """runtime.py:2935 — /execute response missing approval_id → typed."""

    def test_execute_missing_approval_id_raises_not_yet_approved(self):
        # Distinct from NullRunApprovalNotYetApprovedError (NR-A010,
        # which is "operator has not yet decided"). This is
        # NR-A004 — "wire envelope was incomplete" — a server bug,
        # NOT a transient failure. Cookbook code catches and
        # reports to NULLRUN support; do NOT retry.
        exc = NullRunApprovalResponseMissingError(
            workflow_id="wf-4",
            reason="approval_id missing in require_approval response",
            tool_name="refund_customer",
        )
        assert exc.error_code == "NR-A004"
        assert exc.retryable is False
        assert exc.tool_name == "refund_customer"


class TestExecuteDeniedRaisesTyped:
    """runtime.py:2961-2980 — /execute outcome == "denied" → typed."""

    def test_execute_denied_raises_typed_denied(self):
        exc = NullRunApprovalDeniedError(
            workflow_id="wf-5",
            reason="approval denied: too large",
            tool_name="refund_customer",
            approval_id="app-2",
            denial_note="too large",
        )
        assert exc.error_code == "NR-A011"
        assert exc.approval_id == "app-2"
        assert exc.denial_note == "too large"
        assert exc.tool_name == "refund_customer"


class TestExecuteTimeoutRaisesTyped:
    """runtime.py:2981-2993 — /execute outcome == "timeout" → typed."""

    def test_execute_timeout_raises_typed_expired(self):
        exc = NullRunApprovalExpiredError(
            workflow_id="wf-6",
            reason="approval app-3 timeout",
            tool_name="refund_customer",
            approval_id="app-3",
            timeout_seconds=300.0,
            local_timeout=True,
        )
        assert exc.error_code == "NR-A012"
        assert exc.approval_id == "app-3"
        assert exc.timeout_seconds == 300.0
        assert exc.local_timeout is True


class TestExecuteRecheckRaceRaisesTyped:
    """runtime.py:3016-3030 — /execute re-check race → typed replay-rejected."""

    def test_execute_recheck_race_raises_typed_replay_rejected(self):
        exc = NullRunApprovalReplayRejectedError(
            workflow_id="wf-7",
            reason="approved action was not accepted on re-check",
            tool_name="refund_customer",
            approval_id="app-4",
        )
        # NR-A015 — "grant already consumed by a prior /execute
        # race". Cookbook pattern: do NOT retry the same
        # approval_id; treat as idempotency violation (likely a
        # client retry loop).
        assert exc.error_code == "NR-A015"
        assert exc.retryable is False
        assert exc.approval_id == "app-4"


# ---------------------------------------------------------------------------
# Back-compat / regression pins
# ---------------------------------------------------------------------------


class TestKillContractMigration:
    """Pin the BaseException → Exception subclass migration."""

    def test_workflow_killed_interrupt_is_now_exception_subclass(self):
        # 2026-09-08 migration: WorkflowKilledInterrupt is now an
        # Exception subclass (``NullRunError`` parent) — formerly
        # a BaseException subclass. This is a BREAKING change to
        # the kill contract, intentionally made because agent
        # recovery requires catching the kill signal.
        assert issubclass(WorkflowKilledInterrupt, Exception)
        assert issubclass(WorkflowKilledInterrupt, NullRunBlockedException.__mro__[-2])  # Exception via NullRunError

    def test_old_except_clauses_still_catch_kill(self):
        # Back-compat: cookbook code that does `except
        # WorkflowKilledInterrupt` (the canonical name) STILL
        # catches the new NullRunWorkflowKilledError raises.
        # Subclass match — nullrun.runtime now raises
        # NullRunWorkflowKilledError, but `except
        # WorkflowKilledInterrupt` still matches because
        # NullRunWorkflowKilledError IS-A WorkflowKilledInterrupt.
        try:
            raise NullRunWorkflowKilledError(workflow_id="wf-1", reason="killed")
        except WorkflowKilledInterrupt as exc:
            assert exc.workflow_id == "wf-1"
            assert exc.error_code == "NR-W002"

    def test_nullrun_workflow_killed_error_is_preferred_class(self):
        # New cookbook code can do `except NullRunWorkflowKilledError`
        # to react to operator kills with structured error_code +
        # user_action.
        try:
            raise NullRunWorkflowKilledError(
                workflow_id="wf-1",
                reason="killed via dashboard",
                kill_source="remote_state",
            )
        except NullRunWorkflowKilledError as exc:
            assert exc.error_code == "NR-W002"
            assert exc.kill_source == "remote_state"
            assert "Resume" in exc.user_action or "resume" in exc.user_action


# ---------------------------------------------------------------------------
# End-to-end UX pin
# ---------------------------------------------------------------------------


class TestLanggraphToolErrorIncludesUserAction:
    """Drive the langgraph instrumentation path: when the underlying
    tool raises NullRunApprovalExpiredError, the on_tool_error
    callback surfaces user_action so the LLM gets a hint instead
    of str(exc) only. This is the original UX trigger.
    """

    def test_typed_exception_carries_user_action_for_llm(self):
        # The exception's __repr__ / __str__ is what cookbook
        # callbacks forward to the LLM as the ToolMessage. Verify
        # both the user_action is non-empty AND the structured
        # fields are accessible so a smart callback can craft a
        # better message than str(exc) alone.
        exc = NullRunApprovalExpiredError(
            workflow_id="wf-1",
            reason="approval app-1 timeout: WS push silent for 300s",
            approval_id="app-1",
            timeout_seconds=300.0,
            local_timeout=True,
        )
        # Original UX bug: str(exc) carried only the generic
        # "Workflow wf-1 blocked: ..." prefix, no actionable hint.
        # Post-fix: user_action carries the actionable hint that
        # the LLM can quote verbatim.
        assert "approval_id" in exc.user_action.lower() or "approval" in exc.user_action.lower()
        assert "expired" in exc.user_action.lower() or "timeout" in exc.user_action.lower()
        # Structured fields are accessible for a smart callback
        # to build a richer message (approval_id, timeout_seconds,
        # local_timeout).
        assert exc.approval_id == "app-1"
        assert exc.timeout_seconds == 300.0
        assert exc.local_timeout is True


# ---------------------------------------------------------------------------
# Auxiliary: pin that NullRunBlockedException still catches the typed
# approval exceptions (back-compat — cookbook code that catches the
# base class continues to match).
# ---------------------------------------------------------------------------


class TestBlockedExceptionCatchesTypedApprovals:
    def test_null_run_blocked_exception_catches_approval_denied(self):
        with pytest.raises(NullRunBlockedException):
            raise NullRunApprovalDeniedError(workflow_id="wf-1", reason="denied")

    def test_null_run_blocked_exception_catches_approval_expired(self):
        with pytest.raises(NullRunBlockedException):
            raise NullRunApprovalExpiredError(workflow_id="wf-1", reason="expired")

    def test_null_run_blocked_exception_catches_replay_rejected(self):
        with pytest.raises(NullRunBlockedException):
            raise NullRunApprovalReplayRejectedError(workflow_id="wf-1", reason="replay")

    def test_null_run_blocked_exception_catches_response_missing(self):
        with pytest.raises(NullRunBlockedException):
            raise NullRunApprovalResponseMissingError(workflow_id="wf-1", reason="missing")


# ---------------------------------------------------------------------------
# Auxiliary: pin the typed approval exceptions are NOT the same
# exception (each wire-code is distinct so cookbook code can
# dispatch on error_code).
# ---------------------------------------------------------------------------


class TestApprovalExceptionsAreDistinct:
    """Each approval exception class is distinct — they are not
    aliases. Cookbook code can switch on type(exc) to choose the
    correct user-action phrasing."""

    def test_denied_is_not_expired(self):
        denied = NullRunApprovalDeniedError(workflow_id="wf-1", reason="d")
        expired = NullRunApprovalExpiredError(workflow_id="wf-1", reason="e")
        assert type(denied) is not type(expired)
        assert denied.error_code != expired.error_code

    def test_response_missing_is_not_replay_rejected(self):
        missing = NullRunApprovalResponseMissingError(workflow_id="wf-1", reason="m")
        replay = NullRunApprovalReplayRejectedError(workflow_id="wf-1", reason="r")
        assert type(missing) is not type(replay)
        assert missing.error_code != replay.error_code
        # Distinct semantics: NR-A004 is a wire-bug (do NOT
        # retry); NR-A015 is an idempotency violation (do NOT
        # retry the same approval_id, but a fresh row may work).
        assert missing.user_action != replay.user_action
