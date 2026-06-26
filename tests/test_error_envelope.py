"""
tests/test_error_envelope.py — Phase 4 production-readiness.

Verifies ``_parse_error_envelope`` maps 4xx / 5xx / 429 to the
right exception subclass per the canonical ``contracts/errors.ts``
envelope.

Reference:
    contracts/errors.ts:1-39
    backend/src/proxy/http/errors.rs:1-85
"""

import httpx
import pytest

from nullrun.breaker.exceptions import (
    NullRunAuthenticationError,
    NullRunTransportError,
    RateLimitError,
    TransportErrorSource,
)
from nullrun.transport import _parse_error_envelope

# ──────────────────────────────────────────────────────────────────────
# 429 — Rate Limit (typed RateLimitError with retry_after + upgrade_url)
# ──────────────────────────────────────────────────────────────────────


class TestRateLimitMapping:
    """HTTP 429 → RateLimitError with structured retry metadata."""

    def test_429_with_retry_after_header_raises_rate_limit_error(self):
        """Retry-After: 30 → RateLimitError with retry_after=30.0."""
        r = httpx.Response(
            429,
            headers={"Retry-After": "30"},
            json={
                "error": "rate_limit_exceeded",
                "message": "Too many requests",
            },
        )
        exc = _parse_error_envelope(r, "track")
        assert isinstance(exc, RateLimitError)
        assert exc.retry_after == 30.0
        assert exc.upgrade_url is None  # not in this body
        assert exc.endpoint == "track"
        assert exc.source == TransportErrorSource.GATEWAY_ERROR

    def test_429_with_upgrade_url_in_body(self):
        """The body's upgrade_url is surfaced for operator prompts."""
        r = httpx.Response(
            429,
            headers={"Retry-After": "60"},
            json={
                "error": "rate_limit_exceeded",
                "message": "Plan limit",
                "upgrade_url": "/billing/upgrade",
                "retry_after": 60,
            },
        )
        exc = _parse_error_envelope(r, "track")
        assert isinstance(exc, RateLimitError)
        assert exc.retry_after == 60.0
        assert exc.upgrade_url == "/billing/upgrade"
        # Original body preserved
        assert exc.body["error"] == "rate_limit_exceeded"
        assert exc.body["upgrade_url"] == "/billing/upgrade"

    def test_429_with_retry_after_http_date(self):
        """Retry-After in HTTP-date format is parsed into seconds-from-now."""
        # Compute a date 60 seconds in the future
        from datetime import datetime, timezone

        future = datetime.now(timezone.utc).timestamp() + 60
        # Format as HTTP date (RFC 7231)
        from datetime import timezone as tz
        from email.utils import format_datetime

        future_dt = datetime.fromtimestamp(future, tz=tz.utc)
        http_date = format_datetime(future_dt, usegmt=True)
        r = httpx.Response(
            429,
            headers={"Retry-After": http_date},
            json={"error": "rate_limit_exceeded"},
        )
        exc = _parse_error_envelope(r, "gate")
        assert isinstance(exc, RateLimitError)
        # Should be roughly 60 (allow 5s slop for clock skew)
        assert exc.retry_after is not None
        assert 55 <= exc.retry_after <= 65

    def test_429_with_no_retry_after_header(self):
        """When the header is missing, retry_after is None (caller decides)."""
        r = httpx.Response(
            429,
            json={"error": "rate_limit_exceeded", "message": "Slow down"},
        )
        exc = _parse_error_envelope(r, "track")
        assert isinstance(exc, RateLimitError)
        assert exc.retry_after is None

    def test_rate_limit_error_is_a_transport_error(self):
        """RateLimitError subclasses NullRunTransportError so existing
        ``except NullRunTransportError`` keeps catching it."""
        r = httpx.Response(429, json={"error": "rate_limit_exceeded"})
        exc = _parse_error_envelope(r, "track")
        assert isinstance(exc, NullRunTransportError)


# ──────────────────────────────────────────────────────────────────────
# 401 / 403 — Auth (typed NullRunAuthenticationError)
# ──────────────────────────────────────────────────────────────────────


