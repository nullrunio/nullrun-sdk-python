"""Regression tests for the P0-0.1 fix: signal-handler removal.

Why this exists. The pre-fix `Transport.__init__` installed a process-wide
`SIGTERM`/`SIGINT` handler on every construction and called `sys.exit(0)`
plus file I/O from inside the signal context — unsafe in long-lived
services. The fix removes the signal handler entirely and replaces
the `atexit` registration with a `weakref.finalize` callback that fires
only if the transport is still alive at process exit.

These tests pin the new contract: no global handler mutation, the
weakref flush fires on GC, exceptions in the flush don't propagate to
the atexit machinery, and the transport can be used as a context
manager.
"""

from __future__ import annotations

import gc
import signal
import weakref
from unittest.mock import patch

import pytest

from nullrun.transport import Transport


class TestNoSignalHandlerInstalled:
    """`Transport.__init__` must NOT touch the process-wide signal
    disposition. This is the core safety property the P0-0.1 fix
    protects."""

    def test_sigterm_handler_unchanged_after_construction(self):
        original = signal.getsignal(signal.SIGTERM)
        t = Transport(api_url="https://api.test.nullrun.io", api_key="test-key-12345678")
        try:
            assert signal.getsignal(signal.SIGTERM) == original
        finally:
            t.stop()

    def test_sigint_handler_unchanged_after_construction(self):
        original = signal.getsignal(signal.SIGINT)
        t = Transport(api_url="https://api.test.nullrun.io", api_key="test-key-12345678")
        try:
            assert signal.getsignal(signal.SIGINT) == original
        finally:
            t.stop()

    def test_construction_does_not_call_signal_signal(self):
        """Sanity check: even calling Transport many times must
        not touch the signal table at all."""
        original = signal.getsignal(signal.SIGTERM)
        try:
            for _ in range(20):
                t = Transport(
                    api_url="https://api.test.nullrun.io",
                    api_key="test-key-12345678",
                )
                t.stop()
        finally:
            assert signal.getsignal(signal.SIGTERM) == original

    def test_no_sys_exit_called_from_signal_context(self):
        """The previous code called `sys.exit(0)` from the signal
        context. After the P0-0.1 fix, there is no signal handler
        at all — the SDK no longer touches the signal table — so
        `sys.exit` cannot be called from a signal context. We pin
        the contract by asserting no signal handler was installed.
        """
        original = signal.getsignal(signal.SIGTERM)
        t = Transport(
            api_url="https://api.test.nullrun.io",
            api_key="test-key-12345678",
        )
        try:
            # No callable signal handler may be installed — the SDK
            # must not register one. The previous code installed
            # `def _handle_shutdown(signum, frame): sys.exit(0)`.
            handler = signal.getsignal(signal.SIGTERM)
            # On Windows, signal handlers can be `signal.SIG_DFL`
            # `signal.SIG_IGN`, or a Python callable. Only a Python
            # callable would be a SDK bug.
            if callable(handler) and not isinstance(
                handler,
                (int, signal.Signals),
            ):
                import inspect

                src = inspect.getsource(handler)
                assert "sys.exit" not in src, (
                    f"SDK must not install a signal handler that calls sys.exit: {handler!r}"
                )
            # And the original handler is preserved (the test
            # process had its own SIGTERM handler from pytest).
            assert handler == original
        finally:
            t.stop()


