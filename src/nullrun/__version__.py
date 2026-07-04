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

v3.13 / 0.13.0 (2026-07-04) — drift-fixes release: closes the SDK-side
items left over from the docs-vs-code audit captured in
`docs/drift.md`.

  1. ``idempotency_key`` wired onto the v3 /track single-event
     payload. New contextvar
     ``nullrun.context._server_minted_idempotency_key_var`` +
     ``get_/set_/reset_/clear_server_minted_idempotency_key``;
     ``_capture_server_minted_execution_id`` now also captures
     ``response["operation_id"]`` (which equals the /check
     idempotency_key, runtime.py:1260); ``_enrich_event`` stamps
     the value onto the ``wire_event`` for ``llm_call``;
     ``_build_v3_track_payload`` propagates it onto the v3 /track
     body with a contextvar fallback for tests + direct callers.
     Without this, transport-level retry on the same event either
     503'd with ``RESERVATION_NOT_FOUND`` (reservation key DEL'd
     after the first consume per CLAUDE.md §25) or double-billed
     the underlying budget.

  2. Wire ``status_code`` preserved through every decision
     exception class. ``NullRunBlockedException``,
     ``NullRunBudgetError``, ``NullRunChainError``,
     ``NullRunWorkflowInactiveError``,
     ``NullRunConsumeOverbudgetError`` now all accept
     ``status_code: int | None = None``; ``_parse_v3_error_envelope``
     sets it from ``response.status_code`` for every branch —
     402 budget, 403 workflow/chain cross-org, 422
     ``CONSUME_OVERBUDGET``, 503 ``RATE_LIMIT_REDIS_UNAVAILABLE``,
     etc. FastAPI exception handlers reading ``exc.status_code``
     previously got ``None`` / 500 for budget blocks (the backend's
     402 was lost in the constructor chain).

  3. The runtime.py module docstring now distinguishes
     SDK-side transport failure (network/5xx/breaker open →
     fail-OPEN on /check) from wire 4xx/5xx that names an
     enforcement failure (``BUDGET_REDIS_UNAVAILABLE`` → 402
     fail-CLOSED; ``RATE_LIMIT_REDIS_UNAVAILABLE`` → 503
     fail-CLOSED). The README had conflated the two with a single
     "fail-OPEN on infra failures" claim.

Tests:
  * ``tests/test_drift_fixes_2026_07_04.py`` — 15 tests (5 idempotency,
    8 status_code on every decision exception, 2 fail-CLOSED on
    wire 503 RATE_LIMIT_REDIS_UNAVAILABLE).
  * ``tests/test_v3_wire_contract.py::TestGateCacheRuntimeFlow`` — 3
    runtime-level chain-mode cache tests that close the 0.12.2
    patch-coverage gap (dragged codecov/patch below the 70% floor
    on PR #52). Drives ``NullRunRuntime.check_workflow_budget``
    inside ``with workflow(...) + with chain(...)`` to exercise
    cache_enabled / cache-hit / cache-miss /
    cache-bypass-via-env branches (runtime.py:1287-1310).

Backends on 1.0.0 keep working unchanged. Pinning unchanged:
SDK_MIN_VERSION_FOR_V3 = "0.12.0". Recommended upgrade
path: 0.12.2 -> 0.13.0 (no on-wire breaking change; the SDK
will pick up the new idempotency_key stamping automatically).
"""

__version__ = "0.13.0"
__platform_version__ = "1.0.0"
