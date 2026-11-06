"""P2-19 — a control-plane frame the client could have verified must be
verified, or refused.

The receiver verified the signature only when all four of these were
present: ``signature`` and ``timestamp`` on the frame, and ``api_key``
and ``secret_key`` on the client. A frame that was missing ANY one of
them skipped the verification entirely and was then read from its outer
body — the body the server never signed.

The hole that matters: a client that HOLDS the API key and the HMAC
secret can verify every frame the server sends it, because the server
signs every frame it sends for such a key
(``ws_control.rs::send_signed_or_raw``). A frame that arrives at such a
client without a signature — or with a signature but no timestamp — did
not come from the server. It must be refused, not delivered.

The other side of the contract, and the reason this is not a blanket
"unsigned means forged": a client WITHOUT a secret (a legacy key) can
verify nothing, so the unsigned path stays open for it. The kill switch
must keep working there — it can only stop the agent, never release it.

The cells drive the real ``WebSocketConnection._handle_message``.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from nullrun.observability import metrics
from nullrun.transport import generate_hmac_signature
from nullrun.transport_websocket import WebSocketConnection

API_KEY = "nr_live_test_key"
SECRET = "s3cret"

KILL_FRAME: dict[str, Any] = {
    "type": "state_change",
    "workflow_id": "wf-1",
    "state": "Killed",
    "version": 1,
    "message_id": "m-1",
}


def _signed_envelope(message: dict, api_key: str, secret_key: str) -> dict:
    """The server's ``SignedWsMessage`` shape.

    The payload is flattened into the outer body AND published
    hex-encoded as ``signed_payload``; the HMAC covers the INNER
    payload bytes only.
    """
    timestamp = int(time.time())
    payload_json = json.dumps(message, separators=(",", ":"), sort_keys=True)
    signature = generate_hmac_signature(
        api_key, secret_key, timestamp, payload_json.encode("utf-8")
    )
    envelope = dict(message)
    envelope["signature"] = signature
    envelope["timestamp"] = timestamp
    envelope["api_key"] = api_key
    envelope["signed_payload"] = payload_json.encode("utf-8").hex()
    return envelope


class _StubWS:
    """Captures what the SDK writes back (the ACK)."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.closed = False

    async def send(self, data) -> None:
        if isinstance(data, str):
            self.sent.append(data.encode("utf-8"))
        else:
            self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


def _conn(
    *,
    api_key: str | None = API_KEY,
    secret_key: str | None = SECRET,
    on_state_change=None,
    on_approval_resolved=None,
) -> tuple[WebSocketConnection, _StubWS]:
    conn = WebSocketConnection(
        url="wss://example.invalid/ws/control/org-1",
        headers={},
        api_key=api_key,
        secret_key=secret_key,
        on_state_change=on_state_change,
        on_approval_resolved=on_approval_resolved,
    )
    stub = _StubWS()
    conn._conn = stub
    conn._running = True
    return conn, stub


