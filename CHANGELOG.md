## [0.18.5] - 2026-09-26

Consolidated release that bundles three rounds of work landed on
`cleanup/deprecation-removal` (#111) and never shipped as separate
versions: the 0.18.3 `init`/`init_or_die` unification, the 0.18.4
`handle` → `guard` rename plus top-level `status()` removal, and
the follow-on deprecation sweep (drop redundant `@guarded`, drop
framework-extras / `auto_instrument` public surface, strip legacy
"pre-fix / post-fix" docstring framing, drop stale `init_or_die`
references in source docstrings). Also adds a mechanical lint/type
cleanup pass that brings `ruff check src tests` and `mypy
src/nullrun` back to green after the 1882-insertion / 15621-deletion
sweep across the branch (71 unused imports, 9 unused locals,
`BusinessImpact.impact` narrowed from `Any` to `NoImpactPayload`,
`_LAZY_EXPORTS` annotation tightened).

### Surface (breaking)

- `nullrun.handle` renamed to `nullrun.guard`. Same
  `@contextmanager` body (catches `NullRunError`, re-raises
  `WorkflowKilledInterrupt`, renders the four-line developer report,
  `sys.exit(1)` on failure). The name `guard` was freed when
  `@guarded` was dropped earlier in this release. Migrate by
  replacing `from nullrun import handle` / `with nullrun.handle:`
  with `from nullrun import guard` / `with nullrun.guard():`.
- Top-level `nullrun.status()` removed. Reach the snapshot via
  `nullrun.get_runtime().status()` (returns the same frozen
  `NullRunStatus` dataclass). The wrapper's only role was raising
  `NullRunConfigError(NR-C004)` when no runtime was bound — that
  path is now `get_runtime()`'s job. `NullRunStatus` itself
  remains importable as a type.
- `nullrun.init_or_die()` removed. CLI fail-fast behavior is now a
  parameter on `init()`: `nullrun.init(fail_on_exit=True)` prints
  the same four-line developer report and `sys.exit(1)` on
  configuration failure.
- `nullrun.shutdown()` is now auto-registered via `atexit` inside
  `init()` — long-running scripts get a clean WS close on process
  exit without an explicit call. Calling `shutdown()` manually
  remains safe and idempotent.
- `@nullrun.guarded` decorator removed. It was a 3-line syntactic
  shortcut for `with nullrun.guard():`; with `guard` as the
  canonical error-translation path the decorator form was pure
  duplication.
- Framework-extras install groups dropped from `pyproject.toml`
  (`[agents]`, `[langchain]`, `[langgraph]`, `[llama-index]`,
  `[crewai]`, `[autogen]`). The silent auto-detect instrumentation
  patches remain in code, but `nullrun` no longer advertises
  per-framework install paths or example wiring. Public install
  is one line: `pip install nullrun`.
- `nullrun.auto_instrument` / `nullrun.is_auto_instrumented`
  removed from the top-level namespace (they were internal
  triggers, not user-facing API). Module-level `track_event`
  alias removed (duplicate of `track`; the `runtime.track_event`
  method is still used internally). `NullRunCallback` removed
  from `_LAZY_EXPORTS` (advanced / manual path; reachable via
  `nullrun.toolbox.langgraph` if ever needed).

### Lifecycle

- `init()` gains a `fail_on_exit: bool = False` keyword argument.
  When True, missing `NULLRUN_API_KEY` (NR-C001) prints the
  developer-facing report and exits 1 instead of raising. Default
  False preserves the embedder-friendly raise semantics.
- The `_shutdown_atexit_registered` module-level flag prevents
  double registration when `init()` is called more than once.

### Tooling

- Strips "pre-fix / post-fix", "previously / now we", "was X / is
  now Y", and "legacy" framing from public docstrings across
  `runtime.py`, `transport.py`, `decorators.py`, and 14 other
  modules. External ticket identifiers (DEF-*, ADR-*, AUDIT P*,
  IDEM-01, CLOSE-ORPHAN) are preserved as anchors.
- Mechanical lint cleanup: 71 unused imports (F401) auto-removed,
  9 unused locals (F841) hand-removed across `tests/`. `ruff check
  src tests` and `mypy src/nullrun` are clean on the merged
  branch.

### Curated surface after 0.18.5

```text
__version__, init, protect, shutdown, on_error, guard,
NullRunError, NullRunAuthError, NullRunConfigError,
NullRunBackendError, NullRunBudgetError, NullRunToolBlockedError,
WorkflowKilledInterrupt, NullRunWorkflowKilledError,
NullRunMcpDestructiveBlockedError,
NullRunMcpReadonlyBypassBlockedError,
NullRunMcpApprovalRequiredError,
NullRunApprovalDbUnavailableError,
format_user_message, set_user_message
```

(Net vs 0.18.2: `status`, `handle`, `guarded`, `init_or_die`,
`auto_instrument`, `is_auto_instrumented`, `track_event` removed;
`guard` added. Net -6 symbols vs 0.18.2.)

Wire contract: unchanged from 0.18.0.

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