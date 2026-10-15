"""
Regression (2026-10-10): ``Transport.connect_websocket`` wired
``on_policy_invalidated`` as an ``async def`` while the WS dispatch
path calls it **synchronously**.

Same bug class as ``test_approval_ws_sync_callback.py`` (fixed
2026-07-24), missed on the sibling callback. Observed live on a real
init against production::

    RuntimeWarning: coroutine 'Transport.connect_websocket.<locals>.
    wrapped_policy_invalidated' was never awaited
      self.on_policy_invalidated(organization_id, policy_id, new_version)

The body never runs. Worse than the approval case: the dispatcher's
``try/except`` cannot catch it either, because constructing an
un-awaited coroutine raises nothing. The callback is created and
silently discarded, so the ``logger.info`` and the user-supplied
``on_policy_invalidated`` never fire.

The file already documented the rule one function below the defect::

    # Synchronous adapter: dispatch is dict-only, not awaitable. An
    # async def would produce a coroutine the handler ignores.
    def wrapped_approval_resolved(payload): ...

``wrapped_key_rotated`` is genuinely ``async`` and correctly so — it
awaits ``self._refetch_credentials()``, and its dispatcher DOES
await it. ``on_policy_invalidated`` awaits nothing, so it must be
sync. These tests pin both halves so neither drifts back.
"""

from __future__ import annotations

import asyncio
import inspect

from nullrun.transport import Transport
from nullrun.transport_websocket import WebSocketConnection


def _capture_wrapped_callbacks(**callbacks) -> dict[str, object]:
    """Run ``connect_websocket`` and return what it hands the connection.

    The wrappers live in a closure, so we capture the kwargs at the
    ``WebSocketConnection(...)`` boundary instead of re-implementing
    them. The real ``__init__`` is skipped — we only need the values.
    """
    captured: dict[str, object] = {}

    real_init = WebSocketConnection.__init__
    real_connect = WebSocketConnection.connect

    def _capturing_init(self, *args, **kwargs):
        for name in (
            "on_policy_invalidated",
            "on_key_rotated",
            "on_approval_resolved",
            "on_state_change",
        ):
            captured[name] = kwargs.get(name)

    async def _no_connect(self):  # pragma: no cover - never reached
        return None

    WebSocketConnection.__init__ = _capturing_init  # type: ignore[method-assign]
    WebSocketConnection.connect = _no_connect  # type: ignore[method-assign]
    try:
        transport = Transport(api_url="https://api.nullrun.io", api_key="test-key")
        asyncio.run(transport.connect_websocket(organization_id="org-1", **callbacks))
    finally:
        WebSocketConnection.__init__ = real_init
        WebSocketConnection.connect = real_connect
    return captured


def test_wrapped_policy_invalidated_is_synchronous():
    """``on_policy_invalidated`` must be a plain ``def``.

    The dispatcher calls it without ``await`` and inside a
    ``try/except`` that can never see an un-awaited coroutine — so
    the wrong shape fails silently rather than loudly.
    """
    captured = _capture_wrapped_callbacks()
    wrapped = captured["on_policy_invalidated"]

    assert not inspect.iscoroutinefunction(wrapped), (
        "on_policy_invalidated wrapper must be a plain function; "
        "async def produces an un-awaited coroutine, the dispatcher "
        "discards it, and neither the log line nor the user callback "
        "ever runs."
    )
    assert callable(wrapped)


def test_wrapped_policy_invalidated_invokes_callback_synchronously():
    """Calling the wrapper must run the body — and run it once.

    This is the assertion the 2026-07-24 approval test had and the
    policy callback never got: an ``async def`` wrapper still passes a
    naive ``callable()`` check and still passes an
    ``iscoroutinefunction`` check on the WRONG object. Only calling it
    and observing the side effect distinguishes "defined async" from
    "defined sync but forgot to run".
    """
    received: list[tuple] = []

    captured = _capture_wrapped_callbacks(
        on_policy_invalidated=lambda ws_id, policy_id, ver: received.append(
            (ws_id, policy_id, ver)
        )
    )
    wrapped = captured["on_policy_invalidated"]

    wrapped("org-1", "pol-42", 7)

    assert received == [("org-1", "pol-42", 7)], (
        "the wrapper did not forward synchronously — with an async def "
        "the callback is never invoked"
    )


def test_wrapped_key_rotated_stays_async():
    """``on_key_rotated`` IS legitimately async — pin it.

    It awaits ``self._refetch_credentials()`` and its dispatcher DOES
    await it, so it must stay ``async def``. Written so a future
    "make everything sync" sweep does not silently break key rotation
    (the credentials refetch would become a fire-and-forget coroutine).
    """
    captured = _capture_wrapped_callbacks()
    wrapped = captured["on_key_rotated"]

    assert inspect.iscoroutinefunction(wrapped), (
        "on_key_rotated must stay async — it awaits "
        "_refetch_credentials() and the dispatcher awaits it"
    )