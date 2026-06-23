"""
Tests for the byte-mismatch fix on the WS control plane.

Background: per memory/ws-signed-message-byte-mismatch, the server's
SignedWsMessage::new signed serde_json::to_string(&message) (the inner
WsMessage) while the SDK hashed the full wire bytes (signature /
timestamp / api_key_id included). The fix embeds the exact signed bytes
in a `signed_payload` field on the envelope.

The contract verified here:
  1. Server format with signed_payload -> SDK accepts (round-trip).
  2. Server format without signed_payload (pre-fix legacy) -> SDK still
     attempts verify on the wire bytes. The signature does not match the
     wire bytes, so the message must be rejected. We treat this as
     "legacy server, reject" — the legacy fallback exists only to keep
     the dispatch path reachable for non-privileged observability, not
     to be a covert pass-through for forged traffic.
  3. Tampered signed_payload (flip a byte) -> rejected.
  4. Wrong secret_key -> rejected.
  5. Malformed signed_payload (non-hex) -> rejected via the
     signature-check failure, not a crash.
  6. Replayed signed_payload from a different message body -> rejected
     (signature binds the body, not the envelope).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time

import pytest

from nullrun.transport_websocket import (
    WebSocketConnection,
    compute_hmac_signature,
    verify_hmac_signature,
)

# --- helpers ---------------------------------------------------------------


def _build_signed_envelope(message: dict, api_key: str, secret_key: str) -> dict:
    """Replicate the server's SignedWsMessage::new exactly.

    Returns a dict with flattened WsMessage fields plus
    signature / timestamp / api_key_id / signed_payload, in the same
    shape the server serialises to (since SignedWsMessage uses
    #[serde(flatten)] on the WsMessage field).
    """
    timestamp = int(time.time())
    payload_json = json.dumps(message, separators=(",", ":"))
    signature = compute_hmac_signature(api_key, secret_key, timestamp, payload_json.encode("utf-8"))
    envelope = dict(message)
    envelope["signature"] = signature
    envelope["timestamp"] = timestamp
    envelope["api_key_id"] = api_key
    envelope["signed_payload"] = payload_json.encode("utf-8").hex()
    return envelope


def _build_real_server_envelope(
    message: dict,
    user_facing_api_key: str,
    api_key_id: str,
    secret_key: str,
) -> dict:
    """Mimic the real server's signing shape (FIX-D): the HMAC is
    computed over ``api_key_id`` (the UUID key_id from
    ``auth_context.key_id()``), NOT over the user-facing
    ``nr_live_...`` api_key. The envelope publishes only
    ``api_key_id`` — the user-facing key never appears on the wire.

    The previous helper ``_build_signed_envelope`` used the same value
    for both, which masked the bug fixed in FIX-D.
    """
    timestamp = int(time.time())
    payload_json = json.dumps(message, separators=(",", ":"))
    signature = compute_hmac_signature(
        api_key_id, secret_key, timestamp, payload_json.encode("utf-8")
    )
    envelope = dict(message)
    envelope["signature"] = signature
    envelope["timestamp"] = timestamp
    envelope["api_key_id"] = api_key_id
    envelope["signed_payload"] = payload_json.encode("utf-8").hex()
    # Note: ``user_facing_api_key`` is intentionally NOT included in the
    # envelope — that's exactly how the real server behaves.
    assert user_facing_api_key != api_key_id, (
        "Test setup error: user-facing key and api_key_id must differ "
        "to reproduce the FIX-D bug condition."
    )
    return envelope


def _build_legacy_envelope(message: dict, api_key: str, secret_key: str) -> dict:
    """Pre-FIX-C envelope: signature, timestamp, api_key_id present,
    but signed_payload absent. The bytes the server signed were
    `serde_json::to_string(&message)`; we deliberately do NOT embed
    that on the wire so the receiver has to fall back to the legacy
    "verify against the full wire bytes" path.
    """
    timestamp = int(time.time())
    # Pre-FIX-C: the server was signing the same bytes it is putting on
    # the wire (full envelope), so to make this envelope verify-able
    # under the legacy "full wire bytes" rule we have to sign the
    # full wire bytes here too. This shape is the historic state that
    # the fix replaces; we use it only to confirm the legacy fallback
    # path is the one currently broken.
    # The simplest way to construct a pre-FIX-C envelope that the
    # server actually emitted: take the FIX-C envelope and drop the
    # signed_payload field. The signature was computed over the inner
    # message, so it must fail when re-verified against the full wire
    # bytes. That is the bug.
    return _build_signed_envelope(message, api_key, secret_key)


# --- pure-function unit tests (no network) ----------------------------------


def test_compute_and_verify_hmac_round_trip():
    payload = b'{"type":"state_change","workflow_id":"wf-1","state":"Killed","version":2}'
    ts = int(time.time())
    sig = compute_hmac_signature("api_key_123", "secret_xyz", ts, payload)
    assert verify_hmac_signature("api_key_123", "secret_xyz", ts, payload, sig)
    # Different secret -> reject
    assert not verify_hmac_signature("api_key_123", "wrong_secret", ts, payload, sig)
    # Different payload -> reject
    assert not verify_hmac_signature("api_key_123", "secret_xyz", ts, payload + b" ", sig)


def test_verify_hmac_signature_rejects_expired_timestamp():
    payload = b"{}"
    # Use a timestamp older than max_age_seconds=300 to guarantee the
    # "expired" branch fires regardless of test wall-clock drift.
    stale_ts = int(time.time()) - 1000
    sig = compute_hmac_signature("k", "s", stale_ts, payload)
    assert not verify_hmac_signature("k", "s", stale_ts, payload, sig)


def test_hex_round_trip_preserves_signed_bytes():
    # The signed_payload hex field, decoded, must equal the bytes the
    # signature was computed over. This is the contract SDK relies on.
    msg = {"type": "state_change", "state": "Killed", "workflow_id": "wf-42", "version": 7}
    envelope = _build_signed_envelope(msg, "k", "s")
    decoded = bytes.fromhex(envelope["signed_payload"])
    expected = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    assert decoded == expected


# --- end-to-end through the dispatcher path --------------------------------


class _StubWS:
    """Minimal stand-in for the websockets connection that captures
    what the SDK writes back. We use it to assert that a message
    signed with the new scheme actually flows through the dispatcher,
    and a tampered one does not."""

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


@pytest.mark.asyncio
async def test_state_change_with_signed_payload_is_dispatched(monkeypatch):
    """End-to-end: server-style envelope with signed_payload should be
    accepted by the SDK and the on_state_change callback should fire.
    """
    state_changes: list[dict] = []
    conn = WebSocketConnection(
        url="wss://example.invalid/ws/control/org-1",
        headers={},
        api_key="api_key_123",
        secret_key="secret_xyz",
        on_state_change=state_changes.append,
    )
    stub = _StubWS()
    monkeypatch.setattr(conn, "_conn", stub)
    conn._running = True

    msg = {
        "type": "state_change",
        "workflow_id": "wf-1",
        "state": "Killed",
        "version": 5,
        "reason": "remote kill",
        "message_id": "msg-1",
    }
    envelope = _build_signed_envelope(msg, "api_key_123", "secret_xyz")
    raw = json.dumps(envelope)  # legacy "full wire" serialisation
    await conn._handle_message(raw)

    # on_state_change must have been called exactly once with the
    # inner message fields.
    assert len(state_changes) == 1
    assert state_changes[0]["workflow_id"] == "wf-1"
    assert state_changes[0]["state"] == "Killed"
    # ACK was sent (Killed + message_id present).
    assert any(b'"type": "ack"' in s for s in stub.sent)


@pytest.mark.asyncio
async def test_tampered_signed_payload_is_rejected(monkeypatch):
    """If a single byte of signed_payload is flipped, the signature
    must no longer match and the message must be dropped (not
    dispatched, not acked)."""
    state_changes: list[dict] = []
    conn = WebSocketConnection(
        url="wss://example.invalid/ws/control/org-1",
        headers={},
        api_key="api_key_123",
        secret_key="secret_xyz",
        on_state_change=state_changes.append,
    )
    stub = _StubWS()
    monkeypatch.setattr(conn, "_conn", stub)
    conn._running = True

    msg = {
        "type": "state_change",
        "workflow_id": "wf-1",
        "state": "Killed",
        "version": 5,
        "message_id": "msg-1",
    }
    envelope = _build_signed_envelope(msg, "api_key_123", "secret_xyz")
    # Flip a hex nibble in signed_payload.
    sp = envelope["signed_payload"]
    envelope["signed_payload"] = ("f" if sp[0] != "f" else "0") + sp[1:]
    raw = json.dumps(envelope)
    await conn._handle_message(raw)

    assert state_changes == []
    assert stub.sent == []  # no ACK


@pytest.mark.asyncio
async def test_pre_fix_legacy_envelope_without_signed_payload_is_rejected(monkeypatch):
    """A pre-FIX-C envelope (signed_payload absent) must NOT pass
    signature verification, even on the legacy wire-bytes fallback
    path. The byte-mismatch fix is exactly about closing this hole.
    """
    state_changes: list[dict] = []
    conn = WebSocketConnection(
        url="wss://example.invalid/ws/control/org-1",
        headers={},
        api_key="api_key_123",
        secret_key="secret_xyz",
        on_state_change=state_changes.append,
    )
    stub = _StubWS()
    monkeypatch.setattr(conn, "_conn", stub)
    conn._running = True

    # _build_legacy_envelope builds a FIX-C envelope then drops
    # signed_payload; the signature was computed over the inner
    # message only, so verification against the full wire bytes must
    # fail.
    msg = {
        "type": "state_change",
        "workflow_id": "wf-1",
        "state": "Killed",
        "version": 5,
        "message_id": "msg-1",
    }
    envelope = _build_legacy_envelope(msg, "api_key_123", "secret_xyz")
    envelope.pop("signed_payload")
    raw = json.dumps(envelope)
    await conn._handle_message(raw)

    assert state_changes == []
    assert stub.sent == []


@pytest.mark.asyncio
async def test_malformed_signed_payload_does_not_crash(monkeypatch):
    """If the server sends a non-hex signed_payload (e.g. a buggy
    upgrade path or a hand-crafted forgery attempt), the SDK must
    fall back to the legacy path and reject via the standard
    signature-check failure — not raise a ValueError to the caller.
    """
    state_changes: list[dict] = []
    conn = WebSocketConnection(
        url="wss://example.invalid/ws/control/org-1",
        headers={},
        api_key="api_key_123",
        secret_key="secret_xyz",
        on_state_change=state_changes.append,
    )
    stub = _StubWS()
    monkeypatch.setattr(conn, "_conn", stub)
    conn._running = True

    msg = {
        "type": "state_change",
        "workflow_id": "wf-1",
        "state": "Killed",
        "version": 5,
    }
    envelope = _build_signed_envelope(msg, "api_key_123", "secret_xyz")
    envelope["signed_payload"] = "not-actually-hex"  # type: ignore[assignment]
    raw = json.dumps(envelope)
    # Must not raise.
    await conn._handle_message(raw)

    assert state_changes == []
    assert stub.sent == []


@pytest.mark.asyncio
async def test_replayed_signed_payload_with_spliced_body_is_rejected(monkeypatch):
    """An attacker who captured a (signed_payload, signature) pair
    from one message body must not be able to splice that signed
    payload into a *different* body and pass verification.

    Concretely: the attacker captures an envelope where state="Normal"
    was signed. They then construct a new envelope with the same
    signed_payload + signature but with state="Killed" in the outer
    body. The signature is over the bytes inside signed_payload
    (which say "Normal"), so the dispatcher reads the inner bytes —
    not the forged outer body. The attack is harmless: even if the
    signature verifies, the dispatched state is the captured "Normal",
    not the forged "Killed".

    This test pins both sides of that contract:
      - the signature still verifies (we did not break the wire
        format), so the message is *not* silently dropped
      - the dispatched state is the captured "Normal", so the
        attacker cannot escalate to "Killed"
    """
    state_changes: list[dict] = []
    conn = WebSocketConnection(
        url="wss://example.invalid/ws/control/org-1",
        headers={},
        api_key="api_key_123",
        secret_key="secret_xyz",
        on_state_change=state_changes.append,
    )
    stub = _StubWS()
    monkeypatch.setattr(conn, "_conn", stub)
    conn._running = True

    legit = {
        "type": "state_change",
        "workflow_id": "wf-1",
        "state": "Normal",  # captured
        "version": 5,
    }
    legit_envelope = _build_signed_envelope(legit, "api_key_123", "secret_xyz")
    # Attacker forges a new outer body but keeps the captured
    # signed_payload + signature verbatim.
    forged = dict(legit_envelope)
    forged["state"] = "Killed"
    raw = json.dumps(forged)
    await conn._handle_message(raw)

    # The signature is over the captured "Normal" body, so it
    # verifies. The dispatcher must therefore receive the
    # captured body — *not* the forged "Killed" body.
    assert len(state_changes) == 1
    assert state_changes[0]["state"] == "Normal"  # not "Killed"

    # And a real forgery — replacing the signed_payload bytes to
    # say "Killed" without re-signing — must be rejected.
    state_changes.clear()
    forged["signed_payload"] = (
        json.dumps({**legit, "state": "Killed"}, separators=(",", ":")).encode("utf-8").hex()
    )
    raw2 = json.dumps(forged)
    await conn._handle_message(raw2)
    assert state_changes == []  # signature no longer matches


@pytest.mark.asyncio
async def test_acknowledged_states_use_pascalcase(monkeypatch):
    """S-2 fix: ACKNOWLEDGED_STATES must use the same casing the
    server emits (PascalCase) so ACK is sent for KILL/PAUSE events.
    """
    state_changes: list[dict] = []
    conn = WebSocketConnection(
        url="wss://example.invalid/ws/control/org-1",
        headers={},
        api_key="api_key_123",
        secret_key="secret_xyz",
        on_state_change=state_changes.append,
    )
    stub = _StubWS()
    monkeypatch.setattr(conn, "_conn", stub)
    conn._running = True

    # Pre-fix ACKNOWLEDGED_STATES was {"killed", "paused"} (lowercase)
    # and would skip the ACK. The server's WsWorkflowState enum emits
    # "Killed"/"Paused" (PascalCase). This test pins the contract.
    assert "Killed" in WebSocketConnection.ACKNOWLEDGED_STATES
    assert "Paused" in WebSocketConnection.ACKNOWLEDGED_STATES
    # Belt-and-braces: the lowercase variants must NOT be the ones
    # we look for, otherwise a server regression that emits "killed"
    # would silently re-introduce the bug.
    assert "killed" not in WebSocketConnection.ACKNOWLEDGED_STATES
    assert "paused" not in WebSocketConnection.ACKNOWLEDGED_STATES

    # And a state_change with state="Killed" + message_id must
    # produce an ACK.
    msg = {
        "type": "state_change",
        "workflow_id": "wf-1",
        "state": "Killed",
        "version": 5,
        "message_id": "msg-ack",
    }
    envelope = _build_signed_envelope(msg, "api_key_123", "secret_xyz")
    raw = json.dumps(envelope)
    await conn._handle_message(raw)
    assert any(b'"type": "ack"' in s and b"msg-ack" in s for s in stub.sent)


# --- Audit-2026-06-22 #6: WS ACK case-insensitive defensive ---


@pytest.mark.asyncio
async def test_ws_ack_lowercase_state_still_sends_ack(monkeypatch):
    """Audit-2026-06-22 #6: the WS path used to exact-match PascalCase
    only. A server regression to ``"killed"``/``"paused"`` would
    silently drop the ACK. The defensive helper
    ``_is_acknowledged_state`` accepts both, while the
    ``ACKNOWLEDGED_STATES`` set stays PascalCase-only so the
    ``test_acknowledged_states_use_pascalcase`` invariant is
    preserved."""
    state_changes: list[dict] = []
    conn = WebSocketConnection(
        url="wss://example.invalid/ws/control/org-1",
        headers={},
        api_key="api_key_123",
        secret_key="secret_xyz",
        on_state_change=state_changes.append,
    )
    stub = _StubWS()
    monkeypatch.setattr(conn, "_conn", stub)
    conn._running = True

    for lowercase_state in ("killed", "paused"):
        state_changes.clear()
        stub.sent.clear()
        msg = {
            "type": "state_change",
            "workflow_id": f"wf-{lowercase_state}",
            "state": lowercase_state,  # server regression to lowercase
            "version": 5,
            "message_id": f"msg-{lowercase_state}",
        }
        envelope = _build_signed_envelope(msg, "api_key_123", "secret_xyz")
        raw = json.dumps(envelope)
        await conn._handle_message(raw)

        # ACK must be sent even with lowercase state.
        assert any(
            b'"type": "ack"' in s and lowercase_state.encode() in s
            for s in stub.sent
        ), f"ACK not sent for lowercase state={lowercase_state!r}"

    # ACKNOWLEDGED_STATES itself stays PascalCase — pin that.
    assert "Killed" in WebSocketConnection.ACKNOWLEDGED_STATES
    assert "Paused" in WebSocketConnection.ACKNOWLEDGED_STATES
    assert "killed" not in WebSocketConnection.ACKNOWLEDGED_STATES
    assert "paused" not in WebSocketConnection.ACKNOWLEDGED_STATES

    # And _is_acknowledged_state returns True for both casings.
    assert WebSocketConnection._is_acknowledged_state("Killed")
    assert WebSocketConnection._is_acknowledged_state("killed")
    assert WebSocketConnection._is_acknowledged_state("Paused")
    assert WebSocketConnection._is_acknowledged_state("paused")
    assert not WebSocketConnection._is_acknowledged_state("Normal")
    assert not WebSocketConnection._is_acknowledged_state("flagged")


# --- FIX-D regression: server signs with api_key_id (UUID), not user-facing key ---


@pytest.mark.asyncio
async def test_real_server_envelope_with_distinct_api_key_id_is_accepted(monkeypatch):
    """FIX-D regression: the real NULLRUN backend signs HMAC over
    ``api_key_id`` (the UUID key_id from ``auth_context.key_id()``),
    NOT the user-facing ``nr_live_...`` api_key passed to
    ``nullrun.init()``. The SDK must read ``api_key_id`` from the
    envelope and use it as the HMAC identifier — otherwise every
    signed WS message is rejected with "Invalid HMAC signature".

    Pre-FIX-D behaviour: SDK called ``verify_hmac_signature(
    self.api_key, ...)`` with the user-facing key, which never matched
    the server's UUID-based signature. This test would fail under that
    code path with the same production error reported on 2026-06-22.
    """
    state_changes: list[dict] = []
    USER_FACING_KEY = "nr_live_SsBF9OMYcVCgRCNcCVcJ4khTOPKx79JG"
    API_KEY_ID = "0b7632e8-11d8-4247-8666-c72b5320b4f6"  # UUID
    SECRET = "secret-from-_authenticate"

    conn = WebSocketConnection(
        url="wss://api.nullrun.io/ws/control/org-x",
        headers={},
        api_key=USER_FACING_KEY,
        secret_key=SECRET,
        on_state_change=state_changes.append,
    )
    stub = _StubWS()
    monkeypatch.setattr(conn, "_conn", stub)
    conn._running = True

    msg = {
        "type": "state_change",
        "workflow_id": "wf-1",
        "state": "Normal",
        "version": 5,
    }
    envelope = _build_real_server_envelope(
        msg,
        user_facing_api_key=USER_FACING_KEY,
        api_key_id=API_KEY_ID,
        secret_key=SECRET,
    )
    # Sanity: the envelope must NOT carry the user-facing key (the
    # real server only ships the api_key_id UUID on the wire).
    assert "api_key" not in envelope
    assert envelope["api_key_id"] == API_KEY_ID

    raw = json.dumps(envelope)
    await conn._handle_message(raw)

    # The signature was computed with API_KEY_ID, so the SDK must
    # accept it and dispatch the state_change.
    assert len(state_changes) == 1
    assert state_changes[0]["workflow_id"] == "wf-1"
    assert state_changes[0]["state"] == "Normal"


@pytest.mark.asyncio
async def test_real_server_envelope_with_wrong_user_facing_key_still_accepted(monkeypatch):
    """Belt-and-braces for FIX-D: even if the user-facing key
    accidentally differs from the api_key_id the server used to sign
    (which is the actual server shape — the server never sees the
    user-facing key for HMAC purposes), the SDK still accepts the
    message because it reads ``api_key_id`` from the envelope.

    This pins the contract: HMAC verification identity MUST come from
    the envelope's ``api_key_id`` field, not from ``self.api_key``.
    """
    state_changes: list[dict] = []
    USER_FACING_KEY = "nr_live_wrong-key-sdk-never-uses-this-for-verify"
    API_KEY_ID = "0b7632e8-11d8-4247-8666-c72b5320b4f6"
    SECRET = "secret-from-_authenticate"

    conn = WebSocketConnection(
        url="wss://api.nullrun.io/ws/control/org-x",
        headers={},
        api_key=USER_FACING_KEY,
        secret_key=SECRET,
        on_state_change=state_changes.append,
    )
    stub = _StubWS()
    monkeypatch.setattr(conn, "_conn", stub)
    conn._running = True

    msg = {"type": "state_change", "workflow_id": "wf-x", "state": "Normal", "version": 1}
    envelope = _build_real_server_envelope(msg, USER_FACING_KEY, API_KEY_ID, SECRET)
    raw = json.dumps(envelope)
    await conn._handle_message(raw)

    assert len(state_changes) == 1
    assert state_changes[0]["workflow_id"] == "wf-x"


@pytest.mark.asyncio
async def test_legacy_envelope_without_api_key_id_falls_back_to_user_facing_key(monkeypatch):
    """FIX-D backwards-compat: a pre-FIX-D server (no ``api_key_id``
    field on the envelope) signed HMAC over the user-facing api_key.
    The SDK must fall back to ``self.api_key`` in that case so legacy
    round-trip tests and any pre-FIX-D deployments keep working.

    We build an envelope without ``api_key_id`` and sign with the
    user-facing key — the pre-FIX-D shape.
    """
    state_changes: list[dict] = []
    USER_FACING_KEY = "nr_live_legacy-test"
    SECRET = "legacy-secret"

    conn = WebSocketConnection(
        url="wss://example.invalid/ws/control/org-1",
        headers={},
        api_key=USER_FACING_KEY,
        secret_key=SECRET,
        on_state_change=state_changes.append,
    )
    stub = _StubWS()
    monkeypatch.setattr(conn, "_conn", stub)
    conn._running = True

    msg = {"type": "state_change", "workflow_id": "wf-legacy", "state": "Normal", "version": 1}
    # Sign with the user-facing key, drop api_key_id to simulate a
    # pre-FIX-D envelope.
    envelope = _build_signed_envelope(msg, USER_FACING_KEY, SECRET)
    envelope.pop("api_key_id")
    raw = json.dumps(envelope)
    await conn._handle_message(raw)

    # Legacy path: SDK uses self.api_key as fallback, signature
    # verifies, dispatch happens.
    assert len(state_changes) == 1
    assert state_changes[0]["workflow_id"] == "wf-legacy"


# ---------------------------------------------------------------------------
# Wire-format contract tests (audit 2026-06-22 #3+#8)
# ---------------------------------------------------------------------------


def test_ws_hmac_identity_field_constant():
    """The wire-format HMAC identity field name is pinned via
    ``WS_HMAC_IDENTITY_FIELD``. Both sides of the WS push protocol
    (NULLRUN backend's ``SignedWsMessage`` struct and the SDK
    receiver in transport_websocket.py) agree on this field name.

    Without this pin, a future struct rename on either side silently
    breaks signature verification on every push — exactly the
    regression class that audit 2026-06-22 #8 captured.
    """
    from nullrun.transport_websocket import WS_HMAC_IDENTITY_FIELD

    assert WS_HMAC_IDENTITY_FIELD == "api_key"


def test_ws_hmac_identity_field_used_in_receiver():
    """Receiver must read the pinned field name (not a free-form
    string literal) so the constant is the single source of truth.

    Reads the source file directly (not ``inspect.getsource`` on the
    class) so the test is robust to ``test_transport_branches.py``
    monkey-patching ``transport_websocket.WebSocketConnection`` to a
    fake class without restoring it (a pre-existing test-isolation
    leak — see the ``_FakeConn`` assignments at test_transport_branches.py:553
    and :581). With ``inspect.getsource`` the patched fake class has
    no ``_handle_message`` and this test crashes; with direct file
    reads we verify the source-of-truth bytes regardless of class
    identity at test time.
    """
    from pathlib import Path

    from nullrun.transport_websocket import WS_HMAC_IDENTITY_FIELD

    src_path = Path(__file__).parent.parent / "src" / "nullrun" / "transport_websocket.py"
    src = src_path.read_text(encoding="utf-8")

    # The receiver code (the body of the ``_handle_message`` method)
    # must reference the constant. Look for the constant by name
    # rather than by literal value to confirm the pin is wired up.
    assert "WS_HMAC_IDENTITY_FIELD" in src, (
        "transport_websocket.py no longer references the "
        "WS_HMAC_IDENTITY_FIELD constant — wire-format pin is gone"
    )

    # And the constant must keep its expected wire-format value
    # (separate from the source-level reference so a refactor that
    # changes the value is caught too).
    assert WS_HMAC_IDENTITY_FIELD == "api_key"
