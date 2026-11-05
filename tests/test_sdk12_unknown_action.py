"""SDK-12 — an unknown action type is REFUSED, not silently returned.

The audit entry, verbatim:

    **SDK-12 — an unknown action type is logged and returned.**
    *Location:* ``src/nullrun/actions.py`` (unknown action type ⇒ logged
    and returned; the code's own comment calls it fail-open).
    *Failure:* an action the SDK does not know is **not executed and not
    refused** — the caller believes it ran. ``except BaseException``
    also swallows ``KeyboardInterrupt``/``SystemExit``.
    *Verify:* an unknown action type must raise. *Fix:* raise; narrow
    the handler.

What these tests pin
--------------------
1. An unknown action type RAISES a typed error, so the caller can tell
   "it ran" from "it could not run".
2. The raise is NOT a block — B14's anti-DoS property survives (an
   unrecognised control-plane value must not stop the workflow).
3. The raise is NOT a transport error — ADR-126's two-step opt-in must
   not be able to convert it into "the body runs".
4. ``except BaseException`` is gone: KeyboardInterrupt / SystemExit
   reach the caller.
5. A handler's OWN refusal (kill / pause / block) is still swallowed
   and logged at INFO; a handler FAULT is logged at ERROR.
6. At the only production call site (``/track/batch``) the raise is
   reported as a control-plane integrity event, not as a "malformed
   action", and the rest of the batch still runs.
"""

from __future__ import annotations

import logging

import pytest
import respx
from httpx import Response

import nullrun.actions as _act
import nullrun.decorators as _dec
from nullrun.actions import ActionHandler, ActionType
from nullrun.breaker.exceptions import (
    NullRunBlockedException,
    NullRunTransportError,
    NullRunUnknownActionError,
)
from nullrun.observability import metrics
from nullrun.runtime import NullRunRuntime

BASE_URL = "https://api.test.nullrun.io"


# ---------------------------------------------------------------------------
# 1. The unknown action type raises
# ---------------------------------------------------------------------------


def test_an_unknown_action_type_raises():
    """The audit's Verify clause, verbatim: it must raise."""
    h = ActionHandler()
    with pytest.raises(NullRunUnknownActionError) as info:
        h.handle("totally_made_up_action", "wf-mystery", "test reason")
    assert "totally_made_up_action" in str(info.value)


def test_the_unknown_action_is_not_a_block():
    """B14's property survives: an unknown type must not stop the workflow.

    Pre-B14 the unknown type degraded to ``ActionType.BLOCK`` and the
    SDK became a DoS amplifier. The raise must therefore NOT be a
    ``NullRunBlockedException`` — an infrastructure error is the right
    shape: the action did not run, but the workflow is not blocked.
    """
    h = ActionHandler()
    with pytest.raises(NullRunUnknownActionError) as info:
        h.handle("weird", "wf-1", reason="x")
    assert not isinstance(info.value, NullRunBlockedException), (
        "the unknown-action refusal must not be a block — that was the "
        "DoS amplifier B14 removed. An unrecognised control-plane value "
        "must not stop every subsequent tool call in the workflow."
    )


def test_the_unknown_action_is_not_a_transport_error():
    """ADR-126's two-step opt-in opens TRANSPORT errors only.

    "The body runs" is not an acceptable answer to "the control plane
    sent an instruction this SDK cannot execute".
    """
    assert not issubclass(NullRunUnknownActionError, NullRunTransportError)


def test_the_unknown_action_invokes_no_handler():
    """No handler runs — the action is not executed."""
    h = ActionHandler()
    handler_mock = pytest.importorskip("unittest.mock")
    mock = handler_mock.MagicMock()
    h.register_handler(ActionType.BLOCK, mock)
    h.register_handler(ActionType.ALERT, mock)
    with pytest.raises(NullRunUnknownActionError):
        h.handle("nope", "wf-1", reason="x")
    mock.assert_not_called()


def test_the_unknown_action_is_recorded_forensically():
    """The history entry survives — the operator can correlate the log."""
    h = ActionHandler()
    with pytest.raises(NullRunUnknownActionError):
        h.handle("not_a_real_action", "wf-mystery", "real reason")
    history = h.get_action_history()
    assert len(history) == 1
    assert "unknown_action_type:not_a_real_action" in history[0].reason


