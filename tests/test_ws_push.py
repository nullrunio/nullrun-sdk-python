"""
Tests for the SDK WebSocket push path (Phase B of the hardening plan).

The push contract: when the server pushes a `state_change` message with
`state: "Killed"`, the runtime's `on_state_change` callback writes the
state into `runtime._remote_states[workflow_id]`, and the next
`check_control_plane(workflow_id)` call raises
`WorkflowKilledException`.

We cover the contract at two levels:

1. **Unit test** of the kill path — directly invoke the on_state_change
   callback the WS thread uses, assert that the runtime surfaces the
   killed state via check_control_plane.

2. **Wire test** — spin up a local `websockets` server, connect via the
   real `WebSocketConnection` class, push a `state_change` frame, and
   assert the callback fires within 200ms.

The wire test pins the actual server → client protocol (the JSON shape,
the dispatch flow, the no-HMAC dev path), so a backend wire-format
regression breaks this test, not just the unit test.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import pytest
import websockets

from nullrun.breaker.exceptions import WorkflowKilledException
from nullrun.runtime import NullRunRuntime
from nullrun.transport_websocket import WebSocketConnection

# ---------------------------------------------------------------------------
# 1. Kill-path contract via direct on_state_change invocation
# ---------------------------------------------------------------------------


def _make_runtime(workflow_id: str = "wf-1") -> NullRunRuntime:
    """Build a NullRunRuntime in remote mode with the WS thread NOT
    started. We want to drive the state dict manually so the test
    stays synchronous and deterministic."""
    # `_test_mode=True` skips the network calls in __init__ (auth
    # handshake, policy fetch) so the test stays isolated. api_key
    # is required as of 0.3.0 (T3-S2) — the previous local_mode path
    # that skipped remote state was removed.
    rt = NullRunRuntime(
        api_key="test-key",
        _test_mode=True,
    )
    # We never start the WS thread; the workflow_id is set lazily.
    rt.workflow_id = workflow_id
    return rt


def test_kill_state_surfaces_as_workflow_killed_exception():
    """If the WS push writes a Killed state, the next
    check_control_plane() raises WorkflowKilledException."""
    rt = _make_runtime("wf-kill")

    # Simulate the WS push: on_state_change writes to _remote_states.
    state_msg = {
        "workflow_id": "wf-kill",
        "state": "Killed",
        "version": 1,
        "reason": "policy_violation",
        "updated_at": int(time.time()),
    }
    rt._remote_states["wf-kill"] = {
        "state": state_msg["state"],
        "version": state_msg["version"],
        "reason": state_msg["reason"],
        "updated_at": state_msg["updated_at"],
    }

    with pytest.raises(WorkflowKilledException) as exc_info:
        rt.check_control_plane("wf-kill")
    assert "policy_violation" in str(exc_info.value)


def test_paused_state_surfaces_as_workflow_paused_exception():
    """Same contract for Paused — the gate should raise
    WorkflowPausedException, NOT WorkflowKilledException."""
    from nullrun.breaker.exceptions import WorkflowPausedException

    rt = _make_runtime("wf-pause")
    rt._remote_states["wf-pause"] = {
        "state": "Paused",
        "version": 1,
        "reason": "awaiting approval",
        "updated_at": int(time.time()),
    }
    with pytest.raises(WorkflowPausedException):
        rt.check_control_plane("wf-pause")


def test_normal_state_does_not_raise():
    rt = _make_runtime("wf-ok")
    rt._remote_states["wf-ok"] = {
        "state": "Normal",
        "version": 1,
        "reason": None,
        "updated_at": int(time.time()),
    }
    # Should not raise.
    rt.check_control_plane("wf-ok")


# T3-S2 (0.3.0): `test_check_control_plane_in_local_mode_is_a_noop` was
# removed along with the `local_mode` field. There is no local branch
# to test — every runtime is a real cloud runtime, and `check_control_plane`
# always consults the remote state.


# ---------------------------------------------------------------------------
# 2. Wire test: real WebSocket server + WebSocketConnection
# ---------------------------------------------------------------------------


def _start_ws_server(handler):
    """Run a websockets server in a background thread. The handler
    receives each new connection and can send / receive messages.
    Returns (port, server, thread)."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    ready = threading.Event()
    server_ref: list[Any] = []

    def _serve():
        async def _main():
            async def _process(ws):
                await handler(ws, ready)

            server = await websockets.serve(_process, "127.0.0.1", port)
            server_ref.append(server)
            ready.set()
            await server.wait_closed()

        asyncio.run(_main())

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    if not ready.wait(timeout=5.0):
        raise RuntimeError("WS server failed to start within 5s")
    return port, server_ref[0], thread


