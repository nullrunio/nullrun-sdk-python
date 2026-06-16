"""
Tests for span-context attachment in track_llm / track_tool.

Phase 2 Commit 5: track_llm and track_tool must auto-include
`trace_id` / `span_id` (and `parent_span_id` / `depth`) from the
active SpanContext set by `@protect` or a manual `set_span`. This
lets the backend render LLM/tool calls under the right node of the
trace timeline without the user threading IDs through every call.

If no span is active, the fields are omitted from the event and the
existing `_enrich_event` fallback generates fresh IDs from the
loose contextvars (or synthesises new ones).
"""
from types import SimpleNamespace
from typing import List

import pytest

from nullrun.tracing import (
    create_child_span,
    create_root_span,
    reset_span,
    set_span,
)


# ──────────────────────────────────────────────────────────────
# Capture events from the runtime
# ──────────────────────────────────────────────────────────────

@pytest.fixture
def capturing_runtime(make_runtime, mock_api):
    """
    A runtime that records every event passed to its `track()`.

    We monkey-patch the *instance* method (not the class) so the rest
    of the runtime (transport, breaker, enrichment) still runs as
    normal — the patch is just an observer. The real `track` is
    captured and re-invoked so the runtime's own bookkeeping works.
    """
    rt = make_runtime()
    events: List[dict] = []

    original_track = rt.track

    def capturing_track(event: dict) -> dict:
        # Shallow copy — the runtime mutates the dict after this
        # call (enrichment, dedup, etc.), so we snapshot it.
        events.append(dict(event))
        return original_track(event)

    rt.track = capturing_track  # type: ignore[method-assign]

    # Return a small namespace so tests can grab both pieces. We use
    # SimpleNamespace rather than an inner class because `events = events`
    # in a class body would shadow the outer `events` name (class bodies
    # don't follow LEGB lookup like functions do).
    return SimpleNamespace(runtime=rt, events=events)


# ──────────────────────────────────────────────────────────────
# track_llm span context
# ──────────────────────────────────────────────────────────────

def test_track_llm_attaches_active_span(capturing_runtime):
    """track_llm inside an active SpanContext tags the event with
    trace_id / span_id / parent_span_id / depth."""
    span = create_root_span()
    token = set_span(span)
    try:
        capturing_runtime.runtime.track_llm(input_tokens=10, output_tokens=5, model="gpt-4o")
    finally:
        reset_span(token)

    assert len(capturing_runtime.events) == 1
    event = capturing_runtime.events[0]
    assert event["trace_id"] == span.trace_id
    assert event["span_id"] == span.span_id
    # Root span: no parent.
    assert event["parent_span_id"] is None
    assert event["depth"] == 0


def test_track_llm_nested_span_has_parent(capturing_runtime):
    """Inside a child span, the event's parent_span_id is the
    child's parent — i.e. the outer @protect's span."""
    outer = create_root_span()
    outer_token = set_span(outer)
    try:
        inner = create_child_span(outer)
        inner_token = set_span(inner)
        try:
            capturing_runtime.runtime.track_llm(input_tokens=1, output_tokens=1)
        finally:
            reset_span(inner_token)
    finally:
        reset_span(outer_token)

    event = capturing_runtime.events[0]
    assert event["trace_id"] == outer.trace_id  # same trace
    assert event["span_id"] == inner.span_id    # current span
    assert event["parent_span_id"] == outer.span_id
    assert event["depth"] == 1


def test_track_llm_no_active_span_omits_span_fields(capturing_runtime):
    """Outside any @protect / set_span, track_llm must NOT add
    trace_id / span_id (the enrichment path will generate fresh ones)."""
    capturing_runtime.runtime.track_llm(input_tokens=10, output_tokens=5)

    event = capturing_runtime.events[0]
    assert "trace_id" not in event
    assert "span_id" not in event
    assert "parent_span_id" not in event
    assert "depth" not in event


def test_track_llm_output_tokens_default_zero(capturing_runtime):
    """output_tokens defaults to 0 — embeddings / completion-less calls
    don't have to pass it."""
    capturing_runtime.runtime.track_llm(input_tokens=100)
    event = capturing_runtime.events[0]
    assert event["input_tokens"] == 100
    assert event["output_tokens"] == 0
    # Legacy aggregate `tokens` is still set for the wire format.
    assert event["tokens"] == 100


def test_track_llm_keyword_only_kwargs(capturing_runtime):
    """model / latency_ms / metadata are keyword-only after the `*`.
    Positional calls to those would TypeError; we test that the
    keyword path still works."""
    capturing_runtime.runtime.track_llm(
        input_tokens=50,
        output_tokens=20,
        model="claude-3",
        latency_ms=300,
        metadata={"region": "us-east-1"},
    )
    event = capturing_runtime.events[0]
    assert event["model"] == "claude-3"
    assert event["latency_ms"] == 300
    assert event["metadata"] == {"region": "us-east-1"}


# ──────────────────────────────────────────────────────────────
# track_tool span context
# ──────────────────────────────────────────────────────────────

