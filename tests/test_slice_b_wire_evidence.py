"""ADR-037 Slice B (2026-08-31) — wire-evidence echo smoke tests.

These tests pin the SDK-side of the protocol-3→4 additive bump:
the backend's /gate response now echoes the SDK-supplied
`action_digest` and a `policy_hash` slot (None today; Slice D
wires per-request computation). The SDK must:

1. Bump `NULLRUN_PROTOCOL_VERSION` to 4.
2. Capture the response's `action_digest` / `policy_hash` into
   contextvars readable by callers / tests.
3. Tolerate pre-v4 backends (no echo keys in JSON — both reads
   return None).
4. Tolerate malformed wire values (non-str type — drop, do not
   raise).

Wire contract reference: backend/src/proxy/http/gate/internal.rs
(::GateResponse.action_digest + .policy_hash, both
`skip_serializing_if = "Option::is_none"`).

The companion backend smoke checks live in
backend/src/proxy/http/gate/internal.rs::tests::slice_b_* and
backend/src/proxy/http/gate/schemas.rs::tests::slice_b_*.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from nullrun.context import (
    clear_server_minted_execution_id,
    get_last_gate_action_digest,
    get_last_gate_policy_hash,
)
from nullrun.runtime import _capture_wire_evidence
from nullrun.transport import (
    HEADER_PROTOCOL,
    NULLRUN_PROTOCOL_VERSION,
    _protocol_header_value,
)

# ---------------------------------------------------------------------------
# 1. Protocol constant is the single source of truth
# ---------------------------------------------------------------------------


def test_protocol_version_is_four() :
    """The SDK's protocol version MUST be 4 to match the backend
    bump (ADR-037 Slice B, 2026-08-31). The backend reads this
    value off the X-NULLRUN-PROTOCOL header; an SDK <4 fails the
    handshake with 400 PROTOCOL_TOO_OLD.

    Wire-additive — v3 SDKs that haven't bumped yet are unaffected
    (the v4 backend still accepts proto=3 because the bump is
    additive: MIN stays at 2).
    """
    assert NULLRUN_PROTOCOL_VERSION == 4, (
        f"expected NULLRUN_PROTOCOL_VERSION=4 after Slice B "
        f"(ADR-037, 2026-08-31); got {NULLRUN_PROTOCOL_VERSION}. "
        f"This breaks the /gate handshake against the bumped backend."
    )


def test_protocol_header_value_matches_constant() :
    """The header string emitted on /gate MUST match the constant.

    Drift here means the SDK and the backend's
    `X-NULLRUN-PROTOCOL` parser disagree on the wire value.
    """
    assert _protocol_header_value() == "4"
    assert HEADER_PROTOCOL == "X-NULLRUN-PROTOCOL"


# ---------------------------------------------------------------------------
# 2. Wire-evidence capture — happy path
# ---------------------------------------------------------------------------


def test_capture_wire_evidence_extracts_action_digest_from_response() :
    """A /gate response with `action_digest` set MUST be captured
    into the contextvar readable via `get_last_gate_action_digest`.
    This is the architectural invariant the SDK enforces:

        SDK → /gate (action_digest) → response (action_digest)
        ↓                                  ↓
        contextvar.get()                   contextvar.get()

    A reader that does ``get_last_gate_action_digest()`` after a
    /check sees the digest the backend re-verified and echoed.
    """
    clear_server_minted_execution_id()  # also clears v4 echo slots
    digest = (
        "dfc96387ca539b7130caebe705e042f2e34e52ab44352ae5e527bcef64f0df27"
    )
    response = {
        "decision": "allow",
        "execution_id": "01936c5e-7c8a-7def-9a01-abcdef012345",
        "action_digest": digest,
        "policy_hash": None,
    }

    action_digest, policy_hash = _capture_wire_evidence(response)

    assert action_digest == digest
    assert policy_hash is None
    assert get_last_gate_action_digest() == digest
    assert get_last_gate_policy_hash() is None


def test_capture_wire_evidence_extracts_policy_hash_when_present() :
    """When the backend populates `policy_hash` (Slice D future),
    the SDK MUST capture it. Today the backend always sends None,
    but the SDK is forward-compatible so a future Slice D wire
    doesn't require an SDK bump.
    """
    clear_server_minted_execution_id()
    policy_hash = (
        "9b71d224bd62f3785d96d46ad3ea3d73319bfbc2890caadae2dff72519673ca7"
    )
    response = {
        "decision": "allow",
        "execution_id": "01936c5e-7c8a-7def-9a01-abcdef012345",
        "action_digest": (
            "dfc96387ca539b7130caebe705e042f2e34e52ab44352ae5e527bcef64f0df27"
        ),
        "policy_hash": policy_hash,
    }

    action_digest, captured_policy = _capture_wire_evidence(response)

    assert action_digest is not None
    assert captured_policy == policy_hash
    assert get_last_gate_policy_hash() == policy_hash


# ---------------------------------------------------------------------------
# 3. Wire-evidence capture — pre-v4 / no-op backends
# ---------------------------------------------------------------------------


def test_capture_wire_evidence_tolerates_missing_keys() :
    """Pre-v4 backends omit both keys (skip_serializing_if on the
    backend). The SDK MUST NOT raise; both captures must be None.

    This is the v3-SDK-compat smoke check #5 — a v4 SDK against a
    v3 backend sees no echo and degrades gracefully.
    """
    clear_server_minted_execution_id()
    response = {
        "decision": "allow",
        "execution_id": "01936c5e-7c8a-7def-9a01-abcdef012345",
        # No action_digest, no policy_hash
    }

    action_digest, policy_hash = _capture_wire_evidence(response)

    assert action_digest is None
    assert policy_hash is None
    assert get_last_gate_action_digest() is None
    assert get_last_gate_policy_hash() is None


def test_capture_wire_evidence_tolerates_non_dict_response() :
    """Defensive — runtime never passes a non-dict, but a buggy
    transport layer could. Both captures must reset to None and
    the call must not raise.
    """
    clear_server_minted_execution_id()
    # Set a stale value first to confirm the reset
    from nullrun.context import set_last_gate_action_digest
    set_last_gate_action_digest("stale-value-from-previous-block")

    action_digest, policy_hash = _capture_wire_evidence("not a dict")  # type: ignore[arg-type]

    assert action_digest is None
    assert policy_hash is None
    assert get_last_gate_action_digest() is None


def test_capture_wire_evidence_drops_malformed_values() :
    """A buggy proxy could echo a non-string (int, list, bool). The
    SDK MUST log a warning, drop the value (capture stays None),
    and NOT raise — /check must still succeed because the backend
    has already validated the value during /gate processing.
    """
    clear_server_minted_execution_id()

    with patch("nullrun.runtime.logger") as mock_logger:
        response = {
            "decision": "allow",
            "action_digest": 12345,  # type: ignore[dict-item]
            "policy_hash": ["not", "a", "string"],  # type: ignore[list-item]
        }
        action_digest, policy_hash = _capture_wire_evidence(response)

    assert action_digest is None
    assert policy_hash is None
    # Logger emitted a warning for each malformed field
    assert mock_logger.warning.call_count == 2
    assert get_last_gate_action_digest() is None
    assert get_last_gate_policy_hash() is None


# ---------------------------------------------------------------------------
# 4. Integration with _capture_server_minted_execution_id
# ---------------------------------------------------------------------------


def test_execution_id_capture_also_captures_wire_evidence() :
    """The shared call path means a single /check captures both
    execution_id AND wire evidence together. This is the runtime
    invariant: ``response.execution_id`` and
    ``response.action_digest`` always come from the SAME /check
    decision (so a reader that consults both contextvars sees a
    coherent (execution_id, action_digest) tuple).
    """
    clear_server_minted_execution_id()
    digest = (
        "dfc96387ca539b7130caebe705e042f2e34e52ab44352ae5e527bcef64f0df27"
    )

    from nullrun.runtime import _capture_server_minted_execution_id

    response = {
        "decision": "allow",
        "reservation_id": "01936c5e-7c8a-7def-9a01-abcdef012345",
        "operation_id": "11111111-2222-3333-4444-555555555555",
        "action_digest": digest,
    }

    captured_id = _capture_server_minted_execution_id(response)

    assert captured_id == "01936c5e-7c8a-7def-9a01-abcdef012345"
    # Wire evidence was captured on the same call
    assert get_last_gate_action_digest() == digest


def test_execution_id_capture_without_wire_evidence_legacy_backend() :
    """Pre-v4 backend: /check captures execution_id but no
    action_digest (key absent). The wire-evidence capture is a
    no-op (None on both), and the runtime proceeds.
    """
    clear_server_minted_execution_id()

    from nullrun.runtime import _capture_server_minted_execution_id

    response = {
        "decision": "allow",
        "reservation_id": "01936c5e-7c8a-7def-9a01-abcdef012345",
        "operation_id": "11111111-2222-3333-4444-555555555555",
        # No action_digest (pre-v4 backend)
    }

    captured_id = _capture_server_minted_execution_id(response)

    assert captured_id == "01936c5e-7c8a-7def-9a01-abcdef012345"
    # No wire evidence echoed (legacy backend)
    assert get_last_gate_action_digest() is None
    assert get_last_gate_policy_hash() is None


# ---------------------------------------------------------------------------
# 5. Clear semantics — block-exit drops both v4 echo slots
# ---------------------------------------------------------------------------


def test_clear_server_minted_execution_id_drops_wire_evidence() :
    """`clear_server_minted_execution_id` is the runtime's "block
    exited, drop the capture" hook. It MUST also reset the v4
    echo slots so a /check in one block never leaks wire evidence
    into a /track in a sibling block.
    """
    from nullrun.context import set_last_gate_action_digest
    set_last_gate_action_digest("stale-digest")

    clear_server_minted_execution_id()

    assert get_last_gate_action_digest() is None
    assert get_last_gate_policy_hash() is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])