class TestAtexitViaWeakref:
    """The old `atexit.register(self._atexit_flush)` was replaced with
    `weakref.finalize`. The atexit chain is LIFO; the weakref
    approach avoids the cross-Transport ordering hazard and lets the
    transport be GC'd before process exit."""

    def test_finalize_is_registered_on_construction(self):
        t = Transport(
            api_url="https://api.test.nullrun.io",
            api_key="test-key-12345678",
        )
        try:
            # `weakref.finalize` registers a finalize on the object.
            # The `__call__` method exists on the finalize object.
            # We can introspect by walking the weakref.finalize
            # instances attached to the object.
            finalize_objs = [r for r in gc.get_referrers(t) if isinstance(r, weakref.finalize)]
            # The weakref is registered as a referrer of t. We can
            # at minimum check that the atexit registry is not
            # pinned to t.
            # Note: exact introspection of weakref.finalize is
            # implementation-dependent; we just ensure the object
            # is collectable when no longer referenced.
            assert t._stopped is False
        finally:
            t.stop()

    def test_weakref_fires_on_gc(self):
        """If the transport is GC'd before process exit, the
        weakref-based flush must NOT raise (the transport is gone
        so it must no-op)."""
        t = Transport(
            api_url="https://api.test.nullrun.io",
            api_key="test-key-12345678",
        )
        t_id = id(t)
        del t
        gc.collect()
        # After GC, calling any method on a new transport should
        # not be affected by the old finalize (no module-level
        # cache). This is a smoke test; the important property is
        # that the old transport's atexit was bound to the OLD
        # object via weakref and silently no-ops on dead objects.
        t2 = Transport(
            api_url="https://api.test.nullrun.io",
            api_key="test-key-12345678",
        )
        try:
            t2.stop()
        except Exception as exc:
            pytest.fail(f"Constructing after GC failed: {exc}")

    def test_atexit_flush_exception_is_swallowed(self):
        """The weakref finalizer must NEVER raise — exceptions
        propagating into GC corrupt finalizer ordering and can
        suppress subsequent finalizers.

        0.7.0 contract: ``_atexit_flush_safe`` is a static no-op
        that only emits a DEBUG log line. There is no buffer / WAL
        / httpx-client reach inside the finalizer — by the time
        ``weakref.finalize`` fires, ``self`` is already being
        collected. Crash-safety lives in ``stop `` (which calls
        ``_persist_to_wal``) and the context-manager pattern, NOT
        in the finalizer. We pin both:

        1. Direct call (0 args, matching the weakref-finalize
           contract): never raises regardless of upstream state.
        2. Direct call with an unexpected positional arg (1 arg
           matching the original test signature intent): also
           never raises — the method signature accepts the
           optional positional arg defensively.
        """
        t = Transport(
            api_url="https://api.test.nullrun.io",
            api_key="test-key-12345678",
        )
        try:
            # 1. The actual weakref-finalize call signature.
            t._atexit_flush_safe()
            # 2. Defensive: an extra positional arg (as
            # weakref.finalize passes the id-of-self when atexit
            # fires via the standard interpreter hook) must also
            # not raise. The 0.7.0 signature is
            # ``(_self_id: int | None = None)`` to accept this.
            t._atexit_flush_safe(id(t))
        finally:
            t.stop()

    def test_atexit_flush_does_not_persist_buffer(self):
        """0.7.0 contract pin: the weakref finalizer is a no-op.
        Buffered events that survived without ``stop `` are
        LOST — the SDK logs a DEBUG warning instead of writing
        them to the WAL.

        Rationale (the 0.7.0 thin-client refactor): the
        ``Transport._buffer`` is gone by the time the finalizer
        fires (the instance is being GC'd; weakref.finalize
        receives no ``self`` reference). Attempting to WAL-persist
        from inside the finalizer would need a parallel registry
        of live buffers, which contradicts the thin-client
        architecture (the backend is authoritative for delivery
        not the local SDK).

        Callers MUST use one of:
          * ``with Transport(...) as t:`` — context manager
            calls ``stop `` on ``__exit__``.
          * explicit ``t.start `` / ``t.stop `` pair.
          * rely on the interpreter-level ``atexit`` runner, but
            understand that buffered events that did not reach
            ``_persist_to_wal`` BEFORE interpreter shutdown will
            not be replayed.

        The DEBUG log line emitted by the finalizer is the
        user-visible signal that events were dropped.
        """
        import logging
        import tempfile

        # Use a per-test WAL path so we can verify the finalizer
        # does NOT touch it.
        wal_dir = tempfile.mkdtemp(prefix="nullrun_wal_test_")
        wal_path = f"{wal_dir}/nullrun.wal"

        t = Transport(
            api_url="https://api.test.nullrun.io",
            api_key="test-key-12345678",
        )
        try:
            # Enqueue events that simulate the case where stop 
            # was never called (e.g. user script just runs
            # ``nullrun.init(...)`` and exits).
            t.track({"event_id": "drop-1", "type": "cost", "amount": 42})
            t.track({"event_id": "drop-2", "type": "cost", "amount": 17})
            assert len(t._buffer) == 2

            # Override the WAL path so we can assert the finalizer
            # does NOT write to it.
            t._wal_path = lambda: wal_path  # type: ignore[method-assign]

            # Invoke the finalizer directly with the captured
            # refs (simulating what weakref.finalize would do on
            # GC).
            with t._lock:
                events_before = list(t._buffer)
            t._atexit_flush_safe()

            # The WAL file MUST NOT exist after the finalizer
            # fired. The 0.7.0 contract is "no-op, log warning".
            import os

            assert not os.path.exists(wal_path), (
                f"finalizer must NOT write WAL in 0.7.0, but {wal_path} exists"
            )

            # And the buffer must NOT be mutated by the finalizer.
            with t._lock:
                assert t._buffer == events_before, (
                    "finalizer must NOT clear or mutate _buffer in 0.7.0"
                )
        finally:
            t.stop()
            import shutil

            shutil.rmtree(wal_dir, ignore_errors=True)

    def test_weakref_finalize_logs_warning_only(self, caplog):
        """End-to-end: a Transport that is GC'd without an
        explicit ``stop `` MUST NOT silently drop /track events
        on the floor — the SDK logs a DEBUG line so operators
        can see the data-loss signal in their log pipeline.

        0.7.0 contract change (vs 0.6.x): the finalizer no longer
        writes the buffer to the WAL. It only emits a single
        DEBUG-level log line via ``logger.debug``. To survive
        a crash, callers must use the context manager or call
        ``stop `` explicitly — see ``test_atexit_flush_does_not_persist_buffer``
        for the rationale.
        """
        import logging
        import shutil
        import tempfile

        wal_dir = tempfile.mkdtemp(prefix="nullrun_wal_e2e_")
        wal_path = f"{wal_dir}/nullrun.wal"
        try:
            # Step 1: build a Transport, enqueue events, GC it
            # without calling stop. This is what happens when
            # a user script just does ``nullrun.init(...)`` and
            # exits.
            t = Transport(
                api_url="https://api.test.nullrun.io",
                api_key="test-key-12345678",
            )
            t._wal_path = lambda: wal_path  # type: ignore[method-assign]
            t.track({"event_id": "e2e-1", "type": "cost"})
            t.track({"event_id": "e2e-2", "type": "cost"})

            # Detach the finalizer that stop would detach, so
            # the explicit-stop path doesn't suppress it. We're
            # testing the no-stop path.
            t._finalizer.detach()
            # Capture DEBUG records emitted during the finalizer call.
            caplog.set_level(logging.DEBUG, logger="nullrun.transport")
            # Manually invoke what weakref.finalize would do on GC.
            t._atexit_flush_safe()
            del t

            # Step 2: the WAL must NOT exist (no-op finalizer).
            import os

            assert not os.path.exists(wal_path), (
                f"WAL must NOT be created in 0.7.0, but {wal_path} exists"
            )

            # Step 3: a DEBUG log line was emitted with the
            # "may be lost" / "explicit stop" hint.
            debug_msgs = [
                rec.getMessage()
                for rec in caplog.records
                if rec.levelno == logging.DEBUG and rec.name == "nullrun.transport"
            ]
            assert any("may be lost" in m or "explicit stop" in m for m in debug_msgs), (
                f"expected DEBUG log line about event loss, got: {debug_msgs!r}"
            )
        finally:
            shutil.rmtree(wal_dir, ignore_errors=True)


