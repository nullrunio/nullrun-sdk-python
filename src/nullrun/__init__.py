"""
NullRun Platform SDK.

Enforcement gateway client for AI agents. Curated 6-symbol surface:
`init`, `protect`, `track_llm`, `track_tool`, `track_event`. Everything
else is reachable on demand via `from nullrun import X` but does NOT
appear in `dir(nullrun)`.

Usage:
    import nullrun
    nullrun.init(api_key="nr_live_...")

    @nullrun.protect
    def my_agent(query):
        return call_llm(query)

See README.md for LangGraph, OpenAI Agents, llama-index, crewai, autogen
auto-instrumentation; CHANGELOG.md for breaking changes between versions.
"""

from __future__ import annotations

import threading as _threading

# Use lazy import inside __getattr__ instead of `import importlib` at
# module top-level — keeps `dir(nullrun)` focused on the curated surface.
from nullrun.__version__ import __version__

# Module-level lock that serialises the three singleton-slot writes
# inside `init()`. See plan item B3.
_init_lock = _threading.Lock()

# ---------------------------------------------------------------------------
# Curated public surface (Phase 3.4)
# ---------------------------------------------------------------------------
# These six names are imported eagerly so they show up in `dir(nullrun)` and
# in tab-completion — that's the "track AI cost in 5 minutes" surface. All
# other names (legacy Breaker exports, instrumentation, exceptions, …) live
# in `_LAZY_EXPORTS` below and are loaded on first access via __getattr__.
from nullrun.decorators import protect  # the gate decorator
from nullrun.runtime import track_event, track_llm, track_tool


def status():
    """Return the current runtime state as a Layer-3
    :class:`NullRunStatus` snapshot.

    Synchronous, thread-safe, side-effect-free — safe to call
    from the agent loop, the transport flush thread, or a
    debug console. The returned dataclass is frozen so it can
    be cached, shared, and compared with ``==``.

    Designed for the "the agent is stuck, what's wrong?"
    runbook:

        >>> import nullrun
        >>> print(nullrun.status().summary())
        NullRunStatus(degraded fallback=last_good@42s reason=last policy fetch failed at 2026-06-24T10:30:15+00:00)

    See ``nullrun.observability.status`` for the state
    derivation rules (the four headline states:
    ``ok`` / ``degraded`` / ``offline`` / ``misconfigured``).

    Raises:
        NullRunConfigError: ``nullrun.init()`` has not been
            called yet, or the runtime was shut down. The
            snapshot only makes sense when there is a runtime
            to snapshot.
    """
    # Read the module-level ``_runtime`` directly so we do NOT
    # trigger ``get_instance()``'s lazy construction. ``status()``
    # must NEVER create a runtime as a side effect — a fresh
    # import of ``nullrun`` followed by ``nullrun.status()``
    # should report "no runtime" cleanly, not try to spin one
    # up (which would itself raise a different config error
    # about missing api_key).
    import nullrun.runtime as _rt_mod
    from nullrun.breaker.exceptions import NullRunConfigError

    rt = _rt_mod._runtime
    if rt is None:
        raise NullRunConfigError(
            "nullrun.status() requires a runtime. Call nullrun.init() first.",
            error_code="NR-C004",
            user_action=(
                "Call nullrun.init(api_key='nr_live_...') before "
                "calling nullrun.status(). The snapshot only makes "
                "sense when there is a runtime to inspect."
            ),
        )
    return rt.status()


