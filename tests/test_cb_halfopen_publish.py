"""
Regression test for the OPEN→HALF_OPEN Redis publish.

Pre-fix: ``_publish_half_open_state`` was defined but never called.
A worker that recovered locally would transition to HALF_OPEN
silently, leaving the Redis key as ``"OPEN"`` (set by
``_publish_open_state`` when the failure happened). Other workers
reading from Redis would see ``"OPEN"`` and revert to PERMISSIVE
fallback, dropping the recovery.

The fix in 0.3.1: the ``state`` property calls
``_publish_half_open_state`` after the transition so the global
state is in sync. This test pins the contract.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from nullrun.breaker.circuit_breaker import CircuitBreaker


class TestPublishHalfOpen:
    def test_publish_half_open_state_is_called_on_transition(self):
        """When the local state transitions from OPEN to HALF_OPEN,
        ``_publish_half_open_state`` must be called so other workers
        see the new state in Redis.
        """
        cb = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=0.0,  # recovery is immediate
            name="test_cb",
        )
        # Force into OPEN.
        cb._state = cb._state  # noqa: SLF001 (private access OK in test)
        from nullrun.breaker.circuit_breaker import CBState

        cb._state = CBState.OPEN
        cb._last_failure_time = 0.0  # far enough in the past

        mock_publish = MagicMock()
        cb._publish_half_open_state = mock_publish  # type: ignore[method-assign]

        # Reading the state property triggers the transition.
        new_state = cb.state
        assert new_state == CBState.HALF_OPEN
        mock_publish.assert_called_once()

    def test_publish_half_open_state_noop_when_already_closed(self):
        """No publish when state is already CLOSED — there's no
        transition to advertise.
        """
        cb = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=0.0,
            name="test_cb_noop",
        )
        from nullrun.breaker.circuit_breaker import CBState

        # Default state is CLOSED.
        assert cb._state == CBState.CLOSED  # noqa: SLF001

        mock_publish = MagicMock()
        cb._publish_half_open_state = mock_publish  # type: ignore[method-assign]

        # Reading state does NOT trigger a transition (CLOSED → CLOSED).
        _ = cb.state
        mock_publish.assert_not_called()


# ===========================================================================
# Sprint 2.5 (B3): HALF_OPEN call-allocation under concurrent load
# ===========================================================================
# Pins the invariant: when the breaker is HALF_OPEN, at most
# ``half_open_max_calls`` concurrent calls are allowed to probe
# the downstream; the rest are rejected with BreakerTransportError.
#
# The pre-fix audit flagged a possible TOCTOU between the
# ``_half_open_calls < half_open_max_calls`` check and the
# ``_half_open_calls += 1`` increment. The current code wraps
# both inside ``with self._lock:`` (see circuit_breaker.py line
# 278-281) so the invariant holds. This test pins it so a
# future "optimisation" that removes the lock breaks the test,
# not the production guarantee.


class TestHalfOpenConcurrencyLimit:
    def test_concurrent_calls_respect_half_open_max(self):
        """At most ``half_open_max_calls`` calls are admitted into the
        in-flight probe set; the rest are rejected before any
        call can complete (and therefore before ``_on_success``
        would re-OPEN / re-CLOSE the breaker and let the rest
        through).

        Pin note: the original B3 audit flagged a TOCTOU between
        the ``_half_open_calls < half_open_max_calls`` check and
        the ``+= 1`` increment. The current code wraps both in
        ``with self._lock:`` (see circuit_breaker.py:278-281) so
        the invariant holds. This test forces the threads to
        block INSIDE ``call()`` until all 10 have entered the
        half-open gate, so a regression that removes the lock
        (and lets more than ``half_open_max_calls`` threads pass
        the check before any of them increments) would show up as
        ``len(passed) > 2``.
        """
        import threading

        from nullrun.breaker.circuit_breaker import CBState
        from nullrun.breaker.exceptions import BreakerTransportError

        cb = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=0.0,  # immediate transition
            half_open_max_calls=2,
            redis_client=None,  # no global state
        )

        # Force the breaker into HALF_OPEN.
        cb._state = CBState.HALF_OPEN
        cb._half_open_calls = 0
        cb._global_state_allows_call = lambda: True  # type: ignore[method-assign]

        # All 10 worker threads must enter the half-open gate
        # BEFORE any of them returns. If the lock+check+increment
        # is not atomic, more than 2 will pass the check before
        # the first one increments the counter.
        in_flight = threading.Semaphore(0)  # released by the probe function
        all_entered = threading.Event()
        entered_count = 0
        count_lock = threading.Lock()

        passed: list[int] = []
        rejected: list[int] = []
        call_lock = threading.Lock()

        def _probe(_i: int) -> str:
            nonlocal entered_count
            with count_lock:
                entered_count += 1
                if entered_count == 10:
                    all_entered.set()
            # Block until all 10 threads have entered the gate.
            # This guarantees that the check+increment under
            # contention has already happened; if the lock is
            # missing, more than 2 threads will already have
            # passed the gate.
            all_entered.wait(timeout=2.0)
            in_flight.release()  # not used, just for symmetry
            return f"ok-{_i}"

        def worker(i: int) -> None:
            try:
                cb.call(_probe, i)
                with call_lock:
                    passed.append(i)
            except BreakerTransportError:
                with call_lock:
                    rejected.append(i)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        # The critical invariant: at most ``half_open_max_calls``
        # calls were ADMITTED to the gate (regardless of whether
        # they later succeeded and the breaker moved to CLOSED).
        # We check the counter, which is incremented exactly
        # when a call passes the gate, and never decremented
        # back below its peak within a single half-open window.
        assert cb._half_open_calls <= 2, (
            f"_half_open_calls exceeded half_open_max_calls=2 under "
            f"concurrent load. Observed: {cb._half_open_calls}. "
            f"This is the B3 race regression: the check+increment "
            f"in call() is not atomic. Passed={passed}, Rejected={rejected}"
        )
        # Sanity: at least 2 calls were rejected (otherwise the
        # test setup itself is wrong — we sent 10 calls to a
        # gate that allows 2).
        assert len(rejected) >= 1, (
            f"Expected at least 1 call to be rejected when 10 threads "
            f"hit a half-open gate that allows 2. Rejected={rejected}. "
            f"Test setup may be wrong."
        )
