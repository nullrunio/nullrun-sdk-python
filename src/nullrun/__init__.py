"""
NullRun Platform SDK.

A unified SDK for NullRun AI Agent Safety Layer platform products.

Phase 3.4: the curated public surface is six symbols — see `__all__` below.
Everything else is reachable on demand via `from nullrun import X` for
backward compatibility, but does NOT appear in `dir(nullrun)`. This keeps
the SDK discoverable for the "track AI cost in 5 minutes" use case.

T9 (0.3.0): the legacy Breaker exports (`BreakerError`, `CostLimitExceeded`,
`ApprovalRequired`, `BreakerTimeout`, `Policy`, `FallbackMode`,
`PoolConfig`) were removed from `_LAZY_EXPORTS`. They are still reachable
via the canonical exception names (`NullRunBlockedException`,
`WorkflowPausedException`, etc.) and the canonical policy/transport
modules (`from nullrun.runtime import Policy`,
`from nullrun.transport import FallbackMode, PoolConfig`). The
`NullRunNoop` fallback and the `local_mode` field were also removed
(T3-S2) — see CHANGELOG.

Usage:
    # Initialize at app startup
    import nullrun
    nullrun.init(organization_id="org-123", api_key="your-key")

    # Wrap any function as a gate
    @nullrun.protect
    def my_agent_step():
        return call_llm(...)

    # Manual cost tracking
    nullrun.track_llm(input_tokens=80, output_tokens=20, model="gpt-4o")
    nullrun.track_tool(tool_name="search", duration_ms=150)
    nullrun.track_event({"type": "llm_call", "input_tokens": 80, "output_tokens": 20})
"""

from __future__ import annotations

# Use lazy import inside __getattr__ instead of `import importlib` at
# module top-level — keeps `dir(nullrun)` focused on the curated surface.
from nullrun import __version__

# ---------------------------------------------------------------------------
# Curated public surface (Phase 3.4)
# ---------------------------------------------------------------------------
# These six names are imported eagerly so they show up in `dir(nullrun)` and
# in tab-completion — that's the "track AI cost in 5 minutes" surface. All
# other names (legacy Breaker exports, instrumentation, exceptions, …) live
# in `_LAZY_EXPORTS` below and are loaded on first access via __getattr__.
from nullrun.decorators import protect  # the gate decorator
from nullrun.runtime import track_event, track_llm, track_tool


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
        from nullrun.breaker.exceptions import NullRunAuthenticationError

        raise NullRunAuthenticationError(
            "nullrun.init() requires an api_key. Pass api_key='nr_live_...' "
            "explicitly or set the NULLRUN_API_KEY environment variable. "
            "(Silent no-op fallback was removed in 0.3.0 — see CHANGELOG.)"
        )

    # Imported lazily so we don't pull the runtime into the namespace
    # when the user only wants the static helpers.
    import nullrun.runtime as _rt_mod
    from nullrun.runtime import NullRunRuntime

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
    import nullrun.decorators as _dec_mod
    _dec_mod._runtime = runtime

    # Phase D6: wire auto-instrumentation AFTER the runtime is fully
    # constructed. In 0.3.0 api_key is required, so this branch is
    # unconditional — we always have a remote LLM traffic source if
    # auto-instrumentation libraries are installed.
    from nullrun.instrumentation.auto import auto_instrument
    auto_instrument(runtime)

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
    "patch_openai": ("nullrun.instrumentation", "patch_openai"),
    "unpatch_openai": ("nullrun.instrumentation", "unpatch_openai"),

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

    # Exceptions (Phase 3)
    "NullRunBlockedException": ("nullrun.breaker.exceptions", "NullRunBlockedException"),
    "NullRunAuthenticationError": ("nullrun.breaker.exceptions", "NullRunAuthenticationError"),
    "LoopDetectedException": ("nullrun.breaker.exceptions", "LoopDetectedException"),
    "RetryStormException": ("nullrun.breaker.exceptions", "RetryStormException"),
    "RateLimitExceededException": ("nullrun.breaker.exceptions", "RateLimitExceededException"),
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
    "protect",         # gate decorator
    "track_llm",
    "track_tool",
    "track_event",
]

# Decision History is a backend + dashboard surface only.
# The SDK does not (and cannot) replay LLM calls because NULLRUN does
# not store request/response payloads or hold client LLM keys.

# Phase 0.6: The `nullrun.replay` module was a stub that never matched the real
# backend capability (NULLRUN does not store request bodies, so there is no
# agentic replay to expose from the SDK). The user-facing surface has been
# renamed to Decision History, which lives on the backend and is accessed via
# the dashboard, not from the SDK. The replay module has been removed; do not
# re-export ReplayManager / ReplaySession / ReplayEvent / EventRecorder.
