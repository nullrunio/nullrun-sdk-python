"""
Tests for the developer-facing error report rendered by ``guard``
and the CLI ``init(fail_on_exit=True)`` path.

Pre-fix (2026-09-22), the catch-all exit path printed only the catalog
user-message ("There's a configuration issue. Please contact support.")
to stderr. That wording is correct for end-users but gave a developer
running an example with a missing ``NULLRUN_API_KEY`` zero actionable
detail. The new report has four lines that answer the four questions a
developer actually asks:

  1. **what**  -- the stage that failed (auth / gate / track / ...)
  2. **where** -- the wire endpoint + status code + transport source
  3. **why**   -- the underlying exception message + machine error_code
  4. **how to fix** -- the ``user_action`` from the typed class

These tests pin the four-line invariant so a future "let's tidy up the
error path" cannot silently drop the structured detail back to a single
sentence.

History: 0.18.4 renamed ``handle`` to ``guard``.
"""
from __future__ import annotations

import pytest

import nullrun
from nullrun import guard
from nullrun._handle import _render_dev_error_report
from nullrun.breaker.exceptions import (
    NullRunAuthenticationError,
    NullRunError,
    NullRunTransportError,
)

# --- _render_dev_error_report unit tests -----------------------------------


def test_report_includes_what_where_why_and_fix():
    """The four headline lines are always present, in order, with the
    catalog user-message as line 1."""
    exc = NullRunAuthenticationError(
        "Auth failed with status 401. API key may be invalid or expired.",
        error_code="NR-A003",
        user_action="Rotate the API key in the dashboard.",
    )
    report = _render_dev_error_report(exc, "There's a configuration issue. Please contact support.")

    lines = report.split("\n")
    assert lines[0] == "There's a configuration issue. Please contact support."
    # Line 2 carries the [error_code] + what + retryable hint.
    assert "[NR-A003]" in lines[1]
    assert "what:" in lines[1]
    assert "authentication" in lines[1].lower()
    assert "not retryable" in lines[1].lower()
    # Line 3 is the where line -- endpoint + status when known.
    assert "where:" in lines[2]
    assert "endpoint=" in lines[2]
    # Line 4 is the underlying exception message.
    assert "why:" in lines[3]
    assert "Auth failed with status 401" in lines[3]
    # Line 5 is the user_action -- the developer-facing fix.
    assert "how to fix:" in lines[4]
    assert "Rotate the API key" in lines[4]


def test_report_stage_derived_from_class_name_when_no_endpoint():
    """When the exception has no ``endpoint`` attribute, the stage label
    falls back to the class name. ``NullRunAuthenticationError`` should
    derive to ``authentication`` -- not the raw CamelCase."""
    exc = NullRunError("oops", error_code="NR-0000")
    report = _render_dev_error_report(exc, "Something went wrong.")
    assert "authentication" not in report.lower()  # this one is the base class
    assert "[NR-0000]" in report


def test_report_includes_transport_endpoint_and_source():
    """Transport errors carry ``endpoint`` + ``source`` + ``status_code``;
    all three must show up on the where line so the developer can tell
    apart a network blip from a backend-side 5xx."""
    from nullrun.transport import TransportErrorSource

    exc = NullRunTransportError(
        "Auth request failed: connection refused.",
        source=TransportErrorSource.NETWORK_ERROR,
        endpoint="auth",
    )
    report = _render_dev_error_report(exc, "I'm having trouble connecting.")

    assert "endpoint=auth" in report
    assert "NETWORK_ERROR" in report or "network_error" in report
    assert "why:" in report
    assert "connection refused" in report.lower()


def test_report_truncates_long_underlying_messages():
    """A verbose backend response (e.g. a Postgres stack trace) must not
    blow up the terminal. Cap at 400 chars + ellipsis."""
    long_msg = "x" * 1000
    exc = NullRunError(long_msg, error_code="NR-B002")
    report = _render_dev_error_report(exc, "Service unavailable.")
    # The why line carries at most 400 chars of the message.
    why_line = next(line for line in report.split("\n") if line.startswith("           why:"))
    assert len(why_line) < 400 + len("           why:") + 5
    assert "..." in why_line


