"""
NullRun instrumentation module.

Provides low-level instrumentation primitives for various AI
frameworks. The user-facing "wrap my compiled app" helpers
live in `nullrun.toolbox` (e.g. `nullrun.toolbox.langgraph.wrapper`,
which replaced `nullrun.instrumentation.langgraph.instrument`
in Phase 1 Commit 6).

The v0.x ``openai.ChatCompletion.create`` patcher was removed
in 0.4.0 — ``openai>=1.0`` does not expose that attribute. All
OpenAI v1.0+ traffic is now tracked vendor-independently by the
httpx transport hook in ``nullrun.instrumentation.auto``.
"""

from nullrun.instrumentation.auto import auto_instrument, is_auto_instrumented
from nullrun.instrumentation.langgraph import NullRunCallback

__all__ = [
    "NullRunCallback",
    "auto_instrument",
    "is_auto_instrumented",
]
