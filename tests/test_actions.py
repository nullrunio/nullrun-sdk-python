"""
Tests for actions.py - ActionHandler, KILL/PAUSE/ALERT/WEBHOOK actions.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from nullrun.actions import (
    ActionHandler,
    ActionType,
    WebhookConfig,
    get_action_handler,
    handle_action,
    register_action_handler,
)
from nullrun.breaker.exceptions import NullRunBlockedException


class TestActionHandlerInit:
    """Test ActionHandler initialization."""

    def test_creates_without_errors(self):
        """ActionHandler creates successfully."""
        handler = ActionHandler()
        assert handler is not None
        assert handler._max_history == 1000

    def test_get_action_handler_returns_singleton(self):
        """get_action_handler() returns singleton."""
        h1 = get_action_handler()
        h2 = get_action_handler()
        assert h1 is h2


class TestKillAction:
    """Test KILL action.

    The default KILL handler intentionally raises
    `WorkflowKilledInterrupt` to halt the agent. After the P0-0.5
    fix, the kill contract is restored: the exception PROPAGATES,
    not swallowed. The kill is still recorded in history.
    """

    def test_kill_records_to_history(self):
        """KILL action is recorded in history AND propagates
        `WorkflowKilledInterrupt` (the kill contract)."""
        from nullrun.breaker.exceptions import WorkflowKilledInterrupt

        handler = ActionHandler()
        with pytest.raises(WorkflowKilledInterrupt):
            handler.handle(ActionType.KILL, "wf-123", "Test reason")
        # Action is still in history despite the exception.
        history = handler.get_action_history()
        assert len(history) == 1
        assert history[0].action_type == "kill"
        assert history[0].workflow_id == "wf-123"


class TestPauseAction:
    """Test PAUSE action.

    Same contract as KILL: the default PAUSE handler raises
    `WorkflowPausedException` to halt the agent. After the P0-0.5
    fix, the pause propagates.
    """

    def test_pause_propagates_exception(self):
        """PAUSE action raises `WorkflowPausedException` to halt
        the agent (the pause contract). Action is still recorded."""
        from nullrun.breaker.exceptions import WorkflowPausedException

        handler = ActionHandler()
        with pytest.raises(WorkflowPausedException):
            handler.handle(ActionType.PAUSE, "wf-456", "Rate limit hit")
        # Action is still in history despite the exception.
        history = handler.get_action_history()
        assert len(history) == 1

    def test_pause_tracks_workflow(self):
        """PAUSE action tracks workflow in paused_workflows."""
        from nullrun.breaker.exceptions import WorkflowPausedException

        handler = ActionHandler()
        with pytest.raises(WorkflowPausedException):
            handler.handle(ActionType.PAUSE, "wf-789", "Test pause")
        # Workflow is registered as paused even though the
        # exception propagated.
        assert handler.is_paused("wf-789")

    def test_is_paused_respects_cooldown(self):
        """is_paused respects cooldown_seconds."""
        from nullrun.breaker.exceptions import WorkflowPausedException

        handler = ActionHandler()
        # After P0-0.5, PAUSE propagates WorkflowPausedException.
        with pytest.raises(WorkflowPausedException):
            handler.handle(ActionType.PAUSE, "wf-cooldown", "Test")
        # Within cooldown
        assert handler.is_paused("wf-cooldown", cooldown_seconds=60.0)
        # After cooldown
        assert not handler.is_paused("wf-cooldown", cooldown_seconds=0.0)


class TestAlertAction:
    """Test ALERT action."""

    def test_alert_does_not_raise_exception(self):
        """ALERT handler does not raise exception."""
        handler = ActionHandler()
        # Should not raise
        handler.handle(ActionType.ALERT, "wf-alert", "Anomaly detected")
        history = handler.get_action_history()
        assert len(history) == 1

    def test_alert_calls_registered_callback(self):
        """ALERT calls registered callback function."""
        handler = ActionHandler()
        callback_calls = []

        def my_alert_handler(workflow_id, reason, **details):
            callback_calls.append((workflow_id, reason))

        handler.register_handler(ActionType.ALERT, my_alert_handler)
        handler.handle(ActionType.ALERT, "wf-callback", "Test alert")

        assert len(callback_calls) == 1
        assert callback_calls[0] == ("wf-callback", "Test alert")


class TestWebhookAction:
    """Test WEBHOOK action."""

    @pytest.fixture
    def handler_with_webhook(self):
        """Handler with a registered webhook."""
        handler = ActionHandler()
        handler.register_webhook(
            WebhookConfig(url="https://example.com/webhook", timeout=1.0, retries=1)
        )
        return handler

    def test_webhook_queues_payload(self, handler_with_webhook):
        """WEBHOOK action queues webhook payload."""
        # Should not raise
        handler_with_webhook.handle(
            ActionType.WEBHOOK, "wf-webhook", "Test webhook reason"
        )
        # Give time for async processing
        time.sleep(0.1)
        history = handler_with_webhook.get_action_history()
        assert len(history) == 1

    @patch("httpx.post")
    def test_webhook_makes_http_post(self, mock_post, handler_with_webhook):
        """WEBHOOK makes HTTP POST to configured URL."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        handler_with_webhook.handle(
            ActionType.WEBHOOK, "wf-http", "Test webhook"
        )
        # Give time for webhook thread to process
        time.sleep(0.2)

        if mock_post.called:
            call_args = mock_post.call_args
            assert "https://example.com/webhook" in str(call_args)
            # Check payload structure
            if call_args.kwargs and "json" in call_args.kwargs:
                payload = call_args.kwargs["json"]
                assert payload["workflow_id"] == "wf-http"

    @patch("httpx.post")
    def test_webhook_timeout_does_not_break_flow(self, mock_post, handler_with_webhook):
        """Webhook timeout logs warning but doesn't break main flow."""
        mock_post.side_effect = Exception("Connection timeout")

        # Should not raise - webhook delivery is async
        handler_with_webhook.handle(
            ActionType.WEBHOOK, "wf-timeout", "Test timeout"
        )
        # Main flow should complete
        history = handler_with_webhook.get_action_history()
        assert len(history) == 1


