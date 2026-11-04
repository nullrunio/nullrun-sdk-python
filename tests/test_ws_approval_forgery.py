"""tests/test_ws_approval_forgery.py — a forged approval frame must not
release a parked gate.

SDK-5 (reliability audit 2026-11-02).

The defect
----------
The HMAC on the control-plane WebSocket covers the bytes inside
``signed_payload`` — NOT the flattened outer wire body. The outer body
is therefore attacker-writable: anyone who can inject bytes into the
socket may rewrite ``type``, ``outcome``, ``execution_id`` … while
keeping a captured ``(signed_payload, signature)`` pair intact. The
signature check still PASSES, because it verifies bytes the attacker
never touched.

The ``approval_resolved`` branch read its fields from the OUTER body
and handed that dict to ``on_approval_resolved``. In the runtime that
callback pops the pending approval and sets its ``threading.Event`` —
releasing a gate that was parked waiting for a human.

The fix
-------
1. ONE source of truth: the branch type and every field are read from
   the verified ``signed_payload``; the outer body is a fallback only
   for frames that could not be authenticated at all.
2. The privileged branch requires an AUTHENTICATED frame. A frame the
   SDK cannot authenticate never releases a gate — the waiter stays
   parked and fails closed on the approval timeout.

WHAT THIS FILE DRIVES
---------------------
The real ``WebSocketConnection._handle_message`` with the real
envelope shape the backend produces (``SignedWsMessage``: the payload
is flattened into the outer body AND published hex-encoded as
``signed_payload``). A source-pin could not prove the forgery is
neutralised — the whole point is which dict the dispatcher reads.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest

from nullrun.observability import metrics
from nullrun.runtime import (
    NullRunApprovalDeniedError,
    NullRunApprovalExpiredError,
    NullRunRuntime,
)
from nullrun.transport import generate_hmac_signature
from nullrun.transport_websocket import WebSocketConnection

API_KEY = "nr_live_test_key"
SECRET = "s3cret"
# The backend mints the reservation id as a UUID (RFC-4122 v7).
RESERVATION_ID = "018f3c5a-7b2d-7c4e-9f10-2b3c4d5e6f70"

# The genuine, server-signed payload: the operator DENIED this call.
SIGNED_DENIAL: dict[str, Any] = {
    "type": "approval_resolved",
    "approval_id": "ap-1",
    "workflow_id": "wf-1",
    "execution_id": "ex-1",
    "outcome": "denied",
    "note": "not this one",
    "resolved_at": 1730000000,
    "message_id": "m-1",
}

# The genuine, server-signed payload: the operator APPROVED this call.
SIGNED_APPROVAL: dict[str, Any] = {
    "type": "approval_resolved",
    "approval_id": "ap-1",
    "workflow_id": "wf-1",
    "execution_id": "ex-1",
    "outcome": "approved",
    "note": "ok",
    "resolved_at": 1730000000,
    "message_id": "m-1",
}


def _signed_envelope(message: dict, api_key: str, secret_key: str) -> dict:
    """The server's ``SignedWsMessage`` shape.

    The payload is flattened into the outer body AND published
    hex-encoded as ``signed_payload``; the HMAC covers the INNER
    payload bytes only. That asymmetry is the whole attack surface.
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
    on_approval_resolved=None,
    on_state_change=None,
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


def _park_a_gate(
    rt: NullRunRuntime, approval_id: str = "ap-1"
) -> tuple[threading.Event, dict]:
    """Register a pending approval the way ``_wait_for_approval_resolution``
    does.

    The runtime POPS the entry from ``_approval_pending`` and stashes the
    outcome on the entry dict itself (the waiter holds the reference), so
    both the event and the entry are handed back.
    """
    event = threading.Event()
    entry = {
        "approval_id": approval_id,
        "workflow_id": "wf-1",
        "execution_id": "ex-1",
        "event": event,
        "timeout_seconds": 300,
    }
    with rt._approval_lock:
        rt._approval_pending[approval_id] = entry
    return event, entry