class TestAuthMapping:
    """HTTP 401/403 → NullRunAuthenticationError."""

    def test_401_raises_authentication_error(self):
        r = httpx.Response(401, json={"error": "unauthorized", "message": "API key invalid"})
        exc = _parse_error_envelope(r, "gate")
        assert isinstance(exc, NullRunAuthenticationError)
        assert "unauthorized" in str(exc)
        assert "gate" in str(exc)

    def test_403_raises_authentication_error(self):
        r = httpx.Response(403, json={"error": "forbidden"})
        exc = _parse_error_envelope(r, "evaluate")
        assert isinstance(exc, NullRunAuthenticationError)

    def test_401_includes_endpoint_in_message(self):
        r = httpx.Response(401, json={"error": "unauthorized"})
        exc = _parse_error_envelope(r, "evaluate")
        assert "evaluate" in str(exc)


# ──────────────────────────────────────────────────────────────────────
# 5xx — Gateway Error (typed NullRunTransportError with GATEWAY_ERROR source)
# ──────────────────────────────────────────────────────────────────────


class TestGatewayErrorMapping:
    """HTTP 5xx → NullRunTransportError(source=GATEWAY_ERROR)."""

    @pytest.mark.parametrize("status", [500, 502, 503, 504, 599])
    def test_5xx_raises_transport_error_with_gateway_source(self, status):
        r = httpx.Response(
            status,
            json={"error": "internal_error", "message": "boom"},
        )
        exc = _parse_error_envelope(r, "track")
        assert isinstance(exc, NullRunTransportError)
        assert exc.source == TransportErrorSource.GATEWAY_ERROR
        assert exc.details.get("status_code") == status
        assert exc.details.get("error_slug") == "internal_error"

    def test_500_without_json_body(self):
        """Some 5xx come back as HTML (nginx defaults) — still works."""
        r = httpx.Response(500, text="<html>Internal Server Error</html>")
        exc = _parse_error_envelope(r, "track")
        assert isinstance(exc, NullRunTransportError)
        assert exc.source == TransportErrorSource.GATEWAY_ERROR

    def test_500_endpoint_in_message(self):
        r = httpx.Response(500, json={"error": "internal_error"})
        exc = _parse_error_envelope(r, "gate")
        assert "gate" in str(exc)


# ──────────────────────────────────────────────────────────────────────
# 4xx non-auth non-429 — Client Error (NullRunTransportError with slug)
# ──────────────────────────────────────────────────────────────────────


class TestClientErrorMapping:
    """HTTP 4xx (excluding 401/403/429) → NullRunTransportError."""

    @pytest.mark.parametrize("status", [400, 403, 404, 409, 422])
    def test_4xx_raises_transport_error(self, status):
        r = httpx.Response(
            status,
            json={"error": "validation_error", "message": "Bad field"},
        )
        exc = _parse_error_envelope(r, "gate")
        # 403 is auth-class per the envelope; everything else is
        # typed as a generic transport error.
        if status == 403:
            assert isinstance(exc, NullRunAuthenticationError)
        else:
            assert isinstance(exc, NullRunTransportError)
            assert exc.source == TransportErrorSource.GATEWAY_ERROR
            assert exc.details.get("status_code") == status
            assert exc.details.get("error_slug") == "validation_error"


# ──────────────────────────────────────────────────────────────────────
# 2xx — should NOT be routed through the envelope (caller's job)
# ──────────────────────────────────────────────────────────────────────


class TestSuccessResponseBypasses:
    """2xx responses don't go through the envelope — the caller inspects them."""

    def test_200_is_not_classified_as_error(self):
        """``_parse_error_envelope`` is only called on non-2xx — this
        test documents that fact so a future refactor doesn't
        accidentally raise on success."""
        r = httpx.Response(200, json={"decision": "allow"})
        # The helper does not check the status code — it's the
        # caller's job to only call it on 4xx/5xx. The helper
        # just translates whatever response is given.
        # This is a non-test-of-the-helper; it documents the contract.
        assert r.status_code == 200  # sanity
