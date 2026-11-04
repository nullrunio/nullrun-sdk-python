"""tests/test_langchain_refusal_surfaces.py — a gate refusal must REACH
the caller, not vanish into a debug log line.

SDK-4 (reliability audit 2026-11-02).

The defect
----------
``NullRunCallback.on_llm_start`` classified a gate refusal with a
three-class allow-list::

    _ENFORCEMENT_EXCEPTIONS = (
        NullRunBudgetError, WorkflowKilledInterrupt, WorkflowPausedException,
    )

Every OTHER refusal the gate can issue fell into the broad
``except Exception`` arm, was logged at DEBUG and DROPPED:

* ``TOOL_BLOCKED`` -> ``NullRunToolBlockedError`` -> ``NullRunDecision``
* ``RATE_LIMIT_EXCEEDED`` -> ``RateLimitError`` -> ``NullRunTransportError``

The agent therefore carried on after the gate had refused the call, and
the refusal was invisible to the application that is supposed to honour
it.

The fix
-------
1. The refusal family is named by BASE class (``NullRunDecision``,
   ``RateLimitError``, ``WorkflowKilledInterrupt``), so a refusal
   subclass added later cannot fall back into the fail-OPEN arm.
2. The refusal is RE-RAISED out of the callback, and the handler sets
   ``raise_error = True`` — langchain-core re-raises a handler's
   exception in BOTH the sync and the async dispatch path when that
   flag is set. Before the fix the refusal could not leave the
   callback at all.
3. ``_only_a_gate_refusal_escapes`` guarantees that a gate refusal is
   the ONLY exception that can leave a NullRun callback, so opting
   into ``raise_error`` cannot break the host chain for a cosmetic
   failure.

WHY THIS FILE DRIVES A REAL CALLBACK MANAGER
-------------------------------------------
A source-pin on the module can prove that ``raise`` appears after the
enforcement arm. It cannot prove that the framework PROPAGATES what
the handler raises — that is langchain-core's contract, and the
contract is exactly what was silently inverted. So this file attaches
the real handler to a real ``CallbackManager`` / ``AsyncCallbackManager``
and reads what the caller receives.

The transport arm is checked too: a backend outage must still fail
OPEN (ADR-008). Opting into ``raise_error`` must not turn "the gate
could not be reached" into a hard failure.
"""

from __future__ import annotations

import asyncio

import pytest

from nullrun.breaker.exceptions import (
    NullRunBackendError,
    NullRunBudgetError,
    NullRunToolBlockedError,
    RateLimitError,
    TransportErrorSource,
    WorkflowKilledInterrupt,
)
from nullrun.instrumentation.langgraph import (
    _ENFORCEMENT_EXCEPTIONS,
    NullRunCallback,
    drain_deferred_enforcement,
)


class _StubRuntime:
    """A runtime whose budget pre-flight raises a fixed exception."""

    def __init__(self, exc: BaseException | None) -> None:
        self.exc = exc
        self.checks = 0

    def check_workflow_budget(self, *a, **k):
        self.checks += 1
        if self.exc is not None:
            raise self.exc
        return None

    def track_event(self, *a, **k):
        return None

    def track_tool(self, *a, **k):
        return None


def _tool_blocked() -> NullRunToolBlockedError:
    return NullRunToolBlockedError(
        workflow_id="w-1",
        reason="tool is on the block list",
        tool_name="shell",
    )


def _rate_limited() -> RateLimitError:
    return RateLimitError(
        "rate limit exceeded",
        source=TransportErrorSource.GATEWAY_ERROR,
        endpoint="gate",
    )


# Every refusal the gate can issue on the /gate pre-flight, and the two
# classes the audit named explicitly.
REFUSALS = [
    ("TOOL_BLOCKED", _tool_blocked),
    ("RATE_LIMIT", _rate_limited),
    ("BUDGET", lambda: NullRunBudgetError(workflow_id="w-1", reason="budget exhausted")),
    ("KILLED", lambda: WorkflowKilledInterrupt(workflow_id="w-1", reason="killed")),
]


@pytest.fixture(autouse=True)
def _clear_deferred():
    drain_deferred_enforcement()
    yield
    drain_deferred_enforcement()


def _callback(exc: BaseException | None) -> NullRunCallback:
    return NullRunCallback(runtime=_StubRuntime(exc))


class TestRefusalReachesTheCaller:
    @pytest.mark.parametrize("name,make", REFUSALS, ids=[n for n, _ in REFUSALS])
    def test_sync_dispatch_propagates_the_refusal(self, name, make):
        """The refusal must leave the callback, not die in a debug log."""
        pytest.importorskip("langchain_core")
        from langchain_core.callbacks.manager import CallbackManager

        cb = _callback(make())

        with pytest.raises(BaseException) as caught:
            CallbackManager([cb]).on_llm_start({}, ["hi"])

        assert type(caught.value) is type(make()), (
            f"{name}: the caller must receive the gate refusal, got "
            f"{type(caught.value).__name__}"
        )

    @pytest.mark.parametrize("name,make", REFUSALS, ids=[n for n, _ in REFUSALS])
    def test_async_dispatch_propagates_the_refusal(self, name, make):
        """The async path is a DIFFERENT code path in langchain-core.

        The sync handler is run through an executor there, and the
        exception is read back on the event-loop thread. Both were
        measured: without ``raise_error`` the refusal was swallowed on
        both.
        """
        pytest.importorskip("langchain_core")
        from langchain_core.callbacks.manager import AsyncCallbackManager

        cb = _callback(make())

        with pytest.raises(BaseException) as caught:
            asyncio.run(AsyncCallbackManager([cb]).on_llm_start({}, ["hi"]))

        assert type(caught.value) is type(make()), (
            f"{name}: the async caller must receive the gate refusal, got "
            f"{type(caught.value).__name__}"
        )

    def test_the_refusal_is_also_recorded_for_the_protect_boundary(self):
        """The secondary channel is kept: a host framework that ignores
        ``raise_error`` still gets the decision at the ``@protect``
        boundary.
        """
        from nullrun.instrumentation.langgraph import drain_deferred_enforcement

        cb = _callback(_tool_blocked())
        with pytest.raises(NullRunToolBlockedError):
            # `run_id` is required: without it the callback returns early
            # (it cannot open a span it would never close) and never
            # reaches the budget pre-flight.
            cb.on_llm_start({}, ["hi"], run_id="run-1")

        deferred = drain_deferred_enforcement()
        assert deferred is not None, (
            "the refusal must also be recorded — it is the only channel "
            "that can abort on the @protect boundary's own thread"
        )
        assert isinstance(deferred, NullRunToolBlockedError)


class TestTransportFailureStillFailsOpen:
    """ADR-008: a dead backend must NOT freeze the agent.

    Opting into ``raise_error`` must not turn "the gate could not be
    reached" into a hard failure — that would be a different bug, and
    a worse one (an outage would take every agent down).
    """

    def test_a_transport_failure_does_not_escape(self):
        pytest.importorskip("langchain_core")
        from langchain_core.callbacks.manager import CallbackManager

        cb = _callback(NullRunBackendError("boom", endpoint="gate", status_code=500))

        CallbackManager([cb]).on_llm_start({}, ["hi"])  # must not raise

    def test_a_transport_failure_is_not_recorded_as_a_refusal(self):
        """A dead backend is not a decision — it must not abort a later
        ``@protect`` call either.
        """
        cb = _callback(NullRunBackendError("boom", endpoint="gate", status_code=500))
        cb.on_llm_start({}, ["hi"])

        assert drain_deferred_enforcement() is None


class TestOnlyARefusalMayEscape:
    """``raise_error = True`` is only safe if nothing else can escape."""

    def test_the_guard_swallows_a_cosmetic_failure(self):
        """A broken span bookkeeping must not abort the user's agent."""
        from nullrun.instrumentation.langgraph import _only_a_gate_refusal_escapes

        class _Obj:
            @_only_a_gate_refusal_escapes
            def cosmetic(self):
                raise RuntimeError("span emission is broken")

        assert _Obj().cosmetic() is None, (
            "a non-refusal failure must be swallowed — it is the reason "
            "opting into raise_error is safe"
        )

    def test_the_guard_lets_a_refusal_through(self):
        from nullrun.instrumentation.langgraph import _only_a_gate_refusal_escapes

        class _Obj:
            @_only_a_gate_refusal_escapes
            def refusal(self):
                raise _tool_blocked()

        with pytest.raises(NullRunToolBlockedError):
            _Obj().refusal()

    def test_a_broken_backend_does_not_break_the_chain(self):
        """End to end: a runtime whose event emission fails."""
        pytest.importorskip("langchain_core")
        from langchain_core.callbacks.manager import CallbackManager

        class _BrokenRuntime(_StubRuntime):
            def track_event(self, *a, **k):
                raise RuntimeError("span emission is broken")

        cb = NullRunCallback(runtime=_BrokenRuntime(None))

        CallbackManager([cb]).on_llm_start({}, ["hi"])  # must not raise

    def test_the_handler_opts_into_propagation(self):
        assert NullRunCallback.raise_error is True, (
            "langchain-core swallows a callback's exception unless the "
            "handler sets raise_error — without it the refusal never "
            "reaches the caller"
        )


class TestTheRefusalFamilyIsNamedByBaseClass:
    """The family must not rot back into a three-class list."""

    def test_every_refusal_class_is_covered(self):
        for name, make in REFUSALS:
            exc = make()
            assert isinstance(exc, _ENFORCEMENT_EXCEPTIONS), (
                f"{name}: a real gate refusal must be caught by the "
                "enforcement arm BEFORE the broad transport arm, or it "
                "is downgraded to a fail-OPEN transport error"
            )

    def test_a_transport_failure_is_not_covered(self):
        """The gate could not be reached -> ADR-008 fail-OPEN."""
        assert not isinstance(
            NullRunBackendError("boom", endpoint="gate", status_code=500),
            _ENFORCEMENT_EXCEPTIONS,
        )

    def test_the_rate_limit_refusal_is_covered(self):
        """`RateLimitError` is a TRANSPORT-shaped class, not a decision.

        Naming only `NullRunDecision` would still have swallowed the
        429 refusal — the shape is what makes this easy to get wrong.
        """
        assert not issubclass(RateLimitError, __import__(
            "nullrun.breaker.exceptions", fromlist=["NullRunDecision"]
        ).NullRunDecision), (
            "if this ever becomes true, the reason RateLimitError is "
            "named explicitly in _ENFORCEMENT_EXCEPTIONS has changed"
        )
        assert isinstance(_rate_limited(), _ENFORCEMENT_EXCEPTIONS)
