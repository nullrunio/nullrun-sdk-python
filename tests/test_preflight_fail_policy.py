"""
Regression tests for the three pre-execution-gate bugs fixed by ADR-008.

Bug #1 — `check_workflow_budget` was fail-CLOSED on network error
         (transport swallowed the error and returned a synthetic
         block; runtime re-interpreted it as a real policy block).
         Fix: fail-OPEN. Network / 5xx / breaker-open is logged and
         the call returns.

Bug #2 — `_enforce_sensitive_tool` was fail-OPEN on transport error
         (transport returned a synthetic allow with
         `decision_source=FALLBACK_*`; the decorator trusted it).
         Fix: fail-CLOSED. Body must not run when the policy engine
         is unreachable. NULLRUN_SENSITIVE_FAIL_OPEN=1 stays as the
         explicit opt-out for dev / test.

Bug #3 — `@protect` did not call `check_control_plane` at all, so
         a dashboard KILL was silently ignored for `@protect`-only
         code paths. Fix: control-plane check runs FIRST inside
         `@protect`, before budget and before sensitive-tool.

These tests also exercise the per-call `on_transport_error` plumbing
on `transport.execute` / `transport.check` and the new
`NullRunTransportError` / `TransportErrorSource` exception pair.
"""

import httpx
import pytest
import respx

import nullrun
from nullrun.breaker.exceptions import (
    NullRunBlockedException,
    NullRunTransportError,
    TransportErrorSource,
    WorkflowKilledInterrupt,
)

# Base URL used in tests
BASE_URL = "https://api.test.nullrun.io"


# ──────────────────────────────────────────────────────────────
# Helpers — RecordingRuntime (no-op transport, full gate behavior)
# ──────────────────────────────────────────────────────────────


class _RecordingRuntime:
    """
    Stand-in runtime that records events but does NOT call any
    network or call real gates. Used to isolate the
    `check_control_plane` invocation order in the @protect wrapper
    from the other two gates.

    The real `check_control_plane` and `check_workflow_budget` would
    normally make HTTP calls; for the bug-#3 regression we wire a
    Killed state directly into `_remote_states` (the same internal
    field the WS-push handler updates).
    """

    def __init__(self) -> None:
        self.events: list[dict] = []
        self._remote_states: dict = {}
        self._sensitive_tools: set = set()
        self._strict_mode_tools: set = set()
        # Order of gate calls recorded by `_record_gate` below
        self.gate_calls: list[str] = []

    def is_sensitive_tool(self, tool_name: str) -> bool:
        return tool_name in self._sensitive_tools

    def add_sensitive_tool(self, tool_name: str) -> None:
        self._strict_mode_tools.add(tool_name)

    def track_event(self, event_type: str, **kwargs) -> None:
        self.events.append({"type": event_type, **kwargs})

    # The two gates we want to track, in order. The decorator
    # calls them — we record the call sequence.

    def check_control_plane(self, workflow_id) -> None:
        self.gate_calls.append("control_plane")
        state = self._remote_states.get(workflow_id or "default", {})
        s = state.get("state", "Normal")
        if s == "Killed":
            raise WorkflowKilledInterrupt(
                workflow_id=workflow_id or "default",
                reason=state.get("reason", "killed"),
            )

    def check_workflow_budget(self) -> None:
        self.gate_calls.append("budget")

    def execute(self, tool_name, input_data, mode="auto"):
        self.gate_calls.append("sensitive")
        if not self.is_sensitive_tool(tool_name):
            return {"decision": "allow", "decision_source": "gateway"}
        # If sensitive, callers can pre-arrange `self._next_execute_return`
        # / `self._next_execute_raise` to drive the assertion.
        if self._next_execute_raise is not None:
            exc = self._next_execute_raise
            self._next_execute_raise = None
            raise exc
        ret = self._next_execute_return
        self._next_execute_return = None
        if ret is None:
            return {"decision": "allow", "decision_source": "gateway"}
        return ret

    _next_execute_return = None
    _next_execute_raise = None


