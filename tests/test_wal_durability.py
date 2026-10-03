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

import httpx
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
# Deterministic 5xx.
#
# EXECUTION_NOT_BOUND is NOT a 4xx, and it is not a 5xx either. The backend
# used to answer StatusCode::SERVICE_UNAVAILABLE for it (handlers.rs:10016)
# while naming error_code EXECUTION_NOT_BOUND in the body. The 24h binding
# TTL makes it deterministic: once the binding is gone, no retry can ever
# succeed. Status-only classification therefore routed it to the
# transient-5xx path, where it retried until the attempt budget died and
# every attempt counted as a transport failure — ten of them opened the
# circuit breaker on a backend that was answering everything else.
#
# The backend has since been corrected to answer 422, but the tests below
# still use 503 on purpose. Deployed SDKs outlive the backend release that
# fixed it: an instance pinned to an older build is talking to a newer one
# for weeks, and clients in the field are not all upgraded in lockstep. The
# SDK must classify on the code precisely so it is correct against BOTH
# statuses — if this suite only ever saw 422, the classification would
# silently depend on the server being upgraded first, and the bug would
# return as a partial outage on the laggard fleet.
#
# These drive `_client.post` rather than `_send_batch_with_retry_info`. The
# classification lives in the inner `_post_batch` closure, and a 5xx never
# escapes `_send_batch_with_retry_info` as an `HTTPStatusError` anyway —
# `_retry_with_backoff` converts retry exhaustion into
# `BreakerTransportError`. Mocking the outer method would test a shape the
# real path cannot produce.
# ---------------------------------------------------------------------------


