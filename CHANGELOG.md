# Changelog

All notable changes to `nullrun-sdk` will be documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html)

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

- **HMAC always-on when `secret_key` is present.** The SDK now signs every
  outgoing POST/GET (auth/verify, /track/batch, /gate, /evaluate, /status)
  via the new `Transport._signed_post` / `_signed_request` helpers. The
  outgoing WebSocket ACK is also signed (mirroring incoming-message
  verification). Header set is built once via `_build_signed_headers`
  (Content-Type, X-API-Version, X-API-Key, X-Signature,
  X-Signature-Timestamp, W3C trace context). Previously only
  /track/batch and /gate were signed; auth/verify, /status GET, and
  WS ACKs were not. Compliant with the canonical
  `HMAC-SHA256(secret_key, "<ts>:<api_key>:<sha256_hex(body)>")` formula
  from `backend/src/auth/hmac.rs:6-9`.

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