class TestAnUnsignedFrameIsRefusedWhenTheClientCanVerify:
    """This client holds the key and the secret, so it can verify."""

    @pytest.mark.asyncio
    async def test_a_frame_without_a_signature_is_refused(self):
        received: list[dict] = []
        conn, stub = _conn(on_state_change=received.append)

        before = metrics.transport.ws_unverified_frame_total
        await conn._handle_message(json.dumps(dict(KILL_FRAME)))

        assert received == [], (
            "an unsigned frame was delivered to a client that can verify "
            "signatures — it did not come from the server"
        )
        assert metrics.transport.ws_unverified_frame_total == before + 1, (
            "the refusal must be counted — an operator has to be able to "
            "see it"
        )
        assert stub.sent == [], "a refused frame must not be ACKed"

    @pytest.mark.asyncio
    async def test_a_frame_with_a_signature_but_no_timestamp_is_refused(self):
        """The audit's case: the frame is missing ONE field.

        Pre-fix the missing timestamp skipped the whole verification
        block, and the frame was read from its unsigned outer body.
        """
        received: list[dict] = []
        conn, _ = _conn(on_state_change=received.append)

        envelope = _signed_envelope(dict(KILL_FRAME), API_KEY, SECRET)
        del envelope["timestamp"]

        before = metrics.transport.ws_unverified_frame_total
        await conn._handle_message(json.dumps(envelope))

        assert received == [], (
            "a frame with a signature but no timestamp skipped "
            "verification and was delivered"
        )
        assert metrics.transport.ws_unverified_frame_total == before + 1

    @pytest.mark.asyncio
    async def test_a_frame_with_a_timestamp_but_no_signature_is_refused(self):
        received: list[dict] = []
        conn, _ = _conn(on_state_change=received.append)

        envelope = dict(KILL_FRAME)
        envelope["timestamp"] = int(time.time())

        before = metrics.transport.ws_unverified_frame_total
        await conn._handle_message(json.dumps(envelope))

        assert received == [], (
            "a frame with a timestamp but no signature skipped "
            "verification and was delivered"
        )
        assert metrics.transport.ws_unverified_frame_total == before + 1

    @pytest.mark.asyncio
    async def test_a_refused_frame_cannot_block_the_next_real_kill(self):
        """A forged frame must not poison the version dedup.

        The dispatcher drops a state event whose version is not newer
        than the last one it saw for that workflow. A forged frame with
        a huge version would raise that bar, and the next REAL kill
        would be dropped as stale — the forgery would silence the kill
        switch.
        """
        received: list[dict] = []
        conn, _ = _conn(on_state_change=received.append)

        forged = dict(KILL_FRAME)
        forged["version"] = 99
        forged["state"] = "Normal"
        await conn._handle_message(json.dumps(forged))

        genuine = _signed_envelope(dict(KILL_FRAME), API_KEY, SECRET)
        await conn._handle_message(json.dumps(genuine))

        assert len(received) == 1, (
            "the real kill never arrived — the forged frame raised the "
            "version bar and the genuine frame was dropped as stale"
        )
        assert received[0]["state"] == "Killed", (
            "the forged frame was delivered and the genuine kill was "
            f"dropped as stale — the SDK dispatched {received[0]!r}"
        )

    @pytest.mark.asyncio
    async def test_an_unsigned_approval_still_bumps_the_approval_counter(self):
        """The approval-specific signal survives the blanket refusal.

        The operator has to see that somebody pushed an approval this
        client cannot verify — that counter is the alertable one.
        """
        received: list[dict] = []
        conn, stub = _conn(on_approval_resolved=received.append)

        before = metrics.transport.ws_unverified_frame_total
        before_approval = metrics.transport.ws_unverified_approval_total
        await conn._handle_message(
            json.dumps(
                {
                    "type": "approval_resolved",
                    "approval_id": "ap-1",
                    "workflow_id": "wf-1",
                    "execution_id": "ex-1",
                    "outcome": "approved",
                }
            )
        )

        assert received == [], "an unsigned approval frame was dispatched"
        assert metrics.transport.ws_unverified_frame_total == before + 1, (
            "the refusal must be counted"
        )
        assert metrics.transport.ws_unverified_approval_total == before_approval + 1, (
            "the approval refusal must stay countable"
        )
        assert stub.sent == [], "a refused frame must not be ACKed"


class TestTheKillSwitchStillWorks:
    """The safety direction must not regress."""

    @pytest.mark.asyncio
    async def test_a_client_without_a_secret_still_receives_the_kill(self):
        """A legacy key has no HMAC secret, so nothing can be verified.

        Refusing the frame here would break the kill switch for every
        org on a legacy key. The frame can only stop the agent, never
        release it.
        """
        received: list[dict] = []
        conn, _ = _conn(api_key="k", secret_key=None, on_state_change=received.append)

        await conn._handle_message(json.dumps(dict(KILL_FRAME)))

        assert len(received) == 1, (
            "the kill switch stopped working — a kill must be delivered "
            "even when the frame carries no signature"
        )
        assert received[0]["state"] == "Killed"

    @pytest.mark.asyncio
    async def test_a_signed_kill_is_delivered(self):
        """The canary for the negative assertions above.

        The same client, the same frame, signed by the server: it must
        arrive.
        """
        received: list[dict] = []
        conn, stub = _conn(on_state_change=received.append)

        envelope = _signed_envelope(dict(KILL_FRAME), API_KEY, SECRET)
        await conn._handle_message(json.dumps(envelope))

        assert len(received) == 1, "a genuine signed kill was not delivered"
        assert received[0]["state"] == "Killed"
        assert received[0]["workflow_id"] == "wf-1"
        assert stub.sent, "a genuine killed frame must be ACKed"
