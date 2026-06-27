"""Coverage padding for Phase 4.1 instrumentation additions.

The PR adds a finish_reason normaliser + cache / reasoning / tool-name
extraction in two places:

* ``nullrun.instrumentation.auto._normalize_finish_reason`` and the
  new branches in ``_openai_extractor`` / ``_anthropic_extractor`` /
  etc.
* ``nullrun.instrumentation.langgraph._safe_get_gen_message``,
  ``_get_finish_reason``, and the Phase 4.1 second-tier fields of
  ``extract_usage_from_response``.

The functions are pure (or near-pure) — feed them a representative
object, assert the canonical fields come out the other side. These
tests also serve as living documentation of the wire shapes we
support, which is why they pin both the happy path and the
best-effort fallbacks (cache_read_tokens / cache_write_tokens /
reasoning_tokens / finish_reason / tool_names).

Pinned by ``.codecov.yml::coverage.status.patch.target`` (70%, with
a 5pp threshold so ≥65% passes). Without these tests the patch
coverage lands around 62% and the GitHub Status check stays red.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nullrun.instrumentation.auto import (
    _anthropic_extractor,
    _normalize_finish_reason,
    _openai_extractor,
)
from nullrun.instrumentation.langgraph import (
    _get_finish_reason,
    _safe_get_gen_message,
    extract_usage_from_response,
)


# ---------------------------------------------------------------------------
# _normalize_finish_reason — pure mapping table
# ---------------------------------------------------------------------------
class TestNormalizeFinishReason:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # OpenAI / Mistral / Ollama — pass-throughs.
            ("stop", "stop"),
            ("length", "length"),
            ("tool_calls", "tool_calls"),
            # OpenAI content-filter block path.
            ("content_filter", "blocked"),
            # Legacy OpenAI "function_call" alias.
            ("function_call", "tool_calls"),
            # Anthropic.
            ("end_turn", "stop"),
            ("max_tokens", "length"),
            ("tool_use", "tool_calls"),
            ("stop_sequence", "stop"),
            # Gemini — uppercase forms that MUST be normalised.
            ("STOP", "stop"),
            ("MAX_TOKENS", "length"),
            ("SAFETY", "blocked"),
            ("RECITATION", "blocked"),
            ("FINISH_REASON_UNSPECIFIED", "unknown"),
            # Cohere.
            ("COMPLETE", "stop"),
            ("ERROR_TOXIC", "blocked"),
            ("ERROR", "blocked"),
        ],
    )
    def test_known_values_map_to_canonical(self, raw: str, expected: str) -> None:
        assert _normalize_finish_reason(raw) == expected

    def test_none_passes_through(self) -> None:
        # ``None`` input MUST stay ``None`` — the wire contract lets
        # the backend distinguish "no finish reason reported" from
        # "finish reason was the string 'unknown'".
        assert _normalize_finish_reason(None) is None

    def test_unknown_string_lowercased_not_dropped(self) -> None:
        # An unknown value MUST still land on the wire (lowercased)
        # rather than silently becoming None. A new provider we
        # haven't catalogued yet shouldn't erase the signal.
        assert _normalize_finish_reason("MY_NEW_PROVIDER_VALUE") == "my_new_provider_value"

    def test_empty_string_returns_none(self) -> None:
        # Defensive: empty string lowercased is still empty, so the
        # function falls back to None.
        assert _normalize_finish_reason("") is None


# ---------------------------------------------------------------------------
# _openai_extractor — Phase 4.1 second-tier fields
# ---------------------------------------------------------------------------
class TestOpenAIPhase41Fields:
    def test_cache_read_and_reasoning_tokens_extracted(self) -> None:
        # OpenAI's o-series responses nest cache + reasoning under
        # prompt_tokens_details / completion_tokens_details.
        body = json.dumps(
            {
                "model": "o3-mini",
                "choices": [{"finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                    "prompt_tokens_details": {"cached_tokens": 80},
                    "completion_tokens_details": {"reasoning_tokens": 30},
                },
            }
        ).encode()
        out = _openai_extractor(body, 200)
        assert out is not None
        assert out["cache_read_tokens"] == 80
        assert out["reasoning_tokens"] == 30
        # OpenAI doesn't expose cache creation tokens — the extractor
        # reports 0 rather than None so the backend schema stays
        # uniform across providers.
        assert out["cache_write_tokens"] == 0

    def test_finish_reason_normalised(self) -> None:
        # The Phase 4.1 extractor pulls ``finish_reason`` off the
        # first choice and routes it through the normaliser.
        body = json.dumps(
            {
                "model": "gpt-4o",
                "choices": [{"finish_reason": "tool_calls"}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        ).encode()
        out = _openai_extractor(body, 200)
        assert out is not None
        assert out["finish_reason"] == "tool_calls"

    def test_tool_names_collected_from_choices(self) -> None:
        # Tool-call names land in ``tool_names``; arguments are
        # deliberately NOT extracted (would leak user-supplied data).
        body = json.dumps(
            {
                "model": "gpt-4o",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {"function": {"name": "get_weather"}},
                                {"function": {"name": "send_email"}},
                            ]
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        ).encode()
        out = _openai_extractor(body, 200)
        assert out is not None
        assert out["tool_names"] == ["get_weather", "send_email"]


# ---------------------------------------------------------------------------
# _anthropic_extractor — Phase 4.1 cache_read + cache_write
# ---------------------------------------------------------------------------
class TestAnthropicPhase41Fields:
    def test_cache_read_and_write_tokens(self) -> None:
        # Anthropic exposes BOTH cache_read_input_tokens and
        # cache_creation_input_tokens — the SDK surfaces both.
        body = json.dumps(
            {
                "model": "claude-3-5-sonnet-20241022",
                "content": [{"type": "text", "text": "hi"}],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 80,
                    "cache_creation_input_tokens": 20,
                },
            }
        ).encode()
        out = _anthropic_extractor(body, 200)
        assert out is not None
        assert out["cache_read_tokens"] == 80
        assert out["cache_write_tokens"] == 20


# ---------------------------------------------------------------------------
# _safe_get_gen_message — defensive LLMResult walker
# ---------------------------------------------------------------------------
class TestSafeGetGenMessage:
    def test_returns_none_when_generations_missing(self) -> None:
        # No ``generations`` attr at all — the helper MUST swallow
        # the AttributeError so the caller can fall through.
        assert _safe_get_gen_message(object()) is None

    def test_returns_none_when_generations_empty(self) -> None:
        assert _safe_get_gen_message(SimpleNamespace(generations=[])) is None

    def test_returns_none_when_first_gen_empty(self) -> None:
        # Outer list present but empty — same fallback.
        assert _safe_get_gen_message(SimpleNamespace(generations=[[]])) is None

    def test_returns_message_when_present(self) -> None:
        msg = SimpleNamespace(content="hello")
        gen = SimpleNamespace(message=msg)
        response = SimpleNamespace(generations=[[gen]])
        assert _safe_get_gen_message(response) is msg

    def test_returns_none_when_message_attr_missing(self) -> None:
        # Generation present but ``.message`` is None — still a hit,
        # just nothing to return.
        response = SimpleNamespace(generations=[[SimpleNamespace(message=None)]])
        assert _safe_get_gen_message(response) is None


# ---------------------------------------------------------------------------
# _get_finish_reason — five-source fallback chain
# ---------------------------------------------------------------------------
class TestGetFinishReason:
    def test_direct_attribute_wins(self) -> None:
        # The direct top-level ``finish_reason`` is the highest
        # priority source.
        response = SimpleNamespace(
            finish_reason="tool_calls",
            response_metadata={"finish_reason": "stop"},  # would lose
        )
        assert _get_finish_reason(response) == "tool_calls"

    def test_response_metadata_fallback(self) -> None:
        # When the wrapper puts the field in response_metadata
        # (OpenAI-via-LangChain path), we still surface it.
        response = SimpleNamespace(response_metadata={"finish_reason": "stop"})
        assert _get_finish_reason(response) == "stop"

    def test_anthropic_stop_reason_alias(self) -> None:
        # Anthropic uses ``stop_reason`` rather than ``finish_reason``.
        response = SimpleNamespace(stop_reason="end_turn")
        assert _get_finish_reason(response) == "end_turn"

    def test_llmresult_callback_path(self) -> None:
        # Callback path: the field lives on the AIMessage inside
        # generations[0][0].message, not on the LLMResult wrapper.
        msg = SimpleNamespace(finish_reason="length")
        gen = SimpleNamespace(message=msg)
        response = SimpleNamespace(generations=[[gen]])
        assert _get_finish_reason(response) == "length"

    def test_llm_output_legacy_path(self) -> None:
        # Legacy LLMResult where finish info sits on llm_output.
        response = SimpleNamespace(llm_output={"finish_reason": "stop"})
        assert _get_finish_reason(response) == "stop"

    def test_returns_none_when_no_source_has_value(self) -> None:
        # All sources present, none populated — explicit None.
        response = SimpleNamespace(
            finish_reason=None,
            stop_reason=None,
            response_metadata={},
            generations=[],
            llm_output={},
        )
        assert _get_finish_reason(response) is None


# ---------------------------------------------------------------------------
# extract_usage_from_response — Phase 4.1 second-tier fields
# ---------------------------------------------------------------------------
class TestExtractUsagePhase41:
    def test_cache_read_tokens_from_anthropic(self) -> None:
        # Anthropic exposes cache_read_input_tokens directly on the
        # usage block; the SDK mirrors it as cache_read_tokens.
        response = SimpleNamespace(
            usage={"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 80}
        )
        out = extract_usage_from_response(response, provider="anthropic", model="claude-3-5-sonnet")
        assert out["cache_read_tokens"] == 80

    def test_cache_write_tokens_from_anthropic(self) -> None:
        response = SimpleNamespace(
            usage={
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 20,
            }
        )
        out = extract_usage_from_response(response, provider="anthropic", model="claude-3-5-sonnet")
        assert out["cache_write_tokens"] == 20

    def test_cache_read_tokens_from_openai_prompt_details(self) -> None:
        # OpenAI nests cached_tokens under prompt_tokens_details;
        # the extractor must reach in there too.
        response = SimpleNamespace(
            usage={
                "input_tokens": 100,
                "output_tokens": 50,
                "prompt_tokens_details": {"cached_tokens": 90},
            }
        )
        out = extract_usage_from_response(response, provider="openai", model="gpt-4o")
        assert out["cache_read_tokens"] == 90

    def test_reasoning_tokens_from_completion_details(self) -> None:
        response = SimpleNamespace(
            usage={
                "input_tokens": 100,
                "output_tokens": 50,
                "completion_tokens_details": {"reasoning_tokens": 30},
            }
        )
        out = extract_usage_from_response(response, provider="openai", model="o3-mini")
        assert out["reasoning_tokens"] == 30

    def test_tool_names_collected_from_message(self) -> None:
        # When the response is an AIMessage (not an LLMResult),
        # tool_calls live on ``response.tool_calls`` directly.
        response = SimpleNamespace(
            usage={"input_tokens": 1, "output_tokens": 1},
            tool_calls=[{"function": {"name": "get_weather"}}],
        )
        out = extract_usage_from_response(response, provider="openai", model="gpt-4o")
        assert "get_weather" in out["tool_names"]

    def test_default_values_when_no_usage(self) -> None:
        # A response with no usage at all still returns a populated
        # dict with the default zeros / None / [] — never a partial
        # dict that crashes the backend ingest path.
        out = extract_usage_from_response(object(), provider="openai", model="gpt-4o")
        assert out["cache_read_tokens"] == 0
        assert out["cache_write_tokens"] == 0
        assert out["reasoning_tokens"] == 0
        assert out["finish_reason"] is None
        assert out["tool_names"] == []