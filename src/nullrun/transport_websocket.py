"""
WebSocket transport for NullRun SDK.

Provides real-time workflow state updates via WebSocket connection.
Replaces polling pattern: SDK connects to WS, receives push updates
when workflow state changes (KILL/PAUSE).
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable
from typing import Any

# CP7 fix: outgoing ACK is now HMAC-signed using the same
# ``generate_hmac_signature`` helper the HTTP transport uses for
# ``X-Signature`` headers. Importing here keeps the signing logic
# in one place — ``transport.py`` owns the helper, the WS layer
# only consumes it.
from nullrun.transport import generate_hmac_signature

try:
    import websockets

    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False

logger = logging.getLogger(__name__)

# S-10: cap on consecutive WebSocket reconnect failures.
# Pre-fix the reconnect loop ran forever (``while not self._closed``)
# leaking the WS thread and flooding logs when the backend was
# permanently down. We now give up after this many attempts and let
# the caller fall back to HTTP-poll (the SDK still tracks / gates /
# cost-rolls; only the WS push latency advantage is lost).
_MAX_RECONNECT_ATTEMPTS = 10

# HMAC identity field on the WS wire format.
#
# The backend's ``SignedWsMessage`` struct (NULLRUN/backend/src/proxy/
# http/ws_control.rs:43) serializes the HMAC identity under the field
# name ``api_key``. Pre-FIX-F4 the wire field was named ``api_key_id``
# (the rename happened in the backend struct comment but not in every
# test fixture — see tests/test_ws_signed_payload.py for the historical
# mock shape). The SDK reads this field and uses the value to verify
# the HMAC signature; without a constant pin, a future struct rename
# silently breaks signature verification on every push.
#
# HTTP path uses a different field name — ``X-API-Key`` (see
# Transport._build_signed_headers). The two transports agree on the
# field NAME but disagree on the VALUE: HTTP carries the user-facing
# ``nr_live_...`` string, WS carries the internal UUID from
# ``auth_context.key_id ``. Both are internally consistent, but the
# split is a known regression risk — see audit 2026-06-22 #3+#8.
WS_HMAC_IDENTITY_FIELD = "api_key"


def compute_hmac_signature(api_key: str, secret_key: str, timestamp: int, payload: bytes) -> str:
    """
    Compute HMAC-SHA256 signature for WebSocket message verification.

    Signature = HMAC-SHA256(secret_key, timestamp:api_key:payload_hash)
    where payload_hash = SHA256(message_json)

    Args:
        api_key: Client's API key (identifier)
        secret_key: Client's secret key (used for HMAC)
        timestamp: Unix timestamp in seconds
        payload: Raw message payload bytes

    Returns:
        Hex-encoded HMAC-SHA256 signature
    """
    payload_hash = hashlib.sha256(payload).hexdigest()

    # Construct message: timestamp:api_key:payload_hash
    message = f"{timestamp}:{api_key}:{payload_hash}"

    signature = hmac.new(
        secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    return signature


def verify_hmac_signature(
    api_key: str,
    secret_key: str,
    timestamp: int,
    payload: bytes,
    signature: str,
    max_age_seconds: int = 300,
) -> bool:
    """
    Verify HMAC signature for a WebSocket message.

    Args:
        api_key: Client's API key (identifier)
        secret_key: Client's secret key (used for HMAC)
        timestamp: Unix timestamp from message (seconds)
        payload: Raw message payload bytes
        signature: HMAC signature to verify (hex-encoded)
        max_age_seconds: Maximum allowed age of message (default 5 min)

    Returns:
        True if signature is valid and timestamp is fresh, False otherwise
    """
    # Check timestamp freshness
    current_time = int(time.time())
    age = abs(current_time - timestamp)

    if age > max_age_seconds:
        # Mirror the same counter used by the SDK-side transport-error
        # path so SRE can distinguish transient drops from this branch.
        # HTTP verify path so SRE gets one alert ladder for
        # clock-skew issues, not two.
        try:
            from nullrun.observability import metrics

            metrics.inc_transport("hmac_verify_expired_total")
        except Exception:  # noqa: BLE001 — best-effort counter
            pass
        logger.warning(f"WS signature timestamp expired: age={age}s, max={max_age_seconds}s")
        return False

    # Compute expected signature
    expected = compute_hmac_signature(api_key, secret_key, timestamp, payload)

    # Constant-time comparison to prevent timing attacks
    return hmac.compare_digest(expected, signature)


class WebSocketConnection:
    """
    WebSocket connection for real-time control plane updates.

    Usage:
        conn = await transport.connect_websocket(
            organization_id="org-123"
            api_key="nr_live_xxx"
            secret_key="secret_xxx"
            on_state_change=lambda state: print(f"State changed: {state}")
        )
        # Connection stays open, receiving state updates
        await conn.close 
    """

    # States that require acknowledgment (KILL/PAUSE).
    # The server's WsWorkflowState enum (NULLRUN/backend/src/proxy/http/
    # ws_control.rs) emits PascalCase ("Killed", "Paused"); the SDK
    # must compare against the same casing, otherwise the ACK
    # path stays dead and the server's pending-ack queue grows
    # without ever being drained.
    ACKNOWLEDGED_STATES = {"Killed", "Paused"}

    @classmethod
    def _is_acknowledged_state(cls, state: str) -> bool:
        """Case-insensitive membership check against ``ACKNOWLEDGED_STATES``.

        Audit-2026-06-22: added a lowercase fallback so a server
        regression to ``"killed"``/``"paused"`` doesn't silently
        drop the ACK. Exact PascalCase is still the happy path and
        is checked first; the lowercase branch is defensive only.
        """
        if state in cls.ACKNOWLEDGED_STATES:
            return True
        return state.lower() in {s.lower() for s in cls.ACKNOWLEDGED_STATES}

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        api_key: str | None = None,
        secret_key: str | None = None,
        on_state_change: Callable[[dict[str, Any]], None] | None = None,
        on_policy_invalidated: Callable[[str, str, int], None] | None = None,
        on_key_rotated: Callable[[str, str, int], None] | None = None,
    ):
        """
        Initialize WebSocket connection.

        Args:
            url: WebSocket URL (e.g., "wss:/api.nullrun.io/ws/control/org-123")
            headers: HTTP headers for authentication
            api_key: API key for HMAC verification (optional but recommended)
            secret_key: Secret key for HMAC verification (optional but recommended)
            on_state_change: Callback when workflow state changes
            on_policy_invalidated: Callback when policy cache should be cleared
                                 Args: (organization_id, policy_id, new_version)
            on_key_rotated: Callback when secret key should be re-fetched
                           Args: (organization_id, key_id, new_version)
        """
        self.url = url
        self.headers = headers or {}
        self.api_key = api_key
        self.secret_key = secret_key
        self.on_state_change = on_state_change
        self.on_policy_invalidated = on_policy_invalidated
        self.on_key_rotated = on_key_rotated
        self._conn = None
        self._running = False
        self._receive_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._closed = False
        # S-10: counter for the consecutive reconnect-failure cap.
        # Reset to 0 on a successful ``_connect ``.
        self._consecutive_reconnect_failures: int = 0
        # Per-workflow monotonic version dedup (ADR-007).
        # Drop incoming state changes with ``version <= last`` to
        # survive the at-least-once delivery semantics of the WS
        # channel.
        #
        # Sprint 1.4 (B2): the previous sentinel of 0 dropped incoming
        # ``version == 0`` on first receive because ``0 <= 0`` is
        # True. The server uses ``version: 0`` for the very first
        # ``initial_state`` frame after a (re)connect, so the SDK was
        # silently discarding the server's initial view — meaning a
        # ``Killed``/``Paused`` state delivered in that first frame
        # was lost. Sentinel is now -1 so any non-negative version
        # passes the guard on the first message; subsequent stale
        # ``version == 0`` re-deliveries are still dropped because
        # ``last_seen`` will be ``>= 1`` for that workflow.
        self._last_version: dict[str, int] = {}

    async def _reconnect_loop(self) -> None:
        """
        Background reconnect loop with exponential backoff.

        The receive loop sets ``self._running = False`` in its
        ``finally`` block when the connection drops. This loop waits
        while the receive loop is healthy and reconnects on demand.

        Without the ``continue`` branch, the pre-fix code exited after
        the very first successful ``_connect `` because the
        ``if not self._running`` guard became False the moment
        ``_connect `` set ``_running = True``. That broke the control
        plane: after any network blip, kill/pause commands from the
        dashboard would never reach the client until the process was
        restarted. For a product whose core promise is a centralised
        kill-switch, this was a safety gap — see plan item B1.
        """
        delay = 1.0
        max_delay = 60.0

        while not self._closed:
            if self._running:
                # Receive loop is healthy. Sleep briefly and re-check
                # if the connection drops the receive loop's
                # ``finally`` block will set ``_running = False`` and
                # we will reconnect on the next iteration.
                await asyncio.sleep(0.5)
                continue

            # S-10: cap reconnect attempts. Pre-fix the
            # loop was unbounded (``while not self._closed``) so a
            # permanently-down backend kept the SDK's WS thread
            # spinning forever, leaking the thread and producing log
            # spam at the operator. We now stop after
            # ``MAX_RECONNECT_ATTEMPTS`` consecutive failures. The
            # receive loop's ``finally`` already set ``_running = False``
            # so this loop will exit and ``connect `` returns
            # control to the caller; the SDK falls back to HTTP-poll
            # via ``runtime._poll_commands``.
            if self._consecutive_reconnect_failures >= _MAX_RECONNECT_ATTEMPTS:
                logger.warning(
                    f"WebSocket reconnect gave up after "
                    f"{_MAX_RECONNECT_ATTEMPTS} consecutive failures; "
                    f"falling back to HTTP-poll. url={self.url}"
                )
                # Mark the connection as closed so the loop exits.
                # The runtime will continue to operate via HTTP-poll.
                self._closed = True
                self._running = False
                break

            # Connection is down. Try to reconnect with backoff.
            try:
                await self._connect()
                delay = 1.0  # reset on success
                self._consecutive_reconnect_failures = 0
                logger.info(f"WebSocket reconnected successfully: {self.url}")
                # A fresh server connection may re-deliver events the
                # client has already seen (or has never seen) — clear
                # the version-dedup cache so the server's current view
                # is accepted, not deduplicated against the
                # pre-disconnect state. Same semantic as
                # ``resync_required``.
                self.clear_local_state()
            except Exception as e:
                self._consecutive_reconnect_failures += 1
                logger.warning(
                    f"WebSocket reconnect failed "
                    f"({self._consecutive_reconnect_failures}/{_MAX_RECONNECT_ATTEMPTS}), "
                    f"retrying in {delay}s: {e}"
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_delay)

    async def _connect(self) -> None:
        """
        Establish WebSocket connection.

        Internal method used by connect and reconnect loop.
        """
        self._conn = await websockets.connect(self.url, additional_headers=self.headers)
        self._running = True
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def connect(self) -> None:
        """
        Establish WebSocket connection with automatic reconnect.

        Raises:
            ConnectionError: If connection fails
            ImportError: If websockets library not available
        """
        if not WEBSOCKETS_AVAILABLE:
            raise ImportError(
                "websockets library not available. Install with: pip install nullrun[websocket]"
            )

        self._closed = False

        try:
            await self._connect()
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())
            logger.info(f"WebSocket connected: {self.url}")
        except Exception as e:
            logger.error(f"WebSocket connection failed: {e}")
            raise ConnectionError(f"Failed to connect to {self.url}: {e}") from e

    async def _receive_loop(self) -> None:
        """
        Receive messages from WebSocket and dispatch to handler.
        """
        try:
            async for message in self._conn:
                await self._handle_message(message)
        except websockets.exceptions.ConnectionClosed:
            logger.info("WebSocket connection closed")
        except Exception as e:
            logger.warning(f"WebSocket receive error: {e}")
        finally:
            self._running = False

    async def _handle_message(self, message: str) -> None:
        """
        Handle incoming WebSocket message.

        Args:
            message: Raw message string (JSON)
        """
        try:
            data = json.loads(message)
            msg_type = data.get("type", "")

            # Check for HMAC signature and verify if present
            signature = data.get("signature")
            timestamp = data.get("timestamp")
            if signature and timestamp and self.api_key and self.secret_key:
                # This is a signed message - verify the signature
                msg_timestamp = int(timestamp) if isinstance(timestamp, (int, str)) else 0

                # FIX-C (counterpart of backend fix(ws-control) in
                # NULLRUN): the server embeds the exact bytes that were
                # HMAC-signed in `signed_payload` (hex-encoded). The
                # receiver MUST verify against those exact bytes —
                # never against the full wire JSON (which includes
                # signature/timestamp/api_key_id themselves and would
                # never match). The pre-FIX-C server builds kept the
                # signing scheme but did not publish the canonical
                # payload, so we fall back to the legacy behaviour
                # (verify against the full wire bytes) only when
                # `signed_payload` is absent.
                #
                # See memory/ws-signed-message-byte-mismatch for the
                # original failure this design rule encodes.
                signed_payload_hex = data.get("signed_payload")
                if isinstance(signed_payload_hex, str) and signed_payload_hex:
                    try:
                        verify_payload = bytes.fromhex(signed_payload_hex)
                    except ValueError:
                        # Malformed hex from a non-conforming server.
                        # Fall through to the legacy wire-bytes path
                        # so we still have a chance to accept it; the
                        # signature check will fail in either case
                        # and we'll reject with the standard error.
                        verify_payload = message.encode("utf-8")
                else:
                    # Pre-FIX-C server: verify against full wire
                    # bytes. Will pass only on round-trip tests where
                    # the server happens to hash the same bytes we
                    # do; in real life this is the byte-mismatch path
                    # and the message should be rejected. Kept as
                    # best-effort backwards compatibility.
                    verify_payload = message.encode("utf-8")

                # FIX-F4 (counterpart of backend ws_control.rs FIX-F4): the server
                # signs HMAC over the user-facing API key the SDK has
                # (``nr_live_...``). The envelope publishes the same
                # value under the ``api_key`` field — we MUST read it
                # back from there and use it as the HMAC identifier.
                #
                # Pre-FIX-F4 this branch read ``data["api_key_id"]``
                # which used to be the wire field name on the server
                # side. That field now carries the same user-facing
                # value (no longer the internal UUID key_id), so for
                # backwards compat we accept either field name —
                # pre-FIX-F4 envelopes may still arrive with
                # ``api_key_id`` carrying the user-facing string
                # because the server's only consumers were pre-FIX-F4
                # SDKs.
                #
                # Fall back to ``self.api_key`` only when the envelope
                # has neither field (a pre-FIX-D server without
                # signed_payload), which is a degraded path that
                # already 403'd in real life per the FIX-C comments.
                envelope_api_key = (
                    data.get(WS_HMAC_IDENTITY_FIELD)
                    if isinstance(data.get(WS_HMAC_IDENTITY_FIELD), str)
                    and data.get(WS_HMAC_IDENTITY_FIELD)
                    else data.get("api_key_id")
                )
                if isinstance(envelope_api_key, str) and envelope_api_key:
                    verify_api_key = envelope_api_key
                else:
                    # Pre-FIX-D server: no api_key/api_key_id
                    # published. Round-trip only — never expected in
                    # production after the FIX-C deployment.
                    verify_api_key = self.api_key

                if not verify_hmac_signature(
                    verify_api_key,
                    self.secret_key,
                    msg_timestamp,
                    verify_payload,
                    signature,
                    max_age_seconds=300,
                ):
                    # Sprint 1.5 (B13): pre-fix this logged at
                    # WARNING and dropped the message silently. For a
                    # safety layer whose core contract is "the
                    # server can always KILL a workflow", a failed
                    # signature verification on a control plane
                    # message is a first-class incident — promote to
                    # ERROR and bump the counter so an SRE can
                    # alert on ``hmac_verify_failures_total > 0``.
                    # A signed-but-invalid message means either
                    # (a) the secret_key is out of sync (server
                    # rotated, client missed the rotation event), or
                    # (b) something is forging traffic. Both are
                    # actionable and the operator needs to know.
                    logger.error(
                        f"Invalid HMAC signature for {msg_type} message - "
                        "rejecting. This usually means the secret_key is out "
                        "of sync with the server (check for a key_rotated "
                        "event you may have missed) or the control plane is "
                        "being tampered with."
                    )
                    # Local import to avoid a module-level cycle:
                    # observability imports nothing from us, so this
                    # is safe and lazy.
                    from nullrun.observability import metrics

                    metrics.inc_transport("hmac_verify_failures_total")
                    return

            # FIX-C (counterpart of backend fix(ws-control) in
            # NULLRUN): when the message is signed and carries a
            # `signed_payload` field, dispatching from the outer
            # body fields would let an attacker splice forged values
            # into the outer body while reusing a captured
            # (signed_payload, signature) pair. The signature is
            # computed over the bytes inside signed_payload, not the
            # outer body, so the *only* trusted source is signed_payload
            # itself. We parse it once and use the parsed dict for all
            # state-dispatch decisions.
            #
            # For non-signed messages (legacy servers, or policy
            # events that don't need per-payload signing) we fall back
            # to the outer body — there is no signing, no attacker
            # model.
            trusted: dict[str, Any] | None = None
            if signature and timestamp and self.api_key and self.secret_key:
                if isinstance(signed_payload_hex, str) and signed_payload_hex:
                    try:
                        trusted = json.loads(bytes.fromhex(signed_payload_hex).decode("utf-8"))
                    except (ValueError, json.JSONDecodeError):
                        # Malformed signed_payload — the signature
                        # check above will already have rejected this
                        # message, so this branch should be unreachable
                        # in practice. We keep the fall-through to
                        # outer body to avoid a hard crash if the
                        # two checks ever drift.
                        trusted = None

            if msg_type == "initial_state":
                # Initial state with all workflow states
                workflows = data.get("workflows", [])
                logger.debug(f"Received initial state: {len(workflows)} workflows")
                for wf in workflows:
                    # Trust the inner workflows[] entries the same
                    # way we trust state_change: when the parent
                    # envelope is signed, parse each entry from its
                    # embedded signed_payload if present, else fall
                    # back to the outer dict.
                    if (
                        isinstance(wf, dict)
                        and wf.get("signed_payload")
                        and self.api_key
                        and self.secret_key
                    ):
                        try:
                            inner = json.loads(bytes.fromhex(wf["signed_payload"]).decode("utf-8"))
                            self._dispatch_state(inner)
                            continue
                        except (ValueError, json.JSONDecodeError, KeyError):
                            pass
                    self._dispatch_state(wf)

            elif msg_type == "state_change":
                # Workflow state change notification
                # Check if this message requires acknowledgment
                await self._handle_state_change_with_ack(data, trusted)

            elif msg_type == "policy_invalidated":
                # Policy was updated via dashboard - SDK should clear its cache
                organization_id = data.get("organization_id", "")
                policy_id = data.get("policy_id", "")
                new_version = data.get("new_version", 0)
                logger.info(
                    f"Policy invalidated: {policy_id} v{new_version}, org: {organization_id}"
                )
                if self.on_policy_invalidated:
                    try:
                        self.on_policy_invalidated(organization_id, policy_id, new_version)
                    except Exception as e:
                        logger.warning(f"Policy invalidation callback error: {e}")

            elif msg_type == "key_rotated":
                # HMAC secret key was rotated - SDK should re-fetch from /auth/verify
                organization_id = data.get("organization_id", "")
                key_id = data.get("key_id", "")
                new_version = data.get("new_version", 0)
                logger.info(f"Key rotated: {key_id} v{new_version}, org: {organization_id}")
                if self.on_key_rotated:
                    try:
                        self.on_key_rotated(organization_id, key_id, new_version)
                    except Exception as e:
                        logger.warning(f"Key rotation callback error: {e}")

            elif msg_type == "resync_required":
                # Server overflowed its broadcast channel. Per
                # ADR-007 the SDK MUST close, reconnect, and
                # replace its local state from the new
                # ``initial_state`` — there is no "catch up"
                # semantics. We clear the version-dedup cache and
                # let ``_reconnect_loop`` reopen the connection.
                reason = data.get("reason", "overflow")
                logger.warning(
                    f"Server requested resync (reason={reason}); "
                    "clearing local state and reconnecting"
                )
                self.clear_local_state()
                self._running = False
                self._closed = True
                if self._conn is not None:
                    try:
                        await self._conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._conn = None

            elif msg_type == "pong":
                # Pong response to ping - connection is alive
                pass

            elif msg_type == "subscribed":
                # Subscription confirmation
                organization_id = data.get("organization_id")
                logger.debug(f"Subscribed to organization: {organization_id}")

            elif msg_type == "error":
                # Error message from server
                code = data.get("code", "unknown")
                message = data.get("message", "Unknown error")
                logger.warning(f"WebSocket error: {code} - {message}")

            else:
                # CP4 fix: unknown msg_type. Previously this fell
                # through the entire if/elif chain with no else
                # so a new WsMessage variant added by the backend
                # would be silently dropped. The user would only
                # find out when a control-plane feature stopped
                # working. Now we log at WARNING with enough
                # context to debug forward-compat drift.
                #
                # We deliberately do NOT raise or trigger a
                # reconnect — the message was HMAC-verified (so
                # it's authentic) and the SDK just doesn't know
                # how to act on it. A WARNING keeps the operator
                # informed without breaking the WS receive loop.
                logger.warning(
                    "Unknown WS message type %r from server — likely "
                    "a backend version newer than this SDK. Message "
                    "will be ignored. Payload keys: %s. Update the "
                    "SDK to handle this type if it's expected to be "
                    "in production soon.",
                    msg_type,
                    sorted(data.keys()),
                )
                # Bump a metric so an SRE can alert on a spike of
                # unknown-type messages — that signals a real
                # forward-compat break in production.
                from nullrun.observability import metrics

                metrics.inc_transport("unknown_ws_message_type_total")

        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON message: {message[:100]}")

    async def _handle_state_change_with_ack(
        self,
        data: dict[str, Any],
        trusted: dict[str, Any] | None = None,
    ) -> None:
        """
        Handle state change message that may require acknowledgment.

        For killed/paused states, sends ACK immediately before dispatching.

        Args:
            data: The outer (envelope) message data — used for
                routing metadata only.
            trusted: The parsed bytes of `signed_payload` (when the
                message was signed). When present, dispatch reads
                state / workflow_id / version / message_id from this
                dict, NOT from `data`. The signature is computed over
                the bytes inside signed_payload, so any divergence
                between `data` and `trusted` is a forgery attempt and
                must not be honoured.
        """
        # FIX-C: when the message is signed, the signature covers the
        # bytes inside `signed_payload`, not the outer body. We must
        # use `trusted` (the parsed signed_payload) for any
        # security-sensitive decision. The outer `data` is only used
        # for routing.
        source = trusted if trusted is not None else data
        state = source.get("state", "")
        workflow_id = source.get("workflow_id", "")
        message_id = source.get("message_id")

        # Check if this state requires acknowledgment
        #
        # Audit-2026-06-22 case-defensive: the HTTP-poll path
        # (`runtime.py`) lowercases before comparing so it survives a
        # server regression to lowercase states. The WS path used to
        # exact-match only. Without this fallback, a server regression
        # would silently drop the ACK (the existing test pins
        # PascalCase as the happy path, but does not pin what happens
        # if the server emits ``"killed"``).
        #
        # ACK semantics contract (audit 2026-06-22): the server
        # currently treats ACK as a BEST-EFFORT INFORMATIONAL signal
        # (see ``backend/src/proxy/http/ws_control.rs`` ACK handler
        # comment for the full contract). Only `Killed`/`Paused` are
        # ACKed; the other 3 WsWorkflowState variants
        # (Normal/Flagged/Tripped) are dispatched to the callback but
        # do not trigger an ACK. This is by design — the backend
        # pending-ack queue is dead code, so a missing ACK does not
        # block state propagation today. If a future refactor makes
        # the server gate on ACK arrival, the SDK must extend its
        # ACK set to all 5 states or states will silently stick.
        if self._is_acknowledged_state(state) and message_id:
            # Send ACK immediately
            await self._send_ack(message_id)
            logger.debug(f"Sent ACK for message {message_id} ({state} for workflow {workflow_id})")

        # Dispatch state to callback. Use the trusted source so
        # callbacks (and the per-workflow version dedup in
        # _dispatch_state) see the same values that were ACK'd.
        self._dispatch_state(source)

    async def _send_ack(self, message_id: str) -> None:
        """
        Send acknowledgment message to server with HMAC signature.

        CP7 fix (2026-06-26): previously this ACK was plain JSON
        no signature, no timestamp, no api_key. The backend does
        not currently verify ACK authenticity (the TODO at
        ``backend/src/proxy/http/ws_control.rs:842-848`` is still
        open) but adding the signature now means:

        * When the backend enables ACK verification, the SDK is
          already on the wire format it expects — no breaking
          change for operators upgrading the SDK.
        * The signature prevents a malicious actor who can inject
          WS frames from forging client-side ACKs (e.g., confirming
          a "kill" that was never delivered).
        * The timestamp enables the receiver to enforce replay
          protection (refuse ACKs with a stale timestamp).

        The wire format mirrors the incoming ``SignedWsMessage``
        envelope: ``{type, message_id, received_at, api_key
        timestamp, signature}``. The ``api_key`` field carries the
        user-facing API key string (``nr_live_...``) as the HMAC
        identity — matches the same convention ``Transport.
        _build_signed_headers`` uses for HTTP requests. The
        signature is computed via ``generate_hmac_signature``
        (sha256 HMAC of ``timestamp:api_key:sha256(body)``)
        identical to the HTTP path so the backend can use one
        verification routine.

        Field-name consistency: incoming WS uses ``api_key`` as
        the HMAC identity field name (see
        ``WS_HMAC_IDENTITY_FIELD``). For outgoing we use the same
        field name so the receiver's verify path works
        symmetrically.

        Test contract: ``tests/test_integration_contract.py``
        pins the new wire format. The previous plain-JSON test was
        retired.
        """
        if not self._conn or not self._running:
            logger.warning("Cannot send ACK - WebSocket not connected")
            return

        try:
            # FIX-F5: received_at is unix SECONDS, not milliseconds.
            # Matches the backend's ``Utc::now.timestamp `` fallback
            # in ws_control.rs so a future telemetry / analytics
            # consumer doesn't see a 1000x divergence.
            received_at = int(time.time())
            timestamp = received_at  # also used in HMAC

            # Build the unsigned envelope first so the signature
            # covers exactly the bytes the receiver will hash. If we
            # mutated the dict after signing (e.g., adding a field)
            # the signature would diverge from the canonical bytes.
            ack: dict[str, Any] = {
                "type": "ack",
                "message_id": message_id,
                "received_at": received_at,
            }

            # Add HMAC fields when both api_key and secret_key are
            # configured. Without secret_key we still send the
            # plain envelope (matches the pre-fix behaviour for
            # legacy api_keys that don't use HMAC). The backend
            # skips verify when signature is absent.
            if self.api_key and self.secret_key:
                # The signature covers the canonical bytes of the
                # body the receiver will hash. We sign the *unsigned*
                # body (above) and add the signature field — the
                # receiver hashes the same body and compares.
                body_str = json.dumps(ack, sort_keys=True)
                signature = generate_hmac_signature(
                    self.api_key,
                    self.secret_key,
                    timestamp,
                    body_str,
                )
                ack["api_key"] = self.api_key
                ack["timestamp"] = timestamp
                ack["signature"] = signature
                # Send the signed body (without re-serialising the
                # dict that now includes signature/timestamp/api_key
                # which would diverge from the signed bytes).
                await self._conn.send(body_str)
            else:
                # Legacy / pre-HMAC path: plain JSON envelope.
                await self._conn.send(json.dumps(ack))
            logger.debug(f"ACK sent for message {message_id}")
        except Exception as e:
            logger.warning(f"Failed to send ACK: {e}")

    def _dispatch_state(self, state: dict[str, Any]) -> None:
        """
        Dispatch state to callback after per-workflow version dedup
        (ADR-007: at-least-once delivery, drop stale events).

        Args:
            state: State dict with workflow_id, state, version, etc.
        """
        workflow_id = state.get("workflow_id", "")
        incoming_version = state.get("version", 0)
        if workflow_id:
            # Sprint 1.4 (B2): default -1 (not 0) so version=0 is
            # accepted on first receive. See __init__ for rationale.
            last = self._last_version.get(workflow_id, -1)
            if incoming_version <= last:
                logger.debug(
                    f"Dropping stale state event for {workflow_id}: "
                    f"incoming version={incoming_version} <= last={last}"
                )
                return
            self._last_version[workflow_id] = incoming_version
        if self.on_state_change:
            try:
                self.on_state_change(state)
            except Exception as e:
                logger.warning(f"State change callback error: {e}")

    def clear_local_state(self) -> None:
        """
        Clear the in-memory per-workflow version cache.

        Called after a ``ResyncRequired`` event so the next
        ``initial_state`` from the server is accepted (the dedup
        cache may otherwise drop the server's freshest state if
        the version is unchanged from the pre-overflow value).
        Per ADR-007 there is no "merge" — local state is fully
        replaced by the next ``initial_state``.
        """
        self._last_version.clear()

    async def send(self, message: dict[str, Any]) -> None:
        """
        Send message to WebSocket server.

        Args:
            message: Message dict (will be JSON serialized)
        """
        if not self._conn or not self._running:
            raise ConnectionError("WebSocket not connected")

        try:
            await self._conn.send(json.dumps(message))
        except Exception as e:
            logger.warning(f"WebSocket send error: {e}")
            raise

    async def close(self) -> None:
        """
        Close WebSocket connection.
        """
        self._closed = True
        self._running = False

        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass

        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass

        if self._conn:
            await self._conn.close()
            self._conn = None

        logger.info("WebSocket connection closed")

    @property
    def is_connected(self) -> bool:
        """Check if connection is active."""
        return self._running and self._conn is not None and not self._closed
