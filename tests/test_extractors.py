"""
Unit tests for the URL-keyed extractor table in
`nullrun.instrumentation.auto`.

Each extractor is fed a canonical response body for its vendor and
asserts the right `(prompt_tokens, completion_tokens, total_tokens
model)` come back. We also cover:

- error responses (`status >= 400`) -> None
- empty body -> None
- malformed JSON -> None
- non-usage payloads (e.g. error body) -> None
- subdomain matching (`eu.api.openai.com` -> OpenAI extractor)
- v1.0 streaming final-chunk pattern (OpenAI sends `usage` only in
  the last SSE chunk; we extract from the accumulated body)
"""

from __future__ import annotations

import json

from nullrun.instrumentation.auto import (
    PROVIDER_EXTRACTORS,
    _anthropic_extractor,
    _bedrock_extractor,
    _cohere_extractor,
    _gemini_extractor,
    _match_extractor,
    _openai_extractor,
)

# ---------------------------------------------------------------------------
# OpenAI / Azure OpenAI / Mistral
# ---------------------------------------------------------------------------


def test_openai_canonical_response():
    body = json.dumps(
        {
            "id": "chatcmpl-abc",
            "model": "gpt-4o-mini",
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 7,
                "total_tokens": 19,
            },
        }
    ).encode()
    out = _openai_extractor(body, 200)
    assert out is not None
    assert out["prompt_tokens"] == 12
    assert out["completion_tokens"] == 7
    assert out["total_tokens"] == 19
    assert out["model"] == "gpt-4o-mini"


def test_openai_synthesizes_total_from_prompt_completion():
    """If `total_tokens` is missing, the extractor sums prompt+completion."""
    body = json.dumps(
        {
            "model": "gpt-4",
            "usage": {"prompt_tokens": 4, "completion_tokens": 6},
        }
    ).encode()
    out = _openai_extractor(body, 200)
    assert out is not None
    assert out["total_tokens"] == 10


def test_openai_zero_usage_returns_none():
    """A response without usage info must not be tracked as an LLM call."""
    body = json.dumps({"id": "x", "choices": []}).encode()
    assert _openai_extractor(body, 200) is None


def test_openai_error_response_returns_none():
    body = json.dumps({"error": {"message": "rate limit"}}).encode()
    assert _openai_extractor(body, 429) is None


def test_openai_empty_body_returns_none():
    assert _openai_extractor(b"", 200) is None


def test_openai_malformed_json_returns_none():
    assert _openai_extractor(b"not-json", 200) is None


def test_openai_v1_streaming_final_chunk():
    """OpenAI v1.0+ streaming responses only carry `usage` in the LAST SSE
    chunk. We feed the full accumulated buffer (multiple SSE chunks
    concatenated) and assert the extractor still pulls out the usage
    object. The body shape we test is the JSON dict inside the final
    `data: {...}` line — a real call site would accumulate until
    `[DONE]` and pass the parsed payload of the last chunk. The
    extractor itself is JSON-shape based, not SSE-frame based."""
    body = json.dumps(
        {
            "model": "gpt-4o",
            "choices": [{"finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
            },
        }
    ).encode()
    out = _openai_extractor(body, 200)
    assert out is not None
    assert out["total_tokens"] == 150


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def test_anthropic_canonical_response():
    body = json.dumps(
        {
            "id": "msg_01",
            "model": "claude-3-5-sonnet-20241022",
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 25, "output_tokens": 9},
        }
    ).encode()
    out = _anthropic_extractor(body, 200)
    assert out is not None
    assert out["prompt_tokens"] == 25
    assert out["completion_tokens"] == 9
    assert out["total_tokens"] == 34
    assert out["model"] == "claude-3-5-sonnet-20241022"


def test_anthropic_zero_usage_returns_none():
    body = json.dumps({"content": []}).encode()
    assert _anthropic_extractor(body, 200) is None


def test_anthropic_error_returns_none():
    body = json.dumps({"type": "error", "error": {"type": "rate_limit"}}).encode()
    assert _anthropic_extractor(body, 429) is None


# ---------------------------------------------------------------------------
# Google Gemini (Generative Language API)
# ---------------------------------------------------------------------------


