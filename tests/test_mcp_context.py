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

from nullrun.context import (
    get_call_mcp_annotations,
    get_call_mcp_class,
    set_mcp_tool_context,
)


class TestMcpContext:
    def test_class_defaults_to_none(self):
        # Fresh context — no setters called yet.
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
        # Explicit None in tool_class position should clear.
        set_mcp_tool_context(tool_class="mcp")
        assert get_call_mcp_class() == "mcp"
        set_mcp_tool_context(tool_class=None)
        assert get_call_mcp_class() is None

    def test_clearing_annotations_with_none(self):
        set_mcp_tool_context(annotations={"destructive": True})
        assert get_call_mcp_annotations() is not None
        set_mcp_tool_context(annotations=None)
        assert get_call_mcp_annotations() is None

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
