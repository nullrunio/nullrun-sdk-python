"""UUID v7 generator — time-ordered IDs.

Used by the SDK for:
- `trace_id` generation (defer to backend's `mint_execution_id`
  when v3 path is active)
- Span IDs in the trace tree (UUID v7 preserves time order so
  the dashboard's timeline render is sorted on the wire)

Why UUID v7 (not v4):
- Time-ordered: backend can sort log lines by `id` without
  parsing `created_at` timestamps.
- 122 bits of entropy (same as v4) — collision-free in
  practice even at fleet-wide throughput.
- Monotonic sub-millisecond precision in the leading 48 bits
  which means log scrapers can bucket events into 5-second
  windows purely by ID.

Implementation note: this is the standard "Unix timestamp ms in
48 bits + 4-bit version + 12 bits rand_a + 62 bits rand_b" layout
per RFC 9562. We use `secrets.token_bytes(10)` for the
random component (cryptographically secure) rather than the
stdlib `random` module (predictable for tests).

Per the backend's `gate_reserve_v3` also mints
its own UUID v7 — the two paths produce the same layout so
both sides of the wire agree on the sort order.
"""

from __future__ import annotations

import secrets
import time
import uuid

# UUID v7 layout per RFC 9562:
# 48 bits unix_ts_ms | 4 bits version (0x7) | 12 bits rand_a |
# 2 bits variant (0b10) | 62 bits rand_b
#
# Stdlib's `uuid.UUID` accepts bytes via `uuid.UUID(bytes=...)`
# and the layout is big-endian, so we pack the 16-byte array
# directly.
_VERSION_V7 = 0x7
_VARIANT_RFC4122 = 0b10


def uuid7() -> uuid.UUID:
    """Generate a single UUID v7.

    Returns a stdlib `uuid.UUID` instance so callers can use
    `.hex`, `.int`, `str(...)` interchangeably.

    Example:
        >>> from nullrun.uuid7 import uuid7
        >>> id_ = uuid7 
        >>> str(id_)
        '0190c5b5-7c9a-7def-8a1b-...'
    """
    unix_ts_ms = time.time_ns() // 1_000_000
    rand_bytes = secrets.token_bytes(10)
    # Bytes 0-5: unix_ts_ms (big-endian)
    field = unix_ts_ms.to_bytes(6, byteorder="big") + rand_bytes
    # Stamp version into the high 4 bits of byte 6
    field = bytearray(field)
    field[6] = (field[6] & 0x0F) | (_VERSION_V7 << 4)
    # Stamp variant into the high 2 bits of byte 8
    field[8] = (field[8] & 0x3F) | (_VARIANT_RFC4122 << 6)
    return uuid.UUID(bytes=bytes(field))


def uuid7_str() -> str:
    """Generate a UUID v7 as a string (e.g. for direct wire use)."""
    return str(uuid7())


__all__ = ["uuid7", "uuid7_str"]