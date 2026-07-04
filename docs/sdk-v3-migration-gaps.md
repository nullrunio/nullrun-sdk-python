# SDK 0.12.0 → 0.12.1 v3 migration history

This document records the four gaps that existed in the v0.12.0 wire-up
of the SDK's server-minted `execution_id` propagation. **It is kept as
historical evidence of the integrity bug** — the gaps are now closed in
0.12.1 (see `CHANGELOG.md`).

The current canonical implementation lives in:

- `src/nullrun/context.py` — `_server_minted_execution_id_var`,
  `_server_minted_reservation_at_var` + 6 helpers
  (`get_/set_/reset_/clear_` × 2 vars).
- `src/nullrun/runtime.py` — `_capture_server_minted_execution_id`,
  `_route_track`, `_build_v3_track_payload`,
  `SERVER_MINTED_RESERVATION_MAX_AGE_SECONDS = 295.0`.
- `tests/test_v3_server_minted.py` — 27 contract tests pinning each
  step (no live backend required; uses respx to mock /gate, /track,
  /track/batch).

Reference: backend `gate/http/internal.rs::reserve_v3_enabled` mints
the uuidv7 server-side; `proxy/handlers.rs::gate_consume_v3` validates
the v3 reserve→consume invariant (consume ≤ reserve + ε_cents,
CLAUDE.md §25 + ADR-005).

## Why this history matters

0.12.0's `__version__.py` docstring (and the v3.12 backend changelog)
promised propagation that was not yet implemented. The integrity bug
surfaced only when an operator audit compared the version bump against
the actual code paths in `runtime.py::1189-1227`, `_enrich_event`,
and `transport.py::track()`. The fix in 0.12.1 closes the loop and
makes the version honest.

If you ever see a v0.12.0 release without a 0.12.1+ in the same deploy,
treat that deployment as drift — the v3 /track wiring was not yet
active at that version.
