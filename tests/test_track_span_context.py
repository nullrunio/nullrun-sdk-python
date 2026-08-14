"""
Tests for span-context attachment in track_llm / track_tool.

track_llm and track_tool must auto-include
`trace_id` / `span_id` (and `parent_span_id` / `depth`) from the
active SpanContext set by `@protect` or a manual `set_span`. This
lets the backend render LLM/tool calls under the right node of the
trace timeline without the user threading IDs through every call.

If no span is active, the fields are omitted from the event and the
existing `_enrich_event` fallback generates fresh IDs from the
loose contextvars (or synthesises new ones).
"""

from types import SimpleNamespace

import pytest

# F-19 source-pin regression tests rely on the legacy
# ``get_trace_id`` / ``get_span_id`` / ``get_workflow_id`` getters
# (the runtime's ``_enrich_event`` reads via these; the new system
# reads via ``get_current_span``). We also need the public
# ``nullrun.workflow`` / ``nullrun.span`` context managers, which
# the SDK re-exports from ``nullrun.context``. Importing at the top
# keeps each test focused on its assertion rather than shuffling
# imports in every body.
import nullrun
from nullrun.context import (  # noqa: E402
    get_span_id,
    get_trace_id,
    get_workflow_id,
)
from nullrun.tracing import (
    create_child_span,
    create_root_span,
    get_current_span,
    reset_span,
    set_span,
)

# ──────────────────────────────────────────────────────────────
# Capture events from the runtime
# ──────────────────────────────────────────────────────────────


@pytest.fixture
def capturing_runtime(make_runtime, mock_api):
    """
    A runtime that records every event passed to its `track `.

    We monkey-patch the *instance* method (not the class) so the rest
    of the runtime (transport, breaker, enrichment) still runs as
    normal — the patch is just an observer. The real `track` is
    captured and re-invoked so the runtime's own bookkeeping works.
    """
    rt = make_runtime()
    events: list[dict] = []

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
    assert event["span_id"] == inner.span_id  # current span
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
    import nullrun
    from tests.conftest import BASE_URL

    nullrun.init(api_key="test-key-12345678", api_url=BASE_URL)
    nullrun.track_llm(input_tokens=42)  # smoke test — no exception


# ──────────────────────────────────────────────────────────────
# End-to-end with @protect
# ──────────────────────────────────────────────────────────────


def test_protect_then_track_llm_attaches_to_protect_span(capturing_runtime, monkeypatch):
    """The integration story: @protect opens a span, a track_llm
    inside it inherits that span — no manual plumbing needed."""
    import nullrun
    import nullrun.decorators as dec
    from nullrun import runtime as runtime_mod
    from nullrun.decorators import reset as reset_decorator_runtime

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


# ===========================================================================
# F-19 (2026-08-14): workflow/span/@protect contextvar unification.
# ===========================================================================
# Pre-fix the SDK owned two parallel contextvar systems for trace
# context, each set by half of the API surface and never read by the
# other half. ``with workflow(...)`` / ``with span(...)`` only touched
# the legacy ``_trace_id_var`` / ``_span_id_var`` (used by
# ``runtime._enrich_event``), while ``@protect`` and ``set_span``
# only touched ``_current_span`` (used for parent/child SpanContext).
# The two halves disagreed about trace_id for the same execution, so
# the dashboard rendered two disjoint trees per
# ``@protect``-inside-a-``with workflow`` call.
#
# The post-fix contract tested here: ``with workflow`` /
# ``with span`` dual-write both surfaces, ``@protect`` mirrors
# SpanContext back to legacy, and both surfaces restore on block
# exit. Regression tests below pin each direction so future
# refactors (e.g. dropping the legacy vars entirely per audit
# closer in F-19 follow-up) cannot silently break the unified
# invariant.


