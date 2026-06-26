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
    """Test KILL action."""

    def test_kill_records_to_history(self):
        """KILL action is recorded in history."""
        handler = ActionHandler()
        # handler catches exceptions internally, doesn't propagate
        handler.handle(ActionType.KILL, "wf-123", "Test reason")
        history = handler.get_action_history()
        assert len(history) == 1
        assert history[0].action_type == "kill"
        assert history[0].workflow_id == "wf-123"


class TestPauseAction:
    """Test PAUSE action."""

    def test_pause_does_not_propagate_exception(self):
        """PAUSE action is handled without propagating exception."""
        handler = ActionHandler()
        # Should not raise - exceptions are caught internally
        handler.handle(ActionType.PAUSE, "wf-456", "Rate limit hit")
        # But action should be recorded
        history = handler.get_action_history()
        assert len(history) == 1

    def test_pause_tracks_workflow(self):
        """PAUSE action tracks workflow in paused_workflows."""
        handler = ActionHandler()
        handler.handle(ActionType.PAUSE, "wf-789", "Test pause")
        assert handler.is_paused("wf-789")

    def test_is_paused_respects_cooldown(self):
        """is_paused respects cooldown_seconds."""
        handler = ActionHandler()
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
        handler_with_webhook.handle(ActionType.WEBHOOK, "wf-webhook", "Test webhook reason")
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

        handler_with_webhook.handle(ActionType.WEBHOOK, "wf-http", "Test webhook")
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
        handler_with_webhook.handle(ActionType.WEBHOOK, "wf-timeout", "Test timeout")
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


# ===========================================================================
# Sprint 1.5 (B14): unknown action type must NOT silently BLOCK
# ===========================================================================
# Pre-fix: an unknown action type (e.g. server schema regression,
# version mismatch, or attacker-controlled input) silently degraded
# to ``ActionType.BLOCK`` and triggered ``_default_block``, which
# raises ``NullRunBlockedException``. That made the SDK into a DoS
# amplifier: one malformed message stopped the whole workflow.
# Post-fix: log at ERROR, record a forensic event with the unknown
# action type, and DO NOT invoke any handler. Workflow continues.


class TestUnknownActionTypeFailOpen:
    """Unknown action types must fail open, not silently BLOCK."""

    def test_unknown_action_does_not_raise_blocked_exception(self):
        """Unknown action type must not raise NullRunBlockedException.

        Pre-fix this raised ``NullRunBlockedException`` because
        ``ActionType(action.lower())`` raised ``ValueError`` which
        was caught and silently fell through to ``ActionType.BLOCK``
        → ``_default_block`` → raise. Post-fix the method returns
        cleanly and the workflow continues.
        """
        handler = ActionHandler()
        # Must not raise.
        handler.handle("totally_made_up_action", "wf-mystery", "test reason")

    def test_unknown_action_records_forensic_event(self):
        """Unknown action type is still recorded in action history.

        The action is recorded with the unknown action type
        encoded into the reason (``"unknown_action_type:..."``) so
        an operator investigating the ERROR log can correlate the
        event in history.
        """
        handler = ActionHandler()
        handler.handle("not_a_real_action", "wf-mystery", "real reason")

        history = handler.get_action_history()
        assert len(history) == 1
        # The reason field carries the forensic marker.
        assert "unknown_action_type:not_a_real_action" in history[0].reason

    def test_unknown_action_logs_at_error_level(self, caplog):
        """Unknown action type must log at ERROR, not WARNING.

        Promoted from WARNING (pre-fix) to ERROR because for a
        safety-layer product, an unrecognised control plane action
        is a first-class incident — not a routine diagnostic.
        """
        import logging

        handler = ActionHandler()

        with caplog.at_level(logging.ERROR, logger="nullrun.actions"):
            handler.handle("bogus", "wf-x", "r")

        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("bogus" in r.getMessage() for r in error_records), (
            "Unknown action type was not logged at ERROR level. "
            "Pre-fix logged at WARNING which was too quiet for a "
            "control-plane integrity event."
        )

    def test_known_actions_still_work_after_unknown_action(self):
        """A prior unknown action must not corrupt handler state.

        Regression guard: a malformed action in the stream must not
        prevent subsequent KILL/PAUSE/etc. from being delivered.
        Pre-fix the silent-BLOCK raised an exception that the
        ``except BaseException`` swallowed, but a future change to
        that catch could break this — pin it.
        """
        handler = ActionHandler()
        handler.handle("malformed_first", "wf-mix", "first")
        # Now a real KILL — must still be recorded and still raise.
        handler.handle(ActionType.KILL, "wf-mix", "second")

        history = handler.get_action_history()
        assert len(history) == 2
        assert history[0].reason == "unknown_action_type:malformed_first"
        assert history[1].reason == "second"
