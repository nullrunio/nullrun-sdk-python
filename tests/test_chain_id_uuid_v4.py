"""
Regression test for QA finding F5 (2026-08-21): ``chain_id`` MUST be a
UUID v4 string per CLAUDE.md §6.

Pre-fix the ``chain()`` context manager and ``set_chain_id()`` setter
accepted any string (the docstring at line 813 even said "UUID v4 (or
any unique string)"). The backend does NOT validate the chain_id format
— non-UUID chain_ids silently auto-register as new ACTIVE chains. The
backend's chain race guard (``HGET chain_key 'org_id'`` per §6 Q2) only
fires when the chain_id already exists; for a NEW chain_id the SDK
gets a fresh ACTIVE acceptance regardless of format.

The QA probe ``qa/edge_inv_chain.py`` exhausted the failure modes:
- ``"!"`` — non-string-shaped
- ``"12345"`` — too short
- ``"00000000-0000-0000-0000-000000000000"`` — nil UUID (version=0)
- ``"ffffffff-ffff-ffff-ffff-ffffffffffff"`` — not a valid UUID at all
- ``"00000000-0000-4000-8000-000000000000"`` — v4 format but variant
  bit is wrong (should be 8/9/a/b at position 19)

Post-fix the SDK validates UUID v4 format (length, canonical UUID
structure, version=4) and raises ``ValueError`` on malformed input,
surfacing typos and predictable-UUID attacks before they hit the
network.
"""

from __future__ import annotations

import uuid

import pytest

# ---------------------------------------------------------------------------
# _validate_chain_id — pure function tests
# ---------------------------------------------------------------------------


def test_validate_chain_id_accepts_uuid_v4():
    """A canonical UUID v4 string MUST be accepted.

    ``uuid.uuid4()`` is the canonical source — the SDK should accept
    anything the platform uuid module produces for version=4.
    """
    from nullrun.context import _validate_chain_id

    cid = str(uuid.uuid4())
    _validate_chain_id(cid)  # must not raise


def test_validate_chain_id_accepts_known_v4_constants():
    """Well-known UUID v4 test vectors (RFC 4122 §4.4 examples and
    common test fixtures) MUST be accepted."""
    from nullrun.context import _validate_chain_id

    # RFC 4122 §4.4 example UUID v4
    _validate_chain_id("f47ac10b-58cc-4372-a567-0e02b2c3d479")
    # Common test fixture
    _validate_chain_id("550e8400-e29b-41d4-a716-446655440000")
    # All-zero UUID is rejected (see nil_uuid test below)
    # All-ones UUID is rejected (see all_ones_uuid test below)


def test_validate_chain_id_rejects_nil_uuid():
    """The nil UUID (version=0) MUST be rejected — version=4 is
    load-bearing per CLAUDE.md §6 (entropy from random bits)."""
    from nullrun.context import _validate_chain_id

    with pytest.raises(ValueError, match="UUID v4"):
        _validate_chain_id("00000000-0000-0000-0000-000000000000")


def test_validate_chain_id_rejects_all_ones_uuid():
    """The all-ones UUID is NOT a valid UUID (variant bits incorrect)
    — ``uuid.UUID(s)`` raises ValueError which we propagate."""
    from nullrun.context import _validate_chain_id

    with pytest.raises(ValueError, match="UUID v4"):
        _validate_chain_id("ffffffff-ffff-ffff-ffff-ffffffffffff")


def test_validate_chain_id_rejects_non_v4_version():
    """Non-v4 UUIDs (UUID v1, v3, v5, v7) MUST be rejected — the
    backend's chain race guard relies on UUID v4 entropy."""
    from nullrun.context import _validate_chain_id

    # UUID v1 (time-based): version digit = '1' at position 14
    v1_example = uuid.uuid1()
    with pytest.raises(ValueError, match="UUID v4"):
        _validate_chain_id(str(v1_example))

    # UUID v3 (name-based MD5): version digit = '3'
    v3_example = uuid.uuid3(uuid.NAMESPACE_DNS, "example.com")
    with pytest.raises(ValueError, match="UUID v4"):
        _validate_chain_id(str(v3_example))

    # UUID v5 (name-based SHA-1): version digit = '5'
    v5_example = uuid.uuid5(uuid.NAMESPACE_DNS, "example.com")
    with pytest.raises(ValueError, match="UUID v4"):
        _validate_chain_id(str(v5_example))

    # UUID v7 (time-ordered, RFC 9562): version digit = '7'. Construct
    # manually because uuid.uuid7() is not in stdlib before 3.14 (and
    # even then may not be the canonical form).
    v7 = "0189bf94-1e34-7c2a-a706-9c4d3a2b1f08"
    with pytest.raises(ValueError, match="UUID v4"):
        _validate_chain_id(v7)