def test_the_unknown_action_is_counted():
    """The counter is the operator's signal that server and SDK drifted."""
    before = metrics.runtime.unknown_action_total
    h = ActionHandler()
    with pytest.raises(NullRunUnknownActionError):
        h.handle("counted", "wf-1", reason="x")
    assert metrics.runtime.unknown_action_total == before + 1


def test_the_unknown_action_carries_its_context():
    """The error names the action and the workflow — it is the only handle."""
    h = ActionHandler()
    with pytest.raises(NullRunUnknownActionError) as info:
        h.handle("mystery", "wf-42", reason="because")
    exc = info.value
    assert exc.action == "mystery"
    assert exc.workflow_id == "wf-42"
    assert exc.reason == "because"
    assert exc.error_code == "NR-P005"


def test_a_known_action_after_an_unknown_one_still_runs():
    """One bad action must not poison the dispatcher."""
    h = ActionHandler()
    with pytest.raises(NullRunUnknownActionError):
        h.handle("malformed_first", "wf-mix", "first")
    h.handle(ActionType.KILL, "wf-mix", "second")
    history = h.get_action_history()
    assert len(history) == 2
    assert history[0].reason == "unknown_action_type:malformed_first"
    assert history[1].reason == "second"


# ---------------------------------------------------------------------------
# 2. The handler arm is narrowed
# ---------------------------------------------------------------------------


def test_keyboard_interrupt_is_not_swallowed():
    """``except BaseException`` is gone — a Ctrl-C must reach the caller."""
    h = ActionHandler()

    def boom(workflow_id, reason, **details):
        raise KeyboardInterrupt

    h.register_handler(ActionType.ALERT, boom)
    with pytest.raises(KeyboardInterrupt):
        h.handle("alert", "wf-1", reason="x")


def test_system_exit_is_not_swallowed():
    """Same for SystemExit."""
    h = ActionHandler()

    def boom(workflow_id, reason, **details):
        raise SystemExit(2)

    h.register_handler(ActionType.ALERT, boom)
    with pytest.raises(SystemExit):
        h.handle("alert", "wf-1", reason="x")


def test_the_handlers_own_refusal_is_swallowed_and_logged_at_info(caplog):
    """A kill is the action's OUTCOME, not a fault in the handler.

    Pre-SDK-12 the kill refusal was logged at ERROR, so every
    server-side kill raised a false alarm on the ERROR channel.
    """
    h = ActionHandler()
    with caplog.at_level(logging.INFO, logger="nullrun.actions"):
        h.handle("kill", "wf-1", reason="budget")  # must not raise
    assert any(r.levelno >= logging.ERROR for r in caplog.records) is False, (
        "a delivered kill must not be logged as an ERROR — it is the "
        "action working as designed"
    )
    assert any("kill" in r.getMessage() for r in caplog.records)


def test_a_handler_fault_is_still_logged_at_error(caplog):
    """A buggy custom handler stays on the ERROR channel."""
    h = ActionHandler()

    def boom(workflow_id, reason, **details):
        raise RuntimeError("oops")

    h.register_handler(ActionType.ALERT, boom)
    with caplog.at_level(logging.INFO, logger="nullrun.actions"):
        h.handle("alert", "wf-1", reason="x")  # must not raise
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_the_kill_is_still_recorded_in_history():
    """The swallow must not cost the audit entry."""
    h = ActionHandler()
    h.handle("kill", "wf-1", reason="x")
    assert any(e.action_type == "kill" for e in h.get_action_history())


# ---------------------------------------------------------------------------
# 3. The public helper
# ---------------------------------------------------------------------------


def test_the_public_helper_raises_for_an_unknown_type():
    """``nullrun.handle_action`` is the public door — it must not lie."""
    import nullrun

    with pytest.raises(NullRunUnknownActionError):
        nullrun.handle_action("bogus", "wf-1", "x")


# ---------------------------------------------------------------------------
# 4. The only production call site: /track/batch
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_api():
    with respx.mock:
        respx.post(f"{BASE_URL}/api/v1/auth/verify").mock(
            return_value=Response(
                200,
                json={
                    "organization_id": "ws-test",
                    "workflow_id": "00000000-0000-0000-0000-000000000001",
                    "plan": "pro",
                    "features": [],
                    "limits": {"max_cost_cents": 10000},
                },
            )
        )
        yield


