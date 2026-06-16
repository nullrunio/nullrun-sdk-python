"""
NullRun instrumentation module.

Provides low-level instrumentation primitives for various AI
frameworks. The user-facing "wrap my compiled app" helpers
live in `nullrun.toolbox` (e.g. `nullrun.toolbox.langgraph.wrapper`,
which replaced `nullrun.instrumentation.langgraph.instrument`
in Phase 1 Commit 6).
"""

from nullrun.instrumentation.auto import auto_instrument, is_auto_instrumented
from nullrun.instrumentation.langgraph import NullRunCallback
from nullrun.instrumentation.openai import patch_openai, unpatch_openai

__all__ = [
    "NullRunCallback",
    "patch_openai",
    "unpatch_openai",
    "auto_instrument",
    "is_auto_instrumented",
]
