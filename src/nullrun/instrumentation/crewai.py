"""
crewai auto-instrumentation for NullRun SDK.

Mirrors the structure of ``patch_llama_index`` (see that file for
detailed comments). CrewAI's canonical integration point is the
``step_callback`` / ``task_callback`` parameters on ``Crew``.

Hook: ``Crew.kickoff`` and ``Crew.kickoff_async`` are wrapped so a
``step_callback`` and ``task_callback`` are installed on every crew
the user creates (unless they already supplied one). After the
crew completes, ``crew.usage_metrics`` is read once and emitted as
an ``llm_call`` event with the aggregated prompt / completion
token totals. Token usage for httpx-routed providers is already
captured by the auto-patch in ``auto.py``.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_crewai_patched = False
_orig_kickoff: Callable[..., Any] | None = None
_orig_kickoff_async: Callable[..., Any] | None = None


def _emit_usage_metrics(runtime: Any, crew: Any) -> None:
    """Read ``crew.usage_metrics`` post-run and emit one llm_call per model."""
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

    global _orig_kickoff, _orig_kickoff_async
    _orig_kickoff = Crew.kickoff
    _orig_kickoff_async = getattr(Crew, "kickoff_async", None)

    def _wrap_kickoff(self: Any, inputs: Any = None, **kwargs: Any) -> Any:
        # Install step_callback if absent.
        if "step_callback" not in kwargs:
            def step_cb(step: Any) -> None:
                # Steps carry tool/agent metadata; emit a span_start.
                try:
                    runtime.track_event(
                        event_type="span_start",
                        fn_name="crewai_step",
                        span_kind="agent",
                    )
                except Exception:  # pragma: no cover
                    pass

            kwargs["step_callback"] = step_cb

        result = _orig_kickoff(self, inputs=inputs, **kwargs)
        _emit_usage_metrics(runtime, self)
        return result

    async def _wrap_kickoff_async(self: Any, inputs: Any = None, **kwargs: Any) -> Any:
        if "step_callback" not in kwargs:
            def step_cb(step: Any) -> None:
                try:
                    runtime.track_event(
                        event_type="span_start",
                        fn_name="crewai_step",
                        span_kind="agent",
                    )
                except Exception:  # pragma: no cover
                    pass

            kwargs["step_callback"] = step_cb

        result = await _orig_kickoff_async(self, inputs=inputs, **kwargs)
        _emit_usage_metrics(runtime, self)
        return result

    Crew.kickoff = _wrap_kickoff  # type: ignore[method-assign]
    if _orig_kickoff_async is not None:
        Crew.kickoff_async = _wrap_kickoff_async  # type: ignore[method-assign]
    Crew._nullrun_patched = True  # type: ignore[attr-defined]
    _crewai_patched = True
    logger.info("crewai auto-instrumentation installed")
    return True


def unpatch_crewai() -> None:
    """Detach our Crew.kickoff / kickoff_async wrappers. Test-only."""
    global _crewai_patched
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