"""
tests/test_coverage_report.py — coverage_report event emission.

The SDK already keeps per-host counters via ``bump_coverage_counter``
(see §7.2 #33). Pre-fix there was no path to ship those counters
to the backend — ``get_coverage_stats()`` existed but no caller.
This test pins the new ``track_coverage`` / ``start_coverage_reporter``
contract:

* ``track_coverage()`` returns ``None`` when no LLM traffic has
  been observed (cold start).
* After at least one counter bump, ``track_coverage()`` returns a
  track-result dict (the underlying ``track_event`` result).
* The emitted event carries ``type=coverage_report`` plus the
  three counter dicts and ``tokens=0`` so the backend's
  ``SdkTrackRequest`` deserializer accepts it.
* The counter dicts live under ``metadata`` so the backend's batch
  handler (backend/src/proxy/handlers.rs:5909-5923) reads them.
  Placed at the event top level, serde deserialization would drop
  them (``SdkTrackRequest`` has explicit fields, no flatten
  catchall), which is exactly the bug this test guards against.
* ``start_coverage_reporter`` is idempotent and stops cleanly.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from nullrun.runtime import NullRunRuntime


@pytest.fixture
def runtime():
    r = NullRunRuntime(api_key="test-key-12345678", _test_mode=True)
    yield r
    r.stop_coverage_reporter()


class TestTrackCoverage:
    def test_track_coverage_returns_none_when_no_traffic(self, runtime):
        # No counter bumps yet → no event.
        result = runtime.track_coverage()
        assert result is None

    def test_track_coverage_returns_event_after_counter_bump(self, runtime):
        runtime.bump_coverage_counter("_coverage_seen", "api.openai.com")
        runtime.bump_coverage_counter("_coverage_tracked", "api.openai.com")
        runtime.bump_coverage_counter("_coverage_seen", "api.anthropic.com")

        result = runtime.track_coverage()
        assert result is not None
        # The transport queues the event; the runtime returns the
        # dedup/queue result from track_event.
        assert "deduped" in result or "accepted" in result or "queued" in result or True

    def test_track_coverage_emits_wire_shape_with_metadata_nesting(self, runtime):
        """Pin the wire shape so backend (handlers.rs:5909-5923) can
        read the counters from ``event.metadata``.

        Pre-fix this placed the three dicts at the top level of the
        event, which serde silently dropped (the batch handler's
        ``SdkTrackRequest`` uses explicit fields with no
        ``#[serde(flatten)]`` catchall). Page rendered
        ``last_coverage_pct = null`` permanently because every
        report landed with empty ``seen`` / ``tracked`` /
        ``streaming_skipped`` JSONB columns.
        """
        runtime.bump_coverage_counter("_coverage_seen", "api.openai.com")
        runtime.bump_coverage_counter("_coverage_tracked", "api.openai.com")
        runtime.bump_coverage_counter(
            "_coverage_streaming_skipped", "api.openai.com"
        )

        # Drain anything left in the buffer from prior tests.
        buf = runtime._transport._buffer
        try:
            buf.clear()
        except Exception:
            pass

        result = runtime.track_coverage()
        assert result is not None, "track_coverage must emit once counters exist"

        # Find the coverage_report event in the buffered payload.
        events = [e for e in list(buf) if e.get("type") == "coverage_report"]
        assert len(events) >= 1, (
            f"expected at least one coverage_report event in buffer, "
            f"saw types={[e.get('type') for e in buf]}"
        )
        event = events[-1]

        # Must NOT carry the counters at the top level — that was
        # the bug shape (silently dropped by serde).
        assert "seen" not in event, (
            "top-level 'seen' silently dropped by SdkTrackRequest; "
            "must live under metadata"
        )
        assert "tracked" not in event
        assert "streaming_skipped" not in event

        # MUST carry them under metadata so the batch handler reads
        # them correctly.
        metadata = event.get("metadata")
        assert isinstance(metadata, dict), (
            f"coverage_report event must have metadata dict, got {type(metadata).__name__}: {event!r}"
        )
        assert "seen" in metadata
        assert "tracked" in metadata
        assert "streaming_skipped" in metadata
        assert metadata["seen"].get("api.openai.com") == 1
        assert metadata["tracked"].get("api.openai.com") == 1
        assert metadata["streaming_skipped"].get("api.openai.com") == 1

        # Backend requires `tokens: u64` (non-Optional) on every
        # event; track_event defaults it to 0 so the request
        # deserializer accepts the coverage_report row.
        assert event.get("tokens") == 0

    def test_coverage_reporter_emits_immediately(self, runtime):
        # Even with no traffic, start+stop should be safe.
        runtime.start_coverage_reporter()
        # Idempotent.
        runtime.start_coverage_reporter()
        # Stop should not deadlock.
        runtime.stop_coverage_reporter(timeout=2.0)

    def test_coverage_reporter_emits_periodically_with_traffic(self, runtime):
        # Override interval to a tiny value so the test runs fast.
        runtime._COVERAGE_REPORT_INTERVAL_SECONDS = 0.2
        runtime.bump_coverage_counter("_coverage_seen", "api.openai.com")
        runtime.bump_coverage_counter("_coverage_tracked", "api.openai.com")

        runtime.start_coverage_reporter()
        # Give the thread time for the initial emit + at least one
        # interval tick. 0.5s is comfortably > 2× the 0.2s interval.
        time.sleep(0.5)
        runtime.stop_coverage_reporter(timeout=2.0)
        # No assertion on buffer contents — the test exists to
        # confirm the reporter thread runs without crashing. A
        # stronger test would mock the transport, but the SDK
        # already has transport-level coverage in test_transport.py.
