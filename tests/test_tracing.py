"""
Tests for the new span/trace context system (`nullrun.tracing`).

These tests exercise the structured SpanContext that replaces the loose
`_trace_id` / `_span_id` contextvars in `nullrun.context`. The key
invariants are:
  - root span has no parent and depth 0
  - child spans inherit trace_id and increment depth
  - set_span / reset_span are token-based (PEP 567 ContextVar semantics)
  - reset_span with the matching token restores the previous context
"""

import pytest

from nullrun.tracing import (
    SpanContext,
    create_child_span,
    create_root_span,
    get_current_span,
    reset_span,
    set_span,
)


def test_root_span_has_no_parent():
    """create_root_span() yields parent_span_id=None and depth=0."""
    root = create_root_span()
    assert isinstance(root, SpanContext)
    assert root.parent_span_id is None
    assert root.depth == 0
    # Both ids are present, distinct, and non-empty.
    assert root.trace_id
    assert root.span_id
    assert root.trace_id != root.span_id


def test_child_inherits_trace_id():
    """create_child_span preserves the parent's trace_id and increments depth."""
    root = create_root_span()
    child = create_child_span(root)
    assert child.trace_id == root.trace_id
    assert child.parent_span_id == root.span_id
    assert child.depth == root.depth + 1
    # A child must have its own span_id (the link is parent->child, not shared).
    assert child.span_id != root.span_id


def test_grandchild_chain_depth():
    """Each create_child_span call adds exactly 1 to depth."""
    root = create_root_span()
    g1 = create_child_span(root)
    g2 = create_child_span(g1)
    g3 = create_child_span(g2)
    assert (root.depth, g1.depth, g2.depth, g3.depth) == (0, 1, 2, 3)
    assert g3.parent_span_id == g2.span_id
    assert g3.trace_id == root.trace_id


def test_sibling_children_share_trace_but_diverge_in_span_id():
    """Two children of the same parent share trace_id and parent_span_id
    but each gets its own span_id — the tree branches at the parent."""
    root = create_root_span()
    a = create_child_span(root)
    b = create_child_span(root)
    assert a.trace_id == b.trace_id == root.trace_id
    assert a.parent_span_id == b.parent_span_id == root.span_id
    assert a.span_id != b.span_id
    assert a.depth == b.depth == 1


def test_get_current_span_default_is_none():
    """Outside any set_span, get_current_span() returns None (no trace in progress)."""
    # A fresh test starts with no span set on the runtime ContextVar.
    assert get_current_span() is None


def test_set_and_reset_round_trip():
    """set_span pushes the span onto the contextvar; reset_span with the
    matching token pops it and restores the previous (None) value."""
    root = create_root_span()
    token = set_span(root)
    try:
        assert get_current_span() is root
    finally:
        reset_span(token)
    assert get_current_span() is None


def test_nested_set_restores_parent_after_reset():
    """set_span inside set_span must restore the *outer* span, not None
    when the inner token is reset."""
    outer = create_root_span()
    inner_parent = create_child_span(outer)
    grandchild = create_child_span(inner_parent)

    outer_token = set_span(outer)
    assert get_current_span() is outer

    inner_token = set_span(inner_parent)
    assert get_current_span() is inner_parent

    grandchild_token = set_span(grandchild)
    assert get_current_span() is grandchild

    reset_span(grandchild_token)
    assert get_current_span() is inner_parent

    reset_span(inner_token)
    assert get_current_span() is outer

    reset_span(outer_token)
    assert get_current_span() is None


def test_reset_token_is_single_use():
    """A reset_token can only be consumed once — calling reset_span
    twice with the same token raises RuntimeError. This catches the
    common bug where a finally block runs twice (e.g. wrapped in a
    second try/finally) and overwrites a newer context with stale state.
    """
    root = create_root_span()
    token = set_span(root)
    reset_span(token)
    # First reset succeeded. The token is now consumed — a second
    # reset with the same token must fail, not silently no-op.
    with pytest.raises(RuntimeError):
        reset_span(token)


def test_span_context_is_immutable():
    """SpanContext is a frozen dataclass — runtime mutators cannot
    accidentally rewrite a span's identity after it has been emitted."""
    root = create_root_span()
    with pytest.raises(Exception):
        # Frozen dataclass raises FrozenInstanceError on attribute set
        # the broader `Exception` is fine because exact subclass is
        # not part of the public surface.
        root.span_id = "tampered"  # type: ignore[misc]


# ===========================================================================
# Sprint 2.6 (B5): create_child_span must reject None parent clearly
# ===========================================================================
# Pre-fix: ``create_child_span(None)`` raised
# ``TypeError: unsupported operand for None + 1`` on the
# ``parent.depth + 1`` line. That crashed the whole
# ``@protect`` / track_* pipeline when a caller passed ``None``
# instead of a SpanContext (e.g. ``get_current_span `` returns
# ``None`` when no trace is in progress). Post-fix the function
# raises ``ValueError`` with a clear message.


def test_create_child_span_rejects_none_parent():
    """``create_child_span(None)`` raises ``ValueError`` (not ``TypeError``).

    Regression for B5: pre-fix this raised a confusing
    ``TypeError`` deep inside the dataclass constructor
    (``unsupported operand for None + 1``) which crashed the
    whole tracking pipeline. Now it raises ``ValueError`` with
    a message that points the caller at the right alternative
    (``create_root_span ``).
    """
    from nullrun.tracing import create_child_span

    with pytest.raises(ValueError) as exc_info:
        create_child_span(None)  # type: ignore[arg-type]

    # The message must guide the caller to the right alternative.
    assert "create_root_span" in str(exc_info.value), (
        f"ValueError message should mention create_root_span() "
        f"as the alternative; got: {exc_info.value}"
    )


def test_create_child_span_with_valid_parent_works():
    """Sanity: the defensive check does not break the happy path."""
    from nullrun.tracing import create_child_span, create_root_span

    root = create_root_span()
    child = create_child_span(root)
    assert child.parent_span_id == root.span_id
    assert child.trace_id == root.trace_id
    assert child.depth == root.depth + 1
