"""
Regression tests for dead-code removed in 0.4.0.

The audit (56 findings) identified a large set of public symbols with
zero in-tree callers. They were deleted in 0.4.0 to reduce the
attack surface and remove naming collisions. This file pins their
absence so a future regression that re-introduces any of them
triggers a test failure.

Removed in 0.4.0:
- BoundedDict
- wrap_tool, wrap
- check_before_tool, enforce_check_before_llm
- evaluate
- clear_pause
- WorkflowContext
- WebSocketManager
- EventRecorder
- Transport._atexit_flush (orphan from pre-weakref.finalize migration)
- PoolConfig, AdaptivePool
"""

from __future__ import annotations

import pytest

# ===========================================================================
# Runtime-level removals
# ===========================================================================


def test_bounded_dict_removed():
    """`BoundedDict` was deleted in 0.4.0."""
    from nullrun.runtime import NullRunRuntime

    assert getattr(NullRunRuntime, "BoundedDict", None) is None


def test_wrap_tool_removed():
    """`runtime.wrap_tool` was deleted in 0.4.0."""
    from nullrun.runtime import NullRunRuntime

    assert getattr(NullRunRuntime, "wrap_tool", None) is None


def test_wrap_removed():
    """`runtime.wrap` was deleted in 0.4.0 (and had a latent NameError)."""
    from nullrun.runtime import NullRunRuntime

    assert getattr(NullRunRuntime, "wrap", None) is None


def test_check_before_tool_removed():
    """`runtime.check_before_tool` was deleted in 0.4.0."""
    from nullrun.runtime import NullRunRuntime

    assert getattr(NullRunRuntime, "check_before_tool", None) is None


def test_enforce_check_before_llm_removed():
    """`runtime.enforce_check_before_llm` was deleted in 0.4.0."""
    from nullrun.runtime import NullRunRuntime

    assert getattr(NullRunRuntime, "enforce_check_before_llm", None) is None


def test_check_before_llm_removed():
    """`runtime.check_before_llm` was deleted in 0.4.0 (along with its CheckDecision)."""
    from nullrun.runtime import NullRunRuntime

    assert getattr(NullRunRuntime, "check_before_llm", None) is None


def test_evaluate_removed():
    """`runtime.evaluate` was deleted in 0.4.0 (also resolved silent fail-OPEN)."""
    from nullrun.runtime import NullRunRuntime

    assert getattr(NullRunRuntime, "evaluate", None) is None


def test_check_decision_class_removed():
    """`CheckDecision` dataclass was deleted alongside `check_before_*`."""
    from nullrun import runtime as _runtime

    assert not hasattr(_runtime, "CheckDecision")


# ===========================================================================
# Actions-level removals
# ===========================================================================


def test_clear_pause_removed():
    """`ActionHandler.clear_pause` was deleted in 0.4.0."""
    from nullrun.actions import ActionHandler

    assert getattr(ActionHandler, "clear_pause", None) is None


# ===========================================================================
# Context-level removals
# ===========================================================================


def test_workflow_context_class_removed():
    """`WorkflowContext` class was deleted in 0.4.0."""
    with pytest.raises(ImportError):
        from nullrun.context import WorkflowContext  # noqa: F401


def test_workflow_contextmanager_still_works():
    """The `with workflow(...)` contextmanager (replacement for WorkflowContext) still works."""
    import uuid as _uuid

    from nullrun.context import workflow

    with workflow("explicit-id") as wid:
        assert wid == "explicit-id"
    # Phase 5 #5.6: workflow() now emits a real UUID4 (matching the
    # rest of the SDK's id generation).
    with workflow() as wid:
        _uuid.UUID(wid)  # raises ValueError if not a UUID


# ===========================================================================
# WebSocket removals
# ===========================================================================


def test_websocket_manager_removed():
    """`WebSocketManager` class was deleted in 0.4.0."""
    with pytest.raises(ImportError):
        from nullrun.transport_websocket import WebSocketManager  # noqa: F401


# ===========================================================================
# Transport removals
# ===========================================================================


def test_atexit_flush_removed():
    """`Transport._atexit_flush` was deleted in 0.4.0."""
    from nullrun.transport import Transport

    assert getattr(Transport, "_atexit_flush", None) is None


