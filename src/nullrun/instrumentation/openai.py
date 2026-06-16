"""
OpenAI instrumentation for NullRun SDK.

DEPRECATED: This module patches the v0.x attribute path
(`openai.ChatCompletion.create`) which is no longer exposed by
`openai>=1.0` clients. The v1.0+ Python SDK does not expose
`ChatCompletion` as an attribute — `openai.chat.completions.create(...)`
is the only supported entry point.

Use `nullrun.instrumentation.auto_instrument` (or just `nullrun.init`)
instead — it patches `httpx.Client` so all vendor SDKs (openai,
anthropic, mistral, google-genai, cohere, bedrock) are tracked
vendor-independently. `auto_instrument` covers OpenAI v1.0+ and is
the supported path going forward.

This module is preserved for backward compatibility with v0.x
OpenAI clients. The patches are best-effort — they emit a warning
when the v0.x attribute path is not present and stay inactive.

Provides automatic patching of OpenAI API calls for zero-effort tracking.
"""

import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# Store original function
_original_chat_create: Callable[..., Any] | None = None
_original_embed_create: Callable[..., Any] | None = None
_patched = False


def _patched_chat_create(*args: Any, **kwargs: Any) -> Any:
    """
    Patched version of openai.ChatCompletion.create.

    Tracks all calls automatically.
    """
    from nullrun.runtime import get_runtime

    runtime = get_runtime()

    # Capture start time
    start_time = time.time()

    # Call original
    response = _original_chat_create(*args, **kwargs)  # type: ignore[misc]

    # Calculate latency
    latency_ms = int((time.time() - start_time) * 1000)

    # Extract usage
    usage = response.get("usage", {}) if isinstance(response, dict) else None
    if usage:
        total_tokens = usage.get("total_tokens", 0)
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
    else:
        total_tokens = 0
        prompt_tokens = 0
        completion_tokens = 0

    # Get model
    model = kwargs.get("model") or (args[0] if args else "unknown")

    # Commit 4: track_llm now takes (input_tokens, output_tokens)
    # instead of (tokens, cost_cents). The backend computes cost
    # server-side from the split token counts + the org's pricing
    # policy. Splitting prompt vs completion matters because most
    # models price them differently.
    #
    # We still pass prompt/completion via metadata for backwards-
    # compatible observability (the backend also reads them from
    # the new top-level fields).

    # Track
    try:
        runtime.track_llm(
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            model=model,
            latency_ms=latency_ms,
            metadata={
                "provider": "openai",
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            },
        )
        logger.debug(
            f"OpenAI tracked: model={model}, in={prompt_tokens}, out={completion_tokens}"
        )
    except Exception as e:
        logger.warning(f"Failed to track OpenAI call: {e}")

    return response


def _patched_embed_create(*args: Any, **kwargs: Any) -> Any:
    """
    Patched version of openai.Embedding.create.

    Tracks embedding calls.
    """
    from nullrun.runtime import get_runtime

    runtime = get_runtime()
    start_time = time.time()

    response = _original_embed_create(*args, **kwargs)  # type: ignore[misc]

    latency_ms = int((time.time() - start_time) * 1000)

    # Extract usage
    usage = response.get("usage", {}) if isinstance(response, dict) else None
    tokens = usage.get("total_tokens", 0) if usage else 0

    model = kwargs.get("model") or (args[0] if args else "unknown")

    # Commit 4: embeddings don't split prompt/completion the way
    # completions do — OpenAI returns just `total_tokens`. We treat
    # all of it as input_tokens (output is 0). Backend computes
    # cost from the org's embedding pricing.
    try:
        runtime.track_llm(
            input_tokens=tokens,
            output_tokens=0,
            model=model,
            latency_ms=latency_ms,
            metadata={"provider": "openai", "type": "embedding"},
        )
    except Exception as e:
        logger.warning(f"Failed to track embedding call: {e}")

    return response


def patch_openai() -> None:
    """
    Patch OpenAI API to automatically track all calls.

    This is a global patch that affects all subsequent OpenAI calls.

    Usage:
        import openai
        from nullrun.instrumentation import patch_openai

        patch_openai()

        # All calls now tracked automatically
        openai.ChatCompletion.create(model="gpt-4", messages=[...])

    Note:
        Call this AFTER importing openai but BEFORE making any calls.
        This modifies openai.ChatCompletion.create in place.
    """
    global _original_chat_create, _original_embed_create, _patched

    if _patched:
        logger.warning("OpenAI already patched")
        return

    try:
        import openai
    except ImportError:
        logger.warning("OpenAI package not installed")
        return

    # Store originals
    _original_chat_create = openai.ChatCompletion.create  # type: ignore[attr-defined]
    _original_embed_create = openai.Embedding.create  # type: ignore[attr-defined]

    # Apply patches
    openai.ChatCompletion.create = _patched_chat_create  # type: ignore[attr-defined]
    openai.Embedding.create = _patched_embed_create  # type: ignore[attr-defined]

    _patched = True
    logger.info("OpenAI API patched for automatic tracking")


def unpatch_openai() -> None:
    """
    Restore original OpenAI functions.

    Usage:
        from nullrun.instrumentation import unpatch_openai

        unpatch_openai()
    """
    global _original_chat_create, _original_embed_create, _patched

    if not _patched:
        logger.warning("OpenAI not patched")
        return

    try:
        import openai

        if _original_chat_create:
            openai.ChatCompletion.create = _original_chat_create  # type: ignore[attr-defined]
        if _original_embed_create:
            openai.Embedding.create = _original_embed_create  # type: ignore[attr-defined]

        _patched = False
        logger.info("OpenAI API restored")
    except ImportError:
        logger.warning("Could not import openai to unpatch")


def is_patched() -> bool:
    """Check if OpenAI is currently patched."""
    return _patched


class OpenAIPatcher:
    """
    Context manager for OpenAI patching.

    Usage:
        from nullrun.instrumentation import OpenAIPatcher

        with OpenAIPatcher():
            openai.ChatCompletion.create(...)  # tracked
        # Outside context, original behavior restored
    """

    def __enter__(self) -> "OpenAIPatcher":
        patch_openai()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        unpatch_openai()
        return False