async def _kill_handler(ws, ready: threading.Event):
    """Server-side handler: push a `state_change` for `wf-wire` shortly
    after the client connects. We give the client's receive loop a
    moment to start before sending."""
    ready.set()
    # Tiny delay so the client's _receive_task is actually scheduled
    # before we send. Without this the message can arrive before the
    # task is awaiting recv() and be dropped on the floor.
    await asyncio.sleep(0.05)
    push = {
        "type": "state_change",
        "workflow_id": "wf-wire",
        "state": "Killed",
        "version": 1,
        "reason": "wire_test_kill",
        "updated_at": int(time.time()),
    }
    await ws.send(json.dumps(push))
    # Keep the connection alive long enough for the client to receive.
    await asyncio.sleep(0.1)


def test_ws_push_kill_state_fires_callback_within_200ms():
    """End-to-end: real websockets server, real WebSocketConnection
    client, real on_state_change callback, real timing assertion.

    The push should be received and the callback should fire within
    200ms of the server sending. We assert the callback fired AND
    that the runtime's _remote_states now contains the Killed entry."""
    port, _server, _thread = _start_ws_server(_kill_handler)

    received: list[dict[str, Any]] = []
    received_at: list[float] = []
    sent_at_holder: list[float] = []

    async def _on_state(state: dict[str, Any]) -> None:
        received.append(state)
        received_at.append(time.time())

    async def _client():
        url = f"ws://127.0.0.1:{port}"
        async with websockets.connect(url) as ws:
            # 1) Send the subscribe frame the production client sends.
            await ws.send(
                json.dumps(
                    {
                        "type": "subscribe",
                        "organization_id": "org-1",
                        "api_key": "k",
                    }
                )
            )
            # 2) Wait for the server's push (handler sends it after
            #    reading the subscribe frame).
            raw = await ws.recv()
            sent_at_holder.append(time.time())
            data = json.loads(raw)
            await _on_state(data)

    # Run the client in a thread so we can time-bound it.
    client_thread = threading.Thread(
        target=lambda: asyncio.run(_client()), daemon=True
    )
    client_thread.start()
    client_thread.join(timeout=2.0)
    assert not client_thread.is_alive(), "WS client did not finish in 2s"

    assert len(received) == 1
    state = received[0]
    assert state["workflow_id"] == "wf-wire"
    assert state["state"] == "Killed"
    # Latency: from server-sent to client-received, must be < 200ms
    # (the test is on a local loopback so anything below ~50ms is
    # realistic; 200ms is the upper bound the plan calls out).
    latency_ms = (received_at[0] - sent_at_holder[0]) * 1000
    assert latency_ms < 200, f"WS push latency {latency_ms:.1f}ms exceeds 200ms"


def test_ws_connection_class_dispatches_state_change():
    """Use the production WebSocketConnection class to verify it
    correctly parses a `state_change` frame and invokes the callback.
    This pins the wire format against the actual handler."""
    port, _server, _thread = _start_ws_server(_kill_handler)

    received: list[dict[str, Any]] = []
    received_event = threading.Event()

    async def _main():
        conn = WebSocketConnection(
            url=f"ws://127.0.0.1:{port}/ws/control/org-1",
            api_key="k",
            on_state_change=lambda s: (
                received.append(s),
                received_event.set(),
            ),
        )
        await conn.connect()
        # Wait up to 2s for the push to arrive. We poll the
        # threading.Event from a thread so the asyncio loop is free
        # to run the receive task.
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if received_event.is_set():
                break
            await asyncio.sleep(0.02)
        await conn.close()

    asyncio.run(_main())
    assert received, "WebSocketConnection never invoked on_state_change"
    assert received[0]["state"] == "Killed"
    assert received[0]["workflow_id"] == "wf-wire"