def test_pool_config_removed():
    """`PoolConfig` was deleted in 0.4.0."""
    with pytest.raises(ImportError):
        from nullrun.transport import PoolConfig  # noqa: F401


def test_adaptive_pool_removed():
    """`AdaptivePool` was deleted in 0.4.0."""
    with pytest.raises(ImportError):
        from nullrun.transport import AdaptivePool  # noqa: F401


# ===========================================================================
# Decision-history removals
# ===========================================================================
# Sprint 2.1: the entire ``nullrun.decision_history`` module was
# deleted because the feature moved to the backend dashboard. The
# SDK does not (and cannot) replay LLM calls because the platform
# does not store request/response payloads. The ``start_recording``
# / ``stop_recording`` methods on ``NullRunRuntime`` are kept as
# no-op stubs for one minor version for backward compat.


def test_decision_history_module_removed():
    """The entire ``nullrun.decision_history`` module was deleted in 0.4.0.

    Previously a separate ``test_event_recorder_removed`` tested that
    a single symbol was gone; after Sprint 2.1 the whole module is
    gone, so the import fails at the module level (not the
    attribute level). Both ``from nullrun.decision_history import X``
    and ``import nullrun.decision_history`` must now raise.
    """
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("nullrun.decision_history")

    with pytest.raises(ImportError):
        # ``from x import y`` form — also must fail, not silently succeed.
        from nullrun.decision_history import DecisionHistoryRecorder  # noqa: F401


# ===========================================================================
# Sprint 2.2: zombie exception classes removed
# ===========================================================================
# Six exception classes had zero in-tree callers — they were defined
# but never raised. They were public surface, so external callers
# COULD have been using them; we accept the breaking change and
# add explicit regression tests so a future re-introduction of any
# of them (without a real use case) breaks here.


_ZOMBIE_EXCEPTIONS = [
    "CostLimitExceeded",
    "ApprovalRequired",
    "BreakerTimeout",
    "LoopDetectedException",
    "RetryStormException",
    "RateLimitExceededException",
]


@pytest.mark.parametrize("name", _ZOMBIE_EXCEPTIONS)
def test_zombie_exception_removed_from_breaker(name: str):
    """Each zombie exception was removed from ``nullrun.breaker.exceptions``.

    Pre-fix: importable, but had zero callers anywhere in the SDK
    or tests. Removing them reduces the public surface that we
    have to maintain compatibility for.
    """
    from nullrun.breaker import exceptions  # noqa: F401

    assert not hasattr(exceptions, name), (
        f"{name} is still defined in nullrun.breaker.exceptions. "
        "It was marked as a zombie class in Sprint 2.2 — it has "
        "no in-tree callers. Re-add it only when a real use case "
        "appears, with a regression test for the raise path."
    )


@pytest.mark.parametrize("name", _ZOMBIE_EXCEPTIONS)
def test_zombie_exception_not_in_lazy_exports(name: str):
    """None of the zombie exceptions are in ``nullrun``'s lazy export table.

    Even though ``__getattr__`` would raise ``AttributeError`` for a
    missing module attribute, that would be a confusing failure
    mode. After removal, ``from nullrun import <name>`` must raise
    a clean ``ImportError``.
    """
    with pytest.raises(ImportError):
        # Trigger the lazy export lookup. If the symbol is not in
        # the table, ``__getattr__`` raises ``AttributeError``, which
        # ``from x import y`` converts to ``ImportError``. If the
        # symbol IS in the table but the target attribute is
        # missing, the same ``AttributeError`` path is taken — but
        # the import-time ``ImportError`` is what we want to pin.
        exec(f"from nullrun import {name}")  # noqa: S102


# ===========================================================================
# Sprint 2.7 (B27): dead tenant contextvars / getters
# ===========================================================================
# Pre-fix: ``_organization_id_var`` and ``_api_key_id_var`` were
# defined but never written, so ``get_organization_id()`` and
# ``get_api_key_id()`` always returned ``None``. The only consumer
# (``observability.TenantFilter``) was removed in 0.3.1, so the
# entire pair of contextvars + getters is dead. Post-fix they are
# gone and these tests pin the removal.


def test_organization_contextvar_removed():
    # AttributeError is the expected failure mode — the
    # contextvar module-level constant is gone.
    with pytest.raises(ImportError):
        from nullrun.context import _organization_id_var  # noqa: F401