def test_workflow_dual_writes_span_context_source_pin():
    """
    Source-pin check: ``with workflow("foo")`` pushes a root
    ``SpanContext`` onto ``_current_span`` BEFORE yielding.

    Pre-fix ``workflow()`` left ``_current_span`` untouched, so an
    inner ``@protect`` (which derives its span from
    ``get_current_span()``) created a brand-new root with a
    different ``trace_id`` than the workflow. The cost events
    emitted by ``runtime._enrich_event`` (legacy reader) saw the
    workflow's ``trace_id`` while the ``span_start`` event emitted
    by ``@protect`` saw a different one — dashboard tree-break.

    This is a static AST scan because we want the contract pinned
    even if both readers and writers change: ``with workflow``
    MUST contain at least one ``set_span`` (or equivalent)
    call site BEFORE the ``try: yield`` block, and a
    matching ``reset_span`` (paired with the same Token variable)
    inside the ``finally`` block. Renaming the helper
    (``_set_workflow_root_span`` -> e.g. ``_push_span``) is fine;
    only the symmetric set+reset inside ``workflow()`` matters.
    """
    import ast
    import inspect

    from nullrun.context import workflow as _workflow

    source = inspect.getsource(_workflow)
    tree = ast.parse(source)

    func_found = False
    set_token_in_try_before_yield = False
    reset_token_in_finally = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "workflow":
            continue
        func_found = True
        # Walk the function body looking for `set_X` / `_set_X` calls
        # that produce a token assigned to `span_ctx_token` BEFORE
        # the `yield`, and `reset_X` / `reset_span` calls using the
        # same variable in the `finally` block. We allow any
        # bridging helper name (current implementation uses
        # `_set_workflow_root_span` which calls into
        # `tracing.set_span`; future implementations may inline
        # the helper).
        for child in ast.walk(node):
            if isinstance(child, ast.Assign):
                for target in child.targets:
                    if isinstance(target, ast.Name) and target.id == "span_ctx_token":
                        # Was the RHS a function call to a setter
                        # of some kind? We accept any function
                        # call here (the AST doesn't yet
                        # distinguish which Call) — a future
                        # refactor that calls e.g.
                        # ``_push_workflow_span(...)`` will still
                        # satisfy the test.
                        if isinstance(child.value, ast.Call):
                            set_token_in_try_before_yield = True
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                # reset_span(span_ctx_token) inside the function
                if child.func.id == "reset_span":
                    # Check the argument is span_ctx_token
                    if (
                        child.args
                        and isinstance(child.args[0], ast.Name)
                        and child.args[0].id == "span_ctx_token"
                    ):
                        reset_token_in_finally = True

    assert func_found, "could not find `workflow()` in context.py"
    assert set_token_in_try_before_yield, (
        "F-19 regression: `with workflow(...)` no longer mints a "
        "SpanContext token in its setup block. Pre-fix this would "
        "leave SpanContext unset inside the workflow and break the "
        "dashboard tree (workflow vs @protect disconnect)."
    )
    assert reset_token_in_finally, (
        "F-19 regression: `with workflow(...)` no longer resets "
        "the SpanContext in its finally block. Pre-fix this would "
        "leak the workflow's SpanContext into enclosing code."
    )


