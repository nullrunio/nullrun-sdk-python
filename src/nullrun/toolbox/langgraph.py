"""
LangGraph toolbox helpers for NullRun.

This module is the user-facing entry point for LangGraph
integrations. It is a thin convenience layer that wires the
`NullRunCallback` from `nullrun.instrumentation.langgraph` onto a
LangGraph compiled app so that every `app.invoke(...)` and
`app.stream(...)` call fires the LangChain callback hooks. The
callback extracts `input_tokens` / `output_tokens` from the LLM
response and forwards them to the runtime's `track ` method —
cost is then recomputed by the backend from the org's pricing
policy.

Why this lives in `toolbox/`, not `instrumentation/`:
  - `instrumentation/` ships the generic, low-level patches
    (httpx, OpenAI v1+ attribute path, LangChain callback class).
    These are reusable building blocks.
  - `toolbox/langgraph.py` ships a ready-to-use `wrapper(app)`
    that is a single function call for the most common
    LangGraph case. It is the entry point the user is pointed
    to from the LangGraph integration docs.

The previous location `nullrun.instrumentation.langgraph.instrument`
is removed as of Phase 1 Commit 6. Users who imported it should
switch to `nullrun.toolbox.langgraph.wrapper`.
"""
from __future__ import annotations

import logging
from typing import Any

from nullrun.instrumentation.langgraph import NullRunCallback
from nullrun.runtime import NullRunRuntime, get_runtime

logger = logging.getLogger(__name__)


def wrapper(app: Any, runtime: Any | None = None) -> Any:
    """
    Wrap a compiled LangGraph app with NullRun tracking.

    Every `app.invoke(...)` and `app.stream(...)` call gets a
    `NullRunCallback` attached so the runtime sees the LLM
    usage for cost accounting and policy enforcement.

    Usage:
        from nullrun import init
        from nullrun.toolbox.langgraph import wrapper

        runtime = init 
        graph = build_my_graph 
        graph = wrapper(graph, runtime=runtime)

        result = graph.invoke({"messages": [("user", "hi")]})

    Args:
        app: A compiled LangGraph `StateGraph` (anything with
             `.invoke` and `.stream`).
        runtime: Optional `NullRunRuntime`. Defaults to the
             module-level singleton from `get_runtime `.

    Returns:
        The same `app` object, with `.invoke` and `.stream`
        wrapped in place. The callback is added to LangChain's
        `config["callbacks"]` list per call, so multiple
        wrappers compose without colliding.
    """
    rt: NullRunRuntime = runtime or get_runtime()
    callback = NullRunCallback(runtime=rt)
    original_invoke = getattr(app, "invoke", None)
    original_stream = getattr(app, "stream", None)

    if original_invoke is not None:
        def wrapped_invoke(input: Any, config: Any | None = None, **kwargs: Any) -> Any:
            if config is None:
                config = {}
            if "callbacks" not in config:
                config["callbacks"] = []
            config["callbacks"].append(callback)
            return original_invoke(input, config, **kwargs)
        app.invoke = wrapped_invoke

    if original_stream is not None:
        def wrapped_stream(input: Any, config: Any | None = None, **kwargs: Any) -> Any:
            if config is None:
                config = {}
            if "callbacks" not in config:
                config["callbacks"] = []
            config["callbacks"].append(callback)
            return original_stream(input, config, **kwargs)
        app.stream = wrapped_stream

    logger.info("LangGraph app wrapped with NullRun tracking")
    return app