def test_track_tool_attaches_active_span(capturing_runtime):
    """Same span-tag behaviour as track_llm."""
    span = create_root_span()
    token = set_span(span)
    try:
        capturing_runtime.runtime.track_tool(tool_name="web_search", duration_ms=200)
    finally:
        reset_span(token)

    event = capturing_runtime.events[0]
    assert event["trace_id"] == span.trace_id
    assert event["span_id"] == span.span_id
    assert event["parent_span_id"] is None
    assert event["depth"] == 0


def test_track_tool_no_active_span_omits_span_fields(capturing_runtime):
    """Outside a span, no trace/span fields are added."""
    capturing_runtime.runtime.track_tool(tool_name="calculator")
    event = capturing_runtime.events[0]
    assert "trace_id" not in event
    assert "span_id" not in event


def test_track_tool_is_retry_flag(capturing_runtime):
    """is_retry is preserved on the event (passed through)."""
    span = create_root_span()
    token = set_span(span)
    try:
        capturing_runtime.runtime.track_tool(
            tool_name="flaky_api",
            duration_ms=500,
            is_retry=True,
        )
    finally:
        reset_span(token)

    event = capturing_runtime.events[0]
    assert event["is_retry"] is True
    assert event["tool_name"] == "flaky_api"
    # The runtime sends `latency_ms` on the wire (backend compat) but
    # the public kwarg is `duration_ms`.
    assert event["latency_ms"] == 500


# ──────────────────────────────────────────────────────────────
# Module-level track_llm / track_tool
# ──────────────────────────────────────────────────────────────

def test_module_level_track_llm_attaches_span(capturing_runtime, monkeypatch):
    """The module-level `nullrun.track_llm` should also pick up the
    active span — it forwards to the runtime method, which is where
    the span attachment lives."""
    from nullrun import runtime as runtime_mod

    # Replace the runtime getter with our capturing wrapper so module-
    # level calls land in the same buffer as the method-level ones.
    monkeypatch.setattr(runtime_mod, "get_runtime", lambda: capturing_runtime.runtime)

    span = create_root_span()
    token = set_span(span)
    try:
        runtime_mod.track_llm(input_tokens=7, output_tokens=3)
    finally:
        reset_span(token)

    event = capturing_runtime.events[0]
    assert event["trace_id"] == span.trace_id
    assert event["span_id"] == span.span_id


def test_module_level_track_llm_output_tokens_optional(mock_api):
    """Calling `nullrun.track_llm(input_tokens=N)` with no output_tokens
    must not TypeError — the kwarg now defaults to 0.

    Depends on `mock_api` so respx covers `/track/batch`. We also call
    `nullrun.init(...)` so whatever singleton the module-level
    `track_llm` resolves points at the mocked URL — without this, a
    stale singleton from a previous test (or a fresh one built from
    env defaults) targets the prod URL and respx raises
    AllMockedAssertionError."""
    from tests.conftest import BASE_URL

    import nullrun

    nullrun.init(api_key="test-key-12345678", api_url=BASE_URL)
    nullrun.track_llm(input_tokens=42)  # smoke test — no exception


# ──────────────────────────────────────────────────────────────
# End-to-end with @protect
# ──────────────────────────────────────────────────────────────

def test_protect_then_track_llm_attaches_to_protect_span(capturing_runtime, monkeypatch):
    """The integration story: @protect opens a span, a track_llm
    inside it inherits that span — no manual plumbing needed."""
    import nullrun
    from nullrun import runtime as runtime_mod
    from nullrun.decorators import reset as reset_decorator_runtime

    import nullrun.decorators as dec
    # Wire both: the @protect emit path (uses dec._runtime) AND the
    # module-level nullrun.track_llm path (uses runtime_mod.get_runtime).
    dec._runtime = capturing_runtime.runtime
    monkeypatch.setattr(runtime_mod, "get_runtime", lambda: capturing_runtime.runtime)
    try:
        @nullrun.protect
        def agent(q):
            nullrun.track_llm(input_tokens=20, output_tokens=10, model="gpt-4o")
            return "ok"

        agent("hi")
    finally:
        reset_decorator_runtime()

    # We expect: span_start(agent) + llm_call + span_end(agent)
    types = [e["type"] for e in capturing_runtime.events]
    assert "span_start" in types
    assert "span_end" in types
    assert "llm_call" in types

    span_start = next(e for e in capturing_runtime.events if e["type"] == "span_start")
    llm_call = next(e for e in capturing_runtime.events if e["type"] == "llm_call")
    span_end = next(e for e in capturing_runtime.events if e["type"] == "span_end")

    # llm_call is attributed to agent's span.
    assert llm_call["trace_id"] == span_start["trace_id"]
    assert llm_call["span_id"] == span_start["span_id"]
    assert llm_call["parent_span_id"] is None
    assert llm_call["depth"] == 0

    # span_end matches span_start.
    assert span_end["span_id"] == span_start["span_id"]
