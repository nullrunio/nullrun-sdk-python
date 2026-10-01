"""tests/test_langchain_enforcement.py — a real gate decision must not
degrade into a fail-OPEN transport error on the LangChain callback path.

Audit 2026-09-30, following DEF-MP-TS12-ENF-01 (RUN_ID 20260929T1338).

The defect
----------
``NullRunCallback.on_llm_start`` wrapped ``check_workflow_budget()`` in
``except BaseException`` and logged at ``debug``. That conflated two
categorically different outcomes:

* **transport failure** — the gate could not be reached. ADR-008's
  fail-OPEN policy applies, and is deliberate: a dead backend must not
  freeze the agent, and ``/track`` reconciles the cost afterwards.
* **a real gate decision** — budget exhausted, workflow KILL/PAUSE.
  The gate was reached and said no. Fail-OPEN does NOT apply here.

Under the broad catch, an exhausted budget became an invisible debug
line and the LLM call proceeded.

Why the callback cannot simply re-raise
---------------------------------------
LangChain **swallows** exceptions raised from a callback handler: it
logs ``Error in <handler> callback`` and continues. Verified against
the installed langchain-core, and asserted below so a future langchain
release that changes this is caught rather than silently relied upon.

So the fix is a handoff: the callback stashes the decision, and the
``@protect`` boundary — which can abort — raises it. This module pins
all three links in that chain: the swallow behaviour, the stash, and
the raise.
"""

from __future__ import annotations

import pytest

from nullrun.breaker.exceptions import (
    NullRunBudgetError,
    WorkflowKilledInterrupt,
    WorkflowPausedException,
)
from nullrun.instrumentation.langgraph import (
    _ENFORCEMENT_EXCEPTIONS,
    drain_deferred_enforcement,
    record_deferred_enforcement,
)

ENFORCEMENT = (
    NullRunBudgetError,
    WorkflowKilledInterrupt,
    WorkflowPausedException,
)


@pytest.fixture(autouse=True)
def _clear_deferred():
    """No test may inherit a stashed decision from another."""
    drain_deferred_enforcement()
    yield
    drain_deferred_enforcement()


class TestFrameworkContract:
    def test_langchain_swallows_callback_exceptions(self):
        """The premise of the whole fix, pinned against real langchain.

        If a future langchain-core propagates callback exceptions, the
        handoff becomes unnecessary (though still correct) — but the
        test must fail loudly so someone re-evaluates the design rather
        than the assumption silently rotting.
        """
        pytest.importorskip("langchain_core")
        from langchain_core.callbacks import BaseCallbackHandler
        from langchain_core.callbacks.manager import CallbackManager

        class _Boom(BaseCallbackHandler):
            def on_llm_start(self, serialized, prompts, **kwargs):
                raise RuntimeError("enforcement block")

        swallowed = True
        try:
            CallbackManager([_Boom()]).on_llm_start({}, ["hi"])
        except RuntimeError:
            swallowed = False
        assert swallowed, (
            "langchain-core now PROPAGATES exceptions from on_llm_start. "
            "The callback handoff in instrumentation/langgraph.py is still "
            "correct, but the rationale comment and the ERROR log are now "
            "over-cautious — revisit, do not silently drift."
        )