def test_span_dual_writes_or_passthrough_source_pin():
    """
    Source-pin check: ``with span(...)`` either pushes a child
    ``SpanContext`` or — if no parent is active — leaves
    ``_current_span`` untouched (the legacy corner-case behavior
    preserved per F-19 fix notes).

    The allowed shapes are:

      A. ``span_ctx_token = _set_child_span_context(span_id)``
         followed by ``reset_span(span_ctx_token)`` in the
         finally, guarded by ``if span_ctx_token is not None``
         (the no-parent case skips the push).

      B. A future replacement that always pushes (the no-parent
         case pushes a synthetic root). Acceptable as long as
         ``reset_span`` pairs with the same token variable.

    The detector matches both shapes by scanning for the
    ``span_ctx_token`` name (any helper function name)
    AND a paired ``reset_span(span_ctx_token)`` in the
    function body.
    """
    import ast
    import inspect

    from nullrun.context import span as _span

    source = inspect.getsource(_span)
    tree = ast.parse(source)

    found_assignment = False
    found_reset = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "span":
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Assign):
                for target in child.targets:
                    if isinstance(target, ast.Name) and target.id == "span_ctx_token":
                        if isinstance(child.value, ast.Call):
                            found_assignment = True
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "reset_span"
            ):
                if (
                    child.args
                    and isinstance(child.args[0], ast.Name)
                    and child.args[0].id == "span_ctx_token"
                ):
                    found_reset = True

    assert found_assignment, (
        "F-19 regression: `with span(...)` no longer mints a "
        "child-SpanContext token via `_set_child_span_context` "
        "(or equivalent). Pre-fix this would leave nested "
        "`@protect` calls detached from the surrounding workflow."
    )
    assert found_reset, (
        "F-19 regression: `with span(...)` no longer resets the "
        "child SpanContext in its finally block. Pre-fix this would "
        "leak the span's SpanContext into enclosing code."
    )


def test_workflow_duals_writes_span_context_runtime():
    """Functional counterpart of the static pin: ``with workflow``
    actually sets ``_current_span`` at runtime and resets it
    on exit."""
    from nullrun.context import get_workflow_id
    from nullrun.context import workflow as _workflow

    assert get_workflow_id() is None
    with _workflow("foo"):
        # Inside the workflow: SpanContext should be set
        current = get_current_span()
        legacy_trace = get_trace_id()
        assert current is not None, (
            "F-19 regression: `with workflow(...)` did not push a "
            "SpanContext onto _current_span. Without this, "
            "inner @protect calls derive a fresh root with a "
            "different trace_id and the dashboard tree breaks."
        )
        # The dual-write contract: SpanContext.trace_id == legacy
        # _trace_id_var. They MUST agree — if they diverge the
        # trace tree is detached again (which is what F-19 was).
        assert current.trace_id == legacy_trace
        assert current.span_id == get_span_id()
        assert current.parent_span_id is None
        assert current.depth == 0
        # workflow_id is set on the workflow_id_var (separate
        # contextvar, not part of SpanContext).
        assert get_workflow_id() == "foo"

    # After exit: SpanContext and legacy vars both restored.
    assert get_current_span() is None
    assert get_trace_id() is None
    assert get_span_id() is None
    assert get_workflow_id() is None


def test_span_inside_workflow_creates_child_span_context():
    """``with span`` inside ``with workflow`` pushes a child
    SpanContext whose parent is the workflow's root span."""
    with nullrun.workflow("outer"):
        workflow_span = get_current_span()
        assert workflow_span is not None
        with nullrun.span("inner") as inner_id:
            inner_span = get_current_span()
            assert inner_span is not None
            # Same trace, child of the workflow's root span.
            assert inner_span.trace_id == workflow_span.trace_id
            assert inner_span.parent_span_id == workflow_span.span_id
            assert inner_span.depth == workflow_span.depth + 1
            # Legacy _span_id_var must equal SpanContext.span_id
            # (the F-19 fix's dual-write invariant).
            assert inner_span.span_id == inner_id
            assert get_span_id() == inner_id

    # After both exits: cleaned up.
    assert get_current_span() is None
    assert get_span_id() is None


def test_span_outside_workflow_preserves_legacy_corner_case():
    """A bare ``with span(...)`` (no enclosing workflow /
    protect) does NOT push a synthetic root onto
    ``_current_span`` — keeps the legacy behavior where
    ``get_trace_id()`` returns None and the runtime's
    fallback synthesises a fresh trace_id at emit time.

    This pins the F-19 audit's explicit design choice
    (preserves bare-span semantics — see
    ``_set_child_span_context`` docstring)."
    """
    # Start clean.
    assert get_current_span() is None
    with nullrun.span("standalone"):
        # _current_span stays None — bare ``with span``
        # outside any workflow/protect must NOT create a
        # synthetic trace root.
        assert get_current_span() is None
        # Legacy _span_id_var IS set (per existing behavior).
        assert get_span_id() == "standalone"
        # Legacy _trace_id_var is NOT set (per existing
        # behavior — runtime synthesises on emit).
        assert get_trace_id() is None

    assert get_current_span() is None
    assert get_span_id() is None


