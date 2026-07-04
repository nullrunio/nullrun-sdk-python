# Changelog

All notable changes to `nullrun-sdk` will be documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html)

---


## [0.12.2] - 2026-07-04

Bug-fix release. Two related correctness fixes layered on top of 0.12.1; no wire-format change.

### Fixed

- **BUG #4 — `/check` execution_id**: `check_workflow_budget()` now sends a fresh `uuidv7` as the `execution_id` field on every call, instead of reusing `workflow_id`. The backend's `gate_reserve_v3` overwrites the field with its own server-minted value on the response, but the previous behaviour could confuse the v3 reservation binding on `/track` when `track_single()` reached the backend — the same root cause as the four gaps 0.12.1 closed, from the client-side placeholder angle. (CLAUDE.md §29 §24 ownership.)
- **BUG #5 — chain-mode gate thrash**: new `nullrun.runtime._GATE_CACHE` (5s TTL, keyed on `(workflow_id, chain_id, model)`) collapses consecutive `/gate` calls from inside `with chain(...)` to a single roundtrip, avoiding 100 /gate calls per 100-step agent loop. Single-shot (Hard mode) callers bypass the cache — the gate legitimately flips allow→block between consecutive calls there, and a stale "allow" would leak a budget-exhausted call through. Opt-out via `NULLRUN_GATE_CACHE_DISABLE=1` for callers that want the legacy always-roundtrip behaviour (e.g. live smoke tests per `docs/runbooks/budget-blue-green-smoke.sh`).

### Added

- 158 lines of contract tests in `tests/test_v3_wire_contract.py`: `TestGateExecutionId` (per-call uniqueness + uuidv7 format validation) and `TestGateCache` (5 cache invariant + opt-out cases).

### Changed

- `__version__` bumped from 0.12.1 to 0.12.2.


## [0.12.1] - 2026-07-04

Bug-fix release. The v0.12.0 changelog claimed the SDK propagates the server-minted `execution_id` from /check to /track but the wiring was never shipped — the SDK still sent client-supplied ids on /track/batch and ignored `reservation_id` on /check responses (audit fix per memory `sdk-v3-migration-gaps`).

This release closes the four gaps documented in `docs/sdk-v3-migration-gaps.md`:

- `check_workflow_budget()` now reads `response["reservation_id"]` and stores it on a contextvar (`nullrun.context._server_minted_execution_id_var`).
- New helpers `set_server_minted_execution_id` / `get_server_minted_execution_id` / `reset_server_minted_execution_id` + a paired `_server_minted_reservation_at` timestamp for the 295s TTL guard.
- `_enrich_event` stamps `execution_id` onto the /track payload when the captured reservation is fresh, and drops it (clearing the capture) once past the safety window — prevents forwarding a doomed id that would 503 on /track per CLAUDE.md section 33.
- `_route_track` routes `llm_call` events to the v3 `/api/v1/track` single-event endpoint via `Transport.track_single()` so backend `gate_consume_v3` validates the consume-vs-reserve + epsilon invariant (CLAUDE.md section 25). Span / tool events keep using the legacy `/api/v1/track/batch`.
- `NULLRUN_V3_TRACK_DISABLE=1` opt-out forces everything through the legacy batch path (backends still on v1/v2).

### Added

- `nullrun.context._server_minted_execution_id_var` + `nullrun.context._server_minted_reservation_at_var` + 6 helpers (`get_/set_/reset_/clear_`).
- `nullrun.runtime._capture_server_minted_execution_id(response)` — defensive UUID parse + warn-on-malformed.
- `nullrun.runtime._route_track(wire_event)` — dispatches to single-event /track or batch /track/batch.
- `nullrun.runtime._build_v3_track_payload(event, reservation_id)` — maps an enriched event onto the v3 /track wire schema.
- 27 contract tests in `tests/test_v3_server_minted.py` covering contextvar hygiene, capture defence-in-depth, _enrich_event age threshold, _route_track dispatch, and end-to-end /gate -> /track round trip.

### Changed

- `__version__` bumped from 0.12.0 to 0.12.1 (post-release integrity fix — the v0.12.0 wiring never shipped before this).

### Fixed

- SDK no longer treats the /check `reservation_id` field as decorative. Each LLM-call track event now carries the server-minted uuidv7 the backend minted, so v3 `gate_consume_v3` can find the matching `reservation:{execution_id}` Redis key (300s TTL).
- LLM-call events now POST to `/api/v1/track` (v3 single-event) instead of `/api/v1/track/batch`. This exercises the consume-vs-reserve invariant that the batch path silently skipped (regression of the v1/v2 `monthly_cost` counter — see CLAUDE.md section 0 G1).

## [0.12.0] - 2026-07-03

Server-minted execution_id default ON. Per CLAUDE.md section 24, every /check now mints a server-side uuidv7 execution_id. The SDK no longer needs to generate its own; the response carries the server-minted id which propagates to /track. This is the SDK_MIN_VERSION for the v3 rollout - older SDKs still work for v1/v2 endpoints but should upgrade.

> **Integrity note (2026-07-04):** the propagation claim in this entry was correct in intent but the actual wiring was not shipped in 0.12.0. See 0.12.1 above for the closing fix.

### Added

- `nullrun.uuid7` module - RFC 9562 section 5.7 time-ordered ID generator. Used internally for trace_id and span IDs.
- `nullrun.capabilities` module - probe_capabilities(), parse_capabilities(), validate_sdk_version(). Wired into nullrun.init().

### Changed

- __version__ bumped from 0.11.0 to 0.12.0.

## [0.9.1] - 2026-06-29

### Added

- `nullrun.uuid7` module - RFC 9562 section 5.7 time-ordered ID generator. Used internally for trace_id and span IDs.
- `nullrun.capabilities` module - probe_capabilities(), parse_capabilities(), validate_sdk_version(). Wired into nullrun.init().

### Changed

- __version__ bumped from 0.11.0 to 0.12.0.

Patch on top of 0.9.0. Unifies the LLM-call fingerprint scheme so the
dedup LRU at `runtime.track()` can collapse sibling emissions from the
httpx transport and the LangChain callback for the same real call.

### Fixed

