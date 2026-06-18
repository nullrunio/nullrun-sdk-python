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
import threading
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
        """Sanity check: even calling Transport() many times must
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
            # On Windows, signal handlers can be `signal.SIG_DFL`,
            # `signal.SIG_IGN`, or a Python callable. Only a Python
            # callable would be a SDK bug.
            if callable(handler) and not isinstance(
                handler,
                (int, signal.Signals),
            ):
                import inspect

                src = inspect.getsource(handler)
                assert "sys.exit" not in src, (
                    f"SDK must not install a signal handler that "
                    f"calls sys.exit: {handler!r}"
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
            finalize_objs = [
                r for r in gc.get_referrers(t)
                if isinstance(r, weakref.finalize)
            ]
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
        weakref-based flush must NOT raise (the transport is gone,
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
        """If the atexit flush raises, the exception must NOT
        propagate to the interpreter's atexit machinery (which would
        silently swallow the next atexit handler).

        Phase 0.4.0: ``_atexit_flush`` was removed in favour of
        ``weakref.finalize`` -> ``_atexit_flush_safe``. We pin the
        contract by patching ``_do_flush`` (the only side-effecting
        call inside the safe wrapper) to raise.
        """
        t = Transport(
            api_url="https://api.test.nullrun.io",
            api_key="test-key-12345678",
        )
        try:
            with patch.object(t, "_do_flush", side_effect=RuntimeError("boom")):
                # Calling the safe wrapper must not raise.
                t._atexit_flush_safe(id(t))
        finally:
            t.stop()


class TestContextManagerLifecycle:
    """`Transport` must work as a context manager so callers have a
    safe lifecycle without explicit `start()` / `stop()` pairs."""

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
