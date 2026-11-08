"""
Client-side action handling for NullRun SDK.

When the circuit breaker trips (on backend or locally), these handlers
actually execute the protective actions.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

from nullrun.breaker.exceptions import (
    NullRunBlockedException,
    NullRunUnknownActionError,
    NullRunWorkflowKilledError,
    WorkflowKilledInterrupt,
    WorkflowPausedException,
)
from nullrun.observability import metrics

logger = logging.getLogger(__name__)

# The refusals the DEFAULT handlers raise ON PURPOSE: kill, pause,
# block. A handler that refuses this way HAS EXECUTED — the refusal is
# the action's outcome, not a fault in the handler, so it belongs on
# the INFO channel. Anything else a handler raises is a fault in the
# handler and belongs on the ERROR channel.
#
# Deliberately NOT the whole ``ENFORCEMENT_EXCEPTIONS`` family: that
# family also holds classes that mean "the SDK could not determine the
# answer" (``NullRunGateIndeterminateError``), and downgrading those
# to INFO here would hide a refusal behind a log level.
_ACTION_REFUSALS: tuple[type[BaseException], ...] = (
    WorkflowKilledInterrupt,
    WorkflowPausedException,
    NullRunBlockedException,
)


@dataclass
class ActionEvent:
    """Represents an action event for logging/replay."""
    timestamp: str
    action_type: str
    workflow_id: str
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


class ActionType(str, Enum):
    """Types of actions that can be taken."""
    KILL = "kill"
    PAUSE = "pause"
    ALERT = "alert"
    SNAPSHOT = "snapshot"
    BLOCK = "block"
    WEBHOOK = "webhook"


class WebhookConfig:
    """Configuration for webhook notifications."""
    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 5.0,
        retries: int = 3,
    ):
        self.url = url
        self.headers = headers or {}
        self.timeout = timeout
        self.retries = retries


class ActionHandler:
    """
    Handler for NullRun circuit breaker actions.

    This executes protective actions when triggered:
    - KILL: signals the kill (the default handler raises
      NullRunWorkflowKilledError, NR-W002). The raise is SWALLOWED by
      ``handle`` — see its docstring — so what actually stops the
      workflow is the GATE refusing the next /check (ADR-063 §3), not
      this handler.
    - PAUSE: signals the pause (raises WorkflowPausedException) —
      swallowed the same way, and the gate refuses the next /check.
    - ALERT: Sends notification (can be customized)
    - SNAPSHOT: Captures workflow state for debugging
    - WEBHOOK: Sends HTTP webhook notification

    Usage:
        handler = ActionHandler 

        # Register custom alert handler
        def my_alert(msg):
            send_to_slack(msg)

        handler.register_handler(ActionType.ALERT, my_alert)

        # Register webhook
        handler.register_webhook(WebhookConfig(
            url="https:/hooks.slack.com/..."
            headers={"Content-Type": "application/json"}
        ))

        # Execute action
        handler.handle("kill", workflow_id="wf-123", reason="Budget exceeded")
    """

    def __init__(self) -> None:
        self._handlers: dict[ActionType, Callable[..., Any]] = {
            ActionType.KILL: self._default_kill,
            ActionType.PAUSE: self._default_pause,
            ActionType.ALERT: self._default_alert,
            ActionType.SNAPSHOT: self._default_snapshot,
            ActionType.BLOCK: self._default_block,
            ActionType.WEBHOOK: self._default_webhook,
        }
        self._paused_workflows: dict[str, float] = {}
        self._webhooks: list[WebhookConfig] = []
        self._action_history: list[ActionEvent] = []
        self._max_history = 1000
        self._lock = threading.Lock()
        self._webhook_thread: threading.Thread | None = None
        self._webhook_queue: list[dict[str, Any]] = []
        self._webhook_max_size = 1000  # Limit queue size to prevent memory leak
        self._webhook_running = False

    def register_handler(self, action: ActionType, handler: Callable[..., Any]) -> None:
        """Register a custom handler for an action type."""
        self._handlers[action] = handler

    def register_webhook(self, config: WebhookConfig) -> None:
        """
        Register a webhook for action notifications.

        Args:
            config: WebhookConfig with URL and options
        """
        self._webhooks.append(config)
        logger.info(f"Registered webhook: {config.url}")

    def remove_webhook(self, url: str) -> None:
        """Remove a webhook by URL."""
        self._webhooks = [w for w in self._webhooks if w.url != url]

    def get_action_history(self, limit: int = 100) -> list[ActionEvent]:
        """Get recent action events."""
        with self._lock:
            return self._action_history[-limit:]

    def clear_history(self) -> None:
        """Clear action history."""
        with self._lock:
            self._action_history.clear()

    def _record_action(
        self,
        action_type: ActionType,
        workflow_id: str,
        reason: str,
        details: dict[str, Any],
    ) -> None:
        """Record action to history."""
        with self._lock:
            event = ActionEvent(
                timestamp=datetime.now(timezone.utc).isoformat(),
                action_type=action_type.value,
                workflow_id=workflow_id,
                reason=reason,
                details=details,
            )
            self._action_history.append(event)
            # Trim history
            if len(self._action_history) > self._max_history:
                self._action_history = self._action_history[-self._max_history:]

    def handle(
        self,
        action: str,
        workflow_id: str,
        reason: str | None = None,
        **details: Any,
    ) -> None:
        """
        Handle a circuit breaker action.

        Args:
            action: Action type string ("kill", "pause", "alert", etc.)
            workflow_id: ID of the workflow
            reason: Human-readable reason for the action
            **details: Additional details about the action

        Raises:
            NullRunUnknownActionError: If ``action`` is not a known
                action type. The action was NOT executed and the
                caller is told so. It is deliberately NOT a block, so
                the workflow keeps running — an unrecognised value
                must not become a DoS amplifier.

        Note:
            The handler's own refusal (kill / pause / block) does NOT
            escape this method. It is recorded in the action history
            and logged at INFO. The authoritative enforcement of a kill
            or a pause is the GATE (ADR-063 §3): the gate refuses the
            next /check. A handler that raises anything else is a
            fault and is logged at ERROR.
        """
        try:
            action_type = ActionType(action.lower())
        except ValueError as exc:
            # Pre-fix this degraded silently to ``ActionType.BLOCK``
            # (B14) and triggered ``_default_block`` which raises
            # ``NullRunBlockedException``. That made the SDK into a
            # DoS amplifier: a single malformed ``action`` from the
            # server (or a MITM, or a server schema regression)
            # would block every subsequent tool call in the workflow
            # with no actionable error.
            #
            # B14's fix (log at ERROR, record the event, RETURN) kept
            # the DoS property but left the action neither executed
            # nor refused: the caller could not tell "it ran" from
            # "it could not run" (SDK-12).
            #
            # Post-fix: log at ERROR, record the event for forensic
            # visibility, DO NOT invoke any handler, and RAISE a typed
            # error so the caller learns the action did not run. The
            # error is an infrastructure error, NOT a block — the
            # workflow keeps running.
            logger.error(
                f"Unknown action type received from control plane: {action!r} "
                f"for workflow {workflow_id!r} (reason={reason!r}). "
                "This is a server/SDK version mismatch or a control plane "
                "schema regression. The action was NOT executed — the "
                "workflow keeps running. Investigate ASAP."
            )
            self._record_action(
                ActionType.BLOCK,
                workflow_id,
                f"unknown_action_type:{action}",
                details,
            )
            metrics.inc_runtime("unknown_action_total")
            raise NullRunUnknownActionError(
                f"the control plane sent an unknown action type {action!r} "
                f"for workflow {workflow_id!r}; the action was not executed",
                action=action,
                workflow_id=workflow_id,
                reason=reason,
                cause=exc,
            ) from exc

        handler = self._handlers.get(action_type, self._default_block)

        # Record action to history
        self._record_action(action_type, workflow_id, reason or "Unknown", details)

        # Trigger webhooks asynchronously
        if self._webhooks:
            self._queue_webhook(action_type, workflow_id, reason or "Unknown", details)

        try:
            handler(workflow_id, reason or "Unknown", **details)  # type: ignore[no-untyped-call]
        except _ACTION_REFUSALS as e:
            # The handler REFUSED ON PURPOSE — kill, pause, block. That
            # is the action's outcome, not a fault in the handler, so it
            # goes to INFO and the ERROR channel keeps meaning
            # "something is broken". Pre-SDK-12 this arm logged at
            # ERROR, so every server-side kill raised a false alarm.
            #
            # The refusal is deliberately NOT re-raised: the history
            # entry above is already the audit trail, and the
            # authoritative enforcement of a kill/pause is the GATE
            # (ADR-063 §3 — the gate refuses the next /check).
            # Re-raising here would abort the caller's action loop over
            # an action that was delivered correctly.
            logger.info(
                f"Action {action_type.value} refused for workflow "
                f"{workflow_id}: {e}"
            )
        except Exception as e:
            # A real fault in the handler (a buggy custom handler, a
            # broken call). ``KeyboardInterrupt`` / ``SystemExit`` /
            # ``GeneratorExit`` are NOT caught: they are BaseException,
            # not Exception, and a Ctrl-C must reach the caller.
            logger.error(f"Action handler error: {e}")

    def _default_kill(
        self,
        workflow_id: str,
        reason: str,
        **details: Any,
    ) -> None:
        """Default kill handler - raises NullRunWorkflowKilledError.

        Cookbook code can `except NullRunWorkflowKilledError` to react
        to operator-initiated kills with structured error_code +
        user_action.
        """
        logger.warning(f"KILL action for workflow {workflow_id}: {reason}")
        raise NullRunWorkflowKilledError(
            workflow_id=workflow_id,
            reason=reason,
            kill_source="action_handler",
        )

    def _default_pause(
        self,
        workflow_id: str,
        reason: str,
        duration: float | None = None,
        **details: Any,
    ) -> None:
        """Default pause handler - raises WorkflowPausedException."""
        logger.warning(f"PAUSE action for workflow {workflow_id}: {reason}")

        # Track paused workflow
        with self._lock:
            self._paused_workflows[workflow_id] = time.time()

        raise WorkflowPausedException(
            workflow_id=workflow_id,
            reason=reason,
            resume_after=duration,
        )

    def _default_alert(
        self,
        workflow_id: str,
        reason: str,
        **details: Any,
    ) -> None:
        """Default alert handler - logs the alert."""
        logger.warning(f"ALERT for workflow {workflow_id}: {reason}")

    def _default_snapshot(
        self,
        workflow_id: str,
        reason: str,
        **details: Any,
    ) -> None:
        """Default snapshot handler - logs snapshot request."""
        logger.info(f"SNAPSHOT requested for workflow {workflow_id}: {reason}")

    def _default_block(
        self,
        workflow_id: str,
        reason: str,
        **details: Any,
    ) -> None:
        """Default block handler - raises NullRunBlockedException."""
        raise NullRunBlockedException(
            workflow_id=workflow_id,
            reason=reason,
            action="block",
            **details,
        )

    def _default_webhook(
        self,
        workflow_id: str,
        reason: str,
        **details: Any,
    ) -> None:
        """Default webhook handler - triggers registered webhooks."""
        # Webhooks are handled asynchronously via _queue_webhook
        logger.debug(f"WEBHOOK queued for workflow {workflow_id}: {reason}")

    def _queue_webhook(
        self,
        action_type: ActionType,
        workflow_id: str,
        reason: str,
        details: dict[str, Any],
    ) -> None:
        """Queue webhook for async delivery."""
        payload = {
            "action": action_type.value,
            "workflow_id": workflow_id,
            "reason": reason,
            "details": details,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            # Enforce max queue size to prevent memory leak
            if len(self._webhook_queue) >= self._webhook_max_size:
                removed = self._webhook_queue.pop(0)
                logger.warning(
                    f"Webhook queue overflow, dropping oldest: "
                    f"{removed.get('workflow_id')}"
                )
            self._webhook_queue.append(payload)

        # Start webhook thread if not running
        if not self._webhook_running:
            self._webhook_running = True
            self._webhook_thread = threading.Thread(
                target=self._webhook_delivery,
                daemon=True,
                name="nullrun-webhook"
            )
            self._webhook_thread.start()

    def _webhook_delivery(self) -> None:
        """Background thread for delivering webhooks."""
        while self._webhook_running:
            try:
                # Process queue
                payload = None
                with self._lock:
                    if self._webhook_queue:
                        payload = self._webhook_queue.pop(0)

                if payload is None:
                    time.sleep(0.5)
                    continue

                # Deliver to all registered webhooks
                for webhook in self._webhooks:
                    self._deliver_webhook(webhook, payload)

            except Exception as e:
                logger.error(f"Webhook delivery error: {e}")

    def _deliver_webhook(self, webhook: WebhookConfig, payload: dict[str, Any]) -> None:
        """Deliver a single webhook."""
        if not _HAS_HTTPX:
            logger.warning("httpx not installed, cannot send webhook")
            return

        # 30s cap. Pre-fix the schedule was linear (``0.5 * (attempt+1)``
        # → 0.5s, 1.0s, 1.5s,...). Linear doesn't back off fast enough
        # when the destination is down — a transient outage produced
        # 100+ retries in seconds, and each KILL/PAUSE from the server
        # spawns its own delivery thread, so 1000 events/min generated
        # 1000 spinning daemon threads hammering the dead endpoint.
        #
        # Schedule: 0.5s, 1.0s, 2.0s, 4.0s, 8.0s, 16.0s, 30.0s (capped).
        # Total worst-case wait over 7 retries is ~62s — long enough to
        # ride out a brief blip, short enough that one stuck thread
        # doesn't block forever.
        _BACKOFF_BASE = 0.5
        _BACKOFF_CAP = 30.0
        for attempt in range(webhook.retries):
            try:
                response = httpx.post(
                    webhook.url,
                    json=payload,
                    headers=webhook.headers,
                    timeout=webhook.timeout,
                )
                response.raise_for_status()
                logger.debug(f"Webhook delivered to {webhook.url}")
                return
            except Exception as e:
                logger.warning(f"Webhook attempt {attempt + 1} failed: {e}")
                if attempt < webhook.retries - 1:
                    delay = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_CAP)
                    time.sleep(delay)

    def stop_webhooks(self) -> None:
        """Stop webhook delivery thread."""
        self._webhook_running = False
        if self._webhook_thread:
            self._webhook_thread.join(timeout=2.0)

    def is_paused(self, workflow_id: str, cooldown_seconds: float = 60.0) -> bool:
        """
        Check if a workflow is currently paused.

        Args:
            workflow_id: ID of the workflow
            cooldown_seconds: Consider unpaused after this time

        Returns:
            True if workflow is paused and within cooldown period
        """
        with self._lock:
            if workflow_id not in self._paused_workflows:
                return False

            paused_at = self._paused_workflows[workflow_id]
            elapsed = time.time() - paused_at

            if elapsed > cooldown_seconds:
                # Cooldown expired, remove from paused list
                del self._paused_workflows[workflow_id]
                return False

            return True


# Global action handler instance
_action_handler: ActionHandler | None = None
_handler_lock = threading.Lock()


def get_action_handler() -> ActionHandler:
    """Get the global action handler instance."""
    global _action_handler
    if _action_handler is None:
        with _handler_lock:
            if _action_handler is None:
                _action_handler = ActionHandler()
    return _action_handler


def handle_action(
    action: str,
    workflow_id: str,
    reason: str | None = None,
    **details: Any,
) -> None:
    """
    Handle a circuit breaker action using the global handler.

    Usage:
        handle_action("kill", "wf-123", "Budget exceeded")
    """
    get_action_handler().handle(action, workflow_id, reason, **details)


def register_action_handler(action: ActionType, handler: Callable[..., Any]) -> None:
    """Register a custom handler for an action type."""
    get_action_handler().register_handler(action, handler)