def test_validate_chain_id_rejects_short_strings():
    """Strings that are too short to be a UUID MUST be rejected
    (the QA probe's ``"12345"`` case)."""
    from nullrun.context import _validate_chain_id

    for bad in ["", "1", "12345", "1234567890"]:
        with pytest.raises(ValueError, match="UUID v4"):
            _validate_chain_id(bad)


def test_validate_chain_id_rejects_malformed_strings():
    """Strings shaped wrong (wrong hyphens, wrong characters) MUST be
    rejected (the QA probe's ``"!"`` case)."""
    from nullrun.context import _validate_chain_id

    for bad in [
        "!",
        "not-a-uuid-at-all",
        "00000000_0000_0000_0000_000000000000",  # underscores instead of hyphens
        "00000000-0000-0000-0000-00000000000",  # 35 chars (missing one)
        "00000000-0000-0000-0000-0000000000000",  # 37 chars (extra one)
        "00000000-0000-0000-0000-00000000000g",  # non-hex char
    ]:
        with pytest.raises(ValueError, match="UUID v4"):
            _validate_chain_id(bad)


def test_validate_chain_id_rejects_non_string_types():
    """Non-string inputs (int, None, bytes, etc.) MUST be rejected
    with a clear ValueError — Python's ``uuid.UUID()`` raises TypeError
    for these, which we wrap as ValueError."""
    from nullrun.context import _validate_chain_id

    for bad in [12345, None, b"00000000-0000-4000-8000-000000000000", 1.0]:
        with pytest.raises((ValueError, TypeError)):
            _validate_chain_id(bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# chain() context manager — integration tests
# ---------------------------------------------------------------------------


def test_chain_context_manager_rejects_non_uuid_v4():
    """The ``chain()`` context manager MUST validate chain_id is a
    UUID v4 BEFORE entering the context — a malformed chain_id must
    raise ValueError, not silently auto-register as a new ACTIVE chain."""
    from nullrun.context import chain

    with pytest.raises(ValueError, match="UUID v4"):
        with chain("!"):
            pass  # should never reach here

    with pytest.raises(ValueError, match="UUID v4"):
        with chain("12345"):
            pass

    with pytest.raises(ValueError, match="UUID v4"):
        with chain("00000000-0000-0000-0000-000000000000"):
            pass  # nil UUID rejected

    with pytest.raises(ValueError, match="UUID v4"):
        with chain("ffffffff-ffff-ffff-ffff-ffffffffffff"):
            pass  # all-ones rejected


def test_chain_context_manager_accepts_uuid_v4():
    """A canonical UUID v4 string MUST be accepted by the context
    manager; the chain_id is set on the contextvar and yielded back."""
    from nullrun.context import chain, get_chain_id

    cid = str(uuid.uuid4())
    with chain(cid) as yielded:
        assert yielded == cid
        assert get_chain_id() == cid


def test_chain_context_manager_resets_after_invalid_chain_id():
    """If ``chain()`` raises ValueError on an invalid chain_id, the
    chain_id contextvar MUST be reset (no leak into outer scope).
    The pre-fix code would have raised inside the context manager
    after setting the var, leaking the bad value to the next
    /check call."""
    from nullrun.context import chain, get_chain_id

    assert get_chain_id() is None  # fresh test
    with pytest.raises(ValueError):
        with chain("!"):
            pass
    assert get_chain_id() is None  # contextvar reset


# ---------------------------------------------------------------------------
# set_chain_id() manual setter — integration tests
# ---------------------------------------------------------------------------


def test_set_chain_id_rejects_invalid_uuid():
    """``set_chain_id()`` MUST validate the chain_id before writing
    to the contextvar (mirrors the context manager)."""
    from nullrun.context import get_chain_id, set_chain_id

    original = get_chain_id()
    try:
        with pytest.raises(ValueError, match="UUID v4"):
            set_chain_id("!")
        assert get_chain_id() == original  # not mutated

        with pytest.raises(ValueError, match="UUID v4"):
            set_chain_id("00000000-0000-0000-0000-000000000000")
        assert get_chain_id() == original
    finally:
        set_chain_id(original)


def test_set_chain_id_accepts_none_to_clear():
    """``set_chain_id(None)`` MUST be accepted (clears the context)
    — the ``None`` value is the documented "no chain" state."""
    from nullrun.context import get_chain_id, set_chain_id

    set_chain_id(str(uuid.uuid4()))
    assert get_chain_id() is not None
    set_chain_id(None)
    assert get_chain_id() is None


def test_set_chain_id_accepts_uuid_v4():
    """``set_chain_id()`` MUST accept a UUID v4 string and write
    it to the contextvar."""
    from nullrun.context import get_chain_id, set_chain_id

    cid = str(uuid.uuid4())
    original = get_chain_id()
    try:
        set_chain_id(cid)
        assert get_chain_id() == cid
    finally:
        set_chain_id(original)
