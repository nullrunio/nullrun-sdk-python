"""
Regression tests for the WorkflowKilledInterrupt deprecation-bypass.

``WorkflowKilledException`` is the deprecated parent class. It emits a
``DeprecationWarning`` on construct so old code that explicitly raises
it knows to migrate. ``WorkflowKilledInterrupt`` is the canonical
class and must NOT emit the warning on construct (the SDK raises it
from dozens of call sites — each one would emit a warning if the
bypass were broken).

The bypass is implemented in ``breaker/exceptions.py`` by
calling ``BaseException.__init__`` directly instead of
``super().__init__()`` (which would re-emit the parent's warning).
This test pins the contract.
"""

from __future__ import annotations

import warnings

import pytest

from nullrun.breaker.exceptions import (
    WorkflowKilledException,
    WorkflowKilledInterrupt,
)


class TestWorkflowKilledInterruptBypass:
    def test_interrupt_does_not_emit_deprecation_warning(self):
        """Constructing ``WorkflowKilledInterrupt`` must not emit
        the parent's ``DeprecationWarning``. If this test fails,
        a recent refactor probably re-introduced the
        ``super().__init__()`` call in the subclass.
        """
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            exc = WorkflowKilledInterrupt(workflow_id="wf-1", reason="kill")
        deprecation = [
            w
            for w in caught
            if issubclass(w.category, DeprecationWarning)
            and "WorkflowKilledException" in str(w.message)
        ]
        assert deprecation == [], (
            f"WorkflowKilledInterrupt must not emit "
            f"WorkflowKilledException's DeprecationWarning. Got: "
            f"{[str(w.message) for w in deprecation]}"
        )
        assert exc.workflow_id == "wf-1"
        assert exc.reason == "kill"

    def test_legacy_class_does_emit_deprecation_warning(self):
        """Constructing the legacy ``WorkflowKilledException``
        DOES emit the deprecation warning — that is the
        migration signal for old code.
        """
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            WorkflowKilledException(workflow_id="wf-2", reason="legacy")
        deprecation = [
            w
            for w in caught
            if issubclass(w.category, DeprecationWarning)
            and "WorkflowKilledException" in str(w.message)
        ]
        assert deprecation, (
            "WorkflowKilledException must emit a DeprecationWarning "
            "so callers know to migrate to WorkflowKilledInterrupt."
        )

    def test_interrupt_is_baseexception_not_exception(self):
        """``WorkflowKilledInterrupt`` is a ``BaseException`` subclass
        by design — ``except Exception`` in user code must NOT
        catch a kill signal. Pinned by docs/kill-contract.md §6.
        """
        assert issubclass(WorkflowKilledInterrupt, BaseException)
        assert not issubclass(WorkflowKilledInterrupt, Exception)

    def test_legacy_catch_still_catches_interrupt(self):
        """``except WorkflowKilledException`` (legacy user code)
        must still catch ``WorkflowKilledInterrupt`` because
        ``WorkflowKilledInterrupt`` is a subclass.
        """
        try:
            raise WorkflowKilledInterrupt(workflow_id="wf-3", reason="kill")
        except WorkflowKilledException:
            pass  # expected — legacy clause still works
        else:
            pytest.fail("except WorkflowKilledException did not catch interrupt")