def _excepted_types(method) -> list[list[str]]:
    """Every `except` clause in `method`, in source order, as names.

    AST rather than substring search on purpose. A text search for
    ``except BaseException`` also matches the COMMENT that documents
    the pre-fix code — the self-defeating pin this SDK has already
    been bitten by once (see `test_langgraph_optional._BLOCK_AND_PROBE`).
    Only real handler nodes may satisfy a source pin.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
    found: list[list[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        t = node.type
        if isinstance(t, ast.Name):
            found.append([t.id])
        elif isinstance(t, ast.Tuple):
            found.append([e.id for e in t.elts if isinstance(e, ast.Name)])
        else:
            found.append(["<expr>"])
    return found


class TestEnforcementClassification:
    @pytest.mark.parametrize("exc_cls", ENFORCEMENT)
    def test_real_decisions_are_classified_as_enforcement(self, exc_cls):
        assert exc_cls in _ENFORCEMENT_EXCEPTIONS, (
            f"{exc_cls.__name__} is a real gate decision and must be caught "
            "by the enforcement arm BEFORE the broad transport arm. If it "
            "is not, an exhausted budget / KILL becomes a fail-open "
            "transport error."
        )
        assert issubclass(exc_cls, Exception), (
            f"{exc_cls.__name__} must subclass Exception — the transport arm "
            "catches Exception, so a BaseException-only type would escape "
            "the split entirely."
        )

    def test_enforcement_arm_precedes_transport_arm(self):
        """The except-ordering is load-bearing, so pin it in the AST.

        `except _ENFORCEMENT_EXCEPTIONS` must precede the broad
        `except Exception` in `on_llm_start`. Reversed, the broad arm
        wins and every real decision is downgraded to fail-OPEN.
        """
        from nullrun.instrumentation.langgraph import NullRunCallback

        arms = _excepted_types(NullRunCallback.on_llm_start)
        assert ["_ENFORCEMENT_EXCEPTIONS"] in arms, (
            "the enforcement arm must exist in on_llm_start; got "
            f"{arms}"
        )
        assert ["Exception"] in arms, (
            f"the transport arm must still exist; got {arms}"
        )
        assert arms.index(["_ENFORCEMENT_EXCEPTIONS"]) < arms.index(
            ["Exception"]
        ), (
            "the enforcement arm must be listed BEFORE the broad transport "
            f"arm — Python takes the first matching clause, so reversed "
            f"order silently reinstates the defect. Arms: {arms}"
        )

    def test_no_bare_baseexception_catch_remains(self):
        """`except BaseException` is what conflated the two categories."""
        from nullrun.instrumentation.langgraph import NullRunCallback

        arms = _excepted_types(NullRunCallback.on_llm_start)
        assert ["BaseException"] not in arms, (
            "on_llm_start must not catch BaseException: it conflates a "
            f"transport failure (fail-OPEN is correct) with a real gate "
            f"decision (fail-OPEN is a bug). Arms: {arms}"
        )


class TestDeferredHandoff:
    def test_record_then_drain_roundtrip(self):
        exc = NullRunBudgetError(workflow_id="w1", reason="budget exhausted")
        record_deferred_enforcement(exc)
        assert drain_deferred_enforcement() is exc

    def test_drain_clears(self):
        """A decision must fire once, not on every subsequent call."""
        record_deferred_enforcement(
            NullRunBudgetError(workflow_id="w1", reason="r")
        )
        assert drain_deferred_enforcement() is not None
        assert drain_deferred_enforcement() is None

    def test_drain_empty_is_none(self):
        assert drain_deferred_enforcement() is None

    def test_oldest_decision_wins(self):
        """A queue, not a slot — the first block is the one that bit."""
        first = NullRunBudgetError(workflow_id="w1", reason="first")
        second = WorkflowKilledInterrupt(workflow_id="w1", reason="killed")
        record_deferred_enforcement(first)
        record_deferred_enforcement(second)
        assert drain_deferred_enforcement() is first
        assert drain_deferred_enforcement() is second

    def test_isolated_per_thread(self):
        """One chain's block must not abort a concurrent chain.

        LangGraph runs chains in a thread pool; a module-level list
        would cross-contaminate them.
        """
        import threading

        record_deferred_enforcement(
            NullRunBudgetError(workflow_id="w1", reason="main")
        )
        seen: list[object] = []

        def _worker() -> None:
            seen.append(drain_deferred_enforcement())

        t = threading.Thread(target=_worker)
        t.start()
        t.join()
        assert seen == [None], "a worker thread must not see another thread's decision"
        # The main thread's decision survived.
        assert drain_deferred_enforcement() is not None

    def test_record_never_raises(self):
        """A stashing failure must not make enforcement worse."""

        class _Unhashable:
            __hash__ = None  # type: ignore[assignment]

        # Any value at all must be accepted without raising.
        record_deferred_enforcement(_Unhashable())  # type: ignore[arg-type]
        drain_deferred_enforcement()


class _StubRuntime:
    """A runtime whose every gate passes.

    Patched into ``decorators._get_or_create_runtime`` — the function
    ``_protect_body`` actually calls. Patching a name the decorator
    never reads (``get_runtime``) would construct a real
    ``NullRunRuntime``, which authenticates against the network and
    fails with a 401 before the test means anything.
    """

    def _bump_protect_count(self):
        return None

    def check_control_plane(self, *a, **k):
        return None

    def check_workflow_budget(self, *a, **k):
        return None

    def execute(self, *a, **k):
        return {"decision": "allow", "decision_source": "gateway"}

    def track_tool(self, *a, **k):
        return None

    def _resolve_workflow_id(self, *a, **k):
        return "w-test"

    def _emit_sdk_error(self, *a, **k):
        return None

    def sensitive_fail_open_enabled(self, *a, **k):
        """The real runtime refuses this bypass in production.

        The stub keeps the "every gate passes" contract these tests
        assert against, so it reports the flag OFF -- the default for
        an operator who has set nothing. If the decorator ever reads
        the env var directly again, the production-guard tests in
        `test_sensitive_fail_open_guard.py` are what catch it; this
        stub must not paper over that by mirroring the raw read.
        """
        return False


@pytest.fixture
def stub_runtime(monkeypatch):
    """Swap the real (network-authenticating) runtime for a passing stub."""
    from nullrun import decorators

    stub = _StubRuntime()
    monkeypatch.setattr(
        decorators, "_get_or_create_runtime", lambda: stub, raising=True
    )
    return stub


class TestProtectBoundaryRaises:
    def test_protect_raises_deferred_decision(self, stub_runtime):
        """End-to-end: a stashed block aborts the protected body.

        This is the link that makes the fix mean anything — without it
        the callback's stash is write-only and enforcement is lost
        exactly as before.
        """
        from nullrun import decorators

        @decorators.protect
        def _tool() -> str:
            return "BODY_RAN"

        record_deferred_enforcement(
            NullRunBudgetError(workflow_id="w-test", reason="budget exhausted")
        )
        with pytest.raises(NullRunBudgetError):
            _tool()

    def test_no_deferred_decision_lets_body_run(self, stub_runtime):
        """The counter-test: the fix must not block when nothing is owed.

        Guards the over-correction — a boundary that raises
        unconditionally would freeze every agent, which is a worse
        failure than the bug being fixed.
        """
        from nullrun import decorators

        @decorators.protect
        def _tool() -> str:
            return "BODY_RAN"

        assert _tool() == "BODY_RAN"

    def test_decision_is_consumed_not_replayed(self, stub_runtime):
        """A block fires once; the next call is not poisoned by it."""
        from nullrun import decorators

        @decorators.protect
        def _tool() -> str:
            return "BODY_RAN"

        record_deferred_enforcement(
            NullRunBudgetError(workflow_id="w-test", reason="budget exhausted")
        )
        with pytest.raises(NullRunBudgetError):
            _tool()
        # The drain cleared it — recovery is the operator's call
        # (raise the budget, kill the workflow), not something the
        # SDK silently second-guesses.
        assert _tool() == "BODY_RAN"

    def test_async_wrapper_also_raises(self, stub_runtime):
        """The async path is a separate wrapper — it must drain too.

        A fix that only lands in the sync wrapper leaves every asyncio
        agent ungated, which is most real deployments.
        """
        import asyncio

        from nullrun import decorators

        @decorators.protect
        async def _atool() -> str:
            return "BODY_RAN"

        record_deferred_enforcement(
            WorkflowKilledInterrupt(workflow_id="w-test", reason="killed")
        )
        with pytest.raises(WorkflowKilledInterrupt):
            asyncio.run(_atool())

    def test_helper_is_a_noop_without_the_adapter(self, monkeypatch):
        """`_raise_deferred_enforcement` must tolerate a missing adapter.

        `instrumentation.langgraph` is only imported when LangChain is
        in play. The helper's import is lazy for exactly that reason,
        so a non-LangChain consumer must not hit an ImportError at
        every `@protect` call.
        """
        import builtins

        from nullrun import decorators

        real_import = builtins.__import__

        def _blocked(name, *a, **k):
            if name == "nullrun.instrumentation.langgraph":
                raise ImportError("no langchain here")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _blocked)
        # Must not raise.
        decorators._raise_deferred_enforcement("t")
