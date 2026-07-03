"""
Contract tests pinning the v3 wire format (CLAUDE.md v3.4 alignment).

Background: 0.11.0 added six new endpoints (/check, /track,
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
)
from nullrun.transport import (
    HEADER_PROTOCOL,
    NULLRUN_PROTOCOL_VERSION,
    Transport,
    _parse_v3_error_envelope,
    _V3_ERROR_CODE_MAP,
)


BASE_URL = "https://api.test.nullrun.io"


# ─────────────────────────────────────────────────────────────────────
# FIX §32: every signed POST must carry X-NULLRUN-PROTOCOL: <current>
# ─────────────────────────────────────────────────────────────────────
#
# Without this header the backend's protocol middleware rejects with
# HTTP 400 + error_code PROTOCOL_HEADER_REQUIRED BEFORE the gate
# pipeline runs. Centralising the value in
# ``nullrun.transport._protocol_header_value()`` means a future
# bump is a one-line change.


class TestProtocolHeaderConstant:
    """The wire-protocol version constant + helper stay in sync."""

    def test_version_is_three(self):
        # Bumping this requires a coordinated backend release —
        # see CLAUDE.md §32 (semver: major = breaking wire change).
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
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            route = respx.post(f"{BASE_URL}/api/v1/check").mock(
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
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            route = respx.post(f"{BASE_URL}/api/v1/track").mock(
                return_value=Response(200, json={"status": "ok"})
            )
            t.track_single({"execution_id": "exec-1", "actual_cost_cents": 5})
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
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            route = respx.post(f"{BASE_URL}/api/v1/chain/end").mock(
                return_value=Response(200, json={"status": "ok"})
            )
            t.chain_end("chain-abc")
            sent = route.calls.last.request
            assert sent.headers["X-NULLRUN-PROTOCOL"] == "3"
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
# §16 — chain_id / chain_op / idempotency_key / stream forwarding on
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
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            route = respx.post(f"{BASE_URL}/api/v1/check").mock(
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
# §13 — v3 error envelope → typed exception mapping
# ─────────────────────────────────────────────────────────────────────
#
# The backend returns errors as a JSON envelope of the shape
# ``{"error_code": "BUDGET_HARD_BLOCKED", "error_message": "...",
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
        # CLAUDE.md §4: REDIS_UNAVAILABLE is fail-CLOSED → 402
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
        # CLAUDE.md §4: fail-CLOSED for aggregate rate limit
        resp = self._make_response(
            503,
            {"error_code": "RATE_LIMIT_REDIS_UNAVAILABLE", "error_message": "redis down"},
        )
        exc = _parse_v3_error_envelope(resp, "check")
        assert isinstance(exc, NullRunRateLimitRedisError)

    def test_budget_data_unavailable_maps_to_backend_error(self):
        # CLAUDE.md §17: dashboard must show "Data unavailable", not "$0"
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
        # Server-side convention: header is canonical (RFC 7231),
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
    """Every backend code listed in CLAUDE.md §13 has a mapping entry."""

    def test_catalog_covers_all_documented_codes(self):
        # Frozen catalog: every backend code documented in CLAUDE.md
        # §13 must have a mapping entry. If you add a new code on
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
# §6 — chain context helpers (contextmanager, getters, setters)
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
# §26 — time-based heartbeat scheduling
# ─────────────────────────────────────────────────────────────────────


class TestPingChainScheduler:
    """NullRunRuntime.ping_chain — time-based heartbeat (CLAUDE.md §26)."""

    def test_ping_chain_emits_heartbeats_on_time_schedule(self):
        # The scheduler is a real background thread. We replace
        # the transport's heartbeat() with a counter via
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
# §17 — ApproximateBudget is NEVER for enforcement
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
# §23 — /cancel idempotency contract
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
# §6 — /chain/end idempotency
# ─────────────────────────────────────────────────────────────────────


class TestChainEndEndpoint:
    """chain_end is idempotent — unknown chain_id is a no-op 200."""

    @respx.mock
    def test_chain_end_sends_chain_id_in_body(self):
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            route = respx.post(f"{BASE_URL}/api/v1/chain/end").mock(
                return_value=Response(200, json={"status": "ok"})
            )
            t.chain_end("chain-1")
            sent = route.calls.last.request
            body = sent.content.decode("utf-8")
            assert '"chain_id":"chain-1"' in body
        finally:
            t.stop()