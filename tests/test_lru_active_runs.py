"""
Regression test for plan item S-9 / P1-3: NullRunCallback._active_runs
must be bounded by FIFO eviction.

Pre-fix, ``_active_runs`` was a plain ``dict[str, SpanContext]``. If
``on_chain_start`` ran without a matching ``on_chain_end`` (the chain
body raised before the end hook fired — common in error-heavy
workloads), the SpanContext sat in the dict forever. Long-running
services saw a slow memory leak proportional to error rate.

Post-fix the dict is an ``OrderedDict`` with FIFO eviction at
``_ACTIVE_RUNS_MAX`` (4096). When full, the oldest-inserted run_id is
evicted and a WARNING is logged. ``on_*_end`` for an evicted run_id
becomes a no-op (the lookup misses, which is the same behaviour as
the pre-fix code for any run_id that was never registered — silent
no-op is the established contract).
"""

import logging
from collections import OrderedDict
from unittest.mock import MagicMock

import pytest

from nullrun.instrumentation.langgraph import (
    _ACTIVE_RUNS_MAX,
    NullRunCallback,
)
from nullrun.tracing import SpanContext, create_root_span


@pytest.fixture
def callback():
    """A fresh NullRunCallback with a MagicMock runtime so we don't
    touch the real NullRunRuntime.get_instance() singleton path."""
    return NullRunCallback(runtime=MagicMock())


def test_active_runs_uses_ordered_dict(callback):
    """The internal container is an OrderedDict so we can pop
    insertion-order (FIFO). Using a plain dict would silently lose
    ordering guarantees on Python <3.7."""
    assert isinstance(callback._active_runs, OrderedDict)


def test_register_inserts_at_end(callback):
    """Each ``_register_active_run`` call appends to the end of the
    OrderedDict — like a queue."""
    run_ids = []
    for i in range(3):
        run_id = f"run-{i}"
        ctx = create_root_span()
        callback._register_active_run(run_id, ctx)
        run_ids.append(run_id)
    assert list(callback._active_runs.keys()) == run_ids


def test_active_runs_evicts_oldest_at_cap(callback):
    """Pushing past the cap must evict the oldest entry. The cap is
    documented in the plan as 4096; we don't use the production cap
    value here to keep the test fast — instead we manipulate
    ``_active_runs_max`` directly."""
    # Inject a small cap for this test only.
    callback._active_runs_max = 5

    for i in range(5):
        callback._register_active_run(f"run-{i}", create_root_span())
    assert len(callback._active_runs) == 5
    assert list(callback._active_runs.keys()) == [f"run-{i}" for i in range(5)]

    # 6th insert: evict run-0.
    callback._register_active_run("run-5", create_root_span())
    assert len(callback._active_runs) == 5
    assert "run-0" not in callback._active_runs
    assert list(callback._active_runs.keys()) == [f"run-{i}" for i in range(1, 6)]


def test_active_runs_eviction_logs_warning(callback, caplog):
    """When eviction happens, the operator must see a WARNING — this
    is the observability signal that ``on_*_end`` is silently
    becoming a no-op for some runs."""
    callback._active_runs_max = 2
    callback._register_active_run("a", create_root_span())
    callback._register_active_run("b", create_root_span())

    with caplog.at_level(logging.WARNING, logger="nullrun.instrumentation.langgraph"):
        callback._register_active_run("c", create_root_span())

    assert any("cap reached" in rec.message for rec in caplog.records), (
        f"expected cap-reached warning; got: {[r.message for r in caplog.records]}"
    )


def test_default_cap_matches_plan():
    """The production cap is 4096 (mirrors DEDUP_LRU_MAX in auto.py).
    Bumping this is a deliberate choice that should show up in code
    review, not an accidental drift."""
    assert _ACTIVE_RUNS_MAX == 4096


def test_end_run_for_evicted_id_is_silent_noop(callback):
    """When ``on_*_end`` fires for a run_id that was evicted, the
    callback must not crash and must not emit a span_end event with
    a stale SpanContext. This is the same behaviour the pre-fix code
    had for never-registered run_ids — preserved for BC."""
    callback._active_runs_max = 2
    callback._register_active_run("a", create_root_span())
    callback._register_active_run("b", create_root_span())
    callback._register_active_run("c", create_root_span())  # evicts "a"

    # End the evicted run_id. _end_run pops from _active_runs —
    # the missing key is a no-op, matching pre-fix behaviour for
    # never-registered ids.
    callback._end_run("a", error="something failed")
    # No span_end track_event call should have fired for the evicted run.
    callback.runtime.track_event.assert_not_called()


def test_end_run_for_present_id_emits_span_end(callback):
    """Sanity: the FIFO cap does not break the happy path. A run_id
    that was registered and ends cleanly must still emit span_end."""
    ctx = create_root_span()
    callback._register_active_run("ok", ctx)
    callback._end_run("ok")

    callback.runtime.track_event.assert_called_once()
    event = callback.runtime.track_event.call_args.kwargs
    assert event["event_type"] == "span_end"
    assert event["trace_id"] == ctx.trace_id
