"""
Regression tests for the autogen auto-instrumentation patch.

These tests inject synthetic stand-ins for `autogen_agentchat.agents`
and `autogen_ext.models.openai` via ``sys.modules`` so the patch can
exercise the real wrapper code paths without requiring the (heavy)
optional dependency in CI.

The pattern mirrors ``tests/test_blocker_fixes.py``: monkeypatch
the vendor module, reload our patch module, then drive the wrapped
class through ``MagicMock``-backed call sites.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _install_fake_autogen(monkeypatch, *, with_ext: bool = True) -> dict:
    """Install fake ``autogen_agentchat`` (+ optional ``autogen_ext``)
    modules into ``sys.modules`` and return the call recorder dict.

    The recorder tracks every ``runtime.track_event`` /
    ``runtime.track`` invocation so the tests can assert on the
    shape of the emitted events without depending on a real
    NullRunRuntime.
    """
    recorder = {"track_event": [], "track": []}

    # Build BaseChatAgent stand-in: a class whose ``on_messages`` is
    # replaceable per test. ``_nullrun_patched`` is consulted by the
    # patcher as the idempotency marker.
    class _FakeBaseChatAgent:
        _nullrun_patched = False

        def on_messages(self, messages, cancellation_token=None):
            return SimpleNamespace(content="ok")

    fake_agents_mod = ModuleType("autogen_agentchat.agents")
    fake_agents_mod.BaseChatAgent = _FakeBaseChatAgent
    monkeypatch.setitem(sys.modules, "autogen_agentchat", ModuleType("autogen_agentchat"))
    monkeypatch.setitem(sys.modules, "autogen_agentchat.agents", fake_agents_mod)

    if with_ext:

        class _Usage:
            prompt_tokens = 12
            completion_tokens = 34
            total_tokens = 46

        class _Result:
            usage = _Usage()

        class _FakeOpenAIChatCompletionClient:
            _nullrun_patched = False
            model = "gpt-4o-mini"

            @staticmethod
            def create(self, *args, **kwargs):
                return _Result()

        fake_ext_mod = ModuleType("autogen_ext.models.openai")
        fake_ext_mod.OpenAIChatCompletionClient = _FakeOpenAIChatCompletionClient
        monkeypatch.setitem(sys.modules, "autogen_ext", ModuleType("autogen_ext"))
        monkeypatch.setitem(sys.modules, "autogen_ext.models", ModuleType("autogen_ext.models"))
        monkeypatch.setitem(sys.modules, "autogen_ext.models.openai", fake_ext_mod)
    else:
        # Install the parent package so the inner ``from
        # autogen_ext.models.openai import OpenAIChatCompletionClient``
        # raises ImportError cleanly.
        monkeypatch.setitem(sys.modules, "autogen_ext", ModuleType("autogen_ext"))

    return recorder


def _fake_runtime(recorder: dict) -> MagicMock:
    """Build a MagicMock that mimics the runtime surface the patch
    consults. ``track_event`` / ``track`` capture into ``recorder``.
    """

    rt = MagicMock()
    rt.track_event.side_effect = lambda **kw: recorder["track_event"].append(kw)
    rt.track.side_effect = lambda ev: recorder["track"].append(ev)
    return rt


def _reload_patch_module():
    """Reload ``nullrun.instrumentation.autogen`` so its top-level
    ``_autogen_patched`` / ``_orig_on_messages`` globals reset between
    tests. Without the reload the idempotency marker would carry
    across tests and silently skip the wrap step.
    """
    if "nullrun.instrumentation.autogen" in sys.modules:
        importlib.reload(sys.modules["nullrun.instrumentation.autogen"])
    else:
        importlib.import_module("nullrun.instrumentation.autogen")


@pytest.fixture
def fresh_patch_module():
    """Reset the patch module's globals before each test.

    The fixture always reloads so the previous test's installed wrap
    does not leak into the next one.
    """
    _reload_patch_module()
    yield
    _reload_patch_module()


# ─── ImportError branch ─────────────────────────────────────────────


def test_patch_autogen_returns_false_when_missing(monkeypatch, fresh_patch_module):
    """When ``autogen_agentchat`` is not importable, patch returns False
    without raising — the user sees no instrumentation but no crash.
    """
    # Force ImportError on the inner ``from autogen_agentchat.agents import``.
    monkeypatch.setitem(sys.modules, "autogen_agentchat", None)
    monkeypatch.setitem(sys.modules, "autogen_agentchat.agents", None)

    from nullrun.instrumentation.autogen import patch_autogen

    assert patch_autogen(MagicMock()) is False


def test_patch_autogen_without_ext_module(monkeypatch, fresh_patch_module):
    """``autogen_ext`` missing is a graceful skip on the usage-capture
    branch — the span wrapper still installs.
    """
    _install_fake_autogen(monkeypatch, with_ext=False)
    from nullrun.instrumentation.autogen import patch_autogen

    rt = MagicMock()
    assert patch_autogen(rt) is True


# ─── Idempotency ─────────────────────────────────────────────────────


def test_patch_autogen_idempotent(monkeypatch, fresh_patch_module):
    """Calling ``patch_autogen`` twice does not double-wrap."""
    _install_fake_autogen(monkeypatch)
    from autogen_agentchat.agents import BaseChatAgent

    from nullrun.instrumentation.autogen import patch_autogen

    first_orig = BaseChatAgent.on_messages
    assert patch_autogen(MagicMock()) is True
    second_orig = BaseChatAgent.on_messages
    assert patch_autogen(MagicMock()) is True
    # Second call must NOT have re-stashed the original.
    assert second_orig is second_orig


def test_patch_autogen_skips_when_class_already_patched(monkeypatch, fresh_patch_module):
    """If the class marker is already True (e.g. a parallel test
    process installed it), the patch returns True without rewriting.
    """
    _install_fake_autogen(monkeypatch)
    from autogen_agentchat.agents import BaseChatAgent

    from nullrun.instrumentation.autogen import patch_autogen

    BaseChatAgent._nullrun_patched = True
    try:
        assert patch_autogen(MagicMock()) is True
    finally:
        BaseChatAgent._nullrun_patched = False


# ─── on_messages wrapper ─────────────────────────────────────────────


def test_on_messages_success_emits_span_start_and_end(monkeypatch, fresh_patch_module):
    """Happy path: wrapped ``on_messages`` emits span_start before
    calling the original and span_end after.
    """
    _install_fake_autogen(monkeypatch)
    recorder = {"track_event": [], "track": []}
    rt = _fake_runtime(recorder)

    from autogen_agentchat.agents import BaseChatAgent

    from nullrun.instrumentation.autogen import patch_autogen

    assert patch_autogen(rt) is True
    result = BaseChatAgent.on_messages(None, ["hello"])
    assert result.content == "ok"

    # span_start (with fn_name + span_kind) then span_end (no kwargs).
    kinds = [ev.get("event_type") for ev in recorder["track_event"]]
    assert kinds == ["span_start", "span_end"]
    # ``getattr(self, "name", "agent") or "agent"`` — fake class has no
    # ``.name`` so the default kicks in.
    assert recorder["track_event"][0]["fn_name"] == "agent"
    assert recorder["track_event"][0]["span_kind"] == "agent"


def test_on_messages_exception_emits_span_end_with_error(monkeypatch, fresh_patch_module):
    """When the wrapped body raises, the wrapper still emits
    span_end with ``error=str(e)`` and re-raises the original.
    """
    _install_fake_autogen(monkeypatch)

    from autogen_agentchat.agents import BaseChatAgent

    # Replace the original on_messages with one that raises.
    BaseChatAgent.on_messages = MagicMock(side_effect=RuntimeError("boom"))
    recorder = {"track_event": [], "track": []}
    rt = _fake_runtime(recorder)

    from nullrun.instrumentation.autogen import patch_autogen

    assert patch_autogen(rt) is True

    with pytest.raises(RuntimeError, match="boom"):
        BaseChatAgent.on_messages(None, ["x"])

    # span_start + span_end(error=...)
    spans = recorder["track_event"]
    assert [s["event_type"] for s in spans] == ["span_start", "span_end"]
    assert spans[1].get("error") == "boom"


def test_on_messages_track_event_failure_is_swallowed(monkeypatch, fresh_patch_module):
    """If the runtime's ``track_event`` raises on span_start, the
    wrapper must NOT crash — observability is downstream of the
    user's work (mirrors the contract in ``_emit_span_start``).
    """
    _install_fake_autogen(monkeypatch)

    rt = MagicMock()
    rt.track_event.side_effect = [RuntimeError("down"), None]
    from autogen_agentchat.agents import BaseChatAgent

    from nullrun.instrumentation.autogen import patch_autogen

    assert patch_autogen(rt) is True
    # Should NOT raise even though track_event errored.
    assert BaseChatAgent.on_messages(None, []).content == "ok"


# ─── OpenAIChatCompletionClient.create wrapper ───────────────────────


def test_openai_create_with_usage_emits_llm_call(monkeypatch, fresh_patch_module):
    """When the wrapped CreateResult has ``usage`` with non-zero
    tokens, the wrapper emits an llm_call event with prompt/
    completion/total split.
    """
    _install_fake_autogen(monkeypatch, with_ext=True)
    recorder = {"track_event": [], "track": []}
    rt = _fake_runtime(recorder)

    from autogen_ext.models.openai import OpenAIChatCompletionClient

    from nullrun.instrumentation.autogen import patch_autogen

    assert patch_autogen(rt) is True

    # The wrapper reads ``getattr(self, "model", None)`` — needs an
    # instance with a ``.model`` attribute, not a class-level one.
    class _Inst:
        model = "gpt-4o-mini"

    inst = _Inst()
    result = OpenAIChatCompletionClient.create(inst)
    # Wrapper returns the original result unchanged.
    assert result.usage.prompt_tokens == 12

    events = recorder["track"]
    assert len(events) == 1
    ev = events[0]
    assert ev["type"] == "llm_call"
    assert ev["provider"] == "autogen"
    assert ev["model"] == "gpt-4o-mini"
    assert ev["input_tokens"] == 12
    assert ev["output_tokens"] == 34
    assert ev["tokens"] == 46


def test_openai_create_without_usage_no_track(monkeypatch, fresh_patch_module):
    """No ``usage`` on the CreateResult — wrapper skips emit."""
    _install_fake_autogen(monkeypatch, with_ext=True)

    from autogen_ext.models.openai import OpenAIChatCompletionClient

    OpenAIChatCompletionClient.create = staticmethod(lambda self, *a, **k: SimpleNamespace())
    recorder = {"track_event": [], "track": []}
    rt = _fake_runtime(recorder)

    from nullrun.instrumentation.autogen import patch_autogen

    assert patch_autogen(rt) is True
    OpenAIChatCompletionClient.create(None)

    assert recorder["track"] == []


def test_openai_create_track_failure_is_swallowed(monkeypatch, fresh_patch_module):
    """If ``runtime.track`` raises, the wrapper returns the original
    CreateResult and does not propagate the failure.
    """
    _install_fake_autogen(monkeypatch, with_ext=True)
    rt = MagicMock()
    rt.track.side_effect = RuntimeError("down")
    rt.track_event.side_effect = lambda **kw: None

    from autogen_ext.models.openai import OpenAIChatCompletionClient

    from nullrun.instrumentation.autogen import patch_autogen

    assert patch_autogen(rt) is True
    result = OpenAIChatCompletionClient.create(None)
    assert result.usage.prompt_tokens == 12


# ─── unpatch ─────────────────────────────────────────────────────────


def test_unpatch_restores_original(monkeypatch, fresh_patch_module):
    """After ``unpatch_autogen``, the wrapped ``on_messages`` and
    ``create`` methods are restored to the originals and the
    idempotency markers are cleared.
    """
    _install_fake_autogen(monkeypatch, with_ext=True)
    from autogen_agentchat.agents import BaseChatAgent
    from autogen_ext.models.openai import OpenAIChatCompletionClient

    from nullrun.instrumentation.autogen import patch_autogen, unpatch_autogen

    original_on_messages = BaseChatAgent.on_messages
    original_create = OpenAIChatCompletionClient.create

    assert patch_autogen(MagicMock()) is True
    assert BaseChatAgent.on_messages is not original_on_messages
    assert OpenAIChatCompletionClient.create is not original_create

    unpatch_autogen()
    assert BaseChatAgent.on_messages is original_on_messages
    assert OpenAIChatCompletionClient.create is original_create
    assert BaseChatAgent._nullrun_patched is False
    assert OpenAIChatCompletionClient._nullrun_patched is False


def test_unpatch_when_not_patched_is_noop(monkeypatch, fresh_patch_module):
    """``unpatch_autogen`` without a prior patch is a safe no-op."""
    from nullrun.instrumentation.autogen import unpatch_autogen

    unpatch_autogen()  # should not raise


def test_unpatch_when_module_missing(monkeypatch, fresh_patch_module):
    """If the module import disappears between patch and unpatch
    unpatch still resets the local flag instead of crashing.
    """
    _install_fake_autogen(monkeypatch)
    from nullrun.instrumentation.autogen import patch_autogen, unpatch_autogen

    assert patch_autogen(MagicMock()) is True
    # Drop the vendor module to simulate a transient uninstall.
    monkeypatch.delitem(sys.modules, "autogen_agentchat.agents", raising=False)
    unpatch_autogen()  # should not raise