def on_error(hook):
    """Register a global error hook. Layer 2 of the "give the user
    a chance" design.

    The hook is called for every structured SDK failure (every
    subclass of :class:`NullRunError`) BEFORE the exception
    propagates. The hook sees the same exception the caller will
    catch plus an :class:`ErrorContext` describing where the
    error fired. Multiple hooks are supported; they fire in
    registration order. Hook exceptions are caught and logged
    at DEBUG — a misbehaving hook does not break the SDK.

    What does NOT fire the hook:

    * :class:`WorkflowKilledInterrupt` (BaseException subclass)
      — kill is a non-recoverable signal, not an error.
    * Non-``NullRunError`` exceptions (e.g. raw ``httpx`` errors
      from SDK-internal code paths not yet migrated to the
      structured hierarchy).

    Args:
        hook: Callable ``(err: NullRunError, ctx: ErrorContext) -> None``.
            Must be synchronous.

    Returns:
        Callable ``() -> None`` that unregisters the hook.
        Idempotent — safe to call twice.

    Example::

        import nullrun
        from nullrun.breaker.exceptions import NullRunError

        def my_handler(err, ctx):
            log.warning(
                "NullRun error",
                extra={
                    "code": err.error_code,
                    "stage": ctx.stage,
                    "retryable": err.retryable,
                    "user_action": err.user_action,
                    "workflow_id": ctx.workflow_id,
                },
            )

        unregister = nullrun.on_error(my_handler)
        # ... later, in shutdown:
        unregister()
    """
    # Lazy import — keeps ``import nullrun`` cheap and avoids
    # pulling the observability module into the top-level
    # namespace when the user only wants the static helpers.
    from nullrun.observability.error_hooks import register_hook

    return register_hook(hook)


