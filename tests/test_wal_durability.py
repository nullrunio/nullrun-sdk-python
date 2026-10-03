"""Durability tests for the Transport WAL.

Why this exists. ``Transport._replay_from_wal`` unlinked each recovery file
*before* calling ``_do_flush``:

    for candidate in (f"{wal}.1", wal):
        ...read...
        os.remove(candidate)      # <-- deleted here
    if events:
        self._buffer.extend(events)
        self._do_flush()           # <-- flushed here

The docstring claimed "Both files are removed only after a successful
flush"; the code did the exact opposite. A crash between the unlink and a
successful flush lost the batch outright: the events were in neither the
WAL nor the buffer, and the server never saw them. Since the recovery path
only runs when the backend was unreachable at shutdown, the breaker being
still open at ``start()`` is the *likely* case, not the exotic one.

The fix makes recovery at-least-once: no file is unlinked before its events
are provably safe (accepted, or rewritten into ``.wal``). Re-delivery is
harmless because the backend dedups on ``event_id``, which is why every
event is guaranteed an id before it reaches disk.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from nullrun.breaker.exceptions import BreakerTransportError
from nullrun.transport import Transport

SendResult = Transport.SendResult  # nested dataclass, not a module-level name


@pytest.fixture
def transport(tmp_path, monkeypatch):
    monkeypatch.setenv("NULLRUN_WAL_PATH", str(tmp_path / "sdk.wal"))
    t = Transport(api_url="https://api.test.nullrun.io", api_key="test-key-12345678")
    yield t
    t._client.close()


def _write_wal(path: str, events: list[dict]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _read_wal(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _event(i: int) -> dict:
    return {"event_id": f"evt-{i}", "type": "llm_call", "cost_cents": 1}


# ---------------------------------------------------------------------------
# The core regression: the WAL must survive a flush that does not land.
# ---------------------------------------------------------------------------


def test_wal_survives_failed_flush(transport):
    """A flush that raises must NOT consume the recovery file.

    This is the exact loss the old code shipped: unlink ran before the
    flush, so a BreakerTransportError on the recovery path destroyed the
    only durable copy.
    """
    wal = transport._wal_path()
    _write_wal(wal, [_event(1), _event(2)])

    with patch.object(
        transport, "_do_flush", side_effect=BreakerTransportError("backend down")
    ):
        transport._replay_from_wal()

    assert os.path.exists(wal), "WAL deleted despite the flush not landing"
    assert len(_read_wal(wal)) == 2


def test_replay_retries_unflushed_events_next_start(transport):
    """Events kept on disk by a failed replay are delivered on the next start."""
    wal = transport._wal_path()
    _write_wal(wal, [_event(1), _event(2)])

    with patch.object(
        transport, "_do_flush", side_effect=BreakerTransportError("backend down")
    ):
        transport._replay_from_wal()
    assert os.path.exists(wal)

    # Second attempt, backend healthy: the batch must go out and the file go away.
    sent: list[dict] = []

    def _ok():
        sent.extend(transport._buffer[:])
        transport._buffer.clear()

    with patch.object(transport, "_do_flush", side_effect=_ok):
        transport._replay_from_wal()

    assert {e["event_id"] for e in sent} == {"evt-1", "evt-2"}
    assert not os.path.exists(wal), "WAL not cleaned up after a successful replay"


def test_wal_removed_after_successful_flush(transport):
    """The happy path still cleans up — no unbounded replay loop."""
    wal = transport._wal_path()
    _write_wal(wal, [_event(1)])

    def _deliver():
        transport._buffer.clear()

    with patch.object(transport, "_do_flush", side_effect=_deliver):
        transport._replay_from_wal()

    assert not os.path.exists(wal)


# ---------------------------------------------------------------------------
# All three recovery sources, in age order.
# ---------------------------------------------------------------------------


def test_replay_drains_rotated_inflight_and_active(transport):
    """`.wal.1`, `.wal` and `.inflight` all feed the buffer, oldest first."""
    wal = transport._wal_path()
    _write_wal(f"{wal}.1", [_event(1)])
    _write_wal(wal, [_event(2)])
    _write_wal(f"{wal}.inflight", [_event(3)])

    seen: list[str] = []
    with patch.object(
        transport,
        "_do_flush",
        side_effect=lambda: seen.extend(e["event_id"] for e in transport._buffer[:])
        or transport._buffer.clear(),
    ):
        transport._replay_from_wal()

    assert seen == ["evt-1", "evt-2", "evt-3"]
    for suffix in (".1", "", ".inflight"):
        assert not os.path.exists(f"{wal}{suffix}"), f"{suffix} left behind"


def test_inflight_replayed_after_crash_during_send(transport):
    """A batch on the wire when the process died is recovered from `.inflight`.

    This is the case that motivated adding the file: the batch had already
    left `_buffer` and was mid-HTTP, so neither the buffer nor the periodic
    WAL held it.
    """
    wal = transport._wal_path()
    _write_wal(f"{wal}.inflight", [_event(7), _event(8)])

    seen: list[str] = []
    with patch.object(
        transport,
        "_do_flush",
        side_effect=lambda: seen.extend(e["event_id"] for e in transport._buffer[:])
        or transport._buffer.clear(),
    ):
        transport._replay_from_wal()

    assert seen == ["evt-7", "evt-8"]


def test_empty_inflight_is_cleared(transport):
    """A zero-byte `.inflight` is dropped, not replayed forever."""
    inflight = f"{transport._wal_path()}.inflight"
    os.makedirs(os.path.dirname(inflight) or ".", exist_ok=True)
    open(inflight, "w").close()

    with patch.object(transport, "_do_flush") as flush:
        transport._replay_from_wal()

    assert not os.path.exists(inflight)
    flush.assert_not_called()


def test_corrupt_lines_skipped_without_losing_valid_events(transport):
    """One unparseable line must not discard the rest of the batch."""
    wal = transport._wal_path()
    with open(wal, "w") as f:
        f.write(json.dumps(_event(1)) + "\n")
        f.write("{not json\n")
        f.write(json.dumps(_event(2)) + "\n")

    seen: list[str] = []
    with patch.object(
        transport,
        "_do_flush",
        side_effect=lambda: seen.extend(e["event_id"] for e in transport._buffer[:])
        or transport._buffer.clear(),
    ):
        transport._replay_from_wal()

    assert seen == ["evt-1", "evt-2"]


# ---------------------------------------------------------------------------
# event_id is what makes at-least-once safe; pin the guarantee.
# ---------------------------------------------------------------------------


def test_persist_assigns_event_id_before_writing(transport):
    """Every event on disk carries an id, so re-delivery dedups server-side."""
    transport.track({"type": "llm_call", "cost_cents": 5})
    transport._persist_to_wal()

    rows = _read_wal(transport._wal_path())
    assert len(rows) == 1
    assert rows[0]["event_id"], "event reached the WAL without an event_id"


def test_inflight_carries_event_id(transport):
    """The in-flight file is a recovery source, so it needs ids too."""
    transport.track({"type": "llm_call", "cost_cents": 5})
    transport._persist_inflight(transport._buffer[:])

    rows = _read_wal(f"{transport._wal_path()}.inflight")
    assert len(rows) == 1
    assert rows[0]["event_id"]


# ---------------------------------------------------------------------------
# Inflight lifecycle around a real send.
# ---------------------------------------------------------------------------


def test_inflight_cleared_after_accepted_send(transport):
    """Accepted batches must not linger and be replayed a second time."""
    transport.track(_event(1))
    transport._send_batch_with_retry_info = lambda batch: SendResult(
        accepted_event_ids=[e["event_id"] for e in batch]
    )
    transport._do_flush()

    assert not os.path.exists(f"{transport._wal_path()}.inflight")


def test_inflight_retained_when_send_fails(transport):
    """A failed send leaves the batch on disk — it is only in memory after."""
    transport.track(_event(1))
    with patch.object(transport._circuit_breaker, "call", side_effect=BreakerTransportError("x")):
        transport._do_flush()

    rows = _read_wal(f"{transport._wal_path()}.inflight")
    assert [r["event_id"] for r in rows] == ["evt-1"]
    # and the batch is back in the buffer for retry
    assert [e["event_id"] for e in transport._buffer] == ["evt-1"]


def test_later_successful_flush_clears_stale_inflight(transport):
    """A retained `.inflight` self-heals once a send lands."""
    inflight = f"{transport._wal_path()}.inflight"
    _write_wal(inflight, [_event(1)])

    transport.track(_event(2))
    transport._send_batch_with_retry_info = lambda batch: SendResult(
        accepted_event_ids=[e["event_id"] for e in batch]
    )
    transport._do_flush()

    assert not os.path.exists(inflight)


# ---------------------------------------------------------------------------
# Atomic write helper.
# ---------------------------------------------------------------------------


def test_write_events_atomic_leaves_no_tmp(transport):
    """The tmp file is renamed away or unlinked — never left as litter."""
    target = transport._wal_path()
    assert transport._write_events_atomic(target, [_event(1)]) is True

    siblings = [p for p in os.listdir(os.path.dirname(target)) if ".tmp." in p]
    assert siblings == [], f"temp files left behind: {siblings}"
    assert len(_read_wal(target)) == 1


def test_write_events_atomic_reports_failure(transport, monkeypatch):
    """A failed write is reported so callers keep their recovery files."""
    with patch("nullrun.transport.os.replace", side_effect=OSError("disk full")):
        assert transport._write_events_atomic(transport._wal_path(), [_event(1)]) is False


def test_truncated_last_line_does_not_lose_earlier_events(transport):
    """A crash mid-write leaves a partial last line; the rest must survive.

    This is what the tmp+rename dance buys: a reader sees either the whole
    file or a prefix, never a corrupted middle. The partial trailing line is
    dropped — it was never durable — but everything before it replays.
    """
    wal = transport._wal_path()
    with open(wal, "w") as f:
        f.write(json.dumps(_event(1)) + "\n")
        f.write(json.dumps(_event(2)) + "\n")
        f.write('{"event_id": "evt-3", "type": "ll')  # truncated by the crash

    seen: list[str] = []
    with patch.object(
        transport,
        "_do_flush",
        side_effect=lambda: seen.extend(e["event_id"] for e in transport._buffer[:])
        or transport._buffer.clear(),
    ):
        transport._replay_from_wal()

    assert seen == ["evt-1", "evt-2"], "a torn trailing line cost us earlier events"




# ---------------------------------------------------------------------------
# An escaping exception must not kill the background flush thread.
# ---------------------------------------------------------------------------


def test_unexpected_error_does_not_escape_and_kill_flush_thread(transport):
    """A non-HTTP failure must re-queue, not propagate out of the flush loop.

    The background loop calls `_do_flush` with no try/except: an escaping
    exception would kill the thread and silently stop all delivery for the
    life of the process.
    """
    transport.track(_event(1))
    with patch.object(
        transport, "_send_batch_with_retry_info", side_effect=RuntimeError("socket reset")
    ):
        transport._do_flush()  # must not raise

    assert [e["event_id"] for e in transport._buffer] == ["evt-1"]

