"""
Regression tests for ``nullrun.instrumentation.langgraph``.

Covers:

  - ``extract_usage_from_response`` — every branch of the usage-shape
    fan-out (dict, object, generations, response_metadata, llm_output,
    streaming chunks).
  - ``NullRunCallback`` — span emission (start/end) for chains / tools /
    agents, nested parent/child via ``parent_run_id``, the
    ``_active_runs`` FIFO eviction at 4096 entries, and the LLM-end
    track-event with normalised usage.
  - ``_extract_node_name`` — every branch (dict / list / str / missing).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nullrun.instrumentation.langgraph import (
    NullRunCallback,
    _ACTIVE_RUNS_MAX,
    _extract_node_name,
    extract_usage_from_response,
)


# ─── extract_usage_from_response ─────────────────────────────────────


def test_extract_usage_metadata_dict_form():
    """OpenAI-via-LangChain style: ``response.usage_metadata`` as a dict."""
    response = SimpleNamespace(usage_metadata={
        "input_tokens": 12,
        "output_tokens": 34,
        "total_tokens": 46,
    })
    usage = extract_usage_from_response(response, provider="openai", model="x")
    assert usage["input_tokens"] == 12
    assert usage["output_tokens"] == 34
    assert usage["total_tokens"] == 46
    assert usage["has_usage"] is True


def test_extract_usage_metadata_object_form():
    """Object with .input_tokens / .output_tokens / .total_tokens attrs."""
    response = SimpleNamespace(usage_metadata=SimpleNamespace(
        input_tokens=7,
        output_tokens=11,
        total_tokens=18,
    ))
    usage = extract_usage_from_response(response, provider="openai", model="x")
    assert usage["input_tokens"] == 7
    assert usage["output_tokens"] == 11
    assert usage["total_tokens"] == 18
    assert usage["has_usage"] is True


def test_extract_usage_from_generations():
    """``response.generations[0][0].message.usage_metadata`` — dict."""
    msg = SimpleNamespace(usage_metadata={"input_tokens": 5, "output_tokens": 6, "total_tokens": 11})
    gen = SimpleNamespace(message=msg)
    response = SimpleNamespace(generations=[[gen]])
    usage = extract_usage_from_response(response, provider="openai", model="x")
    assert usage["has_usage"] is True
    assert usage["input_tokens"] == 5


def test_extract_usage_from_generations_object_form():
    """``response.generations[0][0].message.usage_metadata`` as an object."""
    um = SimpleNamespace(input_tokens=1, output_tokens=2, total_tokens=3)
    msg = SimpleNamespace(usage_metadata=um)
    gen = SimpleNamespace(message=msg)
    response = SimpleNamespace(generations=[[gen]])
    usage = extract_usage_from_response(response, provider="openai", model="x")
    assert usage["has_usage"] is True
    assert usage["input_tokens"] == 1


def test_extract_usage_from_response_usage_dict():
    """Anthropic / standard OpenAI: ``response.usage`` as a dict."""
    response = SimpleNamespace(usage={"input_tokens": 100, "output_tokens": 200, "total_tokens": 300})
    usage = extract_usage_from_response(response, provider="anthropic", model="x")
    assert usage["has_usage"] is True
    assert usage["total_tokens"] == 300


def test_extract_usage_from_response_usage_object():
    """``response.usage`` as an object with .input_tokens / .total_tokens."""
    response = SimpleNamespace(usage=SimpleNamespace(input_tokens=4, output_tokens=8, total_tokens=12))
    usage = extract_usage_from_response(response, provider="anthropic", model="x")
    assert usage["has_usage"] is True
    assert usage["total_tokens"] == 12


def test_extract_usage_from_response_metadata_token_usage():
    """``response.response_metadata.token_usage`` — dict form (some providers)."""
    response = SimpleNamespace(response_metadata={"token_usage": {
        "prompt_tokens": 21,
        "completion_tokens": 22,
        "total_tokens": 43,
    }})
    usage = extract_usage_from_response(response, provider="openai", model="x")
    assert usage["has_usage"] is True
    assert usage["input_tokens"] == 21
    assert usage["output_tokens"] == 22


def test_extract_usage_from_response_metadata_alternate_keys():
    """Some providers use ``input_tokens`` / ``output_tokens`` inside token_usage."""
    response = SimpleNamespace(response_metadata={"token_usage": {
        "input_tokens": 8,
        "output_tokens": 9,
    }})
    usage = extract_usage_from_response(response, provider="anthropic", model="x")
    assert usage["has_usage"] is True
    assert usage["input_tokens"] == 8


def test_extract_usage_from_llm_output():
    """``response.llm_output.token_usage`` — ``LLMResult`` callback case."""
    response = SimpleNamespace(llm_output={"token_usage": {
        "prompt_tokens": 50,
        "completion_tokens": 51,
        "total_tokens": 101,
    }})
    usage = extract_usage_from_response(response, provider="openai", model="x")
    assert usage["has_usage"] is True
    assert usage["total_tokens"] == 101


def test_extract_usage_no_usage_data_has_usage_false():
    """Empty response → ``has_usage`` is False and tokens stay zero."""
    response = SimpleNamespace()  # no attrs
    usage = extract_usage_from_response(response, provider="openai", model="x")
    assert usage["has_usage"] is False
    assert usage["total_tokens"] == 0


def test_extract_usage_zero_values_has_usage_false():
    """All-zero usage dict → has_usage False."""
    response = SimpleNamespace(usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
    usage = extract_usage_from_response(response, provider="openai", model="x")
    assert usage["has_usage"] is False


def test_extract_usage_iterable_response_skipped():
    """Streaming-iterable response without usage → no-op branch hit."""
    class _Iter:
        def __iter__(self):
            return iter(["chunk1", "chunk2"])

    response = SimpleNamespace(chunks=_Iter())  # no usage attrs
    usage = extract_usage_from_response(response, provider="openai", model="x")
    assert usage["has_usage"] is False


# ─── _extract_node_name ───────────────────────────────────────────────


def test_extract_node_name_non_dict_returns_default():
    assert _extract_node_name("not a dict", default="chain") == "chain"
    assert _extract_node_name(None, default="chain") == "chain"


def test_extract_node_name_id_str():
    assert _extract_node_name({"id": "my_node"}, default="chain") == "my_node"


def test_extract_node_name_id_list():
    assert _extract_node_name({"id": ["ns", "my_node"]}, default="chain") == "my_node"


def test_extract_node_name_id_empty_list_returns_default():
    assert _extract_node_name({"id": []}, default="chain") == "chain"


def test_extract_node_name_falls_back_to_name():
    assert _extract_node_name({"name": "thing"}, default="chain") == "thing"


def test_extract_node_name_no_known_keys_returns_default():
    assert _extract_node_name({"foo": "bar"}, default="chain") == "chain"


# ─── NullRunCallback: span emission ──────────────────────────────────


def _make_cb_with_recorder() -> tuple[NullRunCallback, list, list]:
    """Build a callback wired to a mock runtime that captures span
    and llm_call emissions.
    """
    spans: list = []
    llms: list = []

    runtime = MagicMock()
    runtime.track_event.side_effect = lambda **kw: spans.append(kw)
    runtime.track.side_effect = lambda ev: llms.append(ev)

    cb = NullRunCallback(runtime=runtime)
    return cb, spans, llms


def test_chain_start_without_run_id_no_op():
    """When LangChain omits ``run_id`` the callback skips emit."""
    cb, spans, _ = _make_cb_with_recorder()
    cb.on_chain_start(serialized={"id": ["a"]}, inputs={})  # no run_id
    assert spans == []


def test_chain_start_then_end_emits_span_pair():
    """Happy path: chain_start emits span_start, chain_end emits span_end."""
    cb, spans, _ = _make_cb_with_recorder()
    cb.on_chain_start(serialized={"id": ["chain"]}, inputs={}, run_id="r1")
    cb.on_chain_end(outputs={"x": 1}, run_id="r1")

    kinds = [s["event_type"] for s in spans]
    assert kinds == ["span_start", "span_end"]
    assert spans[0]["fn_name"] == "chain"
    assert spans[0]["span_kind"] == "chain"
    # span_start + span_end share trace_id / span_id (matched by run_id).
    assert spans[0]["span_id"] == spans[1]["span_id"]
    assert spans[0]["trace_id"] == spans[1]["trace_id"]
    # No parent span — first call should be a root.
    assert spans[0]["parent_span_id"] is None
    assert spans[0]["depth"] == 0


def test_chain_end_without_start_no_op():
    """``on_chain_end`` for an unknown run_id silently no-ops."""
    cb, spans, _ = _make_cb_with_recorder()
    cb.on_chain_end(outputs={}, run_id="orphan")
    assert spans == []


def test_nested_chain_uses_active_run_as_parent():
    """Inner chain's span_id is referenced as the outer span's parent_span_id."""
    cb, spans, _ = _make_cb_with_recorder()
    cb.on_chain_start(serialized={"id": "outer"}, inputs={}, run_id="outer")
    cb.on_chain_start(serialized={"id": "inner"}, inputs={}, run_id="inner", parent_run_id="outer")

    outer_span = spans[0]
    inner_span = spans[1]
    assert inner_span["parent_span_id"] == outer_span["span_id"]
    assert inner_span["trace_id"] == outer_span["trace_id"]
    assert inner_span["depth"] == 1


def test_parent_run_id_falls_back_to_contextvar():
    """When parent_run_id is unknown, fall back to contextvar span."""
    from nullrun.tracing import create_root_span, set_span

    cb, spans, _ = _make_cb_with_recorder()
    # Push a span via the contextvar (mimics @protect).
    parent = create_root_span()
    token = set_span(parent)

    try:
        cb.on_chain_start(serialized={"id": "x"}, inputs={}, run_id="child", parent_run_id="unknown-parent")
    finally:
        from nullrun.tracing import reset_span

        reset_span(token)

    inner = spans[0]
    assert inner["parent_span_id"] == parent.span_id
    assert inner["trace_id"] == parent.trace_id
    assert inner["depth"] == 1


# ─── Tool callbacks ──────────────────────────────────────────────────


def test_tool_start_then_end():
    cb, spans, _ = _make_cb_with_recorder()
    cb.on_tool_start(serialized={"id": "calculator"}, input_str="1+1", run_id="t1")
    cb.on_tool_end(output="2", run_id="t1")
    kinds = [s["event_type"] for s in spans]
    assert kinds == ["span_start", "span_end"]
    assert spans[0]["span_kind"] == "tool"
    assert spans[0]["fn_name"] == "calculator"


def test_tool_error_emits_span_end_with_error():
    cb, spans, _ = _make_cb_with_recorder()
    cb.on_tool_start(serialized={"id": "x"}, input_str="", run_id="t1")
    cb.on_tool_error(error=RuntimeError("boom"), run_id="t1")
    assert spans[1]["event_type"] == "span_end"
    assert spans[1]["error"] == "boom"


def test_tool_end_without_start_no_op():
    cb, spans, _ = _make_cb_with_recorder()
    cb.on_tool_end(output="x", run_id="orphan")
    assert spans == []


def test_tool_start_without_run_id_no_op():
    cb, spans, _ = _make_cb_with_recorder()
    cb.on_tool_start(serialized={"id": "x"}, input_str="", run_id=None)
    assert spans == []


# ─── Agent callbacks ─────────────────────────────────────────────────


def test_agent_action_then_finish():
    cb, spans, _ = _make_cb_with_recorder()
    action = SimpleNamespace(tool="search")
    cb.on_agent_action(action, run_id="a1")
    cb.on_agent_finish(finish=None, run_id="a1")
    kinds = [s["event_type"] for s in spans]
    assert kinds == ["span_start", "span_end"]
    assert spans[0]["fn_name"] == "agent_action:search"
    assert spans[0]["span_kind"] == "agent"


def test_agent_action_without_run_id_no_op():
    cb, spans, _ = _make_cb_with_recorder()
    cb.on_agent_action(SimpleNamespace(tool="x"), run_id=None)
    assert spans == []


def test_agent_action_default_tool_name():
    """``action.tool`` missing → fn_name defaults to ``agent_action:agent``."""
    cb, spans, _ = _make_cb_with_recorder()
    cb.on_agent_action(SimpleNamespace(), run_id="a1")
    assert spans[0]["fn_name"] == "agent_action:agent"


def test_agent_finish_without_action_no_op():
    cb, spans, _ = _make_cb_with_recorder()
    cb.on_agent_finish(finish=None, run_id="orphan")
    assert spans == []


# ─── LLM end → track (not track_event) ───────────────────────────────


def test_on_llm_end_emits_llm_call():
    """``on_llm_end`` extracts usage and forwards to ``runtime.track``."""
    cb, _spans, llms = _make_cb_with_recorder()
    response = SimpleNamespace(usage_metadata={
        "input_tokens": 5,
        "output_tokens": 10,
        "total_tokens": 15,
    })
    cb.on_llm_end(response, invocation_params={"model_name": "gpt-4o", "model_provider": "openai"})
    assert len(llms) == 1
    ev = llms[0]
    assert ev["type"] == "llm_call"
    assert ev["model"] == "gpt-4o"
    assert ev["provider"] == "openai"
    assert ev["tokens"] == 15
    assert ev["has_usage"] is True


def test_on_llm_end_no_usage_still_emits():
    """Even with no usage data, on_llm_end forwards an llm_call event
    with ``has_usage=False`` so the SDK still records the call shape.
    """
    cb, _spans, llms = _make_cb_with_recorder()
    cb.on_llm_end(SimpleNamespace(), invocation_params={})
    assert len(llms) == 1
    assert llms[0]["has_usage"] is False


def test_on_llm_end_runtime_failure_is_swallowed():
    """If ``runtime.track`` raises, on_llm_end swallows the failure."""
    runtime = MagicMock()
    runtime.track.side_effect = RuntimeError("down")
    cb = NullRunCallback(runtime=runtime)
    # Must not raise.
    cb.on_llm_end(SimpleNamespace(usage_metadata={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}))


def test_track_event_failure_is_swallowed():
    """Span emission failures are swallowed — never break the user's chain."""
    runtime = MagicMock()
    runtime.track_event.side_effect = RuntimeError("down")
    cb = NullRunCallback(runtime=runtime)
    cb.on_chain_start(serialized={"id": "x"}, inputs={}, run_id="r1")  # no raise
    cb.on_chain_end(outputs={}, run_id="r1")  # no raise


