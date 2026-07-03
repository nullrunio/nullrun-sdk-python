"""Server capability probe — used by `init()` to validate SDK ↔ backend compatibility.

Per CLAUDE.md §32 the backend exposes a `/health` (and `/.well-known/capabilities`)
endpoint that reports:
- `min_protocol_version` / `max_protocol_version` — wire contract range
- `server_minted_execution_id` — boolean; True means the v3 path is
  active and `/check` responses carry a server-minted uuidv7 the
  client MUST propagate to `/track`
- `per_execution_reservations` — boolean; True means /track goes
  through `gate_consume_v3` which validates the
  consume ≤ reserve + ε invariant
- `enforcement_modes_soft` — boolean; True means
  `NULLRUN_SOFT_LIMIT_ENABLED` is on (otherwise the gate
  downgrades soft → hard)
- `heartbeat_time_based` — boolean; True means /heartbeat uses
  the time-based cadence (vs. chunk-count deprecated v2 path)

The SDK_MIN_VERSION check is the operational coordination per
CLAUDE.md §0 pre-flip checklist: if the backend requires
`server_minted_execution_id=true` and the SDK is < 0.12.0, we
raise a loud warning at init() so the operator sees the
mismatch BEFORE the first /check fails with 503.

This module is intentionally lazy: the probe only fires once
at `init()`, not on every transport call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("nullrun.capabilities")

# SDK_MIN_VERSION_FOR_V3 — bumped in 0.12.0. The backend uses this
# constant as the gate: any SDK below 0.12.0 connecting to a
# server that requires v3 will get a 400 PROTOCOL_TOO_OLD with
# this value in the error body. Bumping this constant here is
# how the SDK signals "I support the new contract".
SDK_MIN_VERSION_FOR_V3 = "0.12.0"


@dataclass(frozen=True)
class ServerCapabilities:
    """Mirror of the backend's `/health` capability payload.

    Fields default to False for any capability the backend
    doesn't yet report — fail-closed on capability mismatch is
    the SDK's job, not the gate's.
    """

    min_protocol_version: int = 0
    max_protocol_version: int = 0
    server_minted_execution_id: bool = False
    per_execution_reservations: bool = False
    enforcement_modes_soft: bool = False
    heartbeat_time_based: bool = False
    sdk_min_version: str = "0.0.0"
    lua_script_version: str = "unknown"

    def is_v3_ready(self) -> bool:
        """True if the backend supports the v3 wire contract.

        Per CLAUDE.md §0 pre-flip checklist, this is the gate
        for SDK_MIN_VERSION coordination. Old SDKs connecting
        to a v3-ready backend will get 503 RESERVATION_NOT_FOUND
        on /track (their `reservation_id` won't be a Uuid); old
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
            "server_minted_execution_id": self.server_minted_execution_id,
            "per_execution_reservations": self.per_execution_reservations,
            "enforcement_modes_soft": self.enforcement_modes_soft,
            "heartbeat_time_based": self.heartbeat_time_based,
            "sdk_min_version": self.sdk_min_version,
            "lua_script_version": self.lua_script_version,
            "is_v3_ready": self.is_v3_ready(),
        }


def parse_capabilities(payload: dict[str, Any]) -> ServerCapabilities:
    """Parse the backend's `/health` JSON into `ServerCapabilities`.

    Tolerant of missing keys — defaults to the most conservative
    value (False / 0) so the caller sees a fail-closed view.
    """
    return ServerCapabilities(
        min_protocol_version=int(payload.get("min_protocol_version", 0)),
        max_protocol_version=int(payload.get("max_protocol_version", 0)),
        server_minted_execution_id=bool(
            payload.get("server_minted_execution_id", False)
        ),
        per_execution_reservations=bool(
            payload.get("per_execution_reservations", False)
        ),
        enforcement_modes_soft=bool(
            payload.get("enforcement_modes_soft", False)
        ),
        heartbeat_time_based=bool(payload.get("heartbeat_time_based", False)),
        sdk_min_version=str(payload.get("sdk_min_version", "0.0.0")),
        lua_script_version=str(payload.get("lua_script_version", "unknown")),
    )


def probe_capabilities(api_url: str, timeout: float = 2.0) -> ServerCapabilities | None:
    """Fetch and parse `/health` from the backend.

    Returns `None` on any failure (timeout, non-2xx, malformed
    JSON). The caller should NOT treat `None` as a hard error —
    it's advisory. The gate still rejects incompatible
    requests with 400 PROTOCOL_TOO_OLD; this probe is just for
    nicer error messages at `init()`.

    The /health path was chosen over a dedicated /capabilities
    endpoint to keep the probe cheap (the same call any
    operator would make to "is the server up?"). The backend's
    /health response includes all capability fields per
    CLAUDE.md §32.
    """
    url = api_url.rstrip("/") + "/health"
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

    Empty list means "everything looks good". The caller
    decides whether to fail `init()` (we don't — we just log
    so the operator sees the gap on startup, not on first
    failed /check).
    """
    warnings: list[str] = []
    if not caps.is_v3_ready():
        warnings.append(
            f"backend is not v3-ready (capabilities={caps.as_dict()!r}); "
            f"SDK {sdk_version} will still work for v1/v2 endpoints"
        )
        return warnings
    # v3-ready backend — check SDK is new enough.
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
    "SDK_MIN_VERSION_FOR_V3",
    "ServerCapabilities",
    "parse_capabilities",
    "probe_capabilities",
    "validate_sdk_version",
]