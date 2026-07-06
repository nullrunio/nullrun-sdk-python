"""
llama-index auto-instrumentation for NullRun SDK.

Subscribes to the llama-index core event dispatcher (v0.10.20+) and
emits ``llm_call`` events for every chat completion. Token usage is
already captured by the httpx transport hook in ``auto.py`` — this
patch is the safety net for cases where the dispatcher fires without
a corresponding HTTP round-trip (e.g. tests, mock providers).

Mirrors the structure of ``patch_langgraph_compiled`` in
``auto.py:815-900``.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_llama_index_patched = False
_orig_subscriber_handlers: list[tuple[Any, Callable[..., Any]]] = []


def patch_llama_index(runtime: Any) -> bool:
    """Install NullRun subscribers on the llama-index core dispatcher.

    Idempotent. Returns False if ``llama_index.core`` is not importable.
    """
    global _llama_index_patched
    if _llama_index_patched:
        return True
    try:
        from llama_index.core.instrumentation import get_dispatcher
        from llama_index.core.instrumentation.events.llm import LLMChatEndEvent
        from llama_index.core.instrumentation.events.tool import FunctionCallEvent
    except ImportError:
        logger.debug("llama-index not installed; auto-patch skipped")
        return False

    dispatcher = get_dispatcher(name="nullrun")

    def on_chat_end(event: Any) -> None:
        try:
            usage = getattr(event.response, "raw", None) or {}
            if hasattr(usage, "usage"):
                usage = usage.usage or {}
            prompt = int(usage.get("prompt_tokens", 0) or 0)
            completion = int(usage.get("completion_tokens", 0) or 0)
            total = int(usage.get("total_tokens", 0) or 0) or (prompt + completion)
            if not (prompt or completion or total):
                return
            # Audit 2026-06-28 (SDK↔backend wire): model used to come
            # only from ``event.response.model`` with a bare ``None``
            # fallback — mock providers and some adapters don't
            # populate ``.model`` on ChatResponse, which sent
            # ``model=None`` to the backend → ``unwrap_or("default")``
            # → fallback warning. Walk the same chain
            # ``_extract_model_from_response`` uses in langgraph.py:
            # 1. ``event.response.model`` — llama-index ChatResponse
            # 2. ``event.response.raw.model`` — OpenAI-style nested
            # response object on the raw attribute
            # 3. ``usage.model`` — provider dict sometimes carries it
            # Empty / None values are dropped — only set ``model`` on
            # the event when we have a real string.
            response = event.response
            model = (
                getattr(response, "model", None)
                or getattr(getattr(response, "raw", None), "model", None)
                or (usage.get("model") if isinstance(usage, dict) else None)
            )
            event_dict: dict[str, Any] = {
                "type": "llm_call",
                "provider": "llama_index",
                "tokens": total,
                "input_tokens": prompt,
                "output_tokens": completion,
                "has_usage": True,
            }
            if model:
                event_dict["model"] = model
            runtime.track(event_dict)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("llama_index on_chat_end: %s", e)

    def on_function_call(event: Any) -> None:
        try:
            tool = getattr(event, "tool", None)
            tool_name = getattr(tool, "name", None) or "tool"
            runtime.track(
                {
                    "type": "tool_call",
                    "tool_name": tool_name,
                }
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("llama_index on_function_call: %s", e)

    dispatcher.add_event_handler(LLMChatEndEvent, on_chat_end)
    dispatcher.add_event_handler(FunctionCallEvent, on_function_call)
    _orig_subscriber_handlers.extend(
        [
            (LLMChatEndEvent, on_chat_end),
            (FunctionCallEvent, on_function_call),
        ]
    )
    _llama_index_patched = True
    logger.info("llama-index auto-instrumentation installed")
    return True


def unpatch_llama_index() -> None:
    """Detach our subscribers. Test-only. Idempotent."""
    global _llama_index_patched
    if not _llama_index_patched:
        return
    try:
        from llama_index.core.instrumentation import get_dispatcher

        dispatcher = get_dispatcher(name="nullrun")
        for event_cls, handler in _orig_subscriber_handlers:
            try:
                dispatcher.remove_event_handler(event_cls, handler)
            except Exception:  # pragma: no cover
                pass
    except ImportError:
        pass
    _orig_subscriber_handlers.clear()
    _llama_index_patched = False