class TestTheOuterBodyIsNotATrustedSource:
    """The signature covers ``signed_payload``, not the outer body."""

    @pytest.mark.asyncio
    async def test_a_spliced_outer_body_cannot_flip_a_denial_into_an_approval(self):
        """The attacker rewrites the OUTER body only.

        The signature verifies (it covers bytes the attacker did not
        touch), so the frame is accepted — and the SDK must dispatch
        the SIGNED outcome, not the spliced one.
        """
        received: list[dict] = []
        conn, stub = _conn(on_approval_resolved=received.append)

        envelope = _signed_envelope(SIGNED_DENIAL, API_KEY, SECRET)
        # The splice: the outer body now says "approved".
        envelope["outcome"] = "approved"
        envelope["note"] = "approved by the operator"
        envelope["message_id"] = "m-attacker"

        await conn._handle_message(json.dumps(envelope))

        assert len(received) == 1, "the signed frame must still be dispatched"
        assert received[0]["outcome"] == "denied", (
            "the OUTER body decided the outcome — the signed payload "
            f"said 'denied', the SDK dispatched {received[0]['outcome']!r}"
        )
        assert received[0]["note"] == "not this one", (
            "the note came from the unsigned outer body"
        )
        # The acknowledgement correlates on the SIGNED message id.
        acks = [json.loads(s) for s in stub.sent]
        assert acks, "no acknowledgement was sent for a genuine signed frame"
        assert all(a.get("message_id") == "m-1" for a in acks), (
            "the acknowledgement correlated on the unsigned outer body's "
            f"message_id — got {acks}"
        )

    @pytest.mark.asyncio
    async def test_the_branch_is_chosen_from_the_verified_payload(self):
        """ANY signed frame must not be relabelled as an approval.

        The attacker keeps a captured ``(signed_payload, signature)``
        pair from a harmless frame and rewrites the outer ``type``.
        The branch must come from the signed payload.
        """
        received: list[dict] = []
        conn, _ = _conn(on_approval_resolved=received.append)

        envelope = _signed_envelope(
            {"type": "subscribed", "organization_id": "org-1"}, API_KEY, SECRET
        )
        # The relabel.
        envelope["type"] = "approval_resolved"
        envelope["approval_id"] = "ap-2"
        envelope["execution_id"] = "ex-2"
        envelope["outcome"] = "approved"

        await conn._handle_message(json.dumps(envelope))

        assert received == [], (
            "a signed 'subscribed' frame was dispatched as an approval — "
            "the branch was chosen from the unsigned outer body"
        )

    @pytest.mark.asyncio
    async def test_the_execution_id_comes_from_the_signed_payload(self):
        """The execution id decides WHICH reservation is released."""
        received: list[dict] = []
        conn, _ = _conn(on_approval_resolved=received.append)

        envelope = _signed_envelope(SIGNED_APPROVAL, API_KEY, SECRET)
        envelope["execution_id"] = "ex-attacker"

        await conn._handle_message(json.dumps(envelope))

        assert len(received) == 1
        assert received[0]["execution_id"] == "ex-1", (
            "the execution id came from the unsigned outer body — it "
            "decides which gate reservation is released"
        )


class TestAnUnauthenticatedFrameNeverReleasesAGate:
    """A frame the SDK cannot authenticate must not release a gate."""

    @pytest.mark.asyncio
    async def test_an_unsigned_approval_frame_is_refused(self):
        received: list[dict] = []
        conn, stub = _conn(on_approval_resolved=received.append)

        before = metrics.transport.ws_unverified_approval_total
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

        assert received == [], (
            "an UNSIGNED approval frame released a gate — the SDK cannot "
            "tell it from a genuine one, so it must refuse it"
        )
        assert metrics.transport.ws_unverified_approval_total == before + 1, (
            "the refusal must be counted — an operator has to be able to "
            "see it"
        )
        assert stub.sent == [], "a refused frame must not be ACKed"

    @pytest.mark.asyncio
    async def test_a_signed_but_tampered_approval_frame_is_refused(self):
        """A frame whose signature does not verify must not release a gate."""
        received: list[dict] = []
        conn, _ = _conn(on_approval_resolved=received.append)

        envelope = _signed_envelope(SIGNED_APPROVAL, API_KEY, SECRET)
        envelope["signature"] = "de" * 32  # 64 hex chars, wrong

        await conn._handle_message(json.dumps(envelope))

        assert received == [], "a frame with a bad signature released a gate"

    @pytest.mark.asyncio
    async def test_a_signed_frame_whose_payload_does_not_parse_is_dropped(self):
        """The signature verifies, but the signed bytes are not JSON.

        The outer body is NOT what was signed — the signature covers the
        bytes inside ``signed_payload`` — so the outer body is not a
        fallback source. The frame is dropped, and the drop is counted.
        """
        received: list[dict] = []
        conn, _ = _conn(on_approval_resolved=received.append)

        payload_bytes = b"this is not json"
        timestamp = int(time.time())
        envelope = {
            "type": "approval_resolved",
            "approval_id": "ap-1",
            "workflow_id": "wf-1",
            "execution_id": "ex-1",
            "outcome": "approved",
            "timestamp": timestamp,
            "api_key": API_KEY,
            "signature": generate_hmac_signature(
                API_KEY, SECRET, timestamp, payload_bytes
            ),
            "signed_payload": payload_bytes.hex(),
        }

        before = metrics.transport.hmac_verify_failures_total
        await conn._handle_message(json.dumps(envelope))

        assert received == [], (
            "a frame whose signed payload does not parse was dispatched from "
            "the outer body — the outer body is not what was signed"
        )
        assert metrics.transport.hmac_verify_failures_total == before + 1, (
            "the drop must be counted — an operator has to be able to see it"
        )


