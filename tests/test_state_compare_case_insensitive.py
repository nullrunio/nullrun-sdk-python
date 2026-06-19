"""Regression tests for S-4: case-insensitive state compare in
``NullRunRuntime.check_control_plane``.

Why this exists. Per ``analyze.md`` §11.6 the wire-format ``state``
value can drift across backend versions — `as_pascal_case()`
emits ``"Paused"`` / ``"Killed"`` today, but a regression to
``"PAUSED"`` / ``"KILLED"`` (the historical UPPERCASE DB format)
would silently bypass the SDK-side kill/pause detection. The
pre-fix code did exact ``state == "Paused"`` / ``state == "Killed"``
comparisons.

The fix normalises ``state.lower()`` before the membership test
so the SDK survives any casing drift without needing a coordinated
backend change. Backend already emits PascalCase per
``handlers.rs:9258``; this is defensive.
"""
from __future__ import annotations

import pytest

from nullrun.breaker.exceptions import WorkflowKilledInterrupt, WorkflowPausedException
from nullrun.runtime import NullRunRuntime


@pytest.fixture
def runtime():
    rt = NullRunRuntime(
        api_key="test-key-12345678",
        _test_mode=True,
        polling=False,
    )
    yield rt
    try:
        rt.shutdown()
    except Exception:
        pass


def _seed_remote_state(rt: NullRunRuntime, state_value) -> None:
    """Push a state dict straight into the in-memory cache via the
    thread-safe helper. We bypass HTTP poll entirely."""
    rt._set_remote_state("wf-test", {"state": state_value, "reason": "test"})


class TestPascalCase:
    """The current backend contract — PascalCase via ``as_pascal_case()``."""

    def test_killed_pascal_case_raises(self, runtime):
        _seed_remote_state(runtime, "Killed")
        with pytest.raises(WorkflowKilledInterrupt):
            runtime.check_control_plane("wf-test")

    def test_paused_pascal_case_raises(self, runtime):
        _seed_remote_state(runtime, "Paused")
        with pytest.raises(WorkflowPausedException):
            runtime.check_control_plane("wf-test")


class TestUppercaseDrift:
    """If a backend regression emits UPPERCASE (the historical DB
    format), the SDK must still raise — the case-insensitive
    compare catches the drift."""

    def test_killed_uppercase_raises(self, runtime):
        _seed_remote_state(runtime, "KILLED")
        with pytest.raises(WorkflowKilledInterrupt):
            runtime.check_control_plane("wf-test")

    def test_paused_uppercase_raises(self, runtime):
        _seed_remote_state(runtime, "PAUSED")
        with pytest.raises(WorkflowPausedException):
            runtime.check_control_plane("wf-test")


class TestLowercaseDrift:
    """If a backend regression emits lowercase, the SDK must still
    raise. (Same code path as Uppercase via .lower(), but exercises
    a separate input variant.)"""

    def test_killed_lowercase_raises(self, runtime):
        _seed_remote_state(runtime, "killed")
        with pytest.raises(WorkflowKilledInterrupt):
            runtime.check_control_plane("wf-test")

    def test_paused_lowercase_raises(self, runtime):
        _seed_remote_state(runtime, "paused")
        with pytest.raises(WorkflowPausedException):
            runtime.check_control_plane("wf-test")


class TestNormalState:
    """Anything that does NOT reduce to ``paused`` / ``killed`` must
    be a silent pass-through — including the default ``Normal``,
    explicit ``"normal"``, ``"running"``, ``"flagged"``, etc."""

    def test_normal_pascal_does_not_raise(self, runtime):
        _seed_remote_state(runtime, "Normal")
        runtime.check_control_plane("wf-test")  # no raise

    def test_normal_lowercase_does_not_raise(self, runtime):
        _seed_remote_state(runtime, "normal")
        runtime.check_control_plane("wf-test")  # no raise

    def test_running_does_not_raise(self, runtime):
        _seed_remote_state(runtime, "Running")
        runtime.check_control_plane("wf-test")  # no raise

    def test_unknown_does_not_raise(self, runtime):
        _seed_remote_state(runtime, "Tripped")  # not in the KILL/PAUSE set
        runtime.check_control_plane("wf-test")  # no raise