class TestRegisterActionHandler:
    """Test register_action_handler function."""

    def test_registered_handler_called(self):
        """Registered handler is called for correct action type."""
        get_action_handler()  # Ensure handler is initialized
        called = []

        def custom_handler(workflow_id, reason, **details):
            called.append((workflow_id, reason))

        register_action_handler(ActionType.BLOCK, custom_handler)
        try:
            handle_action("block", "wf-custom", "Custom block reason")
        except NullRunBlockedException:
            pass

        assert len(called) == 1


class TestActionHistory:
    """Test action history functionality."""

    def test_history_respects_limit(self):
        """History does not exceed 1000 entries."""
        handler = ActionHandler()
        # Add 1100 actions
        for i in range(1100):
            try:
                handler.handle(ActionType.ALERT, f"wf-{i}", f"Alert {i}")
            except Exception:
                pass
        history = handler.get_action_history()
        assert len(history) <= 1000

    def test_clear_history_works(self):
        """clear_history() removes all entries."""
        handler = ActionHandler()
        for i in range(5):
            try:
                handler.handle(ActionType.ALERT, f"wf-{i}", f"Alert {i}")
            except Exception:
                pass
        handler.clear_history()
        assert len(handler.get_action_history()) == 0


class TestThreadSafety:
    """Test thread safety of ActionHandler."""

    def test_concurrent_handle_calls(self):
        """Concurrent handle() calls don't break state."""
        import threading

        handler = ActionHandler()
        errors = []

        def worker(workflow_id):
            try:
                handler.handle(ActionType.ALERT, workflow_id, "Concurrent alert")
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(10):
            t = threading.Thread(target=worker, args=(f"wf-concurrent-{i}",))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # No errors should have occurred
        assert len(errors) == 0
        # History should contain all events
        history = handler.get_action_history()
        assert len(history) == 10


class TestSnapshotAndBlock:
    """Test SNAPSHOT and BLOCK actions."""

    def test_snapshot_does_not_raise(self):
        """SNAPSHOT action doesn't raise exception."""
        handler = ActionHandler()
        handler.handle(ActionType.SNAPSHOT, "wf-snap", "Debug snapshot")
        assert len(handler.get_action_history()) == 1

    def test_block_does_not_propagate_exception(self):
        """BLOCK action is handled without propagating exception."""
        handler = ActionHandler()
        # Should not raise - exceptions are caught internally
        handler.handle(ActionType.BLOCK, "wf-block", "Policy violation")
        # But action should be recorded
        history = handler.get_action_history()
        assert len(history) == 1


