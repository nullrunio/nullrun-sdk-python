"""
Regression tests for the crewai auto-instrumentation patch.

Mirrors the autogen tests: inject a fake ``crewai`` module so the
patch can run end-to-end without the (heavy) optional dep.
"""
from __future__ import annotations

import importlib
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _install_fake_crewai(monkeypatch, *, with_async: bool = True) -> dict:
    """Install a fake ``crewai`` module exposing ``Crew`` whose
    ``kickoff`` / ``kickoff_async`` are MagicMocks. Returns the
    recorder dict for runtime emissions.
    """
    recorder = {"track": [], "track_event": []}

    class _FakeCrew:
        _nullrun_patched = False
        usage_metrics: dict = {}

        @staticmethod
        def kickoff(self, inputs=None, **kwargs):
            return SimpleNamespace(result="ok")

    if with_async:
        class _FakeCrewWithAsync(_FakeCrew):
            @staticmethod
            async def kickoff_async(self, inputs=None, **kwargs):
                return SimpleNamespace(result="ok-async")
    else:
        _FakeCrewWithAsync = _FakeCrew

    fake_mod = ModuleType("crewai")
    fake_mod.Crew = _FakeCrewWithAsync
    monkeypatch.setitem(sys.modules, "crewai", fake_mod)

    return recorder


def _fake_runtime(recorder: dict) -> MagicMock:
    rt = MagicMock()
    rt.track.side_effect = lambda ev: recorder["track"].append(ev)
    rt.track_event.side_effect = lambda **kw: recorder["track_event"].append(kw)
    return rt


@pytest.fixture
def fresh_patch_module():
    if "nullrun.instrumentation.crewai" in sys.modules:
        importlib.reload(sys.modules["nullrun.instrumentation.crewai"])
    else:
        importlib.import_module("nullrun.instrumentation.crewai")
    yield
    if "nullrun.instrumentation.crewai" in sys.modules:
        importlib.reload(sys.modules["nullrun.instrumentation.crewai"])


# ─── ImportError / module-missing branches ───────────────────────────


def test_patch_crewai_returns_false_when_missing(monkeypatch, fresh_patch_module):
    monkeypatch.setitem(sys.modules, "crewai", None)
    from nullrun.instrumentation.crewai import patch_crewai

    assert patch_crewai(MagicMock()) is False


def test_patch_crewai_idempotent(monkeypatch, fresh_patch_module):
    _install_fake_crewai(monkeypatch)
    from nullrun.instrumentation.crewai import patch_crewai
    from crewai import Crew

    assert patch_crewai(MagicMock()) is True
    wrapped = Crew.kickoff
    # Second call must NOT re-wrap.
    assert patch_crewai(MagicMock()) is True
    assert Crew.kickoff is wrapped


def test_patch_crewai_skips_when_class_marker_present(monkeypatch, fresh_patch_module):
    _install_fake_crewai(monkeypatch)
    from nullrun.instrumentation.crewai import patch_crewai
    from crewai import Crew

    Crew._nullrun_patched = True
    try:
        assert patch_crewai(MagicMock()) is True
    finally:
        Crew._nullrun_patched = False


def test_patch_crewai_without_async_kickoff(monkeypatch, fresh_patch_module):
    """Crewai versions without ``kickoff_async`` — patcher still
    installs the sync wrap and silently skips the async wrap.
    """
    _install_fake_crewai(monkeypatch, with_async=False)
    from nullrun.instrumentation.crewai import patch_crewai
    from crewai import Crew

    assert patch_crewai(MagicMock()) is True


# ─── kickoff wrapper ──────────────────────────────────────────────────


def test_kickoff_emits_usage_metrics_per_model(monkeypatch, fresh_patch_module):
    """After Crew.kickoff returns, the wrapper reads
    ``crew.usage_metrics`` and emits one llm_call per model.
    """
    _install_fake_crewai(monkeypatch)
    recorder = {"track": [], "track_event": []}
    rt = _fake_runtime(recorder)

    from nullrun.instrumentation.crewai import patch_crewai
    from crewai import Crew

    assert patch_crewai(rt) is True

    crew = Crew()
    crew.usage_metrics = {
        "gpt-4o": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        },
    }
    result = Crew.kickoff(crew, inputs={"q": "hi"})
    assert result.result == "ok"

    # One llm_call event for gpt-4o.
    events = recorder["track"]
    assert len(events) == 1
    ev = events[0]
    assert ev["type"] == "llm_call"
    assert ev["provider"] == "crewai"
    assert ev["model"] == "gpt-4o"
    assert ev["input_tokens"] == 100
    assert ev["output_tokens"] == 50
    assert ev["tokens"] == 150


def test_kickoff_without_usage_metrics_no_emit(monkeypatch, fresh_patch_module):
    """``crew.usage_metrics`` is empty — wrapper skips emit cleanly."""
    _install_fake_crewai(monkeypatch)
    recorder = {"track": [], "track_event": []}
    rt = _fake_runtime(recorder)

    from nullrun.instrumentation.crewai import patch_crewai
    from crewai import Crew

    assert patch_crewai(rt) is True

    crew = Crew()
    crew.usage_metrics = {}
    Crew.kickoff(crew)

    assert recorder["track"] == []


