"""
LangGraph toolbox helpers for NullRun.

``nullrun.init()`` (or ``@nullrun.protect`` on the agent
function) auto-patches ``langgraph.pregel.Pregel`` via
``nullrun.instrumentation.auto.patch_langgraph_compiled``, which
covers every supported LangGraph invocation path.

Use the auto-patch::

    from nullrun import init, protect

    init()

    @protect
    def my_agent(prompt):
        return graph.invoke({"messages": [("user", prompt)]})
"""
