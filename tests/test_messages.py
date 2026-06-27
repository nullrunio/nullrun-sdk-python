"""Tests for the user-facing message catalog.

These tests pin two invariants:

1. Every ``error_code`` raised by the SDK has a default message in
   :data:`nullrun.messages.DEFAULT_MESSAGES`. Adding a new code in
   ``exceptions.py`` without an entry here is a regression — end users
   would see the generic fallback instead of a meaningful message.

2. :func:`format_user_message` returns a non-empty, non-internal-jargon
   string for every exception class the SDK can raise. The tests do
   NOT assert the exact wording (NULLRUN reserves the right to tune
   phrasing) — only that the message is non-empty and contains no
   developer-facing substrings (``workflow``, ``budget_cents``,
   ``api_key``, ``NULLRUN_`` env vars).
"""
from __future__ import annotations

import pytest

from nullrun import messages
from nullrun.breaker import exceptions as exc


# ---------------------------------------------------------------------------
# Catalog completeness — every code in the SDK has a default message
# ---------------------------------------------------------------------------
# Codes raised by ``NullRunError`` subclasses. If a new subclass is added
# with a new ``error_code``, this list must be updated alongside
# ``DEFAULT_MESSAGES`` in ``nullrun/messages.py``.
_EXPECTED_CODES = {
    "NR-0000",
    "NR-A001",
    "NR-A003",
    "NR-B001",
    "NR-B002",
    "NR-B005",
    "NR-R001",
    "NR-C000",
    "NR-X001",
    "NR-B004",
    "NR-T001",
    "NR-W002",
    "NR-W003",
}


def test_catalog_has_entry_for_every_documented_code():
    """Every code the SDK raises MUST have a default user message.

    Adding a new code without an entry here means end users will see
    the generic fallback instead of a meaningful message. This is the
    single source of truth for catalog completeness — keep this set in
    sync with ``error_code`` declarations across the SDK.
    """
    missing = _EXPECTED_CODES - set(messages.DEFAULT_MESSAGES)
    assert not missing, (
        f"DEFAULT_MESSAGES is missing entries for: {sorted(missing)}. "
        "Add a default user-facing message for each code — see "
        "nullrun/messages.py docstring for tone rules."
    )


def test_catalog_messages_are_non_empty_strings():
    for code, msg in messages.DEFAULT_MESSAGES.items():
        assert isinstance(msg, str), f"{code} message is not a string"
        assert msg.strip(), f"{code} message is empty or whitespace-only"


def test_catalog_messages_have_no_internal_jargon():
    """User-facing text must NOT leak developer-facing substrings.

    Host code is expected to show the formatted message verbatim to
    end users. Anything that looks like an internal identifier
    (``workflow``, ``budget_cents``, ``NULLRUN_*`` env var, ``api_key``)
    is a leak.
    """
    forbidden_substrings = (
        "workflow",  # internal term — agents have workflows, users don't
        "budget_cents",
        "api_key",
        "NULLRUN_",
        "nr_live_",
        "http",
        "://",  # URLs go on user_action, not user_message
    )
    for code, msg in messages.DEFAULT_MESSAGES.items():
        lowered = msg.lower()
        for needle in forbidden_substrings:
            assert needle not in lowered, (
                f"{code} user_message contains forbidden substring "
                f"{needle!r}: {msg!r}"
            )


# ---------------------------------------------------------------------------
# format_user_message — basic lookup
# ---------------------------------------------------------------------------
def test_format_user_message_returns_default_for_known_code():
    budget = exc.NullRunBudgetError(
        workflow_id="wf-1",
        reason="budget_cents=500 exceeded",
    )
    out = messages.format_user_message(budget)
    assert out == messages.DEFAULT_MESSAGES["NR-B004"]


def test_format_user_message_handles_all_block_subclasses():
    """Each block-decision subclass resolves to its own code, not the
    generic NR-X001 fallback."""
    cases = [
        (exc.NullRunBudgetError("wf", "x"), "NR-B004"),
        (exc.NullRunToolBlockedError("wf", "x", tool_name="send_email"), "NR-T001"),
    ]
    for instance, expected_code in cases:
        out = messages.format_user_message(instance)
        assert out == messages.DEFAULT_MESSAGES[expected_code], (
            f"{type(instance).__name__} expected {expected_code}, got {out!r}"
        )


def test_format_user_message_handles_transport_subclasses():
    """Transport errors (NR-B001 / NR-B002 / NR-A003 / NR-B005) all
    have user-facing defaults so end users see clean text on
    transport-level outages rather than raw exception messages."""
    cases = [
        (
            exc.NullRunTransportError(
                "boom",
                source=exc.TransportErrorSource.NETWORK_ERROR,
                endpoint="execute",
            ),
            "NR-B001",
        ),
        (
            exc.NullRunBackendError("boom", endpoint="check"),
            "NR-B002",
        ),
        (
            exc.NullRunAuthError("rejected"),
            "NR-A003",
        ),
        (
            exc.RateLimitError(
                "rate limited",
                source=exc.TransportErrorSource.GATEWAY_ERROR,
                endpoint="check",
            ),
            "NR-R001",
        ),
    ]
    for instance, expected_code in cases:
        out = messages.format_user_message(instance)
        assert out == messages.DEFAULT_MESSAGES[expected_code]


