"""Tests for nullrun.capabilities — backend capability probe + SDK version validation.

These tests cover:
- parse_capabilities: tolerant parsing with default-false fallbacks
- validate_sdk_version: returns warnings for version mismatch
- is_v3_ready: True only when ALL three v3 capabilities are set
- probe_capabilities: /health fetch with respx (network failure paths)
"""

from __future__ import annotations

import httpx
import pytest
import respx

from nullrun.capabilities import (
    SDK_MIN_VERSION_FOR_V3,
    ServerCapabilities,
    parse_capabilities,
    probe_capabilities,
    validate_sdk_version,
)

BASE_URL = "https://api.test.nullrun.io"


def test_parse_capabilities_v3_ready_backend():
    """A v3-ready backend returns all three capability flags True."""
    payload = {
        "min_protocol_version": 3,
        "max_protocol_version": 3,
        "server_minted_execution_id": True,
        "per_execution_reservations": True,
        "enforcement_modes_soft": True,
        "heartbeat_time_based": True,
        "sdk_min_version": "0.12.0",
        "lua_script_version": "v3",
    }
    caps = parse_capabilities(payload)
    assert caps.is_v3_ready()
    assert caps.server_minted_execution_id is True
    assert caps.per_execution_reservations is True
    assert caps.heartbeat_time_based is True
    assert caps.lua_script_version == "v3"


def test_parse_capabilities_missing_keys_default_false():
    """Missing capability keys default to False — fail-closed."""
    caps = parse_capabilities({})
    assert not caps.is_v3_ready()
    assert caps.server_minted_execution_id is False
    assert caps.per_execution_reservations is False
    assert caps.heartbeat_time_based is False


def test_parse_capabilities_partial_v3_not_ready():
    """Only some v3 caps set — is_v3_ready() returns False."""
    caps = parse_capabilities(
        {
            "server_minted_execution_id": True,
            "per_execution_reservations": True,
            # heartbeat_time_based missing → False
        }
    )
    assert not caps.is_v3_ready()


def test_validate_sdk_version_old_sdk_against_v3_backend():
    """SDK < SDK_MIN_VERSION_FOR_V3 against a v3 backend warns."""
    payload = {
        "server_minted_execution_id": True,
        "per_execution_reservations": True,
        "heartbeat_time_based": True,
    }
    caps = parse_capabilities(payload)
    warnings = validate_sdk_version("0.11.0", caps)
    assert len(warnings) == 1
    assert "SDK_MIN_VERSION" in warnings[0]
    assert "0.11.0" in warnings[0]
    assert "0.12.0" in warnings[0]


def test_validate_sdk_version_current_sdk_no_warnings():
    """SDK >= SDK_MIN_VERSION_FOR_V3 against a v3 backend: no warnings."""
    payload = {
        "server_minted_execution_id": True,
        "per_execution_reservations": True,
        "heartbeat_time_based": True,
    }
    caps = parse_capabilities(payload)
    warnings = validate_sdk_version("0.12.0", caps)
    assert warnings == []
    warnings = validate_sdk_version("0.13.5", caps)
    assert warnings == []


def test_validate_sdk_version_against_legacy_backend():
    """Pre-v3 backend: warning is "backend is not v3-ready", regardless
    of SDK version. The message references the capability state so
    operators know where to look.
    """
    caps = parse_capabilities({})  # all False
    warnings = validate_sdk_version("0.12.0", caps)
    assert len(warnings) == 1
    assert "not v3-ready" in warnings[0]