- **Double-emission of llm_call events.** Pre-0.9.1 the httpx transport
  (`NullRunSyncTransport._emit`) and the LangChain callback
  (`NullRunCallback.on_llm_end`) each computed their own `_fingerprint`
  from different inputs — `sha256(host|status|body)` vs
  `sha256(json({path:"langchain_callback", run_id, response_id, model,
  provider, invocation_params}))`. The two fingerprints never
  collided, so the dedup LRU at `runtime.track()` could not collapse
  the two emissions for the same call. On a typical `app.invoke()`
  with 6 LLM calls the backend saw ~12 `llm_call` events on the wire
  (2 per real call), doubling `llm_call_count` and skewing
  `cost_events` aggregates.

  Post-fix both observers call the same helper
  `_fingerprint_for_llm_call(model, provider, response_id)` with the
  three signals reachable from every observation path:
  - httpx transport reads `model` and `id` straight out of the
    OpenAI-style response body (`payload["model"]`,
    `payload["id"]`). `_openai_extractor` now also carries `"id"` on
    its return so the transport has it without re-parsing the body.
  - LangChain callback reads `model` from `invocation_params` /
    `response.llm_output["model_name"]` and `id` from
    `response.llm_output["id"]` / `response.id` / the generation's
    AIMessage `.id` / `response.response_metadata["id"]` — all four
    locations are populated by langchain-openai 1.x for OpenAI chat
    completions.

  When any of the three signals is missing, the helper falls back to
  the empty string on that slot; the resulting fingerprint is still
  deterministic for the call, just less specific. A missing `id`
  (custom chat-model wrappers that don't surface it) still collapses
  the two observers via the model+provider combination.

### Tests

- `tests/test_unified_fingerprint.py` pins the new contract:
  deterministic fingerprint for identical inputs, distinct
  fingerprints for distinct inputs, the httpx transport calls the
  helper with values extracted from the response body, the LangChain
  callback produces the SAME fingerprint for the same LLM call when
  reading the chat-completion id from any of the four known
  langchain locations.
- `tests/test_llm_call_metadata_flags.py` updated to match the new
  extractor shape (`usage["id"]` is now present alongside
  `usage["model"]`).

No public-API break. No behavior change for callers whose
instrumentation already populates `model` correctly.

## [0.11.0] - 2026-07-02

Wire-protocol v3 alignment with the backend's Sprint 6 v1 cut
(CLAUDE.md v3.4). The previous SDK shipped pre-v3 endpoints
(`/api/v1/gate`, `/api/v1/execute`, `/api/v1/track/batch`) without
the `X-NULLRUN-PROTOCOL` header that the v3 backend requires as a
fail-CLOSED pre-check — every signed POST was rejected with HTTP 400
`PROTOCOL_HEADER_REQUIRED`. This release aligns the SDK with the v3
wire contract and adds the missing soft-mode / chain / heartbeat /
cancel / budget-estimate surface.

### BREAKING (wire-contract)

- **`X-NULLRUN-PROTOCOL: 3` is now mandatory on every signed POST.**
  The backend's `proxy/http/gate/protocol.rs` middleware rejects
  requests without the header with HTTP 400 + error_code
  `PROTOCOL_HEADER_REQUIRED` BEFORE the gate pipeline runs. Pre-v3
  SDKs that don't send it will get 400 on every request, including
  `/auth/verify` (which is unsigned but goes through the same
  protocol guard via the `_post_auth_with_retry` path).
  - Routed through the new centralised helper in
    `nullrun.transport._protocol_header_value()` so a future bump
    is a one-line change.
  - The header is set in `_build_signed_headers()` (covers
    `/gate`, `/execute`, `/track/batch`, `_refetch_credentials`)
    AND inlined in the four call sites that build their own
    headers dict (track/batch, gate, execute, WS handshake,
    auth/verify refresh). The `runtime._auth_headers()` helper was
    extended to include the header for the three direct
    `self._client.get/post` call sites (`_post_auth_with_retry`,
    `_fetch_remote_state`, `get_org_status`).

### Added

- **`Transport.check_v3(request)` — POST /api/v1/check.** The v3
  replacement for `/gate`. Adds three optional wire fields
  (CLAUDE.md §16):
  - `chain_id` (UUID v4) — pairs with `chain_op` for soft-mode
    budget enforcement (CLAUDE.md §5, §6).
  - `chain_op` (`"start"` / `"continue"` / `"end"` / `"auto"`)
    — state-machine transitions; absent defaults to auto-register.
  - `idempotency_key` — replays return the original decision.
  - `stream: bool` — hints the backend whether streaming is
    expected (no wire-enforced behaviour change yet).
  - The response carries a server-minted `execution_id` (§24);
    callers MUST NOT treat the request's `execution_id` as
    authoritative.

- **`Transport.track_single(request)` — POST /api/v1/track.**
  Single-event consume path with the CONSUME_SCRIPT invariant
  (`actual_cost <= reserved_cents + epsilon_cents`, CLAUDE.md §25).
  Returns 422 CONSUME_OVERBUDGET when the call's actual cost
  exceeds the reservation by more than epsilon. The reservation is
  NOT silently re-reserved (ADR-005).

- **`Transport.cancel(execution_id, reason=None)` — POST
  /api/v1/cancel.** Idempotent via `cancel:{execution_id}` SETNX
  (CLAUDE.md §23). Repeated calls return 200 OK without side
  effects. Surfaced as `NullRunRuntime.cancel_execution()` for the
  ergonomic wrapper.

- **`Transport.heartbeat(chain_id)` — POST /api/v1/heartbeat.**
  Atomic `EXPIRE chain:{org}:{chain_id} 300` with SETNX-based
  dedup via `heartbeat:{chain_id}:{ts_floor_30s}` (CLAUDE.md §26).
  Cadence: wall-clock 30s (configurable 10-120s). Skew tolerance
  ±5s.

- **`Transport.chain_end(chain_id)` — POST /api/v1/chain/end.**
  Explicit chain close (CLAUDE.md §6). Idempotent — unknown
  chain_id is a no-op 200. Surfaced as
  `NullRunRuntime.chain_end()`.

- **`Transport.approximate_budget(organization_id=None)` — GET
  /api/v1/budget/approximate.** UI-only budget estimation
  (CLAUDE.md §17). Returns 503 `BUDGET_DATA_UNAVAILABLE` when
  ALL sources fail — NEVER returns 0 (the dashboard must not
  display "≈ $0 spent" when data is missing). Surfaced as
  `NullRunRuntime.approximate_budget()`.

- **`Transport._parse_v3_error_envelope(response, endpoint)`**
  — ACTIVE error envelope parser. Maps the backend's
  `error_code` field to typed SDK exception subclasses
  (PROTOCOL_TOO_OLD → `NullRunProtocolError`, CONSUME_OVERBUDGET
  → `NullRunConsumeOverbudgetError`, CHAIN_CROSS_ORG →
  `NullRunChainError`, WORKFLOW_INACTIVE →
  `NullRunWorkflowInactiveError`, etc.). Coexists with the
  frozen `_parse_error_envelope` from 0.6.0 — the frozen
  helper remains for the audit/contract test surface.

- **Chain context (`nullrun.context`).** New contextvars
  `_chain_id_var` + `_chain_op_var` plus the public API:
  - `chain(chain_id, op="start")` — contextmanager (mirrors
    `workflow()`).
  - `get_chain_id()` / `set_chain_id()` — manual setters.
  - `get_chain_op()` / `set_chain_op()` — chain-op enum setter.
  - Reachable from the top-level `nullrun` namespace via
    `_LAZY_EXPORTS` (consistent with `workflow` /
    `set_call_context`).

- **`NullRunRuntime.ping_chain(chain_id, interval=30.0)` —
  time-based heartbeat scheduler (CLAUDE.md §26).** Returns a
  `stop()` callable. The daemon thread emits POST /heartbeat on
  a wall-clock schedule (`time.monotonic`), not on chunk-count.
  Pre-fix chunk-based heuristic (every 50 chunks) had two
  pathological cases — slow chunk rates left chains idle,
  bursty traffic wasted heartbeat budget on a fresh chain.
  Cadence clamped to the 10-120s policy range per §26.

- **`NullRunRuntime.cancel_execution(execution_id, reason=None)`
  + `chain_end(chain_id)` + `approximate_budget()`** — ergonomic
  wrappers around the new `Transport` methods.

### Added (exceptions)

- `NullRunProtocolError` (NR-P001) — PROTOCOL_TOO_OLD /
  PROTOCOL_TOO_NEW.
- `NullRunChainError` (NR-CH001) — CHAIN_MAX_DURATION_EXCEEDED /
  CHAIN_CROSS_ORG / CHAIN_ORG_MISMATCH / CHAIN_NOT_FOUND /
  CHAIN_EXPIRED. Carries `chain_id` and `backend_code` for
  diagnostic clarity.
- `NullRunConsumeOverbudgetError` (NR-O001) — CONSUME_OVERBUDGET.
  Carries `reserved_cents`, `max_allowed_cents`, `actual_cost_cents`,
  `epsilon_cents` so callers can reconcile manually without
  re-parsing the message string.
- `NullRunWorkflowInactiveError` (NR-W004) — WORKFLOW_INACTIVE
  (CLAUDE.md §4 fail-CLOSED on soft-deleted workflow + active key,
  wired in Sprint 6 v1 12.2).
- `NullRunRateLimitRedisError` (NR-R002) —
  RATE_LIMIT_REDIS_UNAVAILABLE. Fail-CLOSED per §4 enforcement
  table (aggregate rate limit = authoritative gate).

All five are subclasses of either `NullRunInfrastructureError`
(protocol / rate-limit-redis) or `NullRunDecision` (chain /
overbudget / workflow-inactive) so existing `except
NullRunError:` clauses keep matching.

### Changed

- **`check_workflow_budget()` forwards chain context.** When the
  caller has wrapped the gate in `with chain(chain_id, op="start")`,
  the SDK now includes `chain_id` + `chain_op` + `idempotency_key`
  in the /gate (or /check) payload so the backend's Lua
  RESERVE_SCRIPT can run the soft-mode branch (CLAUDE.md §5).
  Absent chain context, behaviour is identical to 0.10.0 (single-
  shot Hard). Wire-shape is additive — legacy callers see no
  payload change.
- **`Transport.check()` (legacy /gate) forwards chain_id /
  chain_op / idempotency_key / stream when present.** Same
  additive contract — missing keys are omitted, not nulled.
- **`_auth_headers()` includes `X-NULLRUN-PROTOCOL`.** Affects
  `_post_auth_with_retry`, `_fetch_remote_state`, `get_org_status`.
- **`runtime._post_auth_with_retry` now passes headers.** Pre-fix
  the helper did `self._client.post(url, json=json_body)` with no
  headers — the wire had no `X-API-Key`, no Authorization, and no
  protocol header, which the backend's protocol + CSRF middlewares
  reject. Now it passes `self._auth_headers()`.

### Backwards compatibility

- All five new `Transport` methods are additive. Existing
  `check()` / `execute()` / batch `_send_batch_with_retry_info`
  paths keep their previous signatures.
- The five new exception classes are subclasses of the existing
  public hierarchy (`NullRunError` ← `NullRunDecision` /
  `NullRunInfrastructureError`); existing `except NullRunError:`
  clauses keep matching.
- The wire-protocol header is mandatory ONLY when connecting to
  a v3-or-later backend. Older pre-v3 backends ignore the header
  — no payload-level break.

### Notes

- The v3 `gate_reserve_v3` Lua script (CLAUDE.md §33) is on
  blue-green deployment per §19 — the SDK must work against
  BOTH the legacy `cost/reservation.rs::reserve_budget_atomic`
  (v1/v2 default) AND the v3 Lua path. The new `check_v3` /
  `track_single` helpers are the v3 path; the legacy `check` /
  batch `track` continue to hit the v1/v2 default. Operators
  flip the backend flag `NULLRUN_RESERVE_V3_ENABLED=1` to
  migrate; SDKs on 0.11.0 work in both modes.
- Soft-mode budget enforcement requires the backend's
  `NULLRUN_SOFT_LIMIT_ENABLED=1` flag (CLAUDE.md §0 G3). Without
  it, chain_id is forwarded but the backend still treats soft
  passes as hard blocks. This is the controlled migration
  state noted in §0.

---

## [0.10.0] - 2026-06-29

(Unreleased — work-in-progress; will be backfilled once 0.11.0
ships.)


---

## [0.9.0] - 2026-06-29

Server-derived coverage replaces the in-process counter dicts.
Counter-bump helpers are gone; every `llm_call` span now carries
`metadata.tracked` and `metadata.streaming_skipped` flags so the
backend's `coverage_pct` query can compute coverage from span
metadata alone. Adds `nullrun.shutdown()` for clean WS close on
script exit.

### Breaking changes

- `NullRunRuntime.coverage_report()` removed.
- `NullRunRuntime._coverage_seen` / `_coverage_tracked` /
  `_coverage_streaming_skipped` instance attributes removed.
- `NullRunRuntime.start_coverage_reporter()` daemon thread removed
  (no longer called from `init()`).
- `_safe_bump_coverage` / `_bump_streaming_skipped` helpers removed
  from `nullrun.instrumentation.auto`.
- `llm_call` wire shape: `metadata.tracked: bool` and
  `metadata.streaming_skipped: bool` are now authoritative; the
  separate `coverage_report` event is dropped.

### Added

- `nullrun.shutdown(timeout=2.0)`: sends a clean WebSocket close
  frame and drains in-flight events. Long-running scripts that
  exit via `sys.exit()` previously let the kernel RST the TCP
  socket, which the backend logged as WARN "Connection reset
  without closing handshake". Registering `nullrun.shutdown` in an
  `atexit` handler eliminates the noisy log. No-op if `init()`
  was never called.

### Tests

- `tests/test_llm_call_metadata_flags.py` pins the new contract:
  every `llm_call` span carries `metadata.tracked` or
  `metadata.streaming_skipped`. Coverage is now an out-of-process
  concern.
- `tests/test_coverage_report.py` and `tests/test_coverage_seen_httpx.py`
  removed — coverage is no longer an SDK-side concept.

---

## [0.8.3] - 2026-06-29

Additive patch on top of 0.8.2. Closes the same silent zero-billing
class of bug 0.8.2 closed on the httpx path — but on the **langgraph
callback path** and the **init-ordering hazard** that 0.8.2 didn't
reach. Promotes the missing-model wire failure from WARN to fail-LOUD.

### Fixed

- **langgraph callback model extraction.** `_extract_model_from_response`
  now consults `response.llm_output` FIRST. langchain-openai 1.x puts
  the date-suffixed model id (e.g. `gpt-4.1-mini-2025-04-14`) on
  `LLMResult.llm_output`, while the AIMessage inside
  `generations[0][0].message` leaves `response_metadata` empty. The
  previous chain led with `response_metadata`, so every
  OpenAI-via-LangChain 1.x call silently zero-billed. Also adds an
  "any key containing model" sweep inside `llm_output` for non-OpenAI
  wrappers (proxies, custom chat models).
- **Init-ordering hazard for `patch_httpx`.** The class-level
  `__init__` wrap only catches Clients created AFTER it is installed.
  Users that build `ChatOpenAI(...)` before `nullrun.init(api_key=...)`
  end up with a pre-existing `httpx.Client` that the patch never sees.
  `patch_httpx` now sweeps `gc.get_objects()` once at install and
  wraps any pre-existing `Client`/`AsyncClient` whose transport isn't
  already a `NullRun*Transport`. Idempotent via the existing
  class-level marker.
- **Fail-LOUD missing-model wire tag.** `runtime.track()` now
  escalates the missing-model warning from `logger.warning` to
  `logger.error`, bumps a `dropped_llm_call_no_model` runtime counter
  for dashboards, and tags the wire event with `__missing_model: True`
  so the backend's `into_track_request` gate can reject with HTTP 422
  instead of silently recording a zero-cost call. The event is still
  sent (not fail-CLOSED) so the backend can audit; the flag is
  wire-private and stripped before persisting. Activated only for
  `llm_call`; other event types are silent.

### Tests

- `tests/contract/test_llm_call_model_wire.py` pins all three
  invariants: 7 unit tests for `_extract_model_from_response`
  (every known langchain shape + non-OpenAI wrappers + empty-string
  fallthrough), 3 tests for `track()`'s missing-model wire tagging
  (ERROR + counter + `__missing_model` flag + non-llm_call silence),
  and 2 tests for the eager-wrap sweep (pre-existing Client gets
  wrapped, idempotent on re-patch).

---

## [0.8.2] - 2026-06-29

Additive patch on top of 0.8.0. No public-API break. Continues the
0.8.0 wire-format audit with two regressions that were caught on
review and one contract test that pins the post-2026-06-27 backend
schema so a future rename can't silently break the SDK.

### Fixed

- **`track_coverage()` emits counter dicts under `event.metadata`
  instead of the event top level.** Pre-fix the per-host `seen` /
  `tracked` / `streaming_skipped` dicts sat at the event root, where
  serde silently dropped them — `SdkTrackRequest` uses explicit
  fields with no `#[serde(flatten)]` catchall, so unknown keys are
  discarded. The dashboard's `last_coverage_pct` was permanently
  `null` because every coverage report landed with empty
  `seen`/`tracked`/`streaming_skipped` JSONB columns. Pin:
  `tests/test_coverage_report.py::test_track_coverage_emits_wire_shape_with_metadata_nesting`.
- **Request-body model fallback in
  `NullRunSyncTransport._emit`.** When the response body extractor
  returns `None` for `model` (OpenAI Responses API, Anthropic
  streaming edge cases), `_extract_model_from_request_body` reads
  the model string the user embedded in the request body via
  `ChatOpenAI(model="gpt-4.1-mini")`. Without this every such
  call was zero-billed — backend `unwrap_or("default")` +
  `DEFAULT_RATE` ≈ \$0/call. Unit-tested in
  `tests/test_model_fallback.py`.

### Tests

- `tests/test_batch_response_parsing.py` pins the post-2026-06-27
  `BatchTrackResponse` shape (`actions: Vec<ActionTaken>`,
  `messages: Vec<String>`) and documents that the legacy
  `actions_taken: Vec<String>` field is intentionally dropped in
  0.8.0. Regression test so a future backend rename can't silently
  break the SDK.

---

## [0.8.0] - 2026-06-28

SDK↔backend wire-format audit. Closes a class of silent-fail-OPEN
path that was sending `model=None` (or `model="unknown"`) on
`/track` for many LLM-vendor paths — every such event cost the
backend a `model_pricing` lookup that returned no row, fell
through to `DEFAULT_RATE` (~$30/M), and emitted a fallback warning
the operator couldn't reproduce because the offending observation
was buried in another package's telemetry.

No public-API break. No behavior change for callers whose
instrumentation already populates `model` correctly. Pure wire-
payload hygiene.

### Fixed

- **`NullRunRuntime.track()` strips `None` values from the wire
  payload.** Pre-0.8.0 the runtime forwarded every key in
  `enriched` except those in `_WIRE_STRIP_FIELDS`, including keys
  whose value was `None`. Putting `{"model": null}` on the wire
  triggered backend `unwrap_or("default")` and a fallback warning.
  Backend handles a missing key as well as `null`; dropping `None`
  here keeps the diagnostic signal loud (the new
  `WARN track(): llm_call event missing 'model' field` fires on
  missing-key, which is what we want operators to see) instead of
  silent (the JSON-null case). Activated only for `llm_call` so
  `span_start` / `span_end` / `tool_call` traffic doesn't pollute
  logs.

- **All four instrumentation paths now extract `model` /
  `provider` from the response object as a fallback, not just
  from `invocation_params` / `self.model`.** When langchain 1.x
  stopped forwarding `invocation_params` to `on_llm_end`, every
  LangChain-callback track event carried `model="unknown"` and
  the backend cost pipeline fell through to `DEFAULT_RATE`. The
  same shape applied to llama-index mock providers and autogen
  subclasses that don't expose a `.model` attribute. New
  fallback chain (per path):

  - `NullRunCallback.on_llm_end` (langgraph): `invocation_params.model_name`
    → `response.response_metadata['model_name']` → AIMessage
    `response_metadata` → `response.llm_output['model_name']` →
    `response.model_name` / `response.model` → `'unknown'`
    (truly last resort, not the common case).
  - `extract_from_event` (llama_index): `event.response.model` →
    `event.response.raw.model` → `usage['model']`. Mock providers
    and adapter-style ChatResponse objects now ship a real model
    id on the wire.
  - `on_messages` (autogen): `self.model` → `result.model`. OpenAI's
    response carries the actual model id (may differ from request
    if the server resolved an alias) — this is the right value.
  - `_emit_from_span` (auto, openai-agents): `span['model']` →
    `usage['model']` → `span['response_metadata']['model_name']`.
    Some custom tracer configs leave `span['model']` empty; the
    other two sources usually have it.

- **Two shared helpers added to `instrumentation/langgraph.py`:**
  `_extract_model_from_response` and `_extract_provider_from_response`.
  These mirror the same best-effort pattern `_get_finish_reason`
  already uses, so we have a single "best-effort read from the
  response object" idiom across the module. The autogen /
  llama_index / agents paths duplicate the walk inline (the
  response shapes differ too much to share a single helper), but
  the *ordering* matches: official-attr → metadata → usage
  → wrapper-attr.

### Operator-visible change

`logger.warning("track(): llm_call event missing 'model' field — backend will fall back to DEFAULT_RATE. event=...")` is now emitted from `NullRunRuntime.track()` whenever an `llm_call` event reaches the wire without a `model` field. This log is the single signal an operator needs to reproduce "which observation (httpx / langchain callback / manual track / agents tracer / requests) produced an `llm_call` without `model` set". Activated only for `llm_call`; other event types are silent. Log destination is whatever the host application configures for the `nullrun.runtime` logger.

### Tests

- Tests covering the new helper chain will land in a follow-up
  release once the wire-format audit findings are stable. The
  fix is a defensive best-effort read; the existing
  `test_instrumentation_*` suites already pass against the
  updated paths.

---

Additive patch on top of 0.7.7. Converts two silent fail-OPEN footguns
into explicit `DeprecationWarning` / `RuntimeError`. No behavior
change for callers who don't touch the deprecated surface.

### Deprecated

- `NullRunRuntime.start_recording()` and `NullRunRuntime.stop_recording()` now emit `DeprecationWarning`. They have been silent no-op stubs since Sprint 2.1 (0.4.0). Decision history is available via the backend dashboard at `/control-center/decision-history`. **Both methods will be removed in 0.9.0.**
- Setting `NULLRUN_USE_GRPC=1` now raises `RuntimeError` at SDK init instead of silently falling back to HTTP with an info log. gRPC transport remains on the roadmap but is not yet implemented. Unset the env var to use HTTP. See https://docs.nullrun.io/reference/sdk-api#transport

### Migration

- Replace `runtime.start_recording(workflow_id, metadata=...)` with a dashboard navigation or `nullrun.status()` introspection.
- Remove any `NULLRUN_USE_GRPC` env var from deployment configs (Docker compose, k8s manifests, systemd units).
- Catch `RuntimeError` at SDK init if you want to keep the env var as a feature flag — but the recommended path is to unset it.

---

## [0.7.8] - 2026-06-28

Additive patch on top of 0.7.7. Converts two silent fail-OPEN footguns
into explicit `DeprecationWarning` / `RuntimeError`. No behavior
change for callers who don't touch the deprecated surface.

### Deprecated

- `NullRunRuntime.start_recording()` and `NullRunRuntime.stop_recording()` now emit `DeprecationWarning`. They have been silent no-op stubs since Sprint 2.1 (0.4.0). Decision history is available via the backend dashboard at `/control-center/decision-history`. **Both methods will be removed in 0.9.0.**
- Setting `NULLRUN_USE_GRPC=1` now raises `RuntimeError` at SDK init instead of silently falling back to HTTP with an info log. gRPC transport remains on the roadmap but is not yet implemented. Unset the env var to use HTTP. See https://docs.nullrun.io/reference/sdk-api#transport

### Migration

- Replace `runtime.start_recording(workflow_id, metadata=...)` with a dashboard navigation or `nullrun.status()` introspection.
- Remove any `NULLRUN_USE_GRPC` env var from deployment configs (Docker compose, k8s manifests, systemd units).
- Catch `RuntimeError` at SDK init if you want to keep the env var as a feature flag — but the recommended path is to unset it.

---

## [0.7.7] - 2026-06-27

Additive patch on top of 0.7.6. Fixes the `/gate` pre-flight so the
backend can compute `projected_cost` and `tool_block` decisions from
real per-call data instead of the previous fake `"budget-precheck"`
sentinel and empty tool list. No breaking changes — new helpers
default to `None` / empty so existing call sites keep working.

### Added

- **`nullrun.set_call_context(model=..., tools=[...])`** — per-call
  context the SDK forwards to `/gate` so the backend can enforce
  budget tiers and tool-block on real values.
  ```python
  import nullrun

  with nullrun.workflow(name="support-bot"):
      nullrun.set_call_context(
          model="claude-sonnet-4-6",
          tools=["shell.run", "code.eval"],
      )

      @nullrun.protect
      def chat(message: str) -> str:
          return agent.run(message)
  ```
  - `model` (optional) — LLM model name. Backend uses it to look up
    the per-model rate from `tool_pricing` (Postgres) so
    `projected_cost` matches what `/track` will compute from real
    token counts. Defaults to `None` (backend falls back to
    `claude-sonnet-4` default rate).
  - `tools` (optional) — list of tool names the call intends to use.
    Backend matches each against the workflow's effective
    `blocked_tools` aggregate and returns `block` on any match.
    `None` leaves whatever was previously set; `[]` clears.
  - `nullrun.get_call_model()` and `nullrun.get_call_tools()` are
    the read-side helpers (also reachable via
    `nullrun.context.get_call_model` / `get_call_tools`).

### Fixed

- **`/gate` pre-flight no longer sends `model="budget-precheck"`.**
  Pre-0.7.7 every SDK `/gate` call for any workflow with a budget
  was hard-blocked because the runtime hard-coded the literal
  string `"budget-precheck"` as the model. The backend's
  `PolicyEvaluationGraph.evaluate()` stub treated any synthetic
  `cost_limit` rule with score > 0.8 as `Block` (see
  `backend/src/policy/graph.rs:448-462`,
  `backend/src/proxy/http/gate/internal.rs:619-628`), so the
  pricing lookup never landed on a real model and the rule fired
  with the wrong score. Now the runtime forwards the model from
  `set_call_context(model=...)` (or `None` when unset), and the
  backend's `calculate_projected_cost` falls through to the
  default rate cleanly.

- **`/gate` pre-flight now forwards the per-call `tools` list.**
  `Transport.check` previously dropped the `tools` key from the
  wire payload, so even when the user called
  `set_call_context(tools=[...])` the backend's
  `gate/internal.rs::check_tool_block` had nothing to match
  against. The transport now propagates `tools` when the runtime
  sets it; `[]` vs missing-`None` are distinguished on the wire
  (per `gate/internal.rs::check_tool_block` doc-comment —
  "no tools will be called" is different from "I did not tell you
  what tools").

### Tests

- **`tests/test_gate_real_path.py`** (new, 226 lines) — regression
  test pinning the fix. Three classes:
  - `TestGateRealPathRegression` — default request now returns
    `allow` (not the old blanket block on the synthetic
    `cost_limit` rule), wire payload contains no
    `policy-N` residue from the old graph plumbing, and a real
    `decision="block"` still raises `WorkflowKilledInterrupt`
    (so the fix didn't accidentally remove the real-block path).
  - `TestSetCallContext` — `set_call_context(model=...)` flows
    into the wire body, `set_call_context(tools=[...])` flows
    into the wire body, no-context means no `tools` key at all
    (not `[]`), and `set_call_context(tools=[])` clears a
    previously-set tool list.
  - `TestPackageExports` — the new helpers are reachable from
    `nullrun.*`.

- `tests/conftest.py` — `reset_runtime` fixture now also clears
  `_call_model_var` and `_call_tools_var` so a test's
  `set_call_context(...)` doesn't leak into the next test's wire
  payload.

---

## [0.7.6] - 2026-06-27

Additive patch on top of the 0.7.0 thin-client refactor. Brings a
FastAPI integration, a default user-facing message catalog, and
small transport consistency fixes. No breaking changes.

### Added

- **`nullrun.integrations.fastapi`** — one-line FastAPI integration
  that turns every `NullRunDecision` / `NullRunInfrastructureError`
  thrown by `@nullrun.protect` endpoints into a clean JSON
  response with the right HTTP status code. No per-endpoint
  `except` blocks required.
  ```python
  from fastapi import FastAPI
  import nullrun
  from nullrun.integrations.fastapi import install

  nullrun.init(api_key="nr_live_...")
  app = FastAPI()
  install(app)

  @app.post("/chat")
  @nullrun.protect
  def chat(message: str) -> str:
      return agent.run(message)
  ```
  Response shape:
  ```json
  {
    "error_code": "NR-B004",
    "user_message": "You've reached the usage limit...",
    "category": "decision"
  }
  ```
  HTTP status mapping:
  - `NR-B004` (budget), `NR-L001` (loop), `NR-R001` (rate) → **429**
    with optional `Retry-After`.
  - `NR-T001` (tool blocked), `NR-X001` (generic block) → **403**.
  - `NR-W003` (paused) → **503** with `Retry-After`.
  - `NR-W002` (killed) → **503**. `WorkflowKilledInterrupt` is a
    `BaseException` subclass so Starlette's `add_exception_handler`
    refuses it; the integration uses an ASGI middleware instead
    (hybrid pattern documented in the module docstring).
  - All `NullRunInfrastructureError` subclasses → **503**
    (failure is on our side, not the user's).

- **`nullrun.messages`** — default user-facing message catalog.
  Every `NR-*` error code has an English default message owned by
  NULLRUN, not by customer code, so a Customer Support Bot hitting
  a budget cap shows the same wording across every NullRun-backed
  application.
  - `format_user_message(exc)` — render an exception as a
    user-facing string.
  - `set_user_message(code, text)` — per-process override for
    branded variants in a single deployment.
  - `get_user_message(code)` — raw lookup.
  - `reset_overrides()` — clear all overrides (for tests).

### Changed

- **`Transport._send_batch` canonical JSON serialization** —
  route the `/track/batch` body through `_signed_request_body` for
  consistent compact-separator serialisation (`,`/`:`). HMAC itself
  is unaffected (it hashes the bytes either way), but consistent
  serialisation removes a special-case from the wire-format contract
  tests. Docstring invariant: "All three signed POST call sites
  MUST serialise via this helper."

- **`Transport._send_batch` actions response handling** —
  backend renamed `BatchTrackResponse.actions_taken` (debug names)
  → `BatchTrackResponse.actions` (`ActionTaken` structs with
  human-readable strings moved to `messages`). Single `/track`
  still uses `TrackResponse.actions_taken`. We read both for
  forward-compat; per-element `try/except` so one malformed
  entry doesn't abort the whole loop.

- **`pyproject.toml` metadata** — long-form description with
  keyword coverage for search, `Maintainer:` populated via
  `maintainers = [...]`, expanded classifiers
  (`OS Independent` / Linux / Windows / macOS,
  Python 3.13, `CPython`, `Security`, `AI`, `WWW/HTTP` topics),
  project URL expander (Discussions / Releases / Source /
  Security Policy).

### Tests

- `tests/test_messages.py` (new, 282 lines) — catalog completeness
  (every NR-* code in `exceptions.py` has a default message),
  override / reset behavior, render path.
- `tests/test_integrations_fastapi.py` (new, 289 lines) — HTTP
  status mapping per error code, response shape, ASGI
  middleware path for `WorkflowKilledInterrupt`, hybrid
  (exception handlers + middleware) composition.
- `tests/test_decision_split.py` (new, 199 lines) — pins the
  decision / infrastructure error split.
- Updates to `tests/test_runtime.py`, `tests/test_extractors.py`
  reflecting transport canonical-JSON + actions-renamed changes.

---

## [0.7.0] - 2026-06-26

### BREAKING CHANGES

SDK is now a thin client. All enforcement decisions arrive from the
backend via `/api/v1/gate` and `/api/v1/execute`. Local policy
enforcement, its dataclass, and its hardcoded thresholds are removed.

**Removed:**

- `class Policy`, `Policy.default_local()`, `Policy.strict_local()`,
  `Policy.from_dict()` (was at `nullrun.runtime.Policy`)
- `NullRunRuntime.policy` property
- `NullRunRuntime(policy=...)` constructor kwarg
- `NullRunStatus.active_policy`, `.fallback_policy`,
  `.fallback_reason`, `.last_policy_fetch`,
  `.last_policy_fetch_age_seconds` fields
- `Transport.fetch_policy()` method
- `Transport.clear_policy_cache()` method
- `FallbackMode.CACHED` enum value (gate-decision fallback)
- Local loop/rate detectors: `LoopTracker`, `RateTracker`,
  `LocalDecision` classes
- `NullRunRuntime._local_check()`, `_loop_tracker`, `_rate_tracker`
  instance attrs
- `_local_loop_threshold`, `_local_rate_limit` instance attrs
  (hardcoded 6/1000)
- `CachedDecision`, `PolicyCache` transport classes (tied to the
  removed CACHED fallback mode)
- `NULLRUN_FALLBACK_MODE` env var
- `NULLRUN_POLICY_FAIL_OPEN` env var (no longer needed — backend is
  authoritative)
- `NullRunRuntime._fetch_policy()` method (no local policy fetch on
  init)
- WS `on_policy_invalidated` callback (no local policy to invalidate)

**Migration:**

If you need to display policy values in a UI, fetch them directly
via `GET /api/v1/orgs/{org_id}/policies`. The SDK no longer mirrors
them.

**Audit:** Drift D-01 from 2026-06-26 SDK↔backend audit
(`PolicyResponse` lacked fields SDK expected; local defaults silently
widened limits).

### Transport finalizer behavior change

`Transport._atexit_flush_safe` is now a no-op that emits a single
`DEBUG` log line. It does NOT persist buffered events to the WAL
anymore — by the time `weakref.finalize` fires, `self._buffer` /
`self._lock` / `self._client` are already gone, so any attempt to
write them would either no-op or crash. **Crash-safety now lives
exclusively in `stop()` and the context-manager pattern.** Callers
who relied on the implicit on-exit WAL flush must switch to:

```python
with nullrun.Transport(api_url=..., api_key=...) as t:
    # use t; __exit__ calls stop() which calls _persist_to_wal
    ...
```

or call `t.stop()` explicitly before process exit. A `DEBUG` log
line "Transport finalizer fired without explicit stop(); remaining
events may be lost" is the user-visible signal that events were
dropped.

---

## [0.6.1] — 2026-06-24

Additive release — Layers 1, 2, and 3 of the "give the user a chance"
design land together. Structured exceptions, a global error hook,
and a synchronous runtime snapshot. No breaking changes.

### Layer 1 — structured exception hierarchy

Every public SDK exception now carries a stable, grep-able
`error_code` (e.g. `NR-A001`, `NR-B002`, `NR-R001`) plus a short
imperative `user_action` and a `retryable` flag, so cookbook
examples and Sentry integrations can branch on the code instead
of parsing the message string.

- **`NullRunError` — structured base for every user-facing SDK
  exception.** Carries four actionable fields:
  - `error_code` — stable `NR-LETTERNNN` identifier
    (documented per-code in `docs/errors/<code>.md`).
  - `user_action` — short imperative next-step hint
    ("Set NULLRUN_API_KEY", "Verify API key at …", "Retry in 30s
    — backend is down", …). Empty when there is no actionable
    step.
  - `retryable` — `True` only for transient failures (5xx,
    network blip, transient auth); `False` for config,
    permission, and budget-exhausted (retrying without
    changing something will just hit the same wall).
  - `docs_url` — per-code docs page (falls back to the
    `https://docs.nullrun.io/errors` index when the per-code
    page does not exist yet).
  - `cause` — optional chained `BaseException`.

- **New specialized exception classes** (each is a subclass of
  the existing user-facing class, so existing `except` clauses
  keep matching):

  | Class | Subclass of | `error_code` | `retryable` |
  |---|---|---|---|
  | `NullRunConfigError` | `NullRunError` | `NR-C001` | False |
  | `NullRunAuthError` | `NullRunAuthenticationError` | `NR-A001` | False |
  | `NullRunBackendError` | `NullRunTransportError` | `NR-B002` | **True** |
  | `NullRunBudgetError` | `NullRunBlockedException` | `NR-X001` | False |
  | `NullRunToolBlockedError` | `NullRunBlockedException` | `NR-T001` | False |

- **Public re-exports** — `nullrun.NullRunError`,
  `nullrun.NullRunAuthError`, `nullrun.NullRunConfigError`,
  `nullrun.NullRunBackendError`, `nullrun.NullRunBudgetError`,
  `nullrun.NullRunToolBlockedError`,
  `nullrun.WorkflowKilledInterrupt` are now in
  `nullrun.__all__` and show up in `dir(nullrun)` for
  discoverability. The legacy types (`NullRunBlockedException`,
  `NullRunAuthenticationError`, `WorkflowKilledException`,
  `WorkflowPausedException`) stay importable via the lazy-export
  table for back-compat — adding them here would change
  `dir(nullrun)` for existing users.

### Layer 2 — `nullrun.on_error()` global hook

- **`nullrun.on_error(hook)` — global error hook.** Fires for
  every structured `NullRunError` *before* the exception
  propagates so the call stack is still live. Returns an
  idempotent `unregister` callable.
  - **Skipped** for `WorkflowKilledInterrupt` (BaseException
    subclass — kill is a signal, not an error) and for
    non-`NullRunError` exceptions.
  - **Multiple hooks** fire in registration order.
  - **Hook exceptions** are caught and logged at DEBUG — a
    misbehaving hook cannot break the SDK.
  - **Zero-cost fast path** when no hook is registered
    (`has_hooks()` short-circuit before any allocation).
- **Backed by** `nullrun.observability.error_hooks` —
  `register_hook`, `unregister_hook`, `emit_error`, `clear_hooks`,
  `STAGES`, `ErrorContext`.

### Layer 3 — `nullrun.status()` introspection

- **`nullrun.status()` — synchronous runtime snapshot.** Returns
  a frozen `NullRunStatus` dataclass (state, version, reason,
  auth state, policy state, connectivity, workflow state,
  bounded recent-errors ring buffer).
  - **Four headline states** derived automatically: `ok`,
    `degraded`, `offline`, `misconfigured`.
  - **Raises** `NullRunConfigError` (`NR-C004`) if no runtime
    has been `init()`'d — never lazily creates a runtime as a
    side effect.
  - **Thread-safe** — safe to call from the agent loop, the
    transport flush thread, or a debug console.
- **Backed by** `nullrun.observability.status` —
  `NullRunStatus`, `RecentError`, `WorkflowState`,
  `_RecentErrorRing`.

### Docs

- **`docs/errors/`** — 15 per-code pages (`NR-A001..A003`,
  `NR-B001..B005`, `NR-C001/C003`, `NR-L001`, `NR-R001`,
  `NR-T001`, `NR-W002/W003`) plus a `README.md` index. Each
  page documents the trigger conditions, the `user_action`,
  the `retryable` hint, and a small reproducer / fix snippet.
- **`docs/integration-baseline-2026-06-19.md`** — pinned
  baseline for the next integration run.

### Tests

- **`tests/test_exception_hierarchy.py`** — pins the
  hierarchy shape (class roots), the structured fields on every
  public class, and the five back-compat invariants (`except`
  clauses keep matching across the new subclasses;
  `WorkflowKilledInterrupt` is the only public class not
  catchable by `except Exception`).
- **`tests/test_error_hooks.py`** — registry basics, `emit_error`
  semantics (fires with both args, swallows hook exceptions,
  one-bad-hook-isolated, unregister-mid-dispatch is safe),
  `ErrorContext` validation, the `WorkflowKilledInterrupt` and
  `WorkflowKilledException` bypass rules, and that the global
  `nullrun.on_error` shim is wired through.
- **`tests/test_status.py`** — no-runtime raises `NR-C004`,
  with-runtime snapshot is frozen / equality-stable, key prefix
  is truncated to 10 chars, state derivation (ok / degraded /
  misconfigured), recent-errors ring buffer (capacity 10, fed
  by `_emit_sdk_error`).
- **`tests/test_integration_contract.py`** — `track_event`
  `setdefault` race pinned against the locked helper.
- **`tests/test_dead_code_removed.py::test_dir_size_unchanged`** —
  rewritten to key off `nullrun.__all__` (source of truth for
  the curated surface) instead of a hardcoded symbol count, so
  the curated-surface contract is still pinned without
  blocking legitimate additions.

### Release plumbing

- The previous `0.6.0` on TestPyPI is **yanked** (visible but
  not installable via `pip install nullrun`) — it predates
  the Layer-1 / Layer-2 / Layer-3 work merged in this release,
  so users who pinned `0.6.0` on TestPyPI should upgrade to
  `0.6.1` to pick up the new structured exceptions and
  observability APIs.

### Back-compat

- Every existing `except` clause keeps matching — the new
  exception classes are subclasses of the existing ones.
- `from nullrun.breaker.exceptions import X` keeps working
  unchanged.
- `pip install nullrun==0.6.1` is a drop-in replacement for
  `0.6.0`.

---

## [0.6.0] — 2026-06-23

Hardening pass driven by the 2026-06-22 SDK↔backend integration audit.
Closes three classes of silent fail-OPEN regressions that the previous
release shipped: SDK POSTs being rejected by the backend's CSRF
middleware, WS HMAC identity field drift, and policy-fetch silently
falling through to a permissive default on any backend blip. Coverage
jumped from ~76% to **84.59%** (branch = true).

### Security (P0 — must-fix)

- **FIX-F3 — every signed POST now carries `Authorization: Bearer <api_key>`.**
  The backend's CSRF middleware (`backend/src/auth/csrf.rs::has_bearer_auth`)
  bypasses the cookie-double-submit check whenever any non-empty
  `Authorization` header is present. Pre-fix the SDK only sent
  `X-API-Key`, so every POST hit the "state-changing request without
  session cookie" branch and got 403 — which the SDK's `try/except`
  around `/gate`, `/track`, `/check`, and `/execute` silently
  swallowed. The net effect was that **every SDK-side enforcement
  gate was effectively fail-OPEN on production traffic**. The fix
  uses the user-facing `api_key` as the Bearer value so the bypass
  header is meaningful for debugging; the canonical auth path is
  still `X-API-Key` (+ HMAC when configured). Safe per
  `csrf.rs:80-95` (browsers never auto-attach `Authorization` to
  cross-site requests, so this is not a CSRF regression).

- **FIX-F4 — WebSocket HMAC identity field pinned to `api_key`.**
  Added `WS_HMAC_IDENTITY_FIELD = "api_key"` constant in
  `transport_websocket.py` matching the backend's
  `SignedWsMessage` struct (`backend/src/proxy/http/ws_control.rs:43`).
  The SDK now reads `data["api_key"]` (with `data["api_key_id"]` as
  a backwards-compat fallback for pre-FIX-F4 servers) to verify the
  HMAC signature. Pre-fix a future server-side rename would silently
  break WS signature verification with no compile-time signal.

### Security (P0 — fail-CLOSED contract)

- **Policy fetch is now fail-CLOSED (F-R2-02).** Pre-fix, any HTTP
  exception, non-200 status, or empty `{"data": []}` response silently
  fell through to `Policy.default_local()` — which had
  `budget_cents=1000`, `rate_limit=100`, `loop_threshold=6`, no tool
  block, i.e. effectively unenforced. A 503 from the backend would
  keep the customer's SDK running with zero enforcement for the rest
  of the session. Post-fix the SDK resolves the policy on this gate in
  priority order: (1) the last known-good cached policy
  (`self._last_good_policy` — written by every successful
  `_fetch_policy`), (2) `Policy.strict_local()` (zero budget cap
  forces the backend reservation service, which is itself
  fail-CLOSED), (3) opt-out via `NULLRUN_POLICY_FAIL_OPEN=1` to
  restore the legacy permissive fallback for tests/staging.
  Mirrors the shape of `NULLRUN_SKIP_BUDGET_CHECK=1` and
  `NULLRUN_SENSITIVE_FAIL_OPEN=1`.

- **`Policy.strict_local()` new classmethod.** Tight fail-CLOSED
  fallback: `budget_cents=0`, `rate_limit=1`, `loop_threshold=1`,
  `retry_threshold=1`. The zero budget cap forces every cost-bearing
  operation through the backend's reservation service. The 1-call
  rate limit caps sustained throughput. The threshold-of-1 loop and
  retry detectors fire on the first suspicious repetition.

### Fixed

- **`Policy.from_dict` now reads `rate_limit_per_minute`** (the
  backend field name from `PolicyResponse` in
  `backend/src/proxy/http/policies.rs`). Falls back to legacy
  `rate_limit` for backwards compat. SDK keeps the local attribute
  name `rate_limit` (cents per minute) — only the wire-mapping
  changes.

- **`_is_acknowledged_state` case-insensitive fallback for WS.**
  New helper on `WebSocketConnection` checks PascalCase first (the
  happy path per `handlers.rs:9258` `as_pascal_case()` normaliser),
  then falls back to lowercase for defensive coverage against server
  regressions to `"killed"`/`"paused"`.

- **Backend policy fetch uses the correct route.** Pre-fix the SDK
  POSTed to `/api/v1/policies` with `organization_id` in the body —
  the backend route is `GET /api/v1/orgs/{org_id}/policies`, so the
  call 404'd and silently fell through to `Policy.default_local()`
  (silent fail-OPEN on every policy fetch).

- **`README.md` PyPI badge switched from `dm` to `dt`.** The daily
  mirror (`dm`) was inflating the displayed download count from
  mirror syncs; the total (`dt`) shows the canonical PyPI total.

### Tests

- **`tests/test_integration_contract.py`** (new, 675 lines, 12 test
  classes). Pins the SDK↔backend wire-format contracts surfaced by
  the 2026-06-22 audit: `Authorization` header on every signed POST
  (FIX-F3), `/api/v1/orgs/{org_id}/policies` and
  `/api/v1/orgs/{org_id}/workflows/{wf}` URL shapes, ACK unit
  discrimination, WS HMAC identity field (FIX-F4), backend
  `PolicyResponse` → SDK `Policy` field mapping, canonical-bytes
  guard against silent re-serialisation drift, sensitive-tool
  routing through `/execute`, fail-CLOSED policy fetch under
  exceptions / 5xx / empty data, outgoing WS ACK is plain JSON (not
  signed — corrects the 0.5.2 overclaim), all five workflow states
  (`running` / `paused` / `killed` / `completed` / `failed`)
  accepted, atomic remote-state registration across concurrent
  reconnects. Each test is paired with a specific backend file —
  update both sides in lock-step, do not edit one side alone.

- **`tests/test_high_reliability_fixes.py`** — re-aligned with the
  fail-CLOSED contract after the master merge; pins the
  last-known-good policy cache priority.

- **`tests/test_hmac_byte_equality.py`** — pinned the
  `content=` vs `json=` body-byte equality that the legacy batch
  path silently broke.

- **`tests/test_ws_signed_payload.py`** — expanded to cover the
  `api_key` / `api_key_id` dual-field WS HMAC identity contract.

- **`tests/test_preflight_fail_policy.py`** — updated to cover
  `NULLRUN_POLICY_FAIL_OPEN=1` opt-out alongside the default
  fail-CLOSED path.

- **Coverage:** 84.59% (branch = true, `fail_under = 82`). Per-file
  leaders: `transport.py` 85.01%, `transport_websocket.py` 65.64%,
  `runtime.py` 83.71%, `instrumentation/auto.py` 70.17% (LLM-vendor
  patches — most remain opt-in), `instrumentation/langgraph.py`
  93.69%, `instrumentation/crewai.py` 90.82%,
  `instrumentation/autogen.py` 93.41%.

---

## [0.3.1] — 2026-06-17

Production-readiness hardening. No public-API changes; the curated 6-symbol
surface is unchanged. Aligns the SDK with the contracts in
`NULLRUN/docs/adr/008-sdk-preflight-fail-policy.md` and
`NULLRUN/docs/kill-contract.md`.

### Fixed (P0 — must-fix)

- **gRPC transport code path removed.** `create_grpc_transport` was
  referenced but never defined, so setting `NULLRUN_USE_GRPC=1` raised
  `NameError` at init. The gRPC server at the platform is intentionally
  frozen until the activation checklist (TLS, auth, proto extensions,
  cost pipeline parity, tests) is complete. The SDK now logs an
  INFO line on `NULLRUN_USE_GRPC=1` and silently falls back to
  HTTP. The `grpcio` hard dependency has been dropped from
  `pyproject.toml`. If/when gRPC is unblocked, the SDK will add it back
  as a separate optional extra.
- **`InsecureTransportError` URL check hardened.** Replaced the
  `startswith("http://127.0.0.1")` chain with a `urllib.parse.urlparse`
  + `ipaddress.ip_address` check. The previous check let
  `http://127.0.0.1.attacker.com` and `http://localhost.evil.com`
  through (homograph attacks) and rejected `http://[::1]:8080`
  (IPv6 loopback). The new check allows the full `127.0.0.0/8`
  IPv4 loopback range, `::1`, and `localhost` (case-insensitive).
- **`signal.signal` global hijack removed.** `Transport.__init__` no
  longer installs a process-wide `SIGTERM` / `SIGINT` handler
  that called `sys.exit(0)` from inside the signal context.
  The fix contract was already pinned in `tests/test_signal_safety.py`
  and is now applied to the source.
- **`atexit.register` replaced with `weakref.finalize`.** The
  per-Transport `atexit` chain was growing without bound in
  long-running deployments; weakref finalizers only fire if the
  transport is still alive at process exit.
- **`Transport` is now a context manager.** `with Transport(...) as t:`
  starts the flush thread on enter and stops it on exit. Replaces
  the manual `start() / stop()` pair that was easy to forget.
- **HMAC body byte-equality in the legacy batch path.** The
  pre-fix code signed `body = json.dumps({"events": batch})` and
  then sent the same payload via httpx's `json=...` parameter,
  which re-serialises with compact separators. The signed bytes
  and the wire bytes were not identical. Now the path uses
  `content=body` so the signed bytes are the wire bytes.
- **All 4 examples fixed.** `basic.py` was calling `init()` with no
  args (raises in 0.3.0). `basic_observe.py` was passing
  `organization_id=` (not in the signature) and calling
  `nullrun.coverage_report()` (did not exist). `cost_dashboard.py`
  was using `Authorization: Bearer` and the non-existent
  `/api/v1/orgs/{org_id}/usage` endpoint. All four now use the
  current SDK surface and the canonical `/api/v1/orgs/{org_id}/status`
  endpoint.

### Fixed (P1)

- **AsyncTransport dead code deleted.** 626 lines of unused
  async transport that had no call sites. Tests already removed.
- **TrackResult dead class deleted.** `track()` returns `dict`,
  not `TrackResult`. The class was unreferenced.
- **Singleton-state lock added.** `init()` now wraps the three
  singleton-slot writes (`NullRunRuntime._instance`,
  `_rt_mod._runtime`, `_dec_mod._runtime`) in a module-level
  `threading.Lock` so concurrent `init()` calls cannot leave
  the slots pointing at two different runtimes.
- **Legacy API key warning.** Pre-Phase-139 API keys (no
  `workflow_id` from `/auth/verify`) now emit a one-time
  WARNING explaining that remote kill/pause will not be
  honoured. Without the warning, the dashboard KILL button
  silently no-ops for users on legacy keys.
- **Distributed circuit-breaker race fix.** The pre-fix code
  defined `_publish_half_open_state` but never called it. The
  `state` property now calls it on the `OPEN → HALF_OPEN`
  transition so other workers see the new state in Redis
  instead of falling back to PERMISSIVE.

### Removed (dead code)

- `AsyncTransport` (626 lines)
- `TrackResult` (12 lines)
- `BoundedDict` cost / loop / retry counters
- `_check_local_limits` (the local budget check that read
  `cost_cents` which the SDK never sets — was dead for the
  public API)
- `StructuredLogger`, `get_logger`, `TenantFilter`,
  `configure_logging_with_tenant_context`, `timed` from
  `observability.py` (zero call sites)
- `tenant_context`, `set_tenant_context`, `get_org_id` from
  `context.py` (zero call sites; `get_org_id` was already
  documented as gone in 0.3.0 CHANGELOG)
- `instrumentation/openai.py` (the v0.x patcher that no
  longer applied to `openai>=1.0`)

### Added

- `NullRunRuntime.coverage_report()` — public method that
  returns `{"seen": ..., "tracked": ...,
  "streaming_skipped": ...}`. The auto-instrumentation layer
  already populates the counters; this method just exposes
  them. Called by `examples/basic_observe.py`.
- `Transport.__enter__` / `__exit__` (see above)
- `tests/test_init_contract.py` — pins the 0.3.0 init
  contract (api_key required, singleton state, no
  organization_id kwarg)
- `tests/test_insecure_transport.py` — homograph / IPv6 /
  case-insensitive coverage for the new URL check
- `tests/test_grpc_removed.py` — pins the post-deletion
  gRPC contract
- `tests/test_legacy_key_warning.py` — pins the legacy
  API key warning
- `tests/test_cb_halfopen_publish.py` — pins the
  HALF_OPEN Redis publish
- `tests/test_kill_deprecation.py` — pins the
  `WorkflowKilledInterrupt` deprecation-bypass contract

### Documentation

- `WorkflowKilledInterrupt` docstring now includes a
  "Catching in production" section with the recommended
  Sentry / OpenTelemetry pattern (`except BaseException`,
  not `except Exception`).
- `NULLRUN/docs/sdk/README.md` rewritten to match the
  actual 6-symbol SDK surface and current `track_*`
  signatures. The previous 7-symbol reference was a
  description of an older design that did not match the
  shipped SDK.

## [0.5.2] — 2026-06-19

This release bundles the Sprint 2.5 production-readiness hardening
alongside the Phase 0 contract / lifecycle fixes. The two streams were
shipped as separate `[Unreleased]` sections during development; they
are merged here into a single canonical entry so release tooling that
scans for the `[Unreleased]` anchor picks up the complete change set
exactly once.

### Added (production-readiness hardening)

- **HMAC signing expanded (with documented exceptions, audit 2026-06-22
  round 2 — F-R2-05 / F-R2-14).** The SDK now signs every
  outgoing POST/GET that the backend's `HMAC_REQUIRED_PATHS` allowlist
  requires: `/track/batch`, `/gate`, `/check`, `/execute`. The
  header set is built via `_add_hmac_headers` (Content-Type,
  X-Signature, X-Signature-Timestamp, X-API-Key, Authorization for
  CSRF bypass). Compliance with the canonical
  `HMAC-SHA256(secret_key, "<ts>:<api_key>:<sha256_hex(body)>")`
  formula from `backend/src/auth/hmac.rs:6-9`.

  **Explicitly NOT signed (chicken-and-egg / backend allowlist):**
  - `runtime._authenticate` → `POST /api/v1/auth/verify` on initial
    bootstrap: no `secret_key` exists yet (it is what /auth/verify
    hands back). The key-rotation refetch
    (`Transport._refetch_credentials` at transport.py:1588) IS
    signed because `secret_key` is then populated.
  - `runtime._fetch_policy` → `GET /api/v1/orgs/{id}/policies`.
    Not in `HMAC_REQUIRED_PATHS` (`backend/src/proxy/middleware/
    hmac_verify.rs:58`). Backend allowlist is authoritative.
  - `runtime._fetch_remote_state` → `GET /api/v1/orgs/{id}/workflows/
    {wf}`. Not in `HMAC_REQUIRED_PATHS`.
  - `runtime.get_org_status` → `GET /api/v1/orgs/{id}/status`. Not in
    `HMAC_REQUIRED_PATHS`.

  **Outgoing WebSocket ACK is plain JSON, not signed.** Earlier
  documentation overstated this — `transport_websocket._send_ack`
  sends `{"type": "ack", "message_id", "received_at"}` as plain
  JSON without an HMAC signature. The backend does not currently
  verify ACK authenticity (`ws_control.rs:842-848` is a TODO).
  If that ever changes, the SDK will sign the ACK using the
  same `WS_HMAC_IDENTITY_FIELD` + `secret_key` path as incoming
  messages — until then, treat CHANGELOG claims of "signed ACKs"
  as inaccurate.

- **WebSocket protocol compliance (Phase 2 of the plan).** The SDK now
  honours `resync_required` (closes the connection, clears local state,
  reconnects — no merge per ADR-007), enforces per-workflow `version`
  monotonic dedup (drops events with `version <= last` to survive
  at-least-once delivery), and signs outgoing ACKs. The URL uses
  `X-API-Key` header (never the query string — per SEC-7, the server
  rejects `?api_key=…`).

- **`track_event` fingerprint + coverage counters (Phase 3).** `track_event`
  now emits a stable `_fingerprint` so the dedup LRU at the `track()`
  sink collapses repeat emissions of the same event (the user's manual
  `track_event` plus the httpx transport hook firing on the same LLM
  call). The fingerprint is stripped before the wire send. The
  `_coverage_seen` / `_coverage_tracked` / `_coverage_streaming_skipped`
  counters are now initialised in `__init__` so the
  `_safe_bump_coverage` helper in `nullrun.instrumentation.auto`
  actually increments the dashboard's coverage tab.

- **`SENSITIVE_ARG_KEYS` expanded from 7 to 29 tokens.** Now masks
  `password`, `passwd`, `pwd`, `token`, `secret`, `api_key`, `apikey`,
  `key`, `auth`, `authorization`, `bearer`, `session`, `session_id`,
  `cookie`, `access_token`, `refresh_token`, `id_token`, `private_key`,
  `secret_key`, `email`, `phone`, `ssn`, `credit_card`,
  `credit_card_number`, `cvv`, `cvc`, `pin`, `otp`, `mfa`. Matching
  is case-insensitive.

- **Recursive `_safe_error_str` (Phase 3).** The previous one-level
  regex was replaced with a balanced-brace walker that handles
  arbitrary nesting depth and dict values that contain `{` / `}` in
  string content. Bare `details=foo` (no opening brace) is preserved
  so we don't lose free-form text.

- **`RateLimitError` exception class (Phase 4).** A new
  `RateLimitError(NullRunTransportError)` carries the parsed
  `Retry-After` (seconds) and `upgrade_url` from the 429 envelope
  per `contracts/errors.ts`. The transport layer's
  `_parse_error_envelope` helper maps 4xx / 5xx / 429 to typed
  exceptions (`NullRunAuthenticationError` /
  `NullRunTransportError(GATEWAY_ERROR)` / `RateLimitError`) so
  callers can branch on the type instead of string-matching
  `str(exc)`.

- **`Transport.post_signed_with_401_retry` helper (Phase 4).** The
  runtime can opt into transparent one-shot re-authentication on
  HTTP 401 by passing a `reauth_callback` (typically
  `lambda: self._authenticate()`). The first 401 re-calls
  `auth/verify` to pick up the freshly-rotated `secret_key` and
  retries the original request. A second 401 propagates as
  `NullRunAuthenticationError`.

- **`PolicyCache.clear()` (Phase 2).** New method on the transport's
  policy cache so the `PolicyInvalidated` WebSocket callback can
  flush every cached decision atomically. The
  `Transport.clear_policy_cache` public method now delegates to it
  instead of poking the internal `_cache` dict.

- **`_fingerprint_for_event_dict` helper (Phase 3).** New in
  `nullrun.instrumentation.auto` for the generic event-dict
  fingerprint used by `track_event` (the existing
  `_fingerprint_for` is for HTTP responses keyed on host+body+status).

- **Async Policy Cache**: `AsyncTransport` now uses `PolicyCache` for CACHED fallback mode. Previously the async transport always fell back to PERMISSIVE when gateway was unreachable. Now it caches successful execute decisions and uses them when gateway is unavailable.

- **Custom Sensitive Tools API**: Added `add_sensitive_tool()`, `remove_sensitive_tool()`, `register_sensitive_tools()`, and `get_sensitive_tools()` methods to `NullRunRuntime`. Users can now register custom tools as sensitive requiring strict mode enforcement.

- **`NullRunBlockedException.tool_name` attribute** (FIX-5): The `tool_name`
  kwarg is now a first-class attribute on `NullRunBlockedException`
  (and its subclasses `LoopDetectedException`, etc.) instead of being
  absorbed into `**details`. Cookbook examples that read `exc.tool_name`
  no longer raise `AttributeError`. Backwards-compatible: `tool_name`
  defaults to `None` and does not appear in `exc.details` when unset.
  The stringified exception now includes `tool={name}` when set.

- **`check_control_plane` is case-insensitive on the state value.**
  SDK now normalises the state with `.lower()` before comparing to
  `"paused"` / `"killed"`. Pre-fix a backend regression to UPPERCASE
  (e.g. `"KILLED"` in `state_change`) would have silently failed the
  match and let a killed workflow keep running. Backend already emits
  PascalCase per the `as_pascal_case()` normaliser in
  `handlers.rs:9258`; this is defensive per `analyze.md` §11.6.

### Removed (Phase 5)

- **Empty placeholder modules deleted.** `src/nullrun/flow/`,
  `src/nullrun/gate/`, `src/nullrun/common/` were placeholders for
  promised-but-unimplemented products. Removed.
- **Orphan `protos/` directory deleted.** `grpc_transport.py` was
  removed in 0.4.0; the proto schema is no longer needed in the SDK.
- **`instrumentation/openai.py` (v0.x patcher) deleted.** It patched
  `openai.ChatCompletion.create` which `openai>=1.0` does not
  expose. All OpenAI v1.0+ traffic is now tracked via the httpx
  transport hook in `nullrun.instrumentation.auto`.
- **`DecisionHistoryRecorder.replay_locally` / `replay_event` /
  `replay_from_file` deleted.** They called `runtime.track` (which
  hits the backend) despite the docstring claiming "local-only".
  The honest-scope local recorder surface (`start_recording`,
  `stop_recording`, `record_event`, `estimate_cost`,
  `RecordingSession.to_dict` / `from_dict`) is preserved.
- **`observability.TenantFilter` no longer writes the deprecated
  `org_id` field** — only the canonical `organization_id` and
  `api_key_id` remain. The legacy `get_org_id()` helper is gone
  alongside the workspace_id → organization_id migration.

### Fixed

- **`examples/cost_dashboard.py`** switched from
  `Authorization: Bearer` (which the SDK never uses on the user's
  behalf) to `X-API-Key`, and from the non-existent `/usage`
  endpoint to the canonical `/quota` per `contracts/openapi.yaml`.

- **P0-1 (PCI-DSS / GDPR): positional PII masking.** Sensitive tools
  called positionally (e.g. ``charge("4111-1111-1111-1111", 50)``) now
  mask positional args the same way kwargs already do, by introspecting
  the function signature with ``inspect.signature(fn)`` and applying
  ``SENSITIVE_ARG_KEYS`` to the matching parameter name. Pre-fix the
  PAN at position 0 was forwarded as-is into ``/execute`` and landed
  in the audit log.

- **P0-3 (OOM): streaming response memory cap.** Sync and async
  httpx transports now use bounded chunked reads capped at
  ``MAX_RESPONSE_BYTES`` (16 MiB by default; ``NULLRUN_MAX_RESPONSE_BYTES``
  env var to override). When the cap is exceeded, tracking is skipped
  and ``_coverage_streaming_skipped`` is incremented so the dashboard
  sees which hosts are producing oversized responses. Pre-fix
  ``response.read()`` / ``await response.aread()`` buffered the entire
  response body in memory — a 16+ MB allocation per streaming LLM
  call under load.

- **P0-4 (cost-audit): drop-newest on buffer overflow.** The CB-OPEN
  re-queue path in ``Transport._do_flush_locked`` now drops the
  NEWEST non-critical events instead of the oldest. The oldest
  events (start-of-incident, start-of-billing-period) are exactly
  what a billing investigator needs to reconstruct — losing them
  silently broke monthly rollups. Control-plane events
  (``state_change`` / ``kill_received`` / ``policy_invalidated`` /
  ``key_rotated``) are preserved regardless of position so the
  dashboard's KILL switch continues to land even under sustained
  backend outage.

- **P0-6 + P3-3 (security): redact-before-truncate.** ``_safe_repr``
  now runs ``_strip_details_balanced`` on the FULL repr before
  truncating to ``max_len=50``. Pre-fix the truncate ran first, and
  if ``details={...}`` lived past position 50 in the original repr
  (common for httpx.HTTPError with a long URL), the redact pass
  saw nothing on the truncated slice and the raw payload leaked
  into ``span_end`` audit events.

- **S-8 / P2-4: ``agent_id`` is now a real UUID with dashes.**
  ``agent()`` context manager emits ``str(uuid.uuid4())`` (e.g.
  ``95ca7c0b-8334-478a-af23-2788803ef3b8``) for auto-generated ids.
  Pre-fix the format was ``f"agent-{uuid.uuid4().hex}"`` — 32 hex
  chars with no dashes; backend UUID-typed columns silently
  dropped these to NULL on insert. User-supplied names are still
  preserved verbatim.

- **S-9: LRU cap on ``NullRunCallback._active_runs``** (4096 entries,
  FIFO eviction with WARN log). Pre-fix this dict grew unbounded
  when ``on_chain_end`` did not fire (errors in the chain body
  short-circuited the end hook for some LangChain versions),
  leaking memory in long-running services.

- **S-10: WebSocket reconnect max-attempts cap** (10 consecutive
  failures). Pre-fix the loop was unbounded (``while not
  self._closed:``) and leaked the WS thread forever when the backend
  was permanently down. After the cap the SDK falls back to
  HTTP-poll for control-plane state delivery.

- **P2-1: ``_coverage_seen`` now bumps in the httpx path.**
  Pre-fix the counter was only incremented in the ``requests``
  path (``auto_requests.py:185``), so the dashboard's coverage
  view was empty for the dominant httpx traffic (every OpenAI /
  Anthropic / Gemini / Mistral / Cohere call). Now both sync and
  async httpx ``_emit`` bump the counter.

- **P3-2: webhook delivery uses exponential backoff** (cap 30s).
  Pre-fix the schedule was linear (``0.5 * (attempt + 1)``); under
  sustained outage this produced a tight retry storm on the dead
  endpoint — each KILL/PAUSE spawned its own delivery thread.
  Post-fix the schedule is ``0.5 * 2**attempt`` capped at 30s:
  0.5s, 1.0s, 2.0s, 4.0s, 8.0s, 16.0s, 30.0s.

### Tests

Added regression tests for every item above (57 new tests across 9
new test files: ``test_agent_id_uuid.py``, ``test_args_pii_masked.py``,
``test_streaming_oom_cap.py``, ``test_lru_active_runs.py``,
``test_reconnect_cap.py``, ``test_coverage_seen_httpx.py``,
``test_webhook_backoff.py``, ``test_redact.py``; existing
``test_buffer_invariants.py`` extended with drop-newest + critical-event
preservation cases).

### Legacy

- **SDK silent runtime fallback removed** (FIX-4): `_get_or_create_runtime`
  in `nullrun.decorators` no longer wraps `NullRunRuntime.get_instance()`
  in a `try/except Exception` that rebuilds a no-arg `NullRunRuntime()`.
  In 0.3.0 (T3-S2) the no-arg constructor requires `api_key` and raises
  `NullRunAuthenticationError` — so the fallback swallowed the auth
  error from `get_instance()` only to crash with the same error from
  the fallback path itself. After this fix, the auth error propagates
  cleanly to the first `@protect` invocation, mirroring the fail-loud
  contract of `nullrun.init()`. Aligns with the T3-S2 invariant that
  the SDK has no local mode: a missing API key is a hard error, not a
  silent allow-all.

### Notes

- Public surface unchanged. `init`, `protect`, `track_llm`,
  `track_tool`, `track_event` retain the same call signatures
  documented in the existing examples. The platform's
  `docs/sdk/README.md` describes an alternative 7-symbol surface
  (with `wrap` alias and a different `init(organization_id, ...)`
  signature) — that doc is out of sync with the SDK; an update
  to the platform docs is tracked separately. Per the production
  plan's user decisions, the SDK's surface is the source of truth.

---

## [0.4.0] — 2026-06-17

Production-readiness release. Resolves all BLOCKER + HIGH + MEDIUM + LOW
audit findings from the 0.3.x audit. The curated 6-symbol public surface
(`init`, `protect`, `track_llm`, `track_tool`, `track_event`,
`__version__`) is unchanged. Full PR-by-PR description follows; this
entry is the summary. Phase-7 (framework patches) and Phase-8
(release-prep polish) ship as follow-up releases under the same 0.4.x
line.

### Removed (dead code)

- `BoundedDict` class (`runtime.py`) — dead since 0.3.1.
- `wrap_tool`, `wrap`, `check_before_tool`, `enforce_check_before_llm`,
  `check_before_llm` (and the `CheckDecision` dataclass), `evaluate`
  (`runtime.py`) — zero in-tree callers; `wrap` had a latent
  `NameError` that's gone with the deletion.
- `clear_pause` (`actions.py`) — zero callers.
- `WorkflowContext` class (`context.py`) — duplicate of the
  `workflow()` contextmanager.
- `WebSocketManager` (`transport_websocket.py`) — never instantiated;
  the runtime uses `WebSocketConnection` directly.
- `PoolConfig` + `AdaptivePool` (`transport.py`) — never instantiated;
  `httpx.Limits` is the real pool.
- `Transport._atexit_flush` (`transport.py`) — orphan method from the
  pre-weakref.finalize migration.
- `EventRecorder` (`decision_history.py`) — never used.

### Fixed (BLOCKER)

- **First-`track()` `AttributeError` (Phase 2).** `runtime.track()` no
  longer reads `self._workflow_costs` (a BoundedDict removed in 0.3.1
  whose two callers survived). Returns `local_cost_cents = 0` from
  the new `_local_cost_cents_estimate` attribute.
- **`auto_requests` module was unimportable.** The missing
  `_safe_bump_coverage` helper that `auto_requests.py` imports is
  now defined in `auto.py`. The whole module imports cleanly and the
  coverage dashboard counter is reachable.
- **`auto_instrument()` now calls `patch_requests`.** The `requests`
  library path is no longer dead; ~30-50% of real codebases that use
  `requests` directly are now tracked.

### Fixed (HIGH reliability — Phase 5)

- `_remote_states` now protected by `threading.RLock`. New helpers
  `_remote_state_for` / `_set_remote_state` are the only public mutation
  path. `test_remote_states_race.py` is now meaningful.
- `PolicyCache` no longer writes `policy_version` into the `ttl_seconds`
  field (silent cache-lifetime corruption). Added dedicated
  `policy_version` field on `CachedDecision`.
- `get_instance()` re-auth path is now inside the singleton lock; no
  more TOCTOU window where a concurrent caller can observe a
  half-shutdown runtime.
- `_fetch_remote_state` uses `self._transport._client` (shared pool
  + circuit breaker) instead of a raw `httpx.get`.
- `workflow()` emits a real UUID4 instead of `wf-{hex32}`.
- `@sensitive` propagates `NullRunAuthenticationError` instead of
  silently swallowing it.
- Custom-host LLM endpoints now honour the dashboard KILL switch
  (the kill check is no longer gated on the extractor table).
- `Transport.execute` accepts an `on_transport_error` callback
  (per ADR-008) so sensitive-tool pre-checks can fail-CLOSED on
  classified transport errors.

### Changed (MEDIUM hygiene — Phase 6)

- `NULLRUN_FALLBACK_MODE` env var (or `fallback_mode` constructor arg)
  selects PERMISSIVE / STRICT / CACHED.
- `_rebuild` strips `Transfer-Encoding` alongside `Content-Encoding`.
- `shutdown()` caps join waits at 0.5s (was 2.0s) — safe from
  signal handlers.
- WS URL constructed via `urllib.parse` (rejects unknown schemes).
- `DEDUP_LRU_MAX` raised 512 -> 4096.

### Added (Phase 7 — framework patches)

- `nullrun.instrumentation.llama_index` — `patch_llama_index`
  subscribes to `LLMChatEndEvent` and `FunctionCallEvent` on the
  llama-index core Dispatcher. Optional extra `pip install
  nullrun[llama-index]`.
- `nullrun.instrumentation.crewai` — `patch_crewai` wraps
  `Crew.kickoff` and `Crew.kickoff_async` to install
  `step_callback` / `task_callback`. Post-run reads
  `crew.usage_metrics` and emits one `llm_call` event per model.
  Optional extra `pip install nullrun[crewai]`.
- `nullrun.instrumentation.autogen` — `patch_autogen` wraps
  `BaseChatAgent.on_messages` for span tracking and
  `OpenAIChatCompletionClient.create` for streaming-safe usage
  capture. Optional extra `pip install nullrun[autogen]`.

### Added (Phase 8 — release polish)

- `NullRunRuntime.get_org_status(org_id)` — public helper for
  reading `/api/v1/orgs/{org_id}/status`. Routes through the shared
  transport client. Used by `examples/cost_dashboard.py`.
- `NULLRUN_BATCH_SIZE` and `NULLRUN_FLUSH_INTERVAL_MS` env vars
  override `FlushConfig` without subclassing.
- README "mTLS / client certificate authentication" section
  documenting `NULLRUN_TLS_CLIENT_CERT`, `NULLRUN_TLS_CLIENT_KEY`,
  `NULLRUN_TLS_CA_CERT`.
- Circuit-breaker `OPEN -> HALF_OPEN` jitter sleep capped at 5s
  (was 30s).
- `RecordingSession` no longer persists the dedup `_fingerprint`
  field — it leaks to disk via `save()` otherwise.

### Notes

- The platform's `docs/sdk/README.md` describes a 7-symbol surface that
  does not match the shipped SDK. The SDK's curated surface is the
  source of truth; platform docs re-alignment is tracked separately.

---

## [0.3.0] — 2026-06-15

### Breaking

- **No-api-key init now raises** (T3-S2): `nullrun.init()` and
  `NullRunRuntime(...)` without an `api_key` (and with `NULLRUN_API_KEY`
  unset) now raise `NullRunAuthenticationError` instead of falling back
  to a `NullRunNoop` stub. The previous silent fallback silently
  bypassed every backend gate (budget, policy, control plane) — a real
  safety hole in production. **Action required:** ensure
  `api_key="nr_live_..."` is passed to `init()` (or `NULLRUN_API_KEY`
  is set) in every entry point. The `0.2.0` deprecation warning has
  been removed; the new behavior is hard.
- **`local_mode` field removed**: The auto-derived `local_mode` flag
  on `NullRunRuntime` is gone. The `is_local_mode` property and the
  `NullRunNoop` / `NullRunNoopBreaker` / `_NullContext` classes are
  deleted (`nullrun.noop` module removed). All call sites that read
  `runtime.local_mode` will see `AttributeError` — there is no
  migration path because the field no longer has meaning. Code paths
  that previously branched on `local_mode` now always go through the
  cloud runtime (auth + policy fetch + control plane).

### Removed

- **Legacy Breaker exports** (T9): The 7 legacy re-exports
  (`nullrun.BreakerError`, `nullrun.CostLimitExceeded`,
  `nullrun.ApprovalRequired`, `nullrun.BreakerTimeout`,
  `nullrun.Policy`, `nullrun.FallbackMode`, `nullrun.PoolConfig`)
  are no longer reachable as `from nullrun import X`. The canonical
  exception names (`NullRunBlockedException`, `WorkflowPausedException`,
  `WorkflowKilledException`, `NullRunAuthenticationError`, …) and the
  canonical policy/transport modules
  (`from nullrun.runtime import Policy`,
  `from nullrun.transport import FallbackMode, PoolConfig`) remain
  available. Audited for 0 external callers.

### Migration

- **0.2.x → 0.3.0**:
  - `nullrun.init()` calls without `api_key` will raise. Pass
    `api_key="nr_live_..."` explicitly or set `NULLRUN_API_KEY`.
  - `NullRunRuntime(...)` constructions without `api_key` will raise
    (same fix).
  - Tests using `NullRunNoop` / `local_mode=True` mocking must switch
    to `NullRunRuntime(api_key="test-key", _test_mode=True)` —
    `_test_mode` skips the network calls without silently bypassing
    policy.
  - `from nullrun import BreakerError` (and the 6 other legacy names)
    must use the canonical paths above.

### Added

- **Async Policy Cache**: `AsyncTransport` now uses `PolicyCache` for CACHED fallback mode. Previously the async transport always fell back to PERMISSIVE when gateway was unreachable. Now it caches successful execute decisions and uses them when gateway is unavailable.
- **Custom Sensitive Tools API**: Added `add_sensitive_tool()`, `remove_sensitive_tool()`, `register_sensitive_tools()`, and `get_sensitive_tools()` methods to `NullRunRuntime`. Users can now register custom tools as sensitive requiring strict mode enforcement.

### Deprecated

- **No-api-key init / local mode** (T3-S1): Calling `nullrun.init()` or constructing `NullRunRuntime(...)` without an `api_key` (and with `NULLRUN_API_KEY` unset) now emits a `DeprecationWarning`. The runtime still falls back to local mode and silently bypasses every backend gate (budget, policy, control plane). The fallback will be **removed in 0.3.0** — passing `api_key='nr_live_...'` explicitly or setting `NULLRUN_API_KEY` is the only supported path going forward. Pin the warning to a hard error with `python -W error::DeprecationWarning` to catch callers in CI.

---

## [0.1.1] — 2026-05-20

### Fixed

- **CR-2**: Fixed buffer overflow when circuit breaker is OPEN. Previously, re-queued events were prepended to buffer, causing newest events to be dropped first. Now appends to buffer end and checks max_buffer_size before re-queue.
- **CR-5**: Async circuit breaker now uses `asyncio.Lock` instead of `threading.Lock` for proper async context handling.
- **CR-1+CR-4**: `runtime.py` now creates Transport before `_authenticate()` and `_fetch_policy()`, reusing the HTTP client for connection pooling and consistent timeout/retry policies.
- **AsyncAwait**: Fixed `_call_async()` not awaiting `_on_success_async()` and `_on_failure_async()` coroutines, causing "coroutine was never awaited" warnings in async transport.

### Changed

- Transport buffer now enforces max_buffer_size **before** re-queuing events on circuit breaker OPEN

---

## [0.1.0] — 2026-05-18

### Added

- Circuit breaker core (`src/nullrun/breaker/`) with STRICT / PERMISSIVE / CACHED fallback modes
- HTTP transport with batch event sending (`transport.py`)
- Async transport for asyncio applications
- Retry logic with jitter and policy-aware backoff
- `@protect` decorator for wrapping functions (`decorators.py`)
- Workflow context support (`context.py`)
- Main runtime entrypoint (`runtime.py`)
- `X-API-Version` header on all outgoing requests

### Notes

- Requires Python ≥ 3.10
- Compatible with NullRun API version `2024-01-15`

---

## How to upgrade

### 0.x → next

_No breaking changes yet. Watch this file._

---

[0.5.2]: https://github.com/maltsev-dev/nullrun-sdk/compare/v0.4.0...v0.5.2
[0.1.1]: https://github.com/maltsev-dev/nullrun-sdk/releases/tag/v0.1.1
[0.1.0]: https://github.com/maltsev-dev/nullrun-sdk/releases/tag/v0.1.0
