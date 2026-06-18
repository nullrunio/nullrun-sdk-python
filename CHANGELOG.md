# Changelog

All notable changes to `nullrun-sdk` will be documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html)

---

## [Unreleased]

### Removed (0.4.0 deprecations — full removal in 1.0.0)

- **gRPC transport removed** (`src/nullrun/grpc_transport.py`, `protos/`): The backend
  proto is frozen and missing trace/span fields. The `NULLRUN_USE_GRPC` env var
  is now a no-op that emits a single WARNING at init. HTTP is the only
  supported ingestion path. Affects users who relied on the binary
  protobuf+HTTP/2 path — migrate to the HTTP transport. `grcpio` and
  `grpcio-tools` removed from `pyproject.toml`.
- **AsyncTransport removed** (`src/nullrun/transport.py:AsyncTransport`): ~600 lines
  of duplicate code that was used only in tests. The sync `Transport` works
  fine from async event loops via `nullrun.track_llm` / `@nullrun.protect` —
  the underlying httpx client + background flush thread is non-blocking.
- **`AdaptivePool` removed** (`src/nullrun/transport.py:AdaptivePool`, `PoolConfig`):
  Backpressure pool for the deleted async transport.
- **Process-wide signal handler removed** (`src/nullrun/transport.py:_register_signal_handlers`):
  The `Transport.__init__` no longer overwrites the application's global
  `SIGTERM`/`SIGINT` handlers. Callers in long-lived services MUST now
  call `transport.stop()` explicitly, use `transport` as a context
  manager (`with Transport(...) as t:`), or rely on `weakref.finalize`
  for cleanup at process exit. The new `__exit__` method is the
  recommended pattern.

### Fixed

- **track() double-emit** (P0): The `else` branch of `runtime.track()`
  no longer calls `self._transport.track(...)` twice. Previously the
  non-gRPC code path produced two `/api/v1/track/batch` events per
  call, double-billing customers.
- **Buffer re-binding race** (P0): `Transport._do_flush_locked` no
  longer rebinds `self._buffer` to a new list. A new
  `_drain_batch()` helper uses in-place slice (`del self._buffer[:]`)
  so concurrent `track()` calls holding a reference to the old list
  see the post-drain state instead of appending to dead memory.
- **Buffer overflow check** (P0): The CB-OPEN re-queue previously
  computed `available_space = max_buffer_size - len(self._buffer)`,
  but `self._buffer` was already empty (cleared by `_drain_batch`).
  The overflow check was a no-op, and the buffer grew unboundedly
  under sustained backend outage. The fix checks `len(batch)` against
  `max_buffer_size` and drops the oldest events in the batch.
- **ActionHandler kill contract** (P0): `ActionHandler.handle` no
  longer catches `BaseException`. The default KILL handler
  intentionally raises `WorkflowKilledInterrupt` (a `BaseException`
  subclass) to halt the agent; the previous code silently swallowed
  it, breaking the kill contract. The fix catches only `Exception`
  and explicitly re-raises `WorkflowKilledInterrupt` and
  `WorkflowPausedException`. `KeyboardInterrupt` and `SystemExit`
  also propagate correctly.
- **Signal handler side-effects** (P0): The previous
  `_register_signal_handlers` called `sys.exit(0)` from inside a
  signal context (undefined behaviour per CPython docs) and did file
  I/O (`_persist_to_wal`) from a signal context. Both unsafe; removed
  with the handler.
- **Atexit LIFO ordering** (P0): Multiple `Transport()` instances
  each registered an `atexit` handler, and the LIFO order meant the
  last-constructed transport's flush ran first. The new
  `weakref.finalize` approach is per-instance: if the transport has
  been GC'd, the atexit is a no-op; if not, the flush runs exactly
  once.

### Added

- **Context manager support** (`Transport.__enter__` / `__exit__`): The
  recommended lifecycle for long-lived services:
  `with Transport(api_url=..., api_key=...) as t: ...`
  The `__exit__` calls `stop()` which flushes and closes the
  client.
