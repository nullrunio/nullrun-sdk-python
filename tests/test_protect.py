"""
Tests for `@protect` with automatic span hierarchy.

The decorator must:
  - Create a root span (parent_span_id=None, depth=0) on the outermost call
  - Create a child span (parent_span_id=<outer span_id>, depth+1) on nested calls
  - Restore the previous context (None or parent) after the call
  - Work with sync AND async functions
  - Emit `span_start` and `span_end` events to the runtime
"""

from __future__ import annotations

import asyncio

import pytest

import nullrun
from nullrun.tracing import get_current_span, reset_span, set_span

# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────


@pytest.fixture
def mock_runtime(make_runtime, mock_api):
    """An isolated, mocked runtime for span assertions."""
    return make_runtime()


class _RecordingRuntime:
    """
    Drop-in stand-in for `NullRunRuntime` that records every `track_event`
    call so we can assert on span_start/span_end emission without a
    real backend.

    The decorator calls `check_control_plane`, `check_workflow_budget`
    and `is_sensitive_tool` as pre-execution gates (ADR-008). The default
    no-op implementations here keep the test isolated to the
    span/track_event path; sensitive-tool gating is short-circuited
    (no tool is sensitive in these tests).
    """

    def __init__(self) -> None:
        self.events: list[dict] = []

    def track_event(self, event_type: str, **kwargs) -> None:
        self.events.append({"type": event_type, **kwargs})

    def track_tool(self, tool_name: str, **kwargs) -> None:
        # Commit 33d2b5f wires ``@protect`` to emit a tools/track_tool event
        # after the wrapped body returns. The stub captures that emit the
        # same way it captures span_start/span_end so the dashboard-level
        # assertions keep working unchanged.
        self.events.append({"type": "tool_call", "tool_name": tool_name, **kwargs})

    def check_control_plane(self, workflow_id) -> None:  # noqa: ARG002
        return None

    def check_workflow_budget(self) -> None:
        return None

    def is_sensitive_tool(self, fn_name: str) -> bool:  # noqa: ARG002
        return False

    def execute(self, *args, **kwargs):  # noqa: ARG002
        return None


@pytest.fixture
def recording_runtime():
    """Inject a _RecordingRuntime into the @protect slot."""
    import nullrun.decorators as dec

    rt = _RecordingRuntime()
    dec._runtime = rt
    try:
        yield rt
    finally:
        dec._runtime = None


# ──────────────────────────────────────────────────────────────
# Span hierarchy
# ──────────────────────────────────────────────────────────────


def test_protect_creates_root_span(recording_runtime):
    """Outermost @protect call: parent_span_id is None, depth is 0."""

    @nullrun.protect
    def agent(q):
        return get_current_span()

    span = agent("hello")
    assert span is not None
    assert span.parent_span_id is None
    assert span.depth == 0
    assert span.trace_id
    assert span.span_id


def test_protect_nested_creates_child_span(recording_runtime):
    """A nested @protect call is a child of the outer one (parent_span_id set
    depth=1) AND shares the trace_id."""

    @nullrun.protect
    def orchestrator(q):
        return researcher(q)

    @nullrun.protect
    def researcher(q):
        return get_current_span()

    inner = orchestrator("hello")
    assert inner.parent_span_id is not None
    assert inner.depth == 1

    # Sanity: orchestrator's span is the parent.
    events = recording_runtime.events
    span_starts = [e for e in events if e["type"] == "span_start"]
    fn_names = [e["fn_name"] for e in span_starts]
    assert fn_names == ["orchestrator", "researcher"]
    assert span_starts[0]["span_id"] == inner.parent_span_id
    assert span_starts[0]["trace_id"] == inner.trace_id
    assert span_starts[1]["parent_span_id"] == inner.parent_span_id


