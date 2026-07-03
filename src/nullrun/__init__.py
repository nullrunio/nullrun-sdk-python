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


def shutdown(timeout: float = 2.0) -> None:
    """Gracefully shut down the NullRun runtime.

    Sends a clean WebSocket close frame, drains in-flight events, and
    stops background threads (HTTP poller, WS push listener). After
    this returns, any further ``nullrun.track(...)`` call or
    ``@protect``-decorated call is a no-op.

    Audit 2026-06-29 (WS graceful close on exit): a long-running
    script that exits via ``sys.exit()`` lets the kernel RST the TCP
    socket, which the backend logs as WARN "Connection reset
    without closing handshake". Calling ``nullrun.shutdown()``
    before exit (or registering it via ``atexit``) eliminates the
    noisy log. No-op if ``init()`` was never called.

    Args:
        timeout: seconds to wait for the WS close handshake to
            complete before giving up. The underlying
            ``NullRunRuntime.shutdown()`` already caps WS join at
            0.5s and the WS close at 2.0s — this parameter is
            reserved for future expansion and is currently unused.

    Example::

        import atexit
        import nullrun
        atexit.register(nullrun.shutdown)
    """
    # Lazy import so the SDK module-import path stays light (mirrors
    # the pattern in `init` and `status`).
    from nullrun.runtime import NullRunRuntime
    runtime = NullRunRuntime._instance  # type: ignore[attr-defined]
    if runtime is None:
        return
    runtime.shutdown()


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

    logger = logging.getLogger("nullrun")

    if debug:
        logger.setLevel(logging.DEBUG)

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

    # C3 fix: shut down any existing runtime before constructing a new
    # one. Without this, calling init() twice (or init() after a
    # previous init() without an explicit shutdown()) leaves the prior
    # daemon threads — transport flush, WS control plane, coverage
    # reporter — running against the orphaned runtime. They keep
    # burning CPU, hold sockets open, and can write to stale module
    # slots that no longer reflect the active singleton.
    #
    # shutdown() is best-effort: if the previous runtime is mid-shutdown
    # or in an unrecoverable state, we log and proceed so the new
    # runtime can still come up.
    with _init_lock:
        existing = NullRunRuntime._instance
        if existing is not None:
            logger.warning(
                "nullrun.init() called while a previous runtime is "
                "still alive; shutting down the old one to avoid "
                "orphan threads (C3 fix)."
            )
            try:
                existing.shutdown()
            except Exception as e:  # noqa: BLE001 — best-effort
                logger.warning("previous runtime shutdown raised during init(): %s", e)

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

    # v3.12 / 0.12.0 — server-minted execution_id default ON. Probe
    # the backend's /health endpoint and log any version mismatch
    # so the operator sees the gap at startup rather than on the
    # first failed /check. We do NOT fail init() — the gate still
    # rejects with 400 PROTOCOL_TOO_OLD, and the SDK's role is
    # advisory here.
    try:
        from nullrun.__version__ import __version__
        from nullrun.capabilities import (
            probe_capabilities,
            validate_sdk_version,
        )

        caps = probe_capabilities(runtime.api_url)
        if caps is not None:
            warnings = validate_sdk_version(__version__, caps)
            for w in warnings:
                logger.warning("nullrun.init: %s", w)
        else:
            # /health unreachable — most likely the operator
            # hasn't pointed the SDK at the right host. We don't
            # fail init() (the user might intentionally init()
            # before network is ready) but we log at INFO so the
            # operator sees it.
            logger.info(
                "nullrun.init: could not probe %s/health — "
                "v3 capability negotiation skipped",
                runtime.api_url,
            )
    except Exception as e:  # noqa: BLE001 — best-effort probe
        logger.debug("nullrun.init: capability probe raised %s", e)

    # Phase D6: wire auto-instrumentation AFTER the runtime is fully
    # constructed. In 0.3.0 api_key is required, so this branch is
    # unconditional — we always have a remote LLM traffic source if
    # auto-instrumentation libraries are installed.
    from nullrun.instrumentation.auto import auto_instrument

    auto_instrument(runtime)

    # 0.9.0: coverage reporter removed. Coverage is now derived
    # server-side from llm_call span metadata (host + tracked +
    # streaming_skipped flags). No 60s daemon thread, no per-process
    # counter dicts.

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
    # T4 (2026-06-27): per-call context for /gate pre-flight. Users
    # call `set_call_context(model=..., tools=[...])` inside
    # `with workflow(...)` so the backend's budget + tool_block
    # enforcement sees real values instead of the previous fake
    # `"budget-precheck"` sentinel and empty tool list.
    "set_call_context": ("nullrun.context", "set_call_context"),
    "get_call_model": ("nullrun.context", "get_call_model"),
    "get_call_tools": ("nullrun.context", "get_call_tools"),
    # 2026-07-02 (v0.11.0): chain context for soft-mode budget gate
    # (CLAUDE.md §5, §6, §16). ``chain`` is the contextmanager,
    # ``get_chain_id`` / ``set_chain_id`` are the manual setters.
    "chain": ("nullrun.context", "chain"),
    "get_chain_id": ("nullrun.context", "get_chain_id"),
    "set_chain_id": ("nullrun.context", "set_chain_id"),
    "get_chain_op": ("nullrun.context", "get_chain_op"),
    "set_chain_op": ("nullrun.context", "set_chain_op"),
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
    # User-facing message catalog (NULLRUN owns the wording; see
    # nullrun/messages.py for the design rationale). Eager in
    # spirit — these are the "give the user a chance" surface that
    # makes an SDK exception show up as a clean string instead of
    # raw internal text.
    "format_user_message": ("nullrun.messages", "format_user_message"),
    "set_user_message": ("nullrun.messages", "set_user_message"),
    "get_user_message": ("nullrun.messages", "get_user_message"),
    # Minimal-boilerplate error handling for scripts (see
    # nullrun/_handle.py for the rationale). Pair with @nullrun.protect
    # so a typical ``run an agent and print a friendly message on
    # failure`` script needs no explicit try/except around
    # NullRunError. WorkflowKilledInterrupt (BaseException) still
    # propagates — kill is never swallowed.
    #
    # The module is named ``_handle.py`` (private, leading underscore)
    # so it does not collide with the public ``nullrun.handle``
    # context manager. With a non-underscored name, pytest's test
    # discovery would pre-import ``nullrun.handle`` as a submodule,
    # which shadows the lazy export and breaks ``from nullrun import
    # handle``.
    "handle": ("nullrun._handle", "handle"),
    "guarded": ("nullrun._handle", "guarded"),
    "init_or_die": ("nullrun._handle", "init_or_die"),
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
    # Audit 2026-06-29 (WS graceful close on exit): the user-facing
    # top-level ``shutdown()`` sends a clean WS close frame and
    # drains in-flight events. Without it, a long-running script
    # that exits via ``sys.exit()`` lets the kernel RST the TCP
    # socket → backend logs WARN "Connection reset without closing
    # handshake". Calling ``nullrun.shutdown()`` before
    # ``sys.exit(0)`` (or in an ``atexit`` handler) eliminates the
    # noisy log. No-op if init() was never called.
    "shutdown",
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
    # User-facing message catalog — the single entry point for
    # turning an SDK exception into a string safe to display to
    # end users. ``set_user_message`` lets a deployment brand its
    # own wording per error_code without rewriting the SDK.
    "format_user_message",
    "set_user_message",
    # Minimal-boilerplate error handling for scripts. ``handle`` is
    # the context manager (``with nullrun.handle():``), ``guarded``
    # is the decorator (``@nullrun.guarded``). Both translate any
    # ``NullRunError`` into ``print(format_user_message(exc))`` +
    # ``sys.exit(1)``; ``WorkflowKilledInterrupt`` propagates.
    # ``init_or_die`` is the convenience wrapper around ``init``
    # that catches NR-C001 "no api_key" at startup and exits
    # cleanly — without it the user sees a raw traceback before
    # any ``with handle():`` block is in scope.
    "handle",
    "guarded",
    "init_or_die",
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
