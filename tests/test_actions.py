"""
Tests for actions.py - ActionHandler, KILL/PAUSE/ALERT/WEBHOOK actions.
"""

from __future__ import annotations

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
        # After cooldown. ``time.sleep(0.01)`` before the post-cooldown
        # check guarantees ``time.time() - paused_at > 0.0`` regardless
        # of the platform's time.time() rounding; the pre-fix code used
        # ``cooldown_seconds=0.0`` directly which was a race against the
        # 1-second time.time() resolution on Windows / WSL1 and produced
        # ``elapsed == 0.0`` -> ``elapsed > cooldown`` False -> the
        # workflow stays "paused" forever. Tracked as the pre-existing
        # flake in 0.13.7 changelog; closed here.
        time.sleep(0.01)
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
# B14: unknown action type must NOT silently BLOCK
# ===========================================================================
# Pre-fix: an unknown action type (e.g. server schema regression
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
        ``ActionType(action.lower )`` raised ``ValueError`` which
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


# ─── actions context + init ────────────────────────────────────
"""
Branch-coverage tests for ``nullrun.actions``, ``nullrun.context``
``nullrun.__init__``, and the WorkflowKilledException deprecation
warning. Together these close the last 1-2 % lines that no other
test file exercises.
"""

import threading
import time
import warnings
from unittest.mock import MagicMock

import pytest

import nullrun
from nullrun.actions import (
    ActionEvent,
    ActionHandler,
    ActionType,
    WebhookConfig,
    handle_action,
    register_action_handler,
)
from nullrun.breaker.exceptions import (
    NullRunBlockedException,
    WorkflowKilledException,
    WorkflowKilledInterrupt,
)

# ─── ActionHandler ──────────────────────────────────────────────────


def test_register_handler_replaces_default():
    h = ActionHandler()
    sentinel = MagicMock()
    h.register_handler(ActionType.KILL, sentinel)
    assert h._handlers[ActionType.KILL] is sentinel


def test_register_webhook_adds_to_list():
    h = ActionHandler()
    cfg = WebhookConfig(url="https://example.com/hook")
    h.register_webhook(cfg)
    assert cfg in h._webhooks


def test_remove_webhook_removes_by_url():
    h = ActionHandler()
    h.register_webhook(WebhookConfig(url="https://a"))
    h.register_webhook(WebhookConfig(url="https://b"))
    h.remove_webhook("https://a")
    urls = [w.url for w in h._webhooks]
    assert urls == ["https://b"]


def test_remove_webhook_unknown_url_no_op():
    h = ActionHandler()
    h.remove_webhook("https://never-added")  # must not raise


def test_get_action_history_returns_slice():
    h = ActionHandler()
    for _ in range(5):
        h._record_action(ActionType.KILL, "wf", "x", {})
    recent = h.get_action_history(limit=3)
    assert len(recent) == 3


def test_clear_history_empties_list():
    h = ActionHandler()
    h._record_action(ActionType.KILL, "wf", "x", {})
    h.clear_history()
    assert h._action_history == []


def test_handle_unknown_action_does_not_invoke_handler():
    """B14: unknown action logs ERROR + records BLOCK but
    does NOT invoke any handler (fail-open). Pre-fix this degraded
    to BLOCK → DoS amplifier.
    """
    h = ActionHandler()
    handler_mock = MagicMock()
    h.register_handler(ActionType.BLOCK, handler_mock)
    # ``"weird"`` is not in ActionType — should fail-open.
    h.handle("weird", "wf-1", reason="x")
    handler_mock.assert_not_called()


def test_handle_unknown_action_records_block_event(caplog):
    """Unknown action records a BLOCK event for forensic visibility."""
    import logging

    h = ActionHandler()
    with caplog.at_level(logging.ERROR, logger="nullrun.actions"):
        h.handle("unknown_action_type", "wf-1", reason="x")
    history = h.get_action_history()
    assert any(e.action_type == "block" for e in history)


def test_handle_known_action_invokes_handler():
    h = ActionHandler()
    handler_mock = MagicMock()
    h.register_handler(ActionType.KILL, handler_mock)
    h.handle("kill", "wf-1", reason="budget")
    handler_mock.assert_called_once()


def test_handle_action_lowercases_input():
    """``handle("KILL", ...)`` matches ActionType.KILL after .lower()."""
    h = ActionHandler()
    handler_mock = MagicMock()
    h.register_handler(ActionType.KILL, handler_mock)
    h.handle("KILL", "wf-1", reason="x")
    handler_mock.assert_called_once()


