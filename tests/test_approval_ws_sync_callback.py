"""
Regression (2026-07-24): ``Transport.connect_websocket`` wires the
``on_approval_resolved`` callback as a **plain function**, not an
``async def``.

The WS dispatch path in ``transport_websocket.py`` calls
``self.on_approval_resolved(data)`` synchronously. Declaring
``wrapped_approval_resolved`` as ``async def`` produced an
un-awaited coroutine, and the SDK's pending ``threading.Event``
never fired — the agent stayed parked on the gate until the
300s default timeout even after the operator clicked Approve.

The fix is one-line in ``transport.py``; the regression test
here pins the contract: the callback is invoked synchronously
with the raw payload dict, and a coroutine wrapper is **not**
acceptable.

Same test would have caught the bug 2026-07-23 if it existed
in the test suite at SDK 0.13.11 — it was added when
``connect_websocket`` grew the wrapped wrappers for the other
callbacks, and the audit found the missing sync-only test for
the approval callback specifically.
"""

from __future__ import annotations

import asyncio
import inspect

from nullrun.transport import Transport


def test_wrapped_approval_resolved_is_synchronous():
    """The adapter passed to ``WebSocketConnection(on_approval_resolved=...)``
    must be a plain ``def``. ``async def`` produces a coroutine
    that the dispatcher ignores, leaving the agent stuck on
    the gate.
    """
    transport = Transport(
        api_url="https://api.nullrun.io", api_key="test-key"
    )
    received: list[dict] = []

    def on_approval_resolved(payload):
        received.append(payload)

    # The wrapper lives inside ``Transport.connect_websocket``'s
    # closure. Rather than re-implementing the wrapper to read the
    # ``on_approval_resolved=`` argument it forwards, we patch
    # ``WebSocketConnection.__init__`` to capture whatever the
    # wrapper hands the connection. The real
    # ``WebSocketConnection`` is only used to instantiate the
    # connection object; we never call ``.connect()`` on it.
    captured: dict[str, object] = {}

    from nullrun.transport_websocket import WebSocketConnection

    real_init = WebSocketConnection.__init__
    real_connect = WebSocketConnection.connect

    def _capturing_init(self, *args, **kwargs):
        captured["on_approval_resolved"] = kwargs.get(
            "on_approval_resolved"
        )
        captured["on_policy_invalidated"] = kwargs.get(
            "on_policy_invalidated"
        )
        captured["on_key_rotated"] = kwargs.get(
            "on_key_rotated"
        )
        # Skip the real init — we only need the wrapper values.

    async def _no_connect(self):  # pragma: no cover - placeholder
        return None

    WebSocketConnection.__init__ = _capturing_init  # type: ignore[method-assign]
    WebSocketConnection.connect = _no_connect  # type: ignore[method-assign]
    try:
        transport = Transport(
            api_url="https://api.nullrun.io", api_key="test-key"
        )
        asyncio.run(
            transport.connect_websocket(
                organization_id="org-1",
                on_approval_resolved=on_approval_resolved,
            )
        )
    finally:
        WebSocketConnection.__init__ = real_init
        WebSocketConnection.connect = real_connect

    wrapped = captured["on_approval_resolved"]
    assert not inspect.iscoroutinefunction(wrapped), (
        "on_approval_resolved wrapper must be a plain function; "
        "async def produces an un-awaited coroutine and the SDK "
        "pending Event never fires."
    )
    assert callable(wrapped)

    # Sanity: calling the wrapper invokes the user callback
    # synchronously and exactly once.
    wrapped({"approval_id": "abc", "outcome": "approved"})
    assert received == [{"approval_id": "abc", "outcome": "approved"}]