def test_gemini_canonical_response():
    body = json.dumps(
        {
            "modelVersion": "gemini-1.5-pro",
            "candidates": [{"content": {"parts": [{"text": "hi"}]}}],
            "usageMetadata": {
                "promptTokenCount": 30,
                "candidatesTokenCount": 15,
                "totalTokenCount": 45,
            },
        }
    ).encode()
    out = _gemini_extractor(body, 200)
    assert out is not None
    assert out["prompt_tokens"] == 30
    assert out["completion_tokens"] == 15
    assert out["total_tokens"] == 45
    assert out["model"] == "gemini-1.5-pro"


def test_gemini_synthesizes_total():
    body = json.dumps(
        {
            "modelVersion": "gemini-1.5-flash",
            "usageMetadata": {
                "promptTokenCount": 7,
                "candidatesTokenCount": 3,
            },
        }
    ).encode()
    out = _gemini_extractor(body, 200)
    assert out is not None
    assert out["total_tokens"] == 10


def test_gemini_no_usage_returns_none():
    body = json.dumps({"candidates": []}).encode()
    assert _gemini_extractor(body, 200) is None


# ---------------------------------------------------------------------------
# Cohere
# ---------------------------------------------------------------------------


def test_cohere_v2_response():
    body = json.dumps(
        {
            "model": "command-r-plus",
            "usage": {
                "input_tokens": 18,
                "output_tokens": 4,
                "tokens": 22,
            },
        }
    ).encode()
    out = _cohere_extractor(body, 200)
    assert out is not None
    assert out["prompt_tokens"] == 18
    assert out["completion_tokens"] == 4
    assert out["total_tokens"] == 22
    assert out["model"] == "command-r-plus"


def test_cohere_v1_legacy_prompt_completion_keys():
    """Cohere v1 used prompt_tokens/completion_tokens — make sure we
    still recognize that shape (v2 also accepted as a fallback)."""
    body = json.dumps(
        {
            "model": "command",
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }
    ).encode()
    out = _cohere_extractor(body, 200)
    assert out is not None
    assert out["prompt_tokens"] == 5
    assert out["completion_tokens"] == 2
    assert out["total_tokens"] == 7


# ---------------------------------------------------------------------------
# AWS Bedrock
# ---------------------------------------------------------------------------


def test_bedrock_anthropic_on_bedrock_nested_usage():
    body = json.dumps(
        {
            "modelId": "anthropic.claude-3-sonnet-20240229-v1:0",
            "usage": {"inputTokens": 11, "outputTokens": 5},
        }
    ).encode()
    out = _bedrock_extractor(body, 200)
    assert out is not None
    assert out["prompt_tokens"] == 11
    assert out["completion_tokens"] == 5
    assert out["total_tokens"] == 16
    assert out["model"] == "anthropic.claude-3-sonnet-20240229-v1:0"


def test_bedrock_top_level_input_output_tokens():
    """Some Bedrock adapters put inputTokens/outputTokens at the top level
    rather than under `usage`. We handle both shapes."""
    body = json.dumps(
        {
            "modelId": "mistral.mistral-7b-instruct-v0:2",
            "inputTokens": 8,
            "outputTokens": 4,
        }
    ).encode()
    out = _bedrock_extractor(body, 200)
    assert out is not None
    assert out["prompt_tokens"] == 8
    assert out["completion_tokens"] == 4
    assert out["total_tokens"] == 12


def test_bedrock_error_returns_none():
    body = json.dumps({"message": "AccessDeniedException"}).encode()
    assert _bedrock_extractor(body, 403) is None


# ---------------------------------------------------------------------------
# _match_extractor table
# ---------------------------------------------------------------------------


def test_match_extractor_known_hosts():
    assert _match_extractor("api.openai.com") is _openai_extractor
    assert _match_extractor("openai.azure.com") is _openai_extractor
    assert _match_extractor("api.mistral.ai") is _openai_extractor
    assert _match_extractor("api.anthropic.com") is _anthropic_extractor
    assert _match_extractor("generativelanguage.googleapis.com") is _gemini_extractor
    assert _match_extractor("api.cohere.ai") is _cohere_extractor
    assert _match_extractor("bedrock-runtime.amazonaws.com") is _bedrock_extractor


def test_match_extractor_subdomain_match():
    """A regional OpenAI endpoint like `eu.api.openai.com` should still
    route to the OpenAI extractor."""
    assert _match_extractor("eu.api.openai.com") is _openai_extractor
    assert _match_extractor("us-west-2.api.openai.com") is _openai_extractor


