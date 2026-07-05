"""
Decorators for the NullRun SDK.

Public surface (Phase 2 Commit 4): `protect` is the only gate decorator.
It takes NO parameters — span hierarchy is built automatically from the
caller's context via contextvars, and the workflow is derived from the
API key on the backend (the dashboard surfaces the agent's name from
the key's `name` field).

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

import functools
import inspect
import logging
import os
from collections.abc import Callable
from typing import Any, TypeVar

from nullrun.breaker.exceptions import (
    NullRunBlockedException,
    WorkflowKilledInterrupt,
    WorkflowPausedException,
)
from nullrun.context import get_workflow_id
from nullrun.runtime import NullRunRuntime, get_runtime

# Sentinel used when a gate fires outside a workflow context.
# Matches the constant in nullrun.runtime so we don't introduce
# a new magic string in audit logs.
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

# Phase 3: expanded sensitive-arg keys. The original 7-key set
# missed obvious PII tokens and credential names; ``@sensitive`` and
# ``_safe_kwargs`` would have shipped them in the audit log.
# Matching is case-insensitive (see ``_safe_kwargs`` which calls
# ``.lower `` on the key).
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
    previously lived as separate ``_safe_repr`` + ``_strip_details_balanced``
    calls — there are now two callers that compose them, and the
    invariant ``redact BEFORE truncate`` was being maintained by
    convention only. ``_safe_repr`` is now the single source of truth.
    """
    r = repr(value)
    # Phase 1: redact ``details={...}`` substrings on the FULL repr.
    # Cheap (single linear scan over the string), and ensures the
    # ``details=`` substring is replaced before we potentially
    # truncate it away.
    r = _strip_details_balanced(r)
    # Phase 2: truncate to ``max_len`` so a giant repr doesn't bloat
    # span events. We append ``...<truncated>`` so consumers can
    # see the cut happened.
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


# SEC-29: strip the `details={...}` payload from an exception's
# string form before it lands in the span_end audit event.
# Phase 3 replaced the previous one-level regex with a
# balanced-brace walker that handles nested dicts and dict values
# that contain `{` / `}` in their string content.
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
    """Return a log-safe string for ``error`` (SEC-29, Phase 3)."""
    if error is None:
        return None
    raw = str(error)
    return _strip_details_balanced(raw)


# Module-level cache for the runtime instance — the @protect decorator needs
# a runtime to emit span_start/span_end events, but the runtime is normally
# created via `nullrun.init `. We lazily instantiate one if @protect is
# used before init. The slot is also where tests can inject a noop.
_runtime: NullRunRuntime | None = None


