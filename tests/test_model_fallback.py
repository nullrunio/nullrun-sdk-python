"""
Regression test for Issue 2 (2026-06-28): SDK must propagate the real
model name through ``/api/v1/track/batch`` so the backend's
``MODEL_RATES`` lookup picks up the right per-token price instead of
falling back to ``DEFAULT_RATE`` (≈$0 per call).

Pre-fix: when the OpenAI Responses API or streaming final-chunk
returned without a top-level ``model`` field, the SDK's
``NullRunSyncTransport._emit`` sent the event with ``model=None``,
which the wire-format builder dropped, which the backend then
``unwrap_or("default")``'d and warned ``no canonical rate for model;
falling back to DEFAULT_RATE``.

Post-fix: ``_extract_model_from_request_body`` reads the ``model``
field the SDK user embedded in the request body (e.g.
``ChatOpenAI(model="gpt-4.1-mini")``) and uses it as a fallback when
the response extractor returns ``None`` for ``model``.

This test exercises the helper directly and asserts:
1. Plain JSON body with ``model`` → returns the model string.
2. Empty body → returns ``None``.
3. Malformed JSON → returns ``None`` (no raise).
4. JSON without ``model`` field → returns ``None``.
5. JSON with ``model: ""`` → returns ``None`` (empty string is falsy).

End-to-end coverage of the full ``_emit`` path with a mocked
``NullRunSyncTransport`` lives in ``test_httpx_patch.py`` — this
file is a focused unit test of the fallback helper.
"""

from __future__ import annotations

import json

import httpx

from nullrun.instrumentation.auto import _extract_model_from_request_body


def _request_with_body(body: bytes | None) -> httpx.Request:
    """Build an httpx.Request whose ``.content`` returns the given body."""
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    # httpx.Request stores the content as a property; assignment via
    # ``.read()`` requires content to be bytes. The simplest path is
    # to construct with content= via the constructor.
    return httpx.Request(
        "POST",
        "https://api.openai.com/v1/chat/completions",
        content=body if body is not None else b"",
    )


def test_extracts_model_from_standard_request_body():
    body = json.dumps(
        {
            "model": "gpt-4.1-mini",
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode()
    req = _request_with_body(body)
    assert _extract_model_from_request_body(req) == "gpt-4.1-mini"


def test_returns_none_for_empty_body():
    req = _request_with_body(b"")
    assert _extract_model_from_request_body(req) is None


def test_returns_none_for_malformed_json():
    req = _request_with_body(b"not-json{{{")
    assert _extract_model_from_request_body(req) is None


def test_returns_none_when_model_field_missing():
    body = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
    req = _request_with_body(body)
    assert _extract_model_from_request_body(req) is None


def test_returns_none_when_model_is_empty_string():
    body = json.dumps({"model": ""}).encode()
    req = _request_with_body(body)
    assert _extract_model_from_request_body(req) is None


def test_extracts_full_model_id_from_request_body():
    """Some SDK users pass the full versioned model id (e.g.
    gpt-4.1-mini-2025-04-14) directly. The helper must pass it through
    unmodified — the backend's MODEL_RATES substring lookup matches
    ``gpt-4.1-mini`` even when the model_id is longer.
    """
    body = json.dumps({"model": "gpt-4.1-mini-2025-04-14"}).encode()
    req = _request_with_body(body)
    assert _extract_model_from_request_body(req) == "gpt-4.1-mini-2025-04-14"


def test_extracts_claude_model_from_anthropic_request_body():
    body = json.dumps(
        {
            "model": "claude-sonnet-4-6",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1024,
        }
    ).encode()
    req = _request_with_body(body)
    assert _extract_model_from_request_body(req) == "claude-sonnet-4-6"
