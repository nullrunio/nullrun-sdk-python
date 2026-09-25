"""
LangGraph toolbox helpers for NullRun.

The previous ``wrapper(app, runtime=None)`` escape hatch was removed —
``nullrun.init_or_die()`` (or ``@nullrun.protect`` on the agent
function) auto-patches ``langgraph.pregel.Pregel`` via
``nullrun.instrumentation.auto.patch_langgraph_compiled``, which
covers every supported LangGraph invocation path.

Migrate to the auto-patch::

    from nullrun import init_or_die, protect

    init_or_die()

    @protect
    def my_agent(prompt):
        return graph.invoke({"messages": [("user", prompt)]})
"""
