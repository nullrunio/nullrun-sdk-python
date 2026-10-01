"""`@tool` / `@protect` ordering on the REAL `langchain_core.tools.tool`.

Priority 1 for DEF-MP-TS12-ENF-01: the user-facing surface is one
`@protect`. This file pins the two properties that decide whether that
claim is true in practice for a LangChain agent, neither of which any
existing test asserted:

1. `@tool` OUTSIDE `@protect` is the only ordering that produces a
   `StructuredTool` LangChain's agent loop can bind. `@protect` outside
   `@tool` collapses the tool to a plain function, which is invisible
   to the loop — so the "correct" ordering is not a style preference,
   it is the difference between enforcement and no enforcement.
2. A refusal raised inside the protected body escapes a real
   `StructuredTool.invoke` intact, and cannot be converted into
   model-visible text by `handle_tool_error=True`.

Both were established by probe on 2026-10-01 against
`langchain-core` 0.3.86 before being written here; the probes found the
property, this file keeps it.

`langchain_core` is an optional dependency, so every test is skipped
rather than failed when it is absent — the same rule
`tests/test_langchain_enforcement.py` already follows.
"""

from __future__ import annotations

import pytest

pytest.importorskip("langchain_core")

from langchain_core.tools import StructuredTool, tool  # noqa: E402

from nullrun import decorators  # noqa: E402
from nullrun.breaker.exceptions import NullRunBudgetError  # noqa: E402

REFUSAL = "NR-B002"


class _Deny:
    """Every gate refuses.

    The point of these tests is which exception *escapes the tool*, so
    the stub raises the real enforcement type rather than returning a
    block dict — a returned decision takes a different branch inside
    `_protect_body` and would not exercise the boundary under test.
    """

    def _bump_protect_count(self):
        return None

    def check_control_plane(self, *a, **k):
        return None

    def check_workflow_budget(self, *a, **k):
        raise NullRunBudgetError(
            "no budget", reason="budget exhausted", error_code=REFUSAL
        )

    def execute(self, *a, **k):
        raise NullRunBudgetError(
            "no budget", reason="budget exhausted", error_code=REFUSAL
        )

    def track_tool(self, *a, **k):
        return None

    def _resolve_workflow_id(self, *a, **k):
        return "w-test"

    def _emit_sdk_error(self, *a, **k):
        return None

    def sensitive_fail_open_enabled(self, *a, **k):
        return False


class _Allow(_Deny):
    def check_workflow_budget(self, *a, **k):
        return None

    def execute(self, *a, **k):
        return {"decision": "allow", "decision_source": "gateway"}


@pytest.fixture
def deny(monkeypatch):
    stub = _Deny()
    monkeypatch.setattr(decorators, "_get_or_create_runtime", lambda: stub, raising=True)
    return stub


@pytest.fixture
def allow(monkeypatch):
    stub = _Allow()
    monkeypatch.setattr(decorators, "_get_or_create_runtime", lambda: stub, raising=True)
    return stub


class TestToolOutsideProtectIsBindable:
    """The ordering a user is expected to write."""

    def test_yields_a_structured_tool(self, allow):
        ran = []

        @tool
        @decorators.protect
        def a_search(query: str) -> str:
            """Search."""
            ran.append("A")
            return "body-A"

        assert isinstance(a_search, StructuredTool), (
            "an agent loop can only bind a StructuredTool; if @protect is "
            "the OUTER decorator the tool disappears from the loop entirely"
        )
        assert a_search.name == "a_search"
        assert a_search.description == "Search."

    def test_invokes_the_protected_body(self, allow):
        ran = []

        @tool
        @decorators.protect
        def a_search(query: str) -> str:
            """Search."""
            ran.append("A")
            return "body-A"

        assert a_search.invoke({"query": "x"}) == "body-A"
        assert ran == ["A"]