class TestTheParkedGateItself:
    """The end the audit names: a parked gate must not be released BY A FORGERY."""

    @pytest.mark.asyncio
    async def test_a_spliced_outer_body_cannot_release_the_gate_as_approved(self):
        """The signed payload says DENIED. The gate must wake up on THAT.

        A denial is a legitimate wake-up: the waiter raises
        ``NullRunApprovalDeniedError`` and the agent stops. What must
        never happen is the gate waking up on the spliced ``approved``.
        """
        rt = NullRunRuntime(api_key="test-key", _test_mode=True)
        try:
            event, entry = _park_a_gate(rt)
            conn, _ = _conn(on_approval_resolved=rt._handle_approval_resolved)

            envelope = _signed_envelope(SIGNED_DENIAL, API_KEY, SECRET)
            envelope["outcome"] = "approved"  # the splice

            await conn._handle_message(json.dumps(envelope))

            assert entry["outcome"] == "denied", (
                "the parked gate was released with the SPLICED outcome "
                f"({entry['outcome']!r}) — the operator denied this call"
            )
            assert entry["note"] == "not this one"
            assert event.is_set(), (
                "a signed denial must still wake the waiter — it is the "
                "fail-CLOSED wake-up"
            )
        finally:
            rt.shutdown()

    @pytest.mark.asyncio
    async def test_an_unsigned_approval_does_not_release_the_parked_gate(self):
        """An unauthenticated frame never wakes the waiter at all.

        The gate stays parked; the waiter then fails closed on its own
        approval timeout (``NullRunApprovalExpiredError``).
        """
        rt = NullRunRuntime(api_key="test-key", _test_mode=True)
        try:
            event, _entry = _park_a_gate(rt)
            conn, _ = _conn(on_approval_resolved=rt._handle_approval_resolved)

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

            assert not event.is_set(), (
                "the parked gate was RELEASED by an unauthenticated frame — "
                "the approval was never given by the operator"
            )
            with rt._approval_lock:
                assert "ap-1" in rt._approval_pending, (
                    "the pending approval was consumed by an unauthenticated "
                    "frame"
                )
        finally:
            rt.shutdown()

    @pytest.mark.asyncio
    async def test_a_genuine_approval_releases_the_parked_gate(self):
        """The control: the real path still works."""
        rt = NullRunRuntime(api_key="test-key", _test_mode=True)
        try:
            event, entry = _park_a_gate(rt)
            conn, stub = _conn(on_approval_resolved=rt._handle_approval_resolved)

            envelope = _signed_envelope(SIGNED_APPROVAL, API_KEY, SECRET)
            await conn._handle_message(json.dumps(envelope))

            assert event.is_set(), (
                "a genuine, signed approval did NOT release the parked gate "
                "— the fix broke the feature it was meant to protect"
            )
            assert entry["outcome"] == "approved"
            assert entry["note"] == "ok"
            assert any(b'"type": "ack"' in s for s in stub.sent), (
                "the acknowledgement was not sent for a genuine approval"
            )
        finally:
            rt.shutdown()


