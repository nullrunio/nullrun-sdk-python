## [0.14.10] - 2026-08-11

Sprint 5 internal cleanup — no behavioural change, no SDK_MIN_VERSION bump, no wire-format change. Three release-blocks of dead code, dedup, and developer-experience hygiene. Backward-compatible patch.

### Removed

- **Dead code in `src/nullrun/`** — `extractor._cached_signature` + `compute_impact_digest` + unused imports; duplicate `compute_hmac_signature` / `verify_hmac_signature` in `transport_websocket` (re-exported from `transport`); `_singleton.install_module_proxy`; `_registry.replace_for_test`; `context.set_trace_id` / `reset_trace_id` / `clear_trace_id`; `runtime._start_transport` / `_trigger_action` / `get_org_status` / `_workflow_start_time`. 383 lines deleted across 6 files.
- **`Makefile run-example` target** — referenced `examples/basic.py` deleted in 0.3.1 with the gRPC transport. Local smoke testing now goes through `make smoke-test`.
- **CHANGELOG WIP `[0.10.0]` stub** + 13 `_(Trimmed; see git log X.Y.Z)_` placeholders. Net -29 lines.

### Changed

- **Sync/async transport dedup** — `NullRunSyncTransport` and `NullRunAsyncTransport` now share `_rebuild_response` (byte-identical rebuild path) and `_build_llm_call_event` (shared event-dict so the dedup fingerprint stays identical across sync + async httpx paths). 177 tests pass unchanged.
- **`@protect` sync/async wrapper dedup** — both paths now share a `_protect_body` context manager for the four pre-execution gates. Sync path keeps `unify_block=True` (kill/pause → `NullRunBlockedException`); async path keeps `unify_block=False` (propagates `WorkflowKilledInterrupt` so `asyncio` cancellation works). 114 tests pass.
- **LangChain usage extraction dedup** — `extract_usage_from_response` collapsed from 5 sequential `if` branches into a single `_read_token_attrs` + `_apply_usage` helper loop. 42 tests pass.
- **Decorator chain-walk dedup** — `_stamp_extractor_on_innermost` + `_find_extractor_in_chain` consolidated behind a `_walk_wrapped_chain` generator with a 32-hop cycle guard.
- **`Makefile coverage` target** — was `coverage run -m pytest tests/` (only traced xdist coordinator → 0-hit uploads); now `pytest tests/ --cov=src/nullrun --cov-branch --cov-report=xml:coverage.xml`, matching `.github/workflows/ci.yml:82`.

### Added

- **9 missing error-code docs** in `docs/errors/`: `NR-A004` (approval flow anomaly), `NR-B003` (sensitive-tool impact extractor failure), `NR-C000` (generic config default), `NR-C004` (status before init), `NR-CH001` (chain context invalid), `NR-O001` (overbudget on consume), `NR-P001` (wire-protocol version mismatch), `NR-R002` (aggregate-rate-limiter Redis outage), `NR-W004` (workflow soft-deleted). Three new catalogue categories: **P**rotocol, **Ch**ain, **O**verbudget.

### Fixed

- **CHANGELOG sort order** — release blocks now strictly descending by version (was `0.9.1 → 0.11.0 → 0.9.0`; now `0.11.0 → 0.9.1 → 0.9.0`. Lower section was `0.3.1 → 0.5.2 → 0.4.0`; now `0.5.2 → 0.4.0 → 0.3.1`).

_Tests: 1334 pass, 2 skip (pre-existing); 23/23 exception hierarchy pass._

_Compatibility:_ **No SDK_MIN_VERSION bump.** Strictly internal cleanup; no public API change, no wire-format change, no behavioural change. Drop-in replacement for 0.14.9.

## [0.14.9] - 2026-08-07

v3.38 wire-drift close — three real contract bugs that diverged from backend source code. Verified against `backend/src/proxy/http/protocol.rs`, `backend/src/proxy/middleware/auth.rs`, and CLAUDE.md §5 / §13 — not against comments or documentation. No SDK_MIN_VERSION bump. No on-wire change (backend already shipped the matching wire shape; this SDK release closes the consumer side).