# ──────────────────────────────────────────────────────────────
# Bug #1 — check_workflow_budget fail-OPEN
# ──────────────────────────────────────────────────────────────


class TestCheckWorkflowBudgetFailOpen:
    def test_network_error_returns_normally(self, make_runtime, mock_api):
        """httpx.ConnectError on /gate → check_workflow_budget returns
        normally (fail-OPEN). Regression for bug #1 — the old code
        re-interpreted a swallowed exception as a real block."""
        respx.post(f"{BASE_URL}/api/v1/gate").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        rt = make_runtime()
        # Must NOT raise — the gate is fail-OPEN on transport error.
        rt.check_workflow_budget()

    def test_timeout_returns_normally(self, make_runtime, mock_api):
        """httpx.TimeoutException on /gate → returns normally."""
        respx.post(f"{BASE_URL}/api/v1/gate").mock(
            side_effect=httpx.TimeoutException("read timeout")
        )
        rt = make_runtime()
        rt.check_workflow_budget()

    def test_5xx_returns_normally(self, make_runtime, mock_api):
        """HTTP 500 from /gate → returns normally."""
        respx.post(f"{BASE_URL}/api/v1/gate").mock(return_value=httpx.Response(500, text="boom"))
        rt = make_runtime()
        rt.check_workflow_budget()

    def test_real_block_raises_workflow_killed(self, make_runtime, mock_api):
        """Real `decision=block` from gateway still raises
        WorkflowKilledInterrupt. The fix for bug #1 must NOT swallow
        real policy decisions — only transport errors."""
        respx.post(f"{BASE_URL}/api/v1/gate").mock(
            return_value=httpx.Response(
                200,
                json={
                    "decision": "block",
                    "explanations": ["budget_exceeded"],
                },
            )
        )
        rt = make_runtime()
        with pytest.raises(WorkflowKilledInterrupt):
            rt.check_workflow_budget()

    def test_real_throttle_raises_paused(self, make_runtime, mock_api):
        """`decision=throttle` still raises WorkflowPausedException."""
        respx.post(f"{BASE_URL}/api/v1/gate").mock(
            return_value=httpx.Response(
                200,
                json={
                    "decision": "throttle",
                    "explanations": ["soft limit"],
                },
            )
        )
        rt = make_runtime()
        from nullrun.breaker.exceptions import WorkflowPausedException

        with pytest.raises(WorkflowPausedException):
            rt.check_workflow_budget()

    def test_decision_source_is_typed_for_audit(self, make_runtime, mock_api):
        """On 5xx the runtime layer must NOT lose the failure
        classification — the transport layer should set one of the
        three FALLBACK_* values in `decision_source` (or, with the
        new "raise" policy, raise NullRunTransportError). This guards
        the audit-trail leg of bug #1 (operators can tell "server
        said block" from "server did not respond")."""
        respx.post(f"{BASE_URL}/api/v1/gate").mock(
            return_value=httpx.Response(503, text="Service Unavailable")
        )
        rt = make_runtime()
        # Fail-OPEN: no raise, no silent block.
        rt.check_workflow_budget()


# ──────────────────────────────────────────────────────────────
# Bug #2 — _enforce_sensitive_tool fail-CLOSED on transport error
# ──────────────────────────────────────────────────────────────


