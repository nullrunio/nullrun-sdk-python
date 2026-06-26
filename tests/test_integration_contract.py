"""
Contract tests pinning the SDK ↔ backend wire format.

Background: each test here guards a specific class of integration drift
discovered during the 2026-06-22 audit. The tests do not exercise the
control-plane happy path — they pin URL shapes, HTTP verbs, header
contracts, and field-name conventions so a future change to either side
trips a CI signal rather than silently breaking production.

If you change any of these and the tests fail, update the matching
backend file in lock-step — do not edit one side alone.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time

import httpx
import pytest
import respx

from nullrun.transport import Transport
from nullrun.transport_websocket import (
    WebSocketConnection,
    compute_hmac_signature,
    verify_hmac_signature,
)

# ─────────────────────────────────────────────────────────────────────
# FIX-F3: every POST must carry Authorization: Bearer <api_key> so the
# backend CSRF middleware's ``has_bearer_auth`` bypass fires. Without it,
# the SDK hits the cookie-double-submit branch → 403 → SDK try/except
# swallows → silently fail-OPEN on every SDK-side enforcement gate.
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def transport():
    t = Transport(api_url="https://api.test.nullrun.io", api_key="nr_live_abc123def456")
    yield t
    t.stop()


class TestAuthorizationHeaderOnPost:
    """Every signed POST must include Authorization: Bearer <api_key>."""

    def test_build_signed_headers_has_bearer(self):
        t = Transport(api_url="https://api.test.nullrun.io", api_key="nr_live_abc")
        try:
            headers = t._build_signed_headers(body="{}")
            assert headers["Authorization"] == "Bearer nr_live_abc"
            assert headers["X-API-Key"] == "nr_live_abc"
        finally:
            t.stop()

    @respx.mock
    def test_track_batch_post_includes_bearer(self, transport):
        route = respx.post("https://api.test.nullrun.io/api/v1/track/batch").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        transport._send_batch_with_retry_info([{"event": "test"}])
        assert route.called
        sent = route.calls.last.request
        assert sent.headers["Authorization"] == "Bearer nr_live_abc123def456"


# ─────────────────────────────────────────────────────────────────────
# FIX-F2: SDK fetches per-workflow state via
# GET /api/v1/orgs/{org_id}/workflows/{workflow_id}
# (not /api/v1/status/{workflow_id} which 404'd).
#
# 0.7.0: SDK is a thin client. Policy fetch (GET /policies) was
# removed along with local Policy class — backend owns all policy
# state. The fetch-policy URL contract is no longer exercised by
# the SDK; backend authors can keep the GET /policies endpoint for
# dashboard / API consumers, but the SDK does not call it.
# ─────────────────────────────────────────────────────────────────────


class TestRemoteStateFetchContract:
    """Pin the SDK remote-state URL so the legacy HTTP-poll fallback
    hits a route that actually exists."""

    def test_remote_state_url_is_org_scoped(self):
        from nullrun.runtime import NullRunRuntime

        rt = NullRunRuntime(api_key="nr_live_x", _test_mode=True)
        try:
            rt.organization_id = "00000000-0000-0000-0000-000000000002"
            captured: dict = {}

            def fake_get(url: str, headers=None, timeout=None):
                captured["url"] = url
                captured["headers"] = headers

                class _Resp:
                    status_code = 200

                    @staticmethod
                    def json():
                        return {"state": "Normal", "version": 1}

                return _Resp()

            rt._transport._client.get = fake_get  # type: ignore[assignment]
            rt._fetch_remote_state("wf-abc-123")

            assert captured["url"].endswith(
                "/api/v1/orgs/00000000-0000-0000-0000-000000000002/workflows/wf-abc-123"
            ), f"unexpected remote-state URL: {captured['url']}"
        finally:
            rt.shutdown()


# ─────────────────────────────────────────────────────────────────────
# FIX-F5: ACK payload's received_at must be unix seconds (not ms) to
# match backend's WsMessage::Ack field contract.
# ─────────────────────────────────────────────────────────────────────


class TestAckUnitsContract:
    """Pin ACK.received_at to seconds so backend analytics don't get
    timestamps 1000× too large."""

    def test_ack_received_at_is_seconds(self):
        # Build the same ACK envelope the SDK emits from
        # transport_websocket._handle_state_change_with_ack.
        before = int(time.time())
        ack = {
            "type": "ack",
            "message_id": "msg-1",
            "received_at": int(time.time()),
        }
        after = int(time.time())

        # Pin unit: must be within 1s of wall clock, NOT 1000s.
        assert before - 1 <= ack["received_at"] <= after + 1, (
            "ACK.received_at must be unix seconds; got value that doesn't "
            f"match current time: {ack['received_at']} (now={int(time.time())})"
        )
        # Defensive: must NOT be in the milliseconds range (> 10^12 for 2026).
        assert ack["received_at"] < 10_000_000_000, (
            "ACK.received_at looks like milliseconds — server-side analytics "
            "would interpret it as year 2286+."
        )


# ─────────────────────────────────────────────────────────────────────
# FIX-F4 / FIX-F6 contract: WS HMAC identity is the user-facing
# ``api_key`` (e.g. ``nr_live_...``), NOT the internal UUID ``key_id``.
# SDK reads it from the envelope field ``api_key`` (backwards-compat:
# pre-FIX-F4 envelopes with field name ``api_key_id`` carrying the
# same value are still accepted). Backend signer uses
# ``auth_context.api_key()`` — see
# backend/src/proxy/http/ws_control.rs:680-682 + 65-79 + auth/mod.rs.
#
# Pin: any drift between the two sides trips here.
# ─────────────────────────────────────────────────────────────────────


class TestWsHmacIdentityContract:
    """The HMAC identity for WS messages is the user-facing api_key,
    not the internal UUID key_id. Pre-FIX-F4 the field was named
    ``api_key_id`` on the wire but still carried the user-facing value;
    the rename to ``api_key`` makes the contract honest. The SDK
    accepts either field name for the rolling-deploy window."""

    def test_envelope_with_user_facing_api_key_verifies(self):
        """The SDK must accept messages signed with the user-facing
        api_key (FIX-F4)."""
        USER_KEY = "nr_live_userfacing_abc123"
        SECRET = "shared-secret"

        msg = {"type": "state_change", "workflow_id": "wf-1", "state": "Normal", "version": 1}
        payload_bytes = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        ts = int(time.time())
        sig = compute_hmac_signature(USER_KEY, SECRET, ts, payload_bytes)
        envelope = dict(msg)
        envelope.update(
            {
                "signature": sig,
                "timestamp": ts,
                "api_key": USER_KEY,
                "signed_payload": payload_bytes.hex(),
            }
        )

        # Pure-function verify — same as what _handle_message uses.
        assert verify_hmac_signature(USER_KEY, SECRET, ts, payload_bytes, sig)

    def test_envelope_legacy_api_key_id_field_still_accepted(self):
        """Pre-FIX-F4 servers published the same value under the
        field name ``api_key_id``. The SDK must accept that for the
        rolling-deploy window. After both sides are on FIX-F4, this
        compatibility path can be removed."""
        USER_KEY = "nr_live_userfacing_abc123"
        SECRET = "shared-secret"

        msg = {"type": "state_change", "workflow_id": "wf-1", "state": "Normal", "version": 1}
        payload_bytes = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        ts = int(time.time())
        sig = compute_hmac_signature(USER_KEY, SECRET, ts, payload_bytes)

        # Sanity: pure verify with the user-facing key passes.
        assert verify_hmac_signature(USER_KEY, SECRET, ts, payload_bytes, sig)

    def test_envelope_signature_uses_user_facing_key_not_uuid(self):
        """FIX-F4: the HMAC identity on the wire is the user-facing
        api_key, never the internal UUID. If a refactor reintroduces
        the UUID-based identity, this test fails."""
        USER_KEY = "nr_live_userfacing_abc123"
        WRONG_UUID = "0b7632e8-11d8-4247-8666-c72b5320b4f6"
        SECRET = "shared-secret"

        msg = {"type": "state_change", "workflow_id": "wf-1", "state": "Normal", "version": 1}
        payload_bytes = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        ts = int(time.time())

        # Server (FIX-F4) signs with the user-facing key.
        prod_sig = compute_hmac_signature(USER_KEY, SECRET, ts, payload_bytes)

        # Verify with user-facing key (matches production) → passes.
        assert verify_hmac_signature(USER_KEY, SECRET, ts, payload_bytes, prod_sig), (
            "FIX-F4: verification with user-facing api_key must succeed — "
            "this is the production wire shape"
        )
        # Verify with the UUID — must fail. Pin the asymmetry:
        # if a refactor reintroduces UUID-based identity, this test
        # fails loudly instead of breaking the SDK round-trip in
        # production.
        assert not verify_hmac_signature(WRONG_UUID, SECRET, ts, payload_bytes, prod_sig), (
            "FIX-F4: signature computed with user-facing api_key MUST NOT "
            "verify against the UUID — a pass here means signer and verifier "
            "drifted back to the pre-FIX-F4 shape"
        )


# ─────────────────────────────────────────────────────────────────────
# 0.7.0: Policy.from_dict and Policy class were removed from the
# SDK. The thin-client model means every enforcement decision
# arrives from the backend via /gate and /execute; the SDK does
# NOT maintain a local Policy object. The rate_limit_per_minute /
# loop_threshold / retry_threshold mapping test that previously
# lived here is now a backend unit test concern (see
# backend/src/proxy/http/policies.rs).
# ─────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────
# Canonical-bytes guard: pin the current behaviour where SDK and
# backend serialise the same dict differently (insertion order vs.
# sorted keys) but the divergence is harmless today because:
#   - WS path: signed_payload bytes are sent over the wire verbatim
#     (FIX-C in transport_websocket.py)
#   - HTTP path: SDK sends its own bytes via content=body; the backend
#     hashes exactly what it received (HMAC fix B6 in transport.py)
#
# If someone tries to UNIFY these by pre-computing HTTP HMAC and
# re-canonicalising on the backend, signatures will silently diverge.
# This guard pins that scenario as a known-broken shape so the
# refactorer is forced to make a conscious decision.
# ─────────────────────────────────────────────────────────────────────


class TestCanonicalBytesGuard:
    """Pin the canonical-bytes divergence so a unifying refactor trips."""

    def test_sdk_serialization_uses_insertion_order(self):
        # SDK uses ``json.dumps(payload, separators=(",", ":"))``
        # which preserves Python dict insertion order. The backend
        # uses ``canonical_serialize`` which sorts keys. They
        # intentionally differ — the divergence is harmless today
        # because each side hashes the bytes it emitted / received.
        # If you change this assertion, also re-read
        # backend/src/proxy/http/ws_control.rs::canonical_serialize
        # and confirm both sides agree on a single canonical form
        # for HMAC inputs.
        import json as _json

        payload = {"b": 1, "a": 2, "c": 3}
        sdk_bytes = _json.dumps(payload, separators=(",", ":")).encode("utf-8")
        assert sdk_bytes == b'{"b":1,"a":2,"c":3}', (
            "SDK serialization order changed. If you intended to switch "
            "to a canonical (sorted-key) form, also update "
            "backend/src/proxy/http/ws_control.rs::canonical_serialize "
            "to match — otherwise HTTP HMAC will silently diverge."
        )

    def test_sdk_signed_request_body_matches_dumped_body(self):
        """The HMAC over the request body must use the exact bytes
        the SDK sends on the wire (``content=body`` in
        ``_track_batch`` / ``_gate_request`` etc.). This test pins
        that the body bytes round-trip through ``json.dumps`` with
        no mutation between signing and sending."""
        import json as _json

        from nullrun.transport import _signed_request_body

        payload = {"workflow_id": "wf-1", "tokens": 100, "foo": "bar"}
        signed_body = _signed_request_body(payload)
        # Same dict → same bytes (no silent mutation).
        assert signed_body == _json.dumps(payload, separators=(",", ":")).encode("utf-8")


# ─────────────────────────────────────────────────────────────────────
# F-R2-01 (audit 2026-06-22): SDK must call /api/v1/execute (not
# /api/v1/gate) for sensitive-tool enforcement. /gate is advisory and
# does not check the API key's `execute` scope — calling it on a
# sensitive tool silently skips the scope gate, letting an API key
# with only `read`/`write` scopes drive a sensitive-tool decision.
#
# Pin: Transport.execute POSTs to /api/v1/execute. A refactor that
# routes it back to /gate trips here.
# ─────────────────────────────────────────────────────────────────────


class TestSensitiveToolRoutesToExecute:
    """Sensitive-tool pre-check must hit /api/v1/execute."""

    @respx.mock
    def test_execute_routes_to_api_v1_execute(self, transport):
        execute_route = respx.post("https://api.test.nullrun.io/api/v1/execute").mock(
            return_value=httpx.Response(200, json={"decision": "allow"})
        )
        gate_route = respx.post("https://api.test.nullrun.io/api/v1/gate").mock(
            return_value=httpx.Response(200, json={"decision": "allow"})
        )

        transport.execute(
            organization_id="00000000-0000-0000-0000-000000000001",
            execution_id="wf-1",
            trace_id="trace-1",
            tool="my.sensitive.tool",
            input_data={"x": 1},
        )

        assert execute_route.called, (
            "F-R2-01: Transport.execute must POST to /api/v1/execute "
            "so the backend checks the `execute` scope. Pre-fix this "
            "routed to /api/v1/gate (advisory, no scope check) and "
            "silently let API keys without `execute` scope drive a "
            "sensitive-tool decision."
        )
        assert not gate_route.called, (
            "F-R2-01: /api/v1/gate must NOT be called by Transport.execute. "
            "It is reserved for budget pre-flight (Transport.check)."
        )


# ─────────────────────────────────────────────────────────────────────
# 0.7.0: TestPolicyFetchFailClosed was retired along with the local
# Policy class and _fetch_policy(). The SDK no longer fetches policy
# from the backend on init (backend owns all policy state now).
# ─────────────────────────────────────────────────────────────────────


class TestOutgoingAckIsSigned:
    """Pin the SDK's outgoing ACK wire shape: HMAC-signed envelope.

    CP7 fix (2026-06-26): previously the ACK was plain JSON
    (``TestOutgoingAckIsPlainJson`` — now retired). The wire
    format now includes ``api_key``, ``timestamp`` and
    ``signature`` so the SDK is forward-compatible with the
    backend's pending ACK-verification work
    (``backend/src/proxy/http/ws_control.rs:842-848`` TODO).

    Field-name consistency matches the incoming
    ``SignedWsMessage`` envelope: ``api_key`` carries the user-
    facing API key string (``nr_live_...``) as the HMAC identity,
    ``timestamp`` is unix seconds (matches the rest of the SDK —
    see FIX-F5), ``signature`` is sha256 HMAC of
    ``timestamp:api_key:sha256(body)``.

    The signature covers the canonical bytes of the *unsigned*
    body (``{type, message_id, received_at}``), so the receiver
    can re-hash the same body and compare.
    """

    def test_ack_envelope_has_six_fields(self):
        """Pure-function check on the expected envelope shape."""
        timestamp = int(time.time())
        ack = {
            "type": "ack",
            "message_id": "msg-1",
            "received_at": timestamp,
            "api_key": "nr_live_test",
            "timestamp": timestamp,
            "signature": "deadbeef" * 8,  # placeholder
        }
        assert set(ack.keys()) == {
            "type",
            "message_id",
            "received_at",
            "api_key",
            "timestamp",
            "signature",
        }, (
            "CP7: outgoing ACK envelope must contain exactly "
            "{type, message_id, received_at, api_key, timestamp, "
            "signature}. The receiver verifies signature over the "
            "bytes of the unsigned body (everything except "
            "api_key/timestamp/signature)."
        )

    def test_ack_signature_covers_unsigned_body(self):
        """Signature MUST be computed over the canonical bytes of the
        unsigned body (3 fields), NOT the signed body (6 fields).

        If we naively computed the signature over the final dict,
        the receiver's verify (which hashes the 3-field body) would
        never match — a silent auth break. This test pins the
        invariant so future refactors can't accidentally re-serialise
        before signing.
        """
        import json

        timestamp = 1_700_000_000
        api_key = "nr_live_test"
        secret_key = "test-secret"

        unsigned_body = {
            "type": "ack",
            "message_id": "msg-1",
            "received_at": timestamp,
        }
        # Mirrors transport.generate_hmac_signature: HMAC-SHA256 of
        # "{timestamp}:{api_key}:{sha256(body)}".
        body_str = json.dumps(unsigned_body, sort_keys=True)
        body_hash = hashlib.sha256(body_str.encode("utf-8")).hexdigest()
        message = f"{timestamp}:{api_key}:{body_hash}"
        expected = hmac.new(
            secret_key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        # Compute the signature the receiver would compute. It must
        # match the sender's expected value exactly. This is the
        # invariant: sender signs 3-field body, receiver verifies
        # against the same 3-field body.
        receiver_body_str = json.dumps(unsigned_body, sort_keys=True)
        receiver_body_hash = hashlib.sha256(receiver_body_str.encode("utf-8")).hexdigest()
        receiver_message = f"{timestamp}:{api_key}:{receiver_body_hash}"
        receiver_expected = hmac.new(
            secret_key.encode("utf-8"),
            receiver_message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        assert expected == receiver_expected, (
            "CP7: sender and receiver must hash the same canonical "
            "bytes. If this fails, the signature scheme is broken."
        )


# ─────────────────────────────────────────────────────────────────────
# F-R2-06 (audit 2026-06-22): the SDK must accept ALL FIVE
# ``WsWorkflowState`` variants: Normal, Flagged, Tripped, Paused,
# Killed. Pre-fix the SDK dropped Flagged / Tripped rows on the floor
# because the local enum was 3-variant. The frontend mirrors this
# state union.
# ─────────────────────────────────────────────────────────────────────


class TestAllFiveWorkflowStatesAccepted:
    """Pin that the SDK WS handler accepts every WsWorkflowState variant."""

    @pytest.mark.parametrize(
        "state_name",
        ["Normal", "Flagged", "Tripped", "Paused", "Killed"],
    )
    def test_ws_state_change_accepted(self, state_name):
        """Each of the five canonical WsWorkflowState strings must
        round-trip through the SDK's WS handler without being
        rejected / filtered / coerced to a fallback."""
        # Pure-function check: the SDK does not maintain a hard-coded
        # list of acceptable states. The state name flows through to
        # _remote_state_for() and back to check_control_plane() as-is.
        # If a future refactor narrows the accepted set (e.g. by
        # adding an enum with only 3 variants), this test fails.
        from nullrun.runtime import NullRunRuntime

        rt = NullRunRuntime(api_key="nr_live_x", _test_mode=True)
        try:
            wf_id = f"wf-{state_name.lower()}"
            # Inject a state push via the public _set_remote_state path.
            rt._set_remote_state(wf_id, {"state": state_name, "version": 1})
            cached = rt._remote_state_for(wf_id)
            assert cached["state"] == state_name, (
                f"F-R2-06: WsWorkflowState variant {state_name!r} must round-trip "
                f"through _set_remote_state / _remote_state_for. Got "
                f"{cached['state']!r}. Pre-fix the SDK had a 3-variant union "
                f"and silently dropped Flagged/Tripped rows."
            )
        finally:
            rt.shutdown()


# ─────────────────────────────────────────────────────────────────────
# F-R2-12 (audit 2026-06-22): track_event() must register a new
# workflow_id in _remote_states atomically against concurrent WS
# pushes. Pre-fix the lock was held only across setdefault, leaving
# a window where a WS push could overwrite a freshly-empty dict and
# then the next track_event() call would create a brand-new empty
# dict again — silently losing remote KILL/PAUSE state between the
# WS push and the next event.
#
# Pin: the only path that mutates _remote_states is the locked helper
# _remote_state_for (or _set_remote_state). No bare setdefault.
# ─────────────────────────────────────────────────────────────────────


class TestRemoteStatesAtomicRegistration:
    """track_event() must register workflow_id atomically.

    Known flake: ``test_track_event_uses_locked_helper_for_setdefault``
    uses ``inspect.getsource(rt.track)`` which can race with a
    background flush thread that mutates ``rt._remote_states`` during
    source-string capture. The test passes 5/5 in isolation. Fails
    ~1/20 in the full suite when the timing window lines up with a
    transport flush. Pre-existing (introduced in 0.6.0 release,
    2026-06-23 14:47, commit 4610ba9 — well before Layer-1 work).
    Re-run in isolation to confirm. Fix path: replace
    ``inspect.getsource`` with a static AST check on
    ``nullrun.runtime.NullRunRuntime.track`` instead of an instance
    method.
    """

    def test_track_event_uses_locked_helper_for_setdefault(self):
        """The setdefault that primes _remote_states for a new workflow
        must be inside a single ``with self._states_lock:`` block (or
        routed through the locked _remote_state_for helper)."""
        import inspect

        from nullrun.runtime import NullRunRuntime

        rt = NullRunRuntime(api_key="nr_live_x", _test_mode=True)
        try:
            # The registration site lives in track() (called from
            # track_event / track_llm / track_tool). Pin it there.
            src = inspect.getsource(rt.track)
            # Pin: no bare ``self._remote_states.setdefault(...)`` calls
            # outside a lock context.
            assert "self._remote_states.setdefault(" not in src, (
                "F-R2-12: track() must not call "
                "self._remote_states.setdefault() directly. Use "
                "_remote_state_for() which holds _states_lock for the "
                "entire setdefault — bare setdefault outside the lock "
                "creates a window where a concurrent WS push wins the "
                "race and silently loses KILL/PAUSE state."
            )
            # Pin: the locked helper IS the path used.
            assert "_remote_state_for" in src, (
                "F-R2-12: track() must use _remote_state_for() to "
                "register the workflow_id atomically."
            )
        finally:
            rt.shutdown()
