"""
Regression test for plan item P2-1: coverage_seen must be incremented
in the httpx path, not only the requests path.

Pre-fix, ``_safe_bump_coverage(runtime, "_coverage_seen", host)`` was
only called from ``auto_requests.py:185``. The httpx transport's
``_emit`` (which handles ~95% of LLM traffic — OpenAI, Anthropic,
Gemini, Mistral, Cohere all use httpx under the hood) just called
``runtime.track(...)`` without bumping the counter.

Net effect: the dashboard's ``coverage_seen`` view was empty for the
majority of customers. Operators couldn't tell which LLM hosts an
agent was actually talking to.

Post-fix both sync and async httpx ``_emit`` bump the counter.
"""
import asyncio
from unittest.mock import MagicMock

import httpx
import pytest

from nullrun.instrumentation.auto import (
    NullRunAsyncTransport,
    NullRunSyncTransport,
)


def _make_response(body: bytes, host: str = "api.openai.com") -> httpx.Response:
    request = httpx.Request("POST", f"https://{host}/v1/chat/completions")
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        content=body,
        request=request,
    )


# A minimal OpenAI-completions response body with usage. The extractor
# for api.openai.com reads ``usage.{prompt_tokens, completion_tokens,
# total_tokens}``.
USAGE_BODY = (
    b'{"id":"chatcmpl-1","choices":[{"message":{"role":"assistant","content":"hi"}}],'
    b'"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}'
)


def test_sync_transport_bumps_coverage_seen():
    """A successful OpenAI call via the sync httpx transport must
    bump ``_coverage_seen[api.openai.com]`` to 1."""
    runtime = MagicMock()
    # Provide a real dict for _coverage_seen so the bump survives
    # the test assertion.
    runtime._coverage_seen = {}

    inner = MagicMock()
    inner.handle_request.return_value = _make_response(USAGE_BODY)

    transport = NullRunSyncTransport(inner=inner, runtime=runtime)
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    transport.handle_request(request)

    assert runtime._coverage_seen.get("api.openai.com") == 1, (
        f"coverage_seen[api.openai.com] should be 1 after one httpx "
        f"call; got {runtime._coverage_seen}"
    )


def test_sync_transport_bumps_for_anthropic():
    """Same bump applies to other supported hosts — the dashboard
    should see Anthropic traffic too, not just OpenAI."""
    runtime = MagicMock()
    runtime._coverage_seen = {}

    # Anthropic-style response body: usage.{input_tokens, output_tokens}.
    # See _anthropic_extractor in auto.py.
    anthropic_body = (
        b'{"id":"msg-1","content":[{"type":"text","text":"hi"}],'
        b'"usage":{"input_tokens":10,"output_tokens":4}}'
    )
    inner = MagicMock()
    inner.handle_request.return_value = _make_response(anthropic_body, host="api.anthropic.com")

    transport = NullRunSyncTransport(inner=inner, runtime=runtime)
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    transport.handle_request(request)

    assert runtime._coverage_seen.get("api.anthropic.com") == 1, (
        f"coverage_seen[api.anthropic.com] should be 1; got {runtime._coverage_seen}"
    )


def test_async_transport_bumps_coverage_seen():
    """Async mirror: a call via the async httpx transport also
    bumps the counter."""
    runtime = MagicMock()
    runtime._coverage_seen = {}

    async def fake_handle(_request):
        return _make_response(USAGE_BODY)

    inner = MagicMock()
    inner.handle_async_request.side_effect = fake_handle

    transport = NullRunAsyncTransport(inner=inner, runtime=runtime)
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    asyncio.run(transport.handle_async_request(request))

    assert runtime._coverage_seen.get("api.openai.com") == 1, (
        f"async coverage_seen[api.openai.com] should be 1; got {runtime._coverage_seen}"
    )


def test_sync_transport_bumps_incrementally_across_requests():
    """Multiple calls to the same host must accumulate, not overwrite
    (so the counter is a real frequency, not a 0/1 flag)."""
    runtime = MagicMock()
    runtime._coverage_seen = {}

    inner = MagicMock()
    inner.handle_request.return_value = _make_response(USAGE_BODY)

    transport = NullRunSyncTransport(inner=inner, runtime=runtime)
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")

    for _ in range(3):
        transport.handle_request(request)

    assert runtime._coverage_seen.get("api.openai.com") == 3, (
        f"3 calls should produce coverage_seen=3; got {runtime._coverage_seen}"
    )


def test_sync_transport_no_bump_when_extractor_misses():
    """If the extractor returns None (no usage block in the body),
    we don't call _emit, so the counter is NOT bumped. This is the
    right behaviour — we only want to count LLM calls we actually
    tracked, not every HTTP round-trip to an LLM host."""
    runtime = MagicMock()
    runtime._coverage_seen = {}

    body = b'{"id":"chatcmpl-1","choices":[]}'  # no usage block
    inner = MagicMock()
    inner.handle_request.return_value = _make_response(body)

    transport = NullRunSyncTransport(inner=inner, runtime=runtime)
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    transport.handle_request(request)

    assert runtime._coverage_seen == {}, (
        f"no usage → no bump; got {runtime._coverage_seen}"
    )