def test_match_extractor_unknown_host_returns_none():
    assert _match_extractor("api.example.com") is None
    assert _match_extractor("") is None
    # `similar-but-wrong` suffix is not enough
    assert _match_extractor("notapi.openai.com.evil.test") is None


def test_provider_table_covers_seven_hosts():
    """Sanity check on the table contents — fail loudly if someone
    removes a provider without updating the dedup story."""
    assert len(PROVIDER_EXTRACTORS) == 7
    assert set(PROVIDER_EXTRACTORS.keys()) == {
        "api.openai.com",
        "openai.azure.com",
        "api.mistral.ai",
        "api.anthropic.com",
        "generativelanguage.googleapis.com",
        "api.cohere.ai",
        "bedrock-runtime.amazonaws.com",
    }


# ---------------------------------------------------------------------------
# Phase 4.1: new fields (cache / reasoning / finish / tool_names) and
# the privacy boundary that strips them at the wire.
# ---------------------------------------------------------------------------


def test_openai_no_tool_calls_returns_empty_list():
    """A response without tool_calls must not break the extractor — we
    get an empty list and a normalized finish_reason. Pre-Phase-4.1
    this would have KeyError'd on `tool_calls` because the loop
    iterated over None."""
    body = json.dumps(
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "hello"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 3,
                "total_tokens": 8,
            },
            "model": "gpt-4o",
        }
    ).encode()
    out = _openai_extractor(body, 200)
    assert out is not None
    assert out["tool_names"] == []
    assert out["finish_reason"] == "stop"


def test_openai_caches_and_reasoning_tokens():
    """OpenAI 2024+ exposes prompt cache hits and o-series reasoning
    tokens in nested detail blocks. Make sure both surface as
    first-class fields, not buried in raw_usage."""
    body = json.dumps(
        {
            "model": "o1-mini",
            "choices": [{"finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 1200,
                "completion_tokens": 340,
                "total_tokens": 1540,
                "prompt_tokens_details": {"cached_tokens": 800},
                "completion_tokens_details": {"reasoning_tokens": 200},
            },
        }
    ).encode()
    out = _openai_extractor(body, 200)
    assert out["cache_read_tokens"] == 800
    assert out["cache_write_tokens"] == 0
    assert out["reasoning_tokens"] == 200


def test_anthropic_tool_names_and_finish_normalization():
    """Anthropic stop_reason uses 'end_turn' / 'tool_use' — these must
    normalize to 'stop' / 'tool_calls' so the backend sees a single
    canonical vocabulary."""
    body = json.dumps(
        {
            "model": "claude-sonnet-4-6",
            "stop_reason": "tool_use",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "tool_use", "name": "search_web"},
            ],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
    ).encode()
    out = _anthropic_extractor(body, 200)
    assert out["tool_names"] == ["search_web"]
    assert out["finish_reason"] == "tool_calls"


def test_bedrock_llama_tool_use_shape():
    """Llama-3-on-Bedrock exposes tool_use blocks nested under
    output.message.content, not under top-level content. Make sure
    that third shape is recognized."""
    body = json.dumps(
        {
            "modelId": "meta.llama3-70b-instruct-v1:0",
            "stop_reason": "stop",
            "output": {
                "message": {
                    "content": [
                        {"type": "text", "text": "thinking"},
                        {"type": "tool_use", "name": "lookup_weather"},
                    ]
                }
            },
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }
    ).encode()
    out = _bedrock_extractor(body, 200)
    assert out["tool_names"] == ["lookup_weather"]
    assert out["finish_reason"] == "stop"


def test_normalize_finish_reason_passthrough():
    """Unknown strings must NOT be dropped — they pass through lowercased
    so the backend still records them (e.g. a brand-new provider we
    haven't seen yet)."""
    from nullrun.instrumentation.auto import _normalize_finish_reason

    assert _normalize_finish_reason(None) is None
    assert _normalize_finish_reason("stop") == "stop"
    assert _normalize_finish_reason("end_turn") == "stop"
    assert _normalize_finish_reason("STOP") == "stop"
    assert _normalize_finish_reason("max_tokens") == "length"
    assert _normalize_finish_reason("MAX_TOKENS") == "length"
    assert _normalize_finish_reason("SAFETY") == "blocked"
    assert _normalize_finish_reason("SOME_NEW_REASON") == "some_new_reason"
    # Empty string must not crash either — lowercased empty string
    # becomes falsy and the helper returns None.
    assert _normalize_finish_reason("") is None