- **`_atexit_flush_safe`**: A wrapper around `_atexit_flush` that
  catches any exception in the flush and logs it. The atexit chain
  must never propagate an exception (which would skip subsequent
  atexit handlers in some Python implementations).

---

## [Unreleased]

### Added

- **`Transport.evaluate()`**: New public method for the pre-validation
  ("what if") path. Routes through the SDK's own connection pool, HMAC
  signing, circuit breaker, and retry policy. The previous implementation
  in `runtime.evaluate()` reached into `transport._client` directly,
  silently bypassing the circuit breaker — a production hazard.
- **`Transport.check()` `on_transport_error` parameter**: matches the
  contract of `Transport.execute()` (`"raise"` / `"open"` / `"closed"` /
  `"legacy"`). The previous default returned `decision="block"` on
  every transport error, which contradicted the ADR-008 fail-OPEN
  promise for `check_workflow_budget`.
- **`AsyncTransport.execute()` `on_transport_error` parameter**: mirrors
  the sync `Transport.execute()` contract. The previous async
  implementation used a 2-attempt retry loop with no classified
  failure source — calling gates (e.g. `_enforce_sensitive_tool` in
  `decorators.py`) could not tell a transport failure apart from a
  real policy block.
- **`NullRunRuntime.check_control_plane()` accepts `str | None`**: the
  previous signature required `str` even though the contract is
  "contextvar → API-key-bound workflow → no-op" and `None` is the
  canonical "no workflow scoped" value. The wrapper no longer needs
  to fake a value.
- **`_safe_bump_coverage` helper in `nullrun.instrumentation.auto`**: a
  new module-level utility that bumps a per-host coverage counter on
  the runtime. Tolerates stub runtimes (MagicMock, namespace
  objects) by no-oping when the counter attribute is missing. The
  sync/async transport hooks and `auto_requests` now use it to
  record per-host coverage consistently.
- **README env var table** now documents `NULLRUN_TRANSPORT`
  (WebSocket vs HTTP poller), `NULLRUN_WAL_PATH` (WAL override),
  and `NULLRUN_TLS_CA_CERT` (custom HTTPS CA).

### Fixed

