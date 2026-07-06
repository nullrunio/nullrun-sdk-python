"""
NullRun Toolbox.

A curated set of higher-level, ready-to-use integration helpers for
specific AI SDKs and frameworks. The `instrumentation/` package ships
the low-level patches (httpx, OpenAI v1+ attribute path, auto mode)
the `toolbox/` package ships opinionated wrappers that combine
instrumentation + cost enforcement + workflow scoping for the most
common agent runtimes (LangGraph, LlamaIndex, etc.).

The split keeps the curated public surface (`nullrun.init`
`nullrun.protect`, `nullrun.track_*`) discoverable in `dir(nullrun)`
while the framework-specific glue lives one import away at
`nullrun.toolbox.<framework>`.
"""
from __future__ import annotations

__all__ = [
    "langgraph",
]