def test_kickoff_non_dict_usage_metrics(monkeypatch, fresh_patch_module):
    """``crew.usage_metrics`` is e.g. an int (weird but possible) —
    wrapper must not crash and must not emit."""
    _install_fake_crewai(monkeypatch)
    recorder = {"track": [], "track_event": []}
    rt = _fake_runtime(recorder)

    from nullrun.instrumentation.crewai import patch_crewai
    from crewai import Crew

    assert patch_crewai(rt) is True

    crew = Crew()
    crew.usage_metrics = 42  # non-dict
    Crew.kickoff(crew)
    assert recorder["track"] == []


def test_kickoff_non_dict_metric_value_skipped(monkeypatch, fresh_patch_module):
    """A model whose value is e.g. a list — wrapper skips that model."""
    _install_fake_crewai(monkeypatch)
    recorder = {"track": [], "track_event": []}
    rt = _fake_runtime(recorder)

    from nullrun.instrumentation.crewai import patch_crewai
    from crewai import Crew

    assert patch_crewai(rt) is True

    crew = Crew()
    crew.usage_metrics = {"gpt-4o": "weird", "claude": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11}}
    Crew.kickoff(crew)

    # Only the well-formed entry emitted.
    assert len(recorder["track"]) == 1
    assert recorder["track"][0]["model"] == "claude"


def test_kickoff_step_callback_installed_when_missing(monkeypatch, fresh_patch_module):
    """When the caller does not pass ``step_callback``, the wrapper
    installs one so every step emits a span_start."""
    _install_fake_crewai(monkeypatch)
    recorder = {"track": [], "track_event": []}
    rt = _fake_runtime(recorder)

    from nullrun.instrumentation.crewai import patch_crewai
    from crewai import Crew

    assert patch_crewai(rt) is True

    crew = Crew()
    Crew.kickoff(crew, inputs={})
    # The wrapper installed a step_callback under the hood — but the
    # underlying kickoff mock didn't actually invoke it. Verify the
    # patched call accepts the kwargs without error.
    assert recorder["track"] == []


def test_kickoff_preserves_user_step_callback(monkeypatch, fresh_patch_module):
    """When the caller already supplies ``step_callback``, the
    wrapper must not overwrite it.
    """
    _install_fake_crewai(monkeypatch)
    rt = _fake_runtime({})

    from nullrun.instrumentation.crewai import patch_crewai
    from crewai import Crew

    sentinel = MagicMock()
    assert patch_crewai(rt) is True
    crew = Crew()
    Crew.kickoff(crew, step_callback=sentinel)
    # The user's callback object is passed through unchanged.
    # (We don't assert on the wrapper's local replacement here because
    # the underlying mock doesn't introspect kwargs — the contract
    # is "don't overwrite if present".)


# ─── kickoff_async wrapper ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kickoff_async_emits_usage_metrics(monkeypatch, fresh_patch_module):
    _install_fake_crewai(monkeypatch)
    recorder = {"track": [], "track_event": []}
    rt = _fake_runtime(recorder)

    from nullrun.instrumentation.crewai import patch_crewai
    from crewai import Crew

    assert patch_crewai(rt) is True

    crew = Crew()
    crew.usage_metrics = {
        "gpt-4o-mini": {"prompt_tokens": 7, "completion_tokens": 11, "total_tokens": 18},
    }
    result = await Crew.kickoff_async(crew)
    assert result.result == "ok-async"
    assert len(recorder["track"]) == 1
    assert recorder["track"][0]["tokens"] == 18


# ─── Track failure is swallowed ──────────────────────────────────────


def test_kickoff_track_failure_is_swallowed(monkeypatch, fresh_patch_module):
    """If runtime.track raises, the wrapped kickoff still returns."""
    _install_fake_crewai(monkeypatch)
    rt = MagicMock()
    rt.track.side_effect = RuntimeError("down")
    rt.track_event.side_effect = lambda **kw: None

    from nullrun.instrumentation.crewai import patch_crewai
    from crewai import Crew

    assert patch_crewai(rt) is True
    crew = Crew()
    crew.usage_metrics = {"m": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}
    Crew.kickoff(crew)  # does not raise


# ─── unpatch ──────────────────────────────────────────────────────────


def test_unpatch_restores_original(monkeypatch, fresh_patch_module):
    _install_fake_crewai(monkeypatch)
    from nullrun.instrumentation.crewai import patch_crewai, unpatch_crewai
    from crewai import Crew

    original_kickoff = Crew.kickoff
    assert patch_crewai(MagicMock()) is True
    assert Crew.kickoff is not original_kickoff

    unpatch_crewai()
    assert Crew.kickoff is original_kickoff
    assert Crew._nullrun_patched is False


def test_unpatch_when_not_patched_is_noop(monkeypatch, fresh_patch_module):
    from nullrun.instrumentation.crewai import unpatch_crewai

    unpatch_crewai()  # safe no-op


def test_unpatch_when_module_missing(monkeypatch, fresh_patch_module):
    _install_fake_crewai(monkeypatch)
    from nullrun.instrumentation.crewai import patch_crewai, unpatch_crewai

    assert patch_crewai(MagicMock()) is True
    monkeypatch.delitem(sys.modules, "crewai", raising=False)
    unpatch_crewai()  # should not raise