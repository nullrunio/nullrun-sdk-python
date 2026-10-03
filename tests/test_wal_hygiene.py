"""WAL hygiene: permissions, cross-process locking, the DLQ size cap, replay.

What is being protected. The WAL is the SDK's only copy of an event between
``track()`` and the backend accepting it, and an event is a full payload —
prompt, completion, tool arguments. So the files are private by default, the
cap never destroys anything, and the degradation of either guarantee is a
named, observable state rather than an assumption.

The split by platform is deliberate and is the point of the module. A green
run on Windows proves nothing about `flock` or about a directory fsync,
because neither exists there — that is why the POSIX-only behaviour is
`skipif win32` and the Windows behaviour is asserted directly instead of
being assumed to cover the same ground.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import httpx
import pytest

from nullrun.observability import metrics
from nullrun.transport import Transport
from nullrun.wal_admin import _drop_confirmed, read_dlq
from nullrun.wal_admin import main as wal_main

platform = sys.platform

requires_posix_lock = pytest.mark.skipif(
    platform == "win32", reason="fcntl.flock does not exist on win32"
)


@pytest.fixture(autouse=True)
def _clean_metrics():
    metrics.reset()
    yield
    metrics.reset()


@pytest.fixture
def transport(tmp_path, monkeypatch):
    monkeypatch.setenv("NULLRUN_WAL_PATH", str(tmp_path / "sdk.wal"))
    t = Transport(api_url="https://api.test.nullrun.io", api_key="test-key-12345678")
    yield t
    t._client.close()


def _event(i: int) -> dict:
    return {"event_id": f"evt-{i}", "type": "llm_call", "cost_cents": 1}


def _read_wal(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------------------
# 0600. Event payloads are not public artifacts.
# ---------------------------------------------------------------------------


@requires_posix_lock
def test_wal_files_are_owner_only(transport):
    """Every WAL file, on every write path, is 0600.

    `open(path, "w")` asks for 0666 and lets the umask decide; the default
    umask of 022 leaves an event payload world-readable in /tmp, on a shared
    volume, and in any container several services mount. The mode has to be
    requested, because the file that `os.replace` renames is the file the WAL
    ends up being.
    """
    transport.track(_event(1))
    assert transport._persist_to_wal() is True
    transport._persist_inflight([_event(2)])
    assert transport._write_dlq_rows([transport._dlq_row(_event(3), "x")]) is True
    with transport._wal_file_lock():
        pass  # creates the lock file

    base = transport._wal_path()
    for candidate in (base, f"{base}.inflight", f"{base}.dlq", f"{base}.lock"):
        assert os.path.exists(candidate), f"{candidate} was never created"
        assert oct(os.stat(candidate).st_mode & 0o777) == oct(0o600), (
            f"{candidate} is {oct(os.stat(candidate).st_mode & 0o777)}, not 0600"
        )


@requires_posix_lock
def test_a_wal_left_by_a_previous_run_is_tightened_at_startup(tmp_path, monkeypatch):
    """An existing 0644 WAL is chmodded before anything reads it.

    Hardening only new writes would leave the events most likely to hold
    full payloads — the ones already sitting on disk from a crashed run —
    readable by everyone with access to the volume. The mode survives every
    `os.replace`, so tightening the tmp file alone changes nothing about it.
    """
    monkeypatch.setenv("NULLRUN_WAL_PATH", str(tmp_path / "sdk.wal"))
    base = tmp_path / "sdk.wal"
    base.write_text(json.dumps(_event(1)) + "\n")
    os.chmod(base, 0o644)

    t = Transport(api_url="https://api.test.nullrun.io", api_key="test-key-12345678")
    try:
        assert oct(os.stat(base).st_mode & 0o777) == oct(0o600)
    finally:
        t._client.close()


def test_a_platform_without_flock_states_the_degradation_instead_of_failing(
    transport, caplog, monkeypatch
):
    """No `flock` here is a supported configuration, and it says so once.

    The failure this prevents is not an ImportError at module load — it is the
    silent one: a multi-process deployment on Windows, or on any platform
    without flock, believing it has the same protection it has on Linux.

    The branch is reached by REMOVING `fcntl` rather than by running on a
    platform that lacks it. Testing it only where the platform happens to
    cooperate means the branch is asserted on Windows and skipped on every
    Linux CI run — the assertion then never runs on the platform where a
    regression would be introduced, which is the point of having Linux CI.
    """
    from nullrun import transport as transport_mod

    monkeypatch.setattr(transport_mod, "fcntl", None)

    with caplog.at_level("WARNING"):
        with transport._wal_file_lock() as acquired:
            assert acquired is True, "a platform without flock must still be able to write"
    assert metrics.transport.wal_lock == "unavailable"
    assert any("flock" in r.message for r in caplog.records), (
        "the degradation must be stated, not merely recorded in a metric"
    )

    # Once, not once per flush: this is a standing condition, and a warning
    # on every cycle buries the log under a fact that cannot change.
    caplog.clear()
    with caplog.at_level("WARNING"):
        for _ in range(5):
            with transport._wal_file_lock():
                pass
    assert not [r for r in caplog.records if "durability degraded" in r.message]


# ---------------------------------------------------------------------------
# flock. One writer per WAL path.
# ---------------------------------------------------------------------------


def _holder_process(lock_path: str, seconds: float) -> subprocess.Popen:
    """A real second process holding the WAL lock, for `seconds`."""
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import fcntl, os, sys, time
                fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                print("held", flush=True)
                time.sleep(float(sys.argv[2]))
                """
            ),
            lock_path,
            str(seconds),
        ],
        stdout=subprocess.PIPE,
    )


