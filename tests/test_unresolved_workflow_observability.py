"""tests/test_unresolved_workflow_observability.py — a skipped gate must
be distinguishable from a passing one.

Audit 2026-09-30, findings F5/F6 (follow-up to RUN_ID 20260929T1338).

The defect
----------
Two pre-flight gates no-op when no workflow can be resolved:

  * ``check_control_plane`` — the kill/pause gate
  * ``check_workflow_budget`` — the budget pre-flight

Both are CORRECT to no-op. ``_resolve_workflow_id`` returns None only
for an API key that was never workflow-bound, and a never-bound key
legitimately has no control-plane state and no per-workflow budget.
Raising would break that documented configuration.

What was wrong is that the no-op was completely silent. Consider a
key whose 1:1 workflow binding is lost — a bad migration, a restored
backup, the wrong key. Both gates stop running, and the only symptom
is an agent that ignores the dashboard and spends without a budget,
with nothing in the logs, nothing in metrics, and no error. A working
deployment and a silently-ungated one look identical.

The fix keeps the behaviour and makes the skip countable. These tests
pin that, and — more importantly — pin that the skip does NOT become
an exception, because "observable" must not quietly turn into
"breaks the never-bound-key case".
"""

from __future__ import annotations

import pytest

from nullrun import runtime as rt
from nullrun.breaker.exceptions import TransportErrorSource  # noqa: F401


class _RecordingMetrics:
    """Stands in for the metrics module, recording every counter name."""

    def __init__(self, real):
        self._real = real
        self.calls: list[str] = []

    def inc_runtime(self, name, *a, **k):
        self.calls.append(name)
        return self._real.inc_runtime(name, *a, **k)

    def __getattr__(self, item):
        return getattr(self._real, item)


@pytest.fixture
def counted(monkeypatch):
    """Count every runtime metric without discarding the real ones."""
    rec = _RecordingMetrics(rt.metrics)
    monkeypatch.setattr(rt, "metrics", rec)
    return rec


@pytest.fixture
def unbound_runtime(monkeypatch):
    """A runtime whose workflow can never resolve.

    Only `_resolve_workflow_id` is stubbed. Every other attribute is
    the real implementation, so the test exercises the real control
    flow up to the skip rather than a hand-built mock path.
    """
    stub = object.__new__(rt.NullRunRuntime)
    monkeypatch.setattr(
        rt.NullRunRuntime, "_resolve_workflow_id", lambda self, *a, **k: None
    )
    return stub


class TestControlPlaneSkip:
    def test_skip_is_counted(self, unbound_runtime, counted):
        unbound_runtime.check_control_plane(None)
        assert "control_plane_no_workflow_total" in counted.calls, (
            "a skipped kill/pause gate must be countable, or a lost key "
            "binding is indistinguishable from normal operation"
        )

    def test_skip_does_not_raise(self, unbound_runtime):
        """Observable must not become fatal.

        A never-bound API key is a supported configuration. If this
        raises, the observability fix has broken it.
        """
        unbound_runtime.check_control_plane(None)  # must not raise

    def test_skip_does_not_poll_the_network(self, unbound_runtime, monkeypatch):
        """The no-op must stay a no-op, not a request with no consumer."""

        def _boom(*a, **k):
            raise AssertionError("control plane polled with no workflow")

        monkeypatch.setattr(
            rt.NullRunRuntime, "_fetch_remote_state", _boom, raising=True
        )
        unbound_runtime.check_control_plane(None)


class TestBudgetPreflightSkip:
    def test_skip_is_counted(self, unbound_runtime, counted):
        unbound_runtime.check_workflow_budget()
        assert "budget_preflight_no_workflow_total" in counted.calls, (
            "a skipped budget pre-flight must be countable"
        )

    def test_skip_does_not_raise(self, unbound_runtime):
        unbound_runtime.check_workflow_budget()  # must not raise

    def test_entered_and_skipped_are_distinguishable(
        self, unbound_runtime, counted
    ):
        """The operator needs to tell 'ran and allowed' from 'never ran'.

        `check_calls` is bumped on entry to the pre-flight, and the new
        counter is bumped only when it is skipped. Both present =
        "the gate was reached but had no workflow"; only `check_calls`
        = "the gate ran".
        """
        unbound_runtime.check_workflow_budget()
        assert "check_calls" in counted.calls
        assert "budget_preflight_no_workflow_total" in counted.calls


class TestMetricsNeverGate:
    def test_new_counter_failure_does_not_break_the_skip(
        self, unbound_runtime, monkeypatch
    ):
        """A failure while recording the skip must not propagate.

        The skip is already a no-op; raising while counting it would
        convert "no workflow bound" into "your agent is broken", which
        is strictly worse than what it replaced. This is why the new
        counters are wrapped, matching the `skip_budget_*` counters
        directly above them in the same function.

        Scoped to the two counters this change adds. The pre-existing
        `check_calls` increment earlier in `check_workflow_budget` is
        unguarded, but it is not this change's to assert about, and
        `inc_runtime` is an in-memory increment under a lock that has
        no realistic failure mode in the first place.
        """
        new_counters = {
            "control_plane_no_workflow_total",
            "budget_preflight_no_workflow_total",
        }

        class _PartiallyBrokenMetrics:
            def inc_runtime(self, name, *a, **k):
                if name in new_counters:
                    raise RuntimeError("counter write failed")
                return None

            def __getattr__(self, item):
                return lambda *a, **k: None

        monkeypatch.setattr(rt, "metrics", _PartiallyBrokenMetrics())
        unbound_runtime.check_control_plane(None)  # must not raise
        unbound_runtime.check_workflow_budget()  # must not raise


class TestNoOverCorrection:
    def test_observable_skip_does_not_warn_per_call(self, unbound_runtime, caplog):
        """Debug level, not warning.

        For a legitimately never-bound key this branch runs on EVERY
        protected call. Logging it at warning would turn a supported
        configuration into a wall of noise, which is how real signals
        get ignored.
        """
        import logging

        with caplog.at_level(logging.DEBUG, logger="nullrun.runtime"):
            unbound_runtime.check_control_plane(None)
        records = [r for r in caplog.records if r.name == "nullrun.runtime"]
        assert records, "the skip should still be debug-logged"
        assert all(r.levelno == logging.DEBUG for r in records), (
            "the unresolved-workflow skip must not log above DEBUG — it "
            "fires on every call for a legitimately unbound key"
        )
