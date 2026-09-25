"""Tests for the ``nullrun.guard`` context manager.

Contract:

* ``with guard():`` translates any :class:`nullrun.NullRunError`
  into ``print(format_user_message(exc), file=sys.stderr)`` and then
  ``sys.exit(1)``.
* :class:`nullrun.WorkflowKilledInterrupt` propagates unchanged —
  kill must not be swallowed into a graceful exit.
  (``WorkflowKilledInterrupt`` is now an ``Exception`` subclass via
  ``NullRunError``, but ``guard`` explicitly re-raises it so the
  kill signal still reaches the top of the agent loop.)
* Non-NullRun exceptions also propagate unchanged so the user's own
  bugs surface as honest tracebacks.
* No runtime is required — ``guard`` works without
  ``nullrun.init()``.

History: 0.18.4 renamed ``nullrun.handle`` to ``nullrun.guard``.
Same body, same semantics, shorter verb.
"""
from __future__ import annotations

import pytest

import nullrun
from nullrun import guard
from nullrun.breaker.exceptions import (
    NullRunBudgetError,
    NullRunError,
    WorkflowKilledInterrupt,
)


def test_guard_catches_nullrun_error_and_exits(monkeypatch, capsys):
    """``with guard():`` exits 1 and prints the catalog user-message."""
    exits = []

    def fake_exit(code):
        exits.append(code)
        raise SystemExit(code)

    monkeypatch.setattr("sys.exit", fake_exit)

    with pytest.raises(SystemExit):
        with guard():
            # NullRunBudgetError inherits from NullRunBlockedException
            # whose __init__ takes (workflow_id, reason,...).
            raise NullRunBudgetError("wf-1", "workflow budget exhausted")

    captured = capsys.readouterr()
    assert "limit" in captured.err.lower() or "budget" in captured.err.lower()
    assert exits == [1]


def test_guard_propagates_workflow_killed(monkeypatch):
    """``WorkflowKilledInterrupt`` must NOT be swallowed into sys.exit."""
    monkeypatch.setattr("sys.exit", lambda c: pytest.fail("sys.exit was called"))

    with pytest.raises(WorkflowKilledInterrupt):
        with guard():
            raise WorkflowKilledInterrupt("wf-1", "killed via dashboard")


def test_guard_propagates_value_error(monkeypatch):
    """Non-NullRun exceptions pass through for an honest traceback."""
    monkeypatch.setattr("sys.exit", lambda c: pytest.fail("sys.exit was called"))

    with pytest.raises(ValueError):
        with guard():
            raise ValueError("user bug, not an SDK failure")


def test_guard_returns_on_success(monkeypatch):
    """A clean ``with`` block returns the wrapped expression's value."""
    monkeypatch.setattr("sys.exit", lambda c: pytest.fail("sys.exit was called"))

    with guard():
        result = 1 + 2

    assert result == 3


def test_guard_exit_code_kwarg(monkeypatch, capsys):
    """``guard(exit_code=42)`` honours the override."""
    exits = []

    def fake_exit(code):
        exits.append(code)
        raise SystemExit(code)

    monkeypatch.setattr("sys.exit", fake_exit)

    with pytest.raises(SystemExit):
        with guard(exit_code=42):
            raise NullRunError("oops", error_code="NR-B002")

    assert exits == [42]


def test_no_init_required():
    """``guard`` must not depend on a runtime."""
    # If guard pulled in the runtime, importing this module would have
    # raised during the prior tests. Smoke-test the import path here.
    assert callable(guard)
    assert callable(nullrun.guard)


def test_handle_removed_from_public_surface():
    """``nullrun.handle`` was fully removed in 0.18.4 (renamed to
    ``guard``). This is a regression guard against a future re-add of
    the old name as a deprecation alias."""
    import nullrun as n

    assert not hasattr(n, "handle")
    assert "handle" not in n.__all__