def test_protect_restores_context_after_call(recording_runtime):
    """After @protect returns, get_current_span goes back to whatever
    was active before — usually None at the top of the test."""

    @nullrun.protect
    def agent(q):
        return get_current_span().trace_id

    assert get_current_span() is None  # before
    agent("hello")
    assert get_current_span() is None  # after — contextvar is reset


def test_protect_restores_outer_span_on_nested_exit(recording_runtime):
    """When the inner @protect returns, the OUTER span becomes current
    again — not None. This is the whole point of the token-based
    set_span / reset_span pattern."""

    @nullrun.protect
    def outer(q):
        # Inside outer: we are the current span.
        outer_span = get_current_span()
        inner("x")  # this should NOT clobber outer_span
        # After inner returns, outer_span should be current again.
        return outer_span, get_current_span()

    @nullrun.protect
    def inner(q):
        return get_current_span()

    outer_span, after_inner = outer("q")
    assert after_inner is outer_span  # restored, not None


# ──────────────────────────────────────────────────────────────
# Span event emission
# ──────────────────────────────────────────────────────────────


def test_protect_emits_span_start_and_end(recording_runtime):
    """@protect must emit a span_start before the call and span_end after."""

    @nullrun.protect
    def agent(q):
        return q

    agent("hi")
    events = recording_runtime.events
    starts = [e for e in events if e["type"] == "span_start"]
    ends = [e for e in events if e["type"] == "span_end"]
    assert len(starts) == 1
    assert len(ends) == 1
    assert starts[0]["span_id"] == ends[0]["span_id"]
    assert starts[0]["trace_id"] == ends[0]["trace_id"]
    assert starts[0]["fn_name"] == "agent"
    assert "error" not in ends[0] or ends[0]["error"] is None


def test_protect_emits_error_in_span_end(recording_runtime):
    """If the wrapped function raises, span_end carries the error string."""

    @nullrun.protect
    def boom(q):
        raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        boom("x")

    ends = [e for e in recording_runtime.events if e["type"] == "span_end"]
    assert len(ends) == 1
    assert "kaboom" in (ends[0].get("error") or "")


def test_protect_resets_context_even_on_error(recording_runtime):
    """The contextvar is reset in `finally`, so an exception inside
    @protect must not leave a stale span on the stack."""

    @nullrun.protect
    def boom(q):
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        boom("x")
    assert get_current_span() is None


# ──────────────────────────────────────────────────────────────
# Async support
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_protect_async_creates_root_span(recording_runtime):
    """Async @protect wraps the coroutine in a span, returns the result."""

    @nullrun.protect
    async def async_agent(q):
        await asyncio.sleep(0)
        return get_current_span()

    span = await async_agent("hi")
    assert span.parent_span_id is None
    assert span.depth == 0


@pytest.mark.asyncio
async def test_protect_async_nested_child(recording_runtime):
    """Async -> sync @protect still builds the parent/child tree."""

    @nullrun.protect
    async def outer(q):
        return await inner(q)

    @nullrun.protect
    async def inner(q):
        return get_current_span()

    inner_span = await outer("q")
    assert inner_span.depth == 1
    assert inner_span.parent_span_id is not None


# T3-S2 (0.3.0): `test_protect_with_noop_runtime_allows` and
# `test_protect_with_noop_runtime_async` were removed along with
# `NullRunNoop` itself. Every runtime is now a real `NullRunRuntime`
# with a bound workflow — there is no "tolerate a stub" branch to test.


# ──────────────────────────────────────────────────────────────
# Decorator shape (must work with @protect AND @protect )
# ──────────────────────────────────────────────────────────────


def test_protect_with_empty_parens(recording_runtime):
    """`@nullrun.protect()` is the same as `@nullrun.protect`."""

    @nullrun.protect()
    def agent(q):
        return get_current_span()

    span = agent("x")
    assert span.parent_span_id is None


def test_protect_preserves_function_metadata(recording_runtime):
    """`@protect` must not strip __name__ / __doc__ from the wrapped fn."""

    @nullrun.protect
    def my_documented_func():
        """Important docstring."""
        return 1

    assert my_documented_func.__name__ == "my_documented_func"
    assert "Important docstring" in (my_documented_func.__doc__ or "")


