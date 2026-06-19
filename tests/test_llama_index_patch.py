"""
Regression tests for the llama-index auto-instrumentation patch.

Installs a fake ``llama_index.core.instrumentation`` module so the
patch can subscribe handlers without needing the real dep in CI.
"""
from __future__ import annotations

import importlib
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _install_fake_llama_index(monkeypatch) -> dict:
    """Install ``llama_index.core.instrumentation`` with a fake
    ``get_dispatcher`` that captures event handlers in a list.

    Returns the dispatcher (so tests can fire ``LLMChatEndEvent``
    or ``FunctionCallEvent`` at the registered handlers).
    """
    captured_handlers: list = []

    class _FakeDispatcher:
        def __init__(self):
            self._captured = captured_handlers

        def add_event_handler(self, event_cls, handler):
            self._captured.append((event_cls, handler))

        def remove_event_handler(self, event_cls, handler):
            for i, (cls, h) in enumerate(self._captured):
                if cls is event_cls and h is handler:
                    del self._captured[i]
                    return

    dispatcher = _FakeDispatcher()

    events_mod = ModuleType("llama_index.core.instrumentation.events")
    events_mod_llm = ModuleType("llama_index.core.instrumentation.events.llm")
    events_mod_llm.LLMChatEndEvent = type("LLMChatEndEvent", (), {})
    events_mod_tool = ModuleType("llama_index.core.instrumentation.events.tool")
    events_mod_tool.FunctionCallEvent = type("FunctionCallEvent", (), {})
    events_mod.llm = events_mod_llm
    events_mod.tool = events_mod_tool

    inst_mod = ModuleType("llama_index.core.instrumentation")
    inst_mod.get_dispatcher = MagicMock(return_value=dispatcher)
    monkeypatch.setitem(sys.modules, "llama_index", ModuleType("llama_index"))
    monkeypatch.setitem(sys.modules, "llama_index.core", ModuleType("llama_index.core"))
    monkeypatch.setitem(sys.modules, "llama_index.core.instrumentation", inst_mod)
    monkeypatch.setitem(sys.modules, "llama_index.core.instrumentation.events", events_mod)
    monkeypatch.setitem(sys.modules, "llama_index.core.instrumentation.events.llm", events_mod_llm)
    monkeypatch.setitem(sys.modules, "llama_index.core.instrumentation.events.tool", events_mod_tool)

    return dispatcher


def _fake_runtime() -> MagicMock:
    rt = MagicMock()
    rt.track.side_effect = lambda ev: getattr(rt, "_captured", []).append(ev)
    rt._captured = []
    return rt


@pytest.fixture
def fresh_patch_module():
    if "nullrun.instrumentation.llama_index" in sys.modules:
        importlib.reload(sys.modules["nullrun.instrumentation.llama_index"])
    else:
        importlib.import_module("nullrun.instrumentation.llama_index")
    yield
    if "nullrun.instrumentation.llama_index" in sys.modules:
        importlib.reload(sys.modules["nullrun.instrumentation.llama_index"])


# ─── ImportError branch ──────────────────────────────────────────────


def test_patch_llama_index_returns_false_when_missing(monkeypatch, fresh_patch_module):
    monkeypatch.setitem(sys.modules, "llama_index", None)
    monkeypatch.setitem(sys.modules, "llama_index.core", None)
    monkeypatch.setitem(sys.modules, "llama_index.core.instrumentation", None)
    from nullrun.instrumentation.llama_index import patch_llama_index

    assert patch_llama_index(MagicMock()) is False


# ─── Idempotency ─────────────────────────────────────────────────────


def test_patch_llama_index_idempotent(monkeypatch, fresh_patch_module):
    _install_fake_llama_index(monkeypatch)
    from nullrun.instrumentation.llama_index import patch_llama_index

    assert patch_llama_index(MagicMock()) is True
    assert patch_llama_index(MagicMock()) is True


# ─── Happy paths ─────────────────────────────────────────────────────


