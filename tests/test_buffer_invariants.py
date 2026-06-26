"""Regression tests for the P0-0.3 fix: buffer mutation invariants.

Why this exists. The pre-fix `Transport._do_flush_locked` had three
distinct buffer-mutation bugs:

1. **Re-binding the attribute** — `self._buffer = self._buffer[overflow:]`
   replaced the list with a new object. Any code holding a reference
   to the old list (e.g. an in-flight `track()` call) would silently
   append to dead memory. The new contract uses in-place slice
   (`del self._buffer[:]`) so the attribute is never re-bound.

2. **CB-OPEN re-queue was effectively a no-op** — the `available_space`
   check ran AFTER `self._buffer.clear()`, so the buffer was always
   empty and the overflow slice was dead code. Under sustained
   backend outage, the buffer grew unboundedly. The fix checks the
   batch's own size against `max_buffer_size`.

3. **No single drain point** — the buffer was read, copied, cleared
   in three separate lines in `track()`'s body, with TOCTOU race
   windows between copy and clear. The fix centralizes this through
   a single `_drain_batch()` helper.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

from nullrun.breaker.exceptions import BreakerTransportError
from nullrun.transport import FlushConfig, Transport


@pytest.fixture
def transport():
    t = Transport(api_url="https://api.test.nullrun.io", api_key="test-key-12345678")
    # Stop the background flush thread so the fixture teardown
    # (which calls `t.stop()`) doesn't try to send leftover events
    # to a real network. Each test that needs flushing must start
    # the thread explicitly OR use `_do_flush_locked` directly.
    t._running = False
    if t._flush_thread and t._flush_thread.is_alive():
        t._flush_thread.join(timeout=1.0)
    yield t
    # Teardown: ensure no leftover events, close client.
    t._buffer.clear()
    t._in_flight.clear()
    t._client.close()


class TestBufferIsInPlace:
    """`_drain_batch` must not rebind `_buffer` to a new list — that
    breaks any in-flight `track()` call holding a reference."""

    def test_drain_batch_returns_snapshot_and_clears(self, transport):
        for i in range(5):
            transport._buffer.append({"event_id": f"e{i}"})
        with transport._lock:
            batch = transport._drain_batch()
        assert batch is not None
        assert len(batch) == 5
        assert len(transport._buffer) == 0

    def test_drain_batch_preserves_list_identity(self, transport):
        """After `_drain_batch`, `id(self._buffer)` is unchanged.
        This is the property the in-place `del self._buffer[:]`
        guarantees — a `self._buffer = self._buffer[:]` would break it."""
        for i in range(5):
            transport._buffer.append({"event_id": f"e{i}"})
        original_id = id(transport._buffer)
        with transport._lock:
            transport._drain_batch()
        assert id(transport._buffer) == original_id
        assert transport._buffer == []

    def test_drain_batch_on_empty_buffer_returns_none(self, transport):
        with transport._lock:
            batch = transport._drain_batch()
        assert batch is None


class TestOverflowDropsNewest:
    """The CB-OPEN re-queue must enforce `max_buffer_size` and drop
    the NEWEST events from the batch (not from the buffer) when the
    batch is larger than the limit. Pre-fix this was a no-op
    (the buffer was already empty by the time the overflow check
    ran); then it dropped OLDEST, which broke monthly cost
    rollups (plan §10 P0-4). Critical control-plane events
    (state_change / kill_received / etc.) are preserved."""

    def test_batch_within_max_buffer_size_is_kept_verbatim(self, transport):
        """If `len(batch) <= max_buffer_size`, no events are dropped."""
        transport.config = FlushConfig(batch_size=10, max_buffer_size=100)
        for i in range(50):
            transport._buffer.append({"event_id": f"e{i}"})
        with patch.object(
            transport._circuit_breaker, "call", side_effect=BreakerTransportError("open")
        ):
            transport._do_flush_locked()
        # All 50 events are re-queued (no drop).
        assert len(transport._buffer) == 50

    def test_batch_larger_than_max_buffer_drops_newest(self, transport):
        """If `len(batch) > max_buffer_size`, the NEWEST events in
        the batch are dropped before re-queuing. The survivors are
        the FIRST events (the cost-audit invariant from plan §10
        P0-4: oldest events are most valuable)."""
        transport.config = FlushConfig(batch_size=200, max_buffer_size=10)
        for i in range(20):
            transport._buffer.append({"event_id": f"e{i:02d}"})
        with patch.object(
            transport._circuit_breaker, "call", side_effect=BreakerTransportError("open")
        ):
            transport._do_flush_locked()
        # The batch (20) was larger than max_buffer_size (10), so
        # 10 newest events are dropped. The survivors are the FIRST
        # 10 events — these are the ones we'd want a billing
        # investigator to be able to reconstruct.
        assert len(transport._buffer) == 10
        survivors = [e["event_id"] for e in transport._buffer]
        assert survivors == [f"e{i:02d}" for i in range(0, 10)], (
            f"survivors should be the OLDEST 10 events (cost-audit invariant); got {survivors}"
        )

    def test_critical_state_change_events_are_preserved(self, transport):
        """Even when overflow would force a drop, state_change /
        kill_received / policy_invalidated / key_rotated events are
        kept regardless of position. The dashboard's KILL switch
        has to land even under sustained backend outage (plan
        §11.4 P0-4 recommendation)."""
        transport.config = FlushConfig(batch_size=200, max_buffer_size=4)
        # 6 llm_call + 1 state_change at the very end.
        events = [
            {"event_id": "e00", "type": "llm_call"},
            {"event_id": "e01", "type": "llm_call"},
            {"event_id": "e02", "type": "llm_call"},
            {"event_id": "e03", "type": "llm_call"},
            {"event_id": "e04", "type": "llm_call"},
            {"event_id": "e05", "type": "llm_call"},
            {"event_id": "e06", "type": "state_change"},  # NEWEST, critical
        ]
        for e in events:
            transport._buffer.append(e)

        with patch.object(
            transport._circuit_breaker, "call", side_effect=BreakerTransportError("open")
        ):
            transport._do_flush_locked()

        survivors = [e["event_id"] for e in transport._buffer]
        # The 1 critical event MUST survive even at the cost of a brief
        # overshoot above max_buffer_size.
        assert "e06" in survivors, (
            f"critical state_change event dropped — kill switch is "
            f"silently broken under CB OPEN. survivors: {survivors}"
        )

    def test_oldest_non_critical_kept_when_mixed(self, transport):
        """Mixed batch: oldest critical, newest non-critical. The
        critical survives, AND the oldest non-critical survives
        (cost-audit invariant — we drop newest, keep oldest)."""
        transport.config = FlushConfig(batch_size=200, max_buffer_size=3)
        events = [
            {"event_id": "e00", "type": "llm_call"},  # OLDEST non-critical
            {"event_id": "e01", "type": "llm_call"},
            {"event_id": "e02", "type": "llm_call"},
            {"event_id": "e03", "type": "state_change"},  # critical, mid-batch
            {"event_id": "e04", "type": "llm_call"},  # NEWEST
        ]
        for e in events:
            transport._buffer.append(e)
        with patch.object(
            transport._circuit_breaker, "call", side_effect=BreakerTransportError("open")
        ):
            transport._do_flush_locked()

        survivors = [e["event_id"] for e in transport._buffer]
        # e00 (oldest) and e03 (critical) MUST survive.
        # e04 (newest, non-critical) MUST be dropped.
        assert "e00" in survivors, "oldest non-critical was dropped — cost audit broken"
        assert "e03" in survivors, "critical state_change was dropped — kill switch broken"
        assert "e04" not in survivors, "newest non-critical should be dropped first"


class TestConcurrentTrackDuringFlush:
    """A `track()` call racing with `_do_flush_locked` must not lose
    events. The pre-fix code had TOCTOU windows between
    `_buffer[:]` and `_buffer.clear()`."""

    def test_concurrent_track_does_not_lose_events(self, transport):
        """Spawn N threads each appending M events. After all threads
        finish, every event_id must appear in either the in-memory
        buffer, the in-flight dict, or the mock send."""
        transport.config = FlushConfig(batch_size=5, max_buffer_size=100_000)

        # Patch `_send_batch_with_retry_info` to record sent events.
        sent_ids: list[str] = []

        def _capture_send(batch, *args, **kwargs):
            sent_ids.extend(e["event_id"] for e in batch)
            return Transport.SendResult(accepted_event_ids=[e.get("event_id") for e in batch])

        with patch.object(
            transport,
            "_send_batch_with_retry_info",
            side_effect=_capture_send,
        ):
            # Make the CB always pass.
            transport._circuit_breaker.call = lambda fn: fn()

            n_threads = 4
            n_per_thread = 25
            barrier = threading.Barrier(n_threads)

            def worker(tid: int) -> None:
                barrier.wait()
                for i in range(n_per_thread):
                    transport.track({"event_id": f"t{tid}-e{i}"})

            threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # Final flush to drain any remaining events. Stop the
            # background thread first to avoid races.
            transport._running = False
            if transport._flush_thread and transport._flush_thread.is_alive():
                transport._flush_thread.join(timeout=2.0)
            transport._do_flush()

        # Total events: n_threads * n_per_thread = 4 * 25 = 100.
        # Every event must have been either sent or be in the
        # remaining buffer/in-flight.
        sent_set = set(sent_ids)
        leftover_ids = {
            e.get("event_id")
            for e in list(transport._buffer) + list(transport._in_flight.values())
            if e.get("event_id")
        }
        all_seen = sent_set | leftover_ids

        # No event should be silently lost.
        missing = []
        for tid in range(n_threads):
            for i in range(n_per_thread):
                eid = f"t{tid}-e{i}"
                if eid not in all_seen:
                    missing.append(eid)
        assert not missing, (
            f"Lost {len(missing)} events under concurrent track/flush; first 10: {missing[:10]}"
        )


class TestCircuitOpenRedoesNotDuplicate:
    """When the circuit opens, a re-queued batch must not be sent
    twice. The pre-fix code had a subtle double-extend on the
    async path; this is the sync-path analog."""

    def test_circuit_open_does_not_double_emit(self, transport):
        transport.config = FlushConfig(batch_size=10, max_buffer_size=100)

        for i in range(5):
            transport._buffer.append({"event_id": f"e{i}"})

        with patch.object(
            transport._circuit_breaker, "call", side_effect=BreakerTransportError("open")
        ):
            transport._do_flush_locked()

        # After CB-OPEN: buffer contains the 5 re-queued events,
        # none of them sent (since the send was skipped).
        assert len(transport._buffer) == 5
        assert transport._in_flight == {}