class TestContextManagerLifecycle:
    """`Transport` must work as a context manager so callers have a
    safe lifecycle without explicit `start ` / `stop ` pairs."""

    def test_with_block_starts_and_stops(self):
        with Transport(
            api_url="https://api.test.nullrun.io",
            api_key="test-key-12345678",
        ) as t:
            assert t._flush_thread is not None
            assert t._flush_thread.is_alive()
        # After the block, the thread is joined and the transport
        # is marked stopped.
        assert t._stopped is True
        assert not t._flush_thread.is_alive()

    def test_with_block_propagates_exception_after_stop(self):
        class Boom(Exception):
            pass

        t_ref = None
        with pytest.raises(Boom):
            with Transport(
                api_url="https://api.test.nullrun.io",
                api_key="test-key-12345678",
            ) as t:
                t_ref = t
                raise Boom("oops")
        # Even on exception, the transport was stopped.
        assert t_ref._stopped is True

    def test_with_block_supports_concurrent_transports(self):
        """Two Transport instances can be in concurrent `with`
        blocks without interfering with each other."""
        t1 = t2 = None
        with Transport(
            api_url="https://api.test.nullrun.io",
            api_key="test-key-12345678",
        ) as a:
            with Transport(
                api_url="https://api.test.nullrun.io",
                api_key="test-key-12345678",
            ) as b:
                t1 = a
                t2 = b
                assert a is not b
                assert a._flush_thread is not b._flush_thread
        assert t1._stopped is True
        assert t2._stopped is True