def test_format_user_message_handles_workflow_paused():
    paused = exc.WorkflowPausedException(workflow_id="wf-1", reason="cooldown")
    out = messages.format_user_message(paused)
    assert out == messages.DEFAULT_MESSAGES["NR-W003"]


def test_format_user_message_handles_workflow_killed_baseexception():
    """``WorkflowKilledInterrupt`` is a BaseException subclass. The
    formatter must still resolve it via the inherited ``error_code``
    class attribute on ``WorkflowKilledException`` (the deprecated
    parent class)."""
    killed = exc.WorkflowKilledInterrupt(workflow_id="wf-1", reason="killed via API")
    # NB: the formatter does NOT catch BaseException — caller's job.
    out = messages.format_user_message(killed)
    assert out == messages.DEFAULT_MESSAGES["NR-W002"]


def test_format_user_message_falls_back_for_object_without_error_code():
    """Plain objects (no ``error_code`` attribute) get the fallback."""
    class NotAnError:
        pass

    assert messages.format_user_message(NotAnError()) == messages.FALLBACK_MESSAGE
    assert messages.format_user_message(Exception("boom")) == messages.FALLBACK_MESSAGE


def test_format_user_message_falls_back_for_unknown_code():
    """An exception with an error_code that has no catalog entry still
    returns a non-empty string (the fallback), never raises."""
    weird = exc.NullRunError("msg", error_code="NR-9999")
    assert messages.format_user_message(weird) == messages.FALLBACK_MESSAGE


def test_format_user_message_accepts_locale_kwarg():
    """Locale parameter is reserved; passing anything (including
    unsupported codes) still returns a usable string."""
    budget = exc.NullRunBudgetError("wf", "x")
    assert messages.format_user_message(budget, locale="en")
    assert messages.format_user_message(budget, locale="ru")  # falls back to en


# ---------------------------------------------------------------------------
# get_user_message — raw lookup
# ---------------------------------------------------------------------------
def test_get_user_message_returns_default_for_known_code():
    assert messages.get_user_message("NR-W002") == messages.DEFAULT_MESSAGES["NR-W002"]


def test_get_user_message_returns_fallback_for_unknown_code():
    assert messages.get_user_message("NR-NOPE") == messages.FALLBACK_MESSAGE


# ---------------------------------------------------------------------------
# set_user_message / reset_overrides — per-process customization
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolate_overrides():
    """Snapshot/restore the override dict around every test.

    Without this, a stray ``set_user_message`` in one test leaks into
    others — same gotcha as bare ``module.X = Y`` in pytest, see
    [[test-isolation-monkeypatch-setattr]] in project memory.
    """
    saved = dict(messages._overrides)
    try:
        yield
    finally:
        messages._overrides.clear()
        messages._overrides.update(saved)


def test_set_user_message_overrides_catalog():
    messages.set_user_message("NR-B004", "Out of credits ☕")
    assert messages.get_user_message("NR-B004") == "Out of credits ☕"
    assert messages.format_user_message(
        exc.NullRunBudgetError("wf", "x")
    ) == "Out of credits ☕"


def test_set_user_message_with_empty_string_clears_override():
    messages.set_user_message("NR-B004", "Out of credits ☕")
    messages.set_user_message("NR-B004", "")
    assert messages.get_user_message("NR-B004") == messages.DEFAULT_MESSAGES["NR-B004"]


def test_set_user_message_only_affects_targeted_code():
    """Overriding one code must not bleed into siblings."""
    messages.set_user_message("NR-B004", "Branded budget message")
    assert messages.get_user_message("NR-T001") == messages.DEFAULT_MESSAGES["NR-T001"]


def test_reset_overrides_clears_all():
    messages.set_user_message("NR-B004", "x")
    messages.set_user_message("NR-T001", "y")
    messages.reset_overrides()
    assert messages.get_user_message("NR-B004") == messages.DEFAULT_MESSAGES["NR-B004"]
    assert messages.get_user_message("NR-T001") == messages.DEFAULT_MESSAGES["NR-T001"]


# ---------------------------------------------------------------------------
# Public API surface — names that should be importable from ``nullrun``
# ---------------------------------------------------------------------------
def test_format_user_message_importable_from_top_level():
    import nullrun
    assert hasattr(nullrun, "format_user_message")
    assert nullrun.format_user_message is messages.format_user_message


def test_set_user_message_importable_from_top_level():
    import nullrun
    assert hasattr(nullrun, "set_user_message")
    assert nullrun.set_user_message is messages.set_user_message


def test_get_user_message_importable_from_top_level():
    import nullrun
    assert hasattr(nullrun, "get_user_message")
    assert nullrun.get_user_message is messages.get_user_message


def test_format_and_set_listed_in_all_for_tab_completion():
    """Tab-completion discovery — these names should appear in
    ``dir(nullrun)`` so users find them without reading docs."""
    import nullrun
    assert "format_user_message" in nullrun.__all__
    assert "set_user_message" in nullrun.__all__