def init(
    api_key: str | None = None,
    api_url: str | None = None,
    debug: bool = False,
):
    """
    Initialize the NullRun SDK. Call once at application startup.

    `api_key` is **required** as of 0.3.0. The previous silent fallback to
    "local mode" (a NullRunNoop stub) was removed because it hid policy
    violations and bypassed every backend gate — a real safety hole. Pass
    `api_key=...` explicitly or set the `NULLRUN_API_KEY` environment
    variable before calling `init()`. If neither is set, `init()` raises
    `NullRunAuthenticationError`.

    Args:
        api_key:  NullRun API key (or NULLRUN_API_KEY env var). Required.
        api_url:  Gateway URL (or NULLRUN_API_URL env var)
        debug:    Enable debug logging

    Note: the background control-plane listener (WebSocket + HTTP poll) is
    always started on `init()`. To disable it, construct `NullRunRuntime`
    directly with `polling=False` — this is an internal/test-only knob.

    Returns:
        NullRunRuntime singleton instance.

    Raises:
        NullRunAuthenticationError: if neither `api_key` nor
            `NULLRUN_API_KEY` is set.

    Example:
        import nullrun

        nullrun.init(api_key="your-key")

        @nullrun.protect
        def my_agent():
            return agent.run()
    """
    import logging
    import os

    if debug:
        logging.getLogger("nullrun").setLevel(logging.DEBUG)

    # T3-S2 (0.3.0): api_key is now required. Previous versions fell back
    # to a NullRunNoop stub in `local_mode`, which silently bypassed every
    # backend gate (budget, policy, control plane). That was a real
    # safety hole — production callers were unaware their policies were
    # not being enforced. We raise instead so the misconfiguration is
    # caught at startup rather than producing silent allow-all decisions.
    resolved_key = api_key or os.getenv("NULLRUN_API_KEY")
    if not resolved_key:
        # Layer 1: raise the legacy type (``NullRunAuthenticationError``)
        # so user code with ``except NullRunAuthenticationError:`` still
        # catches this case, but stamp the structured ``error_code`` /
        # ``user_action`` so a Layer-2 on_error hook (or a
        # ``except NullRunError:`` clause) can branch on the catalog
        # value ``NR-C001`` ("configuration: no api_key") without
        # parsing the message.
        from nullrun.breaker.exceptions import NullRunAuthenticationError

        err = NullRunAuthenticationError(
            "nullrun.init() requires an api_key. Pass api_key='nr_live_...' "
            "explicitly or set the NULLRUN_API_KEY environment variable. "
            "(Silent no-op fallback was removed in 0.3.0 — see CHANGELOG.)",
            error_code="NR-C001",
            user_action=(
                "Get an API key at https://app.nullrun.io/settings/api-keys, "
                "then either pass api_key='nr_live_...' to nullrun.init() or "
                "set the NULLRUN_API_KEY environment variable. The SDK cannot "
                "operate without credentials — the silent no-op fallback was "
                "removed in 0.3.0 because it bypassed every backend gate."
            ),
        )
        # Layer 2: fire the on_error hook BEFORE the raise so the
        # hook sees the call stack still live. Stage = "init" so a
        # log-based hook can attribute the failure to startup
        # (e.g. "app crashed before any user code ran"). We skip
        # the build cost when no hook is registered — see
        # ``has_hooks()`` in observability/error_hooks.py.
        from nullrun.observability.error_hooks import ErrorContext, emit_error, has_hooks

        if has_hooks():
            emit_error(
                err,
                ErrorContext(stage="init", api_key_prefix=None),
            )
        raise err

    # Imported lazily so we don't pull the runtime into the namespace
    # when the user only wants the static helpers.
    import threading as _threading

    import nullrun.decorators as _dec_mod
    import nullrun.runtime as _rt_mod
    from nullrun.runtime import NullRunRuntime

    # Phase 0.3.1: the three singleton slots (NullRunRuntime._instance,
    # _rt_mod._runtime, _dec_mod._runtime) must all be assigned
    # atomically. Without a lock, concurrent init() calls from
    # multiple threads can leave the three slots pointing at two
    # different runtimes. The failure mode is silent — the
    # decorator's @protect wrapper reads _dec._runtime once and
    # never re-resolves, so a missed assignment drops every
    # span_start/span_end event for that runtime.
    with _init_lock:
        runtime = NullRunRuntime(
            api_key=api_key,
            api_url=api_url,
            debug=debug,
        )

        # Register as the module-level singleton so `nullrun.track_llm` /
        # `nullrun.track_tool` (which resolve via `get_runtime()`) and any
        # other consumers reading the cached instance find *this* runtime —
        # not whatever a previous test or stale env would otherwise produce.
        _rt_mod._runtime = runtime
        NullRunRuntime._instance = runtime

        # Wire the @protect decorator's own module-level cache to this
        # runtime too. The decorator short-circuits on its local `_runtime`
        # slot and never re-resolves via `get_instance()`, so without this
        # assignment a re-init cycle (init → shutdown → init) leaves the
        # decorator pointing at the dead previous runtime and silently
        # drops span_start/span_end events.
        _dec_mod._runtime = runtime

    # Phase D6: wire auto-instrumentation AFTER the runtime is fully
    # constructed. In 0.3.0 api_key is required, so this branch is
    # unconditional — we always have a remote LLM traffic source if
    # auto-instrumentation libraries are installed.
    from nullrun.instrumentation.auto import auto_instrument

    auto_instrument(runtime)

    # Start the coverage reporter so the backend gets a coverage_report
    # event every 60s. Daemon thread; safe to leak across re-init.
    # The coverage reporter is a no-op when no LLM traffic has been
    # observed (see ``track_coverage``).
    runtime.start_coverage_reporter()

    return runtime