class TestProtectOutsideToolStillEnforces:
    """`@protect` OUTSIDE `@tool` — previously a silent enforcement loss.

    Before 2026-10-01 this collapsed the tool to a plain function:
    `functools.wraps`-based wrapping cannot preserve `.invoke`, so the
    agent loop could not bind the tool and the refusal never fired. The
    tool now has its `func`/`coroutine` wrapped in place and the same
    object is returned, so ordering does not decide whether the agent is
    gated.

    These are the counter-tests for the class above: together they say
    "both orders enforce" rather than "one order is documented".
    """

    def test_stays_a_structured_tool(self, allow):
        @decorators.protect
        @tool
        def b_search(query: str) -> str:
            """Search."""
            return "body-B"

        assert isinstance(b_search, StructuredTool), (
            "@protect must return the tool unchanged; wrapping the object "
            "itself silently removes it from the agent loop"
        )
        assert b_search.name == "b_search"
        assert b_search.description == "Search."

    def test_preserves_the_metadata_an_agent_needs(self, allow):
        @decorators.protect
        @tool
        def b_search(query: str) -> str:
            """Search."""
            return "body-B"

        assert b_search.args_schema is not None
        assert "query" in b_search.args_schema.model_fields

    def test_refusal_propagates_through_this_order_too(self, deny):
        ran = []

        @decorators.protect
        @tool
        def b_search(query: str) -> str:
            """Search."""
            ran.append("B")
            return "body-B"

        with pytest.raises(NullRunBudgetError) as exc:
            b_search.invoke({"query": "x"})
        assert exc.value.error_code == REFUSAL
        assert ran == [], "the body ran despite a refusal"

    def test_allow_path_still_runs(self, allow):
        @decorators.protect
        @tool
        def b_search(query: str) -> str:
            """Search."""
            return "body-B"

        assert b_search.invoke({"query": "x"}) == "body-B"

    def test_async_tool_in_this_order_also_enforces(self, deny):
        import asyncio

        ran = []

        @decorators.protect
        @tool
        async def b_search(query: str) -> str:
            """Search."""
            ran.append("B")
            return "body-B"

        assert isinstance(b_search, StructuredTool)
        with pytest.raises(NullRunBudgetError):
            asyncio.run(b_search.ainvoke({"query": "x"}))
        assert ran == []

    def test_converts_to_the_wire_schema_an_agent_sends(self, allow):
        """What an agent loop actually does with a bound tool.

        `bind_tools` is `BaseChatModel`-specific and the fake chat model
        in `langchain-core` does not implement it, so this asserts the
        step underneath: `convert_to_openai_tool` is what every
        `bind_tools` implementation calls to build the request body, and
        it is what fails first when an object has lost `.name` /
        `.args_schema` / `.description`.
        """
        from langchain_core.utils.function_calling import convert_to_openai_tool

        @decorators.protect
        @tool
        def b_search(query: str) -> str:
            """Search."""
            return "body-B"

        # Assert the type BEFORE converting. Without the in-place wrap the
        # object is a plain function, and `convert_to_openai_tool` fails on
        # it with an opaque `NameError: name 'Annotated' is not defined`
        # from resolving annotations copied by functools.wrwraps -- a real
        # failure, but one that reads as a langchain bug rather than as
        # "this is no longer a tool".
        assert isinstance(b_search, StructuredTool), (
            "@protect returned a plain function; the tool is gone from "
            "the agent loop and enforcement is silently disabled"
        )

        schema = convert_to_openai_tool(b_search)
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "b_search"
        assert schema["function"]["description"] == "Search."
        assert "query" in schema["function"]["parameters"]["properties"]


class TestRefusalEscapesTheTool:
    def test_refusal_propagates_out_of_invoke(self, deny):
        ran = []

        @tool
        @decorators.protect
        def a_search(query: str) -> str:
            """Search."""
            ran.append("A")
            return "body-A"

        with pytest.raises(NullRunBudgetError) as exc:
            a_search.invoke({"query": "x"})
        assert exc.value.error_code == REFUSAL
        assert ran == [], "the body ran despite a refusal"

    def test_handle_tool_error_cannot_turn_a_refusal_into_model_text(self, deny):
        """`handle_tool_error` is LangChain's own "don't crash the loop" flag.

        It only catches `ToolException`. An enforcement refusal is not
        one, so it reaches the bare `except (Exception, KeyboardInterrupt)`
        arm and is re-raised unconditionally. This matters because the
        alternative — a refusal string in the tool message — is exactly
        the DEF-MP-TS12-ENF-01 shape (the agent reads "allowed") in a
        different costume.
        """
        ran = []

        def raw(query: str) -> str:
            ran.append("H")
            return "body-H"

        h_search = StructuredTool.from_function(
            func=decorators.protect(raw),
            name="h_search",
            description="Search.",
            handle_tool_error=True,
        )

        with pytest.raises(NullRunBudgetError):
            h_search.invoke({"query": "x"})
        assert ran == []

    def test_async_refusal_also_escapes_a_structured_tool(self, deny):
        """The async arm is a SEPARATE code path and a separate risk.

        `decorators.protect` builds `async_wrapper` for a coroutine and
        `sync_wrapper` for a plain function; they share nothing but the
        name. The sync arm catches `BaseException` and the async arm
        catches `Exception` (deliberately — its own comment requires
        CancelledError/KeyboardInterrupt to propagate without blocking
        I/O), so "the sync path is safe" says nothing about this one.

        Mutation-verified 2026-10-01: converting the async arm's
        refusal into a returned string leaves the sync tests green and
        turns exactly these two red.
        """
        import asyncio

        ran = []

        @tool
        @decorators.protect
        async def a_search(query: str) -> str:
            """Search."""
            ran.append("A")
            return "body-A"

        with pytest.raises(NullRunBudgetError) as exc:
            asyncio.run(a_search.ainvoke({"query": "x"}))
        assert exc.value.error_code == REFUSAL
        assert ran == [], "the async body ran despite a refusal"

    def test_async_allow_path_runs_the_body(self, allow):
        """Counter-test: the async arm is not simply broken.

        Without this, a mutation that made async raise unconditionally
        would satisfy the refusal test above.
        """
        import asyncio

        ran = []

        @tool
        @decorators.protect
        async def a_search(query: str) -> str:
            """Search."""
            ran.append("A")
            return "body-A"

        assert asyncio.run(a_search.ainvoke({"query": "x"})) == "body-A"
        assert ran == ["A"]

    def test_control_tool_without_protect_still_runs(self, deny):
        """Proves the refusal above came from `@protect`, not from LangChain."""
        ran = []

        @tool
        def c_search(query: str) -> str:
            """Search."""
            ran.append("C")
            return "body-C"

        assert c_search.invoke({"query": "x"}) == "body-C"
        assert ran == ["C"]


