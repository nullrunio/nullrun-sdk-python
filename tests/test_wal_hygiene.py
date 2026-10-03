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


def test_windows_states_the_degradation_instead_of_failing_at_import(transport, caplog):
    """No `flock` here is a supported configuration, and it says so once.

    The failure this prevents is not an ImportError at module load — it is the
    silent one: a multi-process deployment on Windows, or on any platform
    without flock, believing it has the same protection it has on Linux.
    """
    from nullrun import transport as transport_mod

    if transport_mod.fcntl is not None:
        pytest.skip("this platform has flock; the degraded branch cannot be reached")

    with caplog.at_level("WARNING"):
        with transport._wal_file_lock() as acquired:
            assert acquired is True, "a platform without flock must still be able to write"
    assert metrics.transport.wal_lock == "unavailable"

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


def test_a_terminal_refusal_that_cannot_be_recorded_stays_on_the_retry_path(
    transport, monkeypatch
):
    """A permanent rejection with no DLQ space must not clear `.inflight`.

    `_quarantine_to_dlq` clears the in-flight file because the events are
    parked. If the park failed, that clear is the loss: the events are in
    neither the DLQ nor the buffer nor `.inflight`. The refusal will simply
    come back on the next send, which is the right outcome for a rejection we
    could not record.
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
    assert [e["event_id"] for e in transport._buffer] == ["evt-1"]


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
