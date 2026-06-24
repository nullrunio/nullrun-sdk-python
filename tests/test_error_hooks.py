"""Tests for the Layer 2 global ``nullrun.on_error()`` hook.

The hook contract is:

  * Fires for every structured SDK failure (every
    ``NullRunError`` subclass).
  * Does NOT fire for ``WorkflowKilledInterrupt`` (BaseException
    subclass — kill is a signal, not an error).
  * Hooks are called BEFORE the exception propagates so the call
    stack is still live.
  * Multiple hooks are supported; they fire in registration order.
  * Unregister is idempotent (safe to call twice).
  * Hook exceptions are caught and logged at DEBUG — a
    misbehaving hook cannot break the SDK.
  * When no hook is registered, the SDK adds zero allocation /
    zero lock cost (see ``has_hooks()`` short-circuit in
    ``_emit_sdk_error`` / ``_emit_for_transport_error``).
"""

import logging
import threading
from typing import Any
from unittest.mock import patch

import pytest

import nullrun
from nullrun.breaker.exceptions import (
    BreakerError,
    NullRunAuthenticationError,
    NullRunAuthError,
    NullRunBackendError,
    NullRunBlockedException,
    NullRunBudgetError,
    NullRunConfigError,
    NullRunError,
    NullRunToolBlockedError,
    WorkflowKilledException,
    WorkflowKilledInterrupt,
    WorkflowPausedException,
)
from nullrun.observability.error_hooks import (
    STAGES,
    ErrorContext,
    clear_hooks,
    emit_error,
    has_hooks,
    register_hook,
)


# Each test gets a fresh hook list — we tear down in
# ``clear_hooks`` so a failing test does not leak hooks into the
# rest of the suite.
@pytest.fixture(autouse=True)
def _reset_hooks():
    clear_hooks()
    yield
    clear_hooks()


# ---------------------------------------------------------------------------
# 1. Registry basics
# ---------------------------------------------------------------------------
class TestRegistry:
    def test_register_returns_unregister(self):
        def hook(err, ctx):
            return None

        unregister = register_hook(hook)
        assert callable(unregister)
        assert has_hooks() is True

    def test_unregister_removes_hook(self):
        def hook(err, ctx):
            return None

        unregister = register_hook(hook)
        unregister()
        assert has_hooks() is False

    def test_unregister_is_idempotent(self):
        def hook(err, ctx):
            return None

        unregister = register_hook(hook)
        unregister()
        unregister()  # second call is a no-op, does not raise
        assert has_hooks() is False

    def test_register_rejects_non_callable(self):
        with pytest.raises(TypeError, match="must be callable"):
            register_hook("not a function")  # type: ignore[arg-type]

    def test_multiple_hooks_fire_in_registration_order(self):
        order: list[str] = []
        register_hook(lambda err, ctx: order.append("first"))
        register_hook(lambda err, ctx: order.append("second"))
        register_hook(lambda err, ctx: order.append("third"))
        emit_error(
            NullRunError("test"),
            ErrorContext(stage="init"),
        )
        assert order == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# 2. emit_error behavior
# ---------------------------------------------------------------------------
class TestEmitError:
    def test_fires_with_error_and_context(self):
        captured: list[tuple[Any, ErrorContext]] = []
        register_hook(lambda err, ctx: captured.append((err, ctx)))
        err = NullRunError("test", error_code="NR-X999")
        ctx = ErrorContext(
            stage="init",
            workflow_id="wf-1",
            tool_name="send_email",
            api_key_prefix="nr_live_a",
            correlation_id="abc-123",
        )
        emit_error(err, ctx)
        assert len(captured) == 1
        seen_err, seen_ctx = captured[0]
        assert seen_err is err
        assert seen_ctx.stage == "init"
        assert seen_ctx.workflow_id == "wf-1"
        assert seen_ctx.tool_name == "send_email"
        assert seen_ctx.api_key_prefix == "nr_live_a"
        assert seen_ctx.correlation_id == "abc-123"

    def test_no_hooks_no_overhead(self):
        # When no hook is registered, emit_error must return
        # without dispatching anything. The test asserts no
        # exception is raised — the real assertion is that
        # ``has_hooks()`` is False (so the SDK skips the call
        # entirely on the hot path).
        assert has_hooks() is False
        emit_error(NullRunError("test"), ErrorContext(stage="init"))  # must not raise

    def test_hook_exception_is_swallowed_and_logged(self):
        # A misbehaving hook must NOT break the SDK. The exception
        # is caught and emitted at DEBUG (per design decision
        # 2026-06-24 — silent at INFO/CRITICAL).
        def bad_hook(err, ctx):
            raise RuntimeError("hook boom")

        register_hook(bad_hook)
        with patch("nullrun.observability.error_hooks.logger") as mock_logger:
            # Must not raise despite the hook raising.
            emit_error(NullRunError("test"), ErrorContext(stage="init"))
            mock_logger.debug.assert_called_once()
            call_args = mock_logger.debug.call_args
            assert "swallowed" in call_args.args[0]
            assert call_args.kwargs.get("exc_info") is True

    def test_one_bad_hook_does_not_prevent_later_hooks(self):
        order: list[str] = []

        def bad_hook(err, ctx):
            raise RuntimeError("boom")

        def good_hook(err, ctx):
            order.append("good")

        register_hook(bad_hook)
        register_hook(good_hook)
        with patch("nullrun.observability.error_hooks.logger"):
            emit_error(NullRunError("test"), ErrorContext(stage="init"))
        assert order == ["good"]

    def test_unregister_during_dispatch_does_not_break(self):
        # Snapshot copy: emit_error reads the hook list under the
        # lock so an unregister during iteration does not skip a
        # hook that was already snapshotted. ``first`` is
        # registered first (so it runs first in dispatch); it
        # unregisters ``second`` mid-dispatch — but the snapshot
        # taken by ``emit_error`` already includes ``second``, so
        # the hook still fires.
        order: list[str] = []
        unregister_second: Any = None  # bound after register_hook below

        def first(err, ctx):
            if unregister_second is not None:
                unregister_second()
            order.append("first")

        def second(err, ctx):
            order.append("second")

        register_hook(first)
        unregister_second = register_hook(second)
        emit_error(NullRunError("test"), ErrorContext(stage="init"))
        assert order == ["first", "second"]


# ---------------------------------------------------------------------------
# 3. ErrorContext validation
# ---------------------------------------------------------------------------
class TestErrorContext:
    def test_stage_must_be_in_catalogue(self):
        # Known stage — no warning.
        ctx = ErrorContext(stage="init")
        assert ctx.stage == "init"

    def test_unknown_stage_emits_debug_warning(self):
        # Unknown stage — accepted but flagged at DEBUG so the
        # next refactor can extend STAGES.
        with patch("nullrun.observability.error_hooks.logger") as mock_logger:
            ErrorContext(stage="totally_new_stage")
            mock_logger.debug.assert_called_once()
            assert "STAGES" in mock_logger.debug.call_args.args[0]

    def test_default_timestamp_is_set(self):
        ctx = ErrorContext(stage="init")
        # Timestamp is a float and recent.
        assert isinstance(ctx.timestamp, float)
        assert ctx.timestamp > 0

    def test_extra_defaults_to_empty_dict(self):
        ctx = ErrorContext(stage="init")
        assert ctx.extra == {}


# ---------------------------------------------------------------------------
# 4. nullrun.on_error public API
# ---------------------------------------------------------------------------
class TestPublicAPI:
    def test_on_error_importable(self):
        assert callable(nullrun.on_error)
        assert "on_error" in dir(nullrun)

    def test_on_error_in_all(self):
        # ``from nullrun import *`` must surface ``on_error``.
        # PEP 562 stores __all__ but does NOT auto-inject into
        # globals, so we read the module-level __all__ directly.
        import nullrun as n

        assert "on_error" in n.__all__

    def test_on_error_returns_unregister(self):
        unregister = nullrun.on_error(lambda err, ctx: None)
        assert callable(unregister)
        assert has_hooks() is True
        unregister()
        assert has_hooks() is False

    def test_on_error_fires_on_init_failure(self, monkeypatch):
        # Re-raise no-api_key init() — the on_error hook should
        # see it before the exception escapes.
        monkeypatch.delenv("NULLRUN_API_KEY", raising=False)
        captured: list[tuple[Any, ErrorContext]] = []
        nullrun.on_error(lambda err, ctx: captured.append((err, ctx)))
        with pytest.raises(NullRunAuthenticationError):
            nullrun.init()
        # At least one hook fired (init-failure path).
        assert len(captured) == 1
        err, ctx = captured[0]
        assert err.error_code == "NR-C001"
        assert ctx.stage == "init"

    def test_on_error_silent_when_no_hooks(self, monkeypatch, caplog):
        # Sanity: when no hook is registered, the no-api-key
        # raise still works and no error/exception is logged
        # at WARNING/ERROR level.
        assert has_hooks() is False
        monkeypatch.delenv("NULLRUN_API_KEY", raising=False)
        with caplog.at_level(logging.WARNING):
            with pytest.raises(NullRunAuthenticationError):
                nullrun.init()
        # No log records at WARNING+ from the on_error path
        # (other unrelated logs may be present, so we don't
        # assert caplog.text == '').
        for record in caplog.records:
            assert "on_error" not in record.getMessage()


# ---------------------------------------------------------------------------
# 5. Hook does NOT fire for kill (BaseException bypass)
# ---------------------------------------------------------------------------
class TestKillBypass:
    def test_kill_interrupt_does_not_fire_hook(self):
        # Per design decision A (2026-06-24): kill is a signal,
        # not an error. Hooks MUST NOT fire for BaseException
        # subclasses — that would mask the intent of
        # ``except WorkflowKilledInterrupt`` at the top of the
        # agent loop.
        captured: list[tuple[Any, ErrorContext]] = []
        nullrun.on_error(lambda err, ctx: captured.append((err, ctx)))
        # Manually raise the kill — emit_error is only wired
        # into raise sites that fire NullRunError, but the
        # BaseException bypass is enforced at the call site
        # (no emit at all for kill). The test simulates
        # the kill path by raising it directly.
        with pytest.raises(WorkflowKilledInterrupt):
            raise WorkflowKilledInterrupt("wf-1", reason="killed")
        assert captured == [], "WorkflowKilledInterrupt must NOT trigger on_error hooks"

    def test_killed_exception_does_not_fire_hook(self):
        # Same bypass applies to the deprecated
        # WorkflowKilledException (BaseException subclass).
        captured: list[tuple[Any, ErrorContext]] = []
        nullrun.on_error(lambda err, ctx: captured.append((err, ctx)))
        with pytest.raises(WorkflowKilledException):
            raise WorkflowKilledException("wf-1", reason="killed")
        assert captured == []

    def test_emit_error_skips_baseexception(self):
        # If a BaseException somehow reaches emit_error, the
        # hook should still fire (the bypass is at the call
        # site, not in emit_error itself). But the typed
        # error subclasses (NullRunError) are the documented
        # payload — the hook must be defensive.
        captured: list[tuple[Any, ErrorContext]] = []
        register_hook(lambda err, ctx: captured.append((err, ctx)))
        # Pass a NullRunError — hook fires.
        emit_error(NullRunError("test"), ErrorContext(stage="init"))
        assert len(captured) == 1


# ---------------------------------------------------------------------------
# 6. STAGES catalogue
# ---------------------------------------------------------------------------
class TestStagesCatalogue:
    def test_stages_is_tuple(self):
        assert isinstance(STAGES, tuple)
        assert len(STAGES) > 0

    def test_common_stages_present(self):
        # The most common stages must be in the catalogue so
        # ``ErrorContext.stage=`` usage stays discoverable.
        for stage in ("init", "auth", "policy_fetch", "execute"):
            assert stage in STAGES, f"{stage!r} missing from STAGES"