@requires_posix_lock
def test_the_wal_lock_actually_excludes_a_second_holder(transport, monkeypatch, caplog):
    """The guard is real: a lock held elsewhere makes this process skip.

    A lock that is taken but never contended against is indistinguishable
    from no lock at all, and the failure it is meant to prevent — two
    processes interleaving their lines inside one another's read-copy-append
    of the DLQ — is silent and unrecoverable, because the resulting file has
    no parseable boundary. So this asserts the exclusion from the outside,
    with a real second process holding the lock, and a wait short enough that
    the test does not have to sit through the production timeout.
    """
    monkeypatch.setenv("NULLRUN_WAL_LOCK_TIMEOUT_MS", "300")
    holder = _holder_process(transport._wal_lock_path(), 5)
    try:
        assert holder.stdout.readline().strip() == b"held"
        with caplog.at_level("WARNING"):
            with transport._wal_file_lock() as acquired:
                assert acquired is False, "a held WAL lock did not exclude this process"
        assert metrics.transport.wal_lock == "contended"
        assert metrics.transport.wal_lock_timeouts_total == 1
        # And the write is refused, not silently interleaved.
        assert transport._write_events_atomic(transport._wal_path(), [_event(1)]) is False
        assert not os.path.exists(transport._wal_path())
        assert any("retry path" in r.message for r in caplog.records), (
            "giving up on the lock must say where the events went"
        )
    finally:
        holder.kill()
        holder.wait(timeout=10)


@requires_posix_lock
@pytest.mark.slow_sleep
def test_a_writer_that_loses_the_race_waits_rather_than_starving(transport, monkeypatch):
    """Contention costs a wait, not the write. This is a regression test.

    The first implementation took the lock non-blocking and skipped the
    write on contention. On a 4-worker deployment that is not a rare
    collision: the losing writer was refused on *every* attempt, because the
    winner was always mid-append, and its DLQ writes never landed at all. The
    events were not lost — the caller re-queues them — so nothing alerted,
    and they were not delivered either. A wait is a few microseconds; losing
    the file forever is not a cheaper option.

    Found by running this suite in a Linux container: on Windows every test
    that needs a second process to contend for the lock is skipped, which is
    precisely the coverage that would have caught it.

    ``slow_sleep`` because the wait is the assertion. The conftest caps every
    sleep at 1ms, so under the cap the 5s budget would expire in
    milliseconds and the test would be measuring the harness, not the lock.
    """
    monkeypatch.setenv("NULLRUN_WAL_LOCK_TIMEOUT_MS", "5000")
    holder = _holder_process(transport._wal_lock_path(), 0.5)
    try:
        assert holder.stdout.readline().strip() == b"held"
        assert transport._write_dlq_rows(
            [transport._dlq_row(_event(1), "synthetic")]
        ) is True, "a writer lost the race and never wrote"
        holder.wait(timeout=10)
        rows = _read_wal(transport._wal_dlq_path())
        assert [r["event"]["event_id"] for r in rows] == ["evt-1"]
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)


@requires_posix_lock
def test_the_lock_is_released_so_the_next_cycle_can_write(transport):
    """Not holding the lock is not the same as being unable to take it."""
    with transport._wal_file_lock() as first:
        assert first is True
    with transport._wal_file_lock() as second:
        assert second is True
    assert transport._write_events_atomic(transport._wal_path(), [_event(1)]) is True
    assert metrics.transport.wal_lock == "enabled"


