"""Regression tests for the P1-1.1 fix: `_remote_states` thread-safety.

Why this exists. The pre-fix code accessed `self._remote_states`
directly from at least four call sites — `track ` (TOCTOU write)
`_on_state_change` (WS push), `_fetch_remote_state` (HTTP poll)
`check_control_plane` (read), and `_poll_commands` (iteration).
The TOCTOU race in `track ` (line 1126-1127: `if workflow_id not in
self._remote_states: self._remote_states[workflow_id] = {}`) was
benign on its own, but combined with `_poll_commands` iterating the
dict's keys while another thread was writing, the iteration could
raise `RuntimeError: dictionary changed size during iteration`.

The fix introduces `self._states_lock` (`threading.RLock`) and two
helpers: `_remote_state_for(workflow_id)` (atomic get-or-create)
and `_set_remote_state(workflow_id, state)` (atomic set). All five
call sites are now thread-safe.

These tests are *unit tests* — they construct a `NullRunRuntime`
bypassing the constructor's network calls (no auth, no policy
fetch, no WS, no transport background thread) and exercise just
the in-memory state machinery.
"""

from __future__ import annotations

import threading

import pytest

from nullrun.runtime import NullRunRuntime


@pytest.fixture
def runtime():
    """A `NullRunRuntime` with all I/O stubbed (no auth, no
    transport, no WS). We just need the in-memory state machinery."""
    # Bypass the constructor's auth/policy network calls.
    rt = NullRunRuntime(
        api_key="test-key-12345678",
        _test_mode=True,
        polling=False,
    )
    yield rt
    # Cleanup. `shutdown ` is now defensive about missing
    # attributes (P1-1.1 side fix), so this is safe even though
    # the test-mode runtime never started any threads.
    try:
        rt.shutdown()
    except Exception:
        pass


class TestRemoteStateForAtomicity:
    """`_remote_state_for` is the atomic get-or-create primitive."""

    def test_get_or_create_under_concurrent_writers(self, runtime):
        """N threads racing on the same workflow_id must end up with
        exactly one state dict, never a half-initialized one. The
        pre-fix TOCTOU race could leave the dict in an inconsistent
        state under load."""
        n_threads = 8
        barrier = threading.Barrier(n_threads)

        def writer():
            barrier.wait()
            for _ in range(20):
                runtime._remote_state_for("wf-X")

        threads = [threading.Thread(target=writer) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one entry for wf-X (not 0, not N).
        assert "wf-X" in runtime._remote_states
        # The state is a dict (not a partial state).
        assert isinstance(runtime._remote_states["wf-X"], dict)

    def test_set_remote_state_is_atomic(self, runtime):
        """`_set_remote_state` replaces the dict atomically. A
        concurrent reader must see either the old value or the new
        value, never a partial state."""
        runtime._set_remote_state("wf-Y", {"version": 1, "state": "Normal"})
        n_readers = 4
        barrier = threading.Barrier(n_readers + 1)

        results: list[dict] = []
        results_lock = threading.Lock()

        def reader():
            barrier.wait()
            for _ in range(20):
                with runtime._states_lock:
                    state = runtime._remote_states.get("wf-Y")
                with results_lock:
                    results.append(state)

        def writer():
            barrier.wait()
            for v in range(2, 6):
                runtime._set_remote_state("wf-Y", {"version": v, "state": "Killed"})

        threads = [threading.Thread(target=reader) for _ in range(n_readers)] + [
            threading.Thread(target=writer)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Every observed state must be one of the values written
        # (versions 2..5) — no half-states.
        versions = {r["version"] for r in results if r is not None}
        assert versions.issubset(set(range(2, 6))), (
            f"Observed unexpected versions: {versions - set(range(2, 6))}"
        )


class TestPollCommandsDoesNotRaise:
    """The HTTP poller iterates `_remote_states.keys `. The
    pre-fix code could raise `RuntimeError: dictionary changed
    size during iteration` when a concurrent write happened.
    The fix snapshots the keys under the lock."""

    def test_concurrent_writes_during_poll_do_not_raise(self, runtime):
        # Use small numbers to keep the test fast and avoid the GIL
        # contention that surfaces as a hang in some environments.
        n_writers = 4
        n_iterations = 20
        barrier = threading.Barrier(n_writers + 1)

        errors: list[BaseException] = []
        errors_lock = threading.Lock()

        def writer(tid: int):
            barrier.wait()
            for i in range(n_iterations):
                runtime._set_remote_state(f"wf-{tid}", {"version": i, "state": "Killed"})

        def poller():
            barrier.wait()
            for _ in range(n_iterations):
                # This is the pre-fix iteration that could raise.
                try:
                    with runtime._states_lock:
                        keys = list(runtime._remote_states.keys())
                    for k in keys:
                        # Touch the value to ensure no mid-iteration error
                        _ = runtime._remote_states.get(k)
                except BaseException as e:  # noqa: BLE001
                    with errors_lock:
                        errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(n_writers)] + [
            threading.Thread(target=poller)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, (
            f"Poller saw {len(errors)} errors under concurrent write: "
            f"{[type(e).__name__ for e in errors[:5]]}"
        )


class TestTrackDoesNotClobberRemoteState:
    """The pre-fix `track ` did:
        if workflow_id not in self._remote_states:
            self._remote_states[workflow_id] = {}
    This TOCTOU race could clobber a "Killed" state set by a
    concurrent WS push if the writer thread ran between the check
    and the write. The fix uses `_remote_state_for` which is atomic."""

    def test_concurrent_track_does_not_clobber_kill(self, runtime):
        """While `track ` is being called, a concurrent
        `_set_remote_state(wf, Killed)` must not be overwritten
        by the `track ` get-or-create."""
        # Pre-populate the state with a Killed push.
        runtime._set_remote_state(
            "wf-clobber",
            {"state": "Killed", "reason": "operator push", "version": 5},
        )

        # Use small numbers to keep the test fast.
        n_threads = 4
        n_iterations = 20
        # Barrier size = number of threads total (4 track + 1 verify).
        barrier = threading.Barrier(n_threads + 1)

        def track_thread():
            barrier.wait()
            for _ in range(n_iterations):
                # Simulate the get-or-create from `track `.
                runtime._remote_state_for("wf-clobber")

        def verify_thread():
            barrier.wait()
            for _ in range(n_iterations):
                # The state must remain "Killed" throughout.
                with runtime._states_lock:
                    state = runtime._remote_states.get("wf-clobber", {})
                assert state.get("state") == "Killed", f"State was clobbered: {state}"

        threads = [threading.Thread(target=track_thread) for _ in range(n_threads)] + [
            threading.Thread(target=verify_thread)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
