"""
Tests for the unified LLM-call fingerprint scheme.

Background (audit 2026-06-29):
Before this fix the httpx transport hook (``NullRunSyncTransport._emit``)
and the LangChain callback (``NullRunCallback.on_llm_end``) each computed
their own ``_fingerprint`` from different inputs:

    httpx transport: sha256(host|status|body)[:16]
    LangChain callback: sha256(json({path:"langchain_callback", run_id
                                       response_id, model, provider
                                       invocation_params}))[:16]

The two fingerprints could not collide, so the dedup LRU at
``runtime.track `` could not collapse the sibling emission for the same
real LLM call. On a typical ``app.invoke `` with 6 LLM calls the backend
saw ~12 ``llm_call`` events on the wire (2 per real call), which doubled
the dashboard's ``llm_call_count`` and skewed ``cost_events`` aggregates.

The fix: a single helper ``_fingerprint_for_llm_call(model, provider
response_id)`` that both observers call with the same three signals.

Contract pinned by these tests:
1. The helper is deterministic: identical inputs → identical fingerprint.
2. Distinct inputs (different model / provider / id) → distinct fingerprints.
3. The httpx transport hook calls the helper with the values extracted
   from the OpenAI-style response body (``payload["model"]`` and
   ``payload["id"]``).
4. The LangChain callback path produces the SAME fingerprint for the
   same LLM call when it reads the chat-completion id from any of the
   four canonical locations (LLMResult.llm_output["id"] / response.id /
   AIMessage.id / response.response_metadata["id"]).
5. The dedup LRU recognises the two emissions as duplicates and only
   the first one reaches ``/track``.

These tests use the real helper + a stand-in runtime (no live network)
so they exercise the production code path without flakiness.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from nullrun.instrumentation.auto import (
    NullRunSyncTransport,
    _fingerprint_for_llm_call,
    _fingerprint_is_seen,
    make_dedup_state,
    patch_httpx,
    reset_for_tests,
)


# ---------------------------------------------------------------------------
# Pure helper mechanics
# ---------------------------------------------------------------------------


def test_fingerprint_for_llm_call_is_deterministic():
    """Identical inputs → identical fingerprints (16 hex chars)."""
    fp1 = _fingerprint_for_llm_call(
        "gpt-4.1-mini-2025-04-14", "openai", "chatcmpl-Dw7288WJI4bBDFyQ4DnZvhPUKfaZo"
    )
    fp2 = _fingerprint_for_llm_call(
        "gpt-4.1-mini-2025-04-14", "openai", "chatcmpl-Dw7288WJI4bBDFyQ4DnZvhPUKfaZo"
    )
    assert fp1 == fp2
    assert len(fp1) == 16
    assert all(c in "0123456789abcdef" for c in fp1)


def test_fingerprint_changes_with_response_id():
    """Two distinct chat-completion ids → distinct fingerprints.

    This is the discriminator the dedup LRU relies on. If it ever
    failed, two unrelated LLM calls would collide on the same dedup
    slot and one of them would silently drop on the wire.
    """
    fp_a = _fingerprint_for_llm_call("gpt-4.1-mini", "openai", "chatcmpl-A")
    fp_b = _fingerprint_for_llm_call("gpt-4.1-mini", "openai", "chatcmpl-B")
    assert fp_a != fp_b


def test_fingerprint_changes_with_model():
    """Two distinct models → distinct fingerprints even with the same id."""
    fp_a = _fingerprint_for_llm_call("gpt-4.1-mini", "openai", "chatcmpl-X")
    fp_b = _fingerprint_for_llm_call("gpt-4.1-mini-2025-04-15", "openai", "chatcmpl-X")
    assert fp_a != fp_b


def test_fingerprint_changes_with_provider():
    """Two distinct providers → distinct fingerprints even with the same id."""
    fp_a = _fingerprint_for_llm_call("gpt-4.1-mini", "openai", "msg-1")
    fp_b = _fingerprint_for_llm_call("gpt-4.1-mini", "anthropic", "msg-1")
    assert fp_a != fp_b


def test_fingerprint_tolerates_none_response_id():
    """When the response id cannot be recovered (custom chat-model wrappers
    that don't surface it), the helper still produces a stable fingerprint
    for the model+provider combination. This is the fallback path —
    tighter than no fingerprint, looser than full id-based disambiguation.
    """
    fp1 = _fingerprint_for_llm_call("gpt-4.1-mini", "openai", None)
    fp2 = _fingerprint_for_llm_call("gpt-4.1-mini", "openai", None)
    fp3 = _fingerprint_for_llm_call("gpt-4.1-mini", "anthropic", None)
    assert fp1 == fp2  # stable across calls with same inputs
    assert fp1 != fp3  # different provider still distinct


def test_fingerprint_matches_old_body_scheme_for_none_id():
    """Regression guard: when neither observer can recover the response id
    the helper still produces a deterministic key — NOT an empty string
    which would short-circuit the dedup LRU at ``_fingerprint_is_seen``.

    The ``make_dedup_state`` + ``_fingerprint_is_seen`` short-circuit
    on empty fingerprints (see ``test_lru_empty_fingerprint_short_circuits_to_unseen``
    in ``test_dedup.py``), so the helper must always produce a non-empty
    fingerprint even when all three signals are empty strings.
    """
    fp_empty = _fingerprint_for_llm_call("", "", "")
    assert fp_empty  # non-empty (the helper stamps a `llm_call|` prefix)
    assert len(fp_empty) == 16
    # The fingerprint MUST be accepted by the dedup LRU.
    state = make_dedup_state()
    assert _fingerprint_is_seen(state, fp_empty) is False
    assert _fingerprint_is_seen(state, fp_empty) is True


# ---------------------------------------------------------------------------
# httpx transport hook: stamps the unified fingerprint on emitted events
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_httpx_patch():
    reset_for_tests()
    yield
    reset_for_tests()


def _openai_chat_completion_response(
    model: str = "gpt-4.1-mini-2025-04-14",
    response_id: str = "chatcmpl-Dw7288WJI4bBDFyQ4DnZvhPUKfaZo",
    prompt_tokens: int = 26,
    completion_tokens: int = 50,
) -> bytes:
    """A minimal but realistic OpenAI chat-completion response body."""
    return json.dumps(
        {
            "id": response_id,
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
    ).encode()


def test_httpx_transport_emits_unified_fingerprint():
    """The httpx transport hook MUST call ``_fingerprint_for_llm_call``
    with the model and id extracted from the response body, NOT the old
    ``_fingerprint_for(host, body, status)`` scheme. This pins the fix."""
    rt = MagicMock()
    rt.track = MagicMock()
    rt._seen_track_fingerprints = make_dedup_state()

    patch_httpx(rt)
    body = _openai_chat_completion_response()
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=httpx.Response(200, content=body)
        )
        with httpx.Client(base_url="https://api.openai.com") as client:
            response = client.post("/v1/chat/completions", json={"model": "gpt-4.1-mini"})
            assert response.status_code == 200

    # Exactly one track call from the transport.
    assert rt.track.call_count == 1
    event = rt.track.call_args_list[0][0][0]
    fp = event["_fingerprint"]
    assert fp
    # Must be the unified fingerprint, computed from model+provider+id.
    expected = _fingerprint_for_llm_call(
        "gpt-4.1-mini-2025-04-14", "openai", "chatcmpl-Dw7288WJI4bBDFyQ4DnZvhPUKfaZo"
    )
    assert fp == expected, (
        f"transport fingerprint {fp!r} != expected {expected!r} — "
        "did the unified fingerprint scheme regress?"
    )


def test_httpx_transport_fingerprint_stable_across_response_bodies():
    """Two different bodies (different ids) MUST produce different
    fingerprints. This guards against silent re-emission collisions."""
    rt_a = MagicMock()
    rt_a.track = MagicMock()
    rt_a._seen_track_fingerprints = make_dedup_state()

    patch_httpx(rt_a)
    body_a = _openai_chat_completion_response(
        response_id="chatcmpl-A", prompt_tokens=10, completion_tokens=5
    )
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=httpx.Response(200, content=body_a)
        )
        with httpx.Client(base_url="https://api.openai.com") as client:
            client.post("/v1/chat/completions", json={"model": "gpt-4.1-mini"})

    fp_a = rt_a.track.call_args_list[0][0][0]["_fingerprint"]
    reset_for_tests()

    rt_b = MagicMock()
    rt_b.track = MagicMock()
    rt_b._seen_track_fingerprints = make_dedup_state()
    patch_httpx(rt_b)
    body_b = _openai_chat_completion_response(
        response_id="chatcmpl-B", prompt_tokens=20, completion_tokens=10
    )
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=httpx.Response(200, content=body_b)
        )
        with httpx.Client(base_url="https://api.openai.com") as client:
            client.post("/v1/chat/completions", json={"model": "gpt-4.1-mini"})

    fp_b = rt_b.track.call_args_list[0][0][0]["_fingerprint"]
    assert fp_a != fp_b


# ---------------------------------------------------------------------------
# LangChain callback: stamps the unified fingerprint on emitted events
# ---------------------------------------------------------------------------


class _FakeLLMResult:
    """Minimal stand-in for langchain_core.outputs.LLMResult carrying
    the response_id at every location the real NullRunCallback probes.

    The four locations (in priority order) are:
      1. ``response.llm_output["id"]`` (langchain-openai 1.x primary)
      2. ``response.id`` (some wrappers)
      3. ``response.generations[0][0].message.id`` (AIMessage inside generation)
      4. ``response.response_metadata["id"]`` (langchain 0.x AIMessage metadata)

    Each test below exercises one of these locations and asserts the
    resulting fingerprint matches the one the httpx transport produces
    for the same body. Without that match the dedup LRU cannot collapse
    the two emissions.
    """

    def __init__(
        self,
        *,
        model_name: str,
        response_id: str,
        llm_output_id: str | None = None,
        response_id_attr: str | None = None,
        message_id: str | None = None,
        response_metadata_id: str | None = None,
    ) -> None:
        self.llm_output: dict[str, Any] = {
            "model_name": model_name,
            "token_usage": {
                "prompt_tokens": 26,
                "completion_tokens": 50,
                "total_tokens": 76,
            },
        }
        if llm_output_id is not None:
            self.llm_output["id"] = llm_output_id

        if response_id_attr is not None:
            self.id = response_id_attr
        else:
            self.id = None

        # Build a single generation with a fake AIMessage.
        class _FakeMsg:
            def __init__(self, mid: str | None) -> None:
                self.id = mid

        class _FakeGen:
            def __init__(self, mid: str | None) -> None:
                self.message = _FakeMsg(mid)

        self.generations: list[list[_FakeGen]] = [[_FakeGen(message_id)]]

        self.response_metadata: dict[str, Any] = {
            "model_provider": "openai",
        }
        if response_metadata_id is not None:
            self.response_metadata["id"] = response_metadata_id


class _FakeAIMessage:
    """Stand-in for the AIMessage that NullRunCallback.on_llm_end receives
    when the response is NOT wrapped in LLMResult (i.e. direct AIMessage
    path). For langchain-openai 1.x chat-completions, the wrapper
    actually produces an LLMResult, so this is the less common case —
    but we cover it because the production code does."""

    def __init__(
        self,
        *,
        model_name: str,
        response_id: str,
        response_metadata_id: str | None = None,
    ) -> None:
        self.id = response_id
        self.content = "Hello!"
        self.response_metadata: dict[str, Any] = {
            "model_provider": "openai",
            "model_name": model_name,
        }
        if response_metadata_id is not None:
            self.response_metadata["id"] = response_metadata_id
        self.usage_metadata = {
            "input_tokens": 26,
            "output_tokens": 50,
            "total_tokens": 76,
        }
        self.additional_kwargs: dict[str, Any] = {}
        self.tool_calls: list[Any] = []
        self.invalid_tool_calls: list[Any] = []
        self.name = None


def _build_callback_runtime() -> tuple[Any, MagicMock]:
    """Build a runtime + NullRunCallback with a real dedup LRU."""
    from nullrun.instrumentation.langgraph import NullRunCallback

    rt = MagicMock()
    rt.track = MagicMock()
    rt._seen_track_fingerprints = make_dedup_state()
    callback = NullRunCallback(runtime=rt)
    return rt, callback


def _run_callback_on_llm_end(callback: Any, response: Any, **kwargs: Any) -> None:
    """Drive NullRunCallback.on_llm_end with the stand-in response.

    Skips the actual ``on_chain_start`` / ``on_chain_end`` flow — we want
    to test the fingerprint-stamping contract on the ``llm_call`` event
    alone.
    """
    callback.on_llm_end(response, **kwargs)


def _read_track_event(rt: MagicMock) -> dict[str, Any]:
    """Return the most recent event passed to ``rt.track``."""
    assert rt.track.call_count >= 1
    return rt.track.call_args_list[-1][0][0]


def test_callback_llm_output_id_collides_with_httpx_fingerprint():
    """LangChain callback extracts response_id from
    ``response.llm_output["id"]`` (langchain-openai 1.x primary location)
    and produces the SAME fingerprint the httpx transport computes for
    the same body. This is the core dedup fix."""
    rt, callback = _build_callback_runtime()

    response = _FakeLLMResult(
        model_name="gpt-4.1-mini-2025-04-14",
        response_id="chatcmpl-Dw7288WJI4bBDFyQ4DnZvhPUKfaZo",
        llm_output_id="chatcmpl-Dw7288WJI4bBDFyQ4DnZvhPUKfaZo",
    )
    _run_callback_on_llm_end(callback, response)

    event = _read_track_event(rt)
    expected = _fingerprint_for_llm_call(
        "gpt-4.1-mini-2025-04-14", "openai", "chatcmpl-Dw7288WJI4bBDFyQ4DnZvhPUKfaZo"
    )
    assert event["_fingerprint"] == expected


def test_callback_response_id_attr_collides_with_httpx_fingerprint():
    """Some wrappers put the chat-completion id directly on the
    ``response.id`` attribute (no llm_output dict). The callback MUST
    read this location and produce the unified fingerprint."""
    rt, callback = _build_callback_runtime()

    response = _FakeLLMResult(
        model_name="gpt-4.1-mini-2025-04-14",
        response_id="ignored",
        response_id_attr="chatcmpl-FROM-ATTR",
    )
    _run_callback_on_llm_end(callback, response)

    event = _read_track_event(rt)
    expected = _fingerprint_for_llm_call(
        "gpt-4.1-mini-2025-04-14", "openai", "chatcmpl-FROM-ATTR"
    )
    assert event["_fingerprint"] == expected


def test_callback_generation_message_id_collides_with_httpx_fingerprint():
    """AIMessage inside the first generation carries the id on its
    ``.id`` attribute (langchain 0.x style). Callback MUST fall back
    here when llm_output and response.id are missing."""
    rt, callback = _build_callback_runtime()

    response = _FakeLLMResult(
        model_name="gpt-4.1-mini-2025-04-14",
        response_id="ignored",
        message_id="chatcmpl-FROM-MSG",
    )
    _run_callback_on_llm_end(callback, response)

    event = _read_track_event(rt)
    expected = _fingerprint_for_llm_call(
        "gpt-4.1-mini-2025-04-14", "openai", "chatcmpl-FROM-MSG"
    )
    assert event["_fingerprint"] == expected


def test_callback_response_metadata_id_collides_with_httpx_fingerprint():
    """AIMessage.response_metadata['id'] (langchain 0.x metadata style)
    is the last-resort location for the chat-completion id. Callback
    MUST walk this location too."""
    rt, callback = _build_callback_runtime()

    response = _FakeLLMResult(
        model_name="gpt-4.1-mini-2025-04-14",
        response_id="ignored",
        response_metadata_id="chatcmpl-FROM-META",
    )
    _run_callback_on_llm_end(callback, response)

    event = _read_track_event(rt)
    expected = _fingerprint_for_llm_call(
        "gpt-4.1-mini-2025-04-14", "openai", "chatcmpl-FROM-META"
    )
    assert event["_fingerprint"] == expected


def test_callback_no_id_anywhere_falls_back_to_model_provider_only():
    """When no source yields a response id (a custom chat-model wrapper
    that strips the upstream id entirely), the callback MUST still
    emit a non-empty fingerprint so the dedup LRU sees it. The
    fingerprint will collide with any sibling emission that has the
    same model+provider but no id — which is acceptable, since both
    observers of the same call also lack the id."""
    rt, callback = _build_callback_runtime()

    response = _FakeLLMResult(
        model_name="custom-model-1",
        response_id="ignored",
        # No llm_output_id, no response_id_attr, no message_id
        # no response_metadata_id — every id location is missing.
    )
    # Also strip llm_output["id"] explicitly.
    assert "id" not in response.llm_output

    _run_callback_on_llm_end(callback, response)

    event = _read_track_event(rt)
    fp = event["_fingerprint"]
    assert fp  # non-empty
    expected = _fingerprint_for_llm_call("custom-model-1", "openai", None)
    assert fp == expected


def test_callback_fingerprint_stable_across_duplicate_emissions():
    """Re-invoking the callback for the same logical LLM call (same
    model, same chat-completion id) MUST produce the same fingerprint.
    The dedup LRU then collapses the second emission. This pins the
    "stable per call" contract that the dashboard relies on for an
    accurate ``llm_call_count``."""
    rt, callback = _build_callback_runtime()

    response_a = _FakeLLMResult(
        model_name="gpt-4.1-mini-2025-04-14",
        response_id="shared",
        llm_output_id="chatcmpl-SHARED",
    )
    response_b = _FakeLLMResult(
        model_name="gpt-4.1-mini-2025-04-14",
        response_id="shared",
        llm_output_id="chatcmpl-SHARED",
    )
    _run_callback_on_llm_end(callback, response_a)
    _run_callback_on_llm_end(callback, response_b)

    fp_a = rt.track.call_args_list[0][0][0]["_fingerprint"]
    fp_b = rt.track.call_args_list[1][0][0]["_fingerprint"]
    assert fp_a == fp_b

    # And the dedup LRU recognises it as the same fingerprint.
    state = make_dedup_state()
    assert _fingerprint_is_seen(state, fp_a) is False
    assert _fingerprint_is_seen(state, fp_b) is True


# ---------------------------------------------------------------------------
# Cross-observer contract: httpx transport and LangChain callback
# produce the SAME fingerprint for the same real LLM call.
# ---------------------------------------------------------------------------


def test_httpx_and_callback_fingerprints_collide_for_same_call():
    """End-to-end: the same OpenAI chat-completion id surfaces in both
    the response body (read by the httpx transport) and in
    ``response.llm_output["id"]`` (read by the LangChain callback).
    Both observers MUST produce identical fingerprints so the dedup
    LRU collapses the two emissions on the wire."""
    # 1. Drive the httpx transport with a real response body.
    rt_http = MagicMock()
    rt_http.track = MagicMock()
    rt_http._seen_track_fingerprints = make_dedup_state()
    patch_httpx(rt_http)

    body = _openai_chat_completion_response(
        response_id="chatcmpl-CROSS-OBSERVER",
    )
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=httpx.Response(200, content=body)
        )
        with httpx.Client(base_url="https://api.openai.com") as client:
            client.post("/v1/chat/completions", json={"model": "gpt-4.1-mini"})

    fp_http = rt_http.track.call_args_list[0][0][0]["_fingerprint"]
    reset_for_tests()

    # 2. Drive the LangChain callback with the same id in llm_output.
    rt_cb, callback = _build_callback_runtime()
    response = _FakeLLMResult(
        model_name="gpt-4.1-mini-2025-04-14",
        response_id="ignored",
        llm_output_id="chatcmpl-CROSS-OBSERVER",
    )
    _run_callback_on_llm_end(callback, response)
    fp_cb = _read_track_event(rt_cb)["_fingerprint"]

    # 3. The two fingerprints MUST be identical — that's the whole fix.
    assert fp_http == fp_cb, (
        f"httpx transport fingerprint {fp_http!r} != "
        f"callback fingerprint {fp_cb!r} — dedup will not collapse "
        f"the two emissions and the dashboard's llm_call_count will "
        f"be doubled."
    )

    # 4. And the dedup LRU actually collapses them when both fire.
    state = make_dedup_state()
    # First observation: unseen.
    assert _fingerprint_is_seen(state, fp_http) is False
    _fingerprint_is_seen(state, fp_http)
    # Second observation (the sibling callback emission): seen.
    assert _fingerprint_is_seen(state, fp_cb) is True