def test_handle_kill_does_not_propagate_killed_interrupt():
    """``WorkflowKilledInterrupt`` from the handler is SWALLOWED by the
    dispatch loop (BaseException caught and logged). The kill signal
    has already been recorded in history by the time the dispatch
    wraps the handler call — re-raising would lose the audit entry.
    """
    h = ActionHandler()
    h.handle("kill", "wf-1", reason="x")  # no raise
    # History still has the kill event.
    history = h.get_action_history()
    assert any(e.action_type == "kill" for e in history)


def test_handle_pause_records_workflow_in_paused_dict():
    """PAUSE handler raises WorkflowPausedException but it is swallowed
    the workflow_id is recorded in ``_paused_workflows`` first."""
    h = ActionHandler()
    h.handle("pause", "wf-1", reason="x")
    assert "wf-1" in h._paused_workflows


def test_handle_block_does_not_propagate_blocked_exception():
    """BLOCK handler raises NullRunBlockedException but it is swallowed."""
    h = ActionHandler()
    h.handle("block", "wf-1", reason="x")  # no raise
    history = h.get_action_history()
    assert any(e.action_type == "block" for e in history)


def test_handle_handler_exception_swallowed():
    """A buggy custom handler must not crash the dispatch."""
    h = ActionHandler()
    boom = MagicMock(side_effect=RuntimeError("oops"))
    h.register_handler(ActionType.ALERT, boom)
    h.handle("alert", "wf-1", reason="x")  # must not raise


def test_handle_records_event_with_reason():
    h = ActionHandler()
    h.handle("alert", "wf-1", reason="manual escalation")
    events = h.get_action_history()
    assert len(events) == 1
    assert events[0].reason == "manual escalation"


def test_handle_records_event_with_default_reason():
    """``reason=None`` defaults to ``"Unknown"`` for the history record."""
    h = ActionHandler()
    h.handle("alert", "wf-1", reason=None)
    events = h.get_action_history()
    assert events[0].reason == "Unknown"


def test_action_history_trimmed_at_max():
    """History longer than ``_max_history`` is trimmed from the front."""
    h = ActionHandler()
    h._max_history = 3
    for i in range(5):
        h._record_action(ActionType.ALERT, f"wf-{i}", "x", {})
    assert len(h._action_history) == 3
    # Trimmed from the front — the oldest two (``wf-0``, ``wf-1``) are gone.
    wf_ids = [e.workflow_id for e in h._action_history]
    assert wf_ids == ["wf-2", "wf-3", "wf-4"]


def test_action_event_details_default_empty_dict():
    """``ActionEvent.details`` defaults to ``{}`` when not provided."""
    ev = ActionEvent(
        timestamp="2026-01-01T00:00:00Z",
        action_type="kill",
        workflow_id="wf-1",
        reason="x",
    )
    assert ev.details == {}


# ─── is_paused ───────────────────────────────────────────────────────


def test_is_paused_unknown_workflow_returns_false():
    h = ActionHandler()
    assert h.is_paused("wf-never-paused") is False


def test_is_paused_within_cooldown_returns_true():
    h = ActionHandler()
    h._paused_workflows["wf-1"] = time.time()
    assert h.is_paused("wf-1", cooldown_seconds=60.0) is True


def test_is_paused_past_cooldown_returns_false_and_clears():
    h = ActionHandler()
    h._paused_workflows["wf-1"] = time.time() - 100  # 100s ago
    assert h.is_paused("wf-1", cooldown_seconds=60.0) is False
    # Past-cooldown entry is removed so the next call is also False.
    assert "wf-1" not in h._paused_workflows


# ─── webhook async delivery ──────────────────────────────────────────


def test_queue_webhook_starts_delivery_thread():
    h = ActionHandler()
    h.register_webhook(WebhookConfig(url="https://example.com/h"))
    h._queue_webhook(ActionType.KILL, "wf-1", "x", {})
    # A delivery thread is started and registered.
    assert h._webhook_running is True
    assert h._webhook_thread is not None
    # Let the thread exit so the test does not hang.
    h.stop_webhooks()


def test_queue_webhook_overflow_drops_oldest(caplog):
    """Webhook queue overflow → oldest dropped (FIFO) + WARNING logged."""
    import logging

    h = ActionHandler()
    h._webhook_max_size = 2
    with caplog.at_level(logging.WARNING, logger="nullrun.actions"):
        for i in range(4):
            h._queue_webhook(ActionType.KILL, f"wf-{i}", "x", {})
    assert len(h._webhook_queue) == 2
    # Newest two kept.
    assert h._webhook_queue[-1]["workflow_id"] == "wf-3"
    h.stop_webhooks()


