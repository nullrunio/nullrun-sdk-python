"""Regression tests for NullRunBlockedException attribute surface.

Covers FIX-5: `tool_name` was previously absorbed into `**details` and
inaccessible as a first-class attribute, so cookbook examples that read
`exc.tool_name` raised `AttributeError`.

The fix exposed `tool_name` as a kwarg on `NullRunBlockedException.__init__`
and stored it on `self.tool_name`. Subclasses (`LoopDetectedException`,
`RetryStormException`, `RateLimitExceededException`) flow through the
new parameter because they call `super().__init__(...)` with it.

Backwards compat: `tool_name` is optional and defaults to `None`, so
all existing raise sites that do not pass it still work.
"""
from nullrun.breaker.exceptions import (
    LoopDetectedException,
    NullRunBlockedException,
    RateLimitExceededException,
    RetryStormException,
)


def test_tool_name_kwarg_exposed_as_attribute():
    exc = NullRunBlockedException(
        workflow_id="wf-1",
        reason="blocked by policy",
        tool_name="charge_card",
    )
    assert exc.tool_name == "charge_card"


def test_tool_name_defaults_to_none():
    exc = NullRunBlockedException(workflow_id="wf-2", reason="blocked")
    assert exc.tool_name is None


def test_tool_name_does_not_leak_into_details():
    """`tool_name` is a first-class attribute, NOT a detail kwarg.

    Before the fix, the kwarg fell through `**details` and the attribute
    was unreachable. Now it lives on `self.tool_name` and must not also
    appear under `details` (which is meant for the free-form payload
    forwarded by callers).
    """
    exc = NullRunBlockedException(
        workflow_id="wf-3",
        reason="blocked",
        tool_name="refund_payment",
        extra_field="kept-in-details",
    )
    assert exc.tool_name == "refund_payment"
    assert "tool_name" not in exc.details
    assert exc.details == {"extra_field": "kept-in-details"}


def test_loop_detected_subclass_inherits_tool_name():
    """LoopDetectedException passes tool_name via super().__init__."""
    exc = LoopDetectedException(
        workflow_id="wf-loop",
        tool_name="search_web",
        count=7,
    )
    assert exc.tool_name == "search_web"
    assert exc.action == "kill"
    assert exc.details == {"count": 7}


def test_retry_storm_subclass_without_tool_name():
    """Subclasses that do not pass tool_name get tool_name=None."""
    exc = RetryStormException(workflow_id="wf-retry", count=99)
    assert exc.tool_name is None
    assert exc.action == "kill"
    assert exc.details == {"count": 99}


def test_rate_limit_subclass_without_tool_name():
    exc = RateLimitExceededException(
        workflow_id="wf-rl", rate=120.0, limit=60.0
    )
    assert exc.tool_name is None
    assert exc.action == "pause"
    assert exc.details == {"rate": 120.0, "limit": 60.0}


def test_message_includes_tool_suffix_when_present():
    exc = NullRunBlockedException(
        workflow_id="wf-msg",
        reason="policy denied",
        tool_name="delete_account",
    )
    assert "tool=delete_account" in str(exc)


def test_message_omits_tool_suffix_when_absent():
    exc = NullRunBlockedException(workflow_id="wf-msg2", reason="policy denied")
    assert "tool=" not in str(exc)


def test_action_kwarg_still_works_alongside_tool_name():
    """`action` (block/kill/pause) and `tool_name` are independent kwargs."""
    exc = NullRunBlockedException(
        workflow_id="wf-action",
        reason="killed by dashboard",
        action="kill",
        tool_name="send_email",
    )
    assert exc.action == "kill"
    assert exc.tool_name == "send_email"
    assert "(action=kill, tool=send_email" in str(exc)
