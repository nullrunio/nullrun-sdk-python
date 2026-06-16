"""
WebSocket transport for NullRun SDK.

Provides real-time workflow state updates via WebSocket connection.
Replaces polling pattern: SDK connects to WS, receives push updates
when workflow state changes (KILL/PAUSE).
"""

import asyncio
import json
import logging
import time
import hmac
import hashlib
from typing import Any, Callable

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False

logger = logging.getLogger(__name__)


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
    # Compute payload hash: SHA256(payload)
    payload_hash = hashlib.sha256(payload).hexdigest()

    # Construct message: timestamp:api_key:payload_hash
    message = f"{timestamp}:{api_key}:{payload_hash}"

    # Compute HMAC-SHA256
    signature = hmac.new(
        secret_key.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
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
            organization_id="org-123",
            api_key="nr_live_xxx",
            secret_key="secret_xxx",
            on_state_change=lambda state: print(f"State changed: {state}")
        )
        # Connection stays open, receiving state updates
        await conn.close()
    """

    # States that require acknowledgment (KILL/PAUSE)
    ACKNOWLEDGED_STATES = {"killed", "paused"}

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
            url: WebSocket URL (e.g., "wss://api.nullrun.io/ws/control/org-123")
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

    async def _reconnect_loop(self) -> None:
        """
        Background reconnect loop with exponential backoff.

        Attempts to reconnect on connection loss with increasing delays up to max_delay.
        Resets delay on successful connection.
        """
        delay = 1.0
        max_delay = 60.0

        while not self._closed:
            if not self._running and not self._closed:
                try:
                    await self._connect()
                    delay = 1.0  # reset on success
                    logger.info(f"WebSocket reconnected successfully: {self.url}")
                except Exception as e:
                    logger.warning(f"WebSocket reconnect failed, retrying in {delay}s: {e}")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, max_delay)
            else:
                # Connection is running or closed, exit reconnect loop
                break

    async def _connect(self) -> None:
        """
        Establish WebSocket connection.

        Internal method used by connect() and reconnect loop.
        """
        self._conn = await websockets.connect(
            self.url, additional_headers=self.headers
        )
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
                "websockets library not available. "
                "Install with: pip install nullrun[websocket]"
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
                # Use the raw message bytes (same as backend used for signing)
                if not verify_hmac_signature(
                    self.api_key,
                    self.secret_key,
                    msg_timestamp,
                    message.encode('utf-8'),
                    signature,
                    max_age_seconds=300,
                ):
                    logger.warning(f"Invalid HMAC signature for {msg_type} message - rejecting")
                    return

            if msg_type == "initial_state":
                # Initial state with all workflow states
                workflows = data.get("workflows", [])
                logger.debug(f"Received initial state: {len(workflows)} workflows")
                for wf in workflows:
                    self._dispatch_state(wf)

            elif msg_type == "state_change":
                # Workflow state change notification
                # Check if this message requires acknowledgment
                await self._handle_state_change_with_ack(data)

            elif msg_type == "policy_invalidated":
                # Policy was updated via dashboard - SDK should clear its cache
                organization_id = data.get("organization_id", "")
                policy_id = data.get("policy_id", "")
                new_version = data.get("new_version", 0)
                logger.info(f"Policy invalidated: {policy_id} v{new_version}, org: {organization_id}")
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

        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON message: {message[:100]}")

    async def _handle_state_change_with_ack(self, data: dict[str, Any]) -> None:
        """
        Handle state change message that may require acknowledgment.

        For killed/paused states, sends ACK immediately before dispatching.

        Args:
            data: The state change message data
        """
        state = data.get("state", "")
        workflow_id = data.get("workflow_id", "")
        message_id = data.get("message_id")

        # Check if this state requires acknowledgment
        if state in self.ACKNOWLEDGED_STATES and message_id:
            # Send ACK immediately
            await self._send_ack(message_id)
            logger.debug(f"Sent ACK for message {message_id} ({state} for workflow {workflow_id})")

        # Dispatch state to callback
        self._dispatch_state(data)

    async def _send_ack(self, message_id: str) -> None:
        """
        Send acknowledgment message to server.

        Args:
            message_id: The message ID to acknowledge
        """
        if not self._conn or not self._running:
            logger.warning("Cannot send ACK - WebSocket not connected")
            return

        try:
            ack = {
                "type": "ack",
                "message_id": message_id,
                "received_at": int(time.time() * 1000),  # milliseconds
            }
            await self._conn.send(json.dumps(ack))
            logger.debug(f"ACK sent for message {message_id}")
        except Exception as e:
            logger.warning(f"Failed to send ACK: {e}")

    def _dispatch_state(self, state: dict[str, Any]) -> None:
        """
        Dispatch state to callback.

        Args:
            state: State dict with workflow_id, state, version, etc.
        """
        if self.on_state_change:
            try:
                self.on_state_change(state)
            except Exception as e:
                logger.warning(f"State change callback error: {e}")

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


class WebSocketManager:
    """
    Manager for WebSocket connections per organization.

    Maintains a single connection per organization to avoid
    duplicate connections.
    """

    def __init__(self):
        self._connections: dict[str, WebSocketConnection] = {}

    async def connect(
        self,
        organization_id: str,
        url: str,
        headers: dict[str, str] | None = None,
        api_key: str | None = None,
        secret_key: str | None = None,
        on_state_change: Callable[[dict[str, Any]], None] | None = None,
        on_policy_invalidated: Callable[[str, str, int], None] | None = None,
        on_key_rotated: Callable[[str, str, int], None] | None = None,
    ) -> WebSocketConnection:
        """
        Get or create WebSocket connection for an organization.

        Args:
            organization_id: Organization identifier
            url: WebSocket URL
            headers: HTTP headers
            api_key: API key for HMAC verification
            secret_key: Secret key for HMAC verification
            on_state_change: State change callback
            on_policy_invalidated: Callback when policy cache should be cleared
            on_key_rotated: Callback when secret key should be re-fetched

        Returns:
            WebSocketConnection for the organization
        """
        # Return existing connection if available
        if organization_id in self._connections:
            conn = self._connections[organization_id]
            if conn.is_connected:
                return conn
            # Connection was closed, remove it
            del self._connections[organization_id]

        # Create new connection
        conn = WebSocketConnection(
            url=url,
            headers=headers,
            api_key=api_key,
            secret_key=secret_key,
            on_state_change=on_state_change,
            on_policy_invalidated=on_policy_invalidated,
            on_key_rotated=on_key_rotated,
        )
        await conn.connect()
        self._connections[organization_id] = conn
        return conn

    async def disconnect(self, organization_id: str) -> None:
        """
        Disconnect and remove connection for an organization.

        Args:
            organization_id: Organization identifier
        """
        if organization_id in self._connections:
            conn = self._connections[organization_id]
            await conn.close()
            del self._connections[organization_id]

    async def disconnect_all(self) -> None:
        """Disconnect all active connections."""
        for organization_id in list(self._connections.keys()):
            await self.disconnect(organization_id)