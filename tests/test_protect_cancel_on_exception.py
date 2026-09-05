"""
Regression tests for the SDK exception-path cancel fix.

Background — what the fix does:

  Before this fix, `@protect` let ANY exception (control_plane kill,
  workflow_budget block, sensitive-tool reject, fn() raise) propagate
  out of the with-block without closing the budget reservation that
  `check_workflow_budget` opened via /gate. Each exception leaked a
  Redis envelope to TTL expiry (an "orphan" from the server's
  perspective). At anti-DoS scale this caused reserved_total to drift
  up monotonically.

  The fix wraps the with-block in a try/except in BOTH async and sync
  wrappers; on failure, cancel_execution(execution_id) runs to close
  the reservation. A `fn_completed` sentinel prevents cancel from
  firing after track_tool failure — that path means side effects
  already happened and only retry/consume semantics apply.

Critical asymmetry (the one this file exists to lock in):

  - `async_wrapper` catches `Exception`, NOT `BaseException`. This is
    so `asyncio.CancelledError` / `KeyboardInterrupt` / `SystemExit`
    propagate without doing a synchronous blocking HTTP call in a
    cancellation handler — that would delay shutdown by up to 5s and
    trigger "Task was destroyed but pending" warnings. Test #1 is
    THE regression guard for this.

  - `sync_wrapper` catches `BaseException`. Sync code has no event
    loop to delay; matching existing `_protect_body` unify_block
    semantics keeps behavior consistent.

These tests pin both halves. Any future refactor that reverts the
async wrapper to `except BaseException` — e.g., "be safe, catch
everything" — would silently slow down agent cancellation and break
test #1.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import nullrun.decorators as _dec
from nullrun.breaker.exceptions import (
    NullRunTransportError,
    TransportErrorSource,
    WorkflowKilledInterrupt,
)
from nullrun.context import (
    _server_minted_execution_id_var,
    set_server_minted_execution_id,
)
from nullrun.decorators import protect

# ─────────────────────────────────────────────────────────────────────
# RecordingRuntime — no network, no real transport. Records the order
# of gate calls and the cancel_execution invocations so the tests
# can assert exactly what happened on each failure-path.
#
# Pattern is borrowed from tests/test_preflight_fail_policy.py
# (_RecordingRuntime, lines 48-117). We extend it with:
#   - stub `cancel_execution` that captures calls in `cancel_calls`
#   - `capture_execution_id` config: when set and gates pass, simulates
#     check_workflow_budget's real behavior of calling
#     `set_server_minted_execution_id(...)` so the cancel helper
#     sees a populated ContextVar.
# ─────────────────────────────────────────────────────────────────────


class _RecordingRuntime:
    def __init__(
        self,
        *,
        control_plane_raises: BaseException | None = None,
        workflow_budget_raises: BaseException | None = None,
        sensitive_tool_raises: BaseException | None = None,
        track_tool_raises: BaseException | None = None,
        capture_execution_id: str | None = "exec-test-123",
    ) -> None:
        self.gate_calls: list[str] = []
        self.cancel_calls: list[tuple[str, str | None]] = []
        self._remote_states: dict = {}
        self._sensitive_tools: set = set()
        self._control_plane_raises = control_plane_raises
        self._workflow_budget_raises = workflow_budget_raises
        self._sensitive_tool_raises = sensitive_tool_raises
        self._track_tool_raises = track_tool_raises
        self._capture_execution_id = capture_execution_id

    # ---- gate stubs (the four pre-execution checks) ----

    def check_control_plane(self, workflow_id: Any) -> None:
        """Mirror the real signature; raise `control_plane_raises` if set."""
        self.gate_calls.append("control_plane")
        if self._control_plane_raises is not None:
            raise self._control_plane_raises

    def check_workflow_budget(self) -> None:
        """Simulate the real /gate success path: capture the
        server-minted execution_id via the actual
        `set_server_minted_execution_id(...)` primitive so the cancel
        helper sees a populated ContextVar after this returns."""
        self.gate_calls.append("budget")
        if self._capture_execution_id is not None:
            set_server_minted_execution_id(self._capture_execution_id)
        if self._workflow_budget_raises is not None:
            raise self._workflow_budget_raises

    def is_sensitive_tool(self, tool_name: str) -> bool:
        # No sensitive tools by default in these tests; sensitive-tool
        # reject coverage is a separate concern (already exercised in
        # test_preflight_fail_policy.py).
        return False

    def _enforce_sensitive_tool_in_decorators(self, runtime, fn, args, kwargs):
        # Stub doesn't replicate the decorator's _enforce_sensitive_tool
        # body; we'll come back to sensitive-tool reject in a future
        # test if the orphan source data shows it matters.
        return None

    # ---- track / cancel stubs ----

    def track_tool(self, tool_name: str, metadata=None, **kwargs):
        self.gate_calls.append("track_tool")
        if self._track_tool_raises is not None:
            raise self._track_tool_raises
        return {"ok": True}

    def cancel_execution(self, execution_id: str, reason: str | None = None) -> dict:
        self.cancel_calls.append((execution_id, reason))
        return {"status": "cancelled"}


# ─────────────────────────────────────────────────────────────────────
# Fixtures — clean ContextVar between tests, build & pin a runtime.
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_execution_id():
    """server_minted_execution_id is a ContextVar — restore the prior
    value after each test. Without this, capture from one test leaks
    into the next and the order-dependent assertions become flaky."""
    prior = _server_minted_execution_id_var.get()
    set_server_minted_execution_id(None)
    yield
    set_server_minted_execution_id(prior)


@pytest.fixture
def runtime_factory():
    """Build a _RecordingRuntime and pin it to the @protect decorator's
    module-level slot (`_dec._runtime`) the same way
    `tests/conftest.py::make_runtime` does for real NullRunRuntimes."""
    created: list[_RecordingRuntime] = []

    def _make(**kwargs) -> _RecordingRuntime:
        rt = _RecordingRuntime(**kwargs)
        created.append(rt)
        _dec._runtime = rt
        return rt

    yield _make
    _dec._runtime = None  # cleanup; tests should pin fresh each time


# ─────────────────────────────────────────────────────────────────────
# PRIORITY 1 — the two regression guards the spec explicitly named.
# These MUST run first when iterating on the fix; failure here means
# we silently lost the invariant.
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_1_async_cancelled_error_does_not_trigger_cancel(runtime_factory):
    """The single most important regression test.

    If a future refactor reverts `except Exception` to `except
    BaseException` in `async_wrapper`, this test fails. The cost of
    that regression is silent: cancel_execution is a synchronous
    blocking HTTP call (5s timeout). Inside a cancellation handler
    it would:

      1. Delay task cancellation by up to 5s on network errors
         (timeout / shutdown / Ctrl+C).
      2. Make pending-task warnings ("Task was destroyed but it is
         pending") more frequent and harder to diagnose.
      3. In some shutdown paths, the cancel I/O itself gets
         cancelled, leaving orphan cleanup incomplete — but the
         server is idempotent on /cancel so this is acceptable
         (orphan via TTL/reconciliation instead).

    Cancel MUST NOT fire on CancelledError. Execution_id is set in
    ContextVar (to prove the helper WOULD have run if the except
    had caught BaseException), and the assertion is strict:
    cancel_calls must be empty.
    """
    rt = runtime_factory()

    # Capture execution_id as if /gate succeeded. The cancel helper
    # would see this if the except caught BaseException.
    set_server_minted_execution_id("exec-cancellation-test")

    @protect
    async def fn():
        raise asyncio.CancelledError("task got cancelled")

    with pytest.raises(asyncio.CancelledError):
        await fn()

    assert rt.cancel_calls == [], (
        f"cancel_execution was called on CancelledError: {rt.cancel_calls}. "
        "This is the asyncio cancellation-safety regression."
    )


@pytest.mark.asyncio
async def test_2_track_tool_failure_after_fn_completion_does_not_trigger_cancel(
    runtime_factory,
):
    """The second regression guard. fn_completed sentinel must stay True
    past the successful fn() call.

    If someone removes the `fn_completed` sentinel — "simplify the
    wrapper, just always call cancel on Exception" — this test
    fails. The cost of THAT regression is a different kind of bad:
    cancel_execution tells the server "abort this execution, no
    side effects happened". But fn() already ran. Side effects
    already happened (LLM call emitted a response, tool ran,
    possibly mutated state). The reservation got DECRBY'd because
    track_tool would have consumed it on success; cancelling
    instead releases the budget slot and tells the server the
    side effect didn't happen — which is a lie that breaks audit
    and produces a phantom "refund".

    Track_tool failure is rare (network error to /track batch
    sender). On that path we want to RETRY track_tool, not cancel.
    The orphan-if-no-retry is a server-side concern; SDK can't
    usefully address it from outside.
    """
    rt = runtime_factory(
        track_tool_raises=NullRunTransportError(
            "network down for /track",
            source=TransportErrorSource.NETWORK_ERROR,
            endpoint="/api/v1/track",
        ),
    )

    @protect
    async def fn():
        # fn succeeds, side effects happen here in real code
        return "tool-output-data"

    with pytest.raises(NullRunTransportError):
        await fn()

    assert rt.cancel_calls == [], (
        f"cancel_execution was called after successful fn+broken track_tool: "
        f"{rt.cancel_calls}. fn_completed sentinel regressed; cancelling an "
        "actually-completed tool would mislead budget accounting."
    )


# ─────────────────────────────────────────────────────────────────────
# PRIORITY 2 — mechanical coverage of the value-side assertions.
# Less interesting than 1+2 because they're not regression guards
# against over-broad exception handling. They're 'happy path'
# coverage of the cancel logic itself.
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_3_async_fn_raises_value_error_triggers_cancel(runtime_factory):
    """fn() raises a business exception. The wrapper's except Exception
    catches; fn_completed is False (the raise happened before
    fn_completed could be set); the helper sees execution_id in the
    ContextVar (captured by check_workflow_budget) and calls
    cancel_execution."""
    rt = runtime_factory()

    @protect
    async def fn():
        raise ValueError("llm returned malformed JSON")

    with pytest.raises(ValueError, match="malformed JSON"):
        await fn()

    assert rt.cancel_calls == [
        ("exec-test-123", "tool_exception"),
    ], f"expected exactly one cancel call with captured exec id; got {rt.cancel_calls}"

    # Sanity: track_tool is NOT called when fn() raises — the body
    # never reached it.
    assert "track_tool" not in rt.gate_calls


@pytest.mark.asyncio
async def test_4_async_no_execution_id_skips_cancel(runtime_factory):
    """control_plane rejects BEFORE /gate is called. The ContextVar
    stays None; the helper is a no-op even though the wrapper's
    except ran. The orphan, if any (server-side, depends on whether
    /gate ran), is handled by TTL/reconciliation, not the SDK."""
    rt = runtime_factory(
        control_plane_raises=WorkflowKilledInterrupt(
            workflow_id="default",
            reason="user clicked kill",
        ),
        capture_execution_id=None,  # capture_execution_id=None means: don't capture
    )

    @protect
    async def fn():
        return "should not run"

    with pytest.raises(WorkflowKilledInterrupt):
        await fn()

    # control_plane ran (and raised); budget did NOT run (short-circuited).
    assert rt.gate_calls == ["control_plane"]
    # No exec_id → no cancel. Server-side orphan, if any, is reconciliation territory.
    assert rt.cancel_calls == [], (
        "control_plane reject happens pre-/gate. No reservation was created "
        "from the SDK's perspective. SDK must not call cancel."
    )


def test_5_sync_fn_raises_value_error_triggers_cancel(runtime_factory):
    """Sync wrapper mirrors async except for the BaseException vs
    Exception asymmetry. ValueError (a regular Exception) is caught
    on both paths; cancel runs with the captured execution_id."""
    rt = runtime_factory()

    @protect
    def fn():
        raise ValueError("bad input")

    with pytest.raises(ValueError):
        fn()

    assert rt.cancel_calls == [
        ("exec-test-123", "tool_exception"),
    ]


def test_6_sync_baseexception_also_triggers_cancel(runtime_factory):
    """Sync path catches BaseException. KeyboardInterrupt (which
    async_wrapper deliberately lets propagate to keep cancellation
    fast) is caught here and triggers cancel — sync code has no
    event loop to delay, and a Ctrl+C during a long sync agent
    gets a few seconds of cancel I/O before exit. Matches existing
    `_protect_body` unify_block semantics."""
    rt = runtime_factory()

    @protect
    def fn():
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        fn()

    assert rt.cancel_calls == [
        ("exec-test-123", "tool_exception"),
    ]


# ─────────────────────────────────────────────────────────────────────
# Sanity check — the happy path still works. Re-pinning this here
# because the cancellation fix could in principle break the
# success path (the new try/except adds a frame and a closure
# capturing fn_completed).
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_no_cancel_called(runtime_factory):
    """fn() succeeds, track_tool succeeds, control returns normally.
    NO cancel call (we're not in except path). Sanity check that
    the wrapper's added try/except didn't break success."""
    rt = runtime_factory()

    @protect
    async def fn():
        return "ok"

    result = await fn()

    assert result == "ok"
    assert rt.cancel_calls == []
    assert rt.gate_calls == ["control_plane", "budget", "track_tool"]
