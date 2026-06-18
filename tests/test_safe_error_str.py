"""SEC-29: regression tests for the error-string redaction in decorators.

The audit flagged that ``str(NullRunBlockedException)`` (and
``NullRunTransportError``) embed a caller-supplied ``details`` payload
which can include raw tool args / kwargs. We strip that payload before
handing the string to ``_emit_span_end`` so the audit log only sees
the stable envelope.

These tests pin the redaction contract: the redacted form must drop
``details={...}`` from the message, must leave the rest of the
envelope intact, and must accept ``None`` without raising.
"""

from __future__ import annotations

from nullrun.breaker.exceptions import (
    NullRunBlockedException,
    NullRunTransportError,
    TransportErrorSource,
)
from nullrun.decorators import _DETAILS_REDACTED, _safe_error_str


def test_none_returns_none() -> None:
    assert _safe_error_str(None) is None


def test_blocked_exception_strips_details() -> None:
    exc = NullRunBlockedException(
        workflow_id="wf-1",
        reason="rate limit",
        action="pause",
        tool_name="send_email",
        user_pii="alice@example.com",
        count=3,
    )
    redacted = _safe_error_str(exc)
    assert redacted is not None
    assert "alice@example.com" not in redacted
    assert "user_pii" not in redacted
    assert "count" not in redacted
    assert "wf-1" in redacted
    assert "rate limit" in redacted
    assert "pause" in redacted
    assert "send_email" in redacted
    assert _DETAILS_REDACTED in redacted


def test_transport_error_strips_details() -> None:
    exc = NullRunTransportError(
        "connection refused",
        TransportErrorSource.NETWORK_ERROR,
        "/api/check",
        query='SELECT * FROM users WHERE email="alice@x.com"',
        timeout=5,
    )
    redacted = _safe_error_str(exc)
    assert redacted is not None
    assert "alice@x.com" not in redacted
    assert "timeout" not in redacted
    assert "connection refused" in redacted
    assert "NETWORK_ERROR" in redacted
    assert _DETAILS_REDACTED in redacted


def test_plain_exception_unchanged() -> None:
    """Non-blocker exceptions have no `details=...` substring; pass through."""
    exc = RuntimeError("boom")
    assert _safe_error_str(exc) == "boom"


def test_blocked_without_details_does_not_inject_redaction_marker() -> None:
    """If a future exception type has no `details=...` substring, don't add one."""
    exc = ValueError("no details here")
    redacted = _safe_error_str(exc)
    assert redacted == "no details here"
    assert _DETAILS_REDACTED not in redacted