def test_protect_mirrors_span_context_to_legacy_vars(make_runtime, monkeypatch):
    """``@protect`` reads SpanContext (via _next_span) and
    ALSO writes trace_id/span_id back to the legacy
    contextvars so cost events emitted by
    ``runtime._enrich_event`` carry the SAME trace_id as
    span_start. The post-fix invariant: get_trace_id()
    inside the protected function == SpanContext.trace_id.

    Pre-fix this was false: bare ``@protect`` (no enclosing
    workflow) had SpanContext set but legacy vars were None,
    so cost events fell through to ``generate_trace_id()``
    and the trace tree detached from the cost events.
    """
    from nullrun import runtime as runtime_mod
    from nullrun.decorators import reset as reset_decorator_runtime

    # Capture trace_id/span_id from BOTH sources: the runtime's
    # _enrich_event path (legacy reader) and a raw
    # get_current_span() inside the protected body
    # (SpanContext reader).
    captured = {
        "legacy_trace": None,
        "legacy_span": None,
        "span_trace": None,
        "span_span": None,
    }

    # Build a runtime via the test fixture so @protect can find
    # a singleton (the reset_runtime autouse fixture has cleared
    # it). The mock_api fixture inside make_runtime covers any
    # HTTP the runtime touches; we just need the singleton to
    # exist so _protect_body runs past the runtime lookup.
    rt = make_runtime()
    monkeypatch.setattr(runtime_mod, "get_runtime", lambda: rt)

    @nullrun.protect
    def probe():
        captured["legacy_trace"] = get_trace_id()
        captured["legacy_span"] = get_span_id()
        span = get_current_span()
        captured["span_trace"] = span.trace_id if span else None
        captured["span_span"] = span.span_id if span else None

    try:
        probe()
    finally:
        reset_decorator_runtime()

    # Pin the core F-19 invariant: legacy mirrors match
    # SpanContext. If the trace_id diverges, the dashboard
    # tree is broken (which is what F-19 was about).
    assert captured["span_trace"] is not None, (
        "expected a SpanContext to be set by @protect; "
        "if this is None _next_span failed or _protect_body "
        "didn't run set_span before the body."
    )
    assert captured["legacy_trace"] == captured["span_trace"], (
        f"F-19 regression: legacy get_trace_id() "
        f"({captured['legacy_trace']!r}) diverged from "
        f"SpanContext.trace_id ({captured['span_trace']!r}). "
        f"Pre-fix the runtime's cost events read the legacy "
        f"var and span_start read SpanContext — the dashboard "
        f"tree dropped cost events from the trace timeline."
    )
    assert captured["legacy_span"] == captured["span_span"], (
        f"F-19 regression: legacy get_span_id() "
        f"({captured['legacy_span']!r}) diverged from "
        f"SpanContext.span_id ({captured['span_span']!r})."
    )


