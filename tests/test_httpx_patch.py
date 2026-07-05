"""
Tests for the httpx transport hook in `nullrun.instrumentation.auto`.

Covers:
- A new httpx.Client created after `patch_httpx` automatically wraps
  its transport with `NullRunSyncTransport`.
- An OpenAI-shaped response triggers exactly one `runtime.track(...)`
  call with the right provider/tokens/model.
- A non-LLM host is passed through untouched (no track call).
- An OpenAI 4xx error response does NOT emit a track call (status gate).
- The reconstructed response body is identical to the original (so
  callers still get their data back).
- Idempotency: calling `patch_httpx` twice does not double-wrap.
- `reset_for_tests` lets the test suite re-patch in long-lived runs.
- A real-world gzip-encoded OpenAI response (which `httpx` decompresses
  during `response.read `) is rebuilt WITHOUT the `content-encoding`
  header — otherwise the downstream openai/anthropic client tries to
  decompress an already-decompressed body and raises `zlib.error: Error
  -3 while decompressing data: incorrect header check`. Regression
  test for the bug that broke Phase 3 of `policy_e2e_demo.py`.
"""

from __future__ import annotations

import gzip
import json
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from nullrun.instrumentation import auto as _auto_module
from nullrun.instrumentation.auto import (
    NullRunSyncTransport,
    patch_httpx,
    reset_for_tests,
)


@pytest.fixture(autouse=True)
def _clean_httpx_patch():
    """Each test starts with a clean patch state. Without this, repeated
    runs in the same process double-wrap."""
    reset_for_tests()
    yield
    reset_for_tests()


@pytest.fixture
def runtime():
    """A mock runtime that records `track` calls. We never want to talk
    to a real NullRun backend in unit tests."""
    rt = MagicMock()
    rt.track = MagicMock()
    return rt


def _openai_response_body() -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-1",
            "model": "gpt-4o-mini",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hi"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 8,
                "completion_tokens": 2,
                "total_tokens": 10,
            },
        }
    ).encode()


def test_patch_httpx_wraps_new_client_transport(runtime):
    assert patch_httpx(runtime) is True
    with httpx.Client() as client:
        assert isinstance(client._transport, NullRunSyncTransport)


def test_patch_httpx_is_idempotent(runtime):
    patch_httpx(runtime)
    inner_id = id(httpx.Client)
    patch_httpx(runtime)
    # The class-level marker survives a second call without re-wrapping
    assert getattr(httpx.Client, "_nullrun_patched", False) is True
    assert id(httpx.Client) == inner_id
    # And a fresh client is still wrapped exactly once
    with httpx.Client() as client:
        assert isinstance(client._transport, NullRunSyncTransport)
        transport = client._transport
        # The transport wraps a real DefaultTransport (or the test's
        # mock layer), NOT another NullRunSyncTransport.
        assert not isinstance(transport._inner, NullRunSyncTransport)


def test_openai_response_emits_track_call(runtime):
    patch_httpx(runtime)
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=httpx.Response(200, content=_openai_response_body())
        )
        with httpx.Client(base_url="https://api.openai.com") as client:
            response = client.post(
                "/v1/chat/completions",
                json={"model": "gpt-4o-mini", "messages": []},
            )
            assert response.status_code == 200
            # Body must round-trip identically
            assert response.content == _openai_response_body()
    # And runtime.track was called exactly once with a populated event.
    assert runtime.track.call_count == 1
    event = runtime.track.call_args[0][0]
    assert event["type"] == "llm_call"
    assert event["provider"] == "openai"
    assert event["host"] == "api.openai.com"
    assert event["model"] == "gpt-4o-mini"
    assert event["tokens"] == 10
    assert event["input_tokens"] == 8
    assert event["output_tokens"] == 2
    assert event["has_usage"] is True
    assert event["raw_usage"]["total_tokens"] == 10
    assert event["_fingerprint"]


def test_non_llm_host_passes_through_without_track(runtime):
    patch_httpx(runtime)
    with respx.mock(base_url="https://api.example.com") as mock:
        mock.post("/data").mock(return_value=httpx.Response(200, content=b'{"ok": true}'))
        with httpx.Client(base_url="https://api.example.com") as client:
            response = client.post("/data", json={"x": 1})
            assert response.status_code == 200
            assert response.json() == {"ok": True}
    assert runtime.track.call_count == 0


def test_openai_4xx_does_not_emit_track(runtime):
    patch_httpx(runtime)
    error_body = json.dumps({"error": {"message": "rate limit exceeded"}}).encode()
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(return_value=httpx.Response(429, content=error_body))
        with httpx.Client(base_url="https://api.openai.com") as client:
            response = client.post(
                "/v1/chat/completions",
                json={"model": "gpt-4o-mini", "messages": []},
            )
            assert response.status_code == 429
            assert response.content == error_body
    assert runtime.track.call_count == 0


