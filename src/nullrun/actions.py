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
from datetime import datetime
from enum import Enum
from typing import Any

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

from nullrun.breaker.exceptions import (
    NullRunBlockedException,
    WorkflowKilledInterrupt,
    WorkflowPausedException,
)

logger = logging.getLogger(__name__)


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
    - KILL: Immediately stops the workflow (raises WorkflowKilledInterrupt)
    - PAUSE: Temporarily halts the workflow (raises WorkflowPausedException)
    - ALERT: Sends notification (can be customized)
    - SNAPSHOT: Captures workflow state for debugging
    - WEBHOOK: Sends HTTP webhook notification

    Usage:
        handler = ActionHandler()

        # Register custom alert handler
        def my_alert(msg):
            send_to_slack(msg)

        handler.register_handler(ActionType.ALERT, my_alert)

        # Register webhook
        handler.register_webhook(WebhookConfig(
            url="https://hooks.slack.com/...",
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
                timestamp=datetime.utcnow().isoformat(),
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
            WorkflowKilledInterrupt: If action is "kill"
            WorkflowPausedException: If action is "pause"
            NullRunBlockedException: If action is "block"
        """
        try:
            action_type = ActionType(action.lower())
        except ValueError:
            logger.warning(f"Unknown action type: {action}")
            action_type = ActionType.BLOCK

        handler = self._handlers.get(action_type, self._default_block)

        # Record action to history
        self._record_action(action_type, workflow_id, reason or "Unknown", details)

        # Trigger webhooks asynchronously
        if self._webhooks:
            self._queue_webhook(action_type, workflow_id, reason or "Unknown", details)

        try:
            handler(workflow_id, reason or "Unknown", **details)  # type: ignore[no-untyped-call]
        except BaseException as e:
            # Don't let handler exceptions propagate. We catch
            # `BaseException` (not just `Exception`) because
            # `WorkflowKilledInterrupt` is intentionally a
            # `BaseException` subclass — it's a non-recoverable
            # control signal, but inside the ActionHandler dispatch
            # loop we want the kill to be recorded in history
            # (already done above) and swallowed, NOT re-raised into
            # the caller's frame.
            logger.error(f"Action handler error: {e}")

    def _default_kill(
        self,
        workflow_id: str,
        reason: str,
        **details: Any,
    ) -> None:
        """Default kill handler - raises WorkflowKilledInterrupt."""
        logger.warning(f"KILL action for workflow {workflow_id}: {reason}")
        raise WorkflowKilledInterrupt(workflow_id=workflow_id, reason=reason)

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
            "timestamp": datetime.utcnow().isoformat(),
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
                    time.sleep(0.5 * (attempt + 1))

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

    def clear_pause(self, workflow_id: str) -> None:
        """Manually clear paused state for a workflow."""
        with self._lock:
            self._paused_workflows.pop(workflow_id, None)


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