def _get_or_create_runtime() -> NullRunRuntime:
    """Lazy initialization of runtime from environment.

    Order of resolution:
      1. The module-level `_runtime` slot (set by tests or by `init `)
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

    Tries to patch OpenAI on first creation so the auto-instrumentation
    path picks up the runtime the user will eventually use.
    """
    global _runtime

    if _runtime is not None:
        return _runtime

    _runtime = NullRunRuntime.get_instance()

    # The previous OpenAI v0.x auto-patch hook was removed in 0.4.0:
    # openai>=1.0 does not expose ChatCompletion.create as an
    # attribute. All OpenAI v1.0+ traffic is now tracked
    # vendor-independently by the httpx transport hook in
    # nullrun.instrumentation.auto, which is wired by
    # nullrun.init — not at the lazy-resolve path here.
    logger.info("NullRun runtime initialized: mode=cloud")
    return _runtime


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
        3. `_enforce_sensitive_tool` — per-tool policy (no-op if not
                                      marked sensitive).

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

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            runtime = _get_or_create_runtime()
            span = _next_span()
            token = set_span(span)

            # ADR-008 Rule 4: gate order is
            # control_plane → budget → span_start → sensitive
            # Wrapped in try/except so span_end still emits on KILL/PAUSE.
            error: BaseException | None = None
            try:
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

                # 4. Per-tool policy for @sensitive tools. Fails CLOSED
                # on transport error (see _enforce_sensitive_tool).
                _enforce_sensitive_tool(runtime, fn, args, kwargs)

                return await fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001
                # Capture the error so we can include it in span_end
                # *after* the contextvar is reset. Re-raise so the
                # caller's try/except still sees the original exception.
                error = exc
                raise
            finally:
                reset_span(token)
                _emit_span_end(
                    runtime,
                    span,
                    error=_safe_error_str(error),
                )

        return async_wrapper  # type: ignore[return-value]

    @functools.wraps(fn)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        runtime = _get_or_create_runtime()
        span = _next_span()
        token = set_span(span)

        # ADR-008 Rule 4: gate order is
        # control_plane → budget → span_start → sensitive
        # Wrapped in try/except so span_end still emits on KILL/PAUSE.
        error: BaseException | None = None
        try:
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

            # 4. Per-tool policy for @sensitive tools. Fails CLOSED
            # on transport error (see _enforce_sensitive_tool).
            _enforce_sensitive_tool(runtime, fn, args, kwargs)

            return fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001
            error = exc
            # Round 3 (Phase 0.4.0): unify the "blocked" signal at
            # the @protect boundary so callers can catch a single
            # NullRunBlockedException for both policy blocks and
            # sensitive-tool blocks. Direct calls to
            # check_workflow_budget still raise the original
            # exception type so callers that distinguish hard vs
            # soft blocks keep that signal.
            if isinstance(exc, (WorkflowKilledInterrupt, WorkflowPausedException)):
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
            _emit_span_end(
                runtime,
                span,
                error=_safe_error_str(error),
            )

    return sync_wrapper  # type: ignore[return-value]


