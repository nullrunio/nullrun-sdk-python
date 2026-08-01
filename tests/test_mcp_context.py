"""Tests for the v3.31 (Разрыв 3) MCP tool-context helpers.

Pure contextvar plumbing — no network involved. These tests are
the SDK-side contract pin for the wire fields the backend expects:
  * ``tool_class``  — one of `builtin | mcp | custom | invalid`
  * ``mcp_annotations` — dict with `read_only`, `destructive`,
    `open_world` keys, each `bool | None`.

Pre-fix these helpers did not exist; SDKs had to fake
classifications at the wire level by hand. Post-fix the helpers
provide a single owner for the contextvar lifecycle so v3.31's
honest-SDK trust boundary (McpAnnotations forwarded verbatim from
``tools/list``) is testable.
"""

import pytest

import nullrun.context as _ctx
from nullrun.context import (
    get_call_mcp_annotations,
    get_call_mcp_class,
    set_mcp_tool_context,
)


@pytest.fixture(autouse=True)
def _isolate_mcp_context():
    """Reset the module-level ContextVars around every test.

    MCP adapter tests run earlier in the full suite and intentionally
    leave their last call metadata in the current context. Reset the
    variables directly because ``set_mcp_tool_context(None, None)``
    has partial-update semantics and therefore does not clear them.
    """
    _ctx._call_mcp_class_var.set(None)
    _ctx._call_mcp_annotations_var.set(None)
    yield
    _ctx._call_mcp_class_var.set(None)
    _ctx._call_mcp_annotations_var.set(None)


class TestMcpContext:
    def test_class_defaults_to_none(self):
        assert get_call_mcp_class() is None
        assert get_call_mcp_annotations() is None

    def test_set_class_persists(self):
        set_mcp_tool_context(tool_class="mcp")
        assert get_call_mcp_class() == "mcp"
        # Annotations remain None because we only set class.
        assert get_call_mcp_annotations() is None

    def test_set_annotations_persists(self):
        set_mcp_tool_context(
            annotations={
                "read_only": True,
                "destructive": False,
                "open_world": True,
            }
        )
        ann = get_call_mcp_annotations()
        assert ann is not None
        assert ann["read_only"] is True
        assert ann["destructive"] is False
        assert ann["open_world"] is True

    def test_set_both_at_once(self):
        set_mcp_tool_context(
            tool_class="mcp",
            annotations={"destructive": True},
        )
        assert get_call_mcp_class() == "mcp"
        ann = get_call_mcp_annotations()
        assert ann == {"destructive": True}

    def test_partial_updates_dont_drop_unset_side(self):
        # Set both, then update only the class. Annotations
        # should NOT be cleared — the helper's contract is
        # "set what you pass, leave what you don't".
        set_mcp_tool_context(
            tool_class="mcp",
            annotations={"destructive": True},
        )
        set_mcp_tool_context(tool_class="builtin")
        assert get_call_mcp_class() == "builtin"
        ann = get_call_mcp_annotations()
        assert ann == {"destructive": True}

    def test_clearing_class_with_none(self):
        """Partial-update contract: explicit ``None`` does NOT
        clear a previously-set value. ``set_mcp_tool_context`` is
        designed so the caller can update just the fields they
        care about (most callers don't want to wipe the
        annotations when re-stamping the class). Use
        ``set_mcp_tool_context(tool_class='invalid')`` to
        reset to a sentinel, or just don't call the helper at
        all to inherit the default ``None``."""

        set_mcp_tool_context(
            tool_class="mcp",
            annotations={"destructive": True},
        )
        assert get_call_mcp_class() == "mcp"
        assert get_call_mcp_annotations() == {"destructive": True}
        # Explicit None for one field leaves the other alone —
        # partial-update behavior, NOT clear-on-None semantics.
        set_mcp_tool_context(tool_class=None)
        assert get_call_mcp_class() == "mcp"  # unchanged
        # To actually clear, set an explicit sentinel value.

        # To get a fresh-None state for the next test, clear via
        # the helper that exists for this exact purpose:
        import nullrun.context as _ctx

        _ctx._call_mcp_class_var.set(None)  # noqa: SLF001
        _ctx._call_mcp_annotations_var.set(None)  # noqa: SLF001
        assert get_call_mcp_class() is None
        assert get_call_mcp_annotations() is None

    def test_clearing_annotations_with_none(self):
        """Partial-update contract (see also
        ``test_clearing_class_with_none``): ``set_mcp_tool_context``
        with ``annotations=None`` leaves the previously-set
        class alone."""

        set_mcp_tool_context(
            tool_class="mcp",
            annotations={"destructive": True},
        )
        assert get_call_mcp_class() == "mcp"
        assert get_call_mcp_annotations() == {"destructive": True}
        # Passing None for annotations leaves it alone —
        # partial-update semantics.
        set_mcp_tool_context(annotations=None)
        assert get_call_mcp_annotations() == {"destructive": True}
        assert get_call_mcp_class() == "mcp"

    def test_annotations_partial_dict_allowed(self):
        # Operators may forward only the keys they have. The
        # backend treats absent keys as "unknown" rather than
        # false (per Разрыв 3 / wire contract). The SDK does
        # the same — partial dicts are accepted verbatim.
        set_mcp_tool_context(annotations={"destructive": True})
        ann = get_call_mcp_annotations()
        assert "destructive" in ann
        assert "read_only" not in ann
        assert "open_world" not in ann


class TestMcpClassValues:
    @pytest.mark.parametrize(
        "value",
        ["builtin", "mcp", "custom", "invalid"],
    )
    def test_class_strings_round_trip(self, value):
        set_mcp_tool_context(tool_class=value)
        assert get_call_mcp_class() == value