def test_anthropic_response_emits_track(runtime):
    patch_httpx(runtime)
    body = json.dumps(
        {
            "id": "msg_01",
            "model": "claude-3-5-sonnet-20241022",
            "content": [{"type": "text", "text": "hello"}],
            "usage": {"input_tokens": 20, "output_tokens": 6},
        }
    ).encode()
    with respx.mock(base_url="https://api.anthropic.com") as mock:
        mock.post("/v1/messages").mock(return_value=httpx.Response(200, content=body))
        with httpx.Client(base_url="https://api.anthropic.com") as client:
            response = client.post(
                "/v1/messages",
                json={"model": "claude-3-5-sonnet-20241022", "max_tokens": 1024},
            )
            assert response.status_code == 200
    assert runtime.track.call_count == 1
    event = runtime.track.call_args[0][0]
    assert event["provider"] == "anthropic"
    assert event["model"] == "claude-3-5-sonnet-20241022"
    assert event["input_tokens"] == 20
    assert event["output_tokens"] == 6
    assert event["tokens"] == 26


def test_httpx_module_flag_set_after_patch(runtime):
    """`is_auto_instrumented` style introspection: the module-level flag
    flips so callers can detect the patch without poking at the class."""
    assert _auto_module._httpx_patched is False
    patch_httpx(runtime)
    assert _auto_module._httpx_patched is True
    # Reset clears it again — useful for long-lived test runners.
    reset_for_tests()
    assert _auto_module._httpx_patched is False


# ---------------------------------------------------------------------------
# Gzip-encoding regression: the transport consumes the body via
# `response.read `, which makes httpx transparently decompress gzip/br/zstd.
# The rebuilt response must NOT carry the original `content-encoding` header
# — otherwise the caller (e.g. openai/AsyncOpenAI) re-decompresses an
# already-decompressed body and raises `zlib.error: Error -3... incorrect
# header check`. Symptom: every LLM call after `nullrun.init ` raised
# `openai.APIConnectionError: Connection error` from inside the openai
# transport. Root cause was `NullRunSyncTransport._rebuild` passing the
# raw `response.headers` (which still include `content-encoding: gzip`)
# to a fresh `httpx.Response(content=body)` (where `body` is already plain).
# ---------------------------------------------------------------------------


def _gzip_openai_response_body() -> bytes:
    """A real-shape OpenAI chat-completions JSON body, gzipped on the wire."""
    plain = json.dumps(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": "gpt-4o-mini",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 2,
                "total_tokens": 7,
            },
        }
    ).encode("utf-8")
    return gzip.compress(plain)


def test_gzip_response_strips_content_encoding_header(runtime):
    """Real OpenAI traffic comes back `content-encoding: gzip`. The transport
    decompresses during `response.read `; the rebuilt response must drop
    the header so the downstream caller does not double-decompress."""
    patch_httpx(runtime)
    plain_body = _gzip_openai_response_body()
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                content=plain_body,
                headers={"content-encoding": "gzip"},
            )
        )
        with httpx.Client(base_url="https://api.openai.com") as client:
            response = client.post(
                "/v1/chat/completions",
                json={"model": "gpt-4o-mini", "messages": []},
            )
            assert response.status_code == 200
            # The transport ran an extractor on the decompressed body.
            assert runtime.track.call_count == 1
            event = runtime.track.call_args[0][0]
            assert event["input_tokens"] == 5
            assert event["output_tokens"] == 2
            assert event["tokens"] == 7
            # CRITICAL: the rebuilt response must NOT advertise
            # `content-encoding: gzip` — the body it carries is already
            # plain. Without this fix, downstream `response.json ` would
            # try to re-decompress and raise zlib.error.
            assert "content-encoding" not in {k.lower() for k in response.headers}
            # And the caller can read the body as JSON without errors.
            payload = response.json()
            assert payload["model"] == "gpt-4o-mini"
            assert payload["usage"]["total_tokens"] == 7


def test_gzip_response_with_extractor_skip_still_strips_encoding(runtime):
    """A non-LLM host whose body we still consume (no extractor match
    that path is a no-op, but the same decode-and-rebuild path is used
    for ANY known host that returns a non-empty body). Verify the header
    is stripped even when no `track` call fires — the bug was a header
    leak, not a missing track."""
    patch_httpx(runtime)
    # Use a host the extractor table does NOT match — extractor is None
    # so handle_request returns the inner response untouched. This test
    # only exercises the rebuild path through a known host with a body
    # the extractor returns None for (status gate). Skip if we can't
    # construct such a response: covered above by the openai 4xx case
    # which already asserts body round-trips. Here we just check the
    # async transport's rebuild strips encoding too.
    plain = json.dumps({"usage": {"prompt_tokens": 0, "completion_tokens": 0}}).encode()
    gz = gzip.compress(plain)
    inner = httpx.Response(200, content=gz, headers={"content-encoding": "gzip"})
    # Build a request matching the rebuild's expectation.
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    body = inner.read()  # httpx decompresses here
    rebuilt = NullRunSyncTransport._rebuild(inner, body, request)
    assert "content-encoding" not in {k.lower() for k in rebuilt.headers}
    assert rebuilt.json() == {"usage": {"prompt_tokens": 0, "completion_tokens": 0}}
