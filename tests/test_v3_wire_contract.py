"""
Contract tests pinning the v3 wire format.

Each test guards a specific class of wire-drift so a future SDK refactor
trips CI rather than silently breaking the v3 backend. If you change
any of these and the tests fail, update the matching backend file in
lock-step — do not edit one side alone.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from unittest.mock import patch

import httpx
import pytest
import respx
from httpx import Response

from nullrun.breaker.exceptions import (
    NullRunBackendError,
    NullRunBudgetError,
    NullRunChainError,
    NullRunConsumeOverbudgetError,
    NullRunError,
    NullRunProtocolError,
    NullRunRateLimitRedisError,
    NullRunWorkflowInactiveError,
    RateLimitError,
)
from nullrun.context import (
    _chain_id_var,
    _chain_op_var,
    chain,
    get_chain_id,
    set_chain_id,
    workflow,
)
from nullrun.transport import (
    _V3_ERROR_CODE_MAP,
    HEADER_PROTOCOL,
    NULLRUN_PROTOCOL_VERSION,
    Transport,
    _parse_v3_error_envelope,
)

BASE_URL = "https://api.test.nullrun.io"


# ─────────────────────────────────────────────────────────────────────
# FIX: every signed POST must carry X-NULLRUN-PROTOCOL: <current>
# ─────────────────────────────────────────────────────────────────────
#
# Without this header the backend's protocol middleware rejects with
# HTTP 400 + error_code PROTOCOL_HEADER_REQUIRED BEFORE the gate
# pipeline runs. Centralising the value in
# ``nullrun.transport._protocol_header_value `` means a future
# bump is a one-line change.


class TestProtocolHeaderConstant:
    """The wire-protocol version constant + helper stay in sync."""

    def test_version_is_three(self):
        # Bumping this requires a coordinated backend release —
        # see (semver: major = breaking wire change).
        assert NULLRUN_PROTOCOL_VERSION == 3

    def test_header_name_is_dashed(self):
        # Match the backend's HeaderName parsing (axum 0.7 normalises
        # to lowercase; the wire value is the canonical
        # case-sensitive form per the v3 spec).
        assert HEADER_PROTOCOL == "X-NULLRUN-PROTOCOL"

    def test_protocol_header_value_helper(self):
        from nullrun.transport import _protocol_header_value

        # Stored as u32 on the wire — serialise the integer directly
        # (``"3"``, not ``"v3"``).
        assert _protocol_header_value() == "3"


class TestSignedPostIncludesProtocolHeader:
    """Every signed POST must include ``X-NULLRUN-PROTOCOL: 3``."""

    @respx.mock
    def test_track_batch_includes_protocol_header(self):
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            route = respx.post(f"{BASE_URL}/api/v1/track/batch").mock(
                return_value=Response(200, json={"ok": True, "accepted": 1})
            )
            t._send_batch_with_retry_info([{"event": "test"}])
            sent = route.calls.last.request
            assert sent.headers["X-NULLRUN-PROTOCOL"] == "3"
        finally:
            t.stop()

    @respx.mock
    def test_check_includes_protocol_header(self):
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            route = respx.post(f"{BASE_URL}/api/v1/gate").mock(
                return_value=Response(
                    200,
                    json={"decision": "allow", "decision_source": "gateway"},
                )
            )
            t.check({"check_type": "llm", "estimated_tokens": 1})
            sent = route.calls.last.request
            assert sent.headers["X-NULLRUN-PROTOCOL"] == "3"
        finally:
            t.stop()

    @respx.mock
    def test_check_v3_includes_protocol_header(self):
        # 2026-07-04 (B1): ``check_v3`` now delegates to
        # ``check `` which targets /api/v1/gate (the
        # /api/v1/check endpoint was removed 2026-06-27 and returns
        # 410 Gone). Wire the mock against /api/v1/gate to match.
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            route = respx.post(f"{BASE_URL}/api/v1/gate").mock(
                return_value=Response(
                    200,
                    json={
                        "decision": "allow",
                        "decision_source": "gateway",
                        "execution_id": "00000000-0000-0000-0000-000000000099",
                    },
                )
            )
            t.check_v3({"check_type": "llm", "estimated_tokens": 1})
            sent = route.calls.last.request
            assert sent.headers["X-NULLRUN-PROTOCOL"] == "3"
        finally:
            t.stop()

    @respx.mock
    def test_track_single_includes_protocol_header(self):
        # 2026-07-04 (B2): body shape matches the v3 wire
        # contract — ``reservation_id`` (server-minted from /check)
        # ``workflow_id`` + ``tokens`` + ``cost_cents`` (the SDK
        # always emits 0 — backend recomputes from tokens) +
        # ``cost_source: "provisional"``. Pre-fix this test sent the
        # legacy / fictitious shape
        # ``{execution_id, actual_cost_cents}`` which doesn't match
        # ``TrackRequestRaw`` and would 422 on the wire.
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            route = respx.post(f"{BASE_URL}/api/v1/track").mock(
                return_value=Response(200, json={"status": "ok"})
            )
            t.track_single(
                {
                    "reservation_id": "00000000-0000-0000-0000-000000000099",
                    "workflow_id": "wf-1",
                    "tokens": 100,
                    "cost_cents": 0,
                    "cost_source": "provisional",
                }
            )
            sent = route.calls.last.request
            assert sent.headers["X-NULLRUN-PROTOCOL"] == "3"
        finally:
            t.stop()

    @respx.mock
    def test_cancel_includes_protocol_header(self):
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            route = respx.post(f"{BASE_URL}/api/v1/cancel").mock(
                return_value=Response(200, json={"status": "ok"})
            )
            t.cancel("exec-1")
            sent = route.calls.last.request
            assert sent.headers["X-NULLRUN-PROTOCOL"] == "3"
        finally:
            t.stop()

    @respx.mock
    def test_heartbeat_includes_protocol_header(self):
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            route = respx.post(f"{BASE_URL}/api/v1/heartbeat").mock(
                return_value=Response(200, json={"status": "ok"})
            )
            t.heartbeat("chain-abc")
            sent = route.calls.last.request
            assert sent.headers["X-NULLRUN-PROTOCOL"] == "3"
        finally:
            t.stop()

    @respx.mock
    def test_chain_end_includes_protocol_header(self):
        # 2026-07-04 (B3): ``chain_end`` now POSTs to
        # /api/v1/gate with ``chain_op: "end"``. The /api/v1/chain/end
        # endpoint was never registered on the backend.
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            route = respx.post(f"{BASE_URL}/api/v1/gate").mock(
                return_value=Response(200, json={"decision": "allow"})
            )
            t.chain_end("chain-abc")
            sent = route.calls.last.request
            assert sent.headers["X-NULLRUN-PROTOCOL"] == "3"
            body = sent.content.decode("utf-8")
            assert '"chain_id":"chain-abc"' in body
            assert '"chain_op":"end"' in body
        finally:
            t.stop()

    @respx.mock
    def test_approximate_budget_includes_protocol_header(self):
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            route = respx.get(f"{BASE_URL}/api/v1/budget/approximate").mock(
                return_value=Response(
                    200,
                    json={
                        "current_spend_cents_estimate": 500,
                        "is_approximate": True,
                        "source": "RedisPeriod",
                        "confidence": "High",
                        "last_updated_at": "2026-07-02T00:00:00Z",
                    },
                )
            )
            t.approximate_budget(organization_id="org-1")
            sent = route.calls.last.request
            assert sent.headers["X-NULLRUN-PROTOCOL"] == "3"
        finally:
            t.stop()

    @respx.mock
    def test_execute_includes_protocol_header(self):
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            route = respx.post(f"{BASE_URL}/api/v1/execute").mock(
                return_value=Response(
                    200,
                    json={"decision": "allow", "decision_source": "gateway"},
                )
            )
            t.execute(
                organization_id="org-1",
                execution_id="exec-1",
                trace_id="trace-1",
                tool="bash",
                input_data={"command": "ls"},
            )
            sent = route.calls.last.request
            assert sent.headers["X-NULLRUN-PROTOCOL"] == "3"
        finally:
            t.stop()

    @respx.mock
    def test_refetch_credentials_includes_protocol_header(self):
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            route = respx.post(f"{BASE_URL}/api/v1/auth/verify").mock(
                return_value=Response(
                    200,
                    json={"organization_id": "org-1", "secret_key": "s-new"},
                )
            )
            asyncio.run(t._refetch_credentials())
            sent = route.calls.last.request
            assert sent.headers["X-NULLRUN-PROTOCOL"] == "3"
        finally:
            t.stop()


# ─────────────────────────────────────────────────────────────────────
# — chain_id / chain_op / idempotency_key / stream forwarding on
# /gate and /check. Additive: missing keys are omitted, not nulled.
# ─────────────────────────────────────────────────────────────────────


class TestWireContractV3FieldsForwarded:
    """check() forwards v3 fields when present, omits when absent."""

    @respx.mock
    def test_check_forwards_chain_id_and_op(self):
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            route = respx.post(f"{BASE_URL}/api/v1/gate").mock(
                return_value=Response(
                    200,
                    json={"decision": "allow", "decision_source": "gateway"},
                )
            )
            t.check(
                {
                    "check_type": "llm",
                    "estimated_tokens": 1,
                    "chain_id": "00000000-0000-0000-0000-000000000777",
                    "chain_op": "start",
                    "idempotency_key": "idem-1",
                    "stream": True,
                }
            )
            sent = route.calls.last.request
            body = sent.content.decode("utf-8")
            assert '"chain_id":"00000000-0000-0000-0000-000000000777"' in body
            assert '"chain_op":"start"' in body
            assert '"idempotency_key":"idem-1"' in body
            assert '"stream":true' in body
        finally:
            t.stop()

    @respx.mock
    def test_check_omits_chain_id_when_not_provided(self):
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            route = respx.post(f"{BASE_URL}/api/v1/gate").mock(
                return_value=Response(
                    200,
                    json={"decision": "allow", "decision_source": "gateway"},
                )
            )
            t.check({"check_type": "llm", "estimated_tokens": 1})
            sent = route.calls.last.request
            body = sent.content.decode("utf-8")
            # Legacy callers must not get a chain_id key injected —
            # the wire shape stays additive (missing = "single-shot
            # Hard mode").
            assert "chain_id" not in body
            assert "chain_op" not in body
            assert "idempotency_key" not in body
        finally:
            t.stop()

    @respx.mock
    def test_check_v3_accepts_chain_context(self):
        # 2026-07-04 (B1): ``check_v3`` delegates to
        # ``check `` which posts to /api/v1/gate. The /api/v1/check
        # endpoint returns 410 Gone since 2026-06-27.
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            route = respx.post(f"{BASE_URL}/api/v1/gate").mock(
                return_value=Response(
                    200,
                    json={
                        "decision": "allow",
                        "decision_source": "gateway",
                        "execution_id": "00000000-0000-0000-0000-000000000123",
                    },
                )
            )
            t.check_v3(
                {
                    "check_type": "llm",
                    "estimated_tokens": 1,
                    "chain_id": "00000000-0000-0000-0000-000000000555",
                    "chain_op": "continue",
                    "idempotency_key": "idem-2",
                }
            )
            sent = route.calls.last.request
            body = sent.content.decode("utf-8")
            assert '"chain_id":"00000000-0000-0000-0000-000000000555"' in body
            assert '"chain_op":"continue"' in body
            assert '"idempotency_key":"idem-2"' in body
        finally:
            t.stop()


# ─────────────────────────────────────────────────────────────────────
# — v3 error envelope → typed exception mapping
# ─────────────────────────────────────────────────────────────────────
#
# The backend returns errors as a JSON envelope of the shape
# ``{"error_code": "BUDGET_HARD_BLOCKED", "error_message": "..."
# "details": {...}, "retry_after_ms": N}``. The mapping is
# exhaustive (16 codes), so a future addition to the backend is
# caught here as a missing key in ``_V3_ERROR_CODE_MAP``.


class TestV3ErrorEnvelopeMapping:
    """_parse_v3_error_envelope translates backend codes → typed SDK exceptions."""

    def _make_response(self, status: int, body: dict | None) -> httpx.Response:
        if body is None:
            return httpx.Response(status)
        return httpx.Response(status, json=body)

    def test_protocol_too_old_maps_to_protocol_error(self):
        resp = self._make_response(
            400,
            {
                "error_code": "PROTOCOL_TOO_OLD",
                "error_message": "SDK too old",
                "details": {"current": 2, "min": 3},
            },
        )
        exc = _parse_v3_error_envelope(resp, "check")
        assert isinstance(exc, NullRunProtocolError)
        assert exc.error_code == "NR-P001"

    def test_protocol_too_new_maps_to_protocol_error(self):
        resp = self._make_response(
            400,
            {"error_code": "PROTOCOL_TOO_NEW", "error_message": "SDK too new"},
        )
        exc = _parse_v3_error_envelope(resp, "check")
        assert isinstance(exc, NullRunProtocolError)

    def test_budget_hard_blocked_maps_to_budget_error(self):
        resp = self._make_response(
            402,
            {
                "error_code": "BUDGET_HARD_BLOCKED",
                "error_message": "Hard limit reached",
                "details": {"current_spend_cents": 1000, "budget_cents": 1000},
            },
        )
        exc = _parse_v3_error_envelope(resp, "check")
        assert isinstance(exc, NullRunBudgetError)

    def test_redis_unavailable_maps_to_budget_error(self):
        #: REDIS_UNAVAILABLE is fail-CLOSED → 402
        resp = self._make_response(
            402,
            {"error_code": "REDIS_UNAVAILABLE", "error_message": "Redis down"},
        )
        exc = _parse_v3_error_envelope(resp, "check")
        assert isinstance(exc, NullRunBudgetError)

    def test_chain_max_duration_maps_to_chain_error(self):
        resp = self._make_response(
            402,
            {
                "error_code": "CHAIN_MAX_DURATION_EXCEEDED",
                "error_message": "chain > 1h",
                "details": {"chain_id": "abc"},
            },
        )
        exc = _parse_v3_error_envelope(resp, "check")
        assert isinstance(exc, NullRunChainError)
        assert exc.chain_id == "abc"
        assert exc.backend_code == "CHAIN_MAX_DURATION_EXCEEDED"

    def test_chain_cross_org_maps_to_chain_error(self):
        resp = self._make_response(
            403,
            {"error_code": "CHAIN_CROSS_ORG", "error_message": "wrong org"},
        )
        exc = _parse_v3_error_envelope(resp, "check")
        assert isinstance(exc, NullRunChainError)

    def test_workflow_inactive_maps_to_workflow_inactive_error(self):
        resp = self._make_response(
            403,
            {
                "error_code": "WORKFLOW_INACTIVE",
                "error_message": "workflow deleted",
                "details": {"workflow_id": "wf-1"},
            },
        )
        exc = _parse_v3_error_envelope(resp, "check")
        assert isinstance(exc, NullRunWorkflowInactiveError)
        assert exc.workflow_id == "wf-1"

    def test_consume_overbudget_maps_to_consume_overbudget_error(self):
        resp = self._make_response(
            422,
            {
                "error_code": "CONSUME_OVERBUDGET",
                "error_message": "actual > reserved + epsilon",
                "details": {
                    "reserved_cents": 100,
                    "max_allowed_cents": 101,
                    "actual_cost_cents": 150,
                    "epsilon_cents": 1,
                },
            },
        )
        exc = _parse_v3_error_envelope(resp, "track")
        assert isinstance(exc, NullRunConsumeOverbudgetError)
        assert exc.reserved_cents == 100
        assert exc.max_allowed_cents == 101
        assert exc.actual_cost_cents == 150
        assert exc.epsilon_cents == 1

    def test_rate_limit_exceeded_maps_to_rate_limit_error(self):
        resp = self._make_response(
            429,
            {
                "error_code": "RATE_LIMIT_EXCEEDED",
                "error_message": "too many",
                "retry_after_ms": 5000,
            },
        )
        exc = _parse_v3_error_envelope(resp, "check")
        assert isinstance(exc, RateLimitError)
        # retry_after is converted from ms to seconds
        assert exc.retry_after == 5.0

    def test_rate_limit_redis_unavailable_maps_to_infra_error(self):
        #: fail-CLOSED for aggregate rate limit
        resp = self._make_response(
            503,
            {"error_code": "RATE_LIMIT_REDIS_UNAVAILABLE", "error_message": "redis down"},
        )
        exc = _parse_v3_error_envelope(resp, "check")
        assert isinstance(exc, NullRunRateLimitRedisError)

    def test_budget_data_unavailable_maps_to_backend_error(self):
        #: dashboard must show "Data unavailable", not "$0"
        resp = self._make_response(
            503,
            {"error_code": "BUDGET_DATA_UNAVAILABLE", "error_message": "no sources"},
        )
        exc = _parse_v3_error_envelope(resp, "approximate_budget")
        assert isinstance(exc, NullRunBackendError)

    def test_unknown_error_code_falls_back_to_status_branching(self):
        # An error_code we haven't catalogued yet must still raise
        # SOMETHING — the parser falls back to status-code branching.
        resp = self._make_response(
            503,
            {"error_code": "FUTURE_UNKNOWN_CODE", "error_message": "x"},
        )
        exc = _parse_v3_error_envelope(resp, "check")
        assert isinstance(exc, NullRunBackendError)
        # status_code is stashed in details by NullRunBackendError.
        assert exc.details.get("status_code") == 503

    def test_retry_after_header_takes_precedence_over_json(self):
        # Server-side convention: header is canonical (RFC 7231)
        # JSON is a NullRun-specific fallback. Header wins on conflict.
        resp = httpx.Response(
            429,
            json={"error_code": "RATE_LIMIT_EXCEEDED", "error_message": "x"},
            headers={"Retry-After": "3"},
        )
        exc = _parse_v3_error_envelope(resp, "check")
        assert isinstance(exc, RateLimitError)
        assert exc.retry_after == 3.0


class TestV3ErrorMapCatalog:
    """Every backend error code has a mapping entry to a typed exception."""

    def test_catalog_covers_all_documented_codes(self):
        # Frozen catalog: every backend code documented in 
        # must have a mapping entry. If you add a new code on
        # the backend side, add it here too.
        expected = {
            "PROTOCOL_TOO_OLD",
            "PROTOCOL_TOO_NEW",
            "BUDGET_HARD_BLOCKED",
            "BUDGET_SOFT_BLOCKED",
            "BUDGET_OVERDRAFT_EXCEEDED",
            "BUDGET_PERIOD_NOT_STARTED",
            "REDIS_UNAVAILABLE",
            "CHAIN_MAX_DURATION_EXCEEDED",
            "CHAIN_CROSS_ORG",
            "CHAIN_ORG_MISMATCH",
            "WORKFLOW_INACTIVE",
            "API_KEY_REVOKED",
            "CONSUME_OVERBUDGET",
            "RATE_LIMIT_EXCEEDED",
            "RATE_LIMIT_REDIS_UNAVAILABLE",
            "BUDGET_DATA_UNAVAILABLE",
        }
        actual = set(_V3_ERROR_CODE_MAP.keys())
        missing = expected - actual
        assert not missing, f"Missing v3 error_code mappings: {missing}"


# ─────────────────────────────────────────────────────────────────────
# — chain context helpers (contextmanager, getters, setters)
# ─────────────────────────────────────────────────────────────────────


class TestChainContextHelpers:
    """ContextVars + contextmanager for soft-mode chain support."""

    def teardown_method(self):
        # Reset between tests — contextvars leak otherwise.
        _chain_id_var.set(None)
        _chain_op_var.set("auto")

    def test_get_chain_id_default_none(self):
        assert get_chain_id() is None

    def test_set_chain_id_persists(self):
        set_chain_id("chain-1")
        assert get_chain_id() == "chain-1"

    def test_chain_contextmanager_sets_and_resets(self):
        cid = str(uuid.uuid4())
        with chain(cid, op="start") as yielded:
            assert yielded == cid
            assert get_chain_id() == cid
            assert _chain_op_var.get() == "start"
        # Exit: contextvar reset to its pre-block value
        assert get_chain_id() is None

    def test_chain_contextmanager_rejects_invalid_op(self):
        with pytest.raises(ValueError, match="chain\\(\\) op must be"):
            with chain("cid", op="garbage"):
                pass

    def test_chain_nested_restores_outer_on_exit(self):
        with chain("outer", op="start"):
            with chain("inner", op="continue"):
                assert get_chain_id() == "inner"
            # Inner exited — outer restored.
            assert get_chain_id() == "outer"
        # Both exited.
        assert get_chain_id() is None


# ─────────────────────────────────────────────────────────────────────
# — time-based heartbeat scheduling
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.slow_sleep
class TestPingChainScheduler:
    """NullRunRuntime.ping_chain sends time-based heartbeats."""

    def test_ping_chain_emits_heartbeats_on_time_schedule(self):
        # The scheduler is a real background thread. We replace
        # the transport's heartbeat with a counter via
        # ``patch.object`` AND monkey-patch ``threading.Event.wait``
        # so each scheduler iteration takes ~50ms instead of the
        # real 10s interval — turns a 10s test into a sub-second one
        # without changing the production scheduler code.
        #
        # (coverage): this test depends on the real
        # wall clock to accumulate scheduler iterations within the
        # 500ms ``time.sleep`` window. ``@pytest.mark.slow_sleep``
        # on the enclosing class opts out of the conftest autouse
        # ``_fast_sleep`` cap so the scheduler thread sees a real
        # sleep.
        import threading as _threading

        from nullrun.runtime import NullRunRuntime

        rt = NullRunRuntime(api_key="nr_live_x", _test_mode=True, polling=False)
        try:
            call_count = {"n": 0}

            def fake_heartbeat(chain_id):
                call_count["n"] += 1
                return {"status": "ok", "chain_id": chain_id}

            real_wait = _threading.Event.wait

            def fast_wait(self, timeout=None):
                if timeout is not None:
                    return real_wait(self, timeout=0.05)
                return real_wait(self)

            with patch.object(rt._transport, "heartbeat", side_effect=fake_heartbeat), \
                 patch.object(_threading.Event, "wait", fast_wait):
                stop = rt.ping_chain("chain-1", interval=10.0)
                try:
                    # Several iterations of the 50ms-wait loop should
                    # accumulate POST calls within 500ms.
                    time.sleep(0.5)
                finally:
                    stop()

            assert call_count["n"] >= 1, (
                f"scheduler never invoked transport.heartbeat "
                f"(call_count={call_count['n']})"
            )
        finally:
            rt.shutdown()

    def test_ping_chain_rejects_out_of_range_interval(self):
        from nullrun.runtime import NullRunRuntime

        rt = NullRunRuntime(api_key="nr_live_x", _test_mode=True, polling=False)
        try:
            with pytest.raises(ValueError, match="\\[10, 120\\]"):
                rt.ping_chain("chain-1", interval=5.0)
            with pytest.raises(ValueError, match="\\[10, 120\\]"):
                rt.ping_chain("chain-1", interval=200.0)
        finally:
            rt.shutdown()

    @respx.mock
    def test_ping_chain_stop_is_idempotent(self):
        from nullrun.runtime import NullRunRuntime

        rt = NullRunRuntime(api_key="nr_live_x", _test_mode=True, polling=False)
        try:
            respx.post(f"{BASE_URL}/api/v1/heartbeat").mock(
                return_value=Response(200, json={"status": "ok"})
            )
            stop = rt.ping_chain("chain-1", interval=10.0)
            stop()
            stop()  # second call must be a no-op
            stop()  # third call must also be a no-op
        finally:
            rt.shutdown()


# ─────────────────────────────────────────────────────────────────────
# — ApproximateBudget is NEVER for enforcement
# ─────────────────────────────────────────────────────────────────────


class TestApproximateBudgetEndpoint:
    """The /budget/approximate endpoint is UI-only, never for enforcement."""

    @respx.mock
    def test_returns_503_on_data_unavailable(self):
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            respx.get(f"{BASE_URL}/api/v1/budget/approximate").mock(
                return_value=Response(
                    503,
                    json={"error_code": "BUDGET_DATA_UNAVAILABLE"},
                )
            )
            with pytest.raises(NullRunBackendError):
                t.approximate_budget(organization_id="org-1")
        finally:
            t.stop()

    @respx.mock
    def test_returns_parsed_payload_on_success(self):
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            respx.get(f"{BASE_URL}/api/v1/budget/approximate").mock(
                return_value=Response(
                    200,
                    json={
                        "current_spend_cents_estimate": 500,
                        "is_approximate": True,
                        "source": "PostgresOutbox",
                        "confidence": "Medium",
                        "last_updated_at": "2026-07-02T00:00:00Z",
                    },
                )
            )
            data = t.approximate_budget(organization_id="org-1")
            assert data["is_approximate"] is True
            assert data["current_spend_cents_estimate"] == 500
            assert data["confidence"] == "Medium"
        finally:
            t.stop()


# ─────────────────────────────────────────────────────────────────────
# — /cancel idempotency contract
# ─────────────────────────────────────────────────────────────────────


class TestCancelEndpoint:
    """Cancel must be idempotent; non-existent execution_id maps to backend error."""

    @respx.mock
    def test_cancel_sends_execution_id_in_body(self):
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            route = respx.post(f"{BASE_URL}/api/v1/cancel").mock(
                return_value=Response(
                    200, json={"status": "ok", "execution_id": "exec-1"}
                )
            )
            t.cancel("exec-1", reason="user_cancelled")
            sent = route.calls.last.request
            body = sent.content.decode("utf-8")
            assert '"execution_id":"exec-1"' in body
            assert '"reason":"user_cancelled"' in body
        finally:
            t.stop()

    @respx.mock
    def test_cancel_non_existent_raises_backend_error(self):
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            respx.post(f"{BASE_URL}/api/v1/cancel").mock(
                return_value=Response(
                    404, json={"error_code": "EXECUTION_NOT_FOUND"}
                )
            )
            with pytest.raises(NullRunBackendError):
                t.cancel("nonexistent-exec")
        finally:
            t.stop()


# ─────────────────────────────────────────────────────────────────────
# — /chain/end idempotency
# ─────────────────────────────────────────────────────────────────────


class TestChainEndEndpoint:
    """chain_end is idempotent — unknown chain_id is a no-op 200."""

    @respx.mock
    def test_chain_end_sends_chain_id_in_body(self):
        # 2026-07-04 (B3): chain_end targets /api/v1/gate
        # with chain_op=end. Verify both fields land on the wire.
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            route = respx.post(f"{BASE_URL}/api/v1/gate").mock(
                return_value=Response(200, json={"decision": "allow"})
            )
            t.chain_end("chain-1")
            sent = route.calls.last.request
            body = sent.content.decode("utf-8")
            assert '"chain_id":"chain-1"' in body
            assert '"chain_op":"end"' in body
        finally:
            t.stop()


# ─────────────────────────────────────────────────────────────────────
# — /gate execution_id is fresh uuidv7 per call (BUG #4 fix)
# ─────────────────────────────────────────────────────────────────────


class TestGateExecutionId:
    """/gate execution_id is a fresh uuid7 per call, NOT the workflow_id."""

    @respx.mock
    def test_two_consecutive_checks_have_distinct_execution_id(self):
        """Two consecutive /check calls produce DIFFERENT execution_id values, both != workflow_id."""
        import json as _json

        from nullrun.uuid7 import uuid7_str

        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            respx.post(f"{BASE_URL}/api/v1/gate").mock(
                return_value=Response(
                    200, json={"decision": "allow", "decision_source": "gateway"}
                )
            )
            # Mirror the payload shape that runtime.check_workflow_budget
            # constructs at runtime.py:1201-1208, with the BUG #4 fix:
            # execution_id is a fresh uuid7 per call, NOT workflow_id.
            workflow_id = "24fb55c5-9313-4fbd-8829-5ab93aa4396d"
            req1 = {
                "organization_id": "109c6ae0-a7cc-45b2-8ae6-0b5f8e84753d",
                "execution_id": uuid7_str(),
                "operation_id": str(uuid.uuid4()),
                "check_type": "llm",
                "model": "gpt-4.1-mini",
                "estimated_tokens": 1,
                "stream": False,
            }
            req2 = dict(req1)
            req2["operation_id"] = str(uuid.uuid4())
            req2["execution_id"] = uuid7_str()
            t.check(req1)
            first_body = _json.loads(respx.calls.last.request.content)
            t.check(req2)
            second_body = _json.loads(respx.calls.last.request.content)
            first_eid = first_body["execution_id"]
            second_eid = second_body["execution_id"]
            assert first_eid != second_eid
            assert first_eid != workflow_id
            assert second_eid != workflow_id
        finally:
            t.stop()

    @respx.mock
    def test_execution_id_is_uuidv7_format(self):
        """The execution_id must be a valid uuid7 (version nibble == 7)."""
        import json as _json

        from nullrun.uuid7 import uuid7_str

        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            respx.post(f"{BASE_URL}/api/v1/gate").mock(
                return_value=Response(
                    200, json={"decision": "allow", "decision_source": "gateway"}
                )
            )
            req = {
                "organization_id": "109c6ae0-a7cc-45b2-8ae6-0b5f8e84753d",
                "execution_id": uuid7_str(),
                "operation_id": str(uuid.uuid4()),
                "check_type": "llm",
                "model": "gpt-4.1-mini",
                "estimated_tokens": 1,
                "stream": False,
            }
            t.check(req)
            body = _json.loads(respx.calls.last.request.content)
            eid = body["execution_id"]
            parsed = uuid.UUID(eid)
            # UUID v7 has version nibble == 7 (RFC 9562)
            assert parsed.version == 7
        finally:
            t.stop()


# ─────────────────────────────────────────────────────────────────────
# BUG #5 — In-process gate cache for chain-mode
# ─────────────────────────────────────────────────────────────────────


class TestGateCache:
    """BUG #5 (2026-07-04): chain-mode /check calls should be served
    from an in-process 5s TTL cache, not hit /gate every time.
    Single-shot (Hard mode) callers MUST NOT cache.

    These tests pin the cache data-structure invariants + opt-out
    behavior. The runtime-level integration (10 chain-mode calls
    collapse to 1 HTTP roundtrip) is covered by an end-to-end smoke
    against the live API per docs/runbooks/budget-blue-green-smoke.sh
    Invariant 12. The runtime construction needed for in-process
    respx-mocked tests has its own env-bypass quirks; the data
    structure tests below are the durable contract."""

    def setup_method(self):
        from nullrun import runtime
        runtime._GATE_CACHE.clear()

    def test_cache_is_dict_with_ttl_5s(self):
        from nullrun import runtime
        assert isinstance(runtime._GATE_CACHE, dict)
        assert runtime._GATE_CACHE_TTL_SECONDS == 5.0

    def test_store_and_retrieve_within_ttl(self):
        import time as _time

        from nullrun import runtime
        k = ("wf-x", "chain-y", "model-z")
        runtime._GATE_CACHE[k] = (_time.monotonic(), {"decision": "allow"})
        cached = runtime._GATE_CACHE.get(k)
        assert cached is not None
        assert cached[1]["decision"] == "allow"

    def test_per_chain_cache_key_isolation(self):
        import time as _time

        from nullrun import runtime
        k1 = ("wf-x", "chain-A", "model-z")
        k2 = ("wf-x", "chain-B", "model-z")
        runtime._GATE_CACHE[k1] = (_time.monotonic(), {"decision": "allow"})
        runtime._GATE_CACHE[k2] = (_time.monotonic(), {"decision": "block"})
        assert runtime._GATE_CACHE.get(k1)[1]["decision"] == "allow"
        assert runtime._GATE_CACHE.get(k2)[1]["decision"] == "block"

    def test_cache_gate_disabled_when_no_chain_id(self):
        # Mirror the runtime's cache_enabled predicate:
        # chain_id is not None AND NULLRUN_GATE_CACHE_DISABLE != "1"
        import os
        os.environ["NULLRUN_GATE_CACHE_DISABLE"] = ""
        chain_id = None
        cache_enabled = (
            chain_id is not None
            and not os.environ.get("NULLRUN_GATE_CACHE_DISABLE", "").strip() == "1"
        )
        assert cache_enabled is False

    def test_cache_gate_disabled_via_env(self):
        import os
        os.environ["NULLRUN_GATE_CACHE_DISABLE"] = "1"
        chain_id = "chain-y"
        cache_enabled = (
            chain_id is not None
            and not os.environ.get("NULLRUN_GATE_CACHE_DISABLE", "").strip() == "1"
        )
        assert cache_enabled is False
        os.environ.pop("NULLRUN_GATE_CACHE_DISABLE", None)


# ─────────────────────────────────────────────────────────────────────
# BUG #5 — chain-mode gate cache at the runtime level
#`)
# ─────────────────────────────────────────────────────────────────────
#
# The TestGateCache data-structure tests above pin the runtime's
# `_GATE_CACHE` dict invariants in isolation; this class drives the
# full NullRunRuntime.check_workflow_budget path so the
# cache_enabled predicate + cache hit/miss branches in
# ``runtime.py:1287-1310`` are actually exercised end-to-end. Without
# these tests ``pytest-cov`` reports that exact range as uncovered
# which dragged patch coverage on PR #52 below the 70% Codecov floor.


class TestGateCacheRuntimeFlow:
    """Runtime-level chain-mode gate cache coverage.

    Drives ``NullRunRuntime.check_workflow_budget `` inside
    ``with workflow(...) + with chain(...)`` and verifies the
    /gate roundtrip count vs. expected after the 5s in-process
    cache is applied.
    """

    def setup_method(self):
        from nullrun import runtime as rt_mod

        rt_mod._GATE_CACHE.clear()

    def teardown_method(self):
        from nullrun import runtime as rt_mod

        rt_mod._GATE_CACHE.clear()
        # Always unset the gate-cache-disable opt-out so tests don't
        # leak state between runs.
        import os

        os.environ.pop("NULLRUN_GATE_CACHE_DISABLE", None)

    @respx.mock
    def test_chain_mode_collapses_three_checks_to_one_gate_call(self):
        """3 consecutive check_workflow_budget inside `with chain(...)`
        must hit /gate exactly ONCE — the 2nd and 3rd calls fall
        into the cache hit branch (runtime.py:1302).

        Covers:
          runtime.py:1291-1310 (cache_enabled predicate)
          runtime.py:1302 (cache hit `response = cached[1]`)
          runtime.py:1306 (cache miss → transport.check + store).
        """
        from nullrun.runtime import NullRunRuntime

        respx.post(f"{BASE_URL}/api/v1/gate").mock(
            return_value=Response(
                200,
                json={"decision": "allow", "decision_source": "gateway"},
            )
        )
        rt_inst = NullRunRuntime(
            api_key="nr_live_abc123",
            api_url=BASE_URL,
            _test_mode=True,  # skip _authenticate handshake
            polling=False,  # no background WS/HTTP poll thread
        )
        try:
            with workflow("wf-runtime-cache") as _wf_id, chain(
                "chain-runtime-cache"
            ) as _cid:
                # Direct calls in chain scope — bypasses @protect but
                # exercises the same check_workflow_budget codepath.
                rt_inst.check_workflow_budget()
                rt_inst.check_workflow_budget()
                rt_inst.check_workflow_budget()
            gate_calls = [
                c for c in respx.calls if c.request.url.path.endswith("/gate")
            ]
            assert len(gate_calls) == 1, (
                f"chain-mode cache must collapse 3 calls into 1 /gate "
                f"roundtrip; got {len(gate_calls)}"
            )
        finally:
            try:
                rt_inst.shutdown()
            except Exception:
                pass

    @respx.mock
    def test_chain_mode_emits_fresh_uuid7_execution_id_per_call(self):
        """BUG #4 wire at the runtime level: every /gate payload must
        carry a fresh execution_id == uuid7 (NOT workflow_id).

        Disables the chain-mode cache so both ``check_workflow_budget``
        calls actually POST a /gate body — the cache would otherwise
        collapse the second call into a hit and we'd never see the
        second payload.

        Covers:
          runtime.py:1247-1255 (execution_id = uuid7_str )
          runtime.py:1310-1323 (no-cache branch — direct transport.check).
        """
        import json as _json
        import os

        from nullrun.runtime import NullRunRuntime

        os.environ["NULLRUN_GATE_CACHE_DISABLE"] = "1"
        try:
            respx.post(f"{BASE_URL}/api/v1/gate").mock(
                return_value=Response(
                    200,
                    json={"decision": "allow", "decision_source": "gateway"},
                )
            )
            rt_inst = NullRunRuntime(
                api_key="nr_live_abc123",
                api_url=BASE_URL,
                _test_mode=True,
                polling=False,
            )
            try:
                with workflow("wf-runtime-uuid7"), chain("chain-runtime-uuid7"):
                    rt_inst.check_workflow_budget()
                    rt_inst.check_workflow_budget()
                gate_calls = [
                    c for c in respx.calls if c.request.url.path.endswith("/gate")
                ]
                assert len(gate_calls) == 2
                first = _json.loads(gate_calls[0].request.content)["execution_id"]
                second = _json.loads(gate_calls[1].request.content)["execution_id"]
                assert first != second
                assert uuid.UUID(first).version == 7
                assert uuid.UUID(second).version == 7
                assert first != "wf-runtime-uuid7"
                assert second != "wf-runtime-uuid7"
            finally:
                try:
                    rt_inst.shutdown()
                except Exception:
                    pass
        finally:
            os.environ.pop("NULLRUN_GATE_CACHE_DISABLE", None)

    @respx.mock
    def test_chain_mode_disabled_via_env_bypasses_cache(self):
        """NULLRUN_GATE_CACHE_DISABLE=1 → cache_enabled=False → every
        call hits /gate (runtime.py:1275-1277 fallback, runtime.py:1324
        direct transport.check path).

        Covers:
          runtime.py:1294-1295 (cache_enabled=False exit)
          runtime.py:1310-1323 (no-cache branch).
        """
        import os

        from nullrun.runtime import NullRunRuntime

        os.environ["NULLRUN_GATE_CACHE_DISABLE"] = "1"
        try:
            respx.post(f"{BASE_URL}/api/v1/gate").mock(
                return_value=Response(
                    200,
                    json={"decision": "allow", "decision_source": "gateway"},
                )
            )
            rt_inst = NullRunRuntime(
                api_key="nr_live_abc123",
                api_url=BASE_URL,
                _test_mode=True,
                polling=False,
            )
            try:
                with workflow("wf-no-cache"), chain("chain-no-cache"):
                    rt_inst.check_workflow_budget()
                    rt_inst.check_workflow_budget()
                gate_calls = [
                    c for c in respx.calls if c.request.url.path.endswith("/gate")
                ]
                assert len(gate_calls) == 2, (
                    f"with NULLRUN_GATE_CACHE_DISABLE=1 every call must "
                    f"hit /gate; got {len(gate_calls)}"
                )
            finally:
                try:
                    rt_inst.shutdown()
                except Exception:
                    pass
        finally:
            os.environ.pop("NULLRUN_GATE_CACHE_DISABLE", None)


# ─── server-minted execution_id ──────────────────────────────────
"""
Contract tests for the v3 server-minted execution_id wiring
.

