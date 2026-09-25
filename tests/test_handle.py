"""Tests for the ``nullrun.handle`` context manager.

Contract:

* ``with handle():`` translates any :class:`nullrun.NullRunError`
  into ``print(format_user_message(exc), file=sys.stderr)`` and then
  ``sys.exit(1)``.
* :class:`nullrun.WorkflowKilledInterrupt` propagates unchanged —
  kill must not be swallowed into a graceful exit.
  (``WorkflowKilledInterrupt`` is now an ``Exception`` subclass via
  ``NullRunError``, but ``handle`` explicitly re-raises it so the
  kill signal still reaches the top of the agent loop.)
* Non-NullRun exceptions also propagate unchanged so the user's own
  bugs surface as honest tracebacks.
* No runtime is required — ``handle`` works without
  ``nullrun.init()``.
"""
from __future__ import annotations

import pytest

import nullrun
from nullrun import handle
from nullrun.breaker.exceptions import (
    NullRunBudgetError,
    NullRunError,
    WorkflowKilledInterrupt,
)


def test_handle_catches_nullrun_error_and_exits(monkeypatch, capsys):
    """``with handle():`` exits 1 and prints the catalog user-message."""
    exits = []

    def fake_exit(code):
        exits.append(code)
        raise SystemExit(code)

    monkeypatch.setattr("sys.exit", fake_exit)

    with pytest.raises(SystemExit):
        with handle():
            # NullRunBudgetError inherits from NullRunBlockedException
            # whose __init__ takes (workflow_id, reason,...).
            raise NullRunBudgetError("wf-1", "workflow budget exhausted")

    captured = capsys.readouterr()
    assert "limit" in captured.err.lower() or "budget" in captured.err.lower()
    assert exits == [1]


def test_handle_propagates_workflow_killed(monkeypatch):
    """``WorkflowKilledInterrupt`` must NOT be swallowed into sys.exit."""
    monkeypatch.setattr("sys.exit", lambda c: pytest.fail("sys.exit was called"))

    with pytest.raises(WorkflowKilledInterrupt):
        with handle():
            raise WorkflowKilledInterrupt("wf-1", "killed via dashboard")


def test_handle_propagates_value_error(monkeypatch):
    """Non-NullRun exceptions pass through for an honest traceback."""
    monkeypatch.setattr("sys.exit", lambda c: pytest.fail("sys.exit was called"))

    with pytest.raises(ValueError):
        with handle():
            raise ValueError("user bug, not an SDK failure")


def test_handle_returns_on_success(monkeypatch):
    """A clean ``with`` block returns the wrapped expression's value."""
    monkeypatch.setattr("sys.exit", lambda c: pytest.fail("sys.exit was called"))

    with handle():
        result = 1 + 2

    assert result == 3


def test_handle_exit_code_kwarg(monkeypatch, capsys):
    """``handle(exit_code=42)`` honours the override."""
    exits = []

    def fake_exit(code):
        exits.append(code)
        raise SystemExit(code)

    monkeypatch.setattr("sys.exit", fake_exit)

    with pytest.raises(SystemExit):
        with handle(exit_code=42):
            raise NullRunError("oops", error_code="NR-B002")

    assert exits == [42]


def test_no_init_required():
    """``handle`` must not depend on a runtime."""
    # If handle pulled in the runtime, importing this module would have
    # raised during the prior tests. Smoke-test the import path here.
    assert callable(handle)
    assert callable(nullrun.handle)
