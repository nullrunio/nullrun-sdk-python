"""
autogen auto-instrumentation for NullRun SDK.

Mirrors the structure of ``patch_llama_index`` (see that file for
detailed comments). Two integration points:

1. ``BaseChatAgent.on_messages`` (from autogen_agentchat.agents) —
   wrapped to push a tracing span on entry / pop on exit. This
   covers the agent lifecycle regardless of which LLM client the
   user chose.

2. ``OpenAIChatCompletionClient.create`` (from
   autogen_ext.models.openai) — wrapped to capture streaming-safe
   usage. autogen does not always use httpx (some clients hit
   gRPC), so we cannot rely on the httpx transport hook.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_autogen_patched = False
_orig_on_messages: Callable[..., Any] | None = None
_orig_openai_create: Callable[..., Any] | None = None


def patch_autogen(runtime: Any) -> bool:
    global _autogen_patched
    if _autogen_patched:
        return True
    try:
        from autogen_agentchat.agents import BaseChatAgent  # type: ignore[import-not-found]
    except ImportError:
        logger.debug("autogen not installed; auto-patch skipped")
        return False

    if getattr(BaseChatAgent, "_nullrun_patched", False):
        _autogen_patched = True
        return True

    global _orig_on_messages
    _orig_on_messages = BaseChatAgent.on_messages

    def _wrap_on_messages(
        self: Any, messages: Any, cancellation_token: Any = None
    ) -> Any:
        try:
            runtime.track_event(
                event_type="span_start",
                fn_name=getattr(self, "name", "agent") or "agent",
                span_kind="agent",
            )
        except Exception:  # pragma: no cover
            pass

        try:
            resp = _orig_on_messages(self, messages, cancellation_token=cancellation_token)
        except Exception as e:
            try:
                runtime.track_event(
                    event_type="span_end",
                    error=str(e),
                )
            except Exception:  # pragma: no cover
                pass
            raise

        try:
            runtime.track_event(event_type="span_end")
        except Exception:  # pragma: no cover
            pass
        return resp

    BaseChatAgent.on_messages = _wrap_on_messages  # type: ignore[method-assign]

    # Belt-and-suspenders: capture streaming-safe usage off the
    # OpenAI client's CreateResult.usage.
    try:
        from autogen_ext.models.openai import (
            OpenAIChatCompletionClient,  # type: ignore[import-not-found]
        )

        if not getattr(OpenAIChatCompletionClient, "_nullrun_patched", False):
            global _orig_openai_create
            _orig_openai_create = OpenAIChatCompletionClient.create

            def _wrap_create(self: Any, *args: Any, **kwargs: Any) -> Any:
                result = _orig_openai_create(self, *args, **kwargs)
                usage = getattr(result, "usage", None)
                if usage is not None:
                    prompt = int(
                        getattr(usage, "prompt_tokens", 0) or 0
                    )
                    completion = int(
                        getattr(usage, "completion_tokens", 0) or 0
                    )
                    total = int(
                        getattr(usage, "total_tokens", 0) or 0
                    ) or (prompt + completion)
                    if prompt or completion or total:
                        try:
                            runtime.track(
                                {
                                    "type": "llm_call",
                                    "provider": "autogen",
                                    "model": getattr(self, "model", None),
                                    "tokens": total,
                                    "input_tokens": prompt,
                                    "output_tokens": completion,
                                    "has_usage": True,
                                    "raw_usage": {
                                        "prompt_tokens": prompt,
                                        "completion_tokens": completion,
                                    },
                                }
                            )
                        except Exception as e:  # pragma: no cover
                            logger.debug("autogen create emit failed: %s", e)
                return result

            OpenAIChatCompletionClient.create = _wrap_create  # type: ignore[method-assign]
            OpenAIChatCompletionClient._nullrun_patched = True  # type: ignore[attr-defined]
    except ImportError:
        # autogen-agentchat present but autogen-ext not installed —
        # spans still work; usage capture silently skipped.
        pass

    BaseChatAgent._nullrun_patched = True  # type: ignore[attr-defined]
    _autogen_patched = True
    logger.info("autogen auto-instrumentation installed")
    return True


def unpatch_autogen() -> None:
    """Detach our wrappers. Test-only."""
    global _autogen_patched
    if not _autogen_patched:
        return
    try:
        from autogen_agentchat.agents import BaseChatAgent  # type: ignore[import-not-found]
    except ImportError:
        _autogen_patched = False
        return

    if _orig_on_messages is not None:
        BaseChatAgent.on_messages = _orig_on_messages  # type: ignore[method-assign]
    BaseChatAgent._nullrun_patched = False  # type: ignore[attr-defined]

    try:
        from autogen_ext.models.openai import (
            OpenAIChatCompletionClient,  # type: ignore[import-not-found]
        )

        if _orig_openai_create is not None:
            OpenAIChatCompletionClient.create = _orig_openai_create  # type: ignore[method-assign]
        OpenAIChatCompletionClient._nullrun_patched = False  # type: ignore[attr-defined]
    except ImportError:
        pass

    _autogen_patched = False