Background
----------
Pre-0.12.0 the SDK read ``decision`` + ``decision_source`` from
the /check response and IGNORED ``reservation_id``, the
server-minted uuidv7 the backend's ``gate_reserve_v3`` writes
to ``reservation:{execution_id}`` (TTL 300s) and surfaces on
``GateResponse.reservation_id``. Without the round-trip:

  - /track had no way to find the matching reservation key →
    v3 ``consume_budget_v3`` rejected with 503
    ``RESERVATION_NOT_FOUND``.
  - /track kept using the legacy ``/api/v1/track/batch``
    path that writes to ``monthly_cost`` (drift with the
    dashboard's period counter, see G1).

0.12.0 fixes this by:

  1. Capturing ``response["reservation_id"]`` into a
     contextvar (``get_server_minted_execution_id``).
  2. Stamping the captured id onto every llm_call /track
     payload so v3 ``consume_budget_v3`` can find the
     reservation.
  3. Routing llm_call events to ``/api/v1/track`` (v3
     single-event) instead of ``/api/v1/track/batch``.

This file pins each step so a future refactor that breaks
propagation trips CI rather than silently re-introducing
the drift. Pattern follows
``tests/test_v3_wire_contract.py`` — same respx-based pattern
strict-URL assertions, no live backend required.
"""

import time
from unittest.mock import patch

import pytest
import respx
from httpx import Response

from nullrun.context import (
    _server_minted_execution_id_var,
    _server_minted_reservation_at_var,
    clear_server_minted_execution_id,
    get_server_minted_execution_id,
    get_server_minted_reservation_at,
    reset_server_minted_execution_id,
    reset_server_minted_reservation_at,
    set_server_minted_execution_id,
    set_server_minted_reservation_at,
)
from nullrun.runtime import (
    SERVER_MINTED_RESERVATION_MAX_AGE_SECONDS,
    NullRunRuntime,
    _build_v3_track_payload,
    _capture_server_minted_execution_id,
)

BASE_URL = "https://api.test.nullrun.io"

# A valid server-minted uuidv7 for tests. Layout matches the
# backend's mint_execution_id (RFC 9562 — version nibble
# in position 13 is `7`).
SERVER_MINTED_V1 = "0190c5b5-7c9a-7def-8a1b-0123456789ab"
SERVER_MINTED_V2 = "0190c5b5-7c9a-7def-8a1b-fedcba987654"


# ─────────────────────────────────────────────────────────────────
# Conftest-isolated state: every test gets a clean contextvar
# ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_server_minted_contextvar():
    """Forget any captured execution_id before AND after the test.

    Pairs with the ``reset_runtime`` autouse in conftest.py so
    contextvar state never leaks across test cases (test
    isolation — see memory ``test-isolation-monkeypatch-setattr``
    for the monkeypatched-setattr rationale).
    """
    clear_server_minted_execution_id()
    yield
    clear_server_minted_execution_id()


# ─────────────────────────────────────────────────────────────────
# 1. ContextVar: set/get/reset + timestamp pair (audit gap #2)
# ─────────────────────────────────────────────────────────────────

class TestServerMintedExecutionIdContextvar:
    """Token-based API for the server-minted execution_id contextvar.

    Mirrors the user-facing audit spec:
    ``set_server_minted_execution_id(value) -> Token``
    ``get_server_minted_execution_id -> str | None``
    ``reset_server_minted_execution_id(token) -> None``.
    """

    def test_default_value_is_none(self):
        # New ContextVar with no prior set → None. Verifies the SDK
        # doesn't ship with a stale id baked into the context.
        assert get_server_minted_execution_id() is None

    def test_set_returns_token_get_returns_value(self):
        token = set_server_minted_execution_id(SERVER_MINTED_V1)
        try:
            assert get_server_minted_execution_id() == SERVER_MINTED_V1
        finally:
            reset_server_minted_execution_id(token)

    def test_reset_restores_previous_value(self):
        # Layer one scope.
        outer_token = set_server_minted_execution_id(SERVER_MINTED_V1)
        try:
            assert get_server_minted_execution_id() == SERVER_MINTED_V1

            # Layer two scope — set a new value.
            inner_token = set_server_minted_execution_id(SERVER_MINTED_V2)
            try:
                assert get_server_minted_execution_id() == SERVER_MINTED_V2

                # Reset inner — restores outer (not None).
                reset_server_minted_execution_id(inner_token)
                assert get_server_minted_execution_id() == SERVER_MINTED_V1
            finally:
                # Already reset above; guard against re-running.
                if get_server_minted_execution_id() == SERVER_MINTED_V2:
                    reset_server_minted_execution_id(inner_token)
        finally:
            reset_server_minted_execution_id(outer_token)

        # Final: after outermost reset, back to None.
        assert get_server_minted_execution_id() is None

    def test_clear_drops_both_contextvars(self):
        token_e = set_server_minted_execution_id(SERVER_MINTED_V1)
        token_t = set_server_minted_reservation_at(123.456)
        try:
            assert get_server_minted_execution_id() == SERVER_MINTED_V1
            assert get_server_minted_reservation_at() == 123.456

            clear_server_minted_execution_id()

            # Both dropped to their defaults. No token-based
            # restore — this is the "block exited" cleanup path.
            assert get_server_minted_execution_id() is None
            assert get_server_minted_reservation_at() == 0.0
        finally:
            reset_server_minted_execution_id(token_e)
            reset_server_minted_reservation_at(token_t)

    def test_reservation_at_pairs_with_execution_id(self):
        # Captured at the same instant in real code so the two
        # values age in lockstep. Here we drive them separately
        # to verify the two contextvars are independent.
        t_e = set_server_minted_execution_id(SERVER_MINTED_V1)
        t_t = set_server_minted_reservation_at(time.monotonic())
        try:
            # Independent: setting one does NOT touch the other.
            new_e = set_server_minted_execution_id(SERVER_MINTED_V2)
            try:
                assert get_server_minted_execution_id() == SERVER_MINTED_V2
                # Timestamp from earlier set is still visible.
                assert get_server_minted_reservation_at() > 0
            finally:
                reset_server_minted_execution_id(new_e)
        finally:
            reset_server_minted_execution_id(t_e)
            reset_server_minted_reservation_at(t_t)


# ─────────────────────────────────────────────────────────────────
# 2. Capture helper (audit gap #1)
# ─────────────────────────────────────────────────────────────────

class TestCaptureServerMintedExecutionId:
    """``_capture_server_minted_execution_id(response)`` is the
    runtime-side shim that moves ``response["reservation_id"]``
    onto the contextvar. """

    def test_captures_valid_uuid_v7(self):
        out = _capture_server_minted_execution_id(
            {"reservation_id": SERVER_MINTED_V1}
        )
        assert out == SERVER_MINTED_V1
        assert get_server_minted_execution_id() == SERVER_MINTED_V1
        # Timestamp set to a positive monotonic — tests don't pin
        # exact value but verify it's >0 (means "captured").
        assert get_server_minted_reservation_at() > 0

    def test_clears_on_missing_field(self):
        # Pre-populate to verify clear actually clears.
        set_server_minted_execution_id(SERVER_MINTED_V1)

        result = _capture_server_minted_execution_id({"decision": "allow"})
        assert result is None
        assert get_server_minted_execution_id() is None

    def test_clears_on_none_field(self):
        # Backend sometimes returns `reservation_id: null` instead
        # of omitting the field — same outcome expected.
        set_server_minted_execution_id(SERVER_MINTED_V1)
        result = _capture_server_minted_execution_id(
            {"reservation_id": None}
        )
        assert result is None
        assert get_server_minted_execution_id() is None

    def test_drops_malformed_uuid_with_warning(self, caplog):
        import logging

        # Pre-seed so we can verify clear happens even on
        # malformed input.
        set_server_minted_execution_id(SERVER_MINTED_V1)

        with caplog.at_level(logging.WARNING, logger="nullrun.runtime"):
            result = _capture_server_minted_execution_id(
                {"reservation_id": "not-a-uuid"}
            )
        assert result is None
        assert get_server_minted_execution_id() is None
        assert any(
            "is not a valid UUID" in record.message
            for record in caplog.records
        )

    def test_tolerates_non_dict_response(self):
        # Defensive: a malformed transport could surface a
        # non-dict. Don't crash, just clear.
        result = _capture_server_minted_execution_id("not a dict")  # type: ignore[arg-type]
        assert result is None
        assert get_server_minted_execution_id() is None

    def test_drops_non_string_field(self):
        # Backend is the source of truth and only emits strings
        # but a buggy proxy could echo an int. Defensive parse.
        result = _capture_server_minted_execution_id(
            {"reservation_id": 123456}  # type: ignore[dict-item]
        )
        assert result is None
        assert get_server_minted_execution_id() is None


# ─────────────────────────────────────────────────────────────────
# 3. _enrich_event: include execution_id when fresh, drop when stale
# ─────────────────────────────────────────────────────────────────

class TestEnrichEventServerMinted:
    """``NullRunRuntime._enrich_event`` must stamp ``execution_id``
    onto the /track payload from the contextvar (audit gap #3)
    AND drop the field when the captured reservation has aged
    past the 300s TTL.
    """

    def test_includes_execution_id_when_fresh(self, make_runtime):
        rt = make_runtime()

        # Capture a fresh id (timestamp = now).
        _capture_server_minted_execution_id(
            {"reservation_id": SERVER_MINTED_V1}
        )

        enriched = rt._enrich_event(
            {"type": "llm_call", "workflow_id": "wf-1", "tokens": 10}
        )
        assert enriched["execution_id"] == SERVER_MINTED_V1

    def test_explicit_execution_id_wins_over_contextvar(
        self, make_runtime
    ):
        rt = make_runtime()

        _capture_server_minted_execution_id(
            {"reservation_id": SERVER_MINTED_V1}
        )

        enriched = rt._enrich_event(
            {
                "type": "tool_call",
                "workflow_id": "wf-1",
                "execution_id": "user-supplied-id",
            }
        )
        # Caller's value wins — contextvar is fallback only.
        assert enriched["execution_id"] == "user-supplied-id"

    def test_drops_execution_id_when_age_exceeds_threshold(
        self, make_runtime
    ):
        rt = make_runtime()

        # Force the timestamp to ancient history.
        token = set_server_minted_execution_id(SERVER_MINTED_V1)
        stale_at = time.monotonic() - (
            SERVER_MINTED_RESERVATION_MAX_AGE_SECONDS + 10.0
        )
        t_at = set_server_minted_reservation_at(stale_at)
        try:
            enriched = rt._enrich_event(
                {"type": "llm_call", "workflow_id": "wf-1", "tokens": 10}
            )
            # Stale → field dropped, contextvar cleared.
            assert "execution_id" not in enriched
            assert get_server_minted_execution_id() is None
        finally:
            reset_server_minted_execution_id(token)
            reset_server_minted_reservation_at(t_at)

    def test_keeps_execution_id_when_age_just_under_threshold(
        self, make_runtime
    ):
        # Boundary: 1 second before the safety cutoff — still
        # considered fresh.
        rt = make_runtime()
        token = set_server_minted_execution_id(SERVER_MINTED_V1)
        t_at = set_server_minted_reservation_at(
            time.monotonic()
            - (SERVER_MINTED_RESERVATION_MAX_AGE_SECONDS - 1.0)
        )
        try:
            enriched = rt._enrich_event(
                {"type": "llm_call", "workflow_id": "wf-1", "tokens": 10}
            )
            assert enriched["execution_id"] == SERVER_MINTED_V1
        finally:
            reset_server_minted_execution_id(token)
            reset_server_minted_reservation_at(t_at)

    def test_no_execution_id_when_capture_empty(self, make_runtime):
        # No capture in scope → no execution_id field.
        rt = make_runtime()
        enriched = rt._enrich_event(
            {"type": "llm_call", "workflow_id": "wf-1", "tokens": 10}
        )
        assert "execution_id" not in enriched


# ─────────────────────────────────────────────────────────────────
# 4. _build_v3_track_payload: shape the v3 single-event body
# ─────────────────────────────────────────────────────────────────

class TestBuildV3TrackPayload:
    """Map an enriched event onto the ``/api/v1/track`` schema."""

    def test_full_event_builds_full_payload(self):
        out = _build_v3_track_payload(
            {
                "type": "llm_call",
                "workflow_id": "wf-1",
                "tokens": 100,
                "input_tokens": 60,
                "output_tokens": 40,
                "model": "claude-sonnet-4-6",
                "latency_ms": 250,
                "metadata": {"x": "y"},
                "trace_id": "trace-1",
                "span_id": "span-1",
                "agent_id": "agent-1",
            },
            SERVER_MINTED_V1,
        )
        assert out == {
            "reservation_id": SERVER_MINTED_V1,
            "workflow_id": "wf-1",
            "tokens": 100,
            "input_tokens": 60,
            "output_tokens": 40,
            "model": "claude-sonnet-4-6",
            "latency_ms": 250,
            "metadata": {"x": "y"},
            "trace_id": "trace-1",
            "span_id": "span-1",
            "agent_id": "agent-1",
            "cost_cents": 0,
            "cost_source": "provisional",
        }

    def test_minimal_event_only_required_fields(self):
        # workflow_id + tokens + reservation_id are the floor.
        out = _build_v3_track_payload(
            {"type": "llm_call", "workflow_id": "wf-1", "tokens": 1},
            SERVER_MINTED_V1,
        )
        assert out == {
            "reservation_id": SERVER_MINTED_V1,
            "workflow_id": "wf-1",
            "tokens": 1,
            "cost_cents": 0,
            "cost_source": "provisional",
        }

    def test_missing_workflow_id_returns_none(self):
        # Caller falls back to /track/batch.
        out = _build_v3_track_payload(
            {"type": "llm_call", "tokens": 1},
            SERVER_MINTED_V1,
        )
        assert out is None

    def test_missing_tokens_returns_none(self):
        out = _build_v3_track_payload(
            {"type": "llm_call", "workflow_id": "wf-1"},
            SERVER_MINTED_V1,
        )
        assert out is None

    def test_tokens_coerced_to_int(self):
        # Defensive: SDK usually emits int but a user-supplied
        # token via the dict could be a numpy.int64 in a
        # cookbook scenario. Force int so wire is int.
        out = _build_v3_track_payload(
            {"type": "llm_call", "workflow_id": "wf-1", "tokens": "100"},
            SERVER_MINTED_V1,
        )
        assert out is not None
        assert out["tokens"] == 100
        assert isinstance(out["tokens"], int)


# ─────────────────────────────────────────────────────────────────
# 5. _route_track: routes llm_call → /track, others → /track/batch
# ─────────────────────────────────────────────────────────────────

class TestRouteTrack:
    """``NullRunRuntime._route_track(wire_event)`` decides between
    the v3 single-event endpoint (``/api/v1/track``) and the
    legacy batch endpoint (``/api/v1/track/batch``).
    """

    @respx.mock
    def test_llm_call_with_smid_routes_to_single(self, make_runtime):
        rt = make_runtime()

        # Set up both endpoints with respx — only one should fire.
        single_route = respx.post(f"{BASE_URL}/api/v1/track").mock(
            return_value=Response(200, json={"status": "ok"})
        )
        batch_route = respx.post(f"{BASE_URL}/api/v1/track/batch").mock(
            return_value=Response(200, json={"ok": True, "accepted": 1})
        )

        # Capture a server-minted id.
        _capture_server_minted_execution_id(
            {"reservation_id": SERVER_MINTED_V1}
        )

        # Drive through track_llm so the enrich path runs.
        rt.track_llm(
            input_tokens=60,
            output_tokens=40,
            model="claude-sonnet-4-6",
        )

        assert single_route.call_count == 1
        assert batch_route.call_count == 0

        # Wire shape — body contains the captured reservation_id.
        sent = single_route.calls.last.request
        import json as _json
        body = _json.loads(sent.content)
        assert body["reservation_id"] == SERVER_MINTED_V1
        assert body["tokens"] == 100
        assert body["cost_source"] == "provisional"

    @respx.mock
    def test_tool_call_routes_to_batch(self, make_runtime):
        rt = make_runtime()

        single_route = respx.post(f"{BASE_URL}/api/v1/track").mock(
            return_value=Response(200, json={"status": "ok"})
        )
        batch_route = respx.post(f"{BASE_URL}/api/v1/track/batch").mock(
            return_value=Response(200, json={"ok": True, "accepted": 1})
        )

        # Capture anyway — even WITH smid in scope, non-llm_call
        # events still go to the batch endpoint (no reservation
        # to release).
        _capture_server_minted_execution_id(
            {"reservation_id": SERVER_MINTED_V1}
        )

        rt.track_tool(
            tool_name="bash",
            duration_ms=50,
        )

        # track buffers; tool_call events don't trip the v3
        # path because they have no reservation to release. Force
        # the batch flush so respx sees the call.
        rt._transport.flush_now()

        assert single_route.call_count == 0
        assert batch_route.call_count == 1

    @respx.mock
    def test_llm_call_without_smid_falls_back_to_batch(self, make_runtime):
        # No /check in scope → no smid → legacy path.
        rt = make_runtime()

        single_route = respx.post(f"{BASE_URL}/api/v1/track").mock(
            return_value=Response(200, json={"status": "ok"})
        )
        batch_route = respx.post(f"{BASE_URL}/api/v1/track/batch").mock(
            return_value=Response(200, json={"ok": True, "accepted": 1})
        )

        # No capture call here — contextvar stays empty.

        rt.track_llm(
            input_tokens=10,
            output_tokens=5,
            model="claude-sonnet-4-6",
        )
        # Buffer + flush.
        rt._transport.flush_now()

        assert single_route.call_count == 0
        assert batch_route.call_count == 1

    @respx.mock
    def test_v3_track_disable_env_forces_legacy(self, make_runtime, monkeypatch):
        # Env flag opt-out — even WITH smid, force batch.
        monkeypatch.setenv("NULLRUN_V3_TRACK_DISABLE", "1")

        rt = make_runtime()

        single_route = respx.post(f"{BASE_URL}/api/v1/track").mock(
            return_value=Response(200, json={"status": "ok"})
        )
        batch_route = respx.post(f"{BASE_URL}/api/v1/track/batch").mock(
            return_value=Response(200, json={"ok": True, "accepted": 1})
        )

        _capture_server_minted_execution_id(
            {"reservation_id": SERVER_MINTED_V1}
        )

        rt.track_llm(input_tokens=1, output_tokens=1, model="x")
        rt._transport.flush_now()

        assert single_route.call_count == 0
        assert batch_route.call_count == 1


# ─────────────────────────────────────────────────────────────────
# 6. End-to-end: capture from /gate response flows to /track
# ─────────────────────────────────────────────────────────────────

class TestEndToEndCaptureFlow:
    """The two halves of the v3 wire-up must cooperate.

    ``check_workflow_budget`` captures the ``reservation_id``
    from the /gate response. ``track_llm`` (via
    ``_route_track``) reads the captured id and ships it on
    /track. These tests pin the round trip so any refactor
    that breaks the connection is caught at CI time.
    """

    @respx.mock
    def test_reservation_id_from_gate_lands_on_track(self, make_runtime):
        rt = make_runtime()

        # /gate returns reservation_id (server-minted uuidv7).
        respx.post(f"{BASE_URL}/api/v1/gate").mock(
            return_value=Response(
                200,
                json={
                    "decision": "allow",
                    "decision_source": "gateway",
                    "reservation_id": SERVER_MINTED_V1,
                },
            )
        )

        # /track (single) — what the v3 routing should hit.
        single_route = respx.post(f"{BASE_URL}/api/v1/track").mock(
            return_value=Response(200, json={"status": "ok"})
        )

        # Drive /gate (which captures)...
        from nullrun.context import workflow
        with workflow("wf-1"):
            rt.check_workflow_budget()

            #... then drive /track within the same scope.
            rt.track_llm(
                input_tokens=10,
                output_tokens=5,
                model="claude-sonnet-4-6",
            )

        assert single_route.call_count == 1
        import json as _json
        body = _json.loads(single_route.calls.last.request.content)
        assert body["reservation_id"] == SERVER_MINTED_V1

    @respx.mock
    def test_block_response_does_not_infect_subsequent_track(
        self, make_runtime
    ):
        # /gate returns "block" with NO reservation_id. The
        # capture helper should clear any prior capture so the
        # next /track is a legacy batch event (no reservation).
        rt = make_runtime()

        respx.post(f"{BASE_URL}/api/v1/gate").mock(
            return_value=Response(
                200,
                json={
                    "decision": "block",
                    "decision_source": "gateway",
                    "explanation": "budget exhausted",
                    # NO reservation_id — backend does NOT mint
                    # on a hard block (the request didn't
                    # proceed past the gate).
                },
            )
        )

        single_route = respx.post(f"{BASE_URL}/api/v1/track").mock(
            return_value=Response(200, json={"status": "ok"})
        )
        batch_route = respx.post(f"{BASE_URL}/api/v1/track/batch").mock(
            return_value=Response(200, json={"ok": True, "accepted": 1})
        )

        from nullrun.breaker.exceptions import WorkflowKilledInterrupt
        from nullrun.context import workflow
        with workflow("wf-1"):
            # Block path raises — WorkflowKilledInterrupt is a
            # BaseException (carries the kill signal
            # must propagate honestly). Catch it explicitly for
            # this test which only wants to verify contextvar hygiene.
            try:
                rt.check_workflow_budget()
            except WorkflowKilledInterrupt:
                pass

            rt.track_llm(
                input_tokens=1,
                output_tokens=1,
                model="x",
            )
            rt._transport.flush_now()

        # No reservation_id was minted → falls back to batch.
        assert single_route.call_count == 0
        assert batch_route.call_count == 1


# ─── v3.38 wire-drift fixes ─────────────────────────────────
"""Regression tests for the v3.38 wire-drift fixes (2026-08-07).

These pin three contract-level fixes that were verified against
backend source code, not against comments or documentation:

* **capabilities probe route** — the SDK was probing
  ``/health`` (a generic liveness payload) instead of the
  canonical ``/api/v1/capabilities`` route. Pre-fix, every
  ``is_v3_ready()`` returned False because the probe never saw
  a v3 capability payload, leaving every flag a runtime no-op.

* **API_KEY_* error code granularity (v3.38)** — the backend
  split the v3.36 ``API_KEY_REVOKED`` bucket into five distinct
  wire codes (``API_KEY_EXPIRED`` / ``API_KEY_DISABLED`` /
  ``API_KEY_INVALID`` / ``API_KEY_MISSING`` /
  ``API_KEY_MALFORMED``) so SDKs can branch on each lifecycle
  state. Pre-fix, only ``API_KEY_REVOKED`` was mapped in
  ``_V3_ERROR_CODE_MAP`` — the other five silently fell through
  to the generic HTTP-status fallback (``NullRunAuthentication
  Error``) without ever becoming ``NullRunAuthError``, losing
  the diagnostic class. Wire codes are now surfaced on
  ``NullRunAuthError.wire_code``.

* **decision == "soft_pass" handling** — the backend returns
  ``soft_pass`` for soft-mode calls that proceed via the chain's
  overdraft cap (CLAUDE.md §5). Pre-fix, the runtime's
  ``check_workflow_budget`` had no branch for ``soft_pass`` —
  the ``decision == "allow"`` default fall-through meant the
  body proceeded (correct) but the operator saw no log line
  and no overdraft counter incremented (silent budget drift).

The tests pin the fixed behaviour so a future refactor that
breaks any of these three contracts gets caught in CI rather
than at first production /check.
"""

from pathlib import Path

import httpx
import pytest
import respx

from nullrun.breaker import exceptions as exc
from nullrun.capabilities import (
    CAPABILITIES_PATH,
    probe_capabilities,
)
from nullrun.transport import _V3_ERROR_CODE_MAP, _parse_v3_error_envelope

BASE_URL = "https://api.test.nullrun.io"

_RUNTIME_SRC_PATH = (
    Path(__file__).parent.parent / "src" / "nullrun" / "runtime.py"
)


# ---------------------------------------------------------------------------
# Fix #1 — capabilities probe route (/api/v1/capabilities, not /health)
# ---------------------------------------------------------------------------


def test_capabilities_path_constant_is_canonical_route():
    """``CAPABILITIES_PATH`` must point at ``/api/v1/capabilities``.

    The constant is the single source of truth — every
    ``probe_capabilities`` call builds ``{api_url}{CAPABILITIES_PATH}``
    (capabilities.py:290). Pinning the constant here catches a
    refactor that re-introduces the legacy ``/health`` route.
    """
    assert CAPABILITIES_PATH == "/api/v1/capabilities"


def test_probe_capabilities_against_canonical_route_with_v3_payload():
    """A v3 backend responding at /api/v1/capabilities with the
    nested ``capabilities:`` payload yields ``is_v3_ready() == True``.

    Pins the entire probe → parse → flag chain against the canonical
    route. Pre-fix the SDK probed /health and never saw this payload,
    so ``is_v3_ready()`` was always False.
    """
    payload = {
        "min_protocol_version": 3,
        "max_protocol_version": 3,
        "protocol_version": 3,
        "capabilities": {
            "server_minted_execution_id": True,
            "per_execution_reservations": True,
            "enforcement_modes_soft": True,
            "heartbeat_time_based": True,
        },
    }
    with respx.mock:
        respx.get(f"{BASE_URL}/api/v1/capabilities").mock(
            return_value=httpx.Response(200, json=payload)
        )
        # Negative pin — a stale /health mock returning 200 must
        # NOT satisfy the probe. This catches regressions where
        # someone re-adds /health as a fallback.
        respx.get(f"{BASE_URL}/health").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        parsed = probe_capabilities(BASE_URL)
    assert parsed is not None
    assert parsed.is_v3_ready()
    assert parsed.server_minted_execution_id is True
    assert parsed.per_execution_reservations is True
    assert parsed.heartbeat_time_based is True


# ---------------------------------------------------------------------------
# Fix #2 — v3.38 API_KEY_* codes in _V3_ERROR_CODE_MAP + wire_code attr
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wire_code",
    [
        "API_KEY_REVOKED",
        "API_KEY_EXPIRED",
        "API_KEY_DISABLED",
        "API_KEY_INVALID",
        "API_KEY_MISSING",
        "API_KEY_MALFORMED",
    ],
)
def test_v3_error_code_map_covers_all_api_key_states(wire_code):
    """All six v3.38 API_KEY_* wire codes must map to NullRunAuthError.

    Pre-fix the map only covered ``API_KEY_REVOKED`` — the other
    five silently fell through to the generic HTTP-status fallback
    (line ~2616 in transport.py), losing the diagnostic class.
    Pinning the map catches a refactor that drops any of the five
    new entries.
    """
    assert wire_code in _V3_ERROR_CODE_MAP
    assert _V3_ERROR_CODE_MAP[wire_code] is exc.NullRunAuthError


def test_parse_v3_error_envelope_surfaces_wire_code_on_auth_error():
    """A 401 with error_code=API_KEY_EXPIRED yields NullRunAuthError
    whose ``wire_code`` attribute exposes the granular backend code.

    Without ``wire_code``, callers have only the SDK-side NR-A003
    taxonomy and lose the granular lifecycle signal. Mirrors
    NullRunChainError.backend_code pattern (exceptions.py:448).
    """
    response = httpx.Response(
        401,
        json={
            "error_code": "API_KEY_EXPIRED",
            "error_message": "key TTL elapsed",
            "details": {"expires_at": "2026-08-01T00:00:00Z"},
        },
    )
    err = _parse_v3_error_envelope(response, "gate")
    assert isinstance(err, exc.NullRunAuthError)
    # SDK-side taxonomy preserved (NR-A003) — the fix adds wire_code
    # instead of clobbering error_code.
    assert err.error_code == "NR-A003"
    # Granular wire code surfaced for handler dispatch.
    assert err.wire_code == "API_KEY_EXPIRED"


def test_parse_v3_error_envelope_preserves_default_wire_code_for_revoked():
    """API_KEY_REVOKED continues to work — wire_code defaults to it
    when the constructor is called without an explicit value (e.g.
    a future refactor that bypasses the catalog dispatch).
    """
    err = exc.NullRunAuthError("revoked")
    assert err.wire_code == "API_KEY_REVOKED"
    assert err.error_code == "NR-A003"


def test_parse_v3_error_envelope_auth_error_does_not_clobber_unrelated_details():
    """The fix to filter ``details`` to known kwargs must not lose
    extras silently — unknown keys (e.g. ``expires_at``) must land
    on ``self.details`` for caller introspection. Pre-fix the
    envelope parser forwarded every detail as a kwarg, which threw
    TypeError on the first unknown key (e.g. when the backend
    started emitting ``expires_at`` for v3.38 EXPIRED responses).
    """
    response = httpx.Response(
        401,
        json={
            "error_code": "API_KEY_DISABLED",
            "error_message": "admin disabled this key",
            "details": {
                "disabled_at": "2026-08-01T00:00:00Z",
                "disabled_by": "admin@nullrun.io",
            },
        },
    )
    err = _parse_v3_error_envelope(response, "gate")
    assert isinstance(err, exc.NullRunAuthError)
    assert err.wire_code == "API_KEY_DISABLED"
    # The disabled_at / disabled_by fields land on self.details
    # (not lost, not raised).
    details = getattr(err, "details", {}) or {}
    assert details.get("disabled_at") == "2026-08-01T00:00:00Z"
    assert details.get("disabled_by") == "admin@nullrun.io"


# ---------------------------------------------------------------------------
# Fix #3 — decision == "soft_pass" handling in check_workflow_budget
# ---------------------------------------------------------------------------
#
# ``check_workflow_budget(self) -> None`` builds its own ``check_req``
# dict and fetches via ``self._transport.check()`` — the signature
# has no way to inject a response fixture without a full transport
# mock. The soft_pass branch is a pure decision switch (runtime.py
# ~1799-1830) so a source-level scan is the most reliable pin,
# matching the migration_drift_tests pattern used elsewhere in the
# SDK and backend.


def test_check_workflow_budget_handles_soft_pass_decision():
    """``check_workflow_budget`` must contain a ``decision ==
    "soft_pass"`` branch.

    Pre-fix, ``soft_pass`` fell through the ``decision == "allow"``
    default — body executed (correct) but no log line, no counter.
    Operators had zero visibility into "budget soft cap is biting".

    Static scan pins the runtime.py structure so a future refactor
    that drops the branch gets caught in CI rather than at first
    production /check.
    """
    runtime_src = _RUNTIME_SRC_PATH.read_text(encoding="utf-8")

    assert 'decision == "soft_pass"' in runtime_src, (
        "check_workflow_budget must branch on `decision == \"soft_pass\"`. "
        "Pre-fix the branch was missing — soft_pass fell through the "
        "default allow path and operators got no overdraft telemetry."
    )


def test_check_workflow_budget_soft_pass_branch_increments_overdraft_counter():
    """The soft_pass branch must increment ``soft_overdraft_used``
    so operators can graph soft-cap pressure in the dashboard —
    silent budget drift is the regression we are preventing.
    """
    runtime_src = _RUNTIME_SRC_PATH.read_text(encoding="utf-8")

    # Slice the soft_pass branch out of the file by anchoring on
    # the literal and the next known decision branch. The slice
    # must contain the counter increment.
    soft_pass_idx = runtime_src.find('decision == "soft_pass"')
    assert soft_pass_idx >= 0, "soft_pass branch not found"
    require_approval_idx = runtime_src.find(
        'decision == "require_approval"', soft_pass_idx
    )
    assert require_approval_idx >= 0, (
        "decision == require_approval marker not found after soft_pass — "
        "the runtime source structure has drifted from this pin's anchor."
    )
    branch_slice = runtime_src[soft_pass_idx:require_approval_idx]

    assert "soft_overdraft_used" in branch_slice, (
        "soft_pass branch must increment `soft_overdraft_used` so the "
        "dashboard can graph soft-cap pressure."
    )
    assert "metrics.inc_runtime" in branch_slice, (
        "soft_pass branch must call `metrics.inc_runtime(...)` to record "
        "the counter."
    )


def test_check_workflow_budget_soft_pass_branch_logs_overdraft_telemetry():
    """The soft_pass branch must log at WARNING level with the
    backend's ``overdraft_used_cents`` value — that's the operator's
    primary signal that the chain's overdraft cap is burning.
    """
    runtime_src = _RUNTIME_SRC_PATH.read_text(encoding="utf-8")

    soft_pass_idx = runtime_src.find('decision == "soft_pass"')
    require_approval_idx = runtime_src.find(
        'decision == "require_approval"', soft_pass_idx
    )
    branch_slice = runtime_src[soft_pass_idx:require_approval_idx]

    assert "overdraft_used_cents" in branch_slice, (
        "soft_pass branch must surface `overdraft_used_cents` from the "
        "backend response — silent loss of this value means operators "
        "have no visibility into which chains are burning overdraft."
    )
    assert "logger.warning" in branch_slice, (
        "soft_pass branch must log at WARNING level — overdraft pressure "
        "is operator-actionable, not informational."
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])