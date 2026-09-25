## [0.18.3] - 2026-09-25

### Surface (breaking)

- `nullrun.init_or_die()` removed. CLI fail-fast behavior is now a
  parameter on `init()`: `nullrun.init(fail_on_exit=True)` prints the
  same four-line developer report and `sys.exit(1)` on configuration
  failure.
- `nullrun.shutdown()` is now auto-registered via `atexit` inside
  `init()` — long-running scripts get a clean WS close on process
  exit without an explicit call. Calling `shutdown()` manually
  remains safe and idempotent.

### Lifecycle

- `init()` gains a `fail_on_exit: bool = False` keyword argument.
  When True, missing `NULLRUN_API_KEY` (NR-C001) prints the
  developer-facing report and exits 1 instead of raising. Default
  False preserves the embedder-friendly raise semantics.
- The `_shutdown_atexit_registered` module-level flag prevents
  double registration when `init()` is called more than once.

Top-level `dir(nullrun)` no longer exposes `init_or_die`. All other
symbols unchanged from 0.18.2.

## [0.18.2] - 2026-09-22

### Surface

`@protect` is the single user-facing entry point. Every call routes
through `/api/v1/execute` unconditionally — no opt-outs, no per-tool
registry to manage, no alternate decorators.

Top-level `dir(nullrun)` exposes only:

- Lifecycle: `init`, `protect`, `shutdown`, `on_error`, `status`,
  `handle`, `guarded`, `init_or_die`
- Messages: `format_user_message`, `set_user_message`
- Exceptions: `NullRunError` and the typed catalog
  (`NullRunAuthError`, `NullRunBackendError`, `NullRunBudgetError`,
  `NullRunConfigError`, `NullRunMcpApprovalRequiredError`,
  `NullRunMcpDestructiveBlockedError`,
  `NullRunMcpReadonlyBypassBlockedError`, `NullRunToolBlockedError`,
  `NullRunWorkflowKilledError`, `WorkflowKilledInterrupt`)

Wire contract: unchanged from 0.18.0.