def test_deliver_webhook_no_httpx_warns(caplog):
    """If httpx is unavailable, webhook delivery logs and returns."""
    import logging

    import nullrun.actions as act_mod

    h = ActionHandler()
    h.register_webhook(WebhookConfig(url="https://example.com/h"))
    # Force the no-httpx branch.
    original = act_mod._HAS_HTTPX
    act_mod._HAS_HTTPX = False
    try:
        with caplog.at_level(logging.WARNING, logger="nullrun.actions"):
            h._deliver_webhook(h._webhooks[0], {"x": 1})
        assert any("httpx not installed" in r.getMessage() for r in caplog.records)
    finally:
        act_mod._HAS_HTTPX = original


def test_deliver_webhook_success_returns_immediately(monkeypatch):
    """A 200 response on the first attempt stops the loop."""
    h = ActionHandler()
    h.register_webhook(WebhookConfig(url="https://example.com/h"))
    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    monkeypatch.setattr("nullrun.actions.httpx.post", MagicMock(return_value=fake_resp))
    h._deliver_webhook(h._webhooks[0], {"x": 1})  # no raise


def test_deliver_webhook_retries_then_gives_up(monkeypatch):
    """All retries exhausted — loop ends without raising."""
    h = ActionHandler()
    h.register_webhook(WebhookConfig(url="https://example.com/h", retries=2))
    fake_post = MagicMock(side_effect=RuntimeError("down"))
    monkeypatch.setattr("nullrun.actions.httpx.post", fake_post)
    # time.sleep is patched to avoid the actual delay.
    monkeypatch.setattr("time.sleep", MagicMock())
    h._deliver_webhook(h._webhooks[0], {"x": 1})  # no raise
    assert fake_post.call_count == 2


def test_stop_webhooks_joins_thread():
    h = ActionHandler()
    h.register_webhook(WebhookConfig(url="https://example.com/h"))
    h._queue_webhook(ActionType.KILL, "wf-1", "x", {})
    assert h._webhook_thread is not None
    h.stop_webhooks()
    assert h._webhook_running is False


# ─── Module-level helpers ─────────────────────────────────────────────


def test_handle_action_module_helper_dispatches(monkeypatch):
    """``handle_action(...)`` delegates to the global ``ActionHandler``."""
    from nullrun import actions as act_mod

    act_mod._action_handler = None  # force fresh
    h = MagicMock()
    monkeypatch.setattr("nullrun.actions.get_action_handler", lambda: h)
    handle_action("kill", "wf-1", reason="x")
    h.handle.assert_called_once_with("kill", "wf-1", "x")


def test_register_action_handler_module_helper(monkeypatch):
    from nullrun import actions as act_mod

    h = MagicMock()
    monkeypatch.setattr("nullrun.actions.get_action_handler", lambda: h)
    fn = MagicMock()
    register_action_handler(ActionType.KILL, fn)
    h.register_handler.assert_called_once_with(ActionType.KILL, fn)


def test_get_action_handler_returns_singleton():
    from nullrun import actions as act_mod

    act_mod._action_handler = None  # reset
    h1 = act_mod.get_action_handler()
    h2 = act_mod.get_action_handler()
    assert h1 is h2


# ─── nullrun.context ──────────────────────────────────────────────────


def test_generate_trace_id_is_uuid_format():
    from nullrun.context import generate_span_id, generate_trace_id

    tid = generate_trace_id()
    assert tid.count("-") == 4  # canonical UUID4


def test_generate_span_id_is_uuid_format():
    from nullrun.context import generate_span_id

    sid = generate_span_id()
    assert sid.count("-") == 4


def test_attempt_context_manager_pushes_and_restores():
    from nullrun.context import attempt, get_attempt_index, set_attempt_index

    set_attempt_index(0)
    with attempt(3) as idx:
        assert idx == 3
        assert get_attempt_index() == 3
    assert get_attempt_index() == 0


def test_attempt_context_manager_nested():
    from nullrun.context import attempt, get_attempt_index

    with attempt(1):
        with attempt(5):
            assert get_attempt_index() == 5
        assert get_attempt_index() == 1


def test_workflow_context_manager_sets_ids():
    from nullrun.context import get_span_id, get_trace_id, get_workflow_id, workflow

    with workflow("my-flow") as wid:
        assert wid == "my-flow"
        assert get_workflow_id() == "my-flow"
        assert get_trace_id() is not None
        assert get_span_id() is not None
    assert get_workflow_id() is None


