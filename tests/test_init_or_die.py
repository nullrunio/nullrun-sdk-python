"""Tests for ``nullrun.init_or_die`` — the fail-fast wrapper around
``nullrun.init()``.

Contract:

* On success, returns whatever ``init()`` returns (the runtime
  singleton).
* Catches :class:`nullrun.NullRunError` raised by ``init()`` — prints
  the four-line developer report to stderr and exits with ``1`` (or
  the ``exit_code`` override).
* Non-NullRun exceptions propagate unchanged.
"""
from __future__ import annotations

import pytest

import nullrun
from nullrun.breaker.exceptions import NullRunAuthenticationError


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