def test_report_omits_fix_line_when_user_action_is_empty():
    """Some legacy exceptions have an empty ``user_action``. The report
    must not print an empty ``how to fix:`` line in that case -- it's
    noise that suggests the SDK forgot to set the hint."""
    exc = NullRunError("oops", error_code="NR-0000")
    # Base NullRunError default has user_action="".
    report = _render_dev_error_report(exc, "Something went wrong.")
    assert "how to fix" not in report


def test_report_includes_docs_url():
    """The docs URL is the developer's escape hatch for unfamiliar
    error codes. Always present when the class sets it (the base class
    default is the generic error catalog URL)."""
    exc = NullRunError("oops", error_code="NR-B002")
    report = _render_dev_error_report(exc, "Service unavailable.")
    assert "docs:" in report
    assert "https://docs.nullrun.io" in report


# --- guard() integration tests ---------------------------------------------


def test_guard_prints_full_dev_report(monkeypatch, capsys):
    """``with guard():`` exits 1 AND writes the four-line dev report
    to stderr -- not just the catalog headline."""
    exits = []

    def fake_exit(code):
        exits.append(code)
        raise SystemExit(code)

    monkeypatch.setattr("sys.exit", fake_exit)

    with pytest.raises(SystemExit):
        with guard():
            raise NullRunAuthenticationError(
                "Auth failed with status 401.",
                error_code="NR-A003",
                user_action="Rotate the API key.",
            )

    assert exits == [1]
    err = capsys.readouterr().err
    # All four structured lines must be present.
    assert "[NR-A003]" in err
    assert "what:" in err
    assert "where:" in err
    assert "why:" in err
    assert "how to fix:" in err
    assert "Rotate the API key." in err


def test_guard_falls_back_to_legacy_on_helper_bug(monkeypatch, capsys):
    """Defensive: if the report builder itself raises (a future bug),
    ``guard()`` must still exit cleanly with the catalog headline.
    The defensive fallback path is critical -- a buggy helper cannot
    freeze a script that would otherwise exit."""
    from nullrun import _handle as handle_mod

    def broken_render(exc, user_message):
        raise RuntimeError("simulated bug in report builder")

    monkeypatch.setattr(handle_mod, "_render_dev_error_report", broken_render)

    exits = []

    def fake_exit(code):
        exits.append(code)
        raise SystemExit(code)

    monkeypatch.setattr("sys.exit", fake_exit)

    with pytest.raises(SystemExit):
        with guard():
            raise NullRunError("oops", error_code="NR-B002")

    assert exits == [1]
    err = capsys.readouterr().err
    # Falls back to the catalog headline verbatim -- the user still sees
    # *something* and the script still exits with the right code.
    assert "temporarily unavailable" in err.lower()


def test_guard_report_uses_class_name_for_unknown_endpoint(monkeypatch, capsys):
    """When the exception has no ``endpoint`` attribute, the where
    line uses ``endpoint=N/A (config-time failure)`` so the developer
    can immediately tell that the failure happened at startup, not on
    a real wire call."""
    exits = []

    def fake_exit(code):
        exits.append(code)
        raise SystemExit(code)

    monkeypatch.setattr("sys.exit", fake_exit)

    with pytest.raises(SystemExit):
        with guard():
            raise NullRunError(
                "config-time failure",
                error_code="NR-C001",
                user_action="Set NULLRUN_API_KEY.",
            )

    err = capsys.readouterr().err
    assert "endpoint=N/A" in err
    assert "config-time failure" in err


# --- sanity: still callable without runtime --------------------------------


def test_guard_does_not_require_runtime():
    """``guard`` must work without ``nullrun.init()``. Sanity check
    that the helper module is importable on its own."""
    assert callable(guard)
    assert callable(nullrun.guard)