### Fixed

- **Capabilities probe route** — `nullrun.capabilities.CAPABILITIES_PATH` was `"/health"` (a generic liveness endpoint) instead of the canonical `"/api/v1/capabilities"`. [...]
- **API_KEY_* error code granularity (v3.38 backend split)** — backend v3.38 split the `API_KEY_REVOKED` bucket into five distinct wire codes: `API_KEY_EXPIRED` / `API_KEY_DISABLED [...]
- **`NullRunAuthError.wire_code`** — the exception class gains a `wire_code: str | None = None` constructor kwarg that defaults to `"API_KEY_REVOKED"` for backwards compat. [...]

### Added

- **`decision == "soft_pass"` handler in `check_workflow_budget`** — the runtime's `/gate` decision dispatcher gains a `soft_pass` branch (currently the only branch missing from th [...]
  - calls `metrics.inc_runtime("soft_overdraft_used")` so the dashboard can graph soft-cap pressure
  - logs at WARNING with `overdraft_used_cents` / `max_overdraft_cents` / `remaining_overdraft_cents` from the backend response so operators can see which chains are burning overdraft
  - returns normally (the `allow` semantic is correct — the gate already authorised the call via the chain's overdraft cap)

_Tests: 4 additions (tests/conftest.py, tests/test_capabilities.py, tests/test_init_contract.py…)._

_Compatibility:_ **No SDK_MIN_VERSION bump.** All three fixes are consumer-side; the backend already shipped the matching wire shape.

## [0.14.8] - 2026-08-06

Execution Graph v0 — additive sub-agent lineage. The backend landed `parent_execution_id` as an optional wire field on `/api/v1/gate` (backend commit `87fae759`, not pushed yet) so an SDK spawning a sub-agent can name the parent's `execution_id`. Backend validates ownership against the parent's `execution:{id}` Redis binding (mirrors the `/cancel` ownership check) and rejects cross-org / cross-key / not-found with `403 PARENT_EXECUTION_*`. This release ships the SDK-side forward path, the matching capability flag, and the three-way error-code mapping. Wire change is strictly additive (omitted when `None`); no SDK_MIN_VERSION bump.

### Added

- **`parent_execution_id` on `/check` (gate)** — `Transport.check(check_request=...)` forwards the optional `parent_execution_id` field from `check_request` onto the wire when the  [...]
- **`execution_graph` capability flag** — `parse_capabilities` reads the new `execution_graph: bool` from `/api/v1/capabilities` (nested under `capabilities:` with top-level fallba [...]
- **`NullRunChainError.parent_execution_id`** — the chain error class gains an optional `parent_execution_id: str | None = None` constructor kwarg (mirroring the existing `chain_id [...]

### Changed