def _enforce_sensitive_tool(
    runtime: Any,
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    """
    Pre-execution policy check for sensitive tools.

    If `fn.__name__` is in the runtime's sensitive-tool set (built-in
    or registered via `add_sensitive_tool` / `@sensitive`), call
    `runtime.execute(...)` BEFORE the body runs. The /execute endpoint
    is the authoritative gate; `NullRunBlockedException` propagates to
    the caller, mirroring the contract of `check_workflow_budget`.

    kwargs are masked via `SENSITIVE_ARG_KEYS` so passwords / tokens
    never leave the process. The same masking is used for span events.

    ## Fail-OPEN/CLOSED Policy (ADR-008)

    This gate is **fail-CLOSED**: the body MUST NOT run when the
    policy engine is unreachable, regardless of what /execute returns.
    Two failure paths both result in `NullRunBlockedException`:

    1. **Transport raises** `NullRunTransportError` (the new
       `on_transport_error="raise"` path): the runtime layer surfaces
       classified NETWORK / GATEWAY / BREAKER-OPEN failures as
       exceptions. The body of this gate catches them and re-raises
       as `NullRunBlockedException` with the source in the reason
       ("policy engine unavailable: NETWORK_ERROR" etc.).

    2. **Transport returns a dict** whose `decision_source` starts
       with `FALLBACK_` (defense in depth — covers the legacy
       `fallback_mode=PERMISSIVE` path and any future regression in
       `runtime.execute` that drops the `on_transport_error="raise"`
       argument). The body of this gate inspects the result and
       re-raises as `NullRunBlockedException` before the wrapped
       function runs.

    This is the opposite of `check_workflow_budget` /
    `check_control_plane`, which deliberately fail-OPEN — a transient
    backend outage must not freeze the user's agent. Sensitive tools
    have a different threat model: an unblocked `charge_card ` that
    runs when the policy engine is down is worse than a denied
    `charge_card ` during an outage.

    Opt-out: set `NULLRUN_SENSITIVE_FAIL_OPEN=1` to restore the prior
    fail-OPEN behavior on transport error. Useful in dev / test
    environments where the policy engine is intentionally absent.
    The opt-out is intentionally scoped to the *transport-error*
    case; a real `decision=block` from the gateway is still honored
    and still raises `NullRunBlockedException`.
    """
    if not runtime.is_sensitive_tool(fn.__name__):
        return
    masked = _safe_kwargs(kwargs)
    # P0-1: positional args are masked the same way as kwargs. Without
    # this, a sensitive tool called positionally (e.g.
    # ``charge("4111-1111-1111-1111", 50)``) would leak the PAN into
    # the /execute payload that lands in the audit log.
    masked_args = _safe_args(fn, args)

    # ADR-008: prefer `on_transport_error` (raise classified
    # NullRunTransportError); fall back to legacy `fallback_mode` for
    # older runtimes that pre-date the rename.
    from nullrun.breaker.exceptions import (
        NullRunBlockedException,
        NullRunTransportError,
        TransportErrorSource,
    )

    fail_open = os.environ.get("NULLRUN_SENSITIVE_FAIL_OPEN", "").strip() == "1"
    workflow_id = get_workflow_id() or UNKNOWN_WORKFLOW_ID

    try:
        # Round 3 (Phase 0.4.0): pass on_transport_error="raise" so
        # the transport raises NullRunTransportError on network / 5xx
        # failure instead of returning a synthetic dict. The arm
        # below converts the typed error into NullRunBlockedException
        # so the caller's `except NullRunBlockedException` catches it
        # uniformly.
        result = runtime.execute(
            fn.__name__,
            {"args": masked_args, "kwargs": masked},
            on_transport_error="raise",
        )
    except NullRunBlockedException:
        # Real policy-block decision from the gateway — propagate as-is.
        raise
    except NullRunTransportError as exc:
        # ADR-008: classified transport failure. Re-raise as
        # NullRunBlockedException so the caller's existing
        # `except NullRunBlockedException` catches the same way as a
        # real policy block. The body never runs.
        if fail_open:
            logger.warning(
                f"sensitive tool pre-check unavailable for {fn.__name__!r}: "
                f"{exc.source} on /{exc.endpoint}. NULLRUN_SENSITIVE_FAIL_OPEN=1 — body will run."
            )
            return
        # Layer 1: stamp the source-specific error code so the
        # caller can distinguish "backend is down" from "we tripped
        # the local circuit breaker". Both are retryable in the
        # sense that the body will run when the policy engine
        # recovers, but the body still MUST NOT run now (fail-CLOSED).
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
                f"({exc.source.value}). The body of @sensitive "
                f"'{fn.__name__}' did NOT run (fail-CLOSED). "
                f"Set NULLRUN_SENSITIVE_FAIL_OPEN=1 to opt out for "
                f"tests / staging — production should leave it off."
            ),
        )
        # Layer 2: fire the on_error hook. The sensitive-tool
        # path is where a transport failure becomes a hard
        # deny — observability hooks should see it even if the
        # user's except clause swallows the exception.
        runtime._emit_sdk_error(
            err,
            stage="sensitive_tool",
            workflow_id=workflow_id,
            tool_name=fn.__name__,
            extra={"transport_source": exc.source.value},
        )
        raise err from exc
    except Exception as exc:  # noqa: BLE001
        # Any other exception is a transport / network / backend
        # failure. Re-raise as NullRunBlockedException so the caller
        # sees a uniform "this tool was denied" signal — they should
        # not need to also catch httpx.ConnectError or similar.
        if fail_open:
            logger.warning(
                f"sensitive tool pre-check unavailable for {fn.__name__!r}: "
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
                f"exception during the @sensitive pre-check of "
                f"'{fn.__name__}'. The body did NOT run. Check the "
                f"chained exception (raise ... from exc) for the "
                f"root cause."
            ),
        )
        # Layer 2: emit for the generic exception path too.
        # (The NullRunTransportError path above already emits
        # this covers the catch-all ``except Exception`` arm.)
        runtime._emit_sdk_error(
            err,
            stage="sensitive_tool",
            workflow_id=workflow_id,
            tool_name=fn.__name__,
        )
        raise err from exc

    # Defense in depth (ADR-008 Rule 1 + Rule 2): if `runtime.execute`
    # ever returns a dict with `decision_source` indicating a transport
    # failure (legacy `FALLBACK_*` strings OR the typed
    # `TransportErrorSource` enum values), honor the gate's fail-CLOSED
    # policy here. The body still must not run.
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
                    f"sensitive tool pre-check for {fn.__name__!r} returned "
                    f"{decision_source}; NULLRUN_SENSITIVE_FAIL_OPEN=1 — body will run."
                )
                return
            # Layer 1: stamp the source-specific code on the
            # fallback block so cookbook code can distinguish
            # between "the policy engine said block" (NR-T001 etc.)
            # and "we blocked because the policy engine never
            # answered" (NR-B001/B002).
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
                    f"({decision_source}) for @sensitive '{fn.__name__}'. "
                    f"The body did NOT run. Retry once the policy engine "
                    f"is back — or set NULLRUN_SENSITIVE_FAIL_OPEN=1 for "
                    f"tests / staging."
                ),
            )
            # Layer 2: emit the on_error hook with the fallback
            # source as extra metadata so Sentry rules can
            # distinguish "policy engine is down" from "we
            # tripped the local circuit breaker".
            runtime._emit_sdk_error(
                err,
                stage="sensitive_tool",
                workflow_id=workflow_id,
                tool_name=fn.__name__,
                extra={"decision_source": decision_source},
            )
            raise err

    # Real `decision=block` from the gateway is already converted to
    # NullRunBlockedException by `runtime.execute` — no second check
    # needed here. A `decision=allow` with `decision_source=GATEWAY`
    # (the happy path) just falls through and the body runs.


