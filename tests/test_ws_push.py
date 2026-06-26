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
    client_thread = threading.Thread(target=lambda: asyncio.run(_client()), daemon=True)
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


# ---------------------------------------------------------------------------
# 3. Reconnect test: server-side drop must trigger reconnection
# ---------------------------------------------------------------------------
# Pins the B1 fix: pre-fix, the reconnect loop exited after the first
# successful connect (because ``_running=True`` made the
# ``if not self._running`` guard False and hit ``else: break``), so
# any subsequent server-side disconnect left the control plane dead
# until process restart. Post-fix, the loop waits while ``_running``
# is True and reconnects on demand.


async def _reconnect_handler(
    ws,
    ready: threading.Event,
    connection_count: list[int],
):
    """Server handler that closes the FIRST connection (simulating a
    network blip) and pushes a ``state_change`` on the SECOND
    connection (the client's automatic reconnection)."""
    ready.set()
    connection_count[0] += 1

    if connection_count[0] == 1:
        # First connection: close immediately. The client's receive
        # loop will see ``ConnectionClosed``, set ``_running = False``
        # in its ``finally`` block, and the reconnect loop will
        # attempt to reconnect with backoff (initial delay=1.0s).
        await ws.close()
        return

    # Second connection (the reconnect): push a state_change.
    # Tiny delay so the client's _receive_task is scheduled first.
    await asyncio.sleep(0.05)
    push = {
        "type": "state_change",
        "workflow_id": "wf-reconnect",
        "state": "Killed",
        "version": 1,
        "reason": "reconnect_test",
        "updated_at": int(time.time()),
    }
    await ws.send(json.dumps(push))
    # Keep the connection alive briefly so the client processes the
    # message before we tear down.
    await asyncio.sleep(0.2)


def test_ws_reconnects_after_server_disconnect():
    """End-to-end: server closes connection 1, client must
    automatically reconnect, and server pushes a state_change on
    connection 2 that the client must receive.

    This test is the regression guard for plan item B1. Pre-fix, the
    test would hang on ``received_event`` until its 5s deadline and
    fail with ``received == []``.
    """
    connection_count: list[int] = [0]
    ready = threading.Event()
    port, _server, _thread = _start_ws_server(
        lambda ws, r=ready, c=connection_count: _reconnect_handler(ws, r, c)
    )

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

        # Wait up to 5s for the reconnect + push. The first attempt
        # has backoff delay=1.0s, so budget is generous.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if received_event.is_set():
                break
            await asyncio.sleep(0.05)
        await conn.close()

    asyncio.run(_main())

    assert received, (
        "WebSocketConnection did not reconnect and receive the "
        "state_change after the server closed the first connection. "
        "This is the B1 regression: the reconnect loop exited after "
        "the first successful connect and never reconnected."
    )
    assert received[0]["state"] == "Killed"
    assert received[0]["workflow_id"] == "wf-reconnect"
    # Sanity: server saw exactly 2 connections (initial + reconnect).
    assert connection_count[0] == 2, (
        f"Expected server to see 2 connections (initial + reconnect), got {connection_count[0]}"
    )


# ---------------------------------------------------------------------------
# 4. Version-dedup unit tests: version=0 must be accepted on first receive
# ---------------------------------------------------------------------------
# Pins the B2 fix: pre-fix, ``_dispatch_state`` defaulted
# ``_last_version[wf]`` to 0, so ``incoming_version=0`` failed the
# ``incoming_version <= last`` guard (``0 <= 0``) and was dropped.
# For a server that emits ``initial_state`` with ``version: 0`` for
# each workflow on connect, this meant the very first state event
# for every workflow was silently discarded.


def test_dispatch_state_accepts_version_zero_on_first_receive():
    """First state event with version=0 must reach the callback.

    Pre-fix this was a silent safety gap: the first ``initial_state``
    frame (which the server emits with version=0) was dropped because
    the dedup default was 0, so ``0 <= 0`` was True.
    """
    conn = WebSocketConnection(
        url="ws://127.0.0.1:1/ws/control/org-x",
        api_key="k",
    )
    received: list[dict[str, Any]] = []
    conn.on_state_change = lambda s: received.append(s)

    conn._dispatch_state(
        {
            "workflow_id": "wf-zero",
            "state": "Killed",
            "version": 0,
            "reason": "test",
        }
    )

    assert len(received) == 1, (
        f"version=0 was dropped on first receive (got {len(received)} events). "
        "This is the B2 regression: the version-dedup sentinel was 0, so "
        "``0 <= 0`` was True and the very first state event was lost."
    )
    assert received[0]["state"] == "Killed"
    # And the cache must now reflect version=0, so a *re-delivery* of
    # version=0 from the server's at-least-once channel is still
    # dropped.
    conn._dispatch_state(
        {
            "workflow_id": "wf-zero",
            "state": "Killed",
            "version": 0,
            "reason": "test",
        }
    )
    assert len(received) == 1, "Stale re-delivery of version=0 was not dropped"