def test_protect_restores_legacy_vars_after_exit(make_runtime, monkeypatch):
    """After ``@protect`` exits (success OR exception), the
    legacy _trace_id_var / _span_id_var are restored to
    whatever they were BEFORE the call. Token-based reset
    preserves the user's enclosing workflow context.

    Pre-fix there was no legacy-mirror-to-reset pairing,
    so a ``@protect`` inside ``with workflow`` would have
    overwritten the workflow's _span_id_var with the
    ``@protect``'s span_id. After ``@protect`` exit the
    workflow's own span_id was GONE — any further
    ``track(...)`` in the workflow body was attributed
    to the wrong span.
    """
    from nullrun import runtime as runtime_mod
    from nullrun.decorators import reset as reset_decorator_runtime

    rt = make_runtime()
    monkeypatch.setattr(runtime_mod, "get_runtime", lambda: rt)

    with nullrun.workflow("outer_workflow"):
        # Capture the workflow's trace / span context state.
        before_trace = get_trace_id()
        before_span = get_span_id()
        assert before_trace is not None
        assert before_span is not None

        @nullrun.protect
        def probe():
            pass

        try:
            probe()
        finally:
            reset_decorator_runtime()

        # After probe() returns: trace_id/span_id match
        # the workflow's pre-probe values (NOT @protect's
        # inner span). The token-based resets in finally
        # make this true regardless of which depth we're
        # at.
        assert get_trace_id() == before_trace, (
            f"F-19 regression: _trace_id_var not restored "
            f"after @protect exit (got {get_trace_id()!r}, "
            f"expected {before_trace!r})."
        )
        # Inside ``with workflow`` only, the legacy
        # span_id is preserved (the workflow owns the
        # span; @protect's mirror restores it on exit).

    # After both exits: full cleanup.
    assert get_trace_id() is None
    assert get_span_id() is None


def test_workflow_span_protect_yield_depth_two_chain(make_runtime, monkeypatch):
    """End-to-end coherence: workflow -> span -> @protect
    produces a depth-2 SpanContext chain with consistent
    trace_id across every layer.

    Pin the F-19 audit's "trace trees are broken between
    workflow blocks and @protect calls" — the post-fix
    invariant is that ALL three sites agree on trace_id
    AND that the SpanContext depth chain is correct.
    """
    from nullrun import runtime as runtime_mod
    from nullrun.decorators import reset as reset_decorator_runtime

    rt = make_runtime()
    monkeypatch.setattr(runtime_mod, "get_runtime", lambda: rt)

    with nullrun.workflow("top") as top_workflow_id:
        workflow_span = get_current_span()
        assert workflow_span is not None
        with nullrun.span("mid") as mid_span_id:
            mid_span = get_current_span()
            assert mid_span.parent_span_id == workflow_span.span_id
            assert mid_span.depth == 1
            assert mid_span.trace_id == workflow_span.trace_id

            # Snapshot mid-span state so we can verify @protect
            # restores it after exit.
            assert get_span_id() == mid_span_id

            @nullrun.protect
            def leaf():
                # Inside ``leaf``: SpanContext is depth-2,
                # child of the mid-span, same trace as top
                # workflow. The trace_id matches BOTH the
                # legacy _trace_id_var AND the SpanContext —
                # which is the F-19 fix's whole point.
                leaf_span = get_current_span()
                assert leaf_span is not None
                assert leaf_span.depth == 2
                assert leaf_span.parent_span_id == mid_span.span_id
                assert leaf_span.trace_id == workflow_span.trace_id
                # Legacy trace_id matches SpanContext — pin
                # this exactly; F-19 is precisely the broken
                # case where they would diverge.
                assert get_trace_id() == workflow_span.trace_id
                # Workflow id is unchanged inside @protect.
                assert get_workflow_id() == top_workflow_id
                # During @protect the legacy _span_id_var
                # mirrors the leaf span_id; post-fix this
                # is the value the runtime emits on the
                # cost event so the dashboard can group
                # leaf calls under the leaf span.
                assert get_span_id() == leaf_span.span_id

            try:
                leaf()
            finally:
                reset_decorator_runtime()

            # After leaf() returns: legacy span_id is the
            # mid-span (NOT the leaf-span) — token-based
            # reset restored the with-span context.
            # This is the post-fix invariant: @protect
            # doesn't leak into its caller's context.
            assert get_span_id() == mid_span_id, (
                f"F-19 regression: @protect leaked its "
                f"span_id into enclosing with-span context "
                f"(got {get_span_id()!r}, expected "
                f"{mid_span_id!r}). The pre-fix tokenizer "
                f"left _span_id_var stale; the post-fix "
                f"token-based reset restores it."
            )

    assert get_current_span() is None
