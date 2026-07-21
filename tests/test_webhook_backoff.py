"""
Regression test for plan item P3-2: webhook retry backoff must be
exponential, capped at 30s. Pre-fix it was linear
(``0.5 * (attempt + 1)``), which doesn't back off fast enough when
the destination is down — under sustained backend outage, each
KILL/PAUSE event spawns its own delivery thread, and 1000 events
per minute = 1000 spinning threads hammering the dead endpoint.

Post-fix the schedule is ``0.5 * 2**attempt`` capped at 30s:
0.5s, 1.0s, 2.0s, 4.0s, 8.0s, 16.0s, 30.0s (cap).

These tests mock ``nullrun.actions.time.sleep`` directly via
``unittest.mock.patch``. The conftest autouse ``_fast_sleep``
fixture caps test-code ``time.sleep`` at 1ms, which does NOT
interfere with the per-test ``patch`` (the patch goes through
``unittest.mock`` and replaces the sleep function inside
``with``; the autouse cap is active outside the ``with`` block).
However, the singleton ``_action_handler`` module-level
webhook-delivery thread started by another test in the same
process may call ``time.sleep(0.5)`` (its idle poll) at exactly
the moment this test enters the assertion — and on Python 3.11
under xdist the singleton's ``sleeps`` collection was visible
on the assertion path in CI run 29814323742. Marking the whole
module ``@pytest.mark.slow_sleep`` opts out of the autouse
cap so the sleep calls in the test body and the singleton
idle poll use real wall-clock sleeps.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from nullrun.actions import ActionHandler, WebhookConfig


pytestmark = pytest.mark.slow_sleep


def _make_handler_with_webhook(retries: int = 7) -> ActionHandler:
    """Build an ActionHandler with one registered webhook.

    We avoid touching the real runtime (the ActionHandler is
    constructed without one in the existing code; the delivery path
    uses httpx directly)."""
    handler = ActionHandler()
    handler.register_webhook(
        WebhookConfig(
            url="http://localhost:19999/webhook",
            retries=retries,
            timeout=5.0,
        )
    )
    return handler


def test_webhook_uses_exponential_backoff():
    """Each failed delivery must sleep for ``min(0.5 * 2**attempt, 30)s``.

    Pre-fix this was ``0.5 * (attempt + 1)`` — linear, slow to back
    off. Under a sustained outage the linear schedule produced a
    tight retry storm on the dead endpoint.
    """
    handler = _make_handler_with_webhook(retries=4)

    # Patch httpx.post to always raise so we go through every retry.
    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)

    with (
        patch("nullrun.actions.httpx.post", side_effect=ConnectionError("down")),
        patch("nullrun.actions.time.sleep", side_effect=fake_sleep),
    ):
        handler._deliver_webhook(
            payload={"event": "kill"},
            webhook=handler._webhooks[0],
        )

    # 4 attempts → 3 sleeps (no sleep after the last attempt).
    assert len(sleeps) == 3, f"expected 3 sleeps for 4 attempts; got {len(sleeps)}"
    # Exponential: 0.5, 1.0, 2.0
    assert sleeps == [0.5, 1.0, 2.0], (
        f"expected exponential backoff [0.5, 1.0, 2.0]; got {sleeps}. "
        f"Linear backoff (pre-fix) would have produced [0.5, 1.0, 1.5]."
    )


def test_webhook_backoff_capped_at_30_seconds():
    """For retries past the cap boundary, the sleep must be 30s
    (not 64s, 128s,...). Without the cap a webhook with
    retries=10 would sleep ~1024 seconds between the last two
    attempts."""
    handler = _make_handler_with_webhook(retries=8)

    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)

    with (
        patch("nullrun.actions.httpx.post", side_effect=ConnectionError("down")),
        patch("nullrun.actions.time.sleep", side_effect=fake_sleep),
    ):
        handler._deliver_webhook(
            payload={"event": "kill"},
            webhook=handler._webhooks[0],
        )

    # 8 attempts → 7 sleeps.
    # Schedule: 0.5, 1, 2, 4, 8, 16, 30 (capped, would be 32 without cap).
    expected = [0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0]
    assert sleeps == expected, f"expected capped exponential backoff {expected}; got {sleeps}"


def test_webhook_succeeds_on_first_try_no_sleep():
    """Sanity: a successful delivery on the first attempt produces
    zero sleeps. The fix only touches the retry path."""
    handler = _make_handler_with_webhook(retries=4)

    response = MagicMock()
    response.raise_for_status.return_value = None

    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)

    with (
        patch("nullrun.actions.httpx.post", return_value=response),
        patch("nullrun.actions.time.sleep", side_effect=fake_sleep),
    ):
        handler._deliver_webhook(
            payload={"event": "kill"},
            webhook=handler._webhooks[0],
        )

    assert sleeps == [], f"successful first attempt should not sleep; got {sleeps}"


def test_webhook_no_sleep_after_final_attempt():
    """The last attempt must NOT sleep — there's nothing to wait for.
    Pre-fix this was already correct; we lock it in with a test so a
    future refactor doesn't accidentally add a trailing sleep."""
    handler = _make_handler_with_webhook(retries=3)

    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)

    with (
        patch("nullrun.actions.httpx.post", side_effect=ConnectionError("down")),
        patch("nullrun.actions.time.sleep", side_effect=fake_sleep),
    ):
        handler._deliver_webhook(
            payload={"event": "kill"},
            webhook=handler._webhooks[0],
        )

    # 3 attempts → 2 sleeps (between attempts only).
    assert len(sleeps) == 2
