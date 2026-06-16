# Changelog

All notable changes to `nullrun-sdk` will be documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html)

---

## [Unreleased]

### Added

- **Async Policy Cache**: `AsyncTransport` now uses `PolicyCache` for CACHED fallback mode. Previously the async transport always fell back to PERMISSIVE when gateway was unreachable. Now it caches successful execute decisions and uses them when gateway is unavailable.
- **Custom Sensitive Tools API**: Added `add_sensitive_tool()`, `remove_sensitive_tool()`, `register_sensitive_tools()`, and `get_sensitive_tools()` methods to `NullRunRuntime`. Users can now register custom tools as sensitive requiring strict mode enforcement.
- **`NullRunBlockedException.tool_name` attribute** (FIX-5): The `tool_name`
  kwarg is now a first-class attribute on `NullRunBlockedException`
  (and its subclasses `LoopDetectedException`, etc.) instead of being
  absorbed into `**details`. Cookbook examples that read `exc.tool_name`
  no longer raise `AttributeError`. Backwards-compatible: `tool_name`
  defaults to `None` and does not appear in `exc.details` when unset.
  The stringified exception now includes `tool={name}` when set.

### Fixed

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

[Unreleased]: https://github.com/maltsev-dev/nullrun-sdk/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/maltsev-dev/nullrun-sdk/releases/tag/v0.1.1
[0.1.0]: https://github.com/maltsev-dev/nullrun-sdk/releases/tag/v0.1.0
