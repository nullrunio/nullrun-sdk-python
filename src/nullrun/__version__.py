"""NullRun Platform SDK.

v3.12 / 0.12.0 (2026-07-03) — server-minted execution_id default ON.

The backend `gate_reserve_v3` now mints a uuidv7 execution_id
internally (CLAUDE.md §24). This version (`0.12.0`) is the
SDK_MIN_VERSION for the v3 rollout — older SDKs continue to
work because the gate IGNORES the client-supplied execution_id
(it mints its own), but they cannot fully participate in the
v3 /track idempotency contract.

---

v3.12 / 0.12.1 (2026-07-04) — bug-fix: complete the wiring
that 0.12.0 advertised.

Honest history: the v0.12.0 changelog entry above said "the
SDK no longer needs to generate its own execution_id for
/check; it gets the server-minted one back in the response
and propagates it to /track", but the propagation code was
NOT shipped in 0.12.0. The 0.12.0 wire was correct in intent
but the SDK still routed through /track/batch and ignored
`response["reservation_id"]` (see
`docs/sdk-v3-migration-gaps.md` and audit memory
`sdk-v3-migration-gaps`).

0.12.1 ships the four missing pieces:

  1. ``_capture_server_minted_execution_id(response)`` reads
     ``reservation_id`` from the /check response into a
     contextvar ``nullrun.context._server_minted_execution_id_var``.
  2. ``_enrich_event`` stamps the captured id onto /track
     payloads (with a 295s freshness guard so an expired
     reservation never ships a doomed id).
  3. ``_route_track`` dispatches ``llm_call`` events to the
     v3 single-event endpoint ``/api/v1/track`` via
     ``Transport.track_single``, so the backend's
     ``gate_consume_v3`` validates the consume-vs-reserve +
     ε invariant (CLAUDE.md §25).
  4. ``NULLRUN_V3_TRACK_DISABLE=1`` opt-out for backends still
     on the v1/v2 path.

Pinning: still SDK_MIN_VERSION_FOR_V3 = "0.12.0". Operators
upgrading from < 0.12.0 should jump straight to 0.12.1 — 0.12.0
released with the integrity bug above and was never deployed
in production with the v3 wiring.

---

v3.12 / 0.12.2 (2026-07-04) — bug-fix: fresh execution_id per
/check + in-process chain-mode gate cache.

Two related correctness fixes on top of 0.12.1:

  1. ``check_workflow_budget`` now sends a fresh ``uuidv7`` as
     ``execution_id`` on every /check call (instead of reusing
     ``workflow_id``). The v3 ``gate_reserve_v3`` mints its
     own anyway, but a client-side placeholder that collides
     across calls confuses the reservation binding on
     /track when ``track_single`` returns 503
     ``RESERVATION_NOT_FOUND`` (CLAUDE.md §29). The server
     overwrites the field on response, so the freshly-minted
     ``reservation_id`` captured by
     ``_capture_server_minted_execution_id`` still drives
     /track exactly as in 0.12.1.

  2. New in-process gate cache
     (``nullrun.runtime._GATE_CACHE``) serves chain-mode
     @protect calls from a 5s TTL on the same
     ``(workflow_id, chain_id, model)`` triple, collapsing
     100-step agent loops to a single /gate roundtrip. Single-
     shot (Hard mode) callers bypass the cache — the gate
     legitimately flips allow→block between consecutive
     calls there, and a stale "allow" could leak a budget-
     exhausted call. Opt-out via
     ``NULLRUN_GATE_CACHE_DISABLE=1`` for callers that want
     the legacy always-roundtrip behaviour (e.g. for live
     smoke tests per docs/runbooks/budget-blue-green-smoke.sh).

No wire-format change. Pure client-side fix — backends on
1.0.0 keep working unchanged. Pinning unchanged:
SDK_MIN_VERSION_FOR_V3 = "0.12.0". Recommended upgrade
path: 0.12.1 -> 0.12.2.
"""

__version__ = "0.12.2"
__platform_version__ = "1.0.0"
