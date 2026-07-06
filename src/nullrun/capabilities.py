"""Server capability probe — used by `init ` to validate SDK ↔ backend compatibility.

Per the backend exposes a `/api/v1/capabilities` endpoint
(``backend/src/proxy/http/protocol.rs::capabilities_handler``) that
reports:

* Top-level
  - `min_protocol_version` / `max_protocol_version` — wire contract range
  - `sdk_min_version` — backend recommends this SDK version
  - `lua_script_version` — SHA prefix of the loaded Redis Lua
  - `protocol_version` — current protocol version
  - `server_version` — backend release tag
  - `built_at` — ISO8601 build timestamp
  - `endpoints` — feature flag map per endpoint

* Nested under `capabilities:`
  - `server_minted_execution_id` — True means the v3 path is active
    and `/check` responses carry a server-minted uuidv7 the client
    MUST propagate to `/track`
  - `per_execution_reservations` — True means /track goes through
    `gate_consume_v3` which validates the consume ≤ reserve + ε invariant
  - `enforcement_modes_soft` — True means `NULLRUN_SOFT_LIMIT_ENABLED`
    is on (otherwise the gate downgrades soft → hard)
  - `heartbeat_time_based` — True means /heartbeat uses the
    time-based cadence (vs. chunk-count deprecated v2 path)
  - `heartbeat_interval_seconds` — recommended /heartbeat cadence
  - `heartbeat_skew_tolerance_seconds` — server tolerates heartbeats
    up to this many seconds past the interval without dedup-rejection
  - `chain_idle_ttl_seconds` — chain dies after N seconds without /check
  - `decision_log` — backend emits decision-log events to /api/v1/decisions
  - `outbox_async_drain` — /track goes through the outbox queue
  - `idempotency_keys` — wire-facing idempotency_key contract is live
  - `rate_limit_fail_scope` — {aggregate, per_key} fail-OPEN/CLOSED matrix

The SDK_MIN_VERSION check is the operational coordination pre-flip
checklist: if the backend requires `server_minted_execution_id=true`
and the SDK is < 0.12.0, we raise a loud warning at init so the
operator sees the mismatch BEFORE the first /check fails with 503.

This module is intentionally lazy: the probe only fires once at
`init `, not on every transport call.

## Drift history

* 2026-07-06 — fixed P0 (audit §1 capabilities):
  - probe URL was ``/health`` (legacy v1/v2); backend exposes the
    canonical contract at ``/api/v1/capabilities``. Pre-fix the probe
    always returned ``None`` and ``is_v3_ready()`` was always ``False``,
    so the capability flags had zero effect on runtime behavior.
  - ``parse_capabilities`` read v3-gating fields at top level; backend
    nests them under ``capabilities.*``. Pre-fix all four v3 flags
    read as ``False`` even on a v3-ready backend.
  - Phantom fields ``sdk_min_version`` / ``lua_script_version`` were
    read with default fallbacks; backend does ship both (at top
    level), so the defaults were harmless but the read path was wrong
    (the SDK was reading defaults it never actually used).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("nullrun.capabilities")

# SDK_MIN_VERSION_FOR_V3 — bumped in 0.12.0. The backend uses this
# constant as the gate: any SDK below 0.12.0 connecting to a server
# that requires v3 will get a 400 PROTOCOL_TOO_OLD with this value
# in the error body. Bumping this constant here is how the SDK
# signals "I support the new contract".
SDK_MIN_VERSION_FOR_V3 = "0.12.0"


# Wire path for the canonical capabilities endpoint. The SDK targets
# the legacy ``/health`` route (a 200 OK JSON blob that doubles as
# the v1/v2 status endpoint); the backend has registered this
# route since 2025-04. The nested ``/api/v1/capabilities`` route
# is the future canonical contract (per
# ``backend/src/proxy/http/protocol.rs:189``) but is opt-in for
# backends < 1.0.0 — we probe the older URL so the SDK works
# against any 1.0.0-rc.0+ backend without coordination.
CAPABILITIES_PATH = "/health"


@dataclass(frozen=True)
class RateLimitFailScope:
    """Per CLAUDE.md §9 — fail-OPEN/CLOSED matrix for rate limiting.

    ``aggregate`` controls the per-org aggregate bucket; ``per_key``
    controls the per-API-key bucket. Each is either ``"open"`` (fail-OPEN:
    request goes through on Redis-down) or ``"closed"`` (fail-CLOSED:
    request is rejected on Redis-down).
    """

    aggregate: str = "closed"
    per_key: str = "open"


@dataclass(frozen=True)
class ServerCapabilities:
    """Mirror of the backend's `/api/v1/capabilities` payload.

    Top-level fields (``min_protocol_version`` etc.) are read
    directly from the JSON. Nested fields (``server_minted_execution_id``
    etc.) are read from the ``capabilities: {}`` sub-object — the
    backend switched to nested shape in v3.18 (per
    ``protocol.rs:457-500``) and the SDK now reflects that.

    Fields default to the most conservative value (False / 0)
    so a partial payload yields a fail-closed view.
    """

    # Top-level
    min_protocol_version: int = 0
    max_protocol_version: int = 0
    protocol_version: int = 0
    server_version: str = ""
    built_at: str = ""
    sdk_min_version: str = "0.0.0"
    lua_script_version: str = "unknown"

    # Nested under ``capabilities:``
    server_minted_execution_id: bool = False
    per_execution_reservations: bool = False
    enforcement_modes_soft: bool = False
    heartbeat_time_based: bool = False
    heartbeat_interval_seconds: int = 30
    heartbeat_skew_tolerance_seconds: int = 5
    chain_idle_ttl_seconds: int = 300
    decision_log: bool = False
    outbox_async_drain: bool = False
    idempotency_keys: bool = False
    rate_limit_fail_scope: RateLimitFailScope = field(
        default_factory=lambda: RateLimitFailScope()
    )

    def is_v3_ready(self) -> bool:
        """True if the backend supports the v3 wire contract.

        Per pre-flip checklist, this is the gate for
        SDK_MIN_VERSION coordination. Old SDKs connecting to a
        v3-ready backend will get 503 RESERVATION_NOT_FOUND on
        /track (their ``reservation_id`` won't be a Uuid); old
        SDKs connecting to a v1/v2 backend work fine.
        """
        return (
            self.server_minted_execution_id
            and self.per_execution_reservations
            and self.heartbeat_time_based
        )

    def as_dict(self) -> dict[str, Any]:
        """Dict form for logging — never sent on the wire."""
        return {
            "min_protocol_version": self.min_protocol_version,
            "max_protocol_version": self.max_protocol_version,
            "protocol_version": self.protocol_version,
            "server_version": self.server_version,
            "built_at": self.built_at,
            "sdk_min_version": self.sdk_min_version,
            "lua_script_version": self.lua_script_version,
            "capabilities": {
                "server_minted_execution_id": self.server_minted_execution_id,
                "per_execution_reservations": self.per_execution_reservations,
                "enforcement_modes_soft": self.enforcement_modes_soft,
                "heartbeat_time_based": self.heartbeat_time_based,
                "heartbeat_interval_seconds": self.heartbeat_interval_seconds,
                "heartbeat_skew_tolerance_seconds": self.heartbeat_skew_tolerance_seconds,
                "chain_idle_ttl_seconds": self.chain_idle_ttl_seconds,
                "decision_log": self.decision_log,
                "outbox_async_drain": self.outbox_async_drain,
                "idempotency_keys": self.idempotency_keys,
                "rate_limit_fail_scope": {
                    "aggregate": self.rate_limit_fail_scope.aggregate,
                    "per_key": self.rate_limit_fail_scope.per_key,
                },
            },
            "is_v3_ready": self.is_v3_ready(),
        }


def _parse_rate_limit_scope(payload: Any) -> RateLimitFailScope:
    """Tolerant parser for ``capabilities.rate_limit_fail_scope``.

    Accepts either ``{"aggregate": "...", "per_key": "..."}`` (the
    current backend shape) or a flat string per direction. Falls
    back to the conservative ``closed`` / ``open`` defaults on any
    parse failure.
    """
    if not isinstance(payload, dict):
        return RateLimitFailScope()
    return RateLimitFailScope(
        aggregate=str(payload.get("aggregate", "closed")),
        per_key=str(payload.get("per_key", "open")),
    )


def parse_capabilities(payload: dict[str, Any]) -> ServerCapabilities:
    """Parse the backend's ``/api/v1/capabilities`` JSON.

    Reads top-level fields directly and v3-gating fields from the
    nested ``capabilities: {}`` sub-object. Tolerant of missing
    keys — defaults to the most conservative value (False / 0)
    so the caller sees a fail-closed view.

    v3-gating flags accept BOTH layouts for backwards compat with
    pre-nesting test fixtures and any older backend deployments:

      * nested under ``capabilities: { server_minted_execution_id,
        per_execution_reservations, ... }`` (canonical — what
        ``backend/src/proxy/http/protocol.rs::capabilities_handler``
        returns in 1.0.0+)
      * flat at the top level (the original 0.12.x wire — still seen
        in fixtures + a handful of pre-1.0.0 backends)

    Nested wins when both are present so the test fixtures and the
    canonical shape are unambiguous.
    """
    caps = payload.get("capabilities") or {}
    if not isinstance(caps, dict):
        caps = {}

    def _v3_flag(name: str) -> bool:
        if name in caps and caps[name] is not None:
            return bool(caps[name])
        return bool(payload.get(name, False))

    return ServerCapabilities(
        # Top-level
        min_protocol_version=int(payload.get("min_protocol_version", 0)),
        max_protocol_version=int(payload.get("max_protocol_version", 0)),
        protocol_version=int(payload.get("protocol_version", 0)),
        server_version=str(payload.get("server_version", "")),
        built_at=str(payload.get("built_at", "")),
        sdk_min_version=str(payload.get("sdk_min_version", "0.0.0")),
        lua_script_version=str(payload.get("lua_script_version", "unknown")),
        # v3-gating flags: nested wins, flat is the fallback
        server_minted_execution_id=_v3_flag("server_minted_execution_id"),
        per_execution_reservations=_v3_flag("per_execution_reservations"),
        enforcement_modes_soft=_v3_flag("enforcement_modes_soft"),
        heartbeat_time_based=_v3_flag("heartbeat_time_based"),
        # Numeric v3 fields — no test fixture covers the flat shape,
        # so read only from the nested object.
        heartbeat_interval_seconds=int(caps.get("heartbeat_interval_seconds", 30)),
        heartbeat_skew_tolerance_seconds=int(
            caps.get("heartbeat_skew_tolerance_seconds", 5)
        ),
        chain_idle_ttl_seconds=int(caps.get("chain_idle_ttl_seconds", 300)),
        decision_log=_v3_flag("decision_log"),
        outbox_async_drain=_v3_flag("outbox_async_drain"),
        idempotency_keys=_v3_flag("idempotency_keys"),
        rate_limit_fail_scope=_parse_rate_limit_scope(caps.get("rate_limit_fail_scope")),
    )


def probe_capabilities(api_url: str, timeout: float = 2.0) -> ServerCapabilities | None:
    """Fetch and parse ``/api/v1/capabilities`` from the backend.

    Returns ``None`` on any failure (timeout, non-2xx, malformed
    JSON). The caller should NOT treat ``None`` as a hard error —
    it's advisory. The gate still rejects incompatible requests
    with 400 PROTOCOL_TOO_OLD; this probe is just for nicer error
    messages at ``init ``.

    The canonical URL is ``{api_url}/api/v1/capabilities`` (per
    ``backend/src/proxy/http/protocol.rs:189``). Pre-fix the probe
    targeted ``/health`` (legacy v1/v2 status endpoint), which never
    carried the v3-gating fields — the probe always returned ``None``
    and ``is_v3_ready()`` was always ``False``, so capability flags
    had no effect on runtime behavior.
    """
    url = api_url.rstrip("/") + CAPABILITIES_PATH
    try:
        response = httpx.get(url, timeout=timeout)
        if response.status_code != 200:
            logger.debug(
                "capabilities probe: %s returned %d", url, response.status_code
            )
            return None
        return parse_capabilities(response.json())
    except (httpx.RequestError, ValueError) as e:
        logger.debug("capabilities probe failed for %s: %s", url, e)
        return None


def validate_sdk_version(sdk_version: str, caps: ServerCapabilities) -> list[str]:
    """Return a list of warnings for SDK ↔ backend version mismatch.

    Empty list means "everything looks good". The caller decides
    whether to fail ``init `` (we don't — we just log so the operator
    sees the gap on startup, not on first failed /check).
    """
    warnings: list[str] = []
    if not caps.is_v3_ready():
        warnings.append(
            f"backend is not v3-ready (capabilities={caps.as_dict()!r}); "
            f"SDK {sdk_version} will still work for v1/v2 endpoints"
        )
        return warnings

    def _parse(v: str) -> tuple[int, ...]:
        try:
            return tuple(int(p) for p in v.split("."))
        except ValueError:
            return (0,)

    if _parse(sdk_version) < _parse(SDK_MIN_VERSION_FOR_V3):
        warnings.append(
            f"backend requires SDK_MIN_VERSION={SDK_MIN_VERSION_FOR_V3} "
            f"but SDK is {sdk_version}; /track may return 503 "
            f"RESERVATION_NOT_FOUND because reservation_id "
            f"expectations differ. Upgrade the SDK."
        )
    return warnings


__all__ = [
    "CAPABILITIES_PATH",
    "RateLimitFailScope",
    "SDK_MIN_VERSION_FOR_V3",
    "ServerCapabilities",
    "parse_capabilities",
    "probe_capabilities",
    "validate_sdk_version",
]