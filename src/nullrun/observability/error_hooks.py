"""Layer 2 of the "give the user a chance" design — the global
``nullrun.on_error()`` hook.

Pre-Layer-2: the only signal the user got was the raised exception
itself, with no global observability hook. To get metrics / Sentry
wiring / a per-error toast UI, the user had to wrap every call site
in ``try / except NullRunError`` — a leaky pattern that breaks down
the moment a new code path is added.

Post-Layer-2: every structured SDK failure fires every registered
hook BEFORE the exception propagates. The hook sees the same
``NullRunError`` and an ``ErrorContext`` describing where in the
lifecycle the error happened. Multiple hooks are supported. Hook
exceptions are caught and logged at DEBUG (per design discussion
2026-06-24 — visible when DEBUG logging is on, silent at
INFO/CRITICAL so a misbehaving hook does not break production).

What does NOT fire the hook:

* ``WorkflowKilledInterrupt`` (BaseException subclass) — kill is
  a non-recoverable signal, not an error. Catching kill in a
  global error hook would mask the intent of
  ``except WorkflowKilledInterrupt`` / ``except BaseException``
  blocks at the top of the agent loop. See
  ``docs/kill-contract.md`` §6.
* Any non-``NullRunError`` exception raised inside the SDK (e.g.
  ``httpx.ConnectError`` propagated from a code path that has
  not yet been migrated to structured errors). These are bugs
  in the SDK, not user-facing failures.
* Re-raises inside the ``except`` block (i.e. the hook fires
  exactly once per error, even if the error is caught and
  re-raised).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Stage identifiers — short strings so Sentry tags / log filters
# do not get overwhelmed. Adding a new value? Add it to the
# STAGES docstring below so the catalogue stays discoverable.
#
#   init           — nullrun.init() failed (missing api_key, etc.)
#   auth           — _authenticate() against /auth/verify
#   policy_fetch   — GET /api/v1/orgs/{org}/policies
#   execute        — POST /api/v1/execute (gate decision)
#   track          — POST /api/v1/track (event ingest)
#   gate           — POST /api/v1/gate (legacy pre-flight)
#   check          — POST /api/v1/check (budget pre-flight)
#   sensitive_tool — @sensitive pre-check
#   org_status     — get_org_status()
#   ws             — WebSocket control-plane message handling
#   transport      — generic transport-layer raise
STAGES: tuple[str, ...] = (
    "init",
    "auth",
    "policy_fetch",
    "execute",
    "track",
    "gate",
    "check",
    "sensitive_tool",
    "org_status",
    "ws",
    "transport",
)


@dataclass
class ErrorContext:
    """Where the error happened, who hit it, and when.

    Fields are best-effort — a hook may receive a context with
    ``workflow_id=None`` if the error fired before the runtime
    was bound to a workflow (e.g. ``init`` failures). The hook
    MUST tolerate missing fields.
    """

    #: Short stage identifier — see STAGES above.
    stage: str

    #: Workflow that was active when the error fired, or ``None``
    #: for pre-bind errors (init, policy_fetch) and SDK-internal
    #: errors (transport).
    workflow_id: str | None = None

    #: Tool that triggered the error, or ``None`` for non-tool
    #: errors. Set on @sensitive / @protect / track_tool raises.
    tool_name: str | None = None

    #: First 10 characters of the api key in use, or ``None`` if
    #: no key was set yet. Used for log triage — the full key
    #: never leaves the SDK.
    api_key_prefix: str | None = None

    #: Backend correlation id (``X-Correlation-Id`` response
    #: header) when the error came from the backend. ``None``
    #: for pre-bind errors and locally-detected blocks (loop /
    #: rate). Set by the transport layer when the header is
    #: present on a 4xx / 5xx response.
    correlation_id: str | None = None

    #: Free-form dict for stage-specific metadata (e.g.
    #: ``{"status_code": 503}`` for a 5xx). Kept as a dict
    #: (not a TypedDict) so future fields can be added without
    #: a schema migration.
    extra: dict[str, Any] = field(default_factory=dict)

    #: Wall-clock seconds since the epoch (UTC). Useful for
    #: correlating hook events with the SDK's own logging.
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        # Validate stage against the catalogue. Unknown stages
        # are still accepted (callers may invent new ones), but
        # a warning is emitted at DEBUG so the next refactor can
        # extend the STAGES tuple.
        if self.stage not in STAGES:
            logger.debug(
                "ErrorContext.stage=%r is not in the STAGES catalogue; "
                "consider adding it (see error_hooks.STAGES).",
                self.stage,
            )


# The callback type. Sync only — Layer 2 design discussion
# 2026-06-24: async hooks in except blocks are awkward (no
# running event loop to await on), and the SDK surface is
# already sync. Revisit if/when a real async use case appears.
ErrorHook = Callable[["Any", ErrorContext], None]


# Module-level registry. Thread-safe — hooks may be registered
# from one thread and fired from another (e.g. register at app
# startup, fire from a transport background thread).
_lock = threading.RLock()
_hooks: list[ErrorHook] = []


def register_hook(hook: ErrorHook) -> Callable[[], None]:
    """Register an error hook. Returns an unregister function.

    Multiple hooks are supported; they fire in registration
    order. The unregister function is idempotent — calling it
    twice is a no-op.

    Example::

        def my_hook(err, ctx):
            log.error("NullRun %s at %s", err.error_code, ctx.stage)
        unregister = nullrun.on_error(my_hook)
        # ... later:
        unregister()
    """
    if not callable(hook):
        raise TypeError(f"on_error hook must be callable, got {type(hook).__name__}")
    with _lock:
        _hooks.append(hook)

    def unregister() -> None:
        with _lock:
            try:
                _hooks.remove(hook)
            except ValueError:
                # Already unregistered — idempotent.
                pass

    return unregister


def clear_hooks() -> None:
    """Remove every registered hook. Intended for test isolation.

    Production code should NOT call this — use the unregister
    function returned by ``register_hook`` instead.
    """
    with _lock:
        _hooks.clear()


def emit_error(err: Any, ctx: ErrorContext) -> None:
    """Fire every registered hook with the given error and context.

    Called from raise sites in the SDK immediately BEFORE the
    ``raise`` statement, so the hook sees the fully-constructed
    exception while the call stack is still live (design
    decision C, 2026-06-24).

    Hook exceptions are caught and logged at DEBUG (per design
    decision 2026-06-24: silent at INFO/CRITICAL so a
    misbehaving hook does not break production, visible when
    DEBUG logging is on so debugging the hook itself is easy).

    Snapshot the hook list under the lock so a concurrent
    unregister during dispatch does not mutate the iteration.
    """
    with _lock:
        snapshot = list(_hooks)
    if not snapshot:
        # Hot path: most raises happen without a hook registered.
        # Skip the loop entirely so we add zero overhead.
        return
    for hook in snapshot:
        try:
            hook(err, ctx)
        except Exception as exc:  # noqa: BLE001
            # ``logger.debug(..., exc_info=True)`` is the cheapest
            # way to surface the traceback in the user's DEBUG
            # log without emitting anything at INFO/CRITICAL.
            # ``exc_info=True`` attaches the full traceback; if
            # the user only sees the message, they can flip on
            # DEBUG and re-run.
            logger.debug(
                "on_error hook raised (swallowed): %s",
                exc,
                exc_info=True,
            )


def has_hooks() -> bool:
    """True if at least one hook is registered.

    Used by hot-path callers that want to avoid building an
    ``ErrorContext`` when there is no hook to receive it. Most
    raise sites skip this check (the cost of building the
    context is small), but the SDK init path uses it because
    the context for an ``init`` failure is large.
    """
    with _lock:
        return bool(_hooks)
