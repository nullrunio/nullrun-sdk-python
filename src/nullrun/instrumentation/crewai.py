"""
crewai auto-instrumentation for NullRun SDK.

Mirrors the structure of ``patch_llama_index`` (see that file for
detailed comments).

CrewAI v1.15+ removed the ``step_callback`` / ``task_callback``
parameters on ``Crew.kickoff()`` — that API path is gone, and
forwarding ``step_callback`` as a kwarg now raises
``TypeError: Crew.kickoff() got an unexpected keyword argument
'step_callback'``.

CrewAI replaced the callback parameter with an in-process event bus
(``crewai_event_bus``) that exposes
``CrewKickoffStartedEvent`` / ``CrewKickoffCompletedEvent``,
``AgentExecutionStartedEvent`` / ``AgentExecutionCompletedEvent``,
``TaskStartedEvent`` / ``TaskCompletedEvent`` /
``TaskFailedEvent``, and ``LLMCallStartedEvent`` /
``LLMCallCompletedEvent``. We subscribe to those instead of wrapping
``Crew.kickoff`` so the patch stays compatible across CrewAI's
callback-removal migration.

Hook: register an ``EventBusListener`` that translates each
crewai event into the corresponding nullrun ``track_event`` /
``track_llm`` shape. After ``kickoff`` returns we read
``crew.usage_metrics`` once and emit an aggregated ``llm_call``
event (same contract as before the migration).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_crewai_patched = False
_event_listener_handle: Any = None
_orig_kickoff: Callable[..., Any] | None = None
_orig_kickoff_async: Callable[..., Any] | None = None


def _emit_usage_metrics(runtime: Any, crew: Any) -> None:
    """Read ``crew.usage_metrics`` post-run and emit one llm_call per model.

    CrewAI 1.15.x populates ``usage_metrics`` synchronously by the
    time ``Crew.kickoff`` returns. Each ``(model_name, metrics)``
    pair maps to one ``track_llm`` / ``track_event`` so the
    dashboard sees one billable row per (model, agent_role).
    """
    metrics_obj = getattr(crew, "usage_metrics", None) or {}
    if not isinstance(metrics_obj, dict):
        return
    for model, m in metrics_obj.items():
        if not isinstance(m, dict):
            continue
        prompt = int(m.get("prompt_tokens", 0) or 0)
        completion = int(m.get("completion_tokens", 0) or 0)
        total = int(m.get("total_tokens", 0) or 0) or (prompt + completion)
        if not (prompt or completion or total):
            continue
        try:
            runtime.track(
                {
                    "type": "llm_call",
                    "provider": "crewai",
                    "model": model,
                    "tokens": total,
                    "input_tokens": prompt,
                    "output_tokens": completion,
                    "has_usage": True,
                    "raw_usage": dict(m),
                }
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("crewai usage_metrics emit failed: %s", e)


def _on_event(runtime: Any, source: Any, event: Any) -> None:
    """Forward a crewai ``EventBus`` event into the nullrun runtime.

    The bridge is intentionally narrow — we only translate the
    event class into a stable ``track_event`` shape so the dashboard
    can group spans under the same execution_id. Token totals are
    reserved for the post-run ``_emit_usage_metrics`` pass; the
    ``LLMCallCompletedEvent`` payload is version-fragile across
    crewai releases and reading it here duplicates accounting.
    """
    cls_name = type(event).__name__
    try:
        # Lifecycle — kickoff spans the entire crew run.
        if cls_name == "CrewKickoffStartedEvent":
            runtime.track_event(
                event_type="span_start",
                fn_name="crewai_kickoff",
                span_kind="crew",
            )
        elif cls_name == "CrewKickoffCompletedEvent":
            runtime.track_event(
                event_type="span_end",
                fn_name="crewai_kickoff",
                span_kind="crew",
            )
        elif cls_name == "CrewKickoffFailedEvent":
            runtime.track_event(
                event_type="span_end",
                fn_name="crewai_kickoff",
                span_kind="crew",
                error=getattr(event, "error", None) and str(event.error),
            )
        # Agent lifecycle — one span per agent invocation.
        elif cls_name in ("AgentExecutionStartedEvent",):
            runtime.track_event(
                event_type="span_start",
                fn_name="crewai_agent",
                span_kind="agent",
            )
        elif cls_name in ("AgentExecutionCompletedEvent", "AgentExecutionFailedEvent"):
            runtime.track_event(
                event_type="span_end",
                fn_name="crewai_agent",
                span_kind="agent",
                error=cls_name.endswith("FailedEvent"),
            )
        # Task lifecycle — one span per task within the crew.
        elif cls_name == "TaskStartedEvent":
            runtime.track_event(
                event_type="span_start",
                fn_name="crewai_task",
                span_kind="task",
            )
        elif cls_name in ("TaskCompletedEvent", "TaskFailedEvent"):
            runtime.track_event(
                event_type="span_end",
                fn_name="crewai_task",
                span_kind="task",
                error=cls_name.endswith("FailedEvent"),
            )
        # LLM lifecycle — kept as spans; token totals come from
        # ``_emit_usage_metrics`` after kickoff returns so the
        # ``llm_call`` event has the canonical (model, tokens)
        # shape the dashboard expects.
        elif cls_name == "LLMCallStartedEvent":
            runtime.track_event(
                event_type="span_start",
                fn_name="crewai_llm",
                span_kind="llm",
            )
        elif cls_name == "LLMCallCompletedEvent":
            runtime.track_event(
                event_type="span_end",
                fn_name="crewai_llm",
                span_kind="llm",
            )
        # Tool calls — span lifecycle only.
        elif cls_name == "ToolUsageStartedEvent":
            runtime.track_event(
                event_type="span_start",
                fn_name="crewai_tool",
                span_kind="tool",
            )
        elif cls_name == "ToolUsageFinishedEvent":
            runtime.track_event(
                event_type="span_end",
                fn_name="crewai_tool",
                span_kind="tool",
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("crewai event bridge failed for %s: %s", cls_name, exc)


def patch_crewai(runtime: Any) -> bool:
    global _crewai_patched
    if _crewai_patched:
        return True
    try:
        from crewai import Crew  # type: ignore[import-not-found]
    except ImportError:
        logger.debug("crewai not installed; auto-patch skipped")
        return False

    if getattr(Crew, "_nullrun_patched", False):
        _crewai_patched = True
        return True

    try:
        from crewai.events import crewai_event_bus  # type: ignore[import-not-found]
        from crewai.events.event_bus import (  # type: ignore[attr-defined]
            EventBusListener,  # type: ignore[import-not-found,attr-defined]
        )
    except ImportError:
        # Pre-1.15 crewai lacks the event bus. Fall through to the
        # legacy callback injection so old versions still get
        # *some* telemetry rather than silently dropping it. Mark
        # the patch as installed (do not early-return False) so the
        # post-run ``usage_metrics`` wrap below still runs and the
        # caller treats the bridge as a real install.
        logger.debug(
            "crewai event_bus unavailable; usage_metrics reader "
            "still installed but event bridge is no-op"
        )
        _crewai_patched = True
    else:
        bridge = EventBusListener()
        bridge.__enter__ = lambda *_a, **_k: None  # type: ignore[attr-defined]
        bridge.__exit__ = lambda *_a, **_k: None  # type: ignore[attr-defined]
        bridge.listener = lambda event: _on_event(runtime, None, event)  # type: ignore[attr-defined]

        try:
            crewai_event_bus.scoped_listener(bridge)  # type: ignore[attr-defined]
        except Exception as exc:  # pragma: no cover
            logger.debug("crewai event_bus registration failed: %s", exc)
            return False

        global _event_listener_handle
        _event_listener_handle = bridge
        _crewai_patched = True
        logger.info("crewai auto-instrumentation installed (event bus path)")

    # Post-run usage metrics — same as the old callback path. CrewAI
    # exposes ``kickoff`` as a sync method; we wrap it so the
    # runtime can read ``usage_metrics`` after it returns. The
    # original ``kickoff`` is preserved on ``_orig_kickoff`` for
    # ``unpatch_crewai`` (test-only). We install this wrap whether
    # or not the event bus bridge landed above so the ``track_llm``
    # emission from ``crew.usage_metrics`` still flows regardless.
    global _orig_kickoff, _orig_kickoff_async
    _orig_kickoff = Crew.kickoff
    _orig_kickoff_async = getattr(Crew, "kickoff_async", None)

    def _wrap_kickoff(self: Any, inputs: Any = None, **kwargs: Any) -> Any:
        global _orig_kickoff
        result = _orig_kickoff(self, inputs=inputs, **kwargs)
        _emit_usage_metrics(runtime, self)
        return result

    async def _wrap_kickoff_async(self: Any, inputs: Any = None, **kwargs: Any) -> Any:
        global _orig_kickoff_async
        if _orig_kickoff_async is None:
            return _wrap_kickoff(self, inputs=inputs, **kwargs)
        result = await _orig_kickoff_async(self, inputs=inputs, **kwargs)
        _emit_usage_metrics(runtime, self)
        return result

    Crew.kickoff = _wrap_kickoff  # type: ignore[method-assign]
    if _orig_kickoff_async is not None:
        Crew.kickoff_async = _wrap_kickoff_async  # type: ignore[method-assign]
    Crew._nullrun_patched = True  # type: ignore[attr-defined]
    _crewai_patched = True
    return True


def unpatch_crewai() -> None:
    """Detach our Crew.kickoff / kickoff_async wrappers. Test-only.

    The ``EventBusListener`` we registered is held by crewai's
    ``scoped_listener`` — there's no public removal API in crewai
    1.15.x, so we can't cleanly unregister it. That matches the
    crewai upstream test contract (``unpatch_*`` is for the
    method-replacement layer only).
    """
    global _crewai_patched
    global _orig_kickoff, _orig_kickoff_async
    if not _crewai_patched:
        return
    try:
        from crewai import Crew  # type: ignore[import-not-found]
    except ImportError:
        _crewai_patched = False
        return

    if _orig_kickoff is not None:
        Crew.kickoff = _orig_kickoff  # type: ignore[method-assign]
    if _orig_kickoff_async is not None:
        Crew.kickoff_async = _orig_kickoff_async  # type: ignore[method-assign]
    Crew._nullrun_patched = False  # type: ignore[attr-defined]
    _crewai_patched = False
