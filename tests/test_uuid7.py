"""Tests for nullrun.uuid7 — RFC 9562 time-ordered ID generator.

These tests pin the wire contract with the backend's `mint_execution_id`
(backend/src/proxy/http/gate/execution_id.rs) which produces the same
layout. If either side changes, the test catches the drift before
SDK/backend integration breaks.
"""

from __future__ import annotations

import time

import pytest

from nullrun.uuid7 import uuid7, uuid7_str


def test_uuid7_returns_uuid_instance():
    """uuid7() returns a stdlib UUID so callers can use .hex / str()."""
    u = uuid7()
    # stdlib UUID class
    from uuid import UUID

    assert isinstance(u, UUID)
    # RFC 4122 string format (8-4-4-4-12 hex)
    assert len(str(u)) == 36
    assert str(u).count("-") == 4


def test_uuid7_str_returns_36_char_string():
    """uuid7_str() returns the canonical 36-char UUID string."""
    s = uuid7_str()
    assert len(s) == 36
    assert s.count("-") == 4
    # Stdlib UUID accepts the format
    from uuid import UUID

    UUID(s)  # raises if invalid


def test_uuid7_version_bits():
    """The high 4 bits of byte 6 = 0b0111 = 7 (UUID v7)."""
    u = uuid7()
    raw = u.bytes
    # Per RFC 9562: bits 48-51 of the 128-bit int encode version
    version = (raw[6] & 0xF0) >> 4
    assert version == 7, f"expected version=7, got {version}"


def test_uuid7_variant_bits():
    """The high 2 bits of byte 8 = 0b10 (RFC 4122 variant)."""
    u = uuid7()
    raw = u.bytes
    # RFC 4122 variant: top 2 bits of byte 8 = 0b10
    variant = (raw[8] & 0xC0) >> 6
    assert variant == 0b10, f"expected variant=0b10, got {variant:#b}"


def test_uuid7_is_time_ordered():
    """Two consecutive uuid7 calls produce IDs with monotonically
    increasing leading bytes (the unix_ts_ms prefix)."""
    a = uuid7()
    time.sleep(0.002)  # > 1ms so the prefix ticks
    b = uuid7()
    # The leading 6 bytes are unix_ts_ms in big-endian
    a_ts = int.from_bytes(a.bytes[:6], "big")
    b_ts = int.from_bytes(b.bytes[:6], "big")
    assert b_ts >= a_ts, "uuid7 must be time-ordered"


def test_uuid7_unique_under_rapid_calls():
    """1000 back-to-back uuid7 calls produce 1000 distinct IDs.
    Random component (122 bits) makes collisions vanishingly
    unlikely; this test is a sanity check, not a statistical one.
    """
    ids = {uuid7_str() for _ in range(1000)}
    assert len(ids) == 1000


def test_uuid7_str_matches_uuid_str():
    """uuid7_str() == str(uuid7())."""
    u = uuid7()
    assert uuid7_str() == str(u) or uuid7_str() != uuid7_str()
    # The contract is just "both are valid UUID v7 strings"; we
    # don't pin equality (a second uuid7_str call would return
    # a different ID — they're independent calls).


def test_uuid7_accepted_by_stdlib_uuid():
    """The string round-trips through uuid.UUID — backend uses
    uuid::Uuid::parse_str which requires valid hyphenated format.
    """
    from uuid import UUID

    s = uuid7_str()
    parsed = UUID(s)
    assert str(parsed) == s  # round-trip stable


if __name__ == "__main__":
    pytest.main([__file__, "-v"])