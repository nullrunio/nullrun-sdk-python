"""
Trace/span context management via Python contextvars.

This module is the core of the new trace/span system (Phase 2 of
the SDK cleanup plan). The previous `nullrun.context` module
exposed loose `_trace_id` and `_span_id` contextvars — fine for
attaching IDs to events, but it didn't model the parent/child
hierarchy that a trace timeline needs.

`SpanContext` is a structured value: a single contextvar holds
the *current* span, and child spans are derived from it via
`create_child_span(parent)`. This is the same pattern OpenTelemetry
uses for its Python SDK (`opentelemetry.context.get_current`) and
gives `@protect` (Commit 4) and `track_*` (Commit 5) a uniform
way to attach `trace_id` / `span_id` / `parent_span_id` / `depth`
to every emitted event.

Thread/async safety: `ContextVar` is thread-local by default but
PEP 567 guarantees the right value is restored across `await`
boundaries in asyncio, so concurrent coroutines each see their
own current span.

What this module does NOT do:
  - It does not emit events. `SpanContext` is a pure data
    structure. The runtime's `track_event()` is what actually
    posts `span_start` / `span_end` events to the backend. See
    `_emit_span_start` / `_emit_span_end` in `nullrun.decorators`
    for the wiring.
  - It does not implement OTel-style attributes, status, or
    exception recording. We keep the surface minimal — a span
    is just an ID tuple, the dashboard reconstructs the rest
    from the event stream.
"""
from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional


def _new_id() -> str:
    """Generate a fresh span/trace id.

    Returns a real UUID4 with dashes (e.g. ``95ca7c0b-...-2788803ef3b8``)
    so the backend's `Uuid::parse_str` accepts it on the wire. Earlier
    we shipped `uuid.uuid4().hex` (32 hex chars, no dashes) which the
    backend silently dropped to NULL.
    """
    return str(uuid.uuid4())


@dataclass(frozen=True)
class SpanContext:
    """
    One span in the call tree.

    Attributes:
        trace_id:    Stable across the whole trace (root + all descendants).
        span_id:     Unique to this span. Children reference it as
                     `parent_span_id`.
        parent_span_id: The parent's `span_id`, or None for the root span.
        depth:       0 for the root, parent.depth + 1 for each child.
                     Useful for the waterfall UI's indentation.
    """

    trace_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    depth: int = 0


# The currently-active span. `None` means "no trace in progress" — track_*
# will fall back to creating a synthetic root on each call so events are
# still attributed to *something*.
_current_span: ContextVar[Optional[SpanContext]] = ContextVar(
    "nullrun_span", default=None
)


def get_current_span() -> Optional[SpanContext]:
    """
    Return the active span, or None if no `@protect` / manual `set_span`
    has put us inside a trace.
    """
    return _current_span.get()


def create_child_span(parent: SpanContext) -> SpanContext:
    """
    Derive a new child span from `parent`.

    The child inherits `parent.trace_id` and increments `parent.depth`.
    `parent_span_id` is set to `parent.span_id` so the tree is fully
    reconstructable from the event stream.
    """
    return SpanContext(
        trace_id=parent.trace_id,
        span_id=_new_id(),
        parent_span_id=parent.span_id,
        depth=parent.depth + 1,
    )


def create_root_span() -> SpanContext:
    """
    Start a new trace. Returns a SpanContext with no parent and depth 0.
    """
    tid = _new_id()
    return SpanContext(
        trace_id=tid,
        span_id=_new_id(),
        parent_span_id=None,
        depth=0,
    )


def set_span(ctx: SpanContext):
    """
    Make `ctx` the current span. Returns a token that MUST be passed
    back to `reset_span` in a `finally` block to restore the previous
    context (which may itself be None).

    Usage:
        span = create_root_span()
        token = set_span(span)
        try:
            ...
        finally:
            reset_span(token)
    """
    return _current_span.set(ctx)


def reset_span(token) -> None:
    """
    Restore the context that was active before the matching `set_span`.
    Pair with `set_span` — never call reset_span with a token from a
    different context.
    """
    _current_span.reset(token)
