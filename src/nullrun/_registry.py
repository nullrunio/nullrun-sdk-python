"""Runtime registry — single source of truth for the active ``NullRunRuntime``.

Why a registry
--------------
Historically three different slots carried the "current runtime"
identity:

* ``nullrun.runtime._runtime`` — module-level in ``runtime.py``
* ``NullRunRuntime._instance`` — class-level singleton
* ``nullrun.decorators._runtime`` — module-level in ``decorators.py``

Each writer was independent. ``nullrun.init()`` wrote all three;
``NullRunRuntime.get_instance()`` wrote only the class-level slot;
``decorators._get_or_create_runtime()`` wrote only the decorators
slot. Concurrent ``init()`` + ``@protect`` could race and leave one
of the three pointing at a dead runtime, dropping ``span_start`` /
``span_end`` events on the floor (see audit 2026-07-05 H2).

Phase 3 unifies the three writers behind a single
:class:`RuntimeRegistry` so every consumer reads from one place.
The class-level ``NullRunRuntime._instance`` is preserved as a
proxy for backward compatibility (test fixtures, third-party
extensions, dashboard scripts that introspect the SDK), but it now
delegates to the registry.

Thread safety
-------------
The registry uses an ``RLock`` because the same thread can re-enter
during a ``get_instance`` -> ``shutdown`` -> ``get_instance`` sequence
(Phase 5 #5.3 documented the original deadlock from a plain Lock).
Readers (the hot path on every ``@protect`` call) take a snapshot
of the instance pointer once and release the lock immediately;
they do NOT hold the lock across downstream calls (e.g. ``runtime
.check_workflow_budget()``), which would otherwise serialise every
``@protect`` invocation behind the lock.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Imported only for type checking to keep this module lightweight
    # (it sits on the ``import nullrun`` critical path). The runtime
    # class imports ``RuntimeRegistry``, so a runtime import here
    # would create a cycle.
    from nullrun.runtime import NullRunRuntime


class RuntimeRegistry:
    """Thread-safe single-slot registry for the active runtime.

    The registry is a process-wide singleton (``_registry`` below).
    Tests that need isolation should use the
    :func:`replace_for_test` context manager rather than creating a
    second registry; multiple runtimes per process are not supported
    by design (the SDK's enforce-the-active-runtime contract assumes
    exactly one writer at a time).

    Lifetime
    --------
    The instance pointer is ``None`` between ``init`` calls. Reads
    of a ``None`` registry return ``None`` — callers must decide
    whether a missing runtime is an error (most do, via ``init``'s
    NR-C001 raise site). The registry never garbage-collects a
    runtime on its own; callers must call :meth:`shutdown` (or
    :func:`nullrun.shutdown`) to release the runtime's background
    threads before discarding it.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._instance: NullRunRuntime | None = None

    def get(self) -> NullRunRuntime | None:
        """Return the current runtime or ``None``.

        Hot path: takes the lock only long enough to read the
        pointer, then releases. Callers must treat the returned
        value as a snapshot — the runtime may be replaced by a
        concurrent ``init`` immediately after the call returns.
        """
        with self._lock:
            return self._instance

    def set(self, runtime: NullRunRuntime) -> NullRunRuntime | None:
        """Install ``runtime`` as the active instance.

        Returns the previously-installed runtime (or ``None``) so
        the caller can shut it down before it is replaced. The
        swap is atomic — a concurrent ``get`` sees either the
        old or the new instance, never a half-constructed one.
        """
        with self._lock:
            previous = self._instance
            self._instance = runtime
            return previous

    def clear(self) -> NullRunRuntime | None:
        """Drop the registry's reference to the runtime.

        Does NOT shut down the runtime itself — callers must do
        that explicitly. Returns the previous instance so the
        caller can shut it down before discarding it (otherwise
        its background threads — WS poller, transport flush —
        would leak until the next ``set``).
        """
        with self._lock:
            previous = self._instance
            self._instance = None
            return previous

    def replace_for_test(self, runtime: NullRunRuntime | None) -> NullRunRuntime | None:
        """Context-manager-friendly variant for test isolation.

        Returns a callable that the test fixture can invoke in its
        teardown to restore the prior state without explicitly
        holding the lock across the body of the test.
        """
        with self._lock:
            previous = self._instance
            self._instance = runtime
            return previous


# Process-wide singleton. Every consumer (``runtime.py``,
# ``decorators.py``, ``_handle.py``, ``__init__.py``) reads from
# this same registry — there is no second source of truth.
_registry = RuntimeRegistry()


def get_registry() -> RuntimeRegistry:
    """Return the process-wide registry.

    Exposed as a function (not a module attribute) so tests can
    monkeypatch the registry in one place and every consumer sees
    the swap. A module-level constant would be imported by name at
    function-definition time and bypass the patch.
    """
    return _registry


def get_active_runtime() -> NullRunRuntime | None:
    """Convenience pass-through used by ``@protect`` / ``track_*``.

    Equivalent to ``get_registry().get()`` but one fewer attribute
    lookup in the hot path.
    """
    return _registry.get()


__all__ = [
    "RuntimeRegistry",
    "get_registry",
    "get_active_runtime",
]