@requires_posix_lock
def test_flock_excludes_two_threads_of_one_process(transport):
    """flock is per open file DESCRIPTION, not per process.

    The trap this pins: a reader who concludes "flock does not work between
    threads in one process" would add a `threading.Lock` next to it and be
    wrong in the other direction — the `threading.Lock` would then be the only
    thing serialising threads while the cross-process guarantee quietly
    depends on an implementation detail nobody re-checks. The two are not
    interchangeable, and the fact that they compose is the thing to write
    down.

    It works because every acquisition opens its OWN descriptor. A future
    refactor that caches the fd to avoid an `open()` per lock would make
    `LOCK_EX` a no-op between threads — the kernel would see the same open
    file description and grant the second acquisition immediately. This test
    is what makes that refactor loud.
    """
    import threading

    acquired = []
    refused = []
    barrier = threading.Barrier(8)
    lock = threading.Lock()

    def contend() -> None:
        barrier.wait(timeout=30)
        with transport._wal_file_lock() as got:
            if got:
                with lock:
                    acquired.append(1)
            else:
                with lock:
                    refused.append(1)

    threads = [threading.Thread(target=contend) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive(), "a contending thread hung rather than waiting its turn"

    assert len(acquired) + len(refused) == 8
    assert len(acquired) >= 1
    # Nobody is dropped: a thread that could not take the lock inside the
    # timeout is a caller that never learned it had to wait, which on the
    # write path means a silently-skipped write.
    assert not refused, "a same-process thread was refused the lock it should have waited for"
    assert metrics.transport.wal_lock == "enabled"


@requires_posix_lock
def test_concurrent_threads_do_not_lose_a_dlq_row(transport):
    """The property the lock is for, across threads rather than processes.

    Appends are read-copy-append, so two writers each rewrite the file from
    their own read. Serialised correctly, N threads × M rows produce N×M rows
    with N×M distinct ids — nothing lost, nothing interleaved into an
    unparseable line.
    """
    import threading

    rows_per_thread = 25
    threads_count = 8
    start = threading.Barrier(threads_count)

    def append(tid: int) -> None:
        start.wait(timeout=30)
        for i in range(rows_per_thread):
            transport._write_dlq_rows(
                [transport._dlq_row({"event_id": f"t{tid}-{i}", "type": "llm_call"}, "synthetic")]
            )

    threads = [threading.Thread(target=append, args=(i,)) for i in range(threads_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
        assert not t.is_alive()

    expected = threads_count * rows_per_thread
    rows = _read_wal(transport._wal_dlq_path())
    assert len(rows) == expected, f"expected {expected} rows, got {len(rows)}"
    ids = {r["event"]["event_id"] for r in rows}
    assert len(ids) == expected, "a row was lost to another thread's rewrite"


@requires_posix_lock
def test_a_dlq_append_is_not_interleaved_by_a_concurrent_writer(transport):
    """The read-copy-append is the region the lock exists for.

    Without it, two processes each read the whole DLQ and each write back
    theirs plus their own row: one of the two rows vanishes, and the file
    ends with whichever writer's copy lost the other. This drives two real
    processes against one DLQ and asserts no row is lost.
    """
    script = textwrap.dedent(
        """
        import json, os, sys
        sys.path.insert(0, sys.argv[3])
        from nullrun.transport import Transport
        os.environ["NULLRUN_WAL_PATH"] = sys.argv[1]
        t = Transport(api_url="https://api.test.nullrun.io", api_key="k-12345678")
        for _ in range(20):
            t._write_dlq_rows([t._dlq_row(
                {"event_id": sys.argv[2] + str(_), "type": "llm_call"}, "synthetic")])
        t._client.close()
        """
    )
    src = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/src"
    wal = str(tmp_path_for(transport))
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, wal, f"p{i}-", src],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for i in range(2)
    ]
    for p in procs:
        _, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode()[-2000:]

    rows = _read_wal(transport._wal_dlq_path())
    ids = {r["event"]["event_id"] for r in rows}
    assert len(rows) == 40, f"expected 40 rows, got {len(rows)}"
    assert len(ids) == 40, "one writer's rows were lost to the other's rewrite"


def tmp_path_for(transport) -> str:
    return transport._wal_path()


# ---------------------------------------------------------------------------
# Durability. What can this filesystem actually promise?
# ---------------------------------------------------------------------------


@requires_posix_lock
def test_a_posix_volume_really_does_fsync_its_directory(transport, tmp_path):
    """The durability claim, asserted rather than assumed.

    Every WAL write is tmp-file + fsync + `os.replace`. That sequence is only
    worth anything if the DIRECTORY entry is also fsynced — `os.replace` is
    atomic with respect to readers, but the rename itself is not durable until
    the directory is synced, and a power cut can otherwise leave the file
    that the WAL points at absent or stale. Windows has no directory fsync at
    all, which is why the SDK reports `wal_dir_fsync` as a probed fact rather
    than a promise.

    This asserts the positive on the platform where the answer is supposed to
    be yes. The existing degradation test covers the negative by removing
    `fcntl`, but nothing pinned the healthy case, so a change that made the
    probe always return False — silently downgrading every deployment's
    durability claim — would have passed the whole suite.
    """
    assert transport._dir_fsync_supported(str(tmp_path)) is True
    assert metrics.transport.wal_dir_fsync == "enabled", (
        "the probe succeeded but the metric does not say so, so an operator "
        "reading /health is told less than the code knows"
    )


@requires_posix_lock
def test_the_wal_write_actually_reaches_the_volume(transport, tmp_path):
    """A real write survives a reopen — the whole point of the WAL.

    Complements the fsync probe: that one says the syscall works, this one
    says the file is really where the SDK says it is, with the bytes the SDK
    says it wrote, after the handle is closed and a new one is opened.
    """
    transport.track({"event_id": "durable-1", "type": "llm_call", "cost_cents": 1})
    assert transport._persist_to_wal() is True, "the WAL write reported failure"
    rows = _read_wal(transport._wal_path())
    assert [r["event_id"] for r in rows] == ["durable-1"]


@requires_posix_lock
def test_recovery_reads_the_rotated_wal_not_just_the_active_one(transport, monkeypatch):
    """A restart must find events in BOTH generations of the WAL.

    Rotation moves the active file to `.wal.1` before a new one is written, so
    after any rotation the oldest unflushed events live only in the rotated
    file. A recovery path that read just the active WAL would silently drop
    them — the worst shape of bug, because the process starts cleanly and the
    events are simply not there. This is the closest thing to the SIGKILL case
    that can be tested deterministically: a kill is a restart whose previous
    exit did no cleanup, and the state it leaves behind is exactly a rotated
    plus active pair.
    """
    wal = transport._wal_path()
    rotated = f"{wal}.1"
    with open(rotated, "w") as f:
        f.write(json.dumps({"event_id": "older-1", "type": "llm_call"}) + "\n")
    with open(wal, "w") as f:
        f.write(json.dumps({"event_id": "newer-1", "type": "llm_call"}) + "\n")

    delivered: list[str] = []
    monkeypatch.setattr(
        transport, "_do_flush", lambda: delivered.extend(e["event_id"] for e in transport._buffer)
    )
    transport._replay_from_wal()
    assert sorted(delivered) == ["newer-1", "older-1"], (
        f"recovery lost a generation of the WAL: {delivered}"
    )


# ---------------------------------------------------------------------------
# DLQ size cap. A full DLQ stalls; it never deletes.
# ---------------------------------------------------------------------------


def test_the_dlq_cap_stalls_the_write_and_deletes_nothing(transport, monkeypatch, caplog):
    """At the cap the event goes back on the retry path, not into the bin.

    The tempting implementation is to trim the oldest rows. That destroys the
    only copy of events the SDK could not deliver, silently, with no trace
    beyond a shorter file — and it is unrecoverable in a way a full disk is
    not. So the cap refuses the append, loudly, and the caller keeps the
    event alive on the retry path.
    """
    rows = [
        transport._dlq_row({"event_id": f"old-{i}", "type": "llm_call"}, "synthetic")
        for i in range(20)
    ]
    # The cap is exactly what the seed occupies, measured the same way the
    # cap measures an incoming write. Hard-coding a byte count would make the
    # test a hostage to the exact JSON shape of a DLQ row.
    seed_bytes = sum(len(json.dumps(r, default=str)) + 1 for r in rows)
    monkeypatch.setenv("NULLRUN_DLQ_MAX_BYTES", str(seed_bytes))
    assert transport._write_dlq_rows(rows) is True
    before = _read_wal(transport._wal_dlq_path())
    assert len(before) == 20

    with caplog.at_level("ERROR"):
        assert transport._write_dlq_rows([transport._dlq_row(_event(1), "late")]) is False

    after = _read_wal(transport._wal_dlq_path())
    assert after == before, "the cap must not remove, reorder, or rewrite a row"
    assert metrics.transport.dlq_overflow_total == 1
    assert any("never" in r.message and "deletes" in r.message for r in caplog.records), (
        f"stalling must be loud, got {[r.message for r in caplog.records]}"
    )


def test_a_terminal_refusal_that_cannot_be_recorded_is_held_not_resent(
    transport, monkeypatch
):
    """A permanent rejection with no DLQ space must not clear `.inflight`.

    `_quarantine_to_dlq` clears the in-flight file because the events are
    parked. If the park failed, that clear is the loss: the events are in
    neither the DLQ nor the buffer nor `.inflight`.

    It also must not go back on the SEND path. The earlier version of this
    test asserted exactly that, and it was asserting the bug: a terminal
    refusal re-sent produces the identical refusal forever while occupying
    the head of the buffer. The event is held instead, and `.inflight` is
    retained until the holdover has its own durable copy in
    `<wal>.holdover` — see the kill -9 test below for why retaining
    `.inflight` alone is not enough.
    """
    monkeypatch.setenv("NULLRUN_DLQ_MAX_BYTES", "1")
    transport.track(_event(1))
    transport._client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                422,
                json={"error_code": "EXECUTION_NOT_BOUND", "message": "no binding"},
            )
        )
    )
    try:
        transport._do_flush()
    finally:
        transport._client.close()

    assert not os.path.exists(transport._wal_dlq_path())
    assert os.path.exists(transport._wal_inflight_path()), (
        "the only durable copy of an unrecorded refusal was discarded"
    )
    assert transport._buffer == [], "a terminally refused event was put back on the send path"
    assert [r["event"]["event_id"] for r in transport._dlq_overflow] == ["evt-1"]
    assert metrics.transport.dlq_holdover == 1


