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

SDK-14 (reliability audit 2026-11-02)
-------------------------------------
This file used to define its OWN copy of the enforcement family::

    ENFORCEMENT = (NullRunBudgetError, WorkflowKilledInterrupt, WorkflowPausedException)

and assert ``issubclass(case, production_family)`` — one direction
only. Two consequences, both measured:

* **Widening the production family was invisible.** A refusal class
  added to ``ENFORCEMENT_EXCEPTIONS`` and then mishandled by the
  callback (dropped into the broad fail-OPEN arm) changed nothing in
  this suite: the three hand-written cases were still subclasses of
  the family, so every assertion still held. The family is the
  mechanism that keeps a refusal from being laundered into "the gate
  was unreachable, carry on" — and a member nobody drives is a promise
  nobody checks.
* **The copy could rot silently.** Nothing tied the three named
  classes to the refusals the SDK actually raises.

The family now lives in ONE place — the production tuple — and this
file holds only what cannot be derived: the CONSTRUCTORS (every
exception class has its own signature) and the correspondence
INVARIANTS that keep the two in step. A drift in EITHER direction is
RED, never silent.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from nullrun.breaker.categories import NullRunUnclassifiedRefusalError
from nullrun.breaker.exceptions import (
    ENFORCEMENT_EXCEPTIONS,
    NullRunAuthenticationError,
    NullRunBackendError,
    NullRunBudgetError,
    NullRunDecision,
    NullRunGateIndeterminateError,
    NullRunInfrastructureError,
    NullRunMalformedGateResponseError,
    NullRunRateLimitRedisError,
    NullRunToolBlockedError,
    RateLimitError,
    TransportErrorSource,
    WorkflowKilledInterrupt,
    WorkflowPausedException,
)
from nullrun.instrumentation.langgraph import (
    _ENFORCEMENT_EXCEPTIONS,
    _INFRASTRUCTURE_ERRORS,
    _TRANSPORT_ERRORS,
    NullRunCallback,
    drain_deferred_enforcement,
    record_deferred_enforcement,
)

# SDK-14: the CONSTRUCTORS, not the predicate.
#
# A case needs an INSTANCE, and every exception class has its own
# signature — so the list of what to drive cannot be derived from the
# production tuple mechanically. What CAN be derived is the PREDICATE,
# and it is not written here: the production tuple
# (`ENFORCEMENT_EXCEPTIONS`) is the only copy of it, and the
# correspondence invariants in `TestTheFamilyAndTheCasesCorrespond`
# are what keep this registry and that tuple in step. A drift in
# EITHER direction is red.
_CASE_BUILDERS: dict[type[BaseException], Callable[[], BaseException]] = {
    # -- the production family's own members ---------------------------
    NullRunDecision: lambda: NullRunDecision("the gate said no"),
    RateLimitError: lambda: RateLimitError(
        "rate limit exceeded",
        TransportErrorSource.GATEWAY_ERROR,
        "gate",
    ),
    WorkflowKilledInterrupt: lambda: WorkflowKilledInterrupt(
        workflow_id="w-sdk14", reason="killed by the dashboard"
    ),
    NullRunMalformedGateResponseError: lambda: NullRunMalformedGateResponseError(
        "the gate answered with something that is not a decision"
    ),
    NullRunGateIndeterminateError: lambda: NullRunGateIndeterminateError(
        "the SDK could not determine a decision"
    ),
    # -- the concrete refusals the gate actually raises ----------------
    NullRunBudgetError: lambda: NullRunBudgetError(
        workflow_id="w-sdk14", reason="budget exhausted"
    ),
    WorkflowPausedException: lambda: WorkflowPausedException(
        workflow_id="w-sdk14", reason="paused by the operator"
    ),
    NullRunToolBlockedError: lambda: NullRunToolBlockedError(
        workflow_id="w-sdk14", reason="tool is on the block list", tool_name="shell"
    ),
    # -- SDK-16: the classes the RUNTIME raises on purpose and the
    #    enforcement FAMILY does not name -----------------------------
    # A 401 is a credential failure, not a transport condition: no
    # retry can fix a revoked key. The SDK's own table says
    # fail-CLOSED for it, and the @protect boundary obeyed that while
    # the callback swallowed it.
    NullRunAuthenticationError: lambda: NullRunAuthenticationError(
        "the backend rejected the key (401)"
    ),
    # The gate ANSWERED and refused, but without a category the SDK
    # trusts. ADR-062 §2.2: an unclassifiable refusal must RAISE.
    NullRunUnclassifiedRefusalError: lambda: NullRunUnclassifiedRefusalError(
        "the gate refused without a category the SDK understands",
        wire_category=None,
        error_code_wire="NR-UNKNOWN",
    ),
    # The rate-limit backend is down. The SDK's table calls this a
    # named enforcement failure and says the SDK does NOT silently
    # fail-OPEN on it.
    NullRunRateLimitRedisError: lambda: NullRunRateLimitRedisError(
        "the backend cannot reach Redis for the aggregate rate limit (503)"
    ),
}


