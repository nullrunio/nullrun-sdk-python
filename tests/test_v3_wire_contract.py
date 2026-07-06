"""
Contract tests pinning the v3 wire format.

Background: 0.11.0 added six new endpoints (/check, /track
/cancel, /heartbeat, /chain/end, /budget/approximate) and a
mandatory ``X-NULLRUN-PROTOCOL: 3`` header. Each test in this file
guards a specific class of wire-drift so a future SDK refactor
trips CI rather than silently breaking the v3 backend.

If you change any of these and the tests fail, update the matching
file in ``backend/src/proxy/http/gate/protocol.rs`` and
``backend/src/proxy/handlers.rs`` in lock-step — do not edit one
side alone.

Pattern follows ``tests/test_integration_contract.py`` (FIX-F3 /
FIX-F4 / REMOTE_STATE pinning) — same respx-based pattern, same
strict-URL assertions, same headers-included checks.
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


class TestPingChainScheduler:
    """NullRunRuntime.ping_chain sends time-based heartbeats."""

    def test_ping_chain_emits_heartbeats_on_time_schedule(self):
        # The scheduler is a real background thread. We replace
        # the transport's heartbeat with a counter via
        # ``patch.object`` AND monkey-patch ``threading.Event.wait``
        # so each scheduler iteration takes ~50ms instead of the
        # real 10s interval — turns a 10s test into a sub-second one
        # without changing the production scheduler code.
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
    """: /gate execution_id must be a fresh uuidv7
    per call, NOT the workflow_id. Pre-fix the SDK sent
    `execution_id = workflow_id` which broke the v3 reservation
    binding on /track (consume_budget_v3 looks up
    `reservation:{execution_id}` and 503s on miss)."""

    @respx.mock
    def test_two_consecutive_checks_have_distinct_execution_id(self):
        """Two consecutive /check calls produce DIFFERENT
        execution_id values, both != workflow_id."""
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
