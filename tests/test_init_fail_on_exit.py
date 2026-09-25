"""Tests for ``nullrun.init(fail_on_exit=True)`` — the CLI fail-fast path.

Contract:

* On success, returns whatever the runtime ``init()`` returns.
* On ``NullRunAuthenticationError`` (e.g. NR-C001 missing api_key),
  prints the same four-line developer report that ``handle()`` uses
  and ``sys.exit(1)``.
* The default ``init()`` (``fail_on_exit=False``) still raises — the
  library-friendly path.
"""
from __future__ import annotations

import pytest

import nullrun
from nullrun.breaker.exceptions import NullRunAuthenticationError


def test_init_fail_on_exit_does_not_exit_on_happy_path(monkeypatch):
    """``fail_on_exit=True`` only triggers on config errors, not on
    successful init. Stub ``NullRunRuntime`` so we never touch the
    network and just verify ``sys.exit`` is not called."""
    from nullrun.runtime import NullRunRuntime

    class _FakeRuntime:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.api_url = kwargs.get("api_url", "")
            self.api_key = kwargs.get("api_key", "")
            self.organization_id = "fake-org"
            self.workflow_id = "fake-wf"
            self._instance = self

        def shutdown(self, flush=True):
            pass

    monkeypatch.setattr(NullRunRuntime, "__init__", _FakeRuntime.__init__)
    monkeypatch.setattr(NullRunRuntime, "shutdown", _FakeRuntime.shutdown)
    # Skip the capability probe + auto_instrument (both touch network)
    monkeypatch.setattr(
        "nullrun.capabilities.probe_capabilities", lambda url: None
    )
    monkeypatch.setattr(
        "nullrun.instrumentation.auto.auto_instrument", lambda rt: None
    )

    monkeypatch.setenv("NULLRUN_API_KEY", "nr_live_test")
    monkeypatch.setattr("sys.exit", lambda c: pytest.fail("sys.exit was called"))

    rt = nullrun.init(fail_on_exit=True)
    assert rt is not None


def test_init_fail_on_exit_exits_on_missing_api_key(monkeypatch, capsys):
    """NR-C001 from init() → catalog user-message + sys.exit(1)."""
    monkeypatch.delenv("NULLRUN_API_KEY", raising=False)

    exits = []

    def fake_exit(code):
        exits.append(code)
        raise SystemExit(code)

    monkeypatch.setattr("sys.exit", fake_exit)

    with pytest.raises(SystemExit):
        nullrun.init(api_key=None, fail_on_exit=True)

    captured = capsys.readouterr()
    assert "configuration issue" in captured.err.lower()
    assert exits == [1]


def test_init_default_raises_does_not_exit(monkeypatch):
    """The default ``init()`` (without ``fail_on_exit=True``) still
    raises on missing api_key — fail_on_exit is opt-in."""
    monkeypatch.delenv("NULLRUN_API_KEY", raising=False)
    monkeypatch.setattr("sys.exit", lambda c: pytest.fail("sys.exit was called"))

    with pytest.raises(NullRunAuthenticationError):
        nullrun.init(api_key=None)