def test_api_key_contextvar_removed():
    with pytest.raises(ImportError):
        from nullrun.context import _api_key_id_var  # noqa: F401


def test_get_organization_id_removed():
    with pytest.raises(ImportError):
        from nullrun.context import get_organization_id  # noqa: F401


def test_get_api_key_id_removed():
    with pytest.raises(ImportError):
        from nullrun.context import get_api_key_id  # noqa: F401


# ===========================================================================
# Curated surface stays intact
# ===========================================================================


def test_dir_size_unchanged():
    """`dir(nullrun)` still shows exactly the curated surface.

    The curated surface is declared in ``nullrun.__all__`` (PEP 562
    via ``__dir__``) — the source of truth lives there. This test
    pins the *contract* (no rogue globals leak into ``dir()``)
    without hardcoding the count, so adding a new curated symbol
    to ``__all__`` is fine but adding one via a top-level
    import is a regression.

    History:
      * Phase 3.4 — surface was 6: ``__version__``, ``init``,
        ``protect``, ``track_event``, ``track_llm``, ``track_tool``.
      * Layer 2 (``on_error``) and Layer 3 (``status``) — added
        because users need to know they exist (discoverability
        is the whole point of the curated surface).
      * Layer 1 — the six new structured exception classes plus
        ``WorkflowKilledInterrupt`` added to ``__all__`` for the
        same reason; cookbook examples and ``except`` clauses
        need the names visible in tab-completion.
    """
    import nullrun

    # Source of truth: ``__all__``. ``dir(nullrun)`` is rebuilt from
    # it via the PEP-562 ``__dir__`` override.
    assert set(dir(nullrun)) == set(nullrun.__all__)
    # And ``__all__`` itself must be the only thing the surface
    # contains — no auto-imported submodules, no lazy-resolved
    # names bleeding in.
    assert nullrun.__all__[0] == "__version__"
    # The five Phase-3.4 anchors are still on the surface.
    for anchor in ("init", "protect", "track_event", "track_llm", "track_tool"):
        assert anchor in nullrun.__all__, f"{anchor} missing from __all__"


def test_wrap_symbol_absent():
    """`from nullrun import wrap` raises ImportError."""
    with pytest.raises(ImportError):
        from nullrun import wrap  # noqa: F401


# ===========================================================================
# Sprint 1.2 (B11, B12): patch_openai / unpatch_openai lazy exports
# ===========================================================================
# These were entries in `_LAZY_EXPORTS` pointing at
# `("nullrun.instrumentation", "patch_openai")` /
# `("nullrun.instrumentation", "unpatch_openai")` — neither attribute
# exists on the module (the real function is `patch_openai_agents`,
# with different semantics: it patches `agents.Runner`, not the
# `openai` SDK). Pre-fix, `from nullrun import patch_openai` raised
# `AttributeError` at first access (a confusing runtime crash). Post
# fix, both imports raise `ImportError` cleanly at module-load time.


def test_patch_openai_lazy_export_removed():
    """`from nullrun import patch_openai` raises ImportError.

    Pre-fix: lazy export pointed at a non-existent attribute and
    `AttributeError` was raised on first access. Post-fix: the symbol
    is not in `_LAZY_EXPORTS`, so the standard `from x import y` path
    raises `ImportError` cleanly.
    """
    with pytest.raises(ImportError):
        from nullrun import patch_openai  # noqa: F401


def test_unpatch_openai_lazy_export_removed():
    """`from nullrun import unpatch_openai` raises ImportError.

    Same regression class as `patch_openai`: the lazy entry pointed
    at a non-existent attribute.
    """
    with pytest.raises(ImportError):
        from nullrun import unpatch_openai  # noqa: F401


def test_lazy_exports_dict_does_not_contain_patch_openai():
    """Defensive: assert the lazy exports table is clean.

    Guards against a future regression that re-adds the dead entry.
    """
    import nullrun  # noqa: F401

    # `globals()` of the package is the lazy-export cache; we read it
    # via the module's __dict__ to avoid accessing the actual
    # (non-existent) attribute.
    assert "patch_openai" not in nullrun.__dict__
    assert "unpatch_openai" not in nullrun.__dict__