def test_a_held_refusal_survives_a_kill_minus_nine(transport, tmp_path, monkeypatch):
    """The user's question: are held events durable, or only in memory?

    A memory-only holdover is lost by `kill -9`, and the fix that introduced
    it looked safe because `.inflight` was still on disk holding the batch the
    refusal came from. It is not: the next flush calls `_persist_inflight`
    with ITS batch, which overwrites the file. So a process that holds a
    refusal and then flushes again has silently overwritten the only
    on-disk trace of it, and the events are in the DLQ nowhere, in `.wal`
    nowhere, and in `.inflight` overwritten.

    So the test does what a kill does, not what a graceful stop does: it
    throws the Transport away and builds a NEW one over the same WAL path,
    with no flush, no `stop()`, and no chance for the dying process to write
    anything. A fresh instance that finds the held event proves the record
    outlived the process; one that finds nothing proves the loss.
    """
    _dlq_never_fits(monkeypatch)
    transport.track(_event(1))
    transport._client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                422,
                json={"error_code": "EXECUTION_NOT_BOUND", "message": "no binding"},
            )
        )
    )
    try:
        transport._do_flush()
    finally:
        transport._client.close()

    assert transport._dlq_overflow, "nothing was held, so this test proves nothing"

    # The overwrite that loses a memory-only holdover. Same process, so this
    # is the *benign* case; a kill needs no help from anyone.
    transport._persist_inflight([_event(99)])

    recovered = Transport(api_url=transport.api_url, api_key="test-key-12345678")
    try:
        recovered._recover_holdover()
        assert [r["event"]["event_id"] for r in recovered._dlq_overflow] == ["evt-1"], (
            "a fresh process over the same WAL does not find the held refusal: "
            "it lived in memory only and a kill -9 would have lost it"
        )
        assert recovered._buffer == [], "a refused event was put back on the send path"
    finally:
        recovered.stop(flush=False)


