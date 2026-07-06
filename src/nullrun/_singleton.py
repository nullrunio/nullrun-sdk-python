# Backwards-compat proxy descriptor for ``NullRunRuntime._instance``.

# Phase 3 (2026-07-05) refactored the singleton slot into the
# ``nullrun._registry.RuntimeRegistry`` so there is exactly one
# source of truth. External code (test fixtures, third-party
# extensions, dashboard scripts) still introspects
# ``NullRunRuntime._instance`` — this descriptor makes those reads
# and writes route to the registry transparently.
#
# Why a metaclass rather than a property: ``property`` defined in
# the class body fires only on instance access (the descriptor
# protocol requires the attribute to be looked up on the instance,
# not the class). For ``NullRunRuntime._instance`` (a class-level
# access) the descriptor must live on the metaclass. We keep the
# metaclass local to this module so it does not affect subclasses
# declared elsewhere — only the singleton attribute goes through
# the metaclass, every other class attribute is unaffected.

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nullrun._registry import RuntimeRegistry


class _InstanceProxy:
    """Descriptor returning the registry's active runtime.

    Implements ``__get__`` and ``__set__`` so it works both for
    ``NullRunRuntime._instance`` (class-level access through the
    metaclass) and any ``instance._instance`` reads that existing
    subclass code might attempt.
    """

    def __get__(self, instance: Any, owner: Any) -> Any:
        from nullrun._registry import get_active_runtime

        return get_active_runtime()

    def __set__(self, instance: Any, value: Any) -> None:
        from nullrun._registry import get_registry

        registry: RuntimeRegistry = get_registry()
        if value is None:
            registry.clear()
        else:
            registry.set(value)


class _NullRunRuntimeMeta(type):
    """Metaclass that exposes ``_instance`` as a registry-backed proxy.

    Python only invokes a descriptor on class-level access if the
    descriptor lives on the metaclass (``type.__getattribute__``
    consults the type's metaclass first when looking up a data
    descriptor). Defining ``_instance`` here routes the canonical
    singleton access path through the RuntimeRegistry.
    """

    _instance = _InstanceProxy()


__all__ = ["_InstanceProxy", "_NullRunRuntimeMeta"]

def install_module_proxy(module, attribute_name: str = "_runtime") -> None:
    """Install a descriptor on module that proxies the attribute
    to the registry.

    Backwards-compat for code that imports
    nullrun.runtime._runtime or
    nullrun.decorators._runtime directly — historically these
    were plain module attributes holding the active runtime. After
    Phase 3 the registry is the source of truth, so the module
    attribute is now a property-style proxy.

    Args:
        module: The module object to patch.
        attribute_name: Name of the attribute to replace. Defaults
            to "_runtime" which is what both runtime.py and
            decorators.py historically named their module-level
            slot.

    Implementation note: we use a per-module property so the
    descriptor holds no state — every read goes straight through
    to :func:`get_active_runtime` and every write goes to
    :func:`get_registry`.set / :func:`get_registry`.clear.
    """
    from nullrun._registry import get_active_runtime, get_registry

    def _fget(_mod):
        return get_active_runtime()

    def _fset(_mod, value):
        if value is None:
            get_registry().clear()
        else:
            get_registry().set(value)

    setattr(module, attribute_name, property(_fget, _fset, doc="Registry proxy."))


__all__.append("install_module_proxy")



class _RuntimeProxyModule(type(sys.modules[__name__])):  # type: ignore[misc]
    """Subclass the module's metaclass to install a real descriptor
    on _runtime.

    PEP 562 (__getattr__ / __setattr__ defined in a module)
    has a quirk: the __setattr__ override is consulted ONLY
    for attribute assignments on the module instance, not for
    attribute writes inside the module body or by setattr.
    Concretely, runtime._runtime = None (after a fixture reset)
    creates a regular entry in runtime.__dict__ and shadows
    the __getattr__ proxy forever (the proxy only fires when
    the attribute is missing).

    The fix is the standard PEP 562 advanced trick: subclass the
    module's metaclass and define the descriptor on the subclass.
    Module attribute access then goes through the subclass
    metaclass (via type.__getattribute__), which finds the
    descriptor and invokes __get__ / __set__. We swap the
    module's class to the subclass in install_runtime_proxy
    below.

    Implementation note: the parent class is
    type(sys.modules[__name__]) so we subclass the actual
    metaclass of whatever module the helper is installed on,
    rather than hardcoding types.ModuleType. This avoids
    breaking subclasses that replace sys.modules entry
    classes (rare in practice but possible when test fixtures
    mock modules).
    """

    if "_runtime" not in dir():
        # Placeholder so mypy is happy about the descriptor
        # attribute declaration; the real descriptor below is
        # installed by install_runtime_proxy.
        pass

    @property
    def _runtime(self):
        from nullrun._registry import get_active_runtime

        return get_active_runtime()

    @_runtime.setter
    def _runtime(self, value):
        from nullrun._registry import get_registry

        if value is None:
            get_registry().clear()
        else:
            get_registry().set(value)


def install_runtime_proxy(module_name: str = "nullrun.runtime") -> None:
    # No-op when the module is not loaded (e.g. during isolated
    # test fixtures that mount nullrun._singleton without
    # importing runtime.py).
    """Replace the module's metaclass with the proxy variant above.

    Call this once per module that needs the _runtime proxy
    (currently nullrun.runtime and nullrun.decorators).
    The module's __class__ attribute is rebound to the
    subclass; subsequent module._runtime = X writes go
    through the descriptor on the subclass and update the
    registry.
    """
    import sys

    target = sys.modules.get(module_name)
    if target is None:
        return
    target.__class__ = _RuntimeProxyModule
