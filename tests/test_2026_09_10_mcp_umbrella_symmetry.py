"""Source-pin + behavioural regression tests for SDK B.1 (2026-09-10).

B.1 closed two wire-class gaps that were silently dropping typed
information to the generic ``NullRunBlockedException`` / NR-X001
fallback:

1. **MCP umbrella codes** (ADR-013, 2026-08-14, frozen-dormant) —
   ``MCP_DESTRUCTIVE_BLOCKED``, ``MCP_READONLY_BYPASS_BLOCKED``,
   ``MCP_APPROVAL_REQUIRED`` previously all collapsed to
   ``NullRunBlockedException``. Cookbook code that wanted to
   ``except NullRunMcpDestructiveBlockedError:`` etc. fell through
   to the generic arm and surfaced ``FALLBACK_MESSAGE = "Something
   went wrong. Please try again."`` — the very bug the langgraph
   demo test surfaced in 2026-09-08.

2. **APPROVAL_DB_* sibling family** — six codes (DB_UNAVAILABLE,
   PERSISTENCE_FAILED, VALIDATION_FAILED, CONFLICT, NOT_FOUND,
   CREATE_FAILED) previously all collapsed to the base
   ``NullRunBlockedException``. Operators couldn't tell apart a
   transient DB outage from a validation failure. Post-B.1 they
   all map to the single typed ``NullRunApprovalDbUnavailableError``
   so cookbook code can branch on the typed class.

Each test pins the source surface so a future refactor that
re-introduces the bug (e.g. drops one of the typed mappings) is
caught at test time, not in production.
"""

from __future__ import annotations

import os

EXCEPTIONS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "src", "nullrun", "breaker", "exceptions.py"
)
TRANSPORT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "src", "nullrun", "transport.py"
)
INIT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "src", "nullrun", "__init__.py"
)
MESSAGES_PATH = os.path.join(
    os.path.dirname(__file__), "..", "src", "nullrun", "messages.py"
)


def _read(path: str) -> str:
    return open(path, encoding="utf-8").read()


def _strip_comment_lines(src: str) -> str:
    return "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )


# ─── Typed exception class definitions (MCP umbrella) ────────────────────


def test_mcp_destructive_blocked_error_class_defined():
    """``breaker/exceptions.py`` must define ``NullRunMcpDestructiveBlockedError``."""
    src = _read(EXCEPTIONS_PATH)
    code_only = _strip_comment_lines(src)
    assert "class NullRunMcpDestructiveBlockedError" in code_only, (
        "B.1: NullRunMcpDestructiveBlockedError must be defined in "
        "breaker/exceptions.py — typed class for MCP_DESTRUCTIVE_BLOCKED"
    )


def test_mcp_readonly_bypass_blocked_error_class_defined():
    """``breaker/exceptions.py`` must define ``NullRunMcpReadonlyBypassBlockedError``."""
    src = _read(EXCEPTIONS_PATH)
    code_only = _strip_comment_lines(src)
    assert "class NullRunMcpReadonlyBypassBlockedError" in code_only, (
        "B.1: NullRunMcpReadonlyBypassBlockedError must be defined in "
        "breaker/exceptions.py — typed class for MCP_READONLY_BYPASS_BLOCKED"
    )


def test_mcp_approval_required_error_class_defined():
    """``breaker/exceptions.py`` must define ``NullRunMcpApprovalRequiredError``."""
    src = _read(EXCEPTIONS_PATH)
    code_only = _strip_comment_lines(src)
    assert "class NullRunMcpApprovalRequiredError" in code_only, (
        "B.1: NullRunMcpApprovalRequiredError must be defined in "
        "breaker/exceptions.py — typed class for MCP_APPROVAL_REQUIRED"
    )