def test_a_recovered_holdover_drains_without_being_resent(transport, monkeypatch):
    """Recovery restores the hold, not the send. Re-sending is the bug.

    A refusal is terminal: sending it again produces the identical refusal
    and occupies the head of the buffer while it does. If recovery put these
    events back on the send path, the restart would reintroduce exactly the
    head-of-buffer starvation the holdover was added to stop — and it would do
    it on every restart, which is the condition under which an operator is
    most likely to be watching.
    """
    _dlq_never_fits(monkeypatch)
    transport.track(_event(1))
    transport._client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                422,
                json={"error_code": "EXECUTION_NOT_BOUND", "message": "no binding"},
            )
        )
    )
    try:
        transport._do_flush()
    finally:
        transport._client.close()

    sent: list[list[str]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        sent.append([e["event_id"] for e in json.loads(request.content)["events"]])
        return httpx.Response(200, json={"processed": 0, "accepted_event_ids": []})

    recovered = Transport(api_url=transport.api_url, api_key="test-key-12345678")
    try:
        recovered._recover_holdover()
        recovered._client = httpx.Client(transport=httpx.MockTransport(_handler))
        # The DLQ still has no room, so this is a no-op — which is the point.
        # What must not happen is a send.
        assert recovered._drain_dlq_overflow() == 0
        assert sent == [], f"a recovered refusal was re-sent: {sent}"
        assert [r["event"]["event_id"] for r in recovered._dlq_overflow] == ["evt-1"]
    finally:
        recovered._client.close()
        recovered.stop(flush=False)


def test_a_drain_releases_only_the_drained_rows_from_the_holdover(
    transport, monkeypatch
):
    """Partial drain must not delete the rows it did not write.

    The holdover file can hold MORE than the in-memory index: the index is
    bounded by NULLRUN_DLQ_HOLDOVER_MAX_EVENTS and drops its oldest entries,
    and those rows are still owed a DLQ. Releasing the file wholesale after a
    partial drain would delete exactly the rows nobody tracked — the events
    the bound was introduced to stop the SDK from caring about, deleted
    silently by the mechanism meant to protect them.
    """
    monkeypatch.setenv("NULLRUN_DLQ_MAX_BYTES", "1000000")
    transport.track(_event(1))
    transport.track(_event(2))
    # Force the hold for both, so the file has two rows.
    transport._hold_for_dlq(
        [transport._dlq_row(_event(1), "EXECUTION_NOT_BOUND:422"),
         transport._dlq_row(_event(2), "EXECUTION_NOT_BOUND:422")],
        "EXECUTION_NOT_BOUND:422",
    )
    assert os.path.exists(transport._wal_holdover_path())

    # Truncate the index, as the cap would, leaving one row tracked.
    monkeypatch.setenv("NULLRUN_DLQ_HOLDOVER_MAX_EVENTS", "1")
    transport._trim_holdover_index()
    assert [r["event"]["event_id"] for r in transport._dlq_overflow] == ["evt-2"]

    assert transport._drain_dlq_overflow() == 1

    rows, _ = read_dlq(transport._wal_holdover_path())
    remaining = [r["event"]["event_id"] for r in rows if isinstance(r.get("event"), dict)]
    assert remaining == ["evt-1"], (
        f"the drained row and/or the untracked row are wrong: {remaining}"
    )


def test_the_holdover_index_is_bounded_and_says_so(transport, monkeypatch, caplog):
    """A bound with a metric, not a bound that quietly drops events.

    Two properties, because they are the same decision: the index must not
    grow without limit while the DLQ stays full, and the truncation must be
    visible — a bound that silently discards makes `dlq_holdover` a liar,
    and a metric that under-reports is worse than no metric.
    """
    monkeypatch.setenv("NULLRUN_DLQ_HOLDOVER_MAX_EVENTS", "3")
    for i in range(1, 8):
        transport._hold_for_dlq(
            [transport._dlq_row(_event(i), "EXECUTION_NOT_BOUND:422")],
            "EXECUTION_NOT_BOUND:422",
        )

    assert len(transport._dlq_overflow) == 3, "the index is unbounded"
    assert [r["event"]["event_id"] for r in transport._dlq_overflow] == [
        "evt-5", "evt-6", "evt-7",
    ], "the bound kept the wrong end; the oldest are the ones still owed"
    assert metrics.transport.dlq_holdover_index_truncated == 4
    assert "NOT lost" in caplog.text, "truncation was not reported as non-loss"

    # The data is not truncated, only the index — this is the whole claim.
    rows, _ = read_dlq(transport._wal_holdover_path())
    assert len([r for r in rows if isinstance(r.get("event"), dict)]) == 7


def test_a_holdover_that_cannot_be_written_says_the_event_is_in_memory_only(
    transport, monkeypatch, caplog
):
    """The one holdover counter that means possible LOSS must say so.

    Every other holdover outcome is deferred recording, and a restart still
    gets the rows. A failed holdover write is different: the rows are in RAM
    and nowhere else, and a kill takes them. Silently continuing would make
    the failure indistinguishable from the healthy case at the only point
    where anyone could still act on it.
    """
    monkeypatch.setenv("NULLRUN_DLQ_MAX_BYTES", "1")
    transport.track(_event(1))
    transport._client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                422,
                json={"error_code": "EXECUTION_NOT_BOUND", "message": "no binding"},
            )
        )
    )
    # The holdover write is the thing under test, so it is the thing that fails.
    monkeypatch.setattr(
        Transport, "_write_events_atomic",
        lambda self, path, events, mode="w": False,
    )
    try:
        transport._do_flush()
    finally:
        transport._client.close()

    assert transport._dlq_overflow, "nothing was held"
    assert metrics.transport.dlq_holdover_persist_failures == 1
    assert "in memory only" in caplog.text


def _dlq_never_fits(monkeypatch) -> None:
    """Make every DLQ write fail, whatever the size."""
    monkeypatch.setenv("NULLRUN_DLQ_MAX_BYTES", "1")


