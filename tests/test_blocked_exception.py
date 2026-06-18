"""Regression tests for NullRunBlockedException attribute surface.

Covers FIX-5: `tool_name` was previously absorbed into `**details` and
inaccessible as a first-class attribute, so cookbook examples that read
`exc.tool_name` raised `AttributeError`.

The fix exposed `tool_name` as a kwarg on `NullRunBlockedException.__init__`
and stored it on `self.tool_name`.

Backwards compat: `tool_name` is optional and defaults to `None`, so
all existing raise sites that do not pass it still work.

Sprint 2.2: the previously-tested subclasses ``LoopDetectedException``,
``RetryStormException``, and ``RateLimitExceededException`` were
removed because they had no in-tree callers. The base-class
attribute surface tests below still pin the contract for any future
subclass.
"""
from nullrun.breaker.exceptions import NullRunBlockedException


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
