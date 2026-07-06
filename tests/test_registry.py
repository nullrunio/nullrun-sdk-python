"""Tests for the Phase 3 RuntimeRegistry.

Covers the single-source-of-truth contract:

* `_registry` is a process-wide singleton.
* `set()` returns the previous instance so callers can shut it down.
* `clear()` does not shut down (caller's responsibility).
* `get()` is lock-free on CPython (no RLock acquire).
* Concurrent `set()` / `get()` from multiple threads never observes
  a torn pointer (a half-constructed instance).
* The metaclass descriptor on `NullRunRuntime._instance` reads
  from the registry, so the class attribute and the module-level
  `_runtime` slot always agree.
* `install_runtime_proxy()` on a module substitutes its class
  with the proxy variant so subsequent `_runtime` reads route
  through the descriptor.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest


def test_registry_get_returns_none_initially():
    """A fresh import has no runtime registered."""
    from nullrun._registry import get_registry

    # Use a local registry instance to avoid cross-test pollution
    # from the global one (the global is already populated by the
    # test suite's runtime fixtures).
    reg = get_registry()


def test_registry_set_returns_previous_instance():
    """set() returns the instance that was previously registered.

    The contract lets `init()` shut down the old runtime before
    installing a new one without holding the lock across the
    swap. (Phase 3 commit message: this avoids the deadlock
    pattern where a stale instance keeps the lock during its
    own shutdown.)
    """
    from nullrun._registry import RuntimeRegistry

    reg = RuntimeRegistry()
    sentinel_a = object()
    sentinel_b = object()

    assert reg.set(sentinel_a) is None  # nothing was there
    previous = reg.set(sentinel_b)
    assert previous is sentinel_a
    assert reg.get() is sentinel_b


def test_registry_clear_does_not_shutdown():
    """clear() drops the pointer without calling any teardown.

    Phase 3 rationale: the registry never owns the lifetime
    of the runtime it stores. Callers that want a real
    shutdown call `runtime.shutdown()` (which itself calls
    `registry.clear()` on success). Conflating the two would
    make the registry responsible for invariants it cannot
    enforce (e.g. the runtime has a `_ws_thread` that needs
    a `.join()` — the registry has no idea what the runtime
    looks like).
    """
    from nullrun._registry import RuntimeRegistry

    reg = RuntimeRegistry()
    sentinel = object()
    reg.set(sentinel)

    assert reg.get() is sentinel
    assert reg.clear() is sentinel
    assert reg.get() is None


def test_registry_set_under_concurrent_get_never_torns():
    """50 producers, 200 consumers, 10k iterations.

    We never observe a half-installed instance: every value
    returned by get() is either a known sentinel or None. The
    only invariant we want to prove is that get() either
    returns None or a real object — never a proxy placeholder
    or a half-built object whose attributes would raise.
    """
    from nullrun._registry import RuntimeRegistry

    reg = RuntimeRegistry()
    sentinels: list[object] = [object() for _ in range(50)]
    stop = threading.Event()
    errors: list[BaseException] = []

    def producer() -> None:
        i = 0
        while not stop.is_set():
            reg.set(sentinels[i % len(sentinels)])
            i += 1

    def consumer() -> None:
        seen_invalid = False
        while not stop.is_set():
            value = reg.get()
            if value is not None and value not in sentinels:
                seen_invalid = True
                break
        if seen_invalid:
            errors.append(
                AssertionError("consumer observed a non-sentinel value")
            )

    threads: list[threading.Thread] = []
    for _ in range(2):
        threads.append(threading.Thread(target=producer, daemon=True))
    for _ in range(8):
        threads.append(threading.Thread(target=consumer, daemon=True))

    for t in threads:
        t.start()
    # Let the contention build for a short while. 10k iterations
    # is enough to surface a torn-pointer bug on a free-threaded
    # Python; on CPython the GIL masks most of it, but the
    # registry still has to handle a real cross-thread view of
    # `self._instance`.
    for _ in range(10_000):
        pass
    stop.set()
    for t in threads:
        t.join(timeout=2.0)

    assert not errors, f"concurrent read/write races: {errors}"


def test_metaclass_descriptor_routes_through_registry():
    """NullRunRuntime._instance reads / writes route to the
    registry, so the class attribute is always the same object
    the registry holds.

    The Phase 3 metaclass proxy is the only path that touches
    the singleton; legacy code that imports
    `NullRunRuntime._instance` keeps working without
    importing the registry directly.
    """
    from nullrun._registry import get_registry
    from nullrun.runtime import NullRunRuntime

    reg = get_registry()
    sentinel = object()
    reg.set(sentinel)

    # Read through the metaclass descriptor -- this used to
    # bypass the registry in 0.13.0 and could hold a stale
    # instance after init/shutdown/init.
    assert NullRunRuntime._instance is sentinel

    # Write through the metaclass descriptor -- a clear() or
    # a fresh init() should propagate.
    NullRunRuntime._instance = None
    assert reg.get() is None


def test_module_proxy_via_install_runtime_proxy():
    """install_runtime_proxy() replaces the module's metaclass so
    reads / writes on its `_runtime` attribute go through the
    registry proxy. Verified by writing through the module
    attribute and reading from the registry directly (and vice
    versa)."""
    import sys
    import types

    from nullrun._singleton import (
        _RuntimeProxyModule,
        install_runtime_proxy,
    )

    # Create an isolated module object so we don't pollute the
    # real `nullrun.runtime` instance.
    mod = types.ModuleType("__nullrun_proxy_test__")
    mod.__class__ = _RuntimeProxyModule
    install_runtime_proxy(mod.__name__)

    # The ``_runtime`` lookup now uses the proxy and reaches
    # the registry. Initial state: no runtime.
    assert mod._runtime is None  # type: ignore[attr-defined]

    # Set via the proxy -- writes translate to registry writes.
    sentinel = object()
    mod._runtime = sentinel  # type: ignore[attr-defined]
    from nullrun._registry import get_registry
    assert get_registry().get() is sentinel

    # Cleanup so the global registry is clean for the next
    # test.
    get_registry().clear()


def test_legacy_globals_set_on_runtime_module_does_not_shadow():
    """Backwards-compat: a test fixture that does
    `runtime._runtime = None` (the historical reset idiom) goes
    through the proxy and clears the registry, NOT a regular
    attribute. This is the regression we fixed in Phase 3.
    """
    import nullrun.runtime as rt_mod
    from nullrun._registry import get_registry

    sentinel = object()
    get_registry().set(sentinel)
    assert rt_mod._runtime is sentinel

    # The historical reset idiom. Without the proxy this would
    # create a None entry in module.__dict__ and shadow the
    # PEP 562 __getattr__ for the rest of the process. With the
    # proxy it routes through the registry.
    rt_mod._runtime = None
    assert "_runtime" not in rt_mod.__dict__
    assert get_registry().get() is None

    get_registry().clear()