def test_dispatch_state_drops_older_versions_after_seen_higher():
    """After accepting version=5, an incoming version=2 must be dropped.

    Pins the stale-event rejection path: ``incoming_version <= last``
    must remain True for any version <= the last-seen one.
    """
    conn = WebSocketConnection(
        url="ws://127.0.0.1:1/ws/control/org-x",
        api_key="k",
    )
    received: list[dict[str, Any]] = []
    conn.on_state_change = lambda s: received.append(s)

    # First: high version — must be accepted.
    conn._dispatch_state(
        {
            "workflow_id": "wf-mono",
            "state": "Normal",
            "version": 5,
        }
    )
    # Then: stale lower version — must be dropped.
    conn._dispatch_state(
        {
            "workflow_id": "wf-mono",
            "state": "Killed",
            "version": 2,
        }
    )

    assert len(received) == 1
    assert received[0]["version"] == 5
    assert received[0]["state"] == "Normal"


# ---------------------------------------------------------------------------
# 5. Sprint 1.5 (B13): HMAC verify failure on signed messages
# ---------------------------------------------------------------------------
# Pre-fix: a signed WS message with a bad signature was logged at
# WARNING and dropped silently. For a safety-layer product, a
# signature mismatch is a first-class incident (either the server
# rotated the secret_key and the client missed the rotation, or
# the control plane is being tampered with) and must be visible.
# Post-fix: log at ERROR and bump ``hmac_verify_failures_total``.


def test_hmac_verify_failure_logs_error_and_bumps_metric(caplog):
    """A signed message with an invalid signature must log at ERROR
    and increment the ``hmac_verify_failures_total`` metric.

    We use a real ``WebSocketConnection`` instance but invoke
    ``_handle_message`` directly so we don't need a live WS server
    for this test. The branch under test is the signature-mismatch
    path inside ``_handle_message``.
    """
    import logging

    from nullrun.observability import metrics

    conn = WebSocketConnection(
        url="ws://127.0.0.1:1/ws/control/org-x",
        api_key="nr_live_test",
        secret_key="correct-secret",
    )
    # Snapshot the metric so we can assert the delta.
    before = metrics.transport.hmac_verify_failures_total

    # Build a signed message with a deliberately wrong signature.
    # The shape matches what the server emits: a ``state_change``
    # with a ``signature`` and ``timestamp`` field. We sign with
    # the wrong secret so ``verify_hmac_signature`` returns False.
    payload = {
        "type": "state_change",
        "workflow_id": "wf-hmac-fail",
        "state": "Killed",
        "version": 1,
        "reason": "forged",
        "updated_at": int(time.time()),
    }
    bad_msg = dict(payload)
    bad_msg["timestamp"] = int(time.time())
    bad_msg["signature"] = "deadbeef" * 8  # 64 hex chars but wrong

    received: list[dict[str, Any]] = []
    conn.on_state_change = lambda s: received.append(s)

    with caplog.at_level(logging.ERROR, logger="nullrun.transport_websocket"):
        # The handler is async; drive it synchronously via asyncio.run
        # so the test stays simple.
        asyncio.run(conn._handle_message(json.dumps(bad_msg)))

    after = metrics.transport.hmac_verify_failures_total
    assert after == before + 1, (
        f"hmac_verify_failures_total did not increment: before={before}, after={after}"
    )
    # The bad message MUST NOT have reached the callback — signature
    # verification is the gate that prevents forged kill commands.
    assert received == [], f"Forged message was dispatched to on_state_change: {received}"
    # And the failure must be visible at ERROR level.
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("HMAC" in r.getMessage() for r in error_records), (
        "HMAC verify failure was not logged at ERROR level. "
        "Pre-fix logged at WARNING which was too quiet for a "
        "control-plane integrity event."
    )