class TestProtectNeedsNoInitCall:
    """Lazy resolution: `@protect` with no `init()` must fail LOUD, not open.

    The `reset_runtime` autouse fixture in `conftest.py` clears
    `decorators._runtime` before every test, so this is simply the state
    every test above runs in — except they all install a stub, which
    hides what the resolver does when nothing is installed.

    The real resolver (`decorators._get_or_create_runtime`, `:285`) is
    left untouched here: it falls through to
    `NullRunRuntime.get_instance()`, which reads `NULLRUN_API_KEY` and
    raises when there is none. FIX-4 removed the old
    `except`-and-rebuild fallback precisely so this surfaces at the
    first `@protect` call. Stubbing the resolver to return `None` would
    prove nothing — it produced an `AttributeError` on `None`, not the
    documented fail-loud path.
    """

    def test_unpinned_protect_raises_auth_rather_than_running_the_body(
        self, monkeypatch
    ):
        import nullrun.decorators as d
        from nullrun.breaker.exceptions import NullRunAuthenticationError

        monkeypatch.delenv("NULLRUN_API_KEY", raising=False)
        monkeypatch.delenv("NULLRUN_SECRET_KEY", raising=False)
        monkeypatch.setattr(d, "_runtime", None, raising=True)
        ran = []

        @d.protect
        def body():
            ran.append("ran")
            return "ok"

        with pytest.raises(NullRunAuthenticationError) as exc:
            body()
        assert exc.value.error_code == "NR-A001"
        assert ran == [], "an unenforceable call must not run the body"

    def test_async_protect_also_refuses_without_a_runtime(self, monkeypatch):
        import asyncio

        import nullrun.decorators as d
        from nullrun.breaker.exceptions import NullRunAuthenticationError

        monkeypatch.delenv("NULLRUN_API_KEY", raising=False)
        monkeypatch.delenv("NULLRUN_SECRET_KEY", raising=False)
        monkeypatch.setattr(d, "_runtime", None, raising=True)
        ran = []

        @d.protect
        async def body():
            ran.append("ran")
            return "ok"

        with pytest.raises(NullRunAuthenticationError):
            asyncio.run(body())
        assert ran == []

    def test_the_resolver_is_not_swallowing_the_auth_error(self, monkeypatch):
        """FIX-4's invariant: no `except` between resolver and caller.

        A regression guard on the shape, because the failure it prevents
        is a silent one — the fallback this removed logged "we have a
        runtime" and then crashed later, somewhere unrelated.
        """
        import ast
        import inspect
        import textwrap

        import nullrun.decorators as d

        src = textwrap.dedent(inspect.getsource(d._get_or_create_runtime))
        tree = ast.parse(src)
        fn = tree.body[0]
        assert isinstance(fn, ast.FunctionDef)

        # AST, not substring: the docstring *describes* the removed
        # `try/except`, so any text search finds prose about the very
        # construct this asserts is gone. A `handlers` list is empty iff
        # there is no `except` clause, regardless of formatting.
        handlers = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Try) and n.handlers
        ]
        assert handlers == [], (
            "_get_or_create_runtime must not catch the auth error: FIX-4 "
            "removed this fallback and it must stay removed"
        )
