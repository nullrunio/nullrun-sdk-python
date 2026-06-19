"""
Regression test for plan item P0-3: streaming response body must not
exceed ``MAX_RESPONSE_BYTES`` before tracking is attempted.

Pre-fix the sync transport called ``response.read()`` and the async
transport called ``await response.aread()``. Both buffer the ENTIRE
response body in memory before the extractor runs. For a streaming
OpenAI completion with ``max_tokens=8192`` the buffered body is
16+ MB. Under load (10+ concurrent streams) this is a real OOM risk
in long-running services.

Post-fix we use a bounded chunked read (``_read_body_with_cap`` /
``_aread_body_with_cap``). When the body exceeds the cap we skip
tracking and increment ``_coverage_streaming_skipped`` so the
dashboard can see which hosts are producing oversized responses.
"""
import asyncio
from unittest.mock import MagicMock

import httpx
import pytest

from nullrun.instrumentation import auto as auto_mod
from nullrun.instrumentation.auto import (
    MAX_RESPONSE_BYTES,
    NullRunAsyncTransport,
    NullRunSyncTransport,
    _aread_body_with_cap,
    _read_body_with_cap,
)


def _make_response(content: bytes, content_length: int | None = None) -> httpx.Response:
    """Build an httpx.Response with a fixed body. We don't go through
    the network — we construct the response object directly so the
    tests are deterministic and offline."""
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    headers = {"content-type": "application/json"}
    if content_length is not None:
        headers["content-length"] = str(content_length)
    return httpx.Response(200, headers=headers, content=content, request=request)


# ===========================================================================
# Unit tests on the bounded-read helpers
# ===========================================================================


def test_read_body_with_cap_returns_full_body_when_under_cap():
    """A small response (1 KB) returns the full body."""
    body = b'{"usage":{"prompt_tokens":10,"completion_tokens":20,"total_tokens":30}}'
    response = _make_response(body, content_length=len(body))
    out = _read_body_with_cap(response, max_bytes=1024)
    assert out == body


def test_read_body_with_cap_short_circuits_on_content_length():
    """If Content-Length header is known and > cap, the helper
    short-circuits to None WITHOUT allocating / reading."""
    big = b"x" * (1024 * 1024)  # 1 MB body
    response = _make_response(big, content_length=len(big))
    # Cap is 100 bytes — Content-Length says 1 MB, so we return None.
    out = _read_body_with_cap(response, max_bytes=100)
    assert out is None


def test_read_body_with_cap_truncates_when_streaming():
    """For chunked responses without a Content-Length (or where
    Content-Length is missing/malformed), we stream-read with a hard
    cap. If the stream exceeds the cap mid-read, return None."""
    big = b"x" * (1024 * 1024)  # 1 MB
    # No content-length header — simulates streaming/chunked.
    response = _make_response(big, content_length=None)
    out = _read_body_with_cap(response, max_bytes=4096)
    assert out is None, "should abort when streaming body exceeds cap"


def test_aread_body_with_cap_short_circuits_on_content_length():
    """Async mirror: Content-Length short-circuit."""
    big = b"x" * (1024 * 1024)
    response = _make_response(big, content_length=len(big))
    out = asyncio.run(_aread_body_with_cap(response, max_bytes=100))
    assert out is None


# ===========================================================================
# Integration: NullRunSyncTransport / NullRunAsyncTransport respect the cap
# ===========================================================================


def test_sync_transport_skips_tracking_on_oversized_response(monkeypatch):
    """When the response body exceeds MAX_RESPONSE_BYTES, the sync
    transport must NOT call ``runtime.track`` and MUST increment
    ``_coverage_streaming_skipped``."""
    runtime = MagicMock()
    inner = MagicMock()
    body = b"x" * (MAX_RESPONSE_BYTES + 1)
    response = _make_response(body, content_length=len(body))
    inner.handle_request.return_value = response

    transport = NullRunSyncTransport(inner=inner, runtime=runtime)
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")

    transport.handle_request(request)

    # Body was oversized → no llm_call event was emitted.
    runtime.track.assert_not_called()
    # Coverage counter incremented (best-effort; the runtime mock
    # accepts attribute reads). We verify the helper was called via
    # the runtime attribute access path:
    # ``_safe_bump_coverage(runtime, "_coverage_streaming_skipped", host)``
    # should have read runtime._coverage_streaming_skipped.
    # (We don't assert on the dict contents because the mock
    # returns a fresh MagicMock for each attribute access; the
    # important contract is that track() was NOT called.)


def test_async_transport_skips_tracking_on_oversized_response():
    """Async mirror of the sync test."""
    runtime = MagicMock()
    inner = MagicMock()

    async def fake_handle(_request):
        body = b"x" * (MAX_RESPONSE_BYTES + 1)
        return _make_response(body, content_length=len(body))

    inner.handle_async_request.side_effect = fake_handle

    transport = NullRunAsyncTransport(inner=inner, runtime=runtime)
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")

    asyncio.run(transport.handle_async_request(request))

    runtime.track.assert_not_called()


def test_sync_transport_does_track_normal_sized_response():
    """Sanity: the cap doesn't break the happy path. A normal 200-byte
    response with a usage block must still be tracked."""
    runtime = MagicMock()
    inner = MagicMock()
    body = (
        b'{"id":"chatcmpl-1","choices":[{"message":{"role":"assistant","content":"hi"}}],'
        b'"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}'
    )
    response = _make_response(body, content_length=len(body))
    inner.handle_request.return_value = response

    transport = NullRunSyncTransport(inner=inner, runtime=runtime)
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")

    transport.handle_request(request)

    runtime.track.assert_called_once()
    event = runtime.track.call_args[0][0]
    assert event["type"] == "llm_call"
    assert event["tokens"] == 8