def _respond(status: int, body: dict):
    """An httpx transport handler that always answers `status` with `body`."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return _handler


def test_execution_not_bound_is_dead_lettered_not_retried_forever(transport, monkeypatch):
    """503 EXECUTION_NOT_BOUND is deterministic and must not loop forever.

    With status-only classification this is a plain 5xx: the SDK retries it
    indefinitely, and because the batch sits at the head of the retry path
    it blocks every healthy event queued behind it. But the binding TTL is
    24h and nothing the client does can restore it — the only cure is a new
    /gate, which the agent cannot synthesize for a past event.
    """
    # The refusal must be classified on the FIRST response, so the retry
    # budget is irrelevant here; pin it low to keep the test fast.
    monkeypatch.setattr(transport, "_track_max_retries", 2, raising=False)
    transport.track(_event(1))

    transport._client = httpx.Client(
        transport=httpx.MockTransport(
            _respond(503, {"error_code": "EXECUTION_NOT_BOUND", "error_message": "no binding"})
        )
    )
    try:
        transport._do_flush()
    finally:
        transport._client.close()

    rows = _read_wal(transport._wal_dlq_path())
    assert [r["event"]["event_id"] for r in rows] == ["evt-1"], (
        "a permanently-unbound execution is still on the retry path"
    )
    assert rows[0]["error"] == "EXECUTION_NOT_BOUND:503", (
        "the DLQ row must name the wire code, not just the status — an "
        "operator replaying it cannot act on '503'"
    )
    assert transport._buffer == []


def test_execution_not_bound_does_not_open_the_circuit_breaker(transport, monkeypatch):
    """A named refusal is an ANSWER, not an outage — the breaker must not count it.

    This is the same bug class as "any 4xx opens the breaker", one status
    code over. The backend is up and replying correctly; it is telling us
    this particular event can never land. Counting those as transport
    failures trips the breaker after `max_failed_flush` and then blocks
    every healthy event in the buffer for the whole recovery window.
    """
    monkeypatch.setattr(transport, "_track_max_retries", 2, raising=False)
    threshold = transport._circuit_breaker._failure_threshold

    transport._client = httpx.Client(
        transport=httpx.MockTransport(
            _respond(503, {"error_code": "EXECUTION_NOT_BOUND", "error_message": "no binding"})
        )
    )
    try:
        for i in range(threshold + 3):
            transport.track(_event(i))
            transport._do_flush()
    finally:
        transport._client.close()

    state = transport._circuit_breaker._state
    assert state is not state.OPEN, (
        "named deterministic refusals opened the circuit on a healthy backend"
    )
    assert transport._circuit_breaker._failure_count == 0, (
        "a refusal the backend named counted toward the breaker's failure budget"
    )


def test_other_5xx_still_retries_rather_than_being_dead_lettered(transport, monkeypatch):
    """A generic 5xx is NOT deterministic — it must keep retrying.

    Same status, no recognised code: the SDK cannot know whether the backend
    is down, so it must assume it is and keep the data. Quarantining on an
    unrecognised 5xx is how an outage eats a whole fleet's buffer.
    """
    monkeypatch.setattr(transport, "_track_max_retries", 2, raising=False)
    transport.track(_event(1))

    transport._client = httpx.Client(
        transport=httpx.MockTransport(
            _respond(503, {"error_code": "SERVICE_UNAVAILABLE", "error_message": "down"})
        )
    )
    try:
        for _ in range(transport._max_batch_attempts + 2):
            transport._do_flush()
    finally:
        transport._client.close()

    assert not os.path.exists(transport._wal_dlq_path()), (
        "a transient 503 was quarantined — the outage would eat the buffer"
    )
    assert [e["event_id"] for e in transport._buffer] == ["evt-1"], (
        "the event must still be queued for the next attempt"
    )


def test_a_5xx_without_a_parseable_body_is_not_deterministic(transport, monkeypatch):
    """A proxy HTML error page names no code — so it is not a refusal.

    A reverse proxy in front of the backend answers 502 with an HTML error
    document and no JSON envelope. Reading a code out of that is impossible,
    and treating "no code" as "deterministic" would quarantine the entire
    buffer during exactly the outage the proxy is reporting.
    """
    monkeypatch.setattr(transport, "_track_max_retries", 2, raising=False)
    transport.track(_event(1))

    def _html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html><body>502 Bad Gateway</body></html>")

    transport._client = httpx.Client(transport=httpx.MockTransport(_html))
    try:
        transport._do_flush()
    finally:
        transport._client.close()

    assert not os.path.exists(transport._wal_dlq_path()), (
        "an unparseable 502 was treated as a permanent refusal"
    )
    assert [e["event_id"] for e in transport._buffer] == ["evt-1"]


# ---------------------------------------------------------------------------
# 4xx classification. "Any 4xx is permanent" is unsafe: it discards good data
# exactly when the server is recovering.
# ---------------------------------------------------------------------------


def _reject_with(status: int):
    """A send that always fails with `status`."""
    resp = httpx.Response(status, json={}, request=httpx.Request("POST", "https://x"))
    return lambda batch: resp.raise_for_status()


@pytest.mark.parametrize("status", [408, 429])
def test_transient_4xx_is_requeued_never_quarantined(transport, status):
    """408/429 are the server being busy, not the data being wrong.

    Right after an outage every SDK in the fleet replays its WAL at once, the
    rate limiter answers 429, and a naive permanent-4xx rule would dead-letter
    the entire recovering fleet in the first second. These must go back to the
    buffer, and stay there no matter how many attempts are spent.
    """
    transport.track(_event(1))

    with patch.object(transport, "_send_batch_with_retry_info", side_effect=_reject_with(status)):
        for _ in range(transport._max_batch_attempts + 5):
            transport._do_flush()

    assert [e["event_id"] for e in transport._buffer] == ["evt-1"], (
        f"{status} dropped the batch off the retry path"
    )
    assert not os.path.exists(transport._wal_dlq_path()), f"{status} was quarantined"


@pytest.mark.parametrize("status", [401, 403])
def test_auth_4xx_holds_the_batch_for_the_operator(transport, status):
    """A rotated or revoked key must not cost the data.

    401/403 is recoverable by operator action. Quarantining here would strand
    a whole WAL on disk that would have delivered fine the moment the key was
    fixed.
    """
    transport.track(_event(1))

    with patch.object(transport, "_send_batch_with_retry_info", side_effect=_reject_with(status)):
        for _ in range(transport._max_batch_attempts + 5):
            transport._do_flush()

    assert [e["event_id"] for e in transport._buffer] == ["evt-1"]
    assert not os.path.exists(transport._wal_dlq_path()), (
        f"{status} quarantined recoverable data"
    )


def test_413_splits_the_batch_instead_of_quarantining_it(transport):
    """413 says the BATCH is too large; the events themselves are fine.

    The halves are sent as separate batches, not pushed back on the buffer —
    the buffer is flushed wholesale, so re-queuing both halves would rebuild
    the exact batch the server just refused and the split would never converge.
    """
    transport.track(_event(1))
    transport.track(_event(2))
    transport.track(_event(3))
    transport.track(_event(4))

    limit = 2  # pretend the server accepts at most 2 events per request
    sent: list[dict] = []

    def _size_limited(batch):
        if len(batch) > limit:
            _reject_with(413)(batch)
        sent.extend(batch)
        return SendResult(accepted_event_ids=[e["event_id"] for e in batch])

    with patch.object(transport, "_send_batch_with_retry_info", side_effect=_size_limited):
        transport._do_flush()

    assert sorted(e["event_id"] for e in sent) == ["evt-1", "evt-2", "evt-3", "evt-4"]
    assert transport._buffer == []
    assert not os.path.exists(transport._wal_dlq_path()), "a size limit must not dead-letter data"


def test_413_single_event_is_quarantined(transport):
    """Splitting bottoms out: an event that is too big alone can never land."""
    transport.track(_event(1))
    with patch.object(transport, "_send_batch_with_retry_info", side_effect=_reject_with(413)):
        transport._do_flush()

    rows = _read_wal(transport._wal_dlq_path())
    assert [r["event"]["event_id"] for r in rows] == ["evt-1"]


def test_400_batch_is_bisected_to_isolate_the_bad_event(transport):
    """One malformed event must not take the whole batch down with it.

    The backend validates fail-CLOSED, so a single event with an empty `model`
    rejects the entire request. Dead-lettering the batch would bury every
    valid event behind it.
    """
    for i in (1, 2, 3):
        transport.track(_event(i))

    def _only_third_is_bad(batch):
        if any(e["event_id"] == "evt-3" for e in batch):
            _reject_with(400)(batch)
        return SendResult(accepted_event_ids=[e["event_id"] for e in batch])

    with patch.object(transport, "_send_batch_with_retry_info", side_effect=_only_third_is_bad):
        transport._do_flush()

    assert transport._buffer == []
    rows = _read_wal(transport._wal_dlq_path())
    assert [r["event"]["event_id"] for r in rows] == ["evt-3"], (
        "quarantine caught more than the offending event"
    )


def test_repeated_4xx_does_not_open_the_circuit_breaker(transport):
    """A 4xx proves the backend is ALIVE, so it must not trip the breaker.

    This was a live defect: the rejection propagated out of the breaker call,
    which counted it as a transport failure. `max_failed_flush` is 10, so ten
    consecutive rejected batches opened the circuit on a backend that was up
    and answering — and then every buffered event was blocked for the whole
    30s recovery window, for a problem the breaker cannot fix.
    """
    for _ in range(transport.config.max_failed_flush + 5):
        transport.track(_event(1))
        with patch.object(transport, "_send_batch_with_retry_info", side_effect=_reject_with(400)):
            transport._do_flush()

    from nullrun.breaker.circuit_breaker import CBState

    assert transport._circuit_breaker.state == CBState.CLOSED, (
        "repeated 4xx opened the breaker on a live backend"
    )


def test_repeated_deterministic_errors_do_not_open_the_circuit_breaker(transport):
    """A local serialization failure is our bug, not the backend's health."""
    for _ in range(transport.config.max_failed_flush + 5):
        transport.track(_event(1))
        with patch.object(
            transport, "_send_batch_with_retry_info", side_effect=TypeError("not serializable")
        ):
            transport._do_flush()

    from nullrun.breaker.circuit_breaker import CBState

    assert transport._circuit_breaker.state == CBState.CLOSED


