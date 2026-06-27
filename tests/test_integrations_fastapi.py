"""Tests for the FastAPI integration.

Each test mounts a tiny FastAPI app whose handler raises a specific
NullRun exception, then asserts the HTTP response matches the
documented contract (status code, JSON body, headers).

Locale is pinned via the ``Accept-Language`` header (or the custom
``locale_resolver`` where relevant) so the rendered ``user_message``
is deterministic.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from nullrun.breaker import exceptions as exc
from nullrun.integrations import fastapi as nr_fastapi


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------
def _build_app(handler):
    """Build a minimal FastAPI app with the NullRun integration
    installed and a single endpoint that delegates to ``handler``."""
    app = FastAPI()
    nr_fastapi.install(app)

    @app.get("/trigger")
    def trigger():
        return handler()

    return app


# ---------------------------------------------------------------------------
# NullRunDecision → 4xx with user_message
# ---------------------------------------------------------------------------
def test_budget_error_returns_429_with_user_message():
    app = _build_app(
        lambda: (_ for _ in ()).throw(exc.NullRunBudgetError("wf", "budget_cents=500"))
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/trigger", headers={"Accept-Language": "en"})
    assert resp.status_code == 429
    body = resp.json()
    assert body["error_code"] == "NR-B004"
    assert body["category"] == "decision"
    assert body["user_message"] == "You've reached the usage limit for this conversation. Please try again later."
    assert body["retryable"] is False


def test_tool_blocked_returns_403_with_user_message():
    app = _build_app(
        lambda: (_ for _ in ()).throw(
            exc.NullRunToolBlockedError("wf", "blocked", tool_name="send_email")
        )
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/trigger")
    assert resp.status_code == 403
    body = resp.json()
    assert body["error_code"] == "NR-T001"
    assert body["category"] == "decision"
    assert "isn't available right now" in body["user_message"]


def test_workflow_paused_returns_503():
    app = _build_app(
        lambda: (_ for _ in ()).throw(
            exc.WorkflowPausedException("wf", "cooldown", resume_after=30)
        )
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/trigger")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error_code"] == "NR-W003"
    assert body["category"] == "decision"
    # The exception carried resume_after — middleware should set
    # Retry-After on the response so HTTP clients back off correctly.
    assert resp.headers.get("Retry-After") == "30"


def test_generic_block_returns_403():
    app = _build_app(
        lambda: (_ for _ in ()).throw(exc.NullRunBlockedException("wf", "blocked"))
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/trigger")
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "NR-X001"


# ---------------------------------------------------------------------------
# NullRunInfrastructureError → 503
# ---------------------------------------------------------------------------
def test_transport_error_returns_503_with_user_message():
    app = _build_app(
        lambda: (_ for _ in ()).throw(
            exc.NullRunTransportError(
                "boom",
                source=exc.TransportErrorSource.NETWORK_ERROR,
                endpoint="execute",
            )
        )
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/trigger")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error_code"] == "NR-B001"
    assert body["category"] == "infrastructure"
    assert "trouble connecting" in body["user_message"]


def test_backend_error_returns_503_with_user_message():
    app = _build_app(
        lambda: (_ for _ in ()).throw(exc.NullRunBackendError("5xx", endpoint="check"))
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/trigger")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error_code"] == "NR-B002"
    assert body["category"] == "infrastructure"


def test_auth_error_returns_503():
    """Auth failures are infrastructure-side (key rejected), so we map
    them to 503 even though the user did nothing wrong."""
    app = _build_app(lambda: (_ for _ in ()).throw(exc.NullRunAuthError("rejected")))
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/trigger")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error_code"] == "NR-A003"
    assert body["category"] == "infrastructure"


def test_rate_limit_error_surfaces_retry_after_header():
    """RateLimitError carries ``retry_after`` — the middleware must
    forward it as the ``Retry-After`` HTTP header."""
    app = _build_app(
        lambda: (_ for _ in ()).throw(
            exc.RateLimitError(
                "rate limited",
                source=exc.TransportErrorSource.GATEWAY_ERROR,
                endpoint="check",
                retry_after=42,
            )
        )
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/trigger")
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "42"
    body = resp.json()
    assert body["error_code"] == "NR-R001"


# ---------------------------------------------------------------------------
# WorkflowKilledInterrupt (BaseException) → 503 with kill message
# ---------------------------------------------------------------------------
def test_workflow_killed_interrupt_returns_503():
    """Kill is a BaseException subclass — the middleware must catch it
    explicitly (not via NullRunError) and render NR-W002."""
    app = _build_app(
        lambda: (_ for _ in ()).throw(
            exc.WorkflowKilledInterrupt("wf", "killed via API")
        )
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/trigger")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error_code"] == "NR-W002"
    assert body["category"] == "killed"
    assert "administrator" in body["user_message"]


# ---------------------------------------------------------------------------
# Locale resolution
# ---------------------------------------------------------------------------
def test_accept_language_header_drives_locale():
    """``Accept-Language: en`` returns the English catalog text."""
    app = _build_app(
        lambda: (_ for _ in ()).throw(exc.NullRunBudgetError("wf", "x"))
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/trigger", headers={"Accept-Language": "en-US,en;q=0.9"})
    assert resp.json()["user_message"] == (
        "You've reached the usage limit for this conversation. "
        "Please try again later."
    )


def test_missing_accept_language_falls_back_to_english():
    app = _build_app(
        lambda: (_ for _ in ()).throw(exc.NullRunBudgetError("wf", "x"))
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/trigger")  # no Accept-Language header
    assert resp.json()["user_message"] == (
        "You've reached the usage limit for this conversation. "
        "Please try again later."
    )


def test_custom_locale_resolver_overrides_accept_language():
    """A custom resolver wins over Accept-Language — useful when the
    locale comes from a session cookie or JWT claim instead."""
    def resolver(request: Request) -> str:
        return request.headers.get("x-locale", "en")

    app = FastAPI()
    nr_fastapi.install(app, locale_resolver=resolver)

    @app.get("/trigger")
    def trigger():
        raise exc.NullRunBudgetError("wf", "x")

    client = TestClient(app, raise_server_exceptions=False)
    # Different Accept-Language, but the resolver forces en.
    resp = client.get(
        "/trigger",
        headers={"Accept-Language": "fr-FR", "x-locale": "en"},
    )
    assert resp.json()["user_message"] == (
        "You've reached the usage limit for this conversation. "
        "Please try again later."
    )


def test_resolver_exception_falls_back_to_english():
    """A buggy resolver must not crash the error response — the user
    still gets a clean message, just in the default locale."""
    def bad_resolver(request: Request) -> str:
        raise RuntimeError("resolver bug")

    app = FastAPI()
    nr_fastapi.install(app, locale_resolver=bad_resolver)

    @app.get("/trigger")
    def trigger():
        raise exc.NullRunBudgetError("wf", "x")

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/trigger")
    assert resp.status_code == 429
    assert "usage limit" in resp.json()["user_message"]


# ---------------------------------------------------------------------------
# Happy path — middleware does not interfere with normal responses
# ---------------------------------------------------------------------------
def test_normal_endpoint_unchanged():
    """If no NullRun exception fires, the handler returns the body
    exactly as written. The middleware is exception-only."""
    app = FastAPI()
    nr_fastapi.install(app)

    @app.get("/ok")
    def ok():
        return {"hello": "world"}

    client = TestClient(app)
    resp = client.get("/ok")
    assert resp.status_code == 200
    assert resp.json() == {"hello": "world"}


def test_install_is_idempotent():
    """Calling install() twice on the same app must not double-register
    handlers — the second call replaces the first."""
    app = FastAPI()
    nr_fastapi.install(app)
    nr_fastapi.install(app)  # second call

    @app.get("/trigger")
    def trigger():
        raise exc.NullRunBudgetError("wf", "x")

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/trigger")
    # Single 429, not a double-handler crash.
    assert resp.status_code == 429
    assert resp.json()["error_code"] == "NR-B004"


# ---------------------------------------------------------------------------
# _build_headers edge cases — Retry-After handling
# ---------------------------------------------------------------------------
class _AttrBag:
    """Minimal stand-in for a NullRun exception — only the attrs
    that ``_build_headers`` reads (``retry_after`` / ``resume_after``)
    matter."""

    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


def test_build_headers_returns_empty_when_no_retry_hint():
    """No ``retry_after`` / ``resume_after`` → no Retry-After header."""
    assert nr_fastapi._build_headers(_AttrBag()) == {}


def test_build_headers_returns_empty_when_retry_after_non_numeric():
    """A non-numeric ``retry_after`` must NOT raise; it just yields
    no header. The exception class is opaque to the renderer, so a
    typo'd string field shouldn't break the response."""
    assert nr_fastapi._build_headers(_AttrBag(retry_after="soon")) == {}


def test_build_headers_returns_empty_when_retry_after_is_zero():
    """Zero or negative ``retry_after`` is not meaningful for
    Retry-After (RFC 9110 allows zero but a real client would
    spin; the renderer drops it to avoid hot-looping)."""
    assert nr_fastapi._build_headers(_AttrBag(retry_after=0)) == {}
    assert nr_fastapi._build_headers(_AttrBag(retry_after=-5)) == {}


def test_build_headers_falls_back_to_resume_after():
    """``WorkflowPausedException`` uses ``resume_after`` instead of
    ``retry_after`` — the renderer normalizes on the canonical
    HTTP field name."""
    assert nr_fastapi._build_headers(_AttrBag(resume_after=42)) == {"Retry-After": "42"}
