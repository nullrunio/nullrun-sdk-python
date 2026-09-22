"""
LangGraph toolbox helpers for NullRun.

DEPRECATED for auto-instrumentation use cases.

For typical LangGraph usage, ``nullrun.init_or_die()`` (or just
``@nullrun.protect`` on the agent function) auto-patches
``langgraph.pregel.Pregel`` via
``nullrun.instrumentation.auto.patch_langgraph_compiled`` — the same
callback injection that this ``wrapper()`` performs manually. The
auto-patch is the canonical path; users should NOT need to call
``wrapper(graph)`` themselves.

This ``wrapper()`` remains as an **escape hatch** for three narrow
cases where the auto-patch cannot run or where the user needs
explicit control:

  1. Tests with custom runtimes (the auto-patch binds to the active
     runtime; a test fixture that swaps runtimes mid-flight may need
     the wrapper to attach to the new runtime directly).
  2. Apps where ``Pregel`` is imported BEFORE ``nullrun.init_or_die()``
     AND the import side-effects register a non-Pregel transport
     that the auto-patch cannot reach.
  3. Manual control over which ``NullRunCallback`` instance is
     attached (rare; the default singleton is usually correct).

For the canonical path (90%+ of users), omit this wrapper::

    from nullrun import init_or_die, protect

    init_or_die()

    @protect
    def my_agent(prompt):
        return graph.invoke({"messages": [("user", prompt)]})

If you must use this wrapper explicitly (escape hatch)::

    from nullrun import init_or_die
    from nullrun.toolbox.langgraph import wrapper

    runtime = init_or_die()
    graph = build_my_graph()
    graph = wrapper(graph, runtime=runtime)
    result = graph.invoke({"messages": [("user", "hi")]})

Why this lives in ``toolbox/``, not ``instrumentation/``:
  - ``instrumentation/`` ships the generic, low-level patches
    (httpx, OpenAI v1+ attribute path, LangChain callback class,
    Pregel class-method wrap). These are reusable building blocks
    and run automatically on ``init_or_die()``.
  - ``toolbox/langgraph.py`` ships an opinionated one-call wrapper
    that mutates a specific ``app`` instance in place. It is no
    longer the recommended path for typical usage.

The previous location ``nullrun.instrumentation.langgraph.instrument``
has been removed. Users who imported it should switch to
``nullrun.toolbox.langgraph.wrapper`` (escape hatch only) or rely
on the auto-patch (canonical path).
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

    .. deprecated::
        For typical usage, rely on the auto-patch in
        ``nullrun.init_or_die()`` / ``@nullrun.protect``. This wrapper
        is an escape hatch for the narrow cases documented in the
        module docstring (custom runtime, Pregel imported before init,
        manual callback control).

    Every ``app.invoke(...)`` and ``app.stream(...)`` call gets a
    ``NullRunCallback`` attached so the runtime sees the LLM
    usage for cost accounting and policy enforcement.

    Args:
        app: A compiled LangGraph ``StateGraph`` (anything with
             ``.invoke`` and ``.stream``).
        runtime: Optional ``NullRunRuntime``. Defaults to the
             module-level singleton from ``get_runtime()``.

    Returns:
        The same ``app`` object, with ``.invoke`` and ``.stream``
        wrapped in place. The callback is added to LangChain's
        ``config["callbacks"]`` list per call, so multiple
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