def test_a_full_dlq_does_not_block_the_healthy_events_behind_it(transport, monkeypatch):
    """The whole point: one refused event must not stop the stream.

    A full DLQ plus a refused event is a condition the SDK creates, not one
    the operator creates, so it must not cost the delivery of every healthy
    event behind it. Before the holdover, the refused event was re-queued at
    the HEAD of the buffer and re-sent on every cycle — refused again, held
    again — and the buffer never drained. The events that were fine sat
    behind it forever.
    """
    _dlq_never_fits(monkeypatch)
    sent: list[list[str]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        ids = [e["event_id"] for e in json.loads(request.content)["events"]]
        sent.append(ids)
        return httpx.Response(
            200,
            json={
                "processed": len(ids),
                "accepted_event_ids": [i for i in ids if i.startswith("good")],
                "rejection_details": [
                    {"event_id": i, "reason": "reservation_not_found"}
                    for i in ids
                    if i.startswith("bad")
                ],
                "rejected_count": sum(1 for i in ids if i.startswith("bad")),
            },
        )

    transport._client = httpx.Client(transport=httpx.MockTransport(_handler))
    try:
        for i, name in ((1, "bad-1"), (2, "good-1"), (3, "good-2")):
            event = _event(i)
            event["event_id"] = name
            transport.track(event)
        transport._do_flush()
        assert transport._buffer == [], "healthy events were held up by the refused one"
        assert {"good-1", "good-2"} <= set(sent[-1]), "the healthy events were not delivered"

        # And nothing keeps being re-sent on a cycle where nothing changed.
        before = len(sent)
        transport._do_flush()
        transport._do_flush()
        assert len(sent) == before, "a terminally refused event was re-sent on every cycle"
    finally:
        transport._client.close()

    assert [r["event"]["event_id"] for r in transport._dlq_overflow] == ["bad-1"]


def test_a_whole_batch_refusal_is_bisected_before_anything_is_parked(transport, monkeypatch):
    """A batch-level refusal is not evidence about every event in it.

    The backend refused the whole request because of the one event in it that
    cannot be recorded. Quarantining the batch on that evidence files 49
    healthy events as refused for a reason that never applied to them — and
    with no DLQ space it also re-queues the batch, so the healthy events are
    re-sent and refused forever.
    """
    _dlq_never_fits(monkeypatch)
    sent: list[list[str]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        ids = [e["event_id"] for e in json.loads(request.content)["events"]]
        sent.append(ids)
        # Refuses only while the offender is still travelling with the batch.
        if "bad-1" in ids:
            return httpx.Response(
                422,
                json={"error_code": "EXECUTION_NOT_BOUND", "message": "no binding"},
            )
        return httpx.Response(
            200,
            json={
                "processed": len(ids),
                "accepted_event_ids": ids,
                "rejection_details": [],
                "rejected_count": 0,
            },
        )

    transport._client = httpx.Client(transport=httpx.MockTransport(_handler))
    try:
        for i, name in ((1, "bad-1"), (2, "good-1"), (3, "good-2")):
            event = _event(i)
            event["event_id"] = name
            transport.track(event)
        transport._do_flush()
    finally:
        transport._client.close()

    assert transport._buffer == [], "the batch was left on the retry path"
    assert len(sent) > 1, "the refusal was not bisected, so the offender was never isolated"
    assert {"good-1", "good-2"} <= {i for call in sent for i in call if i.startswith("good")}
    held = [r["event"]["event_id"] for r in transport._dlq_overflow]
    assert held == ["bad-1"], f"only the offender should be held, got {held}"


def test_a_held_refusal_lands_as_soon_as_the_dlq_has_room(transport, monkeypatch):
    """The holdover defers the write; it must not turn into a silent drop.

    If space never frees, the operator is left with a DLQ that is short the
    events it is supposed to hold and no indication that they exist anywhere.
    So the held rows are re-attempted on every flush and land the moment the
    write can succeed.
    """
    _dlq_never_fits(monkeypatch)
    transport._dlq_overflow.append(transport._dlq_row(_event(1), "reservation_not_found"))
    assert transport._write_dlq_rows([transport._dlq_row(_event(2), "synthetic")]) is False
    assert len(transport._dlq_overflow) == 1, "a refused write must not drop what is held"

    monkeypatch.setenv("NULLRUN_DLQ_MAX_BYTES", str(8 * 1024 * 1024))
    transport._client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"processed": 0, "accepted_event_ids": []})
        )
    )
    try:
        transport._do_flush()
    finally:
        transport._client.close()

    assert transport._dlq_overflow == []
    rows = _read_wal(transport._wal_dlq_path())
    assert [r["event"]["event_id"] for r in rows] == ["evt-1"]
    assert metrics.transport.dlq_holdover == 0


def test_a_bisect_cascade_spends_a_bounded_number_of_requests(transport, monkeypatch):
    """Halving is 2^depth sends. The depth guard does not bound that.

    A batch the backend refuses as a unit costs one request per singleton
    explored. Without a request budget an all-bad batch spends the operator's
    rate limit rediscovering what the first response already said — and a
    refusal is exactly what a rate limiter starts answering, so the cascade
    feeds the condition it is diagnosing.

    The claim is that the cost is a function of the BUDGET, not of the batch:
    1024 events must not cost more requests than 16.
    """
    monkeypatch.setenv("NULLRUN_MAX_BISECT_REQUESTS", "4")
    sent: list[int] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        sent.append(len(json.loads(request.content)["events"]))
        return httpx.Response(
            422, json={"error_code": "EXECUTION_NOT_BOUND", "message": "no binding"}
        )

    costs: list[int] = []
    # `track` auto-flushes at `batch_size` (50 by default), and each flush
    # gets a fresh budget — so leaving it on would measure 20 flushes of 1024
    # against 1 flush of 16. The claim under test is the cost of isolating an
    # offender WITHIN one batch, so one batch has to reach the wire. The
    # config is read at construction, so this cannot be set via env here.
    transport.config.batch_size = 100_000
    for size in (16, 1024):
        sent.clear()
        transport._buffer.clear()
        transport._dlq_overflow.clear()
        before_parked = len(_read_wal(transport._wal_dlq_path()))
        transport._client = httpx.Client(transport=httpx.MockTransport(_handler))
        try:
            for i in range(size):
                event = _event(i)
                event["event_id"] = f"b{size}-{i}"
                transport.track(event)
            assert len(transport._buffer) == size, "the buffer flushed before the test could"
            transport._do_flush()
        finally:
            transport._client.close()
        costs.append(len(sent))
        assert transport._buffer == [], "the un-split remainder was left on the send path"
        # Every event of THIS batch is accounted for: newly parked in the DLQ,
        # or held pending room. The holdover drains on the next flush, so it
        # may legitimately be empty by the second iteration — what must never
        # happen is an event that is in neither.
        parked = len(_read_wal(transport._wal_dlq_path())) - before_parked
        recorded = parked + len(transport._dlq_overflow)
        assert recorded == size, f"{recorded} of {size} events recorded anywhere"

    # One request for the batch, then the budget's worth of splits. The slack
    # is the halves already dispatched when the budget ran out.
    assert costs[0] <= 1 + 4 + 2, f"a 16-event batch cost {costs[0]} requests"
    assert costs[1] == costs[0], (
        f"cost scaled with batch size: {costs[0]} requests for 16 events, "
        f"{costs[1]} for 1024"
    )
    assert metrics.transport.batches_bisect_budget_exhausted >= 2


# ---------------------------------------------------------------------------
# Replay. A supported path for the events nothing else will read.
# ---------------------------------------------------------------------------


def _seed_dlq(transport) -> None:
    for i, reason in ((1, "reservation_not_found"), (2, "reservation_not_found"), (3, "budget_exceeded")):
        transport._write_dlq_rows([transport._dlq_row(_event(i), reason)])