# ──────────────────────────────────────────────────────────────
# Manually-set span is preserved (don't clobber explicit context)
# ──────────────────────────────────────────────────────────────


def test_protect_respects_externally_set_span(recording_runtime):
    """If user code manually calls set_span(...) before @protect fires
    the new span is a child of THAT, not a root."""
    from nullrun.tracing import create_root_span as make_root

    outer = make_root()
    token = set_span(outer)
    try:

        @nullrun.protect
        def inner(q):
            return get_current_span()

        span = inner("x")
        assert span.parent_span_id == outer.span_id
        assert span.trace_id == outer.trace_id
        assert span.depth == 1
    finally:
        reset_span(token)


# ──────────────────────────────────────────────────────────────
# Re-init wiring (regression: stale runtime in @protect cache)
# ──────────────────────────────────────────────────────────────


def test_init_replaces_stale_decorator_runtime_cache(mock_api):
    """`nullrun.init` must update the @protect decorator's own module-level cache.

    Pre-seed `decorators._runtime` with a sentinel that raises on
    `track_event`, then call `init`. If the fix is in place, init
    overwrites the slot and the sentinel is never reachable.
    """
    import nullrun.decorators as _dec

    class _DeadSentinel:
        """A pre-seeded cache slot that raises if @protect ever uses it."""

        def track_event(self, *args, **kwargs):  # noqa: ARG002
            raise AssertionError(
                "decorators._runtime was not refreshed by init(); "
                "the @protect cache is still pointing at a stale runtime."
            )

    _dec._runtime = _DeadSentinel()

    rt = nullrun.init(
        api_key="test-key-12345678",
        api_url="https://api.test.nullrun.io",
    )
    try:
        # The fix: init must overwrite the decorator's cache slot.
        # Without the fix, this assertion fails because the slot
        # still points at _DeadSentinel.
        assert _dec._runtime is rt, (
            "init() did not update decorators._runtime; "
            "the @protect cache is still pointing at a stale runtime."
        )
        assert not isinstance(_dec._runtime, _DeadSentinel)
    finally:
        _dec._runtime = None
        try:
            rt.shutdown()
        except Exception:
            pass


def test_protect_uses_new_runtime_after_reinit(mock_api):
    """After init → shutdown → init, @protect emits span events to the NEW runtime, not the dead one."""
    import nullrun.decorators as _dec

    first_runtime = _RecordingRuntime()

    # Simulate the first init cycle: pre-seed the cache, run a @protect
    # call (events go to first_runtime), then "shut down" by replacing
    # the cache with a dead sentinel.
    _dec._runtime = first_runtime
    try:

        @nullrun.protect
        def step_a():
            return "a"

        assert step_a() == "a"
    finally:
        first_runtime.events.clear()

    class _DeadRuntime:
        def track_event(self, *args, **kwargs):  # noqa: ARG002
            raise AssertionError("dead runtime called by @protect after re-init")

    _dec._runtime = _DeadRuntime()

    # Re-init must refresh the cache. After this, calling @protect
    # routes to the new runtime, not _DeadRuntime.
    rt = nullrun.init(
        api_key="test-key-12345678",
        api_url="https://api.test.nullrun.io",
    )
    try:
        assert _dec._runtime is rt

        @nullrun.protect
        def step_b():
            return "b"

        assert step_b() == "b"
        # If the regression were live, step_b would have raised inside
        # _emit_span_start via the _DeadRuntime.track_event AssertionError.
    finally:
        _dec._runtime = None
        try:
            rt.shutdown()
        except Exception:
            pass


