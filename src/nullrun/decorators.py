"""
Decorators for the NullRun SDK.

Public surface: `protect` is the only gate decorator. It takes NO
parameters — span hierarchy is built automatically from the caller's
context via contextvars, and the workflow is derived from the API key
on the backend (the dashboard surfaces the agent's name from the
key's `name` field).

Usage:
    # Basic — auto-init from env, auto-build span tree
    import nullrun
    nullrun.init(api_key="...")

    @nullrun.protect
    def my_agent(query: str) -> str:
        return call_llm(query)

    @nullrun.protect
    async def my_async_agent(query: str) -> str:
        return await call_llm_async(query)

    # Manual: protected functions compose into a tree automatically
    @nullrun.protect
    def orchestrator(q):
        return researcher(q) # researcher is a child span

    @nullrun.protect
    def researcher(q):
        return get_current_span # parent's span_id == its parent_span_id

`reset` and `get_protected_runtime` are the runtime-lifecycle helpers.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import logging
import os
import threading
import warnings
from collections.abc import Callable
from contextvars import Token
from typing import Any, TypeVar

from nullrun._registry import get_active_runtime
from nullrun.breaker.exceptions import (
    NullRunBlockedException,
    WorkflowKilledInterrupt,
    WorkflowPausedException,
)
from nullrun.business_impact import BusinessImpact, compute_action_digest
from nullrun.context import (
    _call_tools_var,
    get_call_tools,
    get_server_minted_execution_id,  # for cancel-on-exception helper
    get_workflow_id,
    reset_span_id,
    reset_trace_id,
    set_span_id,
    set_trace_id,
)
from nullrun.runtime import NullRunRuntime, get_runtime

# Sentinel used when a gate fires outside a workflow context.
UNKNOWN_WORKFLOW_ID = "__nullrun_unknown__"

from nullrun.tracing import (
    SpanContext,
    create_child_span,
    create_root_span,
    get_current_span,
    reset_span,
    set_span,
)

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# Expanded sensitive-arg keys. The original 7-key set missed
SENSITIVE_ARG_KEYS = frozenset(
    {
        # Credentials / secrets
        "password",
        "passwd",
        "pwd",
        "token",
        "secret",
        "api_key",
        "apikey",
        "key",
        "auth",
        "authorization",
        "bearer",
        "session",
        "session_id",
        "cookie",
        "access_token",
        "refresh_token",
        "id_token",
        "private_key",
        "secret_key",
        # PII
        "email",
        "phone",
        "ssn",
        "credit_card",
        "credit_card_number",
        "cvv",
        "cvc",
        "pin",
        "otp",
        "mfa",
    }
)


def _safe_repr(value: object, max_len: int = 50) -> str:
    """Safe representation of an argument for logging.

    P0-6: redaction happens BEFORE truncation, not after.
    Pre-fix the order was truncate-then-redact: ``_safe_repr`` cut the
    repr to 50 chars first, and ``_strip_details_balanced`` then tried
    to find ``details={...}`` in that 50-char slice. If ``details=``
    lived past position 50 (a common case — repr of an HTTPError
    with a long URL places the dict payload well into the string), the
    substring was gone, the redact pass saw nothing, and the raw
    ``details={...}`` payload leaked into the audit log.

    Post-fix the order is redact-then-truncate: call
    ``_strip_details_balanced`` first (which works on the full repr)
    then truncate. The cost is a single string scan over ``len(repr)``
    instead of ``len(repr[:50])`` — irrelevant for the 200-byte
    strings we actually pass through this code path.

    P3-3: also consolidates the two-pass flow that
    calls — there are now two callers that compose them, and the
    invariant ``redact BEFORE truncate`` was being maintained by
    convention only. ``_safe_repr`` is now the single source of truth.
    """
    r = repr(value)
    # Redact ``details={...}`` substrings on the FULL repr.
    # Cheap (single linear scan over the string), and ensures the
    # ``details=`` substring is replaced before we potentially
    # truncate it away.
    r = _strip_details_balanced(r)
    # Truncate to ``max_len`` so a giant repr doesn't bloat span
    # events. We append ``...<truncated>`` so consumers can see the
    # cut happened.
    if len(r) > max_len:
        return r[:max_len] + "...<truncated>"
    return r


def _safe_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Mask sensitive kwargs (case-insensitive)."""
    return {
        k: "***" if k.lower() in SENSITIVE_ARG_KEYS else _safe_repr(v) for k, v in kwargs.items()
    }


