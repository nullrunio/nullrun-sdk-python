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


class TestOverflowDropsOldest:
    """The CB-OPEN re-queue must enforce `max_buffer_size` and drop
    the oldest events from the batch (not from the buffer) when the
    batch is larger than the limit. The pre-fix code was a no-op."""

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

    def test_batch_larger_than_max_buffer_drops_oldest(self, transport):
        """If `len(batch) > max_buffer_size`, the oldest events in
        the batch are dropped before re-queuing. (Pre-fix: this was
        a no-op because the buffer was already empty.)"""
        transport.config = FlushConfig(batch_size=200, max_buffer_size=10)
        for i in range(20):
            transport._buffer.append({"event_id": f"e{i:02d}"})
        with patch.object(
            transport._circuit_breaker, "call", side_effect=BreakerTransportError("open")
        ):
            transport._do_flush_locked()
        # The batch (20) was larger than max_buffer_size (10), so
        # 10 oldest events are dropped. The remaining 10 are
        # re-queued. The survivors are the LAST 10 events.
        assert len(transport._buffer) == 10
        survivors = [e["event_id"] for e in transport._buffer]
        assert survivors == [f"e{i:02d}" for i in range(10, 20)]


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
            return Transport.SendResult(
                accepted_event_ids=[e.get("event_id") for e in batch]
            )

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

            threads = [
                threading.Thread(target=worker, args=(t,)) for t in range(n_threads)
            ]
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
            f"Lost {len(missing)} events under concurrent track/flush; "
            f"first 10: {missing[:10]}"
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
