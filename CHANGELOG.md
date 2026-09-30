## [0.19.0] - 2026-09-30

Closes the SDK-side bypasses found auditing `DEF-MP-TS12-ENF-01`
(QA cycle RUN_ID 20260929T1338): the gate answering the agent
"allowed" on calls it had actually blocked or never checked.

### Surface (breaking)

- `NullRunRuntime.execute(..., mode="inline")` removed. It
  returned a synthesised local `allow` **without contacting the
  gateway**, so budget, rate limit and tool-block policies were
  all skipped — the SDK's own `explanation` string said as much.
  The only guard was a sensitivity check, which meant the safety
  of a tool call depended on whether someone had remembered to
  mark it sensitive. It now raises `NullRunConfigError`
  (`error_code="NR-S001"`) at the top of `execute`, before any
  context resolution. **There is no replacement**: every call
  goes through `/execute`. If you were using `mode="inline"` to
  avoid a round-trip, drop the argument — `mode="auto"` (the
  default) already always contacts the gateway.
- `nullrun.runtime.register_strict_mode_forced`,
  `nullrun.runtime.is_strict_mode_forced` and the module-level
  `_STRICT_MODE_FORCED` set removed. They existed only to force
  strict mode past the inline fast path. `register_strict_mode_forced`
  already had zero callers (the `@sensitive` decorator its own
  docstring referenced no longer exists in the SDK); dead security
  machinery reads as a live mechanism and invites a bypass being
  wired back up.
- `MCPAdapter(runtime=None)` no longer means "do not gate".
  `call_tool` was conditional on `self._runtime is not None`, and
  `runtime` defaulted to `None` — so a default-constructed adapter
  (which is what the module's own documented example builds) called
  the MCP server with no `/execute` round-trip at all. The operator
  got a contextvar that a *later* `@protect` wrapper might read on
  its *next* `/check`: post-hoc annotation, not enforcement. The
  umbrella `mcp_destructive_policy` / `mcp_readonly_policy`
  therefore applied to a locally-declared function but not to a
  remote MCP call, on the same agent, in the same loop.
  `runtime=None` now means "resolve the global runtime", on the
  same terms `@protect` resolves it. Resolution is lazy — at
  `call_tool`, not at construction — so the adapter stays
  constructible in fixtures and doc snippets without
  `nullrun.init()`. A missing API key raises rather than degrading
  to an ungated call, matching `@protect`'s fail-loud invariant.
  Passing `runtime=` explicitly still works and still wins.

`mode` itself is unchanged on the wire — it is still sent, and the
backend still ignores it (`transport.py`: "Wire-present but unused
by backend"). Its only real function was deciding whether to skip
enforcement.

The per-runtime sensitivity registry (`add_sensitive_tool`,
`register_sensitive_tools`, `remove_sensitive_tool`,
`is_sensitive_tool`, `get_sensitive_tools`) is **unchanged** — it
is a separate documented surface, and no longer has a consumer
inside `execute`. See ADR-061.

### Fixed

- A real gate decision is no longer treated as a transport
  fail-open. The LangChain/LangGraph callback swallowed
  `NullRunBlockedException` alongside transport errors; enforcement
  exceptions are now recorded and re-raised through a
  thread-local deferred handoff to the `@protect` boundary, which
  is the only place the framework lets an exception abort.
- One definition of a synthetic decision. `decorators.py` and
  `runtime.py` each had their own test for "is this source
  synthetic", and the two disagreed on `AUTH_ERROR`; both now call
  `is_fallback_decision_source`.
- A skipped pre-flight gate is countable. `check_control_plane`
  and `check_workflow_budget` still no-op when no workflow can be
  resolved (correct — a never-bound key has no control-plane state
  and no per-workflow budget), but the no-op now increments
  `control_plane_no_workflow_total` /
  `budget_preflight_no_workflow_total` at DEBUG instead of being
  indistinguishable from normal operation. Behaviour is unchanged:
  it still never raises.

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