def _runtime(api_key: str = "test-key"):
    NullRunRuntime._instance = None
    _dec._runtime = None
    _act._action_handler = None
    rt = NullRunRuntime(
        api_key=api_key,
        api_url=BASE_URL,
        debug=True,
        polling=False,
    )
    NullRunRuntime._instance = rt
    _dec._runtime = rt
    return rt


def _batch_response(actions):
    return Response(
        200,
        json={
            "processed": 1,
            "actions": actions,
            "messages": [],
            "snapshots": [],
            "accepted_event_ids": ["e1"],
            "rejected_count": 0,
            "rejection_details": [],
        },
    )


def _send_one_batch(rt):
    return rt._transport._send_batch_with_retry_info(
        batch=[
            {
                "event_type": "llm_call",
                "workflow_id": "wf-1",
                "model": "gpt-4",
                "tokens": 100,
                "cost_cents": 1,
            }
        ]
    )


def test_an_unknown_action_in_a_batch_is_reported_as_an_integrity_event(
    mock_api, caplog
):
    """Not "skipped malformed action" — that mislabelling is the defect.

    The action was not executed. Filing that under "malformed payload"
    hides a server/SDK version mismatch behind a WARNING.

    The assertion is on the TRANSPORT's own log line, at ANY level: the
    generic arm's message must not appear for this action at all, and
    the transport's integrity-event line must appear at ERROR. (An
    earlier version of this test asserted only "some ERROR mentions the
    action type" — the dispatcher's own ERROR log satisfies that, so
    the test passed even with the transport fix reverted. That was a
    vacuous pass; it is pinned by the mutation in the SDK-12 ADR.)
    """
    rt = _runtime()
    route = respx.post(f"{BASE_URL}/api/v1/track/batch").mock(
        return_value=_batch_response(
            [{"type": "throttle", "workflow_id": "wf-1", "reason": "x"}]
        )
    )
    with caplog.at_level(logging.INFO):
        _send_one_batch(rt)

    assert route.called
    messages = [r.getMessage() for r in caplog.records]
    assert not any("Skipping malformed action" in m for m in messages), (
        "a control-plane integrity event must not be filed as a "
        f"malformed payload. Log lines: {messages}"
    )
    transport_errors = [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.ERROR
        and "Control plane sent an action type" in r.getMessage()
    ]
    assert transport_errors, (
        "the /track/batch call site must report the unknown action as an "
        f"integrity event at ERROR. Log lines: {messages}"
    )
    assert any("throttle" in m for m in transport_errors)


def test_one_unknown_action_does_not_abort_the_rest_of_the_batch(mock_api):
    """The loop continues: the next action is still dispatched."""
    rt = _runtime()
    seen: list[tuple[str, str]] = []

    def alert_handler(workflow_id, reason, **details):
        seen.append((workflow_id, reason))

    _act.get_action_handler().register_handler(ActionType.ALERT, alert_handler)

    respx.post(f"{BASE_URL}/api/v1/track/batch").mock(
        return_value=_batch_response(
            [
                {"type": "throttle", "workflow_id": "wf-1", "reason": "x"},
                {"type": "alert", "workflow_id": "wf-2", "reason": "y"},
            ]
        )
    )
    _send_one_batch(rt)

    assert seen == [("wf-2", "y")], (
        "the known action after the unknown one must still be dispatched"
    )


def test_a_malformed_action_still_logs_the_warning(mock_api, caplog):
    """The generic arm keeps its own label — only the integrity event moved."""
    rt = _runtime()
    respx.post(f"{BASE_URL}/api/v1/track/batch").mock(
        return_value=_batch_response(["not-a-dict"])
    )
    with caplog.at_level(logging.INFO):
        _send_one_batch(rt)
    assert any(
        "non-dict action" in r.getMessage() for r in caplog.records
    ), "the malformed-payload arm must keep its own signal"


def test_the_batch_still_succeeds_with_an_unknown_action(mock_api):
    """The cost events were accepted — the batch must not be reported as failed."""
    rt = _runtime()
    respx.post(f"{BASE_URL}/api/v1/track/batch").mock(
        return_value=_batch_response(
            [{"type": "throttle", "workflow_id": "wf-1", "reason": "x"}]
        )
    )
    result = _send_one_batch(rt)
    assert result.accepted_event_ids == ["e1"]