- **Test fixture path**: `tests/test_kill_contract.py` no longer references the obsolete `sdk-python/` path; it now documents the correct repository name.
- **Examples**: `examples/basic.py`, `examples/async_usage.py`, `examples/basic_observe.py`, `examples/cost_dashboard.py` updated to the 0.3.0 contract (api_key is required, no `organization_id` kwarg, no `coverage_report()`).
- **Version drift**: `src/nullrun/__version__.py` was reporting `0.2.0` while `pyproject.toml` declared `0.3.0`; both now agree on `0.3.0`.
- **License classifier**: `pyproject.toml` now declares `License :: OSI Approved :: Apache Software License` (the actual license is Apache-2.0).
- **Type hint fix**: `NullRunRuntime.wrap_tool` / `wrap` now annotate `Callable[..., Any]` (from `collections.abc`) instead of the built-in `callable` function — `mypy --strict` is now happy.
- **Wrap NameError**: `NullRunRuntime.wrap` previously referenced an undefined `workflow_id` in its `NullRunBlockedException` raise; it now resolves the workflow id from the contextvar (with a `<unknown>` fallback matching the rest of the runtime).
- **`requests` import removed**: `Transport._refetch_credentials` no longer imports `requests` (which was never declared as a dependency); it now uses the SDK's own `httpx` client.
- **`_safe_bump_coverage` added**: `nullrun.instrumentation.auto` now exports the `_safe_bump_coverage` helper that `nullrun.instrumentation.auto_requests` and the sync/async transport hooks all use to record per-host coverage counters on the runtime.
- **Transport error routing (ADR-008)**: `Transport.execute()` and `Transport.check()` now accept an `on_transport_error` kwarg (`"raise"` / `"open"` / `"closed"` / `"legacy"`) and classify the failure source as `NETWORK_ERROR` / `GATEWAY_ERROR` / `BREAKER_OPEN` (was previously opaque to the caller).
- **`transport.check` fail-OPEN**: `check()` no longer silently returns `decision="block"` on every transport error; it routes through the same `_handle_transport_error` helper as `execute()` and lets the caller decide.
- **`shutdown()` closes gRPC**: `NullRunRuntime.shutdown()` now also closes the gRPC channel when one is in use, so a `NULLRUN_USE_GRPC=1` init does not leak the channel on shutdown.
- **`GrpcTransport.track` cost_cents default**: the gRPC `track` method's `cost_cents` argument is now keyword-only with a default of `0`, matching the runtime call site.
- **WAL location**: `Transport._persist_to_wal` now writes to `$NULLRUN_WAL_PATH` or `$TMPDIR/.nullrun.wal` (per-user) instead of `os.getcwd()/.nullrun.wal` — the previous location was unsafe in production.
- **CHANGELOG links**: links now point at the canonical repository `nullrunio/nullrun-sdk-python`.
- **CI mypy strict**: `mypy src/nullrun --strict` is now wired into the `make check` / `make type-check` targets and the GitHub Actions CI workflow runs it explicitly.
- **CI ruff tests**: the GitHub Actions CI workflow now lints both `src/` and `tests/` (the previous version only covered `src/`).
- **AsyncTransport `_client is None` bug**: `AsyncTransport._flush_locked()` now lazy-initializes the httpx async client if `start()` was not called first. Tests that drive `_flush_locked` directly (a few in `tests/test_transport.py`) no longer crash with `'NoneType' object has no attribute 'post'`.
- **`_retry_with_backoff` propagation**: the helper used to wrap the original exception in `BreakerTransportError` on retry exhaustion, conflating "CB OPEN" and "retries exhausted". It now re-raises the original exception so the calling gate can classify the source (network vs 5xx).
- **Defense-in-depth check in `decorators._enforce_sensitive_tool`**: now recognizes both `"FALLBACK_*"` (legacy `fallback_mode` shape) and the new `TransportErrorSource` enum values (`NETWORK_ERROR` / `GATEWAY_ERROR` / `BREAKER_OPEN` / `AUTH_ERROR`). The old check only matched `"FALLBACK_*"`, which meant a synthetic allow with `decision_source="NETWORK_ERROR"` would slip through.
- **`auto._check_kill_before_send` tolerates stub runtimes**: was crashing with `AttributeError: 'X' object has no attribute '_resolve_workflow_id'` when patched against a MagicMock / namespace runtime. Now uses `getattr(runtime, "_resolve_workflow_id", None)` and no-ops when the method is missing.
- **`_strip_wire_only_fields` helper**: centralized the wire-format contract (currently a single field: `cost_cents`) in one method on `NullRunRuntime` so future local-only fields land in the same place.
- **Test: `tests/test_runtime.py`** — removed the `NULLRUN_WORKSPACE_ID` env var that the SDK does not read (the organization id comes from `/auth/verify` in 0.3.0).
- **Test: `tests/test_runtime_default_transport.py`** — fixed path references from `sdk-python/src/...` to the correct `src/...`.
- **Test: `tests/test_toolbox_langgraph.py`** — the wrapper tests now pass a stub `runtime` argument so they do not require `NULLRUN_API_KEY` in the test env. The public wrapper contract (`wrapper(app, runtime=None)`) is unchanged.
- **Test: `tests/test_preflight_fail_policy.py`** — `test_real_block_still_honored` now distinguishes the budget pre-check from the sensitive-tool pre-check via a `side_effect` callback that reads the request body. The two gate calls share the `/api/v1/gate` URL but differ in the `check_type` field, so a single response mock would have masked the real-block path.
- **Dockerfile**: the runtime stage now installs `nullrun[langgraph]` (not `nullrun-breaker[langgraph]`, which never existed) and sets `CMD ["python"]` (the `python -m nullrun.breaker` entry point it referenced does not exist; the SDK is a library, not a CLI).