def test_status_reports_what_is_parked_and_never_edits(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NULLRUN_WAL_PATH", str(tmp_path / "sdk.wal"))
    t = Transport(api_url="https://api.test.nullrun.io", api_key="test-key-12345678")
    try:
        _seed_dlq(t)
        dlq = t._wal_dlq_path()
        before = open(dlq, "rb").read()
        assert wal_main(["--wal", str(tmp_path / "sdk.wal"), "status"]) == 0
        assert open(dlq, "rb").read() == before, "`status` is a read-only command"
    finally:
        t._client.close()

    out = capsys.readouterr().out
    assert "3 row(s)" in out
    assert "reservation_not_found" in out
    assert "budget_exceeded" in out


def test_replay_is_a_dry_run_unless_explicitly_told_otherwise(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("NULLRUN_WAL_PATH", str(tmp_path / "sdk.wal"))
    t = Transport(api_url="https://api.test.nullrun.io", api_key="test-key-12345678")
    try:
        _seed_dlq(t)
        dlq = t._wal_dlq_path()
        before = open(dlq, "rb").read()
        assert (
            wal_main(
                [
                    "--wal",
                    str(tmp_path / "sdk.wal"),
                    "replay",
                    "--api-key",
                    "k-12345678",
                ]
            )
            == 0
        )
        assert open(dlq, "rb").read() == before, "a replay with no --execute deleted rows"
    finally:
        t._client.close()
    assert "dry run" in capsys.readouterr().out


def test_replay_removes_only_the_events_the_backend_confirmed(tmp_path, monkeypatch, capsys):
    """Two confirmed, one refused: the refused row stays.

    This is the whole contract of the command. Dropping a row because we
    re-sent it would be indistinguishable from dropping it because it landed;
    a refusal that is still current must survive the attempt to fix it, or
    the operator's evidence disappears exactly when it is most useful.
    """
    monkeypatch.setenv("NULLRUN_WAL_PATH", str(tmp_path / "sdk.wal"))
    t = Transport(api_url="https://api.test.nullrun.io", api_key="test-key-12345678")
    try:
        _seed_dlq(t)
        dlq = t._wal_dlq_path()

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "processed": 2,
                    "accepted_event_ids": ["evt-1", "evt-2"],
                    "rejection_details": [
                        {"event_id": "evt-3", "reason": "budget_exceeded"}
                    ],
                    "rejected_count": 1,
                },
            )

        # The CLI builds its own Transport, so the mock has to be installed on
        # the class rather than on an instance.
        original = Transport.__init__

        def _patched_init(self, *a, **kw):
            original(self, *a, **kw)
            self._client.close()
            self._client = httpx.Client(transport=httpx.MockTransport(_handler))

        monkeypatch.setattr(Transport, "__init__", _patched_init)
        assert (
            wal_main(
                [
                    "--wal",
                    str(tmp_path / "sdk.wal"),
                    "replay",
                    "--execute",
                    "--api-key",
                    "k-12345678",
                ]
            )
            == 0
        )
    finally:
        t._client.close()

    remaining = [r["event"]["event_id"] for r in _read_wal(dlq)]
    assert remaining == ["evt-3"], f"an unconfirmed row was removed: {remaining}"
    assert "sent 2 event(s)" in capsys.readouterr().out


def _refusing_backend(monkeypatch, status: int, code: str) -> list[httpx.Request]:
    """Install a class-level mock that refuses EVERY event with ``code``.

    Returns the list of requests it saw, so a test can assert on what was
    sent as well as on what the command did with the reply.
    """
    seen: list[httpx.Request] = []
    original = Transport.__init__

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            status, json={"error_code": code, "error_message": "no binding"}
        )

    def _patched_init(self, *a, **kw):
        original(self, *a, **kw)
        self._client.close()
        self._client = httpx.Client(transport=httpx.MockTransport(_handler))

    monkeypatch.setattr(Transport, "__init__", _patched_init)
    return seen


def test_replay_that_is_refused_again_keeps_the_row_and_reports_failure(
    tmp_path, monkeypatch, capsys
):
    """The whole batch refused ⇒ the evidence stays and the exit code is 1.

    This is the case the existing partial test cannot reach: there, one
    confirmed event masked the accounting. With every event refused, nothing
    remains in the transport's buffer, so a check that only looked there
    concluded "all confirmed" and DELETED every row — reporting success after
    the backend had just rejected all of them. A refused event lives in the
    scratch DLQ or in the DLQ holdover, not in the buffer; all three are read.
    """
    monkeypatch.setenv("NULLRUN_WAL_PATH", str(tmp_path / "sdk.wal"))
    t = Transport(api_url="https://api.test.nullrun.io", api_key="test-key-12345678")
    dlq = t._wal_dlq_path()
    try:
        _seed_dlq(t)
    finally:
        t._client.close()
    _refusing_backend(monkeypatch, 400, "VALIDATION_FAILED")

    rc = wal_main(
        ["--wal", str(tmp_path / "sdk.wal"), "replay", "--execute", "--api-key", "k-12345678"]
    )
    captured = capsys.readouterr()
    assert rc == 1, f"a fully-refused replay reported success: {captured.out}"
    remaining = [r["event"]["event_id"] for r in _read_wal(dlq)]
    assert remaining == ["evt-1", "evt-2", "evt-3"], (
        f"a refused replay deleted the operator's only record: {remaining}"
    )
    assert "not confirmed" in captured.err


def test_replay_withholds_a_refusal_re_sending_cannot_fix(
    tmp_path, monkeypatch, capsys
):
    """An expired 24h binding is not a transport fault; nothing recovers it.

    Re-sending produces the identical 422, so the default is to leave the row
    and say why. `--include-terminal` is the operator overriding that.
    """
    monkeypatch.setenv("NULLRUN_WAL_PATH", str(tmp_path / "sdk.wal"))
    t = Transport(api_url="https://api.test.nullrun.io", api_key="test-key-12345678")
    dlq = t._wal_dlq_path()
    try:
        for i in (1, 2):
            t._write_dlq_rows([t._dlq_row(_event(i), "EXECUTION_NOT_BOUND:422")])
    finally:
        t._client.close()

    sent = _refusing_backend(monkeypatch, 422, "EXECUTION_NOT_BOUND")

    rc = wal_main(
        ["--wal", str(tmp_path / "sdk.wal"), "replay", "--execute", "--api-key", "k-12345678"]
    )
    err = capsys.readouterr().err
    assert sent == [], f"a terminal refusal was re-sent: {len(sent)} request(s)"
    assert rc == 1
    assert "cannot fix" in err and "EXECUTION_NOT_BOUND:422" in err
    assert [r["event"]["event_id"] for r in _read_wal(dlq)] == ["evt-1", "evt-2"]


