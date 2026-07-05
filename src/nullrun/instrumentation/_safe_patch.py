"""
Centralised error handling for auto-instrumentation patchers.

Sprint 2.9 (B47): pre-fix, the auto-instrumentation modules had
25+ instances of ``try/except Exception: pass # pragma: no cover``
scattered across ``auto.py``, ``auto_requests.py``, ``autogen.py``
``crewai.py``, ``llama_index.py``. If a patch failed in production
(typically because the vendored SDK changed a method signature)
the SDK would silently degrade and the user would have no idea
why their costs were no longer being tracked.

The fix: every patch call goes through ``safe_patch `` which:
  - Returns ``True``/``False`` based on patch outcome.
  - Logs at WARNING with the patch name + the actual exception
    (so a SRE can grep for ``Auto-instrumentation patch X failed``
    and see WHY each patch broke).
  - Treats ``ImportError`` (optional dep not installed) as a
    normal, expected event — DEBUG level, not WARNING.

Usage:

    from nullrun.instrumentation._safe_patch import safe_patch

    # In auto_instrument:
    paths = [
        safe_patch("httpx", lambda: patch_httpx(runtime))
        safe_patch("langchain", lambda: patch_langchain_callback(runtime))
...
    ]
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeAlias

logger = logging.getLogger(__name__)

# The result type produced by individual patchers. Most return
# ``bool`` (True if the patch was installed, False if the vendor
# class wasn't found). Some return ``None`` (e.g. if they early-
# exit on a missing optional dependency).
PatchResult: TypeAlias = bool | None


def safe_patch(name: str, patch_fn: Callable[[], PatchResult]) -> bool:
    """Run an auto-instrumentation patch with centralised error handling.

    The 25+ scattered ``try/except`` blocks in the auto-instrumentation
    modules all shared the same contract:
      1. ``ImportError`` means the optional dep isn't installed —
         not actionable, just skip.
      2. Any other ``Exception`` is a real patch failure that the
         operator needs to know about.

    ``safe_patch `` captures both cases and logs at the right
    level, returning a single boolean so the caller can count
    successful patches without dealing with try/except itself.

    Args:
        name: Human-readable patch name (e.g. ``"httpx"``
            ``"langchain_callback"``). Used in the log line so
            an operator can grep their logs.
        patch_fn: Zero-arg callable that performs the patch and
            returns ``True`` on success, ``False`` on benign
            no-op (e.g. vendor class not found), or ``None``
            (treated as success).

    Returns:
        ``True`` if the patch was applied (or had nothing to do)
        ``False`` if the patch failed.
    """
    try:
        result = patch_fn()
        # ``None`` is treated as "patch did its job, nothing more
        # to report" — distinct from ``False`` which means "I tried
        # but the vendor class wasn't installed".
        return bool(result) if result is not None else True
    except ImportError as e:
        # Optional dependency not installed (e.g. ``crewai`` is
        # in extras but the user didn't install it). Normal
        # expected case — DEBUG level so it doesn't pollute
        # production logs.
        logger.debug("Skipped %s patch: optional dependency not installed (%s)", name, e)
        return False
    except Exception as e:
        # Real failure. The vendor SDK probably changed a method
        # signature, or the runtime environment is in an
        # unexpected state. Log at WARNING with enough context
        # to investigate — but don't crash the SDK init.
        logger.warning(
            "Auto-instrumentation patch %s failed: %s: %s. "
            "This is a silent cost-tracking gap — please report "
            "this log line.",
            name,
            type(e).__name__,
            e,
        )
        return False