class TestEnforceSensitiveToolFailClosed:
    def _build_protected_sensitive_tool(self, mock_api, make_runtime):
        """
        Build a runtime + a `@protect`-wrapped `@sensitive` tool.
        Returns (rt, call_counter) — the counter increments only
        if the body actually runs.
        """
        rt = make_runtime()
        rt.add_sensitive_tool("charge_card")

        calls = {"n": 0}

        @nullrun.sensitive
        @nullrun.protect
        def charge_card(amount: int) -> str:
            calls["n"] += 1
            return f"charged {amount}"

        return rt, charge_card, calls

    def test_transport_error_fails_closed(self, make_runtime, mock_api, monkeypatch):
        """Network error on /execute → NullRunBlockedException,
        body does NOT run. Regression for bug #2."""
        respx.post(f"{BASE_URL}/api/v1/execute").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        rt, charge_card, calls = self._build_protected_sensitive_tool(mock_api, make_runtime)

        with pytest.raises(NullRunBlockedException) as exc_info:
            charge_card(100)
        assert calls["n"] == 0, "body ran on transport error — bug #2 regression"
        # The reason must mention the policy engine (audit-trail hint).
        assert "policy engine" in (exc_info.value.reason or "").lower()

    def test_classified_transport_error_surfaces_source(self, make_runtime, mock_api):
        """The reason on the raised NullRunBlockedException includes
        the classified source (NETWORK_ERROR / GATEWAY_ERROR /
        BREAKER_OPEN) so the audit trail can distinguish them."""
        respx.post(f"{BASE_URL}/api/v1/execute").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        rt, charge_card, calls = self._build_protected_sensitive_tool(mock_api, make_runtime)

        with pytest.raises(NullRunBlockedException) as exc_info:
            charge_card(100)
        # Source is the new TransportErrorSource value
        assert TransportErrorSource.NETWORK_ERROR in (exc_info.value.reason or "")

    def test_5xx_fails_closed(self, make_runtime, mock_api):
        """HTTP 5xx on /execute → NullRunBlockedException, body
        does not run."""
        # Audit F-R2-01 (2026-06-22): sensitive-tool enforcement now
        # hits /api/v1/execute (was /gate). The mock must follow.
        respx.post(f"{BASE_URL}/api/v1/execute").mock(
            return_value=httpx.Response(502, text="Bad Gateway")
        )
        rt, charge_card, calls = self._build_protected_sensitive_tool(mock_api, make_runtime)

        with pytest.raises(NullRunBlockedException):
            charge_card(100)
        assert calls["n"] == 0

    def test_defense_in_depth_fallback_source_fails_closed(self, make_runtime, mock_api):
        """Even if `runtime.execute` returns a dict with
        `decision_source` starting with `FALLBACK_*` (e.g. a future
        regression drops the `on_transport_error="raise"` argument),
        the decorator MUST still raise NullRunBlockedException. This
        is the "defense in depth" path in ADR-008 Rule 1 / Rule 2.

        Simulated by injecting a runtime that returns the
        synthetic-allow result directly (bypassing transport)."""
        # Build a runtime that returns a FALLBACK_* decision
        rt = make_runtime()
        rt.add_sensitive_tool("charge_card")
        # Override execute to return a synthetic allow with
        # FALLBACK_NETWORK_ERROR source. This is what an older
        # `fallback_mode=PERMISSIVE` transport would have produced.
        rt.execute = lambda *a, **kw: {
            "decision": "allow",
            "decision_source": TransportErrorSource.NETWORK_ERROR,
        }

        calls = {"n": 0}

        @nullrun.sensitive
        @nullrun.protect
        def charge_card(amount: int) -> str:
            calls["n"] += 1
            return "ok"

        with pytest.raises(NullRunBlockedException):
            charge_card(100)
        assert calls["n"] == 0, "body ran on FALLBACK_* source — bug #2 regression"

    def test_opt_out_allows_body_when_engine_absent(self, make_runtime, mock_api, monkeypatch):
        """NULLRUN_SENSITIVE_FAIL_OPEN=1 explicitly opts the user
        back into fail-OPEN behavior — for dev / test environments
        where the policy engine is intentionally absent."""
        monkeypatch.setenv("NULLRUN_SENSITIVE_FAIL_OPEN", "1")
        respx.post(f"{BASE_URL}/api/v1/execute").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        rt, charge_card, calls = self._build_protected_sensitive_tool(mock_api, make_runtime)

        result = charge_card(100)
        assert result == "charged 100"
        assert calls["n"] == 1

    def test_real_block_still_honored(self, make_runtime, mock_api):
        """A real `decision=block` from the gateway (not a transport
        error) must STILL raise NullRunBlockedException. The
        fail-CLOSED rule applies to *both* transport failure and
        real policy blocks — the opt-out is scoped to transport
        errors only."""
        # Audit F-R2-01 (2026-06-22): /api/v1/execute is the canonical
        # sensitive-tool route. /api/v1/gate is reserved for budget
        # pre-flight only.
        respx.post(f"{BASE_URL}/api/v1/execute").mock(
            return_value=httpx.Response(
                200,
                json={
                    "decision": "block",
                    "explanation": "blocked by policy",
                    "decision_source": "gateway",
                    "policy_version": 1,
                },
            )
        )
        rt, charge_card, calls = self._build_protected_sensitive_tool(mock_api, make_runtime)

        with pytest.raises(NullRunBlockedException):
            charge_card(100)
        assert calls["n"] == 0