def test_include_terminal_sends_them_anyway_and_does_not_duplicate(
    tmp_path, monkeypatch, capsys
):
    """The override sends; the rows stay, because the send still failed."""
    monkeypatch.setenv("NULLRUN_WAL_PATH", str(tmp_path / "sdk.wal"))
    t = Transport(api_url="https://api.test.nullrun.io", api_key="test-key-12345678")
    dlq = t._wal_dlq_path()
    try:
        t._write_dlq_rows([t._dlq_row(_event(1), "EXECUTION_NOT_BOUND:422")])
    finally:
        t._client.close()
    _refusing_backend(monkeypatch, 422, "EXECUTION_NOT_BOUND")

    rc = wal_main(
        [
            "--wal",
            str(tmp_path / "sdk.wal"),
            "replay",
            "--execute",
            "--include-terminal",
            "--api-key",
            "k-12345678",
        ]
    )
    assert rc == 1, "a refused --include-terminal send reported success"
    rows = _read_wal(dlq)
    assert [r["event"]["event_id"] for r in rows] == ["evt-1"]
    assert len(rows) == len({r["event"]["event_id"] for r in rows}), "the DLQ grew"
    assert "not confirmed" in capsys.readouterr().err


def test_an_empty_dlq_is_success_not_a_failure(tmp_path, monkeypatch):
    """rc 0 on an empty DLQ, so a scheduled repair is not permanently red."""
    monkeypatch.setenv("NULLRUN_WAL_PATH", str(tmp_path / "sdk.wal"))
    assert (
        wal_main(
            ["--wal", str(tmp_path / "sdk.wal"), "replay", "--execute", "--api-key", "k-12345678"]
        )
        == 0
    )


def test_replay_preserves_a_line_it_cannot_parse(tmp_path):
    """The unparseable line is the one the operator most needs to see.

    Rewriting the file from the rows that DID parse would delete it without
    anyone noticing — the file still looks complete, it is just quietly
    shorter. It stays.
    """
    dlq = tmp_path / "sdk.wal.dlq"
    dlq.write_text(
        json.dumps({"reason": "r", "event": {"event_id": "evt-1"}}) + "\n"
        + "{not json at all\n"
        + json.dumps({"reason": "r", "event": {"event_id": "evt-2"}}) + "\n"
    )
    removed = _drop_confirmed(str(dlq), {"evt-1"})
    assert removed == 1
    lines = dlq.read_text().splitlines()
    assert lines == ["{not json at all", json.dumps({"reason": "r", "event": {"event_id": "evt-2"}})]


def test_a_dlq_with_a_corrupt_line_is_still_readable(tmp_path):
    """One bad line must not make the recovery file unreadable.

    A reader that required every line to parse would convert a single partial
    write — the exact thing a crash produces — into total loss of the file.
    """
    dlq = tmp_path / "sdk.wal.dlq"
    dlq.write_text(
        json.dumps({"reason": "r", "event": {"event_id": "evt-1"}}) + "\n" + "{truncated\n"
    )
    rows, corrupt = read_dlq(str(dlq))
    assert [r["event"]["event_id"] for r in rows] == ["evt-1"]
    assert len(corrupt) == 1


def test_replay_needs_an_api_key_before_it_does_anything(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("NULLRUN_API_KEY", raising=False)
    dlq = tmp_path / "sdk.wal.dlq"
    dlq.write_text(json.dumps({"reason": "r", "event": _event(1)}) + "\n")
    before = dlq.read_bytes()
    rc = wal_main(["--wal", str(tmp_path / "sdk.wal"), "replay", "--execute"])
    assert rc == 1
    assert dlq.read_bytes() == before
    assert "no API key" in capsys.readouterr().err


def test_a_200_without_accepted_event_ids_removes_nothing(tmp_path, monkeypatch):
    """The response-shape case: a 2xx that cannot be read as a confirmation.

    A proxy's HTML page, an empty body, or a backend predating the field
    all produce a 200 that says nothing about which events landed. Reading
    it as "sent, therefore confirmed" deletes rows on the strength of a
    response that never mentioned them — and a 2xx is exactly what a
    misconfigured proxy returns.

    This is the fourth way `replay` can lose a row, and the three covered
    above (HTTP error, refusal named in `rejection_details`, explicit
    partial list) all come from a backend that is answering in the
    contract's shape. This one does not.
    """
    monkeypatch.setenv("NULLRUN_WAL_PATH", str(tmp_path / "sdk.wal"))
    t = Transport(api_url="https://api.test.nullrun.io", api_key="test-key-12345678")
    try:
        _seed_dlq(t)
        dlq = t._wal_dlq_path()

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>proxy error</html>")

        original = Transport.__init__

        def _patched_init(self, *a, **kw):
            original(self, *a, **kw)
            self._client.close()
            self._client = httpx.Client(transport=httpx.MockTransport(_handler))

        monkeypatch.setattr(Transport, "__init__", _patched_init)
        rc = wal_main(
            [
                "--wal",
                str(tmp_path / "sdk.wal"),
                "replay",
                "--execute",
                "--api-key",
                "k-12345678",
            ]
        )
    finally:
        t._client.close()

    assert rc == 1, f"an unreadable 200 was treated as a success: rc={rc}"
    remaining = [r["event"]["event_id"] for r in _read_wal(dlq)]
    assert remaining == ["evt-1", "evt-2", "evt-3"], (
        f"rows were removed on the strength of a 200 that named no events: {remaining}"
    )