def test_transport_failures_still_open_the_circuit_breaker(transport):
    """The guard above must not neuter the breaker — an unreachable backend still counts."""
    transport.track(_event(1))
    refused = httpx.ConnectError("connection refused")
    with patch.object(transport, "_send_batch_with_retry_info", side_effect=refused):
        for _ in range(transport.config.max_failed_flush + 2):
            transport._do_flush()

    from nullrun.breaker.circuit_breaker import CBState

    assert transport._circuit_breaker.state == CBState.OPEN, (
        "a genuinely unreachable backend no longer trips the breaker"
    )


def test_bisect_converges_and_dead_letters_only_the_offender(transport):
    """A bad event anywhere in a large batch costs exactly one event."""
    for i in range(1, 9):
        transport.track(_event(i))
    delivered: list[dict] = []

    def _only_eight_is_bad(batch):
        if any(e["event_id"] == "evt-8" for e in batch):
            _reject_with(422)(batch)
        delivered.extend(batch)
        return SendResult(accepted_event_ids=[e["event_id"] for e in batch])

    with patch.object(transport, "_send_batch_with_retry_info", side_effect=_only_eight_is_bad):
        transport._do_flush()

    assert sorted(e["event_id"] for e in delivered) == [f"evt-{i}" for i in range(1, 8)]
    rows = _read_wal(transport._wal_dlq_path())
    assert [r["event"]["event_id"] for r in rows] == ["evt-8"]