def test_approval_db_unavailable_error_class_defined():
    """``breaker/exceptions.py`` must define ``NullRunApprovalDbUnavailableError``."""
    src = _read(EXCEPTIONS_PATH)
    code_only = _strip_comment_lines(src)
    assert "class NullRunApprovalDbUnavailableError" in code_only, (
        "B.1: NullRunApprovalDbUnavailableError must be defined in "
        "breaker/exceptions.py — typed class for the six "
        "APPROVAL_DB_* sibling codes"
    )


def test_mcp_classes_inherit_from_null_run_blocked_exception():
    """All four B.1 typed classes must inherit from
    ``NullRunBlockedException`` (not ``NullRunError`` directly) so
    cookbook ``except NullRunBlockedException:`` arms still match
    via the MRO. This is the same pattern as the existing
    NR-A010..NR-A015 approval classes."""
    src = _read(EXCEPTIONS_PATH)
    code_only = _strip_comment_lines(src)
    for cls in [
        "NullRunMcpDestructiveBlockedError",
        "NullRunMcpReadonlyBypassBlockedError",
        "NullRunMcpApprovalRequiredError",
        "NullRunApprovalDbUnavailableError",
    ]:
        # Look for ``class <X>(NullRunBlockedException):``
        assert f"class {cls}(NullRunBlockedException):" in code_only, (
            f"B.1: {cls} must inherit from NullRunBlockedException, "
            f"not NullRunError directly. The MRO is the contract — "
            f"cookbook ``except NullRunBlockedException:`` arms must "
            f"still match the typed subclass."
        )


# ─── Transport mappings ──────────────────────────────────────────────────


def test_transport_maps_mcp_destructive_blocked_to_typed_class():
    """``transport.py:_V3_ERROR_CODE_MAP`` must map
    ``MCP_DESTRUCTIVE_BLOCKED`` to ``NullRunMcpDestructiveBlockedError``,
    NOT to the base ``NullRunBlockedException``."""
    src = _read(TRANSPORT_PATH)
    code_only = _strip_comment_lines(src)
    assert (
        '"MCP_DESTRUCTIVE_BLOCKED": NullRunMcpDestructiveBlockedError'
        in code_only
    ), (
        "B.1: transport.py must map MCP_DESTRUCTIVE_BLOCKED to the "
        "typed NullRunMcpDestructiveBlockedError. Pre-B.1 the wire "
        "code collapsed to NullRunBlockedException; cookbook code "
        "branching on the typed class fell through to NR-X001."
    )


def test_transport_maps_mcp_readonly_bypass_blocked_to_typed_class():
    src = _read(TRANSPORT_PATH)
    code_only = _strip_comment_lines(src)
    assert (
        '"MCP_READONLY_BYPASS_BLOCKED": NullRunMcpReadonlyBypassBlockedError'
        in code_only
    ), (
        "B.1: transport.py must map MCP_READONLY_BYPASS_BLOCKED to "
        "NullRunMcpReadonlyBypassBlockedError"
    )


def test_transport_maps_mcp_approval_required_to_typed_class():
    src = _read(TRANSPORT_PATH)
    code_only = _strip_comment_lines(src)
    assert (
        '"MCP_APPROVAL_REQUIRED": NullRunMcpApprovalRequiredError'
        in code_only
    ), (
        "B.1: transport.py must map MCP_APPROVAL_REQUIRED to "
        "NullRunMcpApprovalRequiredError"
    )


def test_transport_maps_all_six_approval_db_codes_to_typed_class():
    """All six APPROVAL_DB_* sibling codes must map to the typed
    ``NullRunApprovalDbUnavailableError``. Pre-B.1 they all collapsed
    to ``NullRunBlockedException`` — operators couldn't tell apart
    a transient DB outage from a validation failure."""
    src = _read(TRANSPORT_PATH)
    code_only = _strip_comment_lines(src)
    for code in [
        "APPROVAL_DB_UNAVAILABLE",
        "APPROVAL_PERSISTENCE_FAILED",
        "APPROVAL_VALIDATION_FAILED",
        "APPROVAL_CONFLICT",
        "APPROVAL_NOT_FOUND",
        "APPROVAL_CREATE_FAILED",
    ]:
        assert (
            f'"{code}": NullRunApprovalDbUnavailableError' in code_only
        ), (
            f"B.1: transport.py must map {code} to "
            f"NullRunApprovalDbUnavailableError (typed). Pre-B.1 the "
            f"wire code collapsed to NullRunBlockedException."
        )