def sensitive(fn: F) -> F:
    """
    Mark a function as sensitive. `@protect` will pre-check
    `runtime.execute(...)` before the body runs.

    This is the discoverable alternative to the lower-level
    `runtime.add_sensitive_tool(fn.__name__)`. Chain with `@protect`
    in either order (both work via `functools.wraps`); the
    recommended form is `@sensitive` outside so the name is
    registered before the wrapper is built:

        @nullrun.sensitive
        @nullrun.protect
        def charge_card(amount: int) -> str:
...
    """
    try:
        # Use the same slot the @protect wrapper uses so the
        # registration lands on the same runtime instance the
        # wrapper will consult. Falling back to get_runtime 
        # would hit a different singleton and silently no-op in
        # tests that build a custom runtime.
        rt = _get_or_create_runtime()
        rt.add_sensitive_tool(fn.__name__)
    except Exception as exc:
        # Sensitive tool registration is part of the fail-CLOSED contract
        # (ADR-008 / sensitive-tool-fail-closed memory). If we
        # cannot reach the runtime to register the tool, the body MUST NOT
        # execute later — but since `@sensitive` only registers the name
        # and the wrapper enforces it on each call, raising here is the
        # correct signal. The earlier `except Exception` quietly turned a
        # registration failure into a body that ran without pre-execution
        # check — a security regression under partial initialization.
        raise RuntimeError(
            f"@sensitive registration failed for {fn.__name__!r}: {exc}. "
            "Cannot proceed without runtime; tool will be blocked until "
            "NullRun initializes correctly."
        ) from exc
    return fn


def reset() -> None:
    """
    Reset NullRun runtime. Mainly for testing or when you need to
    reinitialize the global runtime instance.
    """
    global _runtime
    if _runtime:
        try:
            _runtime.shutdown()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Runtime shutdown raised: {exc}")
    _runtime = None
    logger.info("NullRun runtime reset")


def get_protected_runtime() -> NullRunRuntime | None:
    """Get the current protected runtime (the one `@protect` would use)."""
    global _runtime
    if _runtime is not None:
        return _runtime
    # Fall back to the global singleton if the decorator-level slot is
    # empty — this matches the behaviour of every other helper that
    # reads from `get_runtime `.
    try:
        return get_runtime()
    except Exception:
        return None