# ─── _active_runs FIFO cap ───────────────────────────────────────────


def test_active_runs_cap_evicts_oldest(monkeypatch):
    """When the FIFO cap is hit, the OLDEST run is evicted (with a warning)."""
    cb, spans, _ = _make_cb_with_recorder()
    # Lower the cap to make the test fast.
    monkeypatch.setattr(cb, "_active_runs_max", 3)
    # Open 4 chains.
    for i in range(4):
        cb.on_chain_start(serialized={"id": f"c{i}"}, inputs={}, run_id=f"r{i}")
    # The first run (r0) should have been evicted.
    assert "r0" not in cb._active_runs
    assert "r3" in cb._active_runs


def test_active_runs_cap_eviction_warning(caplog):
    """When eviction fires, a warning is logged so operators see chain-end drops."""
    import logging

    cb, _spans, _ = _make_cb_with_recorder()
    cb._active_runs_max = 2
    with caplog.at_level(logging.WARNING, logger="nullrun.instrumentation.langgraph"):
        for i in range(3):
            cb.on_chain_start(serialized={"id": f"c{i}"}, inputs={}, run_id=f"r{i}")
    assert any("evicted oldest run_id" in r.getMessage() for r in caplog.records)


def test_active_runs_default_max():
    """Default cap matches the documented 4096."""
    cb, _, _ = _make_cb_with_recorder()
    assert cb._active_runs_max == _ACTIVE_RUNS_MAX == 4096