# ─── Top-level discoverability (lazy exports + __all__) ─────────────────


def test_mcp_umbrella_classes_in_lazy_exports():
    """All three MCP umbrella classes must be importable from the
    top-level ``nullrun`` namespace via ``_LAZY_EXPORTS``. Pre-B.1
    they didn't exist; post-B.1 the typed classes are part of the
    public surface (cookbook recipes branch on them by name)."""
    src = _read(INIT_PATH)
    for cls in [
        "NullRunMcpDestructiveBlockedError",
        "NullRunMcpReadonlyBypassBlockedError",
        "NullRunMcpApprovalRequiredError",
    ]:
        assert f'"{cls}":' in src, (
            f"B.1: {cls} must appear in _LAZY_EXPORTS so cookbook "
            f"code can ``from nullrun import {cls}``"
        )


def test_approval_db_unavailable_in_lazy_exports():
    src = _read(INIT_PATH)
    assert '"NullRunApprovalDbUnavailableError":' in src, (
        "B.1: NullRunApprovalDbUnavailableError must appear in "
        "_LAZY_EXPORTS"
    )


def test_mcp_umbrella_classes_in_all():
    """The four B.1 typed classes must appear in ``__all__`` so
    tab-completion surfaces them via ``dir(nullrun)``. Cookbook
    code that wants to ``except NullRunMcpDestructiveBlockedError:``
    needs to discover the class via ``dir(nullrun)`` first."""
    src = _read(INIT_PATH)
    # Anchor on the __all__ block — naive substring search would
    # false-positive on _LAZY_EXPORTS entries.
    import re
    match = re.search(r"__all__\s*=\s*\[(.*?)\]", src, re.DOTALL)
    assert match is not None, "B.1: __all__ list must exist in __init__.py"
    all_block = match.group(1)
    for cls in [
        "NullRunMcpDestructiveBlockedError",
        "NullRunMcpReadonlyBypassBlockedError",
        "NullRunMcpApprovalRequiredError",
        "NullRunApprovalDbUnavailableError",
    ]:
        assert f'"{cls}"' in all_block, (
            f"B.1: {cls} must appear in __all__ so tab-completion "
            f"surfaces it via dir(nullrun)"
        )


# ─── Catalog completeness ────────────────────────────────────────────────


def test_messages_catalog_has_mcp_and_approval_db_entries():
    """``messages.DEFAULT_MESSAGES`` must have entries for the four
    new error codes (NR-MCP01, NR-MCP02, NR-MCP03, NR-A016).
    Without these, ``format_user_message`` falls through to the
    generic ``FALLBACK_MESSAGE = "Something went wrong. Please
    try again."`` — the exact bug the langgraph approval demo
    surfaced in 2026-09-08."""
    from nullrun import messages

    for code in ["NR-MCP01", "NR-MCP02", "NR-MCP03", "NR-A016"]:
        assert code in messages.DEFAULT_MESSAGES, (
            f"B.1: messages.DEFAULT_MESSAGES must contain an entry "
            f"for {code} (the typed class's error_code). Missing "
            f"catalog entry means format_user_message returns the "
            f"generic FALLBACK_MESSAGE for the typed class — "
            f"defeats the whole point of the typed mapping."
        )


# ─── Behavioural smoke tests ─────────────────────────────────────────────