def test_workflow_default_name_is_uuid():
    import uuid

    from nullrun.context import get_workflow_id, workflow

    with workflow() as wid:
        # 36-char UUID with dashes.
        uuid.UUID(wid)
        assert get_workflow_id() == wid


def test_span_context_manager_restores_on_exit():
    from nullrun.context import get_span_id, span

    with span("outer") as sid:
        assert get_span_id() == "outer"
    assert get_span_id() is None


def test_span_default_name_is_uuid():
    import uuid

    from nullrun.context import get_span_id, span

    with span() as sid:
        uuid.UUID(sid)
        assert get_span_id() == sid


def test_agent_context_manager_sets_agent_id():
    from nullrun.context import agent, get_agent_id

    with agent("agent-1") as aid:
        assert aid == "agent-1"
        assert get_agent_id() == "agent-1"
    assert get_agent_id() is None


def test_set_attempt_index_writes_to_contextvar():
    from nullrun.context import get_attempt_index, set_attempt_index

    set_attempt_index(42)
    assert get_attempt_index() == 42
    set_attempt_index(0)  # cleanup


def test_workflow_nested_restores_outer_on_exit():
    from nullrun.context import get_workflow_id, workflow

    with workflow("outer"):
        assert get_workflow_id() == "outer"
        with workflow("inner"):
            assert get_workflow_id() == "inner"
        assert get_workflow_id() == "outer"
    assert get_workflow_id() is None


def test_span_id_in_workflow_resets_to_new_value():
    """: ``with workflow(...)`` resets ``span_id``, not only
    workflow_id / trace_id, so the audit log can correctly nest the
    workflow's own span_start under the workflow_id.
    """
    from nullrun.context import get_span_id, span, workflow

    with span("outer-span"):
        original = get_span_id()
        with workflow("wf-x"):
            # span_id must have changed (new UUID), not still "outer-span".
            new = get_span_id()
            assert new != original
            assert new is not None


# ─── nullrun.__init__ ────────────────────────────────────────────────


def test_init_unknown_attr_raises_attribute_error():
    """``nullrun.something_unknown`` raises AttributeError, not ImportError."""
    with pytest.raises(AttributeError):
        nullrun.no_such_attribute  # noqa: B018


def test_init_lazy_export_loads_attribute():
    """First access to a lazy export caches it on the module."""
    rt = nullrun.NullRunRuntime
    # Subsequent access is the cached object.
    assert nullrun.NullRunRuntime is rt


def test_dir_lists_only_curated_surface():
    """``dir(nullrun)`` shows only the 6 curated names + __version__."""
    public = dir(nullrun)
    # The 6 curated names are explicitly listed.
    for name in ("init", "protect", "track_llm", "track_tool", "track_event"):
        assert name in public
    # Lazy exports are NOT in dir until first access.
    assert "SpanContext" not in public
    assert "NullRunRuntime" not in public


def test_init_module_has_all_attribute():
    """The ``__all__`` attribute lists the curated surface."""
    assert "init" in nullrun.__all__
    assert "protect" in nullrun.__all__


# ─── WorkflowKilledException deprecation warning ─────────────────────


def test_workflow_killed_exception_emits_deprecation_warning():
    """Constructing the deprecated ``WorkflowKilledException`` triggers
    a ``DeprecationWarning``.
    """
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        WorkflowKilledException(workflow_id="wf-1", reason="x")
    assert any(issubclass(item.category, DeprecationWarning) for item in w)


def test_workflow_killed_interrupt_does_not_emit_warning():
    """Constructing the canonical ``WorkflowKilledInterrupt`` does NOT
    emit a deprecation warning (the deprecation is on the parent name).
    """
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        WorkflowKilledInterrupt(workflow_id="wf-1", reason="x")
    assert not any(issubclass(item.category, DeprecationWarning) for item in w)


def test_workflow_killed_interrupt_is_base_exception():
    """``except Exception`` does NOT catch the kill signal."""
    with pytest.raises(WorkflowKilledInterrupt):
        try:
            raise WorkflowKilledInterrupt(workflow_id="wf-1", reason="x")
        except Exception:
            pytest.fail("Exception should not catch WorkflowKilledInterrupt")


def test_workflow_killed_exception_is_caught_by_except_killed_exception():
    """Legacy ``except WorkflowKilledException`` still catches the new
    interrupt (back-compat contract).
    """
    with pytest.raises(WorkflowKilledException):
        raise WorkflowKilledInterrupt(workflow_id="wf-1", reason="x")