@pytest.fixture(autouse=True)
def _clear_deferred():
    """No test may inherit a stashed decision from another."""
    drain_deferred_enforcement()
    yield
    drain_deferred_enforcement()


class TestFrameworkContract:
    def test_langchain_swallows_callback_exceptions(self):
        """The premise of the whole fix, pinned against real langchain.

        A DEFAULT handler's exception is swallowed: langchain-core logs
        "Error in <handler> callback" and continues. That is why the
        NullRun handler has to set `raise_error = True` explicitly
        (SDK-4) — the swallow is the default, not the exception.

        If a future langchain-core propagates callback exceptions even
        without the flag, the handoff becomes unnecessary (though still
        correct) — but the test must fail loudly so someone re-evaluates
        the design rather than the assumption silently rotting.
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


def _fails_closed(case: type[BaseException]) -> bool:
    """Whether the CALLBACK must re-raise this class.

    The production predicate, read from the instrumentation's own
    constants — never spelled out here (SDK-14 removed the copy of the
    family for exactly that reason). It models the ORDER of the
    callback's arms, and that order is pinned separately in the AST
    (`TestEnforcementClassification`):

    1. the family          -> re-raised
    2. a transport failure -> swallowed (ADR-008's fail-OPEN)
    3. other infra errors  -> re-raised (fail-CLOSED)
    """
    if any(issubclass(case, member) for member in _ENFORCEMENT_EXCEPTIONS):
        return True
    if any(issubclass(case, member) for member in _TRANSPORT_ERRORS):
        return False
    return any(issubclass(case, member) for member in _INFRASTRUCTURE_ERRORS)


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


class TestTheFamilyAndTheCasesCorrespond:
    """SDK-14: the suite must assert the PRODUCTION predicate.

    The defect this file used to carry was a COPY of the predicate.
    These tests are what make the copy impossible to keep: they
    compare the production family with the cases this suite actually
    drives, in BOTH directions, and they pin that the instrumentation
    uses the production tuple itself.
    """

    def test_every_family_member_has_a_case(self):
        missing = [
            member.__name__
            for member in ENFORCEMENT_EXCEPTIONS
            if member not in _CASE_BUILDERS
        ]
        assert not missing, (
            "the production enforcement family has members this suite does "
            f"not drive: {missing}. A family member is a promise that the "
            "refusal REACHES the caller; a member nobody drives is a "
            "promise nobody checks. Drive it here before the class ships "
            "— or take the class out of the family with a decision."
        )

    def test_every_case_is_covered_by_the_family(self):
        uncovered = [
            case.__name__
            for case in _CASE_BUILDERS
            if not _fails_closed(case)
        ]
        assert not uncovered, (
            f"these cases are no longer covered by the production "
            f"fail-CLOSED set: {uncovered}. The set was narrowed, or the "
            "case is stale — either way the refusal these classes "
            "represent would now fall into the broad fail-OPEN arm and "
            "be swallowed, and the same refusal would kill the call "
            "through the @protect path and vanish through the "
            "callback path."
        )

    def test_every_family_member_is_an_exception(self):
        """A BaseException-only member would escape the split entirely."""
        not_exceptions = [
            member.__name__
            for member in ENFORCEMENT_EXCEPTIONS
            if not issubclass(member, Exception)
        ]
        assert not not_exceptions, (
            f"{not_exceptions} derive from BaseException only — the broad "
            "arm catches Exception, so such a member would never be "
            "classified at all."
        )

    def test_the_instrumentation_uses_the_production_tuple(self):
        """Identity, not a copy.

        `instrumentation/langgraph.py` imports the production tuple and
        falls back to an EMPTY tuple if that import fails. An empty
        tuple means "no refusal is an enforcement refusal" — every
        refusal is downgraded to a transport error and swallowed by the
        broad arm. The alias must BE the production object, so the two
        can never drift apart and the fallback can never be in effect.
        """
        assert _ENFORCEMENT_EXCEPTIONS is ENFORCEMENT_EXCEPTIONS, (
            "the instrumentation's enforcement alias is not the production "
            f"tuple — it is {_ENFORCEMENT_EXCEPTIONS!r}. An empty alias "
            "means every refusal is treated as a transport error and "
            "swallowed by the broad fail-OPEN arm."
        )


class _GateRefuses:
    """A runtime whose budget pre-flight raises one fixed refusal."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.checks = 0

    def check_workflow_budget(self, *a, **k):
        self.checks += 1
        raise self.exc

    def track_event(self, *a, **k):
        return None

    def track_tool(self, *a, **k):
        return None


class TestEveryCaseIsDriven:
    """SDK-14: a family member must be PROVEN, not asserted.

    `issubclass` says the class is named by the family. It does not say
    the refusal REACHES the caller. That difference is the whole point
    of the family: a member the callback mishandles is downgraded to a
    fail-OPEN transport error, and the call proceeds with no
    reservation — the agent gets the call for free.

    So every case is driven through the REAL callback, and the refusal
    must (a) escape to the caller and (b) be recorded for the
    ``@protect`` boundary.
    """

    @pytest.mark.parametrize(
        "case",
        list(_CASE_BUILDERS),
        ids=[c.__name__ for c in _CASE_BUILDERS],
    )
    def test_the_refusal_reaches_the_caller(self, case):
        pytest.importorskip("langchain_core")
        from langchain_core.callbacks.manager import CallbackManager

        exc = _CASE_BUILDERS[case]()
        cb = NullRunCallback(runtime=_GateRefuses(exc))

        with pytest.raises(type(exc)) as caught:
            CallbackManager([cb]).on_llm_start({}, ["hi"])

        assert caught.value is exc, (
            f"{case.__name__}: the caller must receive the refusal itself, "
            f"got {type(caught.value).__name__}"
        )

    @pytest.mark.parametrize(
        "case",
        list(_CASE_BUILDERS),
        ids=[c.__name__ for c in _CASE_BUILDERS],
    )
    def test_the_refusal_reaches_the_async_caller(self, case):
        """The async path is a DIFFERENT code path in langchain-core."""
        import asyncio

        pytest.importorskip("langchain_core")
        from langchain_core.callbacks.manager import AsyncCallbackManager

        exc = _CASE_BUILDERS[case]()
        cb = NullRunCallback(runtime=_GateRefuses(exc))

        with pytest.raises(type(exc)) as caught:
            asyncio.run(AsyncCallbackManager([cb]).on_llm_start({}, ["hi"]))

        assert type(caught.value) is type(exc), (
            f"{case.__name__}: the async caller must receive the gate "
            f"refusal, got {type(caught.value).__name__}"
        )

    @pytest.mark.parametrize(
        "case",
        list(_CASE_BUILDERS),
        ids=[c.__name__ for c in _CASE_BUILDERS],
    )
    def test_the_refusal_is_recorded_for_the_protect_boundary(self, case):
        """The secondary channel: a host framework that ignores
        ``raise_error`` still gets the decision at the ``@protect``
        boundary, on the boundary's own thread.
        """
        exc = _CASE_BUILDERS[case]()
        cb = NullRunCallback(runtime=_GateRefuses(exc))

        with pytest.raises(type(exc)):
            # `run_id` is required: without it the callback returns early
            # (it cannot open a span it would never close) and never
            # reaches the budget pre-flight.
            cb.on_llm_start({}, ["hi"], run_id="run-sdk14")

        assert drain_deferred_enforcement() is exc, (
            f"{case.__name__}: the refusal must also be recorded — it is "
            "the only channel that can abort on the @protect boundary's "
            "own thread"
        )

    def test_a_transport_failure_still_fails_open(self):
        """ADR-008: a dead backend must NOT freeze the agent.

        The counter-test for the family: an exception the family does
        NOT name is a transport failure, and fail-OPEN is the
        documented policy for it. Without this, "widen the family" would
        look like a free improvement — and the family would grow until
        a dead backend froze every agent.

        SDK-16 sharpened this: a transport failure IS an
        infrastructure error, so the callback's new infrastructure arm
        would catch it and refuse — unless the transport arm comes
        first. The ORDER is pinned in the AST
        (`test_the_transport_arm_precedes_the_infrastructure_arm`); this
        test pins the BEHAVIOUR it exists to protect.
        """
        boom = NullRunBackendError("boom", endpoint="gate", status_code=500)
        assert not isinstance(boom, ENFORCEMENT_EXCEPTIONS), (
            "a transport failure is not a decision — if this ever becomes "
            "true the family's rule has changed and this control is no "
            "longer a control"
        )
        assert isinstance(boom, NullRunInfrastructureError), (
            "a transport failure IS an infrastructure error — that is why "
            "the callback needs a transport arm of its own, listed "
            "before the infrastructure one. If this ever becomes false "
            "the arm order stops mattering and the pin below would be "
            "asserting nothing."
        )

        cb = NullRunCallback(runtime=_GateRefuses(boom))
        cb.on_llm_start({}, ["hi"], run_id="run-sdk14")  # must not raise

        assert drain_deferred_enforcement() is None, (
            "a transport failure is not a decision — it must not abort a "
            "later @protect call either"
        )

    def test_a_cosmetic_failure_is_still_swallowed(self):
        """The other half of the widening: a BUG must not abort the agent.

        ADR-122 §3 rejected "widen the family to every class" partly
        because a cosmetic failure — a span bookkeeping bug, a broken
        runtime double — must not break the user's agent. The callback
        keeps that contract: a fault that is not a NullRun error is
        logged at debug and swallowed.

        Without this test, "re-raise everything" would look like a free
        improvement too, and a typo in the SDK would freeze every
        agent that uses LangChain.
        """
        cb = NullRunCallback(runtime=_GateRefuses(RuntimeError("a bug in the SDK")))
        cb.on_llm_start({}, ["hi"], run_id="run-sdk14")  # must not raise

        assert drain_deferred_enforcement() is None, (
            "a cosmetic failure is not a decision — it must not abort a "
            "later @protect call either"
        )


class TestTheTableTheCallbackObeys:
    """SDK-16: the callback must obey the SDK's OWN fail-OPEN/CLOSED table.

    The table is the module docstring of `nullrun/runtime.py` — the
    authoritative statement of what each gate does when it cannot be
    reached (ADR-063 §2.4). Its row for `check_workflow_budget` says
    the gate fails OPEN for TRANSPORT errors ONLY, and CLOSED for
    everything else.

    This is a DRIFT tripwire, not the proof. The proof is the
    behavioural tests above, which drive the real callback with the
    real classes. The tripwire exists so that editing the table
    without editing the callback is a RED test instead of a silent
    contradiction — the contradiction SDK-16 was.
    """

    def test_the_table_still_says_closed_for_everything_else(self):
        import nullrun.runtime as runtime_mod

        table = runtime_mod.__doc__ or ""
        assert (
            "OPEN (skip check, log warning) for TRANSPORT errors ONLY" in table
        ), (
            "the fail-OPEN/CLOSED table in nullrun/runtime.py no longer "
            "states the check_workflow_budget row this suite enforces. "
            "The table is authoritative (ADR-063 §2.4) — if the row "
            "changed, the callback's posture must be re-decided, not "
            "left to drift."
        )

    def test_the_table_still_names_the_four_classes(self):
        import nullrun.runtime as runtime_mod

        table = runtime_mod.__doc__ or ""
        for name in (
            "NullRunAuthError",
            "NullRunMalformedGateResponseError",
            "NullRunGateIndeterminateError",
            "NullRunRateLimitRedisError",
        ):
            assert name in table, (
                f"{name} is no longer named in the SDK's fail-OPEN/CLOSED "
                "table. The table is what the callback is measured "
                "against, so a class that leaves the table leaves the "
                "posture undecided."
            )


class TestEnforcementClassification:
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

    def test_the_transport_arm_precedes_the_infrastructure_arm(self):
        """SDK-16: the ORDER is what keeps a dead backend fail-OPEN.

        `NullRunTransportError` is a `NullRunInfrastructureError`, so an
        infrastructure arm listed BEFORE the transport arm would catch
        every network failure and turn it into a refusal: a dead
        backend would freeze the agent instead of letting it through.
        The transport arm must come first, in both places that
        classify an exception — the callback's check path and the
        guard around every callback method.
        """
        from nullrun.instrumentation.langgraph import (
            NullRunCallback,
            _only_a_gate_refusal_escapes,
        )

        for where, arms in (
            ("on_llm_start", _excepted_types(NullRunCallback.on_llm_start)),
            (
                "_only_a_gate_refusal_escapes",
                _excepted_types(_only_a_gate_refusal_escapes),
            ),
        ):
            assert ["_TRANSPORT_ERRORS"] in arms, (
                f"{where} must classify a transport failure explicitly; "
                f"arms: {arms}"
            )
            assert ["_INFRASTRUCTURE_ERRORS"] in arms, (
                f"{where} must re-raise a non-transport infrastructure "
                f"error (SDK-16); arms: {arms}"
            )
            assert arms.index(["_TRANSPORT_ERRORS"]) < arms.index(
                ["_INFRASTRUCTURE_ERRORS"]
            ), (
                f"{where}: the transport arm must come FIRST. "
                "NullRunTransportError is a NullRunInfrastructureError, so "
                "the other order turns every network failure into a "
                f"refusal and freezes the agent. Arms: {arms}"
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

    def is_fail_open_allowed(self, *a, **k):
        """The per-tool bound on the switch (ADR-126).

        The stub reports "not classified" — the same default as a fresh
        runtime. With the flag OFF the decorator never asks, so this
        only matters if the order of the two halves ever changes.
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