def test_typed_class_runtime_imports():
    """All four B.1 typed classes must import successfully from
    both the breaker.exceptions module AND the top-level nullrun
    namespace."""
    import nullrun
    from nullrun.breaker import exceptions as exc_mod

    pairs = [
        ("NullRunMcpDestructiveBlockedError", exc_mod.NullRunMcpDestructiveBlockedError),
        ("NullRunMcpReadonlyBypassBlockedError", exc_mod.NullRunMcpReadonlyBypassBlockedError),
        ("NullRunMcpApprovalRequiredError", exc_mod.NullRunMcpApprovalRequiredError),
        ("NullRunApprovalDbUnavailableError", exc_mod.NullRunApprovalDbUnavailableError),
    ]
    for name, breaker_cls in pairs:
        top_level_cls = getattr(nullrun, name, None)
        assert top_level_cls is not None, (
            f"B.1: nullrun.{name} must be importable (lazy export "
            f"failed or class is missing)"
        )
        assert top_level_cls is breaker_cls, (
            f"B.1: nullrun.{name} must be the same class object as "
            f"nullrun.breaker.exceptions.{name} — a separate proxy "
            f"class would defeat isinstance() checks across the "
            f"codebase"
        )


def test_typed_class_isinstance_of_null_run_blocked_exception():
    """The four B.1 typed classes must be ``isinstance(..., NullRunBlockedException)``.
    Cookbook ``except NullRunBlockedException:`` arms rely on this
    MRO behaviour — if a future refactor changes the parent class
    the cookbook handlers silently miss the typed arms."""
    from nullrun import NullRunBlockedException
    from nullrun.breaker import exceptions as exc_mod

    for cls in [
        exc_mod.NullRunMcpDestructiveBlockedError,
        exc_mod.NullRunMcpReadonlyBypassBlockedError,
        exc_mod.NullRunMcpApprovalRequiredError,
        exc_mod.NullRunApprovalDbUnavailableError,
    ]:
        # Instantiate with the minimum required kwargs (workflow_id,
        # reason). The base __init__ accepts **details so additional
        # typed kwargs can be forwarded from transport.py.
        instance = cls(workflow_id="wf-1", reason="test")
        assert isinstance(instance, NullRunBlockedException), (
            f"B.1: {cls.__name__} must be isinstance of "
            f"NullRunBlockedException (MRO contract for cookbook "
            f"handlers). Got MRO: {[c.__name__ for c in type(instance).__mro__]}"
        )


def test_format_user_message_returns_catalog_default_for_typed_classes():
    """``format_user_message`` on a B.1 typed class instance must
    return the catalog default, NOT the FALLBACK_MESSAGE. This is
    the actual user-facing bug B.1 closed — pre-B.1 the wire code
    landed on the base class, the base class's error_code was a
    SCREAMING_SNAKE backend string with no catalog entry, and
    ``format_user_message`` fell through to FALLBACK_MESSAGE."""
    from nullrun import messages
    from nullrun.breaker import exceptions as exc_mod

    cases = [
        (
            exc_mod.NullRunMcpDestructiveBlockedError("wf-1", "destructive blocked"),
            "NR-MCP01",
        ),
        (
            exc_mod.NullRunMcpReadonlyBypassBlockedError("wf-1", "readonly bypass blocked"),
            "NR-MCP02",
        ),
        (
            exc_mod.NullRunMcpApprovalRequiredError("wf-1", "mcp approval pending"),
            "NR-MCP03",
        ),
        (
            exc_mod.NullRunApprovalDbUnavailableError("wf-1", "approval db down"),
            "NR-A016",
        ),
    ]
    for instance, expected_code in cases:
        assert instance.error_code == expected_code, (
            f"B.1: {type(instance).__name__}.error_code must be "
            f"{expected_code}, got {instance.error_code!r}"
        )
        out = messages.format_user_message(instance)
        assert out == messages.DEFAULT_MESSAGES[expected_code], (
            f"B.1: format_user_message on {type(instance).__name__} "
            f"must return the catalog default for {expected_code}, "
            f"not FALLBACK_MESSAGE. Got: {out!r}"
        )
        assert out != messages.FALLBACK_MESSAGE, (
            f"B.1: {type(instance).__name__} fell through to "
            f"FALLBACK_MESSAGE — catalog entry for {expected_code} "
            f"is missing or wire mapping is wrong"
        )
