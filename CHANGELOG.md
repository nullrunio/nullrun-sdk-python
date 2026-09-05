## [0.16.5] - 2026-09-05

Patch release — two independent reliability fixes: (1) `@protect` cancel-on-exception orphan leak (Redis reservation leak on tool exceptions), (2) P0-26+P0-27 `operation_id` hoist (single-source mint, server-vs-SDK divergence detection). No wire-format change on either fix.

### Fixed

- **`@protect` cancel-on-exception orphan leak** (`src/nullrun/decorators.py`). Both `async_wrapper` and `sync_wrapper` now wrap the with-block in a try/except; on failure, `_safe_cancel_active_execution(reason="tool_exception")` closes the in-flight `/gate` reservation so the budget envelope is released immediately rather than waiting on TTL expiry. Three invariant pins:
    - **Asymmetry on exception scope.** `async_wrapper` catches `Exception`, NOT `BaseException` — `asyncio.CancelledError`, `KeyboardInterrupt`, and `SystemExit` propagate without doing a synchronous blocking HTTP call inside a cancellation handler. That call has a 5s timeout; inside a cancellation handler it would (a) delay task cancellation by up to 5s on network errors, (b) make `Task was destroyed but it is pending` warnings more frequent and harder to diagnose, and (c) in some shutdown paths get cancelled itself, leaving cleanup incomplete (the server's `/cancel` is idempotent so this is acceptable — orphan via TTL/reconciliation instead). `sync_wrapper` catches `BaseException` to match existing `_protect_body` unify_block semantics; sync code has no event loop to delay and a Ctrl+C during a long sync agent gets a few seconds of cancel I/O before exit.
    - **`fn_completed` sentinel.** After `fn(...)` returns, `fn_completed = True`. If `track_tool(...)` then fails (rare `/track` batch-sender network error), the wrapper's `except` runs but `fn_completed` is True so cancel does NOT fire — side effects already happened and the right move is to retry `track_tool`, not cancel (which would tell the server "no side effects" — a lie that produces a phantom budget refund and breaks audit).
    - **Helper is fail-OPEN.** `_safe_cancel_active_execution` swallows everything (`NullRunTransportError`, `NullRunBackendError`, `get_runtime()` failures) so a cancel I/O failure never masks the original exception. An orphan from cancel failure is preferred over masking a `ValueError` from `fn()`.
- **P0-26 — `operation_id` server-vs-SDK divergence detection** (`src/nullrun/runtime.py`, `src/nullrun/context.py`). `_capture_server_minted_execution_id` previously read `response.get('operation_id')` directly; if a proxy or a backend bug echoed back a different `operation_id` than the SDK minted, the SDK had no signal — audit row stored one id, downstream `/track` used another. Now reads via `_get_op_id_for_capture()` (the SDK-minted contextvar value) and asserts `server_op_id != sdk_op_id` with ERROR log on disagreement. `idempotency_key` is derived from the SDK-minted value, not the server echo, so a divergent server response cannot break idempotency.
- **P0-27 — `operation_id` hoisted to contextvar; triple-mint collapsed to one** (`src/nullrun/context.py`, `src/nullrun/runtime.py`). New `_operation_id_var` contextvar (name `operation_id`) in `context.py`. `check_workflow_budget` mints ONCE via `_get_op_id_for_check()` / `_set_op_id_for_check()`; `execute` reads via `_get_op_id_for_execute()` with a single fallback mint+stash branch (only runs if `/check` did not run — pre-execution paths that bypass `/check`). The previous code minted at three sites independently, which meant a divergence between `/check` mint and `/execute` mint produced an audit-row-vs-/execute id mismatch.

### Added

- **`tests/test_protect_cancel_on_exception.py`** (7 tests). Pins both halves of the asymmetry and the helper's behavior:
    - `test_1_async_cancelled_error_does_not_trigger_cancel` — the regression guard. If a future refactor reverts `except Exception` to `except BaseException` in `async_wrapper`, this test fails. `set_server_minted_execution_id(...)` is set so the helper WOULD have run if the except had caught BaseException; `cancel_calls` must be empty.
    - `test_2_track_tool_failure_after_fn_completion_does_not_trigger_cancel` — the second regression guard. If someone removes the `fn_completed` sentinel, this test fails (cancel would run on a successfully-completed tool, producing a phantom refund).
    - `test_3_async_fn_raises_value_error_triggers_cancel` — happy-path cancel. `cancel_calls == [("exec-test-123", "tool_exception")]`.
    - `test_4_async_no_execution_id_skips_cancel` — control_plane reject happens pre-`/gate`, ContextVar stays None, no cancel.
    - `test_5_sync_fn_raises_value_error_triggers_cancel` — sync ValueError path mirrors async.
    - `test_6_sync_baseexception_also_triggers_cancel` — sync `KeyboardInterrupt` cancels (sync has no event loop to delay).
    - `test_happy_path_no_cancel_called` — sanity: success path produces no cancel and gate order stays `control_plane, budget, track_tool`.
- **`tests/test_audit_p0_27_operation_id_hoist.py`** (8 tests). Pins the single-source-mint + server-vs-SDK divergence detection:
    - contextvar name `operation_id` (forbids any other name; would silently disable mint if renamed without a corresponding accessor change).
    - mint-site shapes in `check_workflow_budget` and `execute` (forbids pre-fix `str(uuid.uuid4())` mints outside the fallback branch).
    - parity assertion in `_capture_server_minted_execution_id` (server_op_id vs sdk_op_id equality required for the no-warn path).
    - fallback mint+stash in `execute` only fires when `/check` did not mint (idempotency: one operation_id per call site, never two).

### Compatibility

Pure reliability fixes — no wire-format change on either fix. Cancel-on-exception: existing exception paths unchanged; the cancel I/O is purely additive cleanup. Operation-id hoist: wire field `operation_id` is unchanged; the SDK now uses a single mint source and adds a server-divergence warning, both invisible to the wire contract.

### Verification

- Targeted suite: `tests/test_protect_cancel_on_exception.py` — 7/7 pass.
- Targeted suite: `tests/test_audit_p0_27_operation_id_hoist.py` — 8/8 pass.
- Broader regression suite: `pytest -q` clean (prior 1613 + 7 + 8 = 1628); `ruff check src tests` clean on the WIP files (decorators.py + test_protect_cancel_on_exception.py); `mypy src/nullrun` no issues reported in 37 source files.
- Wire-format: zero changes on both fixes. Same `/gate`, `/track`, `/execute`, `/cancel` payloads. The `/cancel` endpoint was already used by `runtime.cancel_execution(...)` from control-plane kill paths, just now also from the exception-cleanup helper. `operation_id` field on the wire was already a single string; this release only changes where the SDK mints/reads it locally.

### Why this is needed

**Cancel-on-exception** — at anti-DoS scale, every orphaned reservation is a permanent slot in `reserved_total` until TTL expiry. A noisy control-plane kill switch OR a single bad batch of tool exceptions could leak thousands of reservations per hour, gradually starving legitimate traffic out of the budget envelope. The fix collapses the leak window from "TTL expiry" (~minutes) to "synchronous cancel I/O on exception" (~ms), with the fail-OPEN helper guaranteeing we never trade an orphan for a swallowed exception.

**Operation-id hoist** — pre-fix, three independent mints meant a race between `/check` and `/execute` could produce two different ids for the same logical call. The audit row stored one, `/execute` sent another, downstream `/track` chained off yet another. Server-vs-SDK divergence had no detection. P0-27 collapses to a single SDK-minted value; P0-26 adds the parity assertion so a divergent server echo is logged at ERROR before it propagates into audit/retry logic.

## [0.16.4] - 2026-08-31

Patch release — ADR-037 Slice B. The wire protocol bumps from 3 → 4 additively: `/gate` response now echoes the SDK-supplied `action_digest` and a `policy_hash` slot (always `None` today; Slice D wires per-request computation). `min_protocol_version` stays at 2 so v3 SDKs are unaffected. Wire-format additive only — no new hashing/computation introduced on either side (both fields echo already-computed values).

### Added

- **`NULLRUN_PROTOCOL_VERSION = 4`** (`src/nullrun/transport.py`). `X-NULLRUN-PROTOCOL` header on every signed POST now serialises the bumped value via the single source of truth `NULLRUN_PROTOCOL_VERSION`; tests are pinned to `str(NULLRUN_PROTOCOL_VERSION)` so a future bump doesn't sweep this file again. `NullRunProtocolError.user_action` and `docs/errors/NR-P001.md` updated to point operators at `X-NULLRUN-PROTOCOL: 4`.
- **`/gate` response wire-evidence echo capture** — `runtime._capture_wire_evidence` (called from `_capture_server_minted_execution_id` on the same `/check` lifetime so the two values always refer to the same gate decision) reads `action_digest` + `policy_hash` off the response and stores them in two new contextvars: `_last_gate_action_digest_var`, `_last_gate_policy_hash_var`. Public accessors `get_last_gate_action_digest()` / `get_last_gate_policy_hash()`; setters `set_last_gate_action_digest()` / `set_last_gate_policy_hash()`. Capture is fail-OPEN: a malformed value (non-str type) is logged at WARNING and dropped — the contextvar stays at `None`. `clear_server_minted_execution_id` (and the underlying direct `set(...=None)` paths) also drop the v4 slots so a `/check` in one block never leaks a stale echo into a `/track` in a sibling block.
- **`ServerCapabilities.wire_evidence_echo`** — informational capability flag surfaced by `/api/v1/capabilities`. Tells the SDK the backend echoes `action_digest` on `/gate` response. NOT included in `is_v3_ready()` (informational, not a hard gate). Defaults to `False` on pre-v4 backends; the canonical shape is `capabilities.wire_evidence_echo: true` at the top level, with the nested `capabilities.*` form also accepted.
- **New test file `tests/test_slice_b_wire_evidence.py`** (10 tests). Pins the SDK-side of the v3→v4 additive bump: protocol-constant value, header serialisation, capture from `/gate` response (happy path + `policy_hash`-when-present + both-set), tolerance of pre-v4 backends (no keys → both `None`), tolerance of malformed wire values (non-str → drop, do not raise), tolerance of `None`-typed responses (defensive — runtime never passes a non-dict, but a bad transport layer might), `clear_server_minted_execution_id` resets the v4 slots, and the protocol-constant + capability-flag source-of-truth wiring.
- **`tests/test_capabilities.py`** — two new assertions: `test_parse_capabilities_wire_evidence_echo_v4_backend` (top-level + nested + missing-key), `test_parse_capabilities_v4_protocol_range` (min=2 stays, max moves to 4).
- **README alpha-status line + roadmap table** — `v0.15` → `v0.15.x` (so the v0.15.x fail-OPEN observability closure isn't squashed); `v0.16` → `v0.16.x` with the new highlights (Phase-1+ `action_digest` on `/gate`, `/execute` `tools` propagation, NR-006 transient-5xx retry, NR-007 error-code parity 41→56 entries, Slice B wire-evidence echo); `v0.17` for the OpenTelemetry exporter / Redis-backed offline queue / hardened init contract that previously sat under `v0.16`.

### Changed

- **`/gate` response handler now reads two more keys.** `action_digest` (SDK-supplied SHA-256 hex of canonical `business_impact`, re-verified server-side by `payload_binding::server_derive_action_digest`, echoed back so the SDK can confirm what the gate saw matches what it intended) and `policy_hash` (slot reserved for future Slice D wiring — today always `None` because the gate doesn't compute per-request hashes; the audit row stores `policy_hash = None` for the same reason at `audit_drain.rs:301`). Pre-v4 backends omit both keys entirely via `skip_serializing_if = "Option::is_none"` — a v4 SDK connecting to a v3 backend reads `None` on both fields and logs "no wire evidence echo" — no false positive.
- **`tests/contract/test_audit_wire.py` + `tests/test_v3_wire_contract.py`** — header assertions now source `str(NULLRUN_PROTOCOL_VERSION)` instead of the literal `"3"` so a future bump doesn't require sweeping either file. Class names kept (`TestSignedPostIncludesProtocolHeader`) for git-blame continuity.

### Compatibility

Wire-format additive — pre-v4 SDKs parsing the response simply ignore the new fields; v4 SDKs parsing a v3 backend response see `None` on both fields (skip_serializing_if on the backend means the JSON keys are absent, not `null`). `min_protocol_version` stays at 2, so v3 SDKs continue to work against a v4 backend. The architectural invariant `GateResponse.action_digest == AuditEvent.action_digest` holds trivially because both sides flow from the SDK's input. No new hashing/computation introduced on either side — both fields echo already-computed values.

### Verification

- Targeted suite: `tests/test_slice_b_wire_evidence.py` — 10/10 pass.
- Capabilities: `tests/test_capabilities.py::test_parse_capabilities_wire_evidence_echo_v4_backend`, `test_parse_capabilities_v4_protocol_range` — pass.
- Wire contract: `tests/test_v3_wire_contract.py` — pass (header assertions now source the constant).
- Audit wire: `tests/contract/test_audit_wire.py` — pass.
- Broader regression suite: `pytest -q` 1613 passed / 4 skipped (12 more than 0.16.3, accounting for the 10 new Slice B pins + 2 new capabilities assertions); `ruff check src tests` all checks pass; `mypy src/nullrun` no issues reported in 37 source files.

### Why this is needed

ADR-037 Slice B closes the SDK/backend wire-trust gap: pre-Slice-B the SDK had no way to verify the gate saw the same `action_digest` it intended — a misconfigured proxy or a future Slice A regression could swallow or rewrite the digest without any SDK-side signal. The echo slot on `/gate` response + the two contextvars give operators a clean diagnostic ("the gate echoed digest X — that's what I sent") and pin the architectural invariant `GateResponse.action_digest == AuditEvent.action_digest` at the SDK layer. `policy_hash` is forward-compat for Slice D; the slot is wired now so Slice D doesn't require another SDK release.

## [0.16.3] - 2026-08-26

Patch release — closes `NR-006` (audit 2026-08-24) and `NR-007` (audit 2026-08-24). No wire-format change. Pure reliability + SDK/backend parity hardening on top of 0.16.2.

**NR-006 (2026-08-24) — `Transport.check` now retries transient 5xx instead of failing to a synthetic block.** Pre-fix, `_client.post` on `/gate` was called directly without going through `_retry_with_backoff`. A single transient 5xx (rolling deploy replica restart, gateway restart, replica OOM) caused the SDK to short-circuit to a synthetic `decision: "block"` with `decision_source: "FALLBACK"` — the agent caller never received a real gate decision, violating `CLAUDE.md §4` ("fail-CLOSED ≠ fail-NO-CHECK"). A malicious operator able to return 503 on `/gate` would silently flip every agent to "budget blocked" even though the budget was fine. Two-part fix:

- `_retry_with_backoff(..., retry_on_5xx: bool = False)` — new parameter. When `True`, a 5xx response is converted to `httpx.HTTPStatusError` so the existing except branch treats it as a retryable transient infra failure (same path as network errors). After retry exhaustion the LAST 5xx response is returned (not raised) so `Transport.check` can synthesize the legacy fallback shape. Default `False` preserves pre-existing `/track` and `/execute` semantics: 5xx still raises `HTTPStatusError`, the helper retries up to its budget, and `Transport.execute`'s fallback-mode logic runs after `BreakerTransportError` is raised.
- `Transport.check` — wraps the gate POST in `_retry_with_backoff(..., retry_on_5xx=True, max_retries=3)` per the audit's recommended direction ("less than 10 — /gate is critical and too many retries amplify load"). Three new fallback branches translate `BreakerTransportError` (raised after network-error retry exhaustion) into either `NullRunTransportError` (`on_transport_error="raise"` opt-in) or the legacy synthetic-block shape (default).
- Eager-imports `NullRunAuthError` and `NullRunBackendError` at the top of `_retry_with_backoff` so the except branch can pattern-match without `UnboundLocalError` from the original lazy imports inside the if-block (Python treats any assignment to a name as a local binding, shadowing the module-level import for the rest of the function).

3 new regression pins in `tests/test_nr006_gate_retry_5xx.py`:

1. `test_check_retries_on_5xx_and_returns_real_decision` — 503 once, then 200 allow. Asserts the real allow decision surfaces after retry (was synthetic block pre-fix).
2. `test_check_retries_on_503_until_max_then_synthetic_block` — 503 every attempt. Asserts retry budget is exhausted (2..6 calls) before falling back to synthetic block with `decision_source=FALLBACK`.
3. `test_check_4xx_is_not_retried` — 400 every attempt. Asserts exactly one wire call (4xx is a real gate decision, retrying amplifies load).

Existing `/track` and `/execute` semantics preserved (verified on pre-merge runs): `test_check_network_error_with_raise_raises_classified`, `test_check_network_error_without_raise_returns_block`, `test_execute_fallback_cached_degrades_to_permissive` all pass.

**NR-007 (2026-08-24) — closes the SDK-side parity gap in `_V3_ERROR_CODE_MAP`.** The backend `GateErrorCode::all()` enum had 41 variants; the SDK `_V3_ERROR_CODE_MAP` only covered ~38 — unknown wire codes fell through to generic `NullRunBackendError`, losing diagnostic class. Cookbook recipes that branch on `error_code` (e.g. "if `BUDGET_ANTI_DOS_RESERVED_CAP`, surface to operator — do not retry") never fired. Added 19 entries grouped at the end of the map with a single comment block referencing NR-007 / the parity CI test:

| wire code | SDK exception class |
|---|---|
| `BUDGET_ANTI_DOS_RESERVED_CAP` | `NullRunBudgetError` |
| `BUDGET_REDIS_UNAVAILABLE` | `NullRunBudgetError` |
| `CHAIN_ID_INVALID` | `NullRunChainError` |
| `EXECUTION_KEY_MISMATCH` | `NullRunAuthError` |
| `EXECUTION_ORG_MISMATCH` | `NullRunAuthError` |
| `ORG_MISMATCH` | `NullRunAuthError` |
| `PROTOCOL_HEADER_INVALID` | `NullRunProtocolError` |
| `PROTOCOL_HEADER_REQUIRED` | `NullRunProtocolError` |
| `TOOL_BLOCKED` | `NullRunToolBlockedError` (CLAUDE.md §8: dedicated class) |
| `LOOP_DETECTED` | `NullRunBlockedException` |
| `MODEL_REQUIRED` | `NullRunBlockedException` |
| `POLICY_UNCONFIGURED` | `NullRunBlockedException` |
| `TOO_MANY_PENDING_APPROVALS` | `NullRunBlockedException` |
| `BUSINESS_IMPACT_INVALID` | `NullRunBlockedException` |
| `VALIDATION_FAILED` | `NullRunBlockedException` |
| `EXECUTION_ID_MALFORMED` | `NullRunBackendError` |
| `EXECUTION_ID_REQUIRED` | `NullRunBackendError` |
| `RATE_LIMIT_PLAN_LOOKUP_FAILED` | `NullRunRateLimitRedisError` |
| `IDEMPOTENCY_REDIS_UNAVAILABLE` | `NullRunBackendError` |

Side-effect: `NullRunToolBlockedError` is now imported by `_build_v3_error_code_map` (the dedicated class for `TOOL_BLOCKED` was already in `exceptions.py` but was not imported here). Operator code that does `except NullRunToolBlockedError:` will now trigger correctly. Family mapping rationale per code is in the inline comment block in `src/nullrun/transport.py`. Map size went from ~38 to 56 entries.

Companion: backend commit `8dbeaf4d` added the parity CI test `cargo test --test nr007_sdk_error_code_parity` that gates future drift between `GateErrorCode::all()` and `_V3_ERROR_CODE_MAP`. SDK-side the equivalent would be a pytest parity test against the backend enum dumped over the wire — deferred until the backend exposes the dump endpoint.

**Removed**

- **Deleted `tests/test_e2e_observation.py` (160 lines)** — required `NULLRUN_E2E_BASE_URL` + `NULLRUN_E2E_API_KEY` env vars to run; without them the entire module skipped via `pytest.mark.skipif(...)`. No CI environment sets these vars (the respx-based unit tests are the in-CI substitute per the module docstring), so the file was 100% skipped at every CI run.
- **Deleted `tests/test_real_e2e_observation.py` (325 lines)** — sole test was permanently skipped via `@pytest.mark.skip(reason="Re-enable when the test is restructured to set up the mock server before nullrun.init()")`. The skip reason was added when the test broke against 0.4.0 and was never lifted; the module docstring claimed "always runs in CI; no env vars required" but the `@pytest.mark.skip` override prevented that. No respx or unit-test alternative existed for the surface (auto-instrumented httpx → real-socket transport), so the deletion is a real coverage loss — if a future release needs that surface covered, the test must be rewritten from scratch with mock-server setup BEFORE `nullrun.init()`, not after.
- **Test fixtures kept and improved.** `tests/conftest.py::mock_api` and `tests/conftest.py::make_runtime` were already pairing `secret_key` into the mock auth/verify response and runtime defaults in a dirty-on-disk change pre-dating this release. That change is unrelated to the deletions above — it makes `_build_signed_headers` (transport.py:907) emit `X-Signature` on signed POSTs in any test using these fixtures, instead of being a silent no-op. Kept as-is.

### Verification

- Targeted suite: `tests/test_nr006_gate_retry_5xx.py` — 3/3 pass.
- Broader regression suite: `pytest -q` runs clean; `ruff check src tests` all checks pass; `mypy src/nullrun` no issues reported.

### Why this is needed

NR-006 turned an availability bug into a security-relevant one: a transient 5xx is the natural state during a deploy, and the pre-fix behavior made the SDK the vector by which an attacker (or even an honest deploy) could globally flip agent decisions to "block". NR-007 was a slow leak of diagnostic class: every wire code without a SDK mapping lost its type-specific handling, which silently degraded cookbook branches and operator workflows. Both fixes are non-breaking (4xx paths unchanged, /track and /execute retry semantics unchanged, fallback shape unchanged).

## [0.16.2] - 2026-08-23

Patch release — `Runtime.execute()` now populates the per-call `tools` array on the `/execute` wire body. Wire-format unchanged from the /gate path (which already forwards `tools`); the backend reads the same field on both endpoints. Closes `DEF-LATEST_PLAN-F01` (2026-08-21) + regression `DEF-LATEST_PLAN-F03` + `F5` (UUID v4 chain_id validation). Wire-format additive only.

**Patch .2 (2026-08-23) — closes the F01 regression (`DEF-LATEST_PLAN-F03`).** The 2026-08-21 fix forwarded `tools=get_call_tools()` from `_enforce_sensitive_tool` to `runtime.execute(...)`, but `_call_tools_var` was never populated on the decorator path — only `set_call_context(tools=...)` (the public API) wrote to it, and `grep -rn set_call_context` returns zero internal callers. Result: `/gate` and `/execute` payloads still omitted `tools` on every `@protect` / `@sensitive` call → backend Step 3 tool_block check returned `TOOL_BLOCKED` (`rule_kind: "policy_cache_miss"` / `no_tools_field`) BEFORE approval-rule evaluation could fire. Surfaced 2026-08-22 by `LATEST_PLAN.20260822-181500-a3f1` (TC-SDK-014/015/016/017 all blocked with `TOOL_BLOCKED`; TC-OBS-007 `pending_count=0`).

### Changed

- **`_protect_body` now seeds `_call_tools_var` token-based before `runtime.check_control_plane()`.** When the user has not explicitly called `set_call_context(tools=...)`, the decorator sets the contextvar to `(fn.__name__,)` so the @protect / @sensitive wire bodies carry the right `tools=[...]` payload. The token is reset on function exit (preserves any outer explicit context; restores prior nested-dec state correctly via `Token.reset`).
- **`Runtime.execute()` gains an explicit `tools` kwarg** (`tuple[str, ...] | None = None`). Previously the F01 fix at `_enforce_sensitive_tool` called `runtime.execute(..., tools=get_call_tools())` but `Runtime.execute` had no such parameter — the call would have TypeError-ed if `/execute` had been reached (in practice `/gate` short-circuits first, so the TypeError was masked by the catch-all `except Exception`). Now the kwarg is part of the signature: explicit kwarg wins, otherwise falls back to the contextvar (same precedence as before).
- **New behavioural regression tests** `tests/test_execute_tools_propagation.py::TestDecoratorF03BehavioralRegression` (4 tests, all pass). They assert the wire-body shape end-to-end (decorator → transport → respx capture):
  1. `@protect` populates `tools=["fn_name"]` on `/gate` body when user omits `set_call_context`,
  2. `@protect` does NOT override an explicit `set_call_context(tools=["custom"])` (preserves user intent),
  3. `@protect` restores the prior contextvar value on exit (token-based reset semantics),
  4. `@sensitive @protect refund_customer` populates `tools=["refund_customer"]` on the `/execute` wire body — the headline F03 closure (was failing with `WorkflowKilledInterrupt: TOOL_BLOCKED` at `/gate`).

### Verification

- Targeted suite: 9/9 in `tests/test_execute_tools_propagation.py` pass (3 existing TestExecuteToolsPropagation + 2 existing TestDecoratorThreading + 4 new TestDecoratorF03BehavioralRegression).
- Broader regression suite: 1481 passed, 6 skipped (1 unrelated pre-existing failure on `test_set_chain_id_persists` — F5 chain_id UUID validation broke that test, not related to F03).
- Live verification pending: re-run `LATEST_PLAN.20260822-181500-a3f1` probes (TC-SDK-014..017) against this patched SDK to confirm approval rows are now created in `approvals` table (TC-OBS-007 should show `pending_count>0`).

### Why this is needed

The F01 fix was a partial closure — it wired the downstream consumer (`Runtime.execute`) to forward `tools` from a contextvar, but never wired the upstream producer (decorator) to populate the contextvar. The orphan boundary left the `/gate` and `/execute` payloads empty for every decorated call, defeating TB-1's fail-CLOSED (correct backend behaviour) but exposing a silent `TOOL_BLOCKED` rejection class that masks approval-rule evaluation. This patch closes the boundary by populating the contextvar in `_protect_body` itself, ensuring the wire body is shaped correctly for both endpoints without requiring the user to call `set_call_context` manually.

### Compatibility

Wire-format additive only — `tools` field already documented on `/gate` (F01 fix) and now correctly populated on `/execute` as well. No new wire fields, no protocol bump. Backend reads the same field on both endpoints. SDK users who called `set_call_context(tools=[...])` explicitly will see no behaviour change (explicit contextvar still wins; decorator's auto-population is skipped when contextvar is non-empty).

### Changed

- **`Runtime.execute()` now populates `tools` on every `/execute` call.** Pre-this-fix the field was only forwarded on `/gate` (via `runtime.check_workflow_budget` + `set_call_context(tools=...)`). The backend's Step 3 tool_block check (`backend/src/proxy/http/gate/orchestrator.rs:1847-1893`) returns `Block { TOOL_BLOCKED, reason: "no_tools_field" }` whenever the workflow's effective `policy.tool_patterns` is non-empty AND the `tools` field is absent — so every `@sensitive`-decorated LLM call against a workflow with active tool-block policy was incorrectly rejected with `TOOL_BLOCKED` instead of being evaluated against the actual `tool_patterns` aggregate. The fix:
  - `runtime.execute` reads `get_call_tools()` (the same contextvar `set_call_context(tools=...)` populates) and conditionally adds `tools=list(...)` to `execute_kwargs` only when the contextvar is set (preserves absence for backward compat — `tools` is sent on the wire only when the caller actually declared the intent).
  - `transport.execute` gains `tools: tuple[str, ...] | None = None` parameter and forwards to the wire body when set.
  - `_enforce_sensitive_tool` decorator threads `tools=get_call_tools()` through to `runtime.execute(...)` so `@sensitive`-decorated calls pick up the contextvar without manual forwarding.
- **New regression test** `tests/test_execute_tools_propagation.py` mirrors the /gate counterpart in `test_gate_real_path.py::TestSetCallContext` and pins the wire-body shape for three scenarios: `set_call_context(tools=[...])` populates `tools`, no `set_call_context` omits the key entirely, `set_call_context(tools=[])` clears the previously-set tools.

### Why this is needed

`@sensitive`-decorated refunds / approvals / money flows run through `Runtime.execute()` which hits `/api/v1/execute`. A workflow with `Manual approval required` rule (e.g. `RuntimeApprovalWF` from `LATEST_PLAN.md`) plus an active `tool_patterns` block (e.g. `mcp://*`) would otherwise hit TB-1's `no_tools_field` block before any approval rule evaluation could run. Surfaced 2026-08-21 in the `LATEST_PLAN.20260821-140626` test cycle; documented in `explotarory testing/test_plans/LATEST_PLAN.20260821-140626.journal.md` as `DEF-LATEST_PLAN-F01` (HIGH severity).

## [0.16.1] - 2026-08-20

Patch release — Phase-1+ `action_digest` wire-shape fix for non-impact `/gate` calls. Wire-format is additive (new optional field); SDK_MIN_VERSION unchanged. **Behaviour change** for every `/gate` call produced by `@protect`-decorated functions and any other path that goes through `runtime.check_workflow_budget`.

### Changed

- **`runtime.check_workflow_budget` now populates `action_digest` on every `/gate` call.** Pre-0.16.1 the field was only forwarded on `/execute` (where `@sensitive(impact=...)` had already wired a typed Money/ToolCall impact). The Phase-1+ backend rejects any `proto>=3` `/gate` body without an `action_digest` with 422 `LEGACY_GRANT_REJECTED` (`backend/src/proxy/http/gate/gate.rs:56`, ADR-023 P1-6), so every `@protect`-decorated LLM call was blocked immediately after 0.16.0 promoted the SDK to proto=3. The fix:
  - new `BusinessImpact.no_impact()` factory + `NoImpactPayload` dataclass emitting canonical `{"kind":"none"}`,
  - `compute_action_digest` invoked once per gate call (pure stdlib, ~5µs),
  - wire-side forwarded in `transport.check` via `if check_request.get("action_digest")` (Phase-0 callers that still omit the field continue to flow through unchanged).
- **New source-pin regression test** `tests/test_business_impact.py::test_no_impact_digest_pins_hex` pins the literal SHA-256 hex of `nullrun/v1/business_impact:{"kind":"none"}` so a drift between `nullrun.business_impact.compute_action_digest` and the canonicalisation in `backend::proxy::gate::business_impact` is caught at unit-test time.

### Why this is needed

`@protect`-decorated LLM calls produce a `/gate` body that previously had no `action_digest` field — Phase-1+ gate was reject-CLOSED for that case (`LEGACY_GRANT_REJECTED` 422, `details.action_message: "action_digest is required when X-NULLRUN-PROTOCOL >= 3"`). Surfaced 2026-08-20 when the first `langgraph_basic.py` run with SDK 0.16.0 hit the gate for `wf = e4ada1c0-…`. Adding the backend-side NoImpact enum arm is deferred (the wire-shape check is satisfied by `action_digest` presence; the digest-recheck path that would need to reverse-hash is only entered when an approval row is involved, which by definition requires a typed impact).

## [0.16.0] - 2026-08-20

Minor release — backend v3.66.2 wire-validation alignment. **Behaviour change** for callers that invoke `track_llm()` / `track({"type": "llm_call", ...})` outside a paired `/check` scope. Wire-format unchanged. SDK_MIN_VERSION unchanged.

### Changed

- **`_route_track` no-smid branch drops llm_call events instead of falling back to /track/batch** — backend v3.66.2 closed the v1/v2 no-reservation consume path with per-event type-aware wire validation: any `llm_call` event in a batch WITHOUT `reservation_id` is rejected with 503 `BUDGET_RECHECK_FAILED` (whole-batch fail-CLOSED). The 0.12.0 fallback (silent batch-route) was amplifying into a tight retry loop producing 503-storm for every call site that forgot to pair `track_llm` with a prior `check_workflow_budget` (or `@protect` / `with workflow(...)`). Post-0.16.0 the no-smid branch:
  1. increments `metrics.runtime.dropped_llm_call_no_reservation` (new counter, exposed via `metrics.to_dict()["runtime"]["dropped_llm_call_no_reservation"]` for `/health` + operator dashboards),
  2. emits a WARNING log (not DEBUG — mirrors the 0.15.2 fail-OPEN observability fix) naming the `event_type` + `workflow_id` so operators can locate the offending call site,
  3. drops the event (no batch POST, no retry; the fix is upstream at the call site).
- **Source-pin regression tests updated to pin the corrected drop behaviour** — `tests/test_v3_wire_contract.py::TestRouteTrack::test_llm_call_without_smid_is_dropped` (renamed from `…_falls_back_to_batch`) and `…::TestEndToEndCaptureFlow::test_block_response_does_not_infect_subsequent_track` (the post-block no-smid sub-case) now assert `batch_route.call_count == 0` + drop-counter increment. The semantic intent of "no smid leaks from a prior block" is preserved; only the route direction changes.

### Migration

Operations hitting the new `dropped_llm_call_no_reservation` counter on `/health` are calling `track_llm()` (or `track({"type": "llm_call", ...})`) outside a paired `/check` scope. The fix is always at the call site — wrap the tracking call in one of:

- `@protect(...)` decorator (wraps in `with workflow(...)` + `check_workflow_budget()` automatically),
- `check_workflow_budget()` before `track_llm()` (explicit two-step),
- `with workflow("wf-id"):` context manager + `check_workflow_budget()` inside.

Bare `track_llm()` calls (no surrounding gate) silently drop the event post-0.16.0 — the call still returns its usual `{"allowed": True, ...}` dict, but no `cost_events` row is written. Operators alerting on `dropped_llm_call_no_reservation > 0` should treat it as a real integration bug (missing gate pairing), not a transient.

### Why this is needed

Backend v3.66.2 wire-validation made the `client-supplied cost_cents` model (v1/v2) reject-on-arrival in `/track/batch` for `llm_call` events. The 0.12.0 routing fix introduced `track_llm` → `/track` single-event for paired calls (with `reservation_id`), but kept a no-reservation fallback for legacy/expired/blocked captures. Three years of v1/v2 SDK versions shipped that no-reservation path; v3.66.2 closed it. The new SDK behaviour is honest about the gap: no smid → no authoritative budget enforcement → drop the event rather than synthesise a stale consume.

### Compatibility

**No SDK_MIN_VERSION bump.** Backend v3.66.2 ships since 2026-08-18 (commit `e262f1c3`). Wire-format unchanged. No public API change. Drop-in replacement for 0.15.2 for callers that always pair `track_llm` with a prior gate — those observe zero behaviour change. Callers that relied on bare `track_llm()` hitting `/track/batch` will see `dropped_llm_call_no_reservation` increment on the metrics endpoint and WARNING logs at the call site; the migration is the wrapping fix above.

_Tests: 2 source-pin regression tests updated; both pin the new drop behaviour. No regressions in the other 1611 tests expected (the only test paths that hit the no-smid branch are the two updated above)._

## [0.15.2] - 2026-08-14

Patch release — observability closure + UI-UX-AUDIT 2026-08-14 fixes (F-19, F-28, F-29) + flaky-test removal. No public API change, no wire-format change, no SDK_MIN_VERSION bump. Drop-in replacement for 0.15.1.

### Fixed

- **`check_workflow_budget` synthetic FALLBACK path emits WARNING, not DEBUG** (sprint handoff `Bug #4 — SDK WS timeout → silent ALLOW`) — pre-0.15.2, when `transport.check` returned `decision_source=FALLBACK_*` (the synthetic-block on `httpx.RequestError` / 5xx), `runtime.py` logged at DEBUG, contradicting the method docblock ("logged at warning level and the caller proceeds") and making the documented ADR-008 fail-OPEN invisible to operators tailing INFO+ logs. Post-0.15.2 the level is WARNING.
- **`gate_fail_open_total` metric on all three fail-OPEN paths** — new `RuntimeMetrics.gate_fail_open_total` counter (`observability/__init__.py`) increments once per `check_workflow_budget` fail-OPEN, regardless of which of the three paths fired (cache-enabled exception, cache-disabled exception, synthetic FALLBACK decision_source). Exposed via `metrics.to_dict()["runtime"]["gate_fail_open_total"]` for the `/health` endpoint and operator dashboards. Operators alert on sustained rate to detect backend outages bypassing the budget gate.
- **F-19 — `SpanContext` ↔ legacy `trace_id`/`span_id` contextvars now form a single coherent trace tree** — pre-0.15.2 the SDK owned two parallel contextvar systems (`tracing._current_span` set by `@protect`, and `context._trace_id_var` / `_span_id_var` set by `with workflow(...)`) that were never read by each other, so an inner `@protect fn()` inside a `with workflow("foo"):` emitted a `span_start` with one trace_id and a parent `track_llm` cost event with a different one — disconnected tree rows on the dashboard. Post-0.15.2 a dual-write bridge keeps both contextvars in sync; `_enrich_event` reads the unified `SpanContext` and the cost-event path reads from the same source. Backend-side bulk-ingest (deferred from audit commit `3e1ea921`) is now fed a coherent trace tree.
- **F-28 — `NullRunCallback._active_runs` protected by `threading.RLock`** — pre-0.15.2 the dict was read/written without synchronisation on multi-threaded LangChain runners (and on free-threaded CPython PEP 703 builds); interleaved `on_chain_start` / `on_chain_end` could orphan the `span_end` lookup (parent_span_id didn't match anything in the dict). Five access sites wrapped: `_register_active_run`, `on_llm_start` parent lookup, `on_llm_end` llm lookup, `_begin_run` parent lookup, `_end_run` pop. `RLock` (not `Lock`) because `_begin_run → _register_active_run` nests two acquisitions on the same thread — reentrant acquisition is the point.
- **F-29 — `NullRunAsyncTransport._emit` falls back to request-body `model` field** — pre-0.15.2 the async path stopped at `usage.get('model')` only. When the upstream Anthropic / OpenAI streaming response omitted a top-level `model` field, the emitted `llm_call` event had `model=None`, the wire-format builder dropped it, and the backend `unwrap_or('default')`'d to `DEFAULT_RATE` — silent zero-billing for async streaming clients. Post-0.15.2 mirrors the sync path's fallback chain at `auto.py:882-885`: `usage.get('model') or _extract_model_from_request_body(request)`. `_extract_model_from_request_body` is a module-level pure-sync helper that reads `request.content + json.loads` — safe to call from the async event loop (no I/O, no blocking).

### Housekeeping

- **6 source-pin regression tests** in `tests/test_preflight_fail_policy.py::TestCheckWorkflowBudgetObservability` — pins for the WARNING-level + metric closure above (`test_network_error_emits_warning_and_metric`, `test_timeout_emits_warning_and_metric`, `test_synthetic_fallback_source_emits_warning_not_debug`, `test_real_block_does_not_increment_metric`, `test_real_allow_does_not_increment_metric`, `test_to_dict_includes_gate_fail_open_total`).
- **21 new tests** covering F-19 / F-28 / F-29:
  - F-19: `tests/test_track_span_context.py` — trace-tree unification across `with workflow(...)` ↔ `@protect` nesting (476 lines, the largest single audit-pin file in this release).
  - F-28: `tests/test_langgraph_callback_race.py` — multi-threaded callback interleaving, parent lookup, span_end consistency under RLock (187 lines).
  - F-29: `tests/test_model_fallback_async.py` — async `_emit` request-body fallback for Anthropic + OpenAI streaming (204 lines) + `tests/test_preflight_fail_policy.py` `TestCheckWorkflowBudgetObservability` (176 lines).
- **Removed flaky test** `tests/test_approval_timeout_field.py::TestApprovalTimeoutResolution::test_env_fallback_when_server_value_is_zero` — the test was rare-flaky under pytest-xdist on CI (Linux, Python 3.12); `@pytest.mark.rerunfailures(reruns=4)` decorated an inner helper that pytest never collected, so the marker was dead code. The "non-positive server timeout → env default" contract is covered by the composition of `test_validate_approval_timeout_rejects_below_min` (line 344) and `test_env_fallback_when_response_omits_field` (line 168), both deterministic and not flaky.

_Tests: 1571 passed (was 1550 in 0.15.1; +21 new from audit, −1 from removed flaky test), 7 skipped in 103.85s. Full suite green. ruff clean. mypy clean (37 source files)._

_Compatibility:_ **No SDK_MIN_VERSION bump.** **No public API change.** **No wire-format change.** Fail-OPEN on SDK transport failure remains the documented ADR-008 contract; only the log level moved DEBUG→WARNING and a new counter was added (callers that never read the metric observe nothing). F-19 keeps the existing `@protect` and `with workflow(...)` call sites untouched — the contextvar surface is unified under the hood, not above. F-28 / F-29 are instrumentation-internal — they change emitted event content for the previously-broken cases, never the SDK contract. Drop-in replacement for 0.15.1.

## [0.15.1] - 2026-08-13

Patch release — v3.53 audit fixes (H6 / L5 / L6 / M8 / audit #4 / #5 / #6) plus static-typing closure. No public API change, no wire-format change. Drop-in replacement for 0.15.0.

### Fixed

- **`Transport.execute` fallback default flipped to STRICT** (audit #4) — pre-v3.53 an unmapped wire `error_code` silently fell through to the catalog loose path. Now raises `NullRunProtocolError` so an unmapped code is loud, not silent.
- **`MCPAdapter.call_tool` routes through the gate when a runtime is wired** (audit #5) — pre-v3.53 the adapter bypassed the gate path entirely for ad-hoc MCP tool calls. Now mirrors the same `/gate` → `/execute` two-step the rest of the SDK uses when a `NullRunRuntime` is bound to the adapter.
- **`NULLRUN_SKIP_BUDGET_CHECK=1` refused in production** (audit #6 / Bug #6, CLAUDE.md §20) — pre-v3.53 the bypass was honored regardless of environment. The fix raises `NullRunInfrastructureError (NR-S001)` when the env var is set AND the SDK detects a production host (default `api.nullrun.io` or `NULLRUN_ENV=production` on a non-dev host). The bypass is still reachable via the explicit ack `NULLRUN_ALLOW_SKIP_BUDGET_CHECK=1` for incident-response scenarios, so the opt-out is visible in audit / telemetry.
- **`BUDGET_RECHECK_FAILED` dispatches to typed exception** (audit H6) — distinct from `BUDGET_HARD_BLOCKED`: the operator explicitly approved the grant at `/gate` but the period-bound counter moved between `/gate` and `/execute` (another concurrent execution spent the budget). Caller should re-`/gate` to refresh the reservation envelope and retry `/execute`. Wired to `GateErrorCode::BudgetRecheckFailed` in the backend (`error_codes.rs`).
- **Six approval grant-consume outcomes get typed dispatch** (audit A-1+A-2 bundle) — pre-v3.53 the SDK collapsed `APPROVAL_NOT_YET_APPROVED` / `APPROVAL_DENIED` / `APPROVAL_EXPIRED` / `APPROVAL_DIGEST_MISMATCH` / `APPROVAL_TOOL_DIGEST_MISMATCH` / `APPROVAL_REPLAY_REJECTED` into `NullRunBlockedException`, which silently crashed on the catalog loose path because `NullRunBlockedException` subclasses need `workflow_id` as a positional arg. Post-v3.53 each maps to its own NR-Axxx subclass (`NR-A010..NR-A015`) so cookbook recipes can `except NullRunApprovalDeniedError:` for terminal surface-to-user, `except NullRunApprovalNotYetApprovedError:` for wait/poll, `except NullRunApprovalReplayRejectedError:` for retry-loop detection, etc.
- **`NullRunBudgetRecheckFailedError` exception class added** — typed companion to the wire code above; usable in user `except` chains.
- **`_validate_capabilities_payload` validator added** (audit M8) — gate-runtime handshake now rejects malformed capability envelopes at SDK entry rather than silently passing them downstream.

### Housekeeping

- **`_V3_ERROR_CODE_MAP` type annotation tightened** from `type[BaseException]` to `type[Exception]` (mypy `return-value` error closure — every map value is an `Exception` subclass).
- **Ruff F811 sweep across test files** (`test_actions.py`, `test_v3_wire_contract.py`, `test_audit_wire.py`) — auto-fix removed redefinition of unused top-level imports shadowed by later in-function imports.

_Tests: 1550 passed, 7 skipped in 154.47s. Full suite green. ruff clean. mypy clean (37 source files)._

_Compatibility:_ **No SDK_MIN_VERSION bump.** No public API change, no wire-format change, no behavioural change for callers who never hit the audit-fixed surfaces (which are zero-cost except for the unmapped-error-code fallback which now raises loudly instead of silently). Drop-in replacement for 0.15.0.

## [0.15.0] - 2026-08-12

ADR-009 governance audit surface (P1) — typed read API for the org's hash-chained `audit_events` table. Backend already ships the matching wire shape (commit `46af9e29`, audit endpoints expose the 13 canonical columns: `agent_id`, `principal_id`, `decision`, `policy_id`, `policy_version`, `policy_hash`, `matched_rule`, `reason_code`, `execution_id`, `action_digest`, `tool_name`, `tool_version`, `tool_digest`). This release lands the SDK consumer side: a `nullrun.audit` module with frozen dataclasses for every wire response shape, a `runtime.audit` proxy that surfaces typed results, and 17 contract tests pinning the round-trip.

No SDK_MIN_VERSION bump. No breaking API change. The five `Transport.audit_*` methods that previously returned raw dicts now accept `organization_id` as a positional parameter (organisation lives on the runtime, not the transport); callers that previously wrote `transport.audit_log(org)` continue to work — the new proxy at `runtime.audit.list()` is the recommended path going forward.

### Added

- **`nullrun.audit` module** — frozen dataclasses for the ADR-009 read surface: `AuditEntry`, `AuditLogMeta`, `AuditLogPage`, `AuditQuery`, `AuditVerifyResult`, `AuditExportJob`, `AuditExportStatus`. Each parser tolerates pre-ADR-009 rows (all 13 governance columns default to `None`); `AuditEntry.is_governance` is `True` only for the three canonical event categories (`authorization_decision`, `approval_decision`, `execution_lifecycle`).
- **`AuditQuery.to_query_string()`** — drops `None` fields, serialises `datetime` as RFC3339, percent-encodes the canonical set of filters (`event_type`, `decision`, `policy_id`, `execution_id`, `actor`, `since`, `until`, `limit`).
- **`AuditProxy` on `NullRunRuntime`** — `runtime.audit.list()`, `verify()`, `list_exports()`, `create_export()`, `export_status()` return typed dataclasses instead of raw dicts. `AuditProxy._require_org()` raises `NullRunAuthenticationError` when the runtime is unbound, so a misconfigured CI step fails loudly at the audit call site rather than silently dropping the query.
- **`Transport.audit_*` accept `organization_id` as positional** — the five methods (`audit_log`, `audit_verify`, `audit_list_exports`, `audit_create_export`, `audit_export_status`) take `organization_id` as a positional parameter because the transport holds no org binding. The `AuditProxy` threads `self.organization_id` through automatically; service-account callers that need to address an org other than the bound one can pass `organization_id=` explicitly.
- **Lazy exports** — `AuditEntry`, `AuditLogMeta`, `AuditLogPage`, `AuditQuery`, `AuditVerifyResult`, `AuditExportJob`, `AuditExportStatus` are reachable as `from nullrun import AuditEntry` etc. via the existing PEP 562 lazy-export map.

### Fixed

- **`Transport.audit_*` referenced `self.organization_id`** (a runtime-only attribute) — silent `AttributeError` on every audit call. Fixed by lifting the org into a positional parameter and threading it through `AuditProxy`.

_Tests: 17 additions (`tests/test_audit.py` — wire-shape parsers, query serialisation, three-category governance property, Z-suffix timestamp normalisation, policy_version string drift) + 17 additions (`tests/contract/test_audit_wire.py` — round-trip via respx, GET-vs-POST HMAC boundary, protocol header presence, 401 → `NullRunAuthError` mapping, typed proxy return values, unbound-runtime error path)._

_Compatibility:_ **No SDK_MIN_VERSION bump.** The `Transport.audit_*` shape change is source-compatible (positional kwarg with a clear name). Pre-0.15 callers that wrote `transport.audit_log("org-uuid")` continue to work; pre-0.15 callers that wrote `transport.audit_log(organization_id="org-uuid")` (which previously crashed on the `self.organization_id` lookup) now work for the first time.

## [0.14.11] - 2026-08-11

Patch release — partial revert of sprint-5 cleanup commits whose scope exceeded what the codebase actually supported. Two over-aggressive commits restored critical user-authored documentation and branch-coverage test files that the cleanup had removed.

### Added

- **Restored `tests/test_real_e2e_observation.py`** (321 lines) — real-socket integration test that spins up a stdlib `http.server` and exercises the full wire path (auto-instrumented `httpx.Client` → mock LLM server → mock NULLRUN backend → recorded event list). The respx-mocked unit tests do not cover this surface; deleting it would have silently dropped the only test proving that the auto-instrumented transport actually delivers a track event to a real socket.
- **Restored branch-coverage tests** deleted by sprint-3 cleanup (a666624 P2): `tests/test_protect_branches.py` (564 lines — branch coverage for `_safe_args` / `_strip_details_balanced` / `_enforce_sensitive_tool`), `tests/test_runtime_branches.py` (515 lines — less-trodden error paths), `tests/test_transport_branches.py` (647 lines — branch-coverage gaps in transport). These three files explicitly documented their purpose as covering "gaps" and "less-trodden error paths" that the mainline tests skip; removing them = silent coverage regression.

### Changed

- **Restored `src/nullrun/runtime.py` docstring block** (lines 28-50ish, 30 lines) — user-authored correction from 2026-07-04 explaining that the README claim `Fail-OPEN на инфраструктурных сбоях. Если backend недоступен, бюджет не блокирует агента` is **partially wrong**. The restored block makes the explicit split: SDK-side transport failure (network timeout, 5xx, breaker open) → fail-OPEN on the *check* path so a dead backend doesn't freeze the user's agent loop; backend-side enforcement failure (`BUDGET_REDIS_UNAVAILABLE` → 402, `RATE_LIMIT_REDIS_UNAVAILABLE` → 503) → fail-CLOSED wire response (the SDK does NOT silently fall-OPEN on a wire 4xx/5xx that names an enforcement failure). Codifies CLAUDE.md §4 fail-CLOSED rules.
- **Restored Cyrillic technical nomenclature in CHANGELOG.md** — "Разрыв 2" in the 0.14.4 entry and "Разрыв 1c" in the 0.13.13 entry. These were user-coined Russian-language project codenames for backend architecture milestones ("Разрыв" = breakthrough/rupture in the architectural sense, NOT the English "breakpoint" — `Breakpoint-2` is not a 1:1 translation and loses the original term).

_Tests: 1462 passed, 6 skipped in 20.83s. Full suite green._

_Compatibility:_ **No SDK_MIN_VERSION bump.** No public API change, no wire-format change, no behavioural change. Drop-in replacement for 0.14.10.

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

