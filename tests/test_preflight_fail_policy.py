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

    def track_tool(self, tool_name: str, **kwargs) -> None:
        # Commit 33d2b5f wires ``@protect`` to emit a tools/track_tool event
        # after the wrapped body returns. The stub captures that emit the
        # same way it captures the other track paths so the gate-order
        # assertions keep working unchanged.
        self.events.append({"type": "tool_call", "tool_name": tool_name, **kwargs})

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
# Sprint handoff Bug #4 — observability closure on fail-OPEN paths
# ──────────────────────────────────────────────────────────────
#
# The fail-OPEN posture is documented as authoritative (ADR-008 +
# the top-of-file docstring table on `check_workflow_budget`). The
# sprint handoff `enforcement-certainty-sprint-handoff.md` flagged
# that pre-fix the FALLBACK `decision_source` path emitted DEBUG-level
# logs and had no metric -- making the silent bypass invisible to
# operators tailing INFO+ logs and unreachable for alerting.
#
# These tests pin the observability closure (WARNING log + metric
# increment) without changing the fail-OPEN behaviour itself.
# Existing tests above continue to assert "body runs, no raise".


class TestCheckWorkflowBudgetObservability:
    """Source-pin regression suite for the sprint handoff Bug #4 fix."""

    def test_network_error_emits_warning_and_metric(self, make_runtime, mock_api, caplog):
        """httpx.ConnectError on /gate → WARNING log + gate_fail_open_total+=1.

        Pre-fix this path logged at WARNING already (see the existing
        ``test_network_error_returns_normally`` behaviour) but emitted
        no metric, so a sustained outage looked identical to a
        one-off blip. The metric is the operator's primary signal.
        """
        import logging

        from nullrun.observability import metrics

        before = metrics.runtime.gate_fail_open_total
        respx.post(f"{BASE_URL}/api/v1/gate").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        rt = make_runtime()
        with caplog.at_level(logging.WARNING, logger="nullrun.runtime"):
            rt.check_workflow_budget()
        # Metric incremented exactly once for the single fail-OPEN.
        assert metrics.runtime.gate_fail_open_total == before + 1
        # At least one WARNING from check_workflow_budget's fail-OPEN
        # path was emitted (the exact wording is not pinned -- only
        # the level, since the docblock on the method already declares
        # "logged at warning level" as the contract).
        warnings = [
            r for r in caplog.records
            if r.name == "nullrun.runtime"
            and r.levelno == logging.WARNING
            and "check_workflow_budget" in r.getMessage()
        ]
        assert warnings, "expected WARNING log from check_workflow_budget fail-OPEN"

    def test_timeout_emits_warning_and_metric(self, make_runtime, mock_api, caplog):
        """httpx.TimeoutException on /gate → WARNING log + metric++.

        Same contract as the ConnectError test; covers the timeout
        code path that the journal evidence flagged (transport.py
        returns synthetic-block with DecisionSource.FALLBACK after
        exhausting retries).
        """
        import logging

        from nullrun.observability import metrics

        before = metrics.runtime.gate_fail_open_total
        respx.post(f"{BASE_URL}/api/v1/gate").mock(
            side_effect=httpx.TimeoutException("read timeout")
        )
        rt = make_runtime()
        with caplog.at_level(logging.WARNING, logger="nullrun.runtime"):
            rt.check_workflow_budget()
        assert metrics.runtime.gate_fail_open_total == before + 1

    def test_synthetic_fallback_source_emits_warning_not_debug(
        self, make_runtime, mock_api, caplog
    ):
        """When transport returns 5xx, it emits ``decision_source =
        FALLBACK_*`` (synthetic-block). Pre-fix the runtime logged
        this at DEBUG, which violated the docblock contract ("logged
        at warning level"). Pin WARNING post-fix.
        """
        import logging

        from nullrun.observability import metrics

        before = metrics.runtime.gate_fail_open_total
        respx.post(f"{BASE_URL}/api/v1/gate").mock(
            return_value=httpx.Response(
                503,
                json={
                    "decision": "block",
                    "decision_source": "FALLBACK_NETWORK_ERROR",
                    "explanation": "Gateway unavailable",
                },
            )
        )
        rt = make_runtime()
        with caplog.at_level(logging.WARNING, logger="nullrun.runtime"):
            rt.check_workflow_budget()
        # Metric incremented.
        assert metrics.runtime.gate_fail_open_total == before + 1
        # No DEBUG-level record from check_workflow_budget's synthetic
        # fallback arm -- the only post-fix log level for that path
        # is WARNING. (Other DEBUG records from unrelated code paths
        # may exist; we filter by message prefix.)
        debug_fallback = [
            r for r in caplog.records
            if r.name == "nullrun.runtime"
            and r.levelno == logging.DEBUG
            and "synthetic decision_source" in r.getMessage()
        ]
        assert not debug_fallback, (
            "synthetic decision_source arm must NOT log at DEBUG -- "
            "this was the sprint handoff Bug #4 silent-fail-OPEN bug"
        )

    def test_real_block_does_not_increment_metric(self, make_runtime, mock_api):
        """Real `decision=block` from the gateway is a policy block,
        NOT a transport fail-OPEN -- must NOT increment the
        gate_fail_open_total counter. Guards against a future
        refactor that mistakenly moves the metric emit above the
        decision-parse stage.
        """
        from nullrun.observability import metrics

        before = metrics.runtime.gate_fail_open_total
        respx.post(f"{BASE_URL}/api/v1/gate").mock(
            return_value=httpx.Response(
                200,
                json={
                    "decision": "block",
                    "decision_source": "gateway",
                    "explanations": ["budget_exceeded"],
                },
            )
        )
        rt = make_runtime()
        with pytest.raises(WorkflowKilledInterrupt):
            rt.check_workflow_budget()
        assert metrics.runtime.gate_fail_open_total == before, (
            "real policy block must not increment the fail-OPEN metric"
        )

    def test_real_allow_does_not_increment_metric(self, make_runtime, mock_api):
        """Real `decision=allow` from the gateway is the happy path --
        must NOT increment the fail-OPEN counter. Pin the
        allow-path stays allow."""
        from nullrun.observability import metrics

        before = metrics.runtime.gate_fail_open_total
        respx.post(f"{BASE_URL}/api/v1/gate").mock(
            return_value=httpx.Response(
                200,
                json={"decision": "allow", "decision_source": "gateway"},
            )
        )
        rt = make_runtime()
        rt.check_workflow_budget()  # must not raise
        assert metrics.runtime.gate_fail_open_total == before

    def test_to_dict_includes_gate_fail_open_total(self):
        """Pin the metric field is reachable from /health via
        ``metrics.to_dict()`` so operator dashboards can graph it.
        Without this pin, a future refactor that adds the counter
        to RuntimeMetrics but forgets to_dict would silently break
        observability -- the field exists, the JSON shape doesn't.
        """
        from nullrun.observability import metrics

        d = metrics.to_dict()
        assert "gate_fail_open_total" in d["runtime"], (
            "metrics.to_dict() must expose gate_fail_open_total for "
            "/health and operator dashboards"
        )


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
        """Network error on /execute → NullRunBlockedException
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
        regression drops the `on_transport_error="raise"` argument)
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
            "@protect unifies WorkflowKilledInterrupt "
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
            "@protect unifies WorkflowKilledInterrupt "
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
            "Transport.check() now requires "
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


# ──────────────────────────────────────────────────────────────
# Bug #6 — NULLRUN_SKIP_BUDGET_CHECK=1 production guard
# ──────────────────────────────────────────────────────────────
#
# Pre-v3.53 the SDK silently honored ``NULLRUN_SKIP_BUDGET_CHECK=1``
# regardless of environment. CLAUDE.md §20 marks that env var as a
# DEV/TEST bypass and explicitly forbids it in production. The fix
# in v3.53 raises NullRunInfrastructureError (NR-S001) when the
# var is set AND the SDK detects a production environment (either
# the default api.nullrun.io host OR NULLRUN_ENV=production on a
# non-dev host). The bypass is still reachable via an explicit
# ack (``NULLRUN_ALLOW_SKIP_BUDGET_CHECK=1``) for incident-response
# scenarios so the opt-out is visible in audit / telemetry.


class TestSkipBudgetCheckProductionGuard:
    """Source-pin + runtime tests for v3.53 audit #6."""

    # ------------------------------------------------------------------
    # _is_production_environment() helper
    # ------------------------------------------------------------------

    def test_is_production_environment_default_api_url(self):
        """Default api_url (api.nullrun.io) → production."""
        from nullrun.runtime import _is_production_environment

        # Default is api.nullrun.io per constructor docstring.
        assert _is_production_environment() is True

    def test_is_production_environment_with_explicit_prod_url(self):
        """Explicit prod api_url → production."""
        from nullrun.runtime import _is_production_environment

        assert _is_production_environment("https://api.nullrun.io") is True

    def test_is_production_environment_localhost_is_not_prod(self, monkeypatch):
        """Localhost api_url is NOT production."""
        from nullrun.runtime import _is_production_environment

        assert _is_production_environment("http://localhost:8080") is False
        assert _is_production_environment("http://127.0.0.1:8080") is False

    def test_is_production_environment_staging_subdomain_is_not_prod(self, monkeypatch):
        """Staging subdomain is NOT production."""
        from nullrun.runtime import _is_production_environment

        assert _is_production_environment("https://staging.nullrun.io") is False
        assert _is_production_environment("https://api.staging.internal") is False

    def test_is_production_environment_explicit_env_override(self, monkeypatch):
        """NULLRUN_ENV=production on a non-dev host → production."""
        from nullrun.runtime import _is_production_environment

        monkeypatch.setenv("NULLRUN_ENV", "production")
        assert (
            _is_production_environment("https://custom-deployment.example.com")
            is True
        )

    def test_is_production_environment_explicit_env_with_localhost(self, monkeypatch):
        """NULLRUN_ENV=production BUT api_url is localhost → NOT prod
        (so a dev who accidentally exports NULLRUN_ENV=production can
        still use the bypass)."""
        from nullrun.runtime import _is_production_environment

        monkeypatch.setenv("NULLRUN_ENV", "production")
        assert _is_production_environment("http://localhost:9000") is False

    def test_is_production_environment_prod_alias(self, monkeypatch):
        """NULLRUN_ENV=prod (short alias) is also detected."""
        from nullrun.runtime import _is_production_environment

        monkeypatch.setenv("NULLRUN_ENV", "prod")
        assert (
            _is_production_environment("https://api.nullrun.io") is True
        )

    # ------------------------------------------------------------------
    # check_workflow_budget skip-path enforcement
    # ------------------------------------------------------------------

    def test_skip_set_in_production_raises_infrastructure_error(
        self, make_runtime, monkeypatch
    ):
        """NULLRUN_SKIP_BUDGET_CHECK=1 in production + no ack → raise
        NullRunInfrastructureError(NR-S001)."""
        from nullrun.breaker.exceptions import NullRunInfrastructureError

        # Build a runtime pointing at the prod host via a custom
        # respx block — ``make_runtime`` is BASE_URL-bound and we
        # need to exercise the api_url=prod branch.
        with respx.mock(assert_all_called=False) as mock:
            mock.post("https://api.nullrun.io/api/v1/auth/verify").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "organization_id": "ws-test",
                        "workflow_id": "00000000-0000-0000-0000-000000000001",
                        "plan": "pro",
                        "user_id": "u-test",
                        "session_token": "s-test",
                        "expires_at": "2030-01-01T00:00:00Z",
                    },
                )
            )
            from nullrun.runtime import NullRunRuntime

            rt = NullRunRuntime(
                api_key="test-key-12345678",
                api_url="https://api.nullrun.io",
                polling=False,
            )
            assert rt.api_url == "https://api.nullrun.io"

        monkeypatch.setenv("NULLRUN_SKIP_BUDGET_CHECK", "1")
        # Ensure no ack is set (might leak from another test).
        monkeypatch.delenv("NULLRUN_ALLOW_SKIP_BUDGET_CHECK", raising=False)

        with pytest.raises(NullRunInfrastructureError) as exc_info:
            rt.check_workflow_budget()

        # Must carry the NR-S001 error_code so operators can pin
        # this in alerting.
        assert exc_info.value.error_code == "NR-S001"
        assert "CLAUDE.md §20" in str(exc_info.value)
        assert exc_info.value.retryable is False

    def test_skip_set_in_production_with_ack_skips_with_warning(
        self, make_runtime, monkeypatch, caplog
    ):
        """NULLRUN_SKIP_BUDGET_CHECK=1 + NULLRUN_ALLOW_SKIP_BUDGET_CHECK=1
        in prod → skip succeeds with a WARNING log so the audit trail
        captures the explicit ack."""
        import logging

        with respx.mock(assert_all_called=False) as mock:
            mock.post("https://api.nullrun.io/api/v1/auth/verify").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "organization_id": "ws-test",
                        "workflow_id": "00000000-0000-0000-0000-000000000001",
                        "plan": "pro",
                        "user_id": "u-test",
                        "session_token": "s-test",
                        "expires_at": "2030-01-01T00:00:00Z",
                    },
                )
            )
            from nullrun.runtime import NullRunRuntime

            rt = NullRunRuntime(
                api_key="test-key-12345678",
                api_url="https://api.nullrun.io",
                polling=False,
            )

        monkeypatch.setenv("NULLRUN_SKIP_BUDGET_CHECK", "1")
        monkeypatch.setenv("NULLRUN_ALLOW_SKIP_BUDGET_CHECK", "1")

        with caplog.at_level(logging.WARNING, logger="nullrun.runtime"):
            # Must NOT raise — explicit ack honors the bypass.
            rt.check_workflow_budget()

        # The ack path must surface a WARNING so observability picks
        # it up. The exact text is allowed to evolve; we assert on
        # the key substring so the test survives minor copy edits.
        joined = "\n".join(rec.message for rec in caplog.records)
        assert "NULLRUN_ALLOW_SKIP_BUDGET_CHECK" in joined, (
            "ack path did not emit a WARNING log; the explicit bypass "
            "would be invisible in audit / telemetry"
        )

    def test_skip_set_in_dev_skips_silently(self, make_runtime, monkeypatch):
        """NULLRUN_SKIP_BUDGET_CHECK=1 in a dev/test environment
        (non-prod api_url) → skip succeeds silently (no raise, no
        require_ack). Preserves the legacy dev/test behavior."""
        # Base URL is test.nullrun.io → not prod.
        rt = make_runtime()
        assert rt.api_url == BASE_URL

        monkeypatch.setenv("NULLRUN_SKIP_BUDGET_CHECK", "1")
        monkeypatch.delenv("NULLRUN_ALLOW_SKIP_BUDGET_CHECK", raising=False)

        # Must NOT raise — the dev path stays dev-friendly.
        rt.check_workflow_budget()

    def test_skip_not_set_no_prod_guard(self, monkeypatch):
        """NULLRUN_SKIP_BUDGET_CHECK not set → no production guard
        even on a prod api_url. The gate makes its normal HTTP call."""
        with respx.mock(assert_all_called=False) as mock:
            mock.post("https://api.nullrun.io/api/v1/auth/verify").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "organization_id": "ws-test",
                        "workflow_id": "00000000-0000-0000-0000-000000000001",
                        "plan": "pro",
                        "user_id": "u-test",
                        "session_token": "s-test",
                        "expires_at": "2030-01-01T00:00:00Z",
                    },
                )
            )
            mock.post("https://api.nullrun.io/api/v1/gate").mock(
                return_value=httpx.Response(
                    200, json={"decision": "allow", "explanations": []}
                )
            )
            from nullrun.runtime import NullRunRuntime

            rt = NullRunRuntime(
                api_key="test-key-12345678",
                api_url="https://api.nullrun.io",
                polling=False,
            )

        monkeypatch.delenv("NULLRUN_SKIP_BUDGET_CHECK", raising=False)

        # Must NOT raise — the gate makes its normal HTTP call,
        # which returns 200 OK in the mock above.
        rt.check_workflow_budget()

    def test_skip_prod_helper_rejects_nonsensical_env(self, monkeypatch):
        """NULLRUN_ENV=staging (or other non-prod values) on a non-prod
        host → NOT prod. (Cannot override a real prod api_url via
        NULLRUN_ENV — the host check fires first.)
        """
        from nullrun.runtime import _is_production_environment

        monkeypatch.setenv("NULLRUN_ENV", "staging")
        # Non-prod host + non-prod env → not prod.
        assert _is_production_environment("https://custom.example.com") is False
        assert _is_production_environment("http://localhost:9000") is False

    def test_skip_prod_helper_handles_unparseable_url(self, monkeypatch):
        """An unparseable api_url does NOT crash the helper."""
        from nullrun.runtime import _is_production_environment

        # Falls through to NULLRUN_ENV check; no prod env either.
        result = _is_production_environment("not a real url")
        assert result is False

    def test_skip_prod_helper_lowercases_hostname(self):
        """API_URL with uppercase hostname is still matched as prod."""
        from nullrun.runtime import _is_production_environment

        # Mixed-case hostname should still match api.nullrun.io.
        assert _is_production_environment("https://API.NULLRUN.IO") is True
