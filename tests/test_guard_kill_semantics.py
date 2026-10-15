"""What ``nullrun.guard()`` does with a kill — and why it differs
between the sync and async ``@protect`` wrappers.

Why this test exists (2026-10-15)
---------------------------------
A QA pass flagged a comment in ``decorators.py`` that claimed
``WorkflowKilledInterrupt`` was a ``BaseException`` subclass and
therefore "bypassed" the ``on_error`` hook. It is not: the class is

    WorkflowKilledInterrupt -> NullRunError -> BreakerError -> Exception

so the explanation was wrong. Chasing the truth exposed something
the wrong explanation had been hiding — an actual behavioural split:

* async ``@protect`` (``unify_block=False``) lets the raw
  ``WorkflowKilledInterrupt`` propagate. ``guard`` re-raises it
  explicitly, so the kill reaches the agent loop.
* sync ``@protect`` (``unify_block=True``) substitutes
  ``NullRunBlockedException`` (error_code ``NR-W002``), which is a
  ``NullRunDecision`` and **not** a ``WorkflowKilledInterrupt``
  subclass. ``guard``'s ``isinstance`` re-raise therefore does not
  match: it prints a report and calls ``sys.exit(1)``.

These tests pin the real behaviour so the comments describing it
cannot go stale the way the old one did.
"""

from __future__ import annotations

import pytest

from nullrun import NullRunBlockedException, WorkflowKilledInterrupt, guard
from nullrun.breaker.exceptions import WorkflowPausedException


class TestKillIsAnExceptionSubclass:
    """The premise every stale comment got wrong."""

    def test_killed_is_an_exception_not_a_base_exception_only_type(self):
        # If this fails, WorkflowKilledInterrupt became a
        # BaseException-only subtype again and every docstring saying
        # "Exception subclass" is wrong.
        assert issubclass(WorkflowKilledInterrupt, Exception), (
            "WorkflowKilledInterrupt must be an Exception subclass. "
            "Docs, middleware rationale and the fastapi 503 path all "
            "depend on this; they previously described it as a "
            "BaseException subclass, which it has not been since the "
            "Layer-1 hierarchy migration."
        )

    def test_paused_is_an_exception_subclass(self):
        assert issubclass(WorkflowPausedException, Exception)


class TestSyncWrapperSubstitution:
    """The sync/async asymmetry that the stale comment concealed."""

    def test_blocked_is_not_a_workflow_killed_interrupt(self):
        # This single fact is what makes `guard` behave differently on
        # the two paths: its `isinstance(exc, WorkflowKilledInterrupt)`
        # re-raise cannot match the sync wrapper's substitute.
        assert not issubclass(NullRunBlockedException, WorkflowKilledInterrupt), (
            "If NullRunBlockedException became a WorkflowKilledInterrupt "
            "subclass, guard's re-raise would start matching on the sync "
            "path too and the asymmetry documented in __init__.py would "
            "be gone — update that comment in the same commit."
        )

    def test_blocked_carries_the_kill_error_code(self):
        err = NullRunBlockedException(
            workflow_id="wf-1",
            reason="killed",
            error_code="NR-W002",
        )
        assert err.error_code == "NR-W002"


class TestGuardPropagatesRawKill:
    """Async path: the kill escapes ``guard`` untouched."""

    def test_guard_reraises_workflow_killed_interrupt(self):
        with pytest.raises(WorkflowKilledInterrupt):
            with guard():
                raise WorkflowKilledInterrupt("wf-1", reason="killed")


class TestGuardTurnsSyncBlockIntoExit:
    """Sync path: ``guard`` prints and exits instead of propagating."""

    def test_guard_exits_instead_of_propagating_the_blocked_wrapper(
        self, capsys, monkeypatch
    ):
        monkeypatch.setattr("sys.exit", _raise_system_exit)
        with pytest.raises(SystemExit) as info:
            with guard(exit_code=7):
                raise NullRunBlockedException(
                    workflow_id="wf-1",
                    reason="killed",
                    error_code="NR-W002",
                )
        assert info.value.code == 7, (
            "guard must honour its exit_code argument on the sync-block "
            "path. If this regressed to propagating the kill, remove "
            "this test and fix the __init__.py comment."
        )
        assert capsys.readouterr().err.strip() != "", (
            "guard must write a report to stderr before exiting, "
            "otherwise the kill vanishes with no trace."
        )


def _raise_system_exit(code: int):
    raise SystemExit(code)