def _safe_args(fn: Callable[..., Any], args: tuple[Any, ...]) -> list[Any]:
    """Mask sensitive positional args (P0-1, plan).

    Pre-fix only kwargs were masked via SENSITIVE_ARG_KEYS. A
    ``def charge(card_number, amount)`` with positional call
    ``charge("4111-1111-1111-1111", 50)`` would leak the PAN into the
    audit log. We now introspect ``fn``'s signature, bind the positional
    args to parameter names, and apply the same ``SENSITIVE_ARG_KEYS``
    mask that kwargs already use.

    Extra positional args (``*args``) have no parameter name to key on —
    we still redact them with ``_safe_repr`` so we don't ship a full
    repr of an arbitrary object to the audit log, but we cannot tell
    them apart from benign primitives. This is the same posture as the
    kwargs branch (apply mask by name; otherwise best-effort repr).
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        # C-extension / built-in without a signature — fall back to
        # safe repr for every arg so we still don't leak raw
        # repr(value) of an arbitrary object.
        return [_safe_repr(a) for a in args]

    # `bound_params` is sliced to at most `len(args)`, so when the
    # function has FEWER positional parameters than args provided
    # (e.g. `*args`-style callables), `bound_params` is shorter
    # than `args` and the trailing loop below handles the excess.
    # We use `strict=False` to make that tolerance explicit and
    # satisfy B905; without it the two iterables must be exactly
    # the same length, which they are not in the *args case.
    bound_params = list(sig.parameters.items())[: len(args)]
    masked: list[Any] = []
    for (pname, _param), value in zip(bound_params, args, strict=False):
        if pname.lower() in SENSITIVE_ARG_KEYS:
            masked.append("***")
        else:
            masked.append(_safe_repr(value))
    # Trailing *args have no name — best-effort safe repr.
    for value in args[len(bound_params) :]:
        masked.append(_safe_repr(value))
    return masked


# Strip the `details={...}` payload from an exception's string form
_DETAILS_REDACTED = "<redacted>"  # the payload only — caller prepends "details="


def _strip_details_balanced(text: str) -> str:
    """Replace every top-level ``details={...}`` substring with
    ``details=<redacted>``.

    Walks the string with a small state machine that tracks
    brace depth and string-literal state. At depth 1 the opening
    ``{`` was just consumed; when the depth returns to 0 the
    substring is replaced. The walker tolerates ``{`` and ``}``
    inside string values so it does not under-report nesting.

    Only ``details={…}`` constructs are redacted; a bare
    ``details=foo`` (no opening brace) is left as-is so we
    don't lose the user's free-form text.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    needle = "details="
    while i < n:
        idx = text.find(needle, i)
        if idx < 0:
            out.append(text[i:])
            break
        out.append(text[i:idx])
        j = idx + len(needle)
        while j < n and text[j] in " \t":
            j += 1
        if j >= n or text[j] != "{":
            end = j
            while end < n and text[end] not in ",)\n":
                end += 1
            out.append(text[idx:end])
            i = end
            continue
        out.append(text[idx:j])
        depth = 0
        in_str: str | None = None
        k = j
        while k < n:
            ch = text[k]
            if in_str is not None:
                if ch == "\\" and k + 1 < n:
                    k += 2
                    continue
                if ch == in_str:
                    in_str = None
            elif ch in ('"', "'"):
                in_str = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    k += 1
                    break
            k += 1
        out.append(_DETAILS_REDACTED)
        i = k
    return "".join(out)


def _safe_error_str(error: BaseException | None) -> str | None:
    """Return a log-safe string for ``error``."""
    if error is None:
        return None
    raw = str(error)
    return _strip_details_balanced(raw)


# Module-level reads/writes route through the registry
# (see nullrun._singleton._RuntimeProxyModule).


def _get_or_create_runtime() -> NullRunRuntime:
    """Lazy initialization of runtime from environment.

    Order of resolution:
      1. The registry (canonical store)
      2. The global `NullRunRuntime.get_instance ` singleton, which
         reads `NULLRUN_API_KEY` / `NULLRUN_API_URL` from the environment
         and constructs the canonical cloud runtime.

    FIX-4 (0.3.x): the previous code wrapped `get_instance ` in a
    `try/except` that caught every exception and rebuilt a no-arg
    `NullRunRuntime ` as a "fallback". That fallback was doubly broken
    in 0.3.0: it silently swallowed `NullRunAuthenticationError` raised
    by the env-var-less branch, then crashed with the same error from
    the no-arg `NullRunRuntime ` constructor (which also requires
    `api_key` per T3-S2). The net effect was a delayed crash with a
    worse error message, plus a misleading "we have a runtime" log line.

    The fix removes the fallback entirely. `get_instance ` propagates
    `NullRunAuthenticationError` to the caller, where it surfaces at
    the first `@protect` invocation — the same fail-loud path that
    `nullrun.init ` uses. This aligns with the T3-S2 invariant that
    the SDK has no local mode: a missing API key must be a hard error
    not a silent allow-all.

    After obtaining the runtime, lazily triggers `auto_instrument()` so
    a user who writes only `@protect` (without calling `init()`
    first) still gets vendor SDK detection + token capture. The lazy
    trigger is idempotent — multiple `@protect` calls in the same
    process converge on a single `auto_instrument()` invocation. The
    call is best-effort: if the auto-instrumentation path raises (e.g.
    a vendor SDK breaks compatibility), the wrapper continues with the
    enforcement gate so enforcement never silently disappears.
    """
    cached = get_active_runtime()
    if cached is not None:
        _ensure_auto_instrumented(cached)
        return cached
    # No active runtime yet -- fall back to the canonical
    # get_instance() path. The result is stored in the registry
    # by the metaclass descriptor on NullRunRuntime._instance
    # (see nullrun._singleton), so every consumer that reads
    # `_runtime` afterward sees the same instance.
    runtime = NullRunRuntime.get_instance()
    _ensure_auto_instrumented(runtime)
    return runtime


# Lazy auto-instrumentation trigger (zero-config decorator path).
#
# The user-facing API is `nullrun.init()` which calls
# `auto_instrument(runtime)` directly (see `nullrun/__init__.py::init`).
# However, a user who writes only
# ``@nullrun.protect`` without calling ``init_or_die()`` first would
# still create a runtime via ``NullRunRuntime.get_instance()`` — but
# no vendor SDK patches would be installed, so token capture would be
# silently absent.
#
# This helper closes that gap. It runs ``auto_instrument()`` exactly
# once per process (the underlying ``auto.py::auto_instrument`` is
# itself idempotent, so this is a process-wide fast-path guard).
# Best-effort: any exception from the patch path is logged at DEBUG
# and swallowed so the enforcement gate continues to run. The moat is
# enforcement; instrumentation is best-effort telemetry.
_auto_instrument_trigger_lock = threading.Lock()
_auto_instrument_triggered = False


def _ensure_auto_instrumented(runtime: Any) -> None:
    """Lazy auto-instrumentation trigger for ``@protect`` without ``init``.

    Idempotent per process. Safe under concurrent ``@protect`` calls
    thanks to ``_auto_instrument_trigger_lock``. Never raises — a
    vendor SDK breaking change must not block the enforcement gate.
    """
    global _auto_instrument_triggered
    with _auto_instrument_trigger_lock:
        if _auto_instrument_triggered:
            return
        try:
            from nullrun.instrumentation.auto import auto_instrument

            auto_instrument(runtime)
            _auto_instrument_triggered = True
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.debug(
                "NullRun: lazy auto_instrument raised %s; "
                "enforcement continues without vendor instrumentation",
                exc,
            )
            # Don't set the flag — a future @protect call may try again
            # in case the failure was transient (e.g. an import-order
            # race where the vendor SDK is now importable).


def _next_span() -> SpanContext:
    """
    Derive the span for a new @protect call.

    If we're already inside a span (i.e. nested @protect calls), the new
    span is a child of the current one. Otherwise we open a fresh root —
    the dashboard reconstructs the whole tree from the `parent_span_id`
    chain emitted in span_start events.
    """
    parent = get_current_span()
    if parent is None:
        return create_root_span()
    return create_child_span(parent)


def _emit_span_start(runtime: Any, ctx: SpanContext, fn_name: str) -> None:
    """
    Best-effort emission of a span_start event.

    A failure here must NEVER block the wrapped function — observability
    is downstream of the user's work. We swallow every exception.
    """
    try:
        runtime.track_event(
            event_type="span_start",
            trace_id=ctx.trace_id,
            span_id=ctx.span_id,
            parent_span_id=ctx.parent_span_id,
            depth=ctx.depth,
            fn_name=fn_name,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"span_start emission failed: {exc}")


def _emit_span_end(
    runtime: Any,
    ctx: SpanContext,
    error: str | None = None,
) -> None:
    """
    Best-effort emission of a span_end event. Same contract as
    `_emit_span_start` — never blocks.
    """
    try:
        runtime.track_event(
            event_type="span_end",
            trace_id=ctx.trace_id,
            span_id=ctx.span_id,
            parent_span_id=ctx.parent_span_id,
            depth=ctx.depth,
            fn_name=getattr(ctx, "fn_name", None),
            error=error,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"span_end emission failed: {exc}")


def _safe_cancel_active_execution(reason: str | None = None) -> None:
    """Best-effort cancel of any in-flight reservation captured by /gate.

    Used by @protect's exception path: when the wrapped function or any
    pre-execution gate raises after /gate has succeeded, the budget
    reservation is still open in Redis and will leak via TTL expiry
    unless closed. This helper makes the cancel call that closes it.

    Behavior:
      - No-op if no execution_id was captured (failure happened
        pre-/gate — e.g., control_plane KILL).
      - Never raises: catches everything including NullRunTransportError
        and NullRunBackendError. Masking the original exception with
        a cancel-failure would defeat observability.
      - Synchronous, blocking HTTP. Caller is the @protect context
        manager; HTTP I/O is the same channel used by
        check_workflow_budget, so it does not change timeout posture.
      - CLOSE-ORPHAN (ADR-047, 2026-09-21): after cancel_execution,
        if a pending approval_id was captured for this execution_id,
        ALSO call consume_approval so the row flips to CONSUMED.
        Best-effort: a network blip here does NOT mask the cancel.
    """
    try:
        execution_id = get_server_minted_execution_id()
    except Exception:
        return
    if not execution_id:
        return
    try:
        runtime = get_runtime()
    except Exception:
        return
    try:
        runtime.cancel_execution(execution_id, reason=reason)
    except Exception:
        # An orphan from cancellation failure is preferred over
        # masking the original exception with a transport error.
        pass
    # CLOSE-ORPHAN: also consume the approval row if one was captured.
    # The reverse index (execution_id → approval_id) is RLock-guarded
    # in Runtime and populated by check_workflow_budget at the
    # outcome=approved branch. If the SDK crashed before reaching that
    # branch, the lookup returns None and this is a no-op.
    try:
        approval_id = runtime.lookup_pending_approval_id_for_execution(
            execution_id
        )
        if approval_id:
            runtime.consume_approval(approval_id, execution_id=execution_id)
    except Exception:
        # Same posture as the cancel: best-effort, never mask.
        return


def protect(fn: F | None = None) -> F | Callable[[F], F]:
    """
        Decorator that wraps a function in a NullRun span.

        Usage:
            @nullrun.protect
            def my_agent(query: str) -> str:
    ...

            @nullrun.protect
            async def my_async_agent(query: str) -> str:
    ...

        The span hierarchy is built automatically from the calling context
        (via `nullrun.tracing.SpanContext` contextvars) — nested `@protect`
        calls become child spans of the outer one. No parameters are needed:
        the workflow is derived from the API key on the backend.

        ## Pre-execution gate order (ADR-008 Rule 4)

        The wrapper runs three gates in this order. KILL short-circuits:

            1. `check_control_plane` — KILL/PAUSE is terminal.
            2. `check_workflow_budget` — "any budget left?" via /gate.
            3. `_run_tool_policy_gate` — per-tool policy via /execute
                                          (runs on every call).

        Each gate has its own fail-OPEN/CLOSED policy declared in
        `runtime.py`; see ADR-008 Rule 5 for the full table. `span_end`
        is emitted on every path (including KILL/PAUSE) so the dashboard
        can render the kill with span context.

        `fn` may be omitted to return the decorator itself (the standard
        `@decorator` vs `@decorator ` shape), so this works for both:

            @nullrun.protect
            def f:...

            @nullrun.protect
            def g:...
    """
    if fn is None:
        # `@nullrun.protect ` with empty parens — return the decorator
        # bound to itself so the next call wraps the target function.
        return protect

    # NOTE: prior 0.18.x versions auto-attached a default
    # ``ToolParamsExtractor(include_all=True)`` here and stored it
    # on the function via ``_nullrun_extractor``. That path was
    # removed because it forced the SDK to know what an "extractor"
    # is. The 0.18.2 design is simpler: ``@protect`` has no
    # per-function state. Every call constructs an opaque
    # ``NoImpact`` envelope locally and forwards it to
    # ``runtime.execute(...)``. All policy decisions
    # (allow / block / require-approval) live on the backend; the
    # SDK only relays (tool_name, kwargs, args) and renders the
    # decision back into an exception class.

    @contextlib.contextmanager
    def _protect_body(args: tuple[Any, ...], kwargs: dict[str, Any], unify_block: bool):
        """Shared ADR-008 Rule-4 scaffolding for sync + async wrappers.

        Runs the pre-execution gates (KILL/PAUSE → /gate budget
        pre-flight → span start → /execute tool policy), yields the
        runtime so the caller can invoke ``fn`` and ``track_tool``
        within the gated region, then emits ``span_end`` with the
        captured error.

        ``unify_block`` controls the kill/pause signal translation.
        Sync wrappers pass ``True`` so the user sees a single
        ``NullRunBlockedException`` regardless of which gate raised;
        async wrappers pass ``False`` so the underlying
        ``WorkflowKilledInterrupt`` propagates — async frameworks
        (asyncio task cancellation, signal handlers) rely on the
        original ``BaseException`` subtype to interrupt cleanly.
        """
        runtime = _get_or_create_runtime()
        span = _next_span()
        token = set_span(span)
        # Mirror the trace_id / span_id into ``_trace_id_var`` /
        # ``_span_id_var`` so the runtime's ``_enrich_event`` (which
        # reads via ``get_trace_id()`` / ``get_span_id()`` for cost
        # events AND for ``parent_trace_id`` derivation at
        # runtime.py:2967) emits events tagged with the SAME
        # trace_id / span_id as SpanContext. Without this mirror a bare
        # ``@protect`` (no enclosing ``with workflow``) sees a
        # tree-break: span_start carries SpanContext.trace_id while
        # llm_call / tool_call carries a freshly generated trace_id.
        # Token-based so a nested ``@protect`` inside an outer
        # ``@protect`` (or inside ``with workflow``)
        # restores the outer trace/span on reset.
        trace_token = set_trace_id(span.trace_id)
        span_token = set_span_id(span.span_id)
        # ``fn.__name__`` when the user did NOT explicitly call
        # ``set_call_context(tools=...)``. The F01 fix
        # (``runtime.execute`` body at runtime.py:2746-2760 and the
        # /gate path at runtime.py:1903-1941) conditionally forwards
        # the per-call tools contextvar onto the wire body, but the
        # upstream contextvar was never populated for the @protect
        # decorator path. Without this fix every wire round-trip
        # omits the `tools` field, the backend's Step 3 tool_block
        # check fails-CLOSED via TB-1 (``no_tools_field``), and
        # /015/016/017) never reach the approval_rule_eval step.
        # Token-based so a nested @protect inside an outer @protect
        # (or inside ``with workflow``) restores the outer contextvar
        # on reset — same shape as the trace/span token resets above.
        _existing_call_tools = get_call_tools()
        if not _existing_call_tools:
            call_tools_token: Token[tuple[str, ...]] | None = _call_tools_var.set(
                (fn.__name__,),
            )
        else:
            call_tools_token = None
        error: BaseException | None = None
        try:
            # the runtime can warn when @protect fires often but no
            # LLM-call event is ever observed (silent-instrumentation
            # failure mode). The bump lives at the entry of the gate
            # so even gates that fail-CLOSED (block / kill) count
            # toward the diagnosis — the operator still wants to
            # know if the dashboard shows zero LLM calls despite
            # the agent running.
            runtime._bump_protect_count()

            # 1. KILL/PAUSE from the dashboard short-circuits
            # everything else. The resolution order is the
            # user-set contextvar first, then the API-key-bound
            # workflow — same precedence as check_workflow_budget.
            runtime.check_control_plane(get_workflow_id() or None)

            # 2. Budget pre-flight via /gate. Raises
            # WorkflowKilledInterrupt on real block; fails open
            # on transport error (see runtime.check_workflow_budget).
            runtime.check_workflow_budget()

            # 3. Span start — best-effort, never blocks.
            _emit_span_start(runtime, span, fn.__name__)

            # 4. Per-tool policy gate via /execute. Runs on EVERY
            # @protect call (no extractor / no short-circuit). The
            # SDK is policy-blind; it ships tool_name + args + kwargs
            # + the NoImpact envelope/digest to the backend and the
            # backend decides allow/block/require-approval.
            _run_tool_policy_gate(runtime, fn, args, kwargs)

            yield runtime
        except BaseException as exc:  # noqa: BLE001
            error = exc
            if unify_block and isinstance(exc, (WorkflowKilledInterrupt, WorkflowPausedException)):
                # Layer 1: pass through the kill/pause error_code so
                # the user can tell WHY the body did not run —
                # ``NR-W002`` (killed) vs ``NR-W003`` (paused). The
                # block subclass carries the right user_action hint.
                _code = "NR-W002" if isinstance(exc, WorkflowKilledInterrupt) else "NR-W003"
                err = NullRunBlockedException(
                    workflow_id=exc.workflow_id,
                    reason=exc.reason,
                    error_code=_code,
                )
                # Layer 2: fire the on_error hook. Kill/pause is a
                # user-visible state change (the dashboard did
                # this) so most observability hooks want to know
                # about it. Note: the underlying kill signal
                # itself (WorkflowKilledInterrupt) does NOT fire
                # the hook (BaseException bypass) — only this
                # re-wrapped form does.
                runtime._emit_sdk_error(err, stage="decorator", workflow_id=exc.workflow_id)
                raise err from exc
            raise
        finally:
            reset_span(token)
            # F-19 follow-up: token-based reset matches the trace/span
            # token pattern (paired with the tokens set above). Order
            # does not matter; both resets restore the prior
            # contextvar regardless of which one runs first.
            reset_trace_id(trace_token)
            reset_span_id(span_token)
            # F03 follow-up: reset the per-call tools contextvar if
            # we set it. Outer ``with workflow`` / nested @protect
            # prior value restored; bare @protect leaves the
            # contextvar empty again (the default).
            if call_tools_token is not None:
                _call_tools_var.reset(call_tools_token)
            _emit_span_end(
                runtime,
                span,
                error=_safe_error_str(error),
            )

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            fn_completed = False
            try:
                with _protect_body(args, kwargs, unify_block=False) as runtime:
                    result = await fn(*args, **kwargs)
                    fn_completed = True
                    runtime.track_tool(
                        fn.__name__,
                        metadata={"arguments": _safe_kwargs(kwargs)},
                    )
                    return result
            except Exception:
                # Close the in-flight reservation unless fn() actually
                # completed — in which case track_tool failure means
                # side effects already happened and only
                # retry/consume semantics apply, not cancel.
                #
                # NB: we intentionally catch Exception, not
                # BaseException. asyncio.CancelledError /
                # KeyboardInterrupt / SystemExit propagate without
                # blocking I/O — synchronous HTTP in a cancellation
                # handler delays shutdown and triggers "Task was
                # destroyed but pending" warnings. Orphan from a
                # cancelled task is left to TTL/reconciliation, which
                # is what the safety net is for.
                if not fn_completed:
                    _safe_cancel_active_execution(reason="tool_exception")
                raise

        return async_wrapper  # type: ignore[return-value]

    @functools.wraps(fn)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        fn_completed = False
        try:
            with _protect_body(args, kwargs, unify_block=True) as runtime:
                result = fn(*args, **kwargs)
                fn_completed = True
                runtime.track_tool(
                    fn.__name__,
                    metadata={"arguments": _safe_kwargs(kwargs)},
                )
                return result
        except BaseException:
            # Sync path: BaseException is fine to catch and run
            # cleanup in. No event loop to delay; KeyboardInterrupt
            # on Ctrl+C just gets a few seconds of cancel I/O before
            # exit. Matches existing _protect_body unify_block
            # semantics.
            if not fn_completed:
                _safe_cancel_active_execution(reason="tool_exception")
            raise

    return sync_wrapper  # type: ignore[return-value]


def _run_tool_policy_gate(
    runtime: Any,
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    """
    Pre-execution per-tool policy gate — runs on EVERY ``@protect``.

    The 0.18.2 design makes every protected call flow through
    ``runtime.execute`` unconditionally; the SDK is policy-blind
    and just relays ``tool_name + masked_args + masked_kwargs +
    NoImpact envelope`` to the /execute endpoint. The backend
    applies allow/block/require-approval rules.

    ## Fail-OPEN/CLOSED Policy (ADR-008)

    The per-tool policy gate is **fail-CLOSED**: the body MUST NOT
    run when the policy engine is unreachable. An unblocked
    ``charge_card`` running while the policy engine is offline is
    a security regression, far worse than a denied call during
    the outage.

    Opt-out: set ``NULLRUN_SENSITIVE_FAIL_OPEN=1`` to restore fail-
    OPEN behavior on transport error (dev / test only). Real
    ``decision=block`` from the gateway is still honored and still
    raises ``NullRunBlockedException``.

    ## Wire contract

    Same fields on /execute as before: ``tool_name``,
    ``{"args": masked_args, "kwargs": masked}``, ``business_impact``
    (now always ``{"kind": "none"}``), ``action_digest`` (SHA-256
    over the canonical NoImpact envelope; pinned, deterministic),
    ``tools``. Backend unchanged — only the SDK's interpretation of
    what to put in ``business_impact`` simplified.
    """
    masked = _safe_kwargs(kwargs)
    masked_args = _safe_args(fn, args)

    # Wire-shape compatibility: ``business_impact`` stays None
    # on /execute when no per-tool typed impact is extracted
    # (the bare @protect shape — backend reads only
    # ``action_digest`` + ``kwargs`` for ToolParameters Approval
    # Rules). The ``action_digest`` is still computed against
    # the canonical NoImpact envelope so the Phase-1+ wire-shape
    # ``tests/test_business_impact.py``.
    no_impact = BusinessImpact.no_impact()
    business_impact_dict: dict[str, Any] | None = None
    action_digest_hex: str = compute_action_digest(no_impact)

    from nullrun.breaker.exceptions import (
        NullRunBlockedException,
        NullRunDecision,
        NullRunExecutionNotFoundError,
        NullRunInfrastructureError,
        NullRunTransportError,
        RateLimitError,
        TransportErrorSource,
    )

    fail_open = os.environ.get("NULLRUN_SENSITIVE_FAIL_OPEN", "").strip() == "1"
    # *display* workflow_id via the runtime's precedence chain
    # (contextvar → self.workflow_id → None). Sentinel stays as the
    # last resort for never-bound keys (no workflow context).
    workflow_id = runtime._resolve_workflow_id(get_workflow_id()) or UNKNOWN_WORKFLOW_ID

    try:
        # Pass on_transport_error="raise" so the transport raises
        # NullRunTransportError on network / 5xx failure instead of
        # returning a synthetic dict. The arm below converts the
        # typed error into NullRunBlockedException so the caller's
        # `except NullRunBlockedException` catches it uniformly.
        result = runtime.execute(
            fn.__name__,
            {"args": masked_args, "kwargs": masked},
            on_transport_error="raise",
            business_impact=business_impact_dict,
            action_digest=action_digest_hex,
            tools=get_call_tools(),
        )
    except NullRunExecutionNotFoundError:
        raise
    except RateLimitError:
        raise
    except NullRunBlockedException:
        # Real policy-block decision from the gateway — propagate as-is.
        raise
    except NullRunTransportError as exc:
        # ADR-008: classified transport failure.
        if fail_open:
            logger.warning(
                f"tool policy gate unavailable for {fn.__name__!r}: "
                f"{exc.source} on /{exc.endpoint}. "
                f"NULLRUN_SENSITIVE_FAIL_OPEN=1 — body will run."
            )
            return
        _code = {
            TransportErrorSource.NETWORK_ERROR: "NR-B001",
            TransportErrorSource.GATEWAY_ERROR: "NR-B002",
            TransportErrorSource.AUTH_ERROR: "NR-A003",
            TransportErrorSource.BREAKER_OPEN: "NR-B005",
        }.get(exc.source, "NR-B001")
        err = NullRunBlockedException(
            workflow_id=workflow_id,
            reason=f"policy engine unavailable: {exc.source}",
            tool_name=fn.__name__,
            error_code=_code,
            user_action=(
                f"The NullRun policy engine is unreachable "
                f"({exc.source.value}). The body of "
                f"'{fn.__name__}' did NOT run (fail-CLOSED). "
                f"Set NULLRUN_SENSITIVE_FAIL_OPEN=1 to opt out for "
                f"tests / staging — production should leave it off."
            ),
        )
        runtime._emit_sdk_error(
            err,
            stage="tool_policy_gate",
            workflow_id=workflow_id,
            tool_name=fn.__name__,
            extra={"transport_source": exc.source.value},
        )
        raise err from exc
    except NullRunDecision:
        # DEF-NR-TRANSPORT-CATCHFANIN-GAP umbrella pass-through:
        # NullRunChainError, NullRunWorkflowInactiveError,
        # NullRunConsumeOverbudgetError, WorkflowPausedException —
        # preserve first-class attributes for cookbook recovery.
        raise
    except NullRunInfrastructureError:
        # DEF-NR-TRANSPORT-CATCHFANIN-GAP umbrella pass-through:
        # NullRunAuthError, NullRunProtocolError,
        # NullRunRateLimitRedisError, NullRunConfigError — preserve
        # first-class attributes.
        raise
    except Exception as exc:  # noqa: BLE001
        if fail_open:
            logger.warning(
                f"tool policy gate unavailable for {fn.__name__!r}: "
                f"{exc}. NULLRUN_SENSITIVE_FAIL_OPEN=1 — body will run."
            )
            return
        err = NullRunBlockedException(
            workflow_id=workflow_id,
            reason=f"policy engine unavailable: {exc}",
            tool_name=fn.__name__,
            error_code="NR-B001",
            user_action=(
                f"The NullRun policy engine raised an unexpected "
                f"exception during the @protect pre-check of "
                f"'{fn.__name__}'. The body did NOT run. Check the "
                f"chained exception (raise ... from exc) for the "
                f"root cause."
            ),
        )
        runtime._emit_sdk_error(
            err,
            stage="tool_policy_gate",
            workflow_id=workflow_id,
            tool_name=fn.__name__,
        )
        raise err from exc

    # Defense in depth: classification audit. If the transport ever
    # returns a synthetic dict whose decision_source marks a
    # fallback, block per ADR-008 fail-CLOSED. This arm is preserved
    # for defense in depth even though the
    # typed transport-error arms above are the canonical path.
    if isinstance(result, dict):
        decision_source = result.get("decision_source", "")
        if isinstance(decision_source, str) and (
            decision_source.startswith("FALLBACK_")
            or decision_source
            in {
                TransportErrorSource.NETWORK_ERROR,
                TransportErrorSource.GATEWAY_ERROR,
                TransportErrorSource.BREAKER_OPEN,
                TransportErrorSource.AUTH_ERROR,
            }
        ):
            if fail_open:
                logger.warning(
                    f"tool policy gate for {fn.__name__!r} returned "
                    f"{decision_source}; NULLRUN_SENSITIVE_FAIL_OPEN=1 — body will run."
                )
                return
            _code = {
                "NETWORK_ERROR": "NR-B001",
                "GATEWAY_ERROR": "NR-B002",
                "AUTH_ERROR": "NR-A003",
                "BREAKER_OPEN": "NR-B005",
            }.get(decision_source, "NR-B001")
            err = NullRunBlockedException(
                workflow_id=workflow_id,
                reason=f"policy engine unavailable: {decision_source}",
                tool_name=fn.__name__,
                error_code=_code,
                user_action=(
                    f"The NullRun policy engine returned a fallback "
                    f"({decision_source}) for '{fn.__name__}'. The "
                    f"body did NOT run. Retry once the policy engine "
                    f"is back — or set NULLRUN_SENSITIVE_FAIL_OPEN=1 "
                    f"for tests / staging."
                ),
            )
            runtime._emit_sdk_error(
                err,
                stage="tool_policy_gate",
                workflow_id=workflow_id,
                tool_name=fn.__name__,
                extra={"decision_source": decision_source},
            )
            raise err

    # Real `decision=block` from the gateway is already converted to
    # NullRunBlockedException by `runtime.execute` — no second check
    # needed here. A `decision=allow` with `decision_source=GATEWAY`
    # (the happy path) just falls through and the body runs.


def reset() -> None:
    """
    Reset NullRun runtime. Mainly for testing or when you need to
    reinitialize the global runtime instance.
    """
    cached = get_active_runtime()
    if cached:
        try:
            cached.shutdown()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Runtime shutdown raised: {exc}")
    # Clear the registry slot. Module-level `_runtime` proxy
    # reads through the registry, so the next `@protect` call
    # sees no active runtime and falls back to get_instance().
    from nullrun._registry import get_registry

    get_registry().clear()
    logger.info("NullRun runtime reset")


def get_protected_runtime() -> NullRunRuntime | None:
    """Get the current protected runtime (the one `@protect` would use)."""
    cached = get_active_runtime()
    if cached is not None:
        return cached
    # Fall back to the global singleton if the registry is empty.
    try:
        return get_runtime()
    except Exception:
        return None


# Install the registry-backed proxy on the module class
# (see nullrun._singleton for the rationale).
from nullrun._singleton import install_runtime_proxy

install_runtime_proxy(__name__)