# ─── protect edge cases (re-init, fail-open, kill/pause) ─────────────────────────
"""
Additional tests for ``nullrun.decorators`` — branch coverage for the
``_safe_args`` / ``_strip_details_balanced`` / ``_enforce_sensitive_tool``
helpers, the fail-CLOSED / fail-OPEN contract, the KILL→BlockedException
unification, and the ``@protect `` paren-form.
"""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nullrun.breaker.exceptions import (
    NullRunBlockedException,
    NullRunTransportError,
    TransportErrorSource,
    WorkflowKilledInterrupt,
    WorkflowPausedException,
)
from nullrun.decorators import (
    SENSITIVE_ARG_KEYS,
    _enforce_sensitive_tool,
    _safe_args,
    _safe_error_str,
    _safe_kwargs,
    _safe_repr,
    _strip_details_balanced,
    protect,
    sensitive,
)
from nullrun.runtime import NullRunRuntime


@pytest.fixture
def test_runtime(monkeypatch, tmp_path):
    """Provide a runtime in test mode so get_runtime returns without
    authenticating against a real server.

    Replays any WAL left over from previous test runs in a
    tmp_path-scoped WAL file so the constructor's
    ``_replay_from_wal`` never reads ``~/.nullrun/sdk.wal`` and
    flushes real on-disk events to a live API. This avoids the
    cross-Python-version flake seen on CI in 2026-07-11 where
    3.11 picked up a stale WAL from a 3.10/3.12 worker that
    finished without explicitly clearing it.
    """
    monkeypatch.setenv("NULLRUN_API_KEY", "test-key-12345678")
    monkeypatch.setenv("NULLRUN_WAL_PATH", str(tmp_path / "sdk.wal"))
    NullRunRuntime.reset_instance()
    rt = NullRunRuntime(api_key="test-key-12345678", _test_mode=True)
    rt.organization_id = "org-1"
    # Stub the transport so the network is never touched in tests.
    # - ``_do_flush`` overrides the public flush.
    # - ``_do_flush_locked`` is what ``track `` calls when the buffer
    # fills — must also be stubbed to be safe.
    # - ``_client`` is the httpx client — magicmock so even a stray
    # ``post`` raises a clean AttributeError instead of hitting the API.
    rt._transport._do_flush = lambda: None
    rt._transport._do_flush_locked = lambda: None
    rt._transport._client = MagicMock()
    NullRunRuntime._instance = rt
    yield rt
    NullRunRuntime.reset_instance()


# ─── _safe_repr ───────────────────────────────────────────────────────


def test_safe_repr_short_value_passes_through(test_runtime):
    """Under the 50-char cap, value flows through unmodified."""
    s = _safe_repr("hi")
    assert s == "'hi'"


def test_safe_repr_long_value_truncated(test_runtime):
    """Over 50 chars, suffix ``...<truncated>`` appended."""
    s = _safe_repr("x" * 200, max_len=50)
    assert s.endswith("...<truncated>")
    assert len(s) > 50


def test_safe_repr_redacts_details_before_truncating(test_runtime):
    """``details={PAN: '4111-...'}`` must be redacted BEFORE truncation."""
    # String kept under the 50-char cap so the redact survives the
    # truncate step (otherwise we'd only verify truncation).
    secret = "4111-1111-1111-1111"
    payload = f"x details={{'card': '{secret}'}}"
    out = _safe_repr(payload, max_len=50)
    assert secret not in out
    assert "<redacted>" in out


# ─── _safe_kwargs ────────────────────────────────────────────────────


def test_safe_kwargs_masks_sensitive_keys(test_runtime):
    out = _safe_kwargs({"password": "p", "token": "t", "user": "alice"})
    assert out["password"] == "***"
    assert out["token"] == "***"
    # Non-sensitive values go through _safe_repr → ``repr ``.
    assert out["user"] == "'alice'"


def test_safe_kwargs_is_case_insensitive(test_runtime):
    out = _safe_kwargs({"PASSWORD": "p", "Token": "t"})
    assert out["PASSWORD"] == "***"
    assert out["Token"] == "***"


# ─── _safe_args ──────────────────────────────────────────────────────


def test_safe_args_masks_positional_sensitive_param(test_runtime):
    """Positional sensitive param (e.g. ``credit_card_number``) is masked."""

    def charge(credit_card_number, amount):
        return amount

    masked = _safe_args(charge, ("4111-1111-1111-1111", 50))
    assert masked[0] == "***"
    # ``repr(50)`` is ``"50"``.
    assert masked[1] == "50"


def test_safe_args_trailing_extra_args_uses_safe_repr():
    """``*args``-style callable: extra positional args use safe_repr."""

    def variadic(*args, **kwargs):
        return args

    masked = _safe_args(variadic, ("x", "ok"))
    # ``*args`` has no name → safe_repr for both (no masking).
    assert masked[0] == "'x'"
    assert masked[1] == "'ok'"


def test_safe_args_no_signature_falls_back_to_safe_repr():
    """C-extension / built-in without signature → safe_repr on all."""

    class _NoSig:
        # Builtin-ish class; ``inspect.signature`` raises ValueError.
        pass

    masked = _safe_args(_NoSig, ("4111", 50))
    assert masked[0] == "'4111'"
    assert masked[1] == "50"


def test_safe_args_signature_raises_typeerror_falls_back():
    """``inspect.signature`` raises ``TypeError`` for some callables."""

    class _Bad:
        # Trigger ValueError path.
        __signature__ = None  # type: ignore[assignment]

    masked = _safe_args(_Bad, ("x",))
    assert masked == ["'x'"]


# ─── _strip_details_balanced ─────────────────────────────────────────


def test_strip_details_balanced_no_details_unchanged():
    s = "no details here"
    assert _strip_details_balanced(s) == s


def test_strip_details_balanced_details_without_brace_unchanged():
    s = "details=plain text without braces"
    # No '{' after 'details=' → left as-is.
    assert _strip_details_balanced(s) == s


def test_strip_details_balanced_simple_payload(test_runtime):
    s = "context=ok details={'a': 1, 'b': 2}"
    out = _strip_details_balanced(s)
    assert "<redacted>" in out
    assert "'a': 1" not in out


def test_strip_details_balanced_nested_dicts(test_runtime):
    """Nested dicts in the details payload → still redacted as a unit."""
    s = "msg details={'a': {'b': {'c': 'secret'}}}"
    out = _strip_details_balanced(s)
    assert "secret" not in out
    assert "<redacted>" in out


def test_strip_details_balanced_string_with_braces_inside(test_runtime):
    """A string value containing ``{`` / ``}`` does NOT break the brace walker."""
    s = 'msg details={"key": "value with { and } inside"}'
    out = _strip_details_balanced(s)
    assert "value with { and } inside" not in out
    assert "<redacted>" in out


def test_strip_details_balanced_multiple_details(test_runtime):
    """Two ``details={...}`` substrings in the same string → both redacted."""
    s = "first details={'a': 1} middle details={'b': 2}"
    out = _strip_details_balanced(s)
    assert out.count("<redacted>") == 2


def test_strip_details_balanced_escaped_quote_in_string(test_runtime):
    r"""A string with an escaped quote (\") is handled by the walker."""
    s = r'msg details={"key": "val\"ue"}'
    out = _strip_details_balanced(s)
    assert "<redacted>" in out


# ─── _safe_error_str ─────────────────────────────────────────────────


def test_safe_error_str_none_returns_none(test_runtime):
    assert _safe_error_str(None) is None


def test_safe_error_str_simple_message_passes_through(test_runtime):
    e = RuntimeError("plain")
    assert _safe_error_str(e) == "plain"


def test_safe_error_str_details_redacted(test_runtime):
    e = RuntimeError("oops details={'secret': 'value'}")
    out = _safe_error_str(e)
    assert "secret" not in out
    assert "<redacted>" in out


# ─── _enforce_sensitive_tool ────────────────────────────────────────


def test_enforce_sensitive_tool_non_sensitive_returns(test_runtime):
    """Non-sensitive tool → no-op, no runtime call."""
    rt = MagicMock()
    rt.is_sensitive_tool.return_value = False
    rt.execute = MagicMock()
    _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
    rt.execute.assert_not_called()


def test_enforce_sensitive_tool_real_block_propagates(test_runtime):
    """``decision=block`` from gateway → raises NullRunBlockedException."""
    rt = MagicMock()
    rt.is_sensitive_tool.return_value = True
    rt.execute.side_effect = NullRunBlockedException(workflow_id="wf-1", reason="denied")
    with pytest.raises(NullRunBlockedException):
        _enforce_sensitive_tool(rt, lambda x: x, (1,), {})


def test_enforce_sensitive_tool_transport_error_fail_closed(test_runtime):
    """``NullRunTransportError`` + no fail-open → raises NullRunBlockedException."""
    rt = MagicMock()
    rt.is_sensitive_tool.return_value = True
    rt.execute.side_effect = NullRunTransportError(
        "down",
        source=TransportErrorSource.NETWORK_ERROR,
        endpoint="/execute",
    )
    with pytest.raises(NullRunBlockedException) as excinfo:
        _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
    assert "NETWORK_ERROR" in excinfo.value.reason


def test_enforce_sensitive_tool_transport_error_fail_open(test_runtime, monkeypatch):
    """``NULLRUN_SENSITIVE_FAIL_OPEN=1`` + transport error → body runs."""
    monkeypatch.setenv("NULLRUN_SENSITIVE_FAIL_OPEN", "1")
    rt = MagicMock()
    rt.is_sensitive_tool.return_value = True
    rt.execute.side_effect = NullRunTransportError(
        "down",
        source=TransportErrorSource.NETWORK_ERROR,
        endpoint="/execute",
    )
    # Must NOT raise.
    _enforce_sensitive_tool(rt, lambda x: x, (1,), {})


def test_enforce_sensitive_tool_generic_exception_fail_closed(test_runtime):
    """Non-transport exception → NullRunBlockedException."""
    rt = MagicMock()
    rt.is_sensitive_tool.return_value = True
    rt.execute.side_effect = ValueError("oops")
    with pytest.raises(NullRunBlockedException):
        _enforce_sensitive_tool(rt, lambda x: x, (1,), {})


def test_enforce_sensitive_tool_generic_exception_fail_open(test_runtime, monkeypatch):
    """Generic exception + fail-open → no raise."""
    monkeypatch.setenv("NULLRUN_SENSITIVE_FAIL_OPEN", "1")
    rt = MagicMock()
    rt.is_sensitive_tool.return_value = True
    rt.execute.side_effect = ValueError("oops")
    _enforce_sensitive_tool(rt, lambda x: x, (1,), {})  # no raise


def test_enforce_sensitive_tool_dict_with_fallback_decision_source(test_runtime):
    """``decision_source`` starts with FALLBACK_ → raises."""
    rt = MagicMock()
    rt.is_sensitive_tool.return_value = True
    rt.execute.return_value = {
        "decision": "allow",
        "decision_source": "FALLBACK_NETWORK_ERROR",
    }
    with pytest.raises(NullRunBlockedException):
        _enforce_sensitive_tool(rt, lambda x: x, (1,), {})


def test_enforce_sensitive_tool_dict_with_typed_error_source(test_runtime):
    """``decision_source`` ∈ TransportErrorSource values → raises."""
    rt = MagicMock()
    rt.is_sensitive_tool.return_value = True
    rt.execute.return_value = {
        "decision": "allow",
        "decision_source": TransportErrorSource.GATEWAY_ERROR,
    }
    with pytest.raises(NullRunBlockedException):
        _enforce_sensitive_tool(rt, lambda x: x, (1,), {})


def test_enforce_sensitive_tool_dict_with_fallback_fail_open(test_runtime, monkeypatch):
    """``decision_source`` FALLBACK_* + fail-open → no raise."""
    monkeypatch.setenv("NULLRUN_SENSITIVE_FAIL_OPEN", "1")
    rt = MagicMock()
    rt.is_sensitive_tool.return_value = True
    rt.execute.return_value = {
        "decision": "allow",
        "decision_source": "FALLBACK_NETWORK_ERROR",
    }
    _enforce_sensitive_tool(rt, lambda x: x, (1,), {})  # no raise


def test_enforce_sensitive_tool_dict_with_gateway_decision_falls_through(test_runtime):
    """``decision_source=gateway`` + ``decision=allow`` → no raise."""
    rt = MagicMock()
    rt.is_sensitive_tool.return_value = True
    rt.execute.return_value = {
        "decision": "allow",
        "decision_source": "gateway",
    }
    _enforce_sensitive_tool(rt, lambda x: x, (1,), {})  # no raise


def test_enforce_sensitive_tool_sensitive_kwargs_masked_in_call(test_runtime):
    """``password`` kwarg on a sensitive tool is masked before /execute."""
    rt = MagicMock()
    rt.is_sensitive_tool.return_value = True
    rt.execute.return_value = {"decision": "allow", "decision_source": "gateway"}
    _enforce_sensitive_tool(rt, lambda x: x, (), {"password": "p", "user": "alice"})
    # ``runtime.execute`` is called positionally: ``(tool_name, input_data,...)``.
    forwarded = rt.execute.call_args.args[1]
    assert forwarded["kwargs"]["password"] == "***"
    # Non-sensitive → safe_repr → ``"'alice'"``.
    assert forwarded["kwargs"]["user"] == "'alice'"


def test_enforce_sensitive_tool_sensitive_positional_arg_masked(test_runtime):
    """``credit_card_number`` positional on a sensitive tool is masked."""
    rt = MagicMock()
    rt.is_sensitive_tool.return_value = True
    rt.execute.return_value = {"decision": "allow", "decision_source": "gateway"}

    def charge(credit_card_number, amount):
        return amount

    _enforce_sensitive_tool(rt, charge, ("4111-1111-1111-1111", 50), {})
    forwarded = rt.execute.call_args.args[1]
    assert forwarded["args"][0] == "***"


# ─── @protect paren-form ─────────────────────────────────────────────


def test_protect_with_parens_returns_decorator(test_runtime):
    """``@protect()`` with empty parens works just like ``@protect``."""
    # Stub track_event so the finally-block span emission does not
    # re-enter check_control_plane with our mocked side effect.
    test_runtime.track_event = MagicMock()

    @protect()
    def f(x):
        return x * 2

    assert f(3) == 6


def test_protect_without_parens_wraps_directly(test_runtime):
    """``@protect`` without parens wraps the function directly."""
    # Stub track_event so the finally-block span emission does not
    # re-enter check_control_plane with our mocked side effect.
    test_runtime.track_event = MagicMock()

    @protect
    def f(x):
        return x * 2

    assert f(3) == 6


# ─── KILL→BlockedException unification ──────────────────────


def test_protect_sync_kill_raises_NullRunBlockedException(test_runtime):
    """``WorkflowKilledInterrupt`` from gate → unified as NullRunBlockedException."""
    from nullrun import decorators as dec_mod

    rt = NullRunRuntime(api_key="test-key-12345678", _test_mode=True)
    rt.track_event = MagicMock()
    rt.check_control_plane = MagicMock(
        side_effect=WorkflowKilledInterrupt(workflow_id="wf-1", reason="admin kill")
    )
    rt.check_workflow_budget = MagicMock()
    dec_mod._runtime = rt

    @protect
    def f():
        return "should not run"

    with pytest.raises(NullRunBlockedException) as excinfo:
        f()
    assert excinfo.value.reason == "admin kill"


def test_protect_sync_pause_raises_NullRunBlockedException(test_runtime):
    """``WorkflowPausedException`` from gate → unified as NullRunBlockedException."""
    from nullrun import decorators as dec_mod

    rt = NullRunRuntime(api_key="test-key-12345678", _test_mode=True)
    rt.track_event = MagicMock()
    rt.check_control_plane = MagicMock(
        side_effect=WorkflowPausedException(workflow_id="wf-1", reason="budget pause")
    )
    rt.check_workflow_budget = MagicMock()
    dec_mod._runtime = rt

    @protect
    def f():
        return "should not run"

    with pytest.raises(NullRunBlockedException) as excinfo:
        f()
    assert excinfo.value.reason == "budget pause"


@pytest.mark.asyncio
async def test_protect_async_kill_re_raises_WorkflowKilledInterrupt(make_test_runtime):
    """Async wrapper does NOT unify — kill signal propagates as-is so async frameworks can interrupt cleanly."""
    from nullrun import decorators as dec_mod

    rt = make_test_runtime()
    rt.track_event = MagicMock()
    rt.check_control_plane = MagicMock(
        side_effect=WorkflowKilledInterrupt(workflow_id="wf-1", reason="x")
    )
    rt.check_workflow_budget = MagicMock()
    dec_mod._runtime = rt

    @protect
    async def f():
        return "ok"

    with pytest.raises(WorkflowKilledInterrupt):
        await f()


# ─── @sensitive decorator ────────────────────────────────────────────


def test_sensitive_registers_tool_with_runtime(test_runtime):
    """``@sensitive`` calls ``add_sensitive_tool`` on the runtime."""

    @sensitive
    def my_charge(amount):
        return amount

    rt = NullRunRuntime.get_instance()
    assert "my_charge" in rt.get_sensitive_tools()


def test_sensitive_runtime_init_failure_raises(test_runtime, monkeypatch):
    """If runtime construction fails inside @sensitive, raises RuntimeError (fail-CLOSED, ADR-008)."""
    from nullrun import decorators

    original_exc = RuntimeError("x")
    monkeypatch.setattr(
        decorators,
        "_get_or_create_runtime",
        MagicMock(side_effect=original_exc),
    )

    with pytest.raises(
        RuntimeError,
        match=r"@sensitive registration failed for 'f'",
    ) as excinfo:

        @sensitive
        def f():
            return 1

    assert excinfo.value.__cause__ is original_exc


# ─── reset ──────────────────────────────────────────────────────────


def test_reset_clears_runtime_slot(test_runtime, monkeypatch):
    """``reset()`` shuts down the runtime and clears the module-level slot."""
    from nullrun import decorators

    rt = NullRunRuntime.get_instance()
    decorators._runtime = rt
    decorators.reset()
    assert decorators._runtime is None


def test_reset_when_no_runtime_is_silent(test_runtime):
    from nullrun import decorators

    decorators._runtime = None
    decorators.reset()  # must not raise


def test_reset_shutdown_failure_is_silent(test_runtime, monkeypatch):
    """``reset()`` swallows runtime shutdown exceptions."""
    from nullrun import decorators

    rt = MagicMock()
    rt.shutdown.side_effect = RuntimeError("oops")
    decorators._runtime = rt
    decorators.reset()  # must not raise
    assert decorators._runtime is None


# ─── get_protected_runtime ──────────────────────────────────────────


def test_get_protected_runtime_returns_runtime(test_runtime):
    from nullrun import decorators

    rt = NullRunRuntime.get_instance()
    decorators._runtime = rt
    assert decorators.get_protected_runtime() is rt


def test_get_protected_runtime_falls_back_to_get_runtime(monkeypatch, make_test_runtime):
    """When the decorator slot is empty, fall back to the global singleton."""
    from nullrun import decorators

    decorators._runtime = None
    NullRunRuntime._instance = make_test_runtime()
    try:
        out = decorators.get_protected_runtime()
        assert out is NullRunRuntime._instance
    finally:
        NullRunRuntime.reset_instance()