def test_llm_chat_end_with_dict_usage_emits_track(monkeypatch, fresh_patch_module):
    """``LLMChatEndEvent`` with ``event.response.raw.usage`` as a
    dict — the wrapper emits an llm_call event with split
    prompt / completion / total.
    """
    dispatcher = _install_fake_llama_index(monkeypatch)
    rt = _fake_runtime()

    from nullrun.instrumentation.llama_index import patch_llama_index
    assert patch_llama_index(rt) is True

    # Two handlers registered: LLMChatEndEvent + FunctionCallEvent.
    assert len(dispatcher._captured) == 2

    import llama_index.core.instrumentation.events.llm as _llm_events
    _LLM = _llm_events.LLMChatEndEvent

    # Fire the LLMChatEndEvent handler manually.
    # The patch reads ``event.response.raw`` and applies ``hasattr(raw,
    # "usage")`` to decide between the dict-form (raw IS the usage
    # dict) and the object-form (raw.usage is the usage dict). Most
    # llama-index responses are the dict form.
    for cls, handler in dispatcher._captured:
        if cls is _LLM:
            response = SimpleNamespace(
                raw={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                model="gpt-4o",
            )
            event = SimpleNamespace(response=response)
            handler(event)
            break

    events = rt._captured
    assert len(events) == 1
    ev = events[0]
    assert ev["type"] == "llm_call"
    assert ev["provider"] == "llama_index"
    assert ev["model"] == "gpt-4o"
    assert ev["input_tokens"] == 10
    assert ev["output_tokens"] == 5
    assert ev["tokens"] == 15


def test_llm_chat_end_without_usage_no_emit(monkeypatch, fresh_patch_module):
    """All-zero usage → wrapper returns early without emitting."""
    dispatcher = _install_fake_llama_index(monkeypatch)
    rt = _fake_runtime()

    from nullrun.instrumentation.llama_index import patch_llama_index
    assert patch_llama_index(rt) is True

    import llama_index.core.instrumentation.events.llm as _llm_events
    _LLM = _llm_events.LLMChatEndEvent

    for cls, handler in dispatcher._captured:
        if cls is _LLM:
            # Empty usage dict → all-zero → early return.
            response = SimpleNamespace(raw={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, model="x")
            handler(SimpleNamespace(response=response))
            break

    assert rt._captured == []


def test_llm_chat_end_response_without_raw(monkeypatch, fresh_patch_module):
    """``event.response.raw`` is missing — wrapper treats as empty."""
    dispatcher = _install_fake_llama_index(monkeypatch)
    rt = _fake_runtime()

    from nullrun.instrumentation.llama_index import patch_llama_index
    assert patch_llama_index(rt) is True

    import llama_index.core.instrumentation.events.llm as _llm_events
    _LLM = _llm_events.LLMChatEndEvent

    for cls, handler in dispatcher._captured:
        if cls is _LLM:
            response = SimpleNamespace(model="x")  # no .raw
            handler(SimpleNamespace(response=response))
            break

    assert rt._captured == []


def test_llm_chat_end_object_usage_attr(monkeypatch, fresh_patch_module):
    """``event.response.raw.usage`` is an object with .prompt_tokens etc."""
    dispatcher = _install_fake_llama_index(monkeypatch)
    rt = _fake_runtime()

    from nullrun.instrumentation.llama_index import patch_llama_index
    assert patch_llama_index(rt) is True

    import llama_index.core.instrumentation.events.llm as _llm_events
    _LLM = _llm_events.LLMChatEndEvent

    class _Usage:
        prompt_tokens = 3
        completion_tokens = 4
        total_tokens = 0  # missing → falls back to prompt+completion

    for cls, handler in dispatcher._captured:
        if cls is _LLM:
            # ``raw`` is an object whose ``.usage`` is a dict. The
            # ``hasattr(usage, "usage")`` branch unwraps once and then
            # ``usage.get(...)`` reads the dict.
            response = SimpleNamespace(
                raw=SimpleNamespace(usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}),
                model="x",
            )
            handler(SimpleNamespace(response=response))
            break

    events = rt._captured
    assert len(events) == 1
    assert events[0]["tokens"] == 7


def test_function_call_event_emits_tool_call(monkeypatch, fresh_patch_module):
    """``FunctionCallEvent`` with a ``tool.name`` attribute — the
    wrapper emits a tool_call event.
    """
    dispatcher = _install_fake_llama_index(monkeypatch)
    rt = _fake_runtime()

    from nullrun.instrumentation.llama_index import patch_llama_index
    assert patch_llama_index(rt) is True

    import llama_index.core.instrumentation.events.tool as _tool_events
    _FCE = _tool_events.FunctionCallEvent

    tool = SimpleNamespace(name="search")
    for cls, handler in dispatcher._captured:
        if cls is _FCE:
            handler(SimpleNamespace(tool=tool))
            break

    events = rt._captured
    assert len(events) == 1
    assert events[0]["type"] == "tool_call"
    assert events[0]["tool_name"] == "search"


def test_function_call_event_tool_without_name_uses_default(monkeypatch, fresh_patch_module):
    """``event.tool`` exists but no ``.name`` — default to 'tool'."""
    dispatcher = _install_fake_llama_index(monkeypatch)
    rt = _fake_runtime()

    from nullrun.instrumentation.llama_index import patch_llama_index
    assert patch_llama_index(rt) is True

    import llama_index.core.instrumentation.events.tool as _tool_events
    _FCE = _tool_events.FunctionCallEvent

    for cls, handler in dispatcher._captured:
        if cls is _FCE:
            handler(SimpleNamespace(tool=SimpleNamespace()))  # no .name
            break

    events = rt._captured
    assert len(events) == 1
    assert events[0]["tool_name"] == "tool"


def test_function_call_event_without_tool_uses_default(monkeypatch, fresh_patch_module):
    """``event.tool`` is None — default to 'tool'."""
    dispatcher = _install_fake_llama_index(monkeypatch)
    rt = _fake_runtime()

    from nullrun.instrumentation.llama_index import patch_llama_index
    assert patch_llama_index(rt) is True

    import llama_index.core.instrumentation.events.tool as _tool_events
    _FCE = _tool_events.FunctionCallEvent

    for cls, handler in dispatcher._captured:
        if cls is _FCE:
            handler(SimpleNamespace(tool=None))
            break

    events = rt._captured
    assert len(events) == 1
    assert events[0]["tool_name"] == "tool"


# ─── Track failure is swallowed ──────────────────────────────────────


def test_track_failure_is_swallowed(monkeypatch, fresh_patch_module):
    dispatcher = _install_fake_llama_index(monkeypatch)
    rt = MagicMock()
    rt.track.side_effect = RuntimeError("down")

    from nullrun.instrumentation.llama_index import patch_llama_index
    assert patch_llama_index(rt) is True

    import llama_index.core.instrumentation.events.llm as _llm_events
    import llama_index.core.instrumentation.events.tool as _tool_events
    _LLM = _llm_events.LLMChatEndEvent
    _FCE = _tool_events.FunctionCallEvent

    # LLM end: must not raise.
    for cls, handler in dispatcher._captured:
        if cls is _LLM:
            response = SimpleNamespace(
                raw={"usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            )
            handler(SimpleNamespace(response=response))
            break

    # Tool call: must not raise.
    for cls, handler in dispatcher._captured:
        if cls is _FCE:
            handler(SimpleNamespace(tool=SimpleNamespace(name="x")))
            break


# ─── unpatch ─────────────────────────────────────────────────────────


def test_unpatch_removes_handlers(monkeypatch, fresh_patch_module):
    dispatcher = _install_fake_llama_index(monkeypatch)
    rt = _fake_runtime()
    from nullrun.instrumentation.llama_index import patch_llama_index, unpatch_llama_index

    assert patch_llama_index(rt) is True
    assert len(dispatcher._captured) == 2
    unpatch_llama_index()
    assert len(dispatcher._captured) == 0


def test_unpatch_when_not_patched_is_noop(monkeypatch, fresh_patch_module):
    from nullrun.instrumentation.llama_index import unpatch_llama_index

    unpatch_llama_index()  # safe


def test_unpatch_when_module_missing(monkeypatch, fresh_patch_module):
    _install_fake_llama_index(monkeypatch)
    from nullrun.instrumentation.llama_index import patch_llama_index, unpatch_llama_index

    assert patch_llama_index(MagicMock()) is True
    monkeypatch.delitem(sys.modules, "llama_index.core.instrumentation", raising=False)
    unpatch_llama_index()  # should not raise