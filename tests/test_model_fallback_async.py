"""
Regression test for UI-UX-AUDIT 2026-08-14 finding F-29:
NullRunAsyncTransport must fall back to the request body's ``model``
field when the response omits it, mirroring the sync path's
``_extract_model_from_request_body` wiring (auto.py:882-885).

Pre-fix the async ``_emit`` stopped at ``usage.get("model")`` — if
the upstream Anthropic / OpenAI streaming response did NOT carry a
``model`` field in the final chunk (or the extractor failed to
populate it), the emitted ``llm_call`` event had ``model=None``,
which the wire-format builder dropped, which the backend then
``unwrap_or("default")``'d to ``DEFAULT_RATE`` and warned
``no canonical rate for model``. Net effect: silent zero-billing
for async streaming clients.

Post-fix the async path mirrors the sync path:

    model_for_event = (
        usage.get("model")
        or _extract_model_from_request_body(request)
    )

The helper is module-level, sync-pure (reads ``request.content``
+ ``json.loads``), and safe to call from the async event loop —
no I/O, no blocking.

The sync path is tested in ``test_model_fallback.py``; this file
is the async-mirror integration coverage.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import httpx

from nullrun.instrumentation.auto import NullRunAsyncTransport


def _make_request_body(model: str | None) -> bytes:
    """Build a request body with the given model — what the SDK user
    embedded in their ChatOpenAI / ChatAnthropic constructor."""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1024,
    }
    # httpx.Request.content requires bytes.
    return json.dumps(body).encode()


def _make_response_with_usage(content: bytes, content_length: int) -> httpx.Response:
    """Build an httpx.Response that has a usage block but NO model
    field — the failure case F-29 closes."""
    request = httpx.Request(
        "POST",
        "https://api.anthropic.com/v1/messages",
        content=content,
    )
    return httpx.Response(
        200,
        headers={
            "content-type": "application/json",
            "content-length": str(content_length),
        },
        content=content,
        request=request,
    )


def _build_response_body_no_model() -> bytes:
    """Anthropic-style response body that carries a usage block but
    no top-level ``model`` field. The audit found this is the common
    shape for async streaming clients — the model is implicit in
    the API path, not the response body."""
    return json.dumps(
        {
            "id": "msg_01",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                # NOTE: no ``model`` field here — this is what F-29
                # closes.
            },
        }
    ).encode()


def _make_async_inner(body: bytes) -> MagicMock:
    """Build a MagicMock inner transport that returns a fixed body
    on ``handle_async_request``. Mirrors the pattern in
    ``test_streaming_oom_cap.py``."""
    inner = MagicMock()

    async def fake_handle(_request):
        return _make_response_with_usage(body, content_length=len(body))

    inner.handle_async_request.side_effect = fake_handle
    return inner


def test_async_transport_falls_back_to_request_body_model():
    """When the response body omits ``model``, the async transport
    must extract it from the request body. The emitted event's
    ``model`` field must carry the request-body value."""
    runtime = MagicMock()
    request_body = _build_response_body_no_model()  # no model field
    inner = _make_async_inner(request_body)

    transport = NullRunAsyncTransport(inner=inner, runtime=runtime)

    # The request body carries model="claude-sonnet-4-6" — that's
    # what we expect the event to surface.
    sent_request_body = _make_request_body("claude-sonnet-4-6")
    request = httpx.Request(
        "POST",
        "https://api.anthropic.com/v1/messages",
        content=sent_request_body,
    )

    asyncio.run(transport.handle_async_request(request))

    # Track was called.
    runtime.track.assert_called_once()
    event = runtime.track.call_args[0][0]

    # The event's ``model`` field is the request-body value, not None.
    assert event["model"] == "claude-sonnet-4-6", (
        f"F-29: async transport must fall back to request body model "
        f"when response body omits it; got event['model']={event['model']!r}"
    )
    # tracked is True (we got usage data, model is best-effort).
    assert event["metadata"]["tracked"] is True


def test_async_transport_prefers_response_body_model():
    """When the response body DOES carry ``model``, the response-body
    value wins — the request-body is only the fallback. This matches
    the sync path's `or` chain."""
    runtime = MagicMock()
    # Response body WITH model field — response wins.
    response_body = json.dumps(
        {
            "id": "msg_01",
            "model": "claude-opus-4-1",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    ).encode()
    inner = _make_async_inner(response_body)

    transport = NullRunAsyncTransport(inner=inner, runtime=runtime)

    sent_request_body = _make_request_body("claude-sonnet-4-6")
    request = httpx.Request(
        "POST",
        "https://api.anthropic.com/v1/messages",
        content=sent_request_body,
    )

    asyncio.run(transport.handle_async_request(request))

    runtime.track.assert_called_once()
    event = runtime.track.call_args[0][0]
    assert event["model"] == "claude-opus-4-1", (
        f"F-29: response-body model must win over request-body when both "
        f"present; got event['model']={event['model']!r}"
    )


def test_async_transport_emits_none_when_neither_source_has_model():
    """Both response body and request body omit ``model`` — the event
    must surface ``model=None`` (the backend's wire-format builder
    drops it, and the cost pipeline ``unwrap_or("default")``s). This
    is the same fallback behaviour as the sync path; not ideal but
    documented and consistent."""
    runtime = MagicMock()
    response_body = _build_response_body_no_model()  # no model
    inner = _make_async_inner(response_body)

    transport = NullRunAsyncTransport(inner=inner, runtime=runtime)

    # Request body also has no model field.
    sent_request_body = _make_request_body(None)
    request = httpx.Request(
        "POST",
        "https://api.anthropic.com/v1/messages",
        content=sent_request_body,
    )

    asyncio.run(transport.handle_async_request(request))

    runtime.track.assert_called_once()
    event = runtime.track.call_args[0][0]
    # Both sources None -> event["model"] is None. The backend's
    # ``DEFAULT_RATE`` fallback is the same as the pre-fix behaviour
    # in this corner case; the F-29 win is that the (very common)
    # one-source-has-model case now resolves correctly.
    assert event["model"] is None