@pytest.mark.parametrize("status", [400, 422])
def test_permanent_4xx_of_a_single_event_is_quarantined(transport, status):
    """At the bottom of the bisect, 400/422 is genuinely undeliverable."""
    transport.track(_event(1))
    with patch.object(transport, "_send_batch_with_retry_info", side_effect=_reject_with(status)):
        transport._do_flush()

    rows = _read_wal(transport._wal_dlq_path())
    assert [r["event"]["event_id"] for r in rows] == ["evt-1"]
    assert all(str(status) in r["error"] for r in rows)
    assert transport._buffer == []
    assert not os.path.exists(transport._wal_inflight_path())


def test_quarantined_batch_is_not_replayed_on_next_start(transport):
    """DLQ contents must never re-enter the retry loop on restart."""
    transport.track(_event(1))
    with patch.object(transport, "_send_batch_with_retry_info", side_effect=_reject_with(422)):
        transport._do_flush()

    assert os.path.exists(transport._wal_dlq_path())
    with patch.object(transport, "_do_flush") as flush:
        transport._replay_from_wal()
    flush.assert_not_called(), "a dead-lettered batch was replayed"


def test_dlq_accumulates_across_quarantines(transport):
    """A second quarantine must not clobber the first — that is data loss too."""
    dlq = transport._wal_dlq_path()
    for i in (1, 2):
        transport.track(_event(i))
        with patch.object(
            transport, "_send_batch_with_retry_info", side_effect=_reject_with(400)
        ):
            transport._do_flush()

    rows = _read_wal(dlq)
    assert [r["event"]["event_id"] for r in rows] == ["evt-1", "evt-2"]


# ---------------------------------------------------------------------------
# Non-HTTP exceptions must not become an eternal blocker.
# ---------------------------------------------------------------------------


def test_5xx_still_retries_and_is_not_dead_lettered(transport):
    """Only permanent rejections are quarantined; transient ones keep retrying."""
    transport.track(_event(1))
    with patch.object(transport._circuit_breaker, "call", side_effect=BreakerTransportError("x")):
        transport._do_flush()

    assert [e["event_id"] for e in transport._buffer] == ["evt-1"]
    assert not os.path.exists(transport._wal_dlq_path())


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


