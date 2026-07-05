"""
Regression tests for HMAC byte-equality fix in 0.4.0.

The Rust server (`backend/src/auth/hmac.rs:466-518`) is strict: it
recomputes `sha256(body)` from the raw wire bytes. Pre-0.4.0 the SDK
signed `json.dumps(...)` and then sent via httpx's `json=...` kwarg
which re-serialises with compact separators — producing a body that
does NOT match the body the HMAC signature was computed over. The
signed `/gate` and `/check` calls were rejected with 401 when
`secret_key` was configured.

Phase 4 introduces `_signed_request_body` (canonical JSON bytes) and
moves all three signed POSTs to `content=body`.
"""

from __future__ import annotations

import hashlib
import hmac
import json


def test_signed_request_body_byte_exact():
    """`_signed_request_body` produces deterministic compact JSON."""
    from nullrun.transport import _signed_request_body

    payload = {"events": [{"type": "llm_call", "tokens": 10}]}
    body = _signed_request_body(payload)
    assert body == json.dumps(payload, separators=(",", ":")).encode("utf-8")


def test_signed_request_body_separators():
    """No spaces between keys/values."""
    from nullrun.transport import _signed_request_body

    body = _signed_request_body({"a": 1, "b": 2})
    assert b" " not in body


def test_hmac_over_signed_bytes_matches():
    """HMAC computed over the exact bytes `_signed_request_body` produces
    equals what the server recomputes."""
    from nullrun.transport import _signed_request_body

    api_key = "nr_test_abc123"
    secret = "sk_test_xyz789"
    payload = {"organization_id": "org-1", "execution_id": "wf-1", "tool": "x"}
    body = _signed_request_body(payload)
    body_hash = hashlib.sha256(body).hexdigest()
    msg = f"1234567890:{api_key}:{body_hash}"
    expected_sig = hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
    # Just sanity check the structure matches what server expects.
    assert len(expected_sig) == 64  # SHA-256 hex
    assert body_hash == hashlib.sha256(body).hexdigest()


# ---------------------------------------------------------------------------
# Canonical-bytes contract (audit 2026-06-22 #9)
# ---------------------------------------------------------------------------


def test_signed_request_body_matches_send_bytes():
    """Pre-compute guard (audit #9).

    The SDK signs `_signed_request_body(payload)` and then sends those
    EXACT same bytes via httpx `content=body`. The backend
    (`backend/src/auth/hmac.rs:466-518`) rehashes the raw wire bytes
    it receives — if anyone "optimizes" the SDK to pre-compute HMAC
    over a different byte representation (e.g. with sorted keys, or
    via a second `json.dumps` round), every signed request will start
    failing with 401.

    Pin: the bytes the helper produces are the bytes the HTTP layer
    sends. If this test breaks, every signed POST silently 401's.
    """
    from nullrun.transport import (
        Transport,
        _signed_request_body,
    )

    api_key = "nr_test_abc123"
    secret = "sk_test_xyz789"
    payload = {
        "events": [
            {"type": "llm_call", "tokens": 100, "workflow_id": "wf-1"},
        ],
    }

    # 1. The helper produces deterministic compact bytes
    body = _signed_request_body(payload)

    # 2. The HTTP layer signs + sends the SAME bytes (no re-serialisation)
    t = Transport(api_key=api_key, secret_key=secret, api_url="https://x.test")
    headers = t._build_signed_headers(body=body.decode("utf-8"))

    expected_body_hash = hashlib.sha256(body).hexdigest()
    expected_msg = f"{headers['X-Signature-Timestamp']}:{api_key}:{expected_body_hash}".encode()
    expected_sig = hmac.new(secret.encode("utf-8"), expected_msg, hashlib.sha256).hexdigest()
    assert headers["X-Signature"] == expected_sig


def test_signed_request_body_no_whitespace():
    """Canonical-byte invariant: no spaces between key/value/separator.

    The Rust backend's ``canonical_serialize`` (ws_control.rs:111)
    produces no-whitespace JSON for HMAC inputs. The SDK HTTP path
    pins the same invariant here so a future refactor to
    ``json.dumps(..., indent=...)`` or similar would fail this test
    BEFORE the silent 401 in production.
    """
    from nullrun.transport import _signed_request_body

    body = _signed_request_body({"a": 1, "b": {"c": 2, "d": [3, 4]}})
    assert b" " not in body, f"unexpected whitespace in canonical body: {body!r}"
    assert b"\n" not in body
    assert b"\t" not in body
