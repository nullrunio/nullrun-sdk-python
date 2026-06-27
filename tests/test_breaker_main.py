"""Coverage padding for ``nullrun.breaker.__main__``.

The module exists so ``python -m nullrun.breaker`` exits cleanly
instead of failing with ``No module named nullrun.breaker.__main__``.
Containerized deployments that previously relied on the broken
entrypoint should call ``nullrun-doctor`` (see
``nullrun.toolbox.diagnostics``) for runtime checks.

Pinned by ``pyproject.toml::[tool.coverage.report].fail_under = 82`` —
without this test, the five statements in ``main()`` stay at 0% and
the suite trips the threshold by a hair.
"""
from __future__ import annotations

import io

import pytest

from nullrun.breaker.__main__ import main


def test_main_returns_zero_and_writes_helpful_message(capsys: pytest.CaptureFixture[str]) -> None:
    """``main()`` is informational, not an error: return code 0, the
    message goes to stderr (so it doesn't pollute the consumer's
    stdout pipe)."""
    rc = main()
    captured = capsys.readouterr()
    assert rc == 0
    # Message goes to stderr so a stdout pipe stays clean.
    assert captured.out == ""
    assert "nullrun-doctor" in captured.err
    assert "library module" in captured.err


def test_main_runs_under_dunder_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """Smoke: ``python -m nullrun.breaker`` path — exercise the
    ``if __name__ == "__main__":`` guard via ``runpy`` so the
    ``SystemExit`` branch is hit."""
    import runpy

    with pytest.raises(SystemExit) as info:
        runpy.run_module("nullrun.breaker.__main__", run_name="__main__")
    assert info.value.code == 0