# ---------------------------------------------------------------------------
# Lazy exports (PEP 562) — backward compat without bloating dir()
# ---------------------------------------------------------------------------
# Each entry maps an attribute name on `nullrun` to (module_path, attr_name)
# inside that module. They are loaded on first attribute access and cached
# in `globals()` so subsequent lookups are O(1) and not visible in
# `vars(nullrun)` until then. This is the same pattern used by pandas /
# sqlalchemy / etc. to keep the top-level namespace discoverable.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    # Runtime + context (advanced)
    "NullRunRuntime": ("nullrun.runtime", "NullRunRuntime"),
    "get_runtime": ("nullrun.runtime", "get_runtime"),
    "get_protected_runtime": ("nullrun.decorators", "get_protected_runtime"),
    "track": ("nullrun.runtime", "track"),
    "reset": ("nullrun.decorators", "reset"),
    "workflow": ("nullrun.context", "workflow"),
    "span": ("nullrun.context", "span"),
    "agent": ("nullrun.context", "agent"),
    "get_workflow_id": ("nullrun.context", "get_workflow_id"),
    "get_trace_id": ("nullrun.context", "get_trace_id"),
    "get_span_id": ("nullrun.context", "get_span_id"),
    "get_agent_id": ("nullrun.context", "get_agent_id"),
    # Instrumentation
    "NullRunCallback": ("nullrun.instrumentation", "NullRunCallback"),
    # NOTE (Sprint 1.2 / B11-B12): `patch_openai` and `unpatch_openai`
    # were removed from `_LAZY_EXPORTS` because they pointed at
    # non-existent attributes on `nullrun.instrumentation` (the actual
    # function is `patch_openai_agents`, with different semantics —
    # it patches `agents.Runner`, not the `openai` SDK). The pre-fix
    # lazy entries caused `AttributeError` on first access, which is
    # a worse failure mode than a clean `ImportError` from
    # `from nullrun import patch_openai` failing because the symbol
    # is no longer in the lazy table.
    # Toolbox — framework-specific wrappers (Phase 1 Commit 6).
    # The previous `instrument()` helper lived at
    # `nullrun.instrumentation.langgraph.instrument`; it is now
    # `nullrun.toolbox.langgraph.wrapper`. Reachable as
    # `from nullrun import wrapper` for one-line import.
    "wrapper": ("nullrun.toolbox.langgraph", "wrapper"),
    # Span / trace context (Phase 2 Commit 3).
    # `tracing.py` is the structured replacement for the loose `_trace_id`
    # / `_span_id` contextvars in `nullrun.context`. `SpanContext` is a
    # single value (parent + children derive from it); `set_span` /
    # `reset_span` are the token-based API the runtime and `@protect`
    # use to push/pop the active span.
    "SpanContext": ("nullrun.tracing", "SpanContext"),
    "get_current_span": ("nullrun.tracing", "get_current_span"),
    "create_root_span": ("nullrun.tracing", "create_root_span"),
    "create_child_span": ("nullrun.tracing", "create_child_span"),
    "set_span": ("nullrun.tracing", "set_span"),
    "reset_span": ("nullrun.tracing", "reset_span"),
    # Decorators
    "sensitive": ("nullrun.decorators", "sensitive"),
    # Actions (Phase 3)
    "ActionHandler": ("nullrun.actions", "ActionHandler"),
    "ActionType": ("nullrun.actions", "ActionType"),
    "ActionEvent": ("nullrun.actions", "ActionEvent"),
    "WebhookConfig": ("nullrun.actions", "WebhookConfig"),
    "handle_action": ("nullrun.actions", "handle_action"),
    "register_action_handler": ("nullrun.actions", "register_action_handler"),
    "get_action_handler": ("nullrun.actions", "get_action_handler"),
    # Exceptions (Phase 3 + Layer 1)
    "NullRunError": ("nullrun.breaker.exceptions", "NullRunError"),
    "NullRunBlockedException": ("nullrun.breaker.exceptions", "NullRunBlockedException"),
    "NullRunAuthenticationError": ("nullrun.breaker.exceptions", "NullRunAuthenticationError"),
    "NullRunAuthError": ("nullrun.breaker.exceptions", "NullRunAuthError"),
    "NullRunConfigError": ("nullrun.breaker.exceptions", "NullRunConfigError"),
    "NullRunBackendError": ("nullrun.breaker.exceptions", "NullRunBackendError"),
    "NullRunBudgetError": ("nullrun.breaker.exceptions", "NullRunBudgetError"),
    "NullRunToolBlockedError": ("nullrun.breaker.exceptions", "NullRunToolBlockedError"),
    # Layer 2: on_error context type
    "ErrorContext": ("nullrun.observability.error_hooks", "ErrorContext"),
    # Layer 3: status dataclasses
    "NullRunStatus": ("nullrun.observability.status", "NullRunStatus"),
    "RecentError": ("nullrun.observability.status", "RecentError"),
    "WorkflowState": ("nullrun.observability.status", "WorkflowState"),
    # Sprint 2.2: zombie exception classes removed. See the
    # NOTE block in breaker/exceptions.py for the list.
    "WorkflowPausedException": ("nullrun.breaker.exceptions", "WorkflowPausedException"),
    "WorkflowKilledException": ("nullrun.breaker.exceptions", "WorkflowKilledException"),
    "WorkflowKilledInterrupt": ("nullrun.breaker.exceptions", "WorkflowKilledInterrupt"),
}