class TestTheGateCallItself:
    """The end the audit names: a parked GATE must not be released."""

    @staticmethod
    def _runtime() -> NullRunRuntime:
        rt = NullRunRuntime(api_key="test-key", _test_mode=True)
        rt.workflow_id = "wf-1"
        rt.organization_id = "org-1"
        rt._transport.check = lambda req: {  # type: ignore[method-assign]
            "decision": "require_approval",
            "decision_source": "gateway",
            "approval_id": "ap-1",
            "approval_timeout_seconds": 1,
            "reservation_id": RESERVATION_ID,
        }
        return rt

    @staticmethod
    def _run_the_gate(rt: NullRunRuntime) -> dict[str, Any]:
        """Run the gate call on a thread (it blocks on the approval)."""
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                box["value"] = rt.check_workflow_budget()
            except BaseException as exc:  # noqa: BLE001 — the outcome IS the test
                box["error"] = exc

        t = threading.Thread(target=run, daemon=True)
        t.start()
        # wait until the gate has parked on the approval
        for _ in range(100):
            with rt._approval_lock:
                if "ap-1" in rt._approval_pending:
                    break
            time.sleep(0.02)
        box["thread"] = t
        return box

    @pytest.mark.asyncio
    async def test_a_forged_approval_does_not_release_the_gate(self):
        """A gate that got ``require_approval`` must not proceed."""
        rt = self._runtime()
        try:
            box = self._run_the_gate(rt)
            conn, _ = _conn(on_approval_resolved=rt._handle_approval_resolved)

            envelope = _signed_envelope(SIGNED_DENIAL, API_KEY, SECRET)
            envelope["outcome"] = "approved"  # the splice

            await conn._handle_message(json.dumps(envelope))
            box["thread"].join(timeout=10)

            assert "error" in box, (
                "the gate call PROCEEDED after a forged approval — the "
                "spend was authorised without the operator"
            )
            assert isinstance(box["error"], NullRunApprovalDeniedError), (
                f"expected the signed DENIAL to surface, got {box['error']!r}"
            )
        finally:
            rt.shutdown()

    @pytest.mark.asyncio
    async def test_an_unsigned_approval_does_not_release_the_gate(self):
        """Unauthenticated ⇒ the waiter times out ⇒ fail-CLOSED."""
        rt = self._runtime()
        try:
            box = self._run_the_gate(rt)
            conn, _ = _conn(on_approval_resolved=rt._handle_approval_resolved)

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
            box["thread"].join(timeout=15)

            assert "error" in box, (
                "the gate call PROCEEDED after an unauthenticated approval"
            )
            assert isinstance(box["error"], NullRunApprovalExpiredError), (
                f"expected the approval to time out, got {box['error']!r}"
            )
        finally:
            rt.shutdown()

    @pytest.mark.asyncio
    async def test_a_genuine_approval_releases_the_gate(self):
        """The control: the real path still works end to end."""
        rt = self._runtime()
        try:
            box = self._run_the_gate(rt)
            conn, _ = _conn(on_approval_resolved=rt._handle_approval_resolved)

            envelope = _signed_envelope(SIGNED_APPROVAL, API_KEY, SECRET)
            await conn._handle_message(json.dumps(envelope))
            box["thread"].join(timeout=10)

            assert "error" not in box, (
                f"a genuine, signed approval did NOT release the gate: "
                f"{box.get('error')!r}"
            )
            assert box.get("value") == RESERVATION_ID, (
                f"the gate call returned {box.get('value')!r} — the "
                "reservation the backend minted"
            )
        finally:
            rt.shutdown()


class TestTheKillSwitchStillWorks:
    """The safety direction must not regress.

    A kill/pause is delivered on the SAME socket. Refusing to deliver
    it would be the dangerous direction, so an unauthenticated
    ``state_change`` is still dispatched — it can only stop the agent,
    never release it.
    """

    @pytest.mark.asyncio
    async def test_an_unsigned_state_change_is_still_delivered(self):
        received: list[dict] = []
        conn, _ = _conn(api_key="k", secret_key=None, on_state_change=received.append)

        await conn._handle_message(
            json.dumps(
                {
                    "type": "state_change",
                    "workflow_id": "wf-1",
                    "state": "Killed",
                    "version": 1,
                }
            )
        )

        assert len(received) == 1, (
            "the kill switch stopped working — a kill must be delivered "
            "even when the frame carries no signature"
        )
        assert received[0]["state"] == "Killed"