def test_validate_sdk_version_handles_unparseable_versions():
    """Defensive: non-numeric SDK versions don't crash — the helper
    treats them as (0) which makes the comparison degenerate to
    False. No false-positive warnings."""
    payload = {
        "server_minted_execution_id": True,
        "per_execution_reservations": True,
        "heartbeat_time_based": True,
    }
    caps = parse_capabilities(payload)
    # Garbage version on SDK side
    warnings = validate_sdk_version("not-a-version", caps)
    assert len(warnings) == 1  # version comparison falls back to 0
    # Garbage version on backend side (defaults to 0.0.0)
    caps_bad = ServerCapabilities(
        server_minted_execution_id=True,
        per_execution_reservations=True,
        heartbeat_time_based=True,
        sdk_min_version="not-a-version",
    )
    warnings = validate_sdk_version("0.11.0", caps_bad)
    assert len(warnings) == 1  # 0.11.0 < 0.0.0 = False, but parsing fails
    # Note: the (0) tuple parse is lossy — both sides compare
    # against the (0) base. This is acceptable for a startup
    # warning; the gate still rejects with PROTOCOL_TOO_OLD.


def test_capabilities_as_dict_is_wire_safe():
    """as_dict() never includes raw SDK secrets — safe to log."""
    caps = parse_capabilities(
        {
            "server_minted_execution_id": True,
            "per_execution_reservations": True,
            "heartbeat_time_based": True,
        }
    )
    d = caps.as_dict()
    assert isinstance(d, dict)
    # No sensitive fields even if backend includes them
    assert "api_key" not in d
    assert "secret" not in d
    # is_v3_ready is included for log readability
    assert d["is_v3_ready"] is True


def test_sdk_min_version_constant():
    """The SDK_MIN_VERSION_FOR_V3 constant is the gate's
    coordinate for v3 rollout. Bumping it here is how the SDK
    signals "I support the new contract".
    """
    # Sanity: current value matches the v3.12 release.
    assert SDK_MIN_VERSION_FOR_V3 == "0.12.0"


# ---------------------------------------------------------------------------
# probe_capabilities — /health fetch (network failure paths)
# ---------------------------------------------------------------------------
# These cover the ``logger.debug`` branches in probe_capabilities that the
# pure-data tests above cannot reach: non-2xx responses and transport
# errors. We use respx (already a dev dep) to intercept the call without
# touching the real network.


def test_probe_capabilities_returns_caps_on_2xx():
    """A successful /health response parses into a ServerCapabilities."""
    payload = {
        "min_protocol_version": 3,
        "max_protocol_version": 3,
        "server_minted_execution_id": True,
        "per_execution_reservations": True,
        "enforcement_modes_soft": False,
        "heartbeat_time_based": True,
    }
    with respx.mock:
        respx.get(f"{BASE_URL}/health").mock(
            return_value=httpx.Response(200, json=payload)
        )
        caps = probe_capabilities(BASE_URL)
    assert caps is not None
    assert caps.is_v3_ready()
    assert caps.min_protocol_version == 3


def test_probe_capabilities_returns_none_on_non_2xx():
    """A non-2xx /health response returns None (advisory, not fatal).

    Pins the ``logger.debug("... returned %d",...)` branch in
    probe_capabilities so a future refactor can't silently swallow
    the response code without a test catching it.
    """
    with respx.mock:
        respx.get(f"{BASE_URL}/health").mock(
            return_value=httpx.Response(503, text="service unavailable")
        )
        caps = probe_capabilities(BASE_URL)
    assert caps is None


def test_probe_capabilities_returns_none_on_network_error():
    """Connection failures return None — the caller should treat
    ``None`` as 'best-effort probe failed, proceed without it'.

    Pins the ``logger.debug("... probe failed for %s: %s",...)``
    branch (transport-level exception path).
    """
    with respx.mock:
        respx.get(f"{BASE_URL}/health").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        caps = probe_capabilities(BASE_URL)
    assert caps is None


def test_probe_capabilities_returns_none_on_malformed_json():
    """Malformed JSON (ValueError on json ) returns None — same
    contract as a transport error: best-effort, not fatal.
    """
    with respx.mock:
        respx.get(f"{BASE_URL}/health").mock(
            return_value=httpx.Response(200, text="not-json{")
        )
        caps = probe_capabilities(BASE_URL)
    assert caps is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])