### Removed

- **`docs/adr/008-...` references**: docstrings in `runtime.py` and `transport.py` no longer reference a path that does not exist in this repository (the ADR lives in the gateway repo, not the SDK).
- **Stale CHANGELOG link to `maltsev-dev/nullrun-sdk`**: replaced with the canonical `nullrunio/nullrun-sdk-python`.
- **Stale `docs/kill-contract.md` and `docs/known-limitations.md` references**: docstrings now point at the gateway repository, where the canonical design notes live.
- **Dead "How to integrate" comment block in `observability.py`**: the comment showed the old direct-attribute pattern (`metrics.transport.batches_sent += 1`); the runtime actually uses the lock-aware `metrics.inc_transport(...)` path.

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

### Added

- **Async Policy Cache**: `AsyncTransport` now uses `PolicyCache` for CACHED fallback mode. Previously the async transport always fell back to PERMISSIVE when gateway was unreachable. Now it caches successful execute decisions and uses them when gateway is unavailable.
- **Custom Sensitive Tools API**: Added `add_sensitive_tool()`, `remove_sensitive_tool()`, `register_sensitive_tools()`, and `get_sensitive_tools()` methods to `NullRunRuntime`. Users can now register custom tools as sensitive requiring strict mode enforcement.

### Fixed

- **SDK silent runtime fallback removed** (FIX-4): `_get_or_create_runtime`
  in `nullrun.decorators` no longer wraps `NullRunRuntime.get_instance()`
  in a `try/except Exception` that rebuilds a no-arg `NullRunRuntime()`.
  In 0.3.0 (T3-S2) the no-arg constructor requires `api_key` and raises
  `NullRunAuthenticationError` — so the fallback swallowed the auth
  error from `get_instance()` only to crash with the same error from
  the fallback path itself. After this fix, the auth error propagates
  cleanly to the first `@protect` invocation, mirroring the fail-loud
  contract of `nullrun.init()`.
- **`NullRunBlockedException.tool_name` attribute** (FIX-5): The `tool_name`
  kwarg is now a first-class attribute on `NullRunBlockedException`
  (and its subclasses `LoopDetectedException`, etc.) instead of being
  absorbed into `**details`. Cookbook examples that read `exc.tool_name`
  no longer raise `AttributeError`. Backwards-compatible: `tool_name`
  defaults to `None` and does not appear in `exc.details` when unset.
  The stringified exception now includes `tool={name}` when set.

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
- Compatible with NullRun API version `2026-06-16`

---

## How to upgrade

### 0.1.x → 0.2.x

_No breaking changes recorded. The 0.2.x line was a hardening series that did not break the public surface._

### 0.2.x → 0.3.0

- `nullrun.init()` calls without `api_key` will raise. Pass
  `api_key="nr_live_..."` explicitly or set `NULLRUN_API_KEY`.
- `NullRunRuntime(...)` constructions without `api_key` will raise
  (same fix).
- Tests using `NullRunNoop` / `local_mode=True` mocking must switch
  to `NullRunRuntime(api_key="test-key", _test_mode=True)` —
  `_test_mode` skips the network calls without silently bypassing
  policy.
- `from nullrun import BreakerError` (and the 6 other legacy names)
  must use the canonical paths:
  `from nullrun.breaker.exceptions import NullRunBlockedException`
  and `from nullrun.runtime import Policy` / `from nullrun.transport import FallbackMode, PoolConfig`.

---

[Unreleased]: https://github.com/nullrunio/nullrun-sdk-python/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/nullrunio/nullrun-sdk-python/compare/v0.1.1...v0.3.0
[0.1.1]: https://github.com/nullrunio/nullrun-sdk-python/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/nullrunio/nullrun-sdk-python/releases/tag/v0.1.0