def __getattr__(name: str):
    """PEP 562 — lazy attribute access for backward-compatible symbols."""
    if name in _LAZY_EXPORTS:
        module_path, attr_name = _LAZY_EXPORTS[name]
        module = __import__(module_path, fromlist=[attr_name])
        value = getattr(module, attr_name)
        # Cache on the module so subsequent lookups are O(1) and
        # dir(nullrun) still reports the curated public surface until
        # the legacy name is actually accessed.
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """PEP 562 — `dir(nullrun)` only shows the curated public surface.

    We deliberately ignore `globals()` here so that auto-imported
    submodules (`nullrun.decorators`, `nullrun.runtime`, etc.) and any
    side-effect imports do NOT leak into the public namespace. Users
    who want internals can still reach them via `from nullrun import X`
    (see `_LAZY_EXPORTS` in `__getattr__`) — `dir()` is for discovery,
    not for reachability.
    """
    return sorted(__all__)


__all__ = [
    # Version (single value, always public)
    "__version__",
    # Phase 3.4: the curated public surface — six symbols.
    # Everything else stays importable as `from nullrun import X` for
    # backward compatibility, but does NOT appear in `dir(nullrun)`
    # until the user actually accesses it.
    "init",
    "protect",  # gate decorator
    "track_llm",
    "track_tool",
    "track_event",
    # Layer 2: global on_error hook. Eager because it is the
    # single most important "give the user a chance" API — the
    # user has to know it exists to call it.
    "on_error",
    # Layer 3: status introspection — synchronous snapshot of the
    # runtime's state, returns a frozen NullRunStatus.
    "status",
    # Layer 1: structured exception base + the most common subclasses
    # the user is expected to ``except`` on. Including them in
    # ``__all__`` means ``from nullrun import *`` and ``dir(nullrun)``
    # surface them for tab-completion — the whole point of giving
    # the user "a chance" is that they need to know the names exist
    # to catch them. The legacy types (``NullRunBlockedException``,
    # ``NullRunAuthenticationError``, ``WorkflowKilledException``,
    # ``WorkflowPausedException``) stay importable via
    # ``_LAZY_EXPORTS`` for back-compat — adding them here would
    # change ``dir(nullrun)`` for existing users.
    "NullRunError",
    "NullRunAuthError",
    "NullRunConfigError",
    "NullRunBackendError",
    "NullRunBudgetError",
    "NullRunToolBlockedError",
    "WorkflowKilledInterrupt",
]

# Sprint 2.1: the SDK-side ``decision_history`` module was deleted.
# Decision history is a backend + dashboard surface only — the SDK
# does not (and cannot) replay LLM calls because NULLRUN does not
# store request/response payloads or hold client LLM keys. The
# orphan ``start_recording`` / ``stop_recording`` methods on
# ``NullRunRuntime`` are kept as no-op stubs for one minor version
# for backward compatibility; they will be removed in 0.5.0.
# Do NOT re-export ReplayManager / ReplaySession / ReplayEvent /
# EventRecorder.
