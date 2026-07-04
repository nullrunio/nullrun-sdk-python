# SDK 0.12.2 → 0.13.0 drift audit (2026-07-04)

This document records the drift that was discovered between the
**documented** behaviour in `SDK_README.md` / `CLAUDE.md` and the
**actual** runtime behaviour surfaced during pre-publish manual
review of 0.12.2. It is the companion audit to `sdk-v3-migration-gaps.md`
(which captured the earlier `execution_id` propagation gaps).

**Scope.** SDK-side code only. Backend drift is owned by the
`nullrun-backend` repo under a separate audit log. PR review caught
the bulk of items; runtime tests + manual repro surfaced the rest.

**Why a document, not a CHANGELOG.** The CHANGELOG entry describes
what shipped in 0.13.0; this document is the trail of breadcrumbs so
the next person who reads the SDK can see *why* each item was filed
the way it was filed, and which items are deferred.

---

## Severity model

| Tier | Definition | Disposition |
|---|---|---|
| **P0** | Bug-shape — backend actually returns one status, SDK reports another, *or* a wire contract documented in CLAUDE.md is silently violated on a happy path. | Fix immediately, ship in next patch release. |
| **P1** | Wire-contract honour that the SDK cheats on — the *intent* is documented, the code path doesn't quite get there. | Fix in current minor release (0.13.0). |
| **P2** | Cosmetic / docs-only — README claims feature X, code does X but README says it does Y; no user-visible regression. | Defer to README rewrite PR; do not block release. |
| **Q?** | Open question — agreed intent but ambiguous wire spec, owner undecided. | Track; resolve before next wire spec bump. |

---

## Findings (closed in 0.13.0)

| ID | Tier | Surface | Description | Fix in 0.13.0 |
|---|---|---|---|---|
| **P1-5 + open Q4** | P1 | `/track` v3 single-event (`Transport.track_single`) | `idempotency_key` not wired onto the v3 /track payload. Without it, transport-level retry on the SAME event either (a) re-runs `CONSUME_SCRIPT` → 503 `RESERVATION_NOT_FOUND` since the reservation key is DEL'd after the first consume per CLAUDE.md §25, or (b) double-bills the underlying budget. | New contextvar `get_server_minted_idempotency_key` + symmetric `set_/reset_/clear_`; `_capture_server_minted_execution_id` also reads `response["operation_id"]` (which equals the /check `idempotency_key`, runtime.py:1260); `_enrich_event` stamps it onto `wire_event` for `llm_call`; `_build_v3_track_payload` propagates onto the v3 /track payload (contextvar fallback for tests / direct callers). |
| **P1-1** | P1 | Decision exception class hierarchy | `NullRunBlockedException` / `NullRunBudgetError` / `NullRunChainError` / `NullRunWorkflowInactiveError` / `NullRunConsumeOverbudgetError` did not accept `status_code`. `_parse_v3_error_envelope` had no place to put the wire `response.status_code`. FastAPI exception handlers reading `exc.status_code` previously got `None` / 500 for budget blocks (the backend's 402 was lost in the constructor chain). | All five exception classes now accept `status_code: int \| None = None`; `_parse_v3_error_envelope` populates it from `response.status_code` for every branch — 402 budget, 403 workflow/chain cross-org, 422 `CONSUME_OVERBUDGET`, 503 `RATE_LIMIT_REDIS_UNAVAILABLE`, etc. |
| **P1-2** | P1 | `runtime.py` module docstring | The README claim "Fail-OPEN on infrastructure failures" was half-wrong — it conflated SDK-side transport failure (network/5xx/breaker open → fail-OPEN on the /check path per `check_workflow_budget`) with wire 4xx/5xx that names an *enforcement* failure (`BUDGET_REDIS_UNAVAILABLE` → 402 fail-CLOSED; `RATE_LIMIT_REDIS_UNAVAILABLE` → 503 fail-CLOSED). The two paths must be distinguished. | `runtime.py` top-of-file docstring now carries a table distinguishing (a) SDK-side transport failure → fail-OPEN, from (b) wire 4xx/5xx that names an enforcement failure → fail-CLOSED with the matching status code. |

---

## Findings (deferred — docs only)

| ID | Tier | Surface | Description | Why deferred |
|---|---|---|---|---|
| **P0-1** | P0 → DOCS | `SDK_README.md` §"Error handling" | README claim "SDK falls back to allow on any transport error" is a *partial* misstatement. The truth is in the runtime.py docstring table added by P1-2 above. | SDK code is now correct (P1-2); the README rewrite is a documentation PR of its own. Blocked on its own unrelated doc PR. |
| **P0-2** | P0 → DOCS | `SDK_README.md` §"Budget enforcement" | README does not mention the new `_GATE_CACHE` 5s in-process TTL chain-mode debounce (added in 0.12.2). Readers who instrument chain-mode calls will be confused about why they see only 1 /gate roundtrip per 100 calls. | Same as P0-1 — doc PR. |
| **P0-3** | P0 → DOCS | `SDK_README.md` §"Wire contract" | README still references the pre-0.11.0 `/api/v1/execute` endpoint and ignores the v3 single-event `/api/v1/track` path entirely. | Same as P0-1 — doc PR. |
| **P0-4** | P0 → DOCS | `SDK_README.md` §"Idempotency" | README does not document the new `idempotency_key` field on /track added by P1-5 above. | Same as P0-1 — doc PR. |
| **P1-3** | P1 → DOCS | `SDK_README.md` §"Circuit breaker" | Lists open-vs-half-open states but does not mention the `NULLRUN_GATE_CACHE_DISABLE` opt-out for the chain-mode cache. | Same as P0-1 — doc PR. |
| **P1-4** | P1 → DOCS | `SDK_README.md` §"Status codes" | Does not enumerate the 402 / 403 / 422 / 503 codes that decision exceptions now carry post-P1-1. | Same as P0-1 — doc PR. |

---

## Tests added (`tests/test_drift_fixes_2026_07_04.py`)

- **F1 / P1-5 + Q4** — 5 tests pinning the idempotency-key contextvar lifecycle (`get_/set_/reset_/clear_` × payload-shape assertion) + v3 /track wire propagation
- **F2 / P1-1** — 8 tests pinning that `status_code` survives the constructor chain for every decision exception class
- **F3 / P1-2** — 2 tests pinning that wire `RATE_LIMIT_REDIS_UNAVAILABLE` → 503 is classified as fail-CLOSED on the runtime side (i.e. caller raises, does *not* fall through to the SDK-side fail-OPEN transport-error handler)

## Patch coverage follow-up

0.13.0 also closes the 0.12.2 patch-coverage gap that previously dragged
codecov/patch below the 70% floor. New `TestGateCacheRuntimeFlow` class
in `tests/test_v3_wire_contract.py` drives `NullRunRuntime.check_workflow_budget`
inside `with chain(...)` and exercises the cache_enabled / cache-hit /
cache-miss / cache-bypass branches that were previously uncovered
(`runtime.py:1287-1310`).

## Cross-references

- `docs/sdk-v3-migration-gaps.md` — earlier audit that motivated 0.12.1
- `CHANGELOG.md` 0.13.0 entry — release-facing summary of F1/F2/F3
- `CLAUDE.md` §25 — wire contract for /track consume-vs-reserve
- `CLAUDE.md` §33 — fail-CLOSED exceptions and corresponding wire codes
