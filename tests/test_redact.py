"""
Regression test for plan items P0-6 + P3-3: redact-before-truncate.

Pre-fix, ``_safe_repr(value, max_len=50)`` truncated ``repr(value)``
to 50 characters FIRST, and ``_strip_details_balanced`` was then
called separately on the truncated string (in ``_safe_error_str``).
If the ``details={...}`` substring lived past position 50 in the
original repr — a common case (the URL in an httpx.HTTPError is
often >50 chars before the dict payload), the substring was gone
from the truncated slice, the redact pass saw nothing, and the raw
``details={...}`` payload leaked into the span_event.

Post-fix ``_safe_repr`` runs redact-then-truncate on the full repr
and is the single source of truth (P3-3).

SECURITY INVARIANT (the only thing this test guards):
    The PII payload (``details={'card_number':...}``) MUST NOT
    appear in the output of ``_safe_repr``, regardless of whether
    the ``<redacted>`` marker is preserved by the truncate.

The presentation invariant (``<redacted>`` appears) is best-effort:
if the redact marker lives past the truncation point, we still don't
leak PII — we just don't get to see the redacted marker. That's
strictly safer than the pre-fix behavior, where PII was leaking.
"""

import pytest

from nullrun.decorators import _safe_error_str, _safe_repr, _strip_details_balanced


class TestSafeReprRedactsBeforeTruncating:
    """P0-6 security invariant: ``details={...}`` payloads past
    the truncation point MUST NOT leak into the output."""

    def test_details_beyond_truncation_point_does_not_leak(self):
        """A repr where ``details=`` sits at position 80 (past the
        default 50-char truncation) must end up with the secret
        value removed. Pre-fix this would have leaked the payload
        because ``_strip_details_balanced`` saw the truncated
        slice with no ``details=`` substring.
        """
        prefix = "x" * 80
        value = f"{prefix} details={{'secret': 'PII'}}"
        out = _safe_repr(value, max_len=50)
        # The SECRET value MUST NOT appear.
        assert "PII" not in out, f"P0-6 regression: PII leaked through _safe_repr. Output: {out!r}"
        assert "secret" not in out, (
            f"P0-6 regression: secret key leaked through _safe_repr. Output: {out!r}"
        )

    def test_details_within_truncation_window_is_redacted(self):
        """Sanity: when ``details=`` is within the truncation window
        redaction happens AND the marker is preserved (pre-fix
        happy path is unaffected by the post-fix order)."""
        value = "details={'x': 1}"
        out = _safe_repr(value, max_len=50)
        assert "x" not in out
        assert "<redacted>" in out

    def test_no_details_substring_just_truncates(self):
        """When the repr contains no ``details={...}``, the string
        is just truncated (no spurious redaction)."""
        value = "a" * 200
        out = _safe_repr(value, max_len=50)
        # repr(value) is `'aaa...'` (with outer quotes). _safe_repr
        # takes the first 50 chars of that repr and appends the
        # truncation marker. So the output starts with the repr's
        # opening quote and ends with the marker.
        assert out.startswith("'")
        assert "...<truncated>" in out
        # Total length: 50 (first 50 chars of repr) + len("...<truncated>") = 64.
        assert len(out) == 50 + len("...<truncated>")

    def test_repr_of_exception_with_long_url_redacts_card_number(self):
        """An httpx-like exception string with a long URL followed by
        a ``details={...}`` payload is the canonical P0-6
        regression scenario. Pre-fix the URL filled the first 50
        chars and ``details=`` was chopped off, leaking the card
        number. Post-fix the redact runs on the full repr and the
        card number never appears in the output."""
        exc_msg = (
            "HTTPError: http://api.example.com/v1/charge?amount=999&"
            "currency=USD&trace=abcdef0123456789 details="
            "{'card_number': '4111-1111-1111-1111', 'cvv': '123'}"
        )
        out = _safe_repr(exc_msg, max_len=50)
        # The card_number MUST NOT appear in the output.
        assert "4111" not in out, (
            f"P0-6 regression: card_number leaked through _safe_repr. Output: {out!r}"
        )
        assert "cvv" not in out, f"P0-6 regression: cvv leaked through _safe_repr. Output: {out!r}"
        assert "123" not in out, (
            f"P0-6 regression: cvv value leaked through _safe_repr. Output: {out!r}"
        )


class TestSafeErrorStrPipeline:
    """P3-3: ``_safe_error_str`` and ``_safe_repr`` are now two
    views over the same redact-then-truncate pipeline. They MUST
    produce consistent output for the same input."""

    def test_safe_error_str_redacts_card_number_in_long_message(self):
        """The same exception-message scenario as above, but going
        through ``_safe_error_str`` (the public span-event hook)."""
        exc_msg = (
            "HTTPError: http://api.example.com/v1/charge?amount=999&"
            "currency=USD&trace=abcdef0123456789 details="
            "{'card_number': '4111-1111-1111-1111', 'cvv': '123'}"
        )
        out = _safe_error_str(Exception(exc_msg))
        assert out is not None
        assert "4111" not in out, f"_safe_error_str leaked card_number. Output: {out!r}"

    def test_safe_error_str_none_returns_none(self):
        """Sanity: ``None`` in → ``None`` out, no redact call."""
        assert _safe_error_str(None) is None

    def test_safe_error_str_preserves_non_details_text(self):
        """Redaction is surgical — only ``details={...}`` is replaced
        free-form text around it is preserved (when not truncated)."""
        exc_msg = "Operation failed: foo bar details={'secret': 'x'} baz"
        out = _safe_error_str(Exception(exc_msg))
        assert out is not None
        assert "Operation failed" in out
        assert "foo bar" in out
        assert "baz" in out
        assert "secret" not in out
        assert "<redacted>" in out


class TestStripDetailsBalancedStillCallable:
    """The lower-level helper stays public (it's used by
    ``_safe_repr`` internally and is the building block for any
    future callers that need raw redaction without truncation).
    This test guards against an accidental rename / removal."""

    def test_strip_details_balanced_replaces_with_marker(self):
        """The helper returns ``details=<redacted>`` (with the
        ``details=`` prefix preserved) so callers can grep for it.
        """
        text = "details={'x': 1}"
        assert _strip_details_balanced(text) == "details=<redacted>"

    def test_strip_details_balanced_handles_nested_braces(self):
        """A ``details={'a': {'b': 1}}`` block redacts the whole
        nested structure (not just the outer one)."""
        text = "details={'a': {'b': 1}}"
        out = _strip_details_balanced(text)
        assert "b" not in out
        assert "<redacted>" in out