- **Three new error codes mapped to `NullRunChainError`** — `PARENT_EXECUTION_NOT_FOUND`, `PARENT_EXECUTION_ORG_MISMATCH`, `PARENT_EXECUTION_KEY_MISMATCH` (all 403) are added to `_ [...]

_Tests: 1 additions (tests/test_transport.py)._

_Compatibility:_ **Backward-compatible additive wire change.** Pre-Execution-Graph SDKs that never set `parent_execution_id` continue to work unchanged — the field is omitted entirely from the wire.

## [0.14.7] - 2026-08-04

Init contract hardening — strip leading and trailing whitespace from `api_key` (and the `NULLRUN_API_KEY` env fallback) BEFORE the truthiness check in `nullrun.init()` and `NullRunRuntime.__init__`. Pre-fix, whitespace-only strings (`"   "`, `"\t"`, `"\n"`) are TRUTHY in Python and silently slipped past the empty-key guard; they were stored on the runtime and reached the gateway as a malformed `Authorization: Bearer   ***` header, surfacing as a backend 401 only on the first `/gate` call rather than at startup.

### Fixed

- **`nullrun.init()` now strips whitespace before the truthiness check** — `src/nullrun/__init__.py:249` resolves `raw_key = api_key if api_key is not None else os.getenv("NULLRUN_ [...]
- **`NullRunRuntime.__init__` mirrors the strip-then-check** — `src/nullrun/runtime.py:370` applies the same contract so direct construction (used by tests and advanced callers) ca [...]

_Tests: 1 additions (tests/test_init_contract.py)._

_Compatibility:_ **Backward-compatible bug fix.** The strip is a strict superset of the empty check: pre-fix callers that passed valid keys continue to work unchanged (`"nr_live_xxx"` strips to itself), and callers that pasted whitespace-only keys now  [...]

## [0.14.5] - 2026-08-01

MCP-aware gate metadata and tool-argument forwarding. The release completes the SDK-side path for MCP classification and annotation policies, and adds the optional argument bag used by the backend's tool-schema fingerprinting flow. All new wire fields are optional and omitted when unavailable.

### Added

- **Per-call MCP context** — `set_mcp_tool_context(...)`, `get_call_mcp_class()`, and `get_call_mcp_annotations()` store and expose the canonical tool class plus normalised MCP ann [...]
- **`MCPAdapter`** — `nullrun.toolbox.mcp.MCPAdapter` wraps an already-connected synchronous MCP client. [...]
- **`tool_arguments` on `/execute` and `/gate`** — `Transport.execute(...)` accepts an optional argument mapping, while `Transport.check(...)` forwards the same field from `check_r [...]

### Fixed

- **MCP context tests no longer leak module-level `ContextVar` state** — the release includes isolation fixes for the class and annotation tests that were flaky only during the ful [...]

_Tests: 3 additions (tests/test_mcp_adapter.py, tests/test_mcp_context.py, tests/test_transport.py)._

_Compatibility:_ **Backward-compatible additive wire change.** Existing callers do not need to pass any new fields; absent MCP metadata and `tool_arguments=None` are omitted.

## [0.14.4] - 2026-07-27

ToolParameters Approval Rules wire contract (Tier 2 / Разрыв 2 follow-up). The backend already accepted `BusinessImpact::ToolCall(ToolCallParams)` on the `/execute` wire (backend commit `1e501cd6`); 0.14.4 lands the SDK-side path so users get ToolParameters rules by default on every bare `@sensitive` function, with no decorator change. Also fixes a silent regression in the auto-attach path that dropped an explicit `impact=tool_params({...})` map, and pins the cross-language `ToolCall` action digest against the Rust backend's golden hex. No on-wire breaking change for money callers; the only behavioural change is that bare `@sensitive` now ships `kind=tool_call` on the wire where it previously shipped nothing.

### Added

- **`BusinessImpact.tool_call(tool_name, params)`** factory — `business_impact.py:323` new factory builds a `BusinessImpact(kind='tool_call', tool_name=..., params=...)` envelope b [...]
- **`ToolCallParams` dataclass** — `business_impact.py:143` mirrors the backend struct (`tool_name` ≤ 128 bytes, `param_name` ≤ 64, JSON-roundtrippable values only). [...]
- **`ToolParamsExtractor` + `tool_params(...)` factory** — `extractor.py:815` (class) and the matching factory. [...]
- **Bare `@sensitive` now ships ToolParameters on the wire** — `decorators.py:1096` (`_do_sensitive_register`) auto-attaches a default `ToolParamsExtractor(include_all=True)` on a  [...]
- **`@sensitive(impact=tool_params({...}))` decorator form** — `decorators.py:1065` new docstring + `decorators.py:711` dispatch branch. [...]

### Fixed

- **Auto-attach chain walk preserves an explicit `impact=tool_params({...})` map** — `decorators.py:43` new helper `_find_extractor_in_chain` walks `__wrapped__` (bounded at 32 hop [...]
- **`_enforce_sensitive_tool` dispatch handles both extractor types** — `decorators.py:677` (success path) and `decorators.py:711` (error path) now branch by extractor type. [...]
- **Bare `@sensitive` regression in the existing `tests/test_sensitive_extractor.py`** — the 5 existing tests still pass because they register the tool manually via `rt.add_sensiti [...]

_Tests: 7 additions (tests/test_business_impact.py, tests/test_extractors.py, tests/test_protect.py…)._

_Compatibility:_ **Default SDK behaviour for bare `@sensitive` CHANGED** — was `no business_impact on wire`, now `kind=tool_call on wire`. Operators who relied on the Phase 0 path (approval_id-only grant consume) must either pass `@sensitive(impact=too [...]

## [0.14.2] - 2026-07-24

Three hotfixes that fell out of the 0.14.1 demo run. Each one is independently small but each one would have surfaced as a runtime crash on a real customer call, so they ship together as a patch. No on-wire breaking change. No SDK_MIN_VERSION bump. Backends on `1.0.0` keep working unchanged.

### Fixed

- **`@protect` decorator now emits a `tools/track_tool` event** — `decorators.py:470` and `decorators.py:521` (sync + async wrappers) now call `runtime.track_tool(fn.__name__, meta [...]
- **`track_tool` event carries `tokens: 0` and a fresh `uuidv7` `execution_id`** — `runtime.py:3077` now stamps both fields onto every `tool_call` event. [...]
- **Approval-resolved WS callback is now a plain sync function** — `transport.py:1757` `wrapped_approval_resolved` was previously declared `async def` to be awaitable, but the WebS [...]
- **WebSocket cancellation is treated as a clean shutdown** — `runtime.py:1160` now catches `asyncio.CancelledError` before the generic `except Exception` block. [...]

_Tests: 4 additions (tests/test_approval_money_flow.py, tests/test_approval_ws_sync_callback.py, tests/test_runtime_branches.py…)._

_Compatibility:_ **Backward-compatible bug fix.** No SDK_MIN_VERSION bump. No public API change.

## [0.14.1] - 2026-07-24

Decimal JSON serialization patch. `track_tool` event payloads that contain a `Decimal` value (e.g. `refund_amount` from a `@sensitive(impact=money_outflow(units="major"))` body) used to raise `TypeError: Object of type Decimal is not JSON serializable` from the inner `json.dumps` call. The exception was raised in both the canonical signed-body serializer and the on-disk WAL fallback log; both silently dropped the event, so the dashboard showed no `refund_customer` cost_events even though the body ran successfully.

### Fixed

- **`_signed_request_body` Decimal serialization** — `transport.py:251` now passes `default=str` to `json.dumps(payload, separators=(",", ":"), default=str)`. [...]
- **WAL fallback `default=str`** — `transport.py:711` `_signed_request_body` WAL fallback (`f.write(json.dumps(event) + "\n")`) also gets `default=str` for consistency. [...]

_Tests: 2 additions (tests/test_approval_money_flow.py, tests/test_sensitive_extractor.py)._

_Compatibility:_ **Backward-compatible bug fix**. No SDK_MIN_VERSION bump. No public API change.

## [0.14.0] - 2026-07-23


### Added

- **`InvalidMoneyPrecisionError`** and **`InvalidMoneyAmountError`** — dedicated `ValueError` subclasses with structured fields. [...]
- **`BusinessImpact`** model (`dataclass(frozen=True)`) with explicit `currency` / `units` / `amount_minor` fields. `details` dict is still accepted on the legacy path.
- **`@sensitive(impact=BusinessImpact(...))`** — new decorator kwarg that emits a structured `business_impact` envelope on the `/track` event. [...]
- **`MoneyImpactExtractor`** — new helper that normalises `Decimal` / `int` / `float` / str into `BusinessImpact` minor-units, raising `InvalidMoneyAmountError` / `InvalidMoneyPrec [...]

### Changed

- **Negative `amount_minor` rejected** on both unit paths. A negative value would silently fall through every `op=gt` predicate (`negative < positive` is always False) — pre-fix a  [...]
- **Sub-precision Decimal rejected** — `Decimal("1.234")` against a USD `allowed=2` precision is now `InvalidMoneyPrecisionError(currency="USD", allowed=2, received=3, received_dig [...]
- **`/execute` handles `require_approval` correctly** — re-checks with the `approval_id` returned by the backend (was dropping the approval handshake on round-trips).
- **Server `approval_timeout` clamped to `[1, 3600]s`** on the SDK side as defence against a malformed / overshooting backend that returns `0` or `2147483647` in the Разрыв 1c fiel [...]

_Tests: 6 additions (tests/test_approval_money_flow.py, tests/test_business_impact.py, tests/test_execute_approval_flow.py…)._

_Compatibility:_ **Backward compatible** on the happy path. Every existing call site keeps working; the new errors are `ValueError` subclasses; the new `BusinessImpact` decorator kwarg is optional.

## [0.13.13] - 2026-07-21

Approval-wait SDK sync with backend commit `0ad03b9` ("\u0420\u0430\u0437\u0440\u044b\u0432 1c", gate hot-path trigger). The backend now sends `approval_timeout_seconds: Option<i64>` and `approval_expires_at: Option<String>` on every `/gate` response so a backend approval rule can set a non-default short timeout. Pre-fix, the SDK only consulted `NULLRUN_APPROVAL_TIMEOUT_SECONDS` (env default 300s), which silently desynced from a 20s backend expiry sweeper. No public API change. No SDK_MIN_VERSION bump. No on-wire change.

### Fixed

- **Approval wait uses server-authoritative `approval_timeout_seconds` when present** \u2014 new optional kwarg `timeout_seconds: float | None = None` on `_wait_for_approval_resolu [...]
- **`check_workflow_budget` reads `response["approval_timeout_seconds"]`** with type and sign validation. Malformed values fall through to the env default path. [...]
- **Diverging server vs env default emits a DEBUG log line** ("approval {id}: using server timeout={X}s (env default would have been {Y}s)") so an operator inspecting logs can see  [...]

_Tests: 1 additions (tests/test_approval_timeout_field.py)._

_Compatibility:_ The new `timeout_seconds` kwarg is optional with a `None` default, so existing callers are unaffected.

## [0.13.12] - 2026-07-20

CI / coverage-testability release. No on-wire change, no SDK_MIN_VERSION bump, no public API change. Backends on `1.0.0` keep working unchanged.

### Changed

- **`pytest` suite is now CI-fast on Windows + xdist** — a new `_fast_sleep` autouse fixture in `tests/conftest.py` caps test-code `time.sleep` calls at 1ms, with two opt-out paths [...]
- **`TestCircuitBreaker` half-open tests no longer sleep the wall clock** — `test_open_transitions_to_half_open_after_timeout`, `test_half_open_success_closes`, and `test_half_open [...]
- **`TestPingChainScheduler` opts out of the cap via marker** — the new `@pytest.mark.slow_sleep` marker on the class lets `test_ping_chain_emits_heartbeats_on_time_schedule` keep  [...]

_Tests: 3 additions (tests/conftest.py, tests/test_transport.py, tests/test_v3_wire_contract.py)._

### CI

- `pyproject.toml` — new `markers = ["slow_sleep: opt out of the conftest autouse time.sleep cap"]` entry under `[tool.pytest.ini_options]`. [...]
- The Codecov badge in `README.md` will now report the real combined coverage on master. Pre-Sprint-0 the badge was stuck at 0% because `coverage run -m pytest -n auto` ran coverage in the coordinator process only; the Sprint 0 PR (#70) already fixed [...]

### Audit

- No SDK public API change. No wire-format change. No backend migration required. [...]
- Pre-Sprint-0 instability under `pytest-cov + xdist`: `test_status.py::TestRecentErrors` and `TestTransport::test_stop_flush_false_skips_final_flush` were observed to flake ~1/3 of the runs in the local environment (passing in isolation, passing in  [...]


## [0.13.0] - 2026-07-04

Drift-fixes release. Closes the SDK-side items on `docs/drift.md` (2026-07-04); no on-wire breaking change — backends on `1.0.0` keep working unchanged.

### Added

- **Idempotency-key propagation to `/track` v3 single-event** — new `nullrun.context._server_minted_idempotency_key_var` + `get_/set_/reset_/clear_server_minted_idempotency_key` he [...]

### Changed

- `runtime.py` module docstring now distinguishes **SDK-side transport failure** (network / 5xx / breaker open → fail-OPEN on the `/check` path) from **wire 4xx/5xx that names an enforcement failure** (`BUDGET_REDIS_UNAVAILABLE` → 402 fail-CLOSED, `R [...]

### Fixed

- **Wire `status_code` preserved on every decision exception** — `NullRunBlockedException`, `NullRunBudgetError`, `NullRunChainError`, `NullRunWorkflowInactiveError`, `NullRunConsu [...]
- **Patch-coverage gap from 0.12.2 closed** — `tests/test_v3_wire_contract.py::TestGateCacheRuntimeFlow` (3 tests) drives `NullRunRuntime.check_workflow_budget` inside `with chain( [...]

_Tests: 2 additions (tests/test_drift_fixes_2026_07_04.py, tests/test_v3_wire_contract.py)._

### Audit

- New `docs/drift.md` records the six P0 + P1 items that turned up during pre-publish review of 0.12.2 (idempotency-key wiring, status_code on exceptions, fail-CLOSED honesty, plus four P0/P1 README issues that are deferred to a README rewrite PR and [...]


## [0.12.2] - 2026-07-04

Bug-fix release. Two related correctness fixes layered on top of 0.12.1; no wire-format change.

### Fixed

- **BUG #4 — `/check` execution_id**: `check_workflow_budget()` now sends a fresh `uuidv7` as the `execution_id` field on every call, instead of reusing `workflow_id`. [...]
- **BUG #5 — chain-mode gate thrash**: new `nullrun.runtime._GATE_CACHE` (5s TTL, keyed on `(workflow_id, chain_id, model)`) collapses consecutive `/gate` calls from inside `with c [...]

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
- `_route_track` routes `llm_call` events to the v3 `/api/v1/track` single-event endpoint via `Transport.track_single()` so backend `gate_consume_v3` validates the consume-vs-reserve + epsilon invariant (CLAUDE.md section 25). [...]
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


## [0.11.0] - 2026-07-02

Wire-protocol v3 alignment with the backend's Sprint 6 v1 cut
(CLAUDE.md v3.4). The previous SDK shipped pre-v3 endpoints
(`/api/v1/gate`, `/api/v1/execute`, `/api/v1/track/batch`) without
the `X-NULLRUN-PROTOCOL` header that the v3 backend requires as a
fail-CLOSED pre-check — every signed POST was rejected with HTTP 400
`PROTOCOL_HEADER_REQUIRED`. This release aligns the SDK with the v3
wire contract and adds the missing soft-mode / chain / heartbeat /
cancel / budget-estimate surface.

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
    `payload["id"]`).

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

_Tests: 3 additions (tests/test_coverage_report.py, tests/test_coverage_seen_httpx.py, tests/test_llm_call_metadata_flags.py)._

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

_Tests: 1 additions (tests/test_batch_response_parsing.py)._

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
## [0.7.8] - 2026-06-28

Additive patch on top of 0.7.7. Converts two silent fail-OPEN footguns
into explicit `DeprecationWarning` / `RuntimeError`. No behavior
change for callers who don't touch the deprecated surface.

### Deprecated

- `NullRunRuntime.start_recording()` and `NullRunRuntime.stop_recording()` now emit `DeprecationWarning`. They have been silent no-op stubs since Sprint 2.1 (0.4.0). [...]
- Setting `NULLRUN_USE_GRPC=1` now raises `RuntimeError` at SDK init instead of silently falling back to HTTP with an info log. gRPC transport remains on the roadmap but is not yet implemented. Unset the env var to use HTTP. See https://docs.nullrun.io/reference/sdk-api#transport

### Migration

- Replace `runtime.start_recording(workflow_id, metadata=...)` with a dashboard navigation or `nullrun.status()` introspection.
- Remove any `NULLRUN_USE_GRPC` env var from deployment configs (Docker compose, k8s manifests, systemd units).
- Catch `RuntimeError` at SDK init if you want to keep the env var as a feature flag — but the recommended path is to unset it.


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

## [0.6.0] — 2026-06-23

Hardening pass driven by the 2026-06-22 SDK↔backend integration audit.
Closes three classes of silent fail-OPEN regressions that the previous
release shipped: SDK POSTs being rejected by the backend's CSRF
middleware, WS HMAC identity field drift, and policy-fetch silently
falling through to a permissive default on any backend blip. Coverage
jumped from ~76% to **84.59%** (branch = true).

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

- **Policy fetch is now fail-CLOSED (F-R2-02).** Pre-fix, any HTTP
  exception, non-200 status, or empty `{"data": []}` response silently
## [0.5.2] — 2026-06-19

This release bundles the Sprint 2.5 production-readiness hardening
alongside the Phase 0 contract / lifecycle fixes. The two streams were
shipped as separate `[Unreleased]` sections during development; they
are merged here into a single canonical entry so release tooling that
scans for the `[Unreleased]` anchor picks up the complete change set
exactly once.

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
## [0.4.0] — 2026-06-17

Production-readiness release. Resolves all BLOCKER + HIGH + MEDIUM + LOW
audit findings from the 0.3.x audit. The curated 6-symbol public surface
(`init`, `protect`, `track_llm`, `track_tool`, `track_event`,
`__version__`) is unchanged. Full PR-by-PR description follows; this
entry is the summary. Phase-7 (framework patches) and Phase-8
(release-prep polish) ship as follow-up releases under the same 0.4.x
line.

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

- **First-`track()` `AttributeError` (Phase 2).** `runtime.track()` no
  longer reads `self._workflow_costs` (a BoundedDict removed in 0.3.1
  whose two callers survived). Returns `local_cost_cents = 0` from
  the new `_local_cost_cents_estimate` attribute.
- **`auto_requests` module was unimportable.** The missing
  `_safe_bump_coverage` helper that `auto_requests.py` imports is
  now defined in `auto.py`. The whole module imports cleanly and the
  coverage dashboard counter is reachable.
- **`auto_instrument()` now calls `patch_requests`.** The `requests`
## [0.3.1] — 2026-06-17

Production-readiness hardening. No public-API changes; the curated 6-symbol
surface is unchanged. Aligns the SDK with the contracts in
`NULLRUN/docs/adr/008-sdk-preflight-fail-policy.md` and
`NULLRUN/docs/kill-contract.md`.

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
## [0.1.1] — 2026-05-20

### Fixed

- **CR-2**: Fixed buffer overflow when circuit breaker is OPEN. Previously, re-queued events were prepended to buffer, causing newest events to be dropped first. [...]
- **CR-5**: Async circuit breaker now uses `asyncio.Lock` instead of `threading.Lock` for proper async context handling.
- **CR-1+CR-4**: `runtime.py` now creates Transport before `_authenticate()` and `_fetch_policy()`, reusing the HTTP client for connection pooling and consistent timeout/retry poli [...]
- **AsyncAwait**: Fixed `_call_async()` not awaiting `_on_success_async()` and `_on_failure_async()` coroutines, causing "coroutine was never awaited" warnings in async transport.

### Changed

- Transport buffer now enforces max_buffer_size **before** re-queuing events on circuit breaker OPEN


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

- Requires Python ≥ 3.10
- Compatible with NullRun API version `2024-01-15`