def test_deterministic_error_is_dead_lettered_after_the_attempt_budget(transport):
    """An error that repeats forever must not pin the batch ahead of the buffer.

    A payload that will not serialize fails identically on every cycle. Left
    alone it would be retried for the life of the process and every event
    queued behind it would never be delivered.
    """
    transport.track(_event(1))
    with patch.object(
        transport, "_send_batch_with_retry_info", side_effect=TypeError("not serializable")
    ):
        for _ in range(transport._max_batch_attempts + 2):
            transport._do_flush()

    rows = _read_wal(transport._wal_dlq_path())
    assert [r["event"]["event_id"] for r in rows] == ["evt-1"]
    assert transport._buffer == [], "the exhausted batch stayed on the retry path"


def test_attempt_counter_is_cleared_after_the_batch_lands(transport):
    """A delivered batch must not leave failure history behind.

    Otherwise the map grows for the life of a long-lived process, and a later
    batch with coincidentally identical event ids starts with a spent budget.
    """
    transport.track(_event(1))
    key = transport._failure_signature(transport._buffer[:])
    transport._batch_attempts[key] = 3

    transport._send_batch_with_retry_info = lambda batch: SendResult(
        accepted_event_ids=[e["event_id"] for e in batch]
    )
    transport._do_flush()

    assert key not in transport._batch_attempts


# ---------------------------------------------------------------------------
# Append into a file a crash left torn.
# ---------------------------------------------------------------------------


def test_torn_tail_plus_append_does_not_corrupt_a_valid_event(transport):
    """The append path must not glue a new row onto a truncated one.

    This is the scenario that makes copy-and-append dangerous: the DLQ (and
    any future appending write) copies the previous file verbatim. If the
    process died mid-write, the last line has no `\\n`, and the next row is
    concatenated onto the truncated JSON — turning one good event plus one new
    event into a single unparseable line.
    """
    dlq = transport._wal_dlq_path()
    os.makedirs(os.path.dirname(dlq) or ".", exist_ok=True)
    with open(dlq, "w") as f:
        f.write(json.dumps({"error": "e", "event": _event(1)}) + "\n")
        f.write('{"error": "e", "event": {"event_id": "evt-2", "cost_c')  # torn

    transport._quarantine_to_dlq([_event(3)], RuntimeError("x"))

    # Every line parses: evt-1 survived intact and evt-3 landed cleanly.
    with open(dlq) as f:
        lines = [line for line in f if line.strip()]
    rows = [json.loads(line) for line in lines]
    ids = [r["event"]["event_id"] for r in rows]
    assert ids == ["evt-1", "evt-3"], f"torn tail corrupted the file: {ids}"


def test_append_to_a_file_that_is_entirely_torn_keeps_the_new_rows(transport):
    """No newline anywhere: the whole old content is torn and must be dropped."""
    dlq = transport._wal_dlq_path()
    os.makedirs(os.path.dirname(dlq) or ".", exist_ok=True)
    with open(dlq, "w") as f:
        f.write('{"error": "e", "event": {"event_id"')

    transport._quarantine_to_dlq([_event(3)], RuntimeError("x"))

    rows = _read_wal(dlq)
    assert [r["event"]["event_id"] for r in rows] == ["evt-3"]


def test_stale_tmp_from_a_dead_process_is_truncated_not_appended(transport):
    """A leftover `.tmp.<pid>` from a crash must not be concatenated onto.

    The tmp name is pid-scoped and opened with "w", so a recycled pid
    truncates the stale file rather than appending to whatever the dead
    process left in it.
    """
    target = transport._wal_path()
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    stale = f"{target}.tmp.{os.getpid()}"
    with open(stale, "w") as f:
        f.write('{"event_id": "ghost", "half-writ')

    assert transport._write_events_atomic(target, [_event(1)]) is True

    rows = _read_wal(target)
    assert [r["event_id"] for r in rows] == ["evt-1"], "stale tmp content leaked into the WAL"
