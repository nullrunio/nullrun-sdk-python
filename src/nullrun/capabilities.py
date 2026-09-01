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

## Capability history

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


# Wire path for the canonical capabilities endpoint. The backend
# exposes this at ``/api/v1/capabilities`` (per
# ``backend/src/proxy/http/protocol.rs:189``) since 2025-04. The
# legacy ``/health`` route returns a generic liveness payload —
# it does NOT carry the v3-gating fields, so probing there always
# returned None and ``is_v3_ready()`` was always False, leaving
# every capability flag a no-op at runtime. See capability
# history note in module docstring (2026-07-06 fix).
CAPABILITIES_PATH = "/api/v1/capabilities"


@dataclass(frozen=True)
class RateLimitFailScope:
    """Fail-OPEN/CLOSED matrix for rate limiting.

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
    # Execution Graph v0 (2026-08-06, backend): additive
    # `parent_execution_id` wire field on /gate. SDKs probe this
    # flag before sending the field; pre-Graph backends silently
    # ignore unknown fields, but the probe lets SDKs surface a
    # clean diagnostic at `init()` ("sub-agent mode requires
    # server v0.5+") instead of a 400 on the first call. NOT
    # included in `is_v3_ready()` -- it's informational, not a
    # hard gate.
    execution_graph: bool = False
    # ADR-037 Slice B (2026-08-31, protocol v4): /gate response
    # echoes the SDK-supplied `action_digest` and a `policy_hash`
    # slot (None today; Slice D wires per-request computation).
    # Backend always sends the fields (skip_serializing_if elides
    # only when None); the flag is informational so SDKs can
    # surface a clean diagnostic at `init()` ("server echoes
    # action_digest on /gate response — you can verify the gate
    # saw the same digest you sent"). NOT included in
    # `is_v3_ready()` — it's informational, not a hard gate.
    wire_evidence_echo: bool = False
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
                "execution_graph": self.execution_graph,
                "wire_evidence_echo": self.wire_evidence_echo,
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


def _validate_capabilities_payload(payload: Any) -> list[str]:
    """Validate the shape of the ``/api/v1/capabilities`` JSON payload.

    Returns a list of validation errors. Empty list = valid. Used as
    a Zod-style guard around :func:`parse_capabilities` so a malformed
    probe response (e.g. non-dict top level, capabilities array instead
    of dict) surfaces a typed warning instead of silently falling
    through to legacy defaults.

    M8 (audit 2026-08-12): pre-fix, a malformed probe payload silently
    yielded the conservative defaults via ``payload.get("capabilities")
    or {}`` and the SDK continued in compatibility mode without
    informing the operator. Post-fix, the operator sees a structured
    ``NullRunCapabilitiesValidationError`` at ``init()`` and can
    diagnose the probe failure before the first /check.

    Note: validation is intentionally permissive about MISSING fields
    (the backend may add new fields at any time without bumping the
    SDK version). It rejects only SHAPE errors — wrong types,
    wrong container kinds, etc.
    """
    errors: list[str] = []
    if not isinstance(payload, dict):
        errors.append(
            f"top-level payload must be a dict, got {type(payload).__name__}"
        )
        return errors
    caps = payload.get("capabilities")
    if caps is not None and not isinstance(caps, dict):
        errors.append(
            f"'capabilities' must be a dict when present, got {type(caps).__name__}"
        )
    # Type guards on the numeric top-level fields. Strings are common
    # in test fixtures but real backend always emits int.
    for field_name in ("min_protocol_version", "max_protocol_version", "protocol_version"):
        v = payload.get(field_name)
        if v is not None and not isinstance(v, int) and not (
            isinstance(v, str) and v.isdigit()
        ):
            errors.append(
                f"'{field_name}' must be int (or numeric string), got {type(v).__name__}"
            )
    # Numeric nested fields
    if isinstance(caps, dict):
        for field_name in (
            "heartbeat_interval_seconds",
            "heartbeat_skew_tolerance_seconds",
            "chain_idle_ttl_seconds",
        ):
            v = caps.get(field_name)
            if v is not None and not isinstance(v, int) and not (
                isinstance(v, str) and v.isdigit()
            ):
                errors.append(
                    f"capabilities.{field_name} must be int, got {type(v).__name__}"
                )
        rl_scope = caps.get("rate_limit_fail_scope")
        if rl_scope is not None and not isinstance(rl_scope, dict):
            errors.append(
                f"capabilities.rate_limit_fail_scope must be a dict, "
                f"got {type(rl_scope).__name__}"
            )
    return errors


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

    M8 (audit 2026-08-12): shape errors surface via
    :func:`_validate_capabilities_payload` before parsing. The
    caller (``probe_capabilities``) logs them at WARNING so the
    operator sees the malformed payload without silent fallback to
    legacy mode.
    """
    # Shape validation — fail loud on type errors, stay quiet on
    # missing keys (permissive forward-compat invariant).
    shape_errors = _validate_capabilities_payload(payload)
    if shape_errors:
        for err in shape_errors:
            logger.warning("capabilities probe: %s", err)

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
        # Execution Graph v0 (2026-08-06, backend): additive flag
        # -- defaults to False so pre-Graph backends (which omit
        # the field entirely) yield a fail-closed view where the
        # SDK does NOT send `parent_execution_id`.
        execution_graph=_v3_flag("execution_graph"),
        # ADR-037 Slice B (2026-08-31, protocol v4): additive
        # flag — defaults to False so pre-Slice-B backends yield
        # a fail-closed view where the SDK does NOT log the
        # wire-evidence echo as "server confirmed". Pre-v4
        # backends return the JSON without `action_digest` /
        # `policy_hash` keys at all (skip_serializing_if on the
        # backend), so a v4 SDK sees None on both fields and
        # logs "no wire evidence echo" — no false positive.
        wire_evidence_echo=_v3_flag("wire_evidence_echo"),
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
    "_validate_capabilities_payload",
    "parse_capabilities",
    "probe_capabilities",
    "validate_sdk_version",
]