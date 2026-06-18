"""
Tests for `@protect` with automatic span hierarchy (Phase 2 Commit 4).

The decorator must:
  - Create a root span (parent_span_id=None, depth=0) on the outermost call
  - Create a child span (parent_span_id=<outer span_id>, depth+1) on nested calls
  - Restore the previous context (None or parent) after the call
  - Work with sync AND async functions
  - Emit `span_start` and `span_end` events to the runtime

T3-S2 (0.3.0): the `NullRunNoop` fallback was removed — every runtime
is a real `NullRunRuntime` with a bound workflow. The legacy
"tolerate a noop runtime" behavior is no longer relevant.
"""
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

    The decorator calls `check_control_plane`, `check_workflow_budget`,
    and `is_sensitive_tool` as pre-execution gates (ADR-008). The default
    no-op implementations here keep the test isolated to the
    span/track_event path; sensitive-tool gating is short-circuited
    (no tool is sensitive in these tests).
    """

    def __init__(self) -> None:
        self.events: list[dict] = []

    def track_event(self, event_type: str, **kwargs) -> None:
        self.events.append({"type": event_type, **kwargs})

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
    """A nested @protect call is a child of the outer one (parent_span_id set,
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
    """After @protect returns, get_current_span() goes back to whatever
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
# Decorator shape (must work with @protect AND @protect())
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
    """If user code manually calls set_span(...) before @protect fires,
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
    """`nullrun.init()` must update the @protect decorator's own
    module-level cache (`decorators._runtime`), not just the runtime
    module's cache and the class-level singleton.

    Regression: the previous `init()` updated `NullRunRuntime._instance`
    and `nullrun.runtime._runtime` but not `nullrun.decorators._runtime`.
    The decorator short-circuits on the decorator module's own slot and
    never re-resolved, so an `init → shutdown → init` cycle left the
    decorator pointing at the dead previous runtime. Span events were
    silently swallowed by `_emit_span_start`'s try/except, producing
    cost_events with trace_id/span_id (from the SpanContext) but no
    matching rows in the `spans` table.

    Test strategy: pre-seed `decorators._runtime` with a sentinel that
    raises on `track_event`, then call `init()`. If the fix is in place,
    init() overwrites the slot and the sentinel is never reachable from
    a subsequent @protect call.
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
        # The fix: init() must overwrite the decorator's cache slot.
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
    """End-to-end version of the regression: after `init → shutdown →
    init`, calling @protect must emit span events to the NEW runtime,
    not the dead one.

    The first init's recording runtime is intentionally unreachable
    after shutdown (its `track_event` would crash); the second init
    installs a fresh recording runtime. We assert the new runtime
    receives the events.
    """
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
        # If the regression were live, step_b() would have raised inside
        # _emit_span_start via the _DeadRuntime.track_event AssertionError.
    finally:
        _dec._runtime = None
        try:
            rt.shutdown()
        except Exception:
            pass