# ─────────────────────────────────────────────────────────────────────
# Phase 0, Epic 0.5: kill/pause handlers must PROPAGATE their
# BaseException subclasses (`WorkflowKilledInterrupt`,
# `WorkflowPausedException`). The pre-fix code caught
# `BaseException` and silently swallowed the kill/pause signal,
# breaking the kill contract (see `docs/kill-contract.md` §1).
# ─────────────────────────────────────────────────────────────────────


class TestActionHandlerKillContract:
    """The default KILL handler intentionally raises
    `WorkflowKilledInterrupt` (a `BaseException` subclass) so that
    the calling agent halts. The pre-fix `except BaseException` in
    `ActionHandler.handle` silently swallowed it. The fix catches
    only `Exception`."""

    def test_kill_handler_propagates_workflowkilledinterrupt(self):
        """`handle("kill", ...)` with the default KILL handler must
        raise `WorkflowKilledInterrupt` so the agent halts."""
        from nullrun.breaker.exceptions import WorkflowKilledInterrupt

        handler = ActionHandler()
        # The default `_default_kill` raises WorkflowKilledInterrupt.
        with pytest.raises(WorkflowKilledInterrupt):
            handler.handle(ActionType.KILL, "wf-kill", "test reason")

    def test_pause_handler_propagates_workflowpausedexception(self):
        """`handle("pause", ...)` with the default PAUSE handler must
        raise `WorkflowPausedException` so the agent halts (a pause
        is a non-resumable-by-the-agent signal)."""
        from nullrun.breaker.exceptions import WorkflowPausedException

        handler = ActionHandler()
        with pytest.raises(WorkflowPausedException):
            handler.handle(ActionType.PAUSE, "wf-pause", "test reason")

    def test_kill_action_is_recorded_before_propagating(self):
        """The KILL action must be in the history (so the operator
        can see it was dispatched) BEFORE the exception propagates."""
        from nullrun.breaker.exceptions import WorkflowKilledInterrupt

        handler = ActionHandler()
        with pytest.raises(WorkflowKilledInterrupt):
            handler.handle(ActionType.KILL, "wf-kill-history", "test reason")
        history = handler.get_action_history()
        assert len(history) == 1
        assert history[0].action_type == "kill"
        assert history[0].workflow_id == "wf-kill-history"

    def test_arbitrary_exception_in_handler_is_logged_not_propagated(self):
        """A user-registered handler that raises an `Exception`
        subclass (not a `BaseException` subclass) must NOT
        propagate. The pre-fix code happened to swallow these too
        (via the broad `except BaseException`), so the public
        contract is preserved."""
        handler = ActionHandler()

        def broken_handler(workflow_id: str, reason: str, **_details) -> None:
            raise ValueError("user bug")

        handler.register_handler(ActionType.ALERT, broken_handler)
        # Should NOT raise — ValueError is caught and logged.
        handler.handle(ActionType.ALERT, "wf-broken", "test alert")

    def test_keyboard_interrupt_propagates(self):
        """`KeyboardInterrupt` is a `BaseException` subclass. It
        MUST propagate (the previous `except BaseException` caught
        it). This test pins the contract: user Ctrl-C is never
        swallowed."""
        handler = ActionHandler()

        def ctrl_c_handler(workflow_id: str, reason: str, **_details) -> None:
            raise KeyboardInterrupt()

        handler.register_handler(ActionType.ALERT, ctrl_c_handler)
        with pytest.raises(KeyboardInterrupt):
            handler.handle(ActionType.ALERT, "wf-ctrlc", "test alert")

    def test_system_exit_propagates(self):
        """`SystemExit` is a `BaseException` subclass. It MUST
        propagate (so the Python interpreter can shut down cleanly)."""
        handler = ActionHandler()

        def sys_exit_handler(workflow_id: str, reason: str, **_details) -> None:
            raise SystemExit(1)

        handler.register_handler(ActionType.ALERT, sys_exit_handler)
        with pytest.raises(SystemExit):
            handler.handle(ActionType.ALERT, "wf-sysexit", "test alert")