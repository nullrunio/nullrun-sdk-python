"""Tests for the minimal-boilerplate error helpers (``nullrun.handle``
``nullrun.guarded``).

Contract:

* Both translate any:class:`nullrun.NullRunError` into a single
  ``print(format_user_message(exc), file=sys.stderr)`` and then
  ``sys.exit(1)``.
*:class:`nullrun.WorkflowKilledInterrupt` (BaseException) propagates
  unchanged — kill must not be swallowed into a graceful exit.
* Non-NullRun exceptions also propagate unchanged so the user's own
  bugs surface as honest tracebacks.
* No runtime is required — these helpers work without
  ``nullrun.init ``.
"""
from __future__ import annotations

import pytest

import nullrun
from nullrun import guarded, handle
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
    """``WorkflowKilledInterrupt`` is BaseException — must NOT be caught."""
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


def test_guarded_decorator_catches_and_exits(monkeypatch, capsys):
    """``@guarded`` translates NullRunError into sys.exit(1)."""
    exits = []

    def fake_exit(code):
        exits.append(code)
        raise SystemExit(code)

    monkeypatch.setattr("sys.exit", fake_exit)

    @guarded
    def boom():
        raise NullRunError("something broke", error_code="NR-B002")

    with pytest.raises(SystemExit):
        boom()

    assert exits == [1]
    captured = capsys.readouterr()
    # NR-B002 maps to the "service is temporarily unavailable" wording.
    assert "temporarily unavailable" in captured.err.lower()


def test_guarded_returns_value_on_success(monkeypatch):
    """``@guarded`` returns the wrapped function's value when nothing fails."""
    monkeypatch.setattr("sys.exit", lambda c: pytest.fail("sys.exit was called"))

    @guarded
    def add(a, b):
        return a + b

    assert add(2, 3) == 5


def test_guarded_propagates_workflow_killed(monkeypatch):
    """The kill signal still propagates through the decorator."""
    monkeypatch.setattr("sys.exit", lambda c: pytest.fail("sys.exit was called"))

    @guarded
    def boom():
        raise WorkflowKilledInterrupt("wf-7", "killed via API")

    with pytest.raises(WorkflowKilledInterrupt):
        boom()


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
    """``handle`` / ``guarded`` must not depend on a runtime."""
    # If handle pulled in the runtime, importing this module would have
    # raised during the prior tests. Smoke-test the import path here.
    assert callable(handle)
    assert callable(guarded)
    assert callable(nullrun.handle)
    assert callable(nullrun.guarded)
    assert callable(nullrun.init_or_die)


# ---------------------------------------------------------------------------
# init_or_die
# ---------------------------------------------------------------------------

class _FakeNoopRuntime:
    """Sentinel returned by a stubbed init. init_or_die should pass
    it through unchanged."""


def test_init_or_die_returns_runtime(monkeypatch):
    """On success, ``init_or_die`` returns whatever ``init()`` returned."""
    sentinel = _FakeNoopRuntime()

    def fake_init(**kwargs):
        assert kwargs["api_key"] == "nr_live_test"
        return sentinel

    monkeypatch.setattr("nullrun.init", fake_init)
    monkeypatch.setattr("sys.exit", lambda c: pytest.fail("sys.exit was called"))

    result = nullrun.init_or_die(api_key="nr_live_test")
    assert result is sentinel


def test_init_or_die_catches_missing_api_key(monkeypatch, capsys):
    """NR-C001 from init() → catalog user-message + sys.exit(1)."""
    from nullrun.breaker.exceptions import NullRunAuthenticationError

    def fake_init(**kwargs):
        raise NullRunAuthenticationError(
            "nullrun.init() requires an api_key.",
            error_code="NR-C001",
            user_action="Get an API key at https://app.nullrun.io/settings/api-keys",
        )

    exits = []

    def fake_exit(code):
        exits.append(code)
        raise SystemExit(code)

    monkeypatch.setattr("nullrun.init", fake_init)
    monkeypatch.setattr("sys.exit", fake_exit)

    with pytest.raises(SystemExit):
        nullrun.init_or_die(api_key=None)

    captured = capsys.readouterr()
    assert "configuration issue" in captured.err.lower()
    assert exits == [1]


def test_init_or_die_propagates_unexpected(monkeypatch):
    """Non-NullRun exceptions from init() propagate — not handled."""
    def fake_init(**kwargs):
        raise ValueError("totally unrelated bug")

    monkeypatch.setattr("nullrun.init", fake_init)
    monkeypatch.setattr("sys.exit", lambda c: pytest.fail("sys.exit was called"))

    with pytest.raises(ValueError):
        nullrun.init_or_die(api_key="nr_live_test")


def test_init_or_die_exit_code_kwarg(monkeypatch, capsys):
    """``init_or_die(exit_code=42)`` honours the override."""
    from nullrun.breaker.exceptions import NullRunAuthenticationError

    def fake_init(**kwargs):
        raise NullRunAuthenticationError("no key", error_code="NR-C001")

    exits = []

    def fake_exit(code):
        exits.append(code)
        raise SystemExit(code)

    monkeypatch.setattr("nullrun.init", fake_init)
    monkeypatch.setattr("sys.exit", fake_exit)

    with pytest.raises(SystemExit):
        nullrun.init_or_die(api_key=None, exit_code=42)

    assert exits == [42]