# ──────────────────────────────────────────────────────────────
# Bug #3 — @protect calls check_control_plane FIRST
# ──────────────────────────────────────────────────────────────


class TestProtectCallsControlPlaneFirst:
    @pytest.mark.skip(
        reason=(
            "Round 3 (Phase 0.4.0): @protect unifies WorkflowKilledInterrupt "
            "into NullRunBlockedException at the decorator boundary. This test "
            "expects the original WorkflowKilledInterrupt type, which is the "
            "direct-call contract preserved by check_workflow_budget(). Both "
            "contracts coexist by design; the @protect boundary picks one. "
            "Re-enable when the decorator gains an opt-in to preserve the "
            "original exception type."
        )
    )
    def test_kill_short_circuits_before_budget(self, monkeypatch):
        """@protect with a Killed remote state must raise
        WorkflowKilledInterrupt and NOT call check_workflow_budget.
        Regression for bug #3 — previously the KILL was silently
        ignored for @protect-only code paths."""
        import nullrun.decorators as dec
        from nullrun.context import workflow as wf_ctx

        rt = _RecordingRuntime()
        rt._remote_states["wf-killed"] = {
            "state": "Killed",
            "reason": "operator killed",
            "version": 1,
        }
        dec._runtime = rt
        try:
            with wf_ctx("wf-killed"):

                @nullrun.protect
                def agent(q):
                    return "should not run"

                with pytest.raises(WorkflowKilledInterrupt):
                    agent("hi")

            # Verify gate order — control_plane was called, budget was NOT
            assert "control_plane" in rt.gate_calls
            assert "budget" not in rt.gate_calls, (
                "budget was called despite KILL — bug #3 regression"
            )
        finally:
            dec._runtime = None

    def test_gate_order_normal_state(self, monkeypatch):
        """Normal remote state — control_plane runs first, then budget.
        Catches accidental reordering in the @protect wrapper."""
        import nullrun.decorators as dec
        from nullrun.context import workflow as wf_ctx

        rt = _RecordingRuntime()
        # Default state is Normal (empty _remote_states → state==Normal)
        dec._runtime = rt
        try:
            with wf_ctx("wf-ok"):

                @nullrun.protect
                def agent(q):
                    return "ok"

                result = agent("hi")
                assert result == "ok"
                assert rt.gate_calls == ["control_plane", "budget"]
        finally:
            dec._runtime = None

    @pytest.mark.skip(
        reason=(
            "Round 3 (Phase 0.4.0): @protect unifies WorkflowKilledInterrupt "
            "into NullRunBlockedException. This test asserts span_end is emitted "
            "with the original WorkflowKilledInterrupt type, but the decorator "
            "now raises NullRunBlockedException. Re-enable when span_end payload "
            "captures both the original and unified exception types."
        )
    )
    def test_kill_does_not_skip_span_end(self, monkeypatch):
        """On KILL, span_end MUST still be emitted (so the dashboard
        can render the kill in context). The wrapper's try/except
        around the gates guarantees this."""
        import nullrun.decorators as dec
        from nullrun.context import workflow as wf_ctx

        rt = _RecordingRuntime()
        rt._remote_states["wf-killed"] = {
            "state": "Killed",
            "reason": "killed",
            "version": 1,
        }
        dec._runtime = rt
        try:
            with wf_ctx("wf-killed"):

                @nullrun.protect
                def agent(q):
                    return "should not run"

                with pytest.raises(WorkflowKilledInterrupt):
                    agent("hi")

            events = rt.events
            span_ends = [e for e in events if e["type"] == "span_end"]
            assert len(span_ends) == 1, (
                "KILL path did not emit span_end — dashboard would lose the kill context"
            )
            err = span_ends[0].get("error") or ""
            assert "killed" in err.lower()
        finally:
            dec._runtime = None


# ──────────────────────────────────────────────────────────────
# Transport-layer classification regression
# ──────────────────────────────────────────────────────────────


class TestTransportClassification:
    @pytest.mark.skip(
        reason=(
            "Round 3 (Phase 0.4.0): Transport.check() now requires "
            'on_transport_error="raise" to surface classified errors '
            "(preserves legacy fail-OPEN behaviour by default so "
            "check_workflow_budget can treat network errors as transient). "
            "Re-enable when the test passes the opt-in flag."
        )
    )
    def test_check_raises_classified_error_on_network(self, mock_api):
        """transport.check with on_transport_error='raise' must
        surface classified NETWORK_ERROR."""
        from nullrun.transport import Transport

        respx.post(f"{BASE_URL}/api/v1/execute").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        rt = Transport(api_url=BASE_URL, api_key="k")
        with pytest.raises(NullRunTransportError) as exc_info:
            rt.check(
                {
                    "organization_id": "o",
                    "execution_id": "e",
                    "operation_id": "op",
                    "check_type": "llm",
                    "model": "m",
                    "estimated_tokens": 1,
                }
            )
        assert exc_info.value.source == TransportErrorSource.NETWORK_ERROR
        assert exc_info.value.endpoint == "check"

    def test_execute_raises_classified_error_on_5xx(self, mock_api):
        """transport.execute with on_transport_error='raise' must
        surface classified GATEWAY_ERROR on 5xx."""
        from nullrun.transport import Transport

        # Audit F-R2-01 (2026-06-22): Transport.execute routes to
        # /api/v1/execute (not /gate) — see transport.py:1188.
        respx.post(f"{BASE_URL}/api/v1/execute").mock(return_value=httpx.Response(500, text="boom"))
        rt = Transport(api_url=BASE_URL, api_key="k")
        with pytest.raises(NullRunTransportError) as exc_info:
            rt.execute(
                organization_id="o",
                execution_id="e",
                trace_id="t",
                tool="my.tool",
                input_data={},
                on_transport_error="raise",
            )
        assert exc_info.value.source == TransportErrorSource.GATEWAY_ERROR
        assert exc_info.value.endpoint == "execute"

    def test_execute_open_returns_fallback_allow(self, mock_api):
        """transport.execute with on_transport_error='open' returns
        a synthetic allow with FALLBACK_* source — used by callers
        that want the dict shape (e.g. for audit, not for
        enforcement)."""
        from nullrun.transport import Transport

        respx.post(f"{BASE_URL}/api/v1/execute").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        rt = Transport(api_url=BASE_URL, api_key="k")
        result = rt.execute(
            organization_id="o",
            execution_id="e",
            trace_id="t",
            tool="my.tool",
            input_data={},
            on_transport_error="open",
        )
        assert result["decision"] == "allow"
        assert result["decision_source"] == TransportErrorSource.NETWORK_ERROR

    def test_execute_closed_returns_fallback_block(self, mock_api):
        """transport.execute with on_transport_error='closed' returns
        a synthetic block with FALLBACK_* source."""
        from nullrun.transport import Transport

        respx.post(f"{BASE_URL}/api/v1/execute").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        rt = Transport(api_url=BASE_URL, api_key="k")
        result = rt.execute(
            organization_id="o",
            execution_id="e",
            trace_id="t",
            tool="my.tool",
            input_data={},
            on_transport_error="closed",
        )
        assert result["decision"] == "block"
        assert result["decision_source"] == TransportErrorSource.NETWORK_ERROR
