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
    NullRunBudgetRecheckFailedError,
    NullRunChainError,
    NullRunConsumeOverbudgetError,
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
        assert NULLRUN_PROTOCOL_VERSION == 4  # v4 = Slice B additive (action_digest, policy_hash on /gate wire)

    def test_header_name_is_dashed(self):
        # Match the backend's HeaderName parsing (axum 0.7 normalises
        # to lowercase; the wire value is the canonical
        # case-sensitive form per the v3 spec).
        assert HEADER_PROTOCOL == "X-NULLRUN-PROTOCOL"

    def test_protocol_header_value_helper(self):
        from nullrun.transport import _protocol_header_value

        # Stored as u32 on the wire — serialise the integer directly
        # (``"4"`` after the Slice B bump, not ``"v3"``). The value
        # is sourced from `NULLRUN_PROTOCOL_VERSION` as the single
        # source of truth so future bumps don't sweep this test.
        assert _protocol_header_value() == str(NULLRUN_PROTOCOL_VERSION)


class TestSignedPostIncludesProtocolHeader:
    """Every signed POST must include ``X-NULLRUN-PROTOCOL: <NULLRUN_PROTOCOL_VERSION>``.

    v4 (2026-08-31, ADR-037 Slice B): protocol bumped 3→4 additively.
    The class name is kept (``TestSignedPostIncludesProtocolHeader``)
    for git-blame continuity; the header value is sourced from
    ``NULLRUN_PROTOCOL_VERSION`` as the single source of truth so
    future bumps don't require sweeping this file again.
    """

    @respx.mock
    def test_track_batch_includes_protocol_header(self):
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            route = respx.post(f"{BASE_URL}/api/v1/track/batch").mock(
                return_value=Response(200, json={"ok": True, "accepted": 1})
            )
            t._send_batch_with_retry_info([{"event": "test"}])
            sent = route.calls.last.request
            assert sent.headers["X-NULLRUN-PROTOCOL"] == str(NULLRUN_PROTOCOL_VERSION)
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
            assert sent.headers["X-NULLRUN-PROTOCOL"] == str(NULLRUN_PROTOCOL_VERSION)
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
            assert sent.headers["X-NULLRUN-PROTOCOL"] == str(NULLRUN_PROTOCOL_VERSION)
        finally:
            t.stop()

    @respx.mock
    def test_track_single_includes_protocol_header(self):
        # 2026-07-04 (B2): body shape matches the v3 wire
        # contract — ``reservation_id`` (server-minted from /check)
        # ``workflow_id`` + ``tokens`` +
        # ``cost_source: "provisional"``. Pre-fix this test sent the
        # legacy / fictitious shape
        # ``{execution_id, actual_cost_cents}`` which doesn't match
        # ``TrackRequestRaw`` and would 422 on the wire.
        # ADR-068 §4b (2026-10-09): the SDK used to emit
        # ``cost_cents: 0`` here. It is stripped on the way out
        # (``_WIRE_STRIP_FIELDS``), so passing it below is still fine —
        # it exercises that an older caller's price does not survive.
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
            assert sent.headers["X-NULLRUN-PROTOCOL"] == str(NULLRUN_PROTOCOL_VERSION)
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
            assert sent.headers["X-NULLRUN-PROTOCOL"] == str(NULLRUN_PROTOCOL_VERSION)
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
            assert sent.headers["X-NULLRUN-PROTOCOL"] == str(NULLRUN_PROTOCOL_VERSION)
        finally:
            t.stop()

    @respx.mock
    def test_chain_end_includes_protocol_header(self):
        # 2026-07-04 (B3): ``chain_end`` now POSTs to
        # /api/v1/gate with ``chain_op: "end"``. The /api/v1/chain/end
        # endpoint was never registered on the backend.
        # 2026-09-11 (DEF-CHAIN-END-ORG-ID): chain_end now requires
        # organization_id (the backend's GateRequest rejects without it).
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            route = respx.post(f"{BASE_URL}/api/v1/gate").mock(
                return_value=Response(200, json={"decision": "allow"})
            )
            t.chain_end("chain-abc", organization_id="org-1")
            sent = route.calls.last.request
            assert sent.headers["X-NULLRUN-PROTOCOL"] == str(NULLRUN_PROTOCOL_VERSION)
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
            assert sent.headers["X-NULLRUN-PROTOCOL"] == str(NULLRUN_PROTOCOL_VERSION)
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
            assert sent.headers["X-NULLRUN-PROTOCOL"] == str(NULLRUN_PROTOCOL_VERSION)
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
            assert sent.headers["X-NULLRUN-PROTOCOL"] == str(NULLRUN_PROTOCOL_VERSION)
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

    # REMOVED 2026-09-10: ``test_redis_unavailable_maps_to_budget_error``
    # — the legacy v2 ``REDIS_UNAVAILABLE`` slug is absent from the
    # backend ``GateErrorCode::all()`` set and is never emitted on the
    # wire. The corresponding SDK map entry in transport.py
    # (``_V3_ERROR_CODE_MAP``) was removed in lockstep so cookbook
    # mapping tables stay in sync with what the backend can actually
    # send. BUDGET_REDIS_UNAVAILABLE / RATE_LIMIT_REDIS_UNAVAILABLE
    # below are the post-v3.36 canonical codes and remain mapped.

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
        # The backend's real details block: reservation in MILLICENTS,
        # actual in cents, no `epsilon_cents` key. See handlers.rs, the
        # ConsumeOverbudget 422.
        resp = self._make_response(
            422,
            {
                "error_code": "CONSUME_OVERBUDGET",
                "error_message": "actual > reserved + epsilon",
                "details": {
                    "reserved_millicents": 1000,
                    "max_allowed_millicents": 1010,
                    "actual_cost_cents": 150,
                    "soft_pass": False,
                    "reservation_recorded": True,
                },
            },
        )
        exc = _parse_v3_error_envelope(resp, "track")
        assert isinstance(exc, NullRunConsumeOverbudgetError)
        assert exc.reserved_millicents == 1000
        assert exc.max_allowed_millicents == 1010
        assert exc.actual_cost_cents == 150
        assert exc.recorded is True

    def test_budget_recheck_failed_maps_to_typed_error(self):
        #: AR-H6 (2026-08-12) — post-approval re-check failure must
        #: dispatch to a typed exception (NR-B006), not the generic
        #: NullRunBudgetError (NR-B004). The two differ in retry
        #: semantics: recheck failures are retryable after re-/gate,
        #: fresh /gate blocks are not. Pre-v3.53 SDK collapsed the
        #: recheck into the generic fallback with no introspection
        #: on current_spend_cents / budget_cents.
        resp = self._make_response(
            402,
            {
                "error_code": "BUDGET_RECHECK_FAILED",
                "error_message": "Post-approval budget authorization failed",
                "details": {
                    "current_spend_cents": 1050,
                    "budget_cents": 1000,
                    "execution_id": "exec-abc",
                },
            },
        )
        exc = _parse_v3_error_envelope(resp, "execute")
        # Typed subclass, distinct from NR-B004 NullRunBudgetError
        assert isinstance(exc, NullRunBudgetRecheckFailedError)
        # Subclass of NullRunBudgetError so existing `except NullRunBudgetError:`
        # pattern keeps matching (back-compat invariant).
        assert isinstance(exc, NullRunBudgetError)
        # First-class attributes surface from the wire envelope.
        assert exc.current_spend_cents == 1050
        assert exc.budget_cents == 1000
        # Recheck is retryable after re-/gate (vs fresh /gate block which is not).
        assert exc.recheck_retryable is True
        assert exc.error_code == "NR-B006"

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
            # REDIS_UNAVAILABLE removed 2026-09-10 — never emitted
            # by the backend (absent from GateErrorCode::all()).
            # Use BUDGET_REDIS_UNAVAILABLE / RATE_LIMIT_REDIS_UNAVAILABLE
            # (canonical post-v3.36) instead.
            "CHAIN_MAX_DURATION_EXCEEDED",
            "CHAIN_CROSS_ORG",
            "CHAIN_ORG_MISMATCH",
            "WORKFLOW_INACTIVE",
            "API_KEY_REVOKED",
            "CONSUME_OVERBUDGET",
            "RATE_LIMIT_EXCEEDED",
            "RATE_LIMIT_REDIS_UNAVAILABLE",
            "BUDGET_DATA_UNAVAILABLE",
            # NR-007 SDK↔backend parity audit (2026-09-08). The 8
            # codes below were unmapped in `_V3_ERROR_CODE_MAP` and
            # silently degraded to the generic NullRunBackendError
            # — cookbook code that branches on the typed exception
            # class would never catch them. Added per
            # `nullrun-examples/SDK_BACKEND_PARITY_MATRIX.md` §5.
            "WORKFLOW_CYCLE_DETECTED",
            "WORKFLOW_DEPTH_EXCEEDED",
            "WORKFLOW_PARENT_BINDING_EXPIRED",
            "WORKFLOW_DEPTH_LOOKUP_FAILED",
            "INVOKE_PERSIST_FAILED",
            "SUBWORKFLOW_INVOKE_DISABLED",
            "APPROVAL_ALREADY_DECIDED",
            "LEGACY_GRANT_REJECTED",
        }
        actual = set(_V3_ERROR_CODE_MAP.keys())
        missing = expected - actual
        assert not missing, f"Missing v3 error_code mappings: {missing}"

        # Defensive: each newly-added code must map to the
        # exception class documented in the parity matrix (NOT
        # the generic NullRunBackendError — that would defeat the
        # purpose of the typed mapping). The test catches a
        # future maintainer who "simplifies" the map by falling
        # everything back to NullRunBackendError.
        from nullrun.breaker.exceptions import (
            NullRunApprovalReplayRejectedError,
            NullRunBackendError,
            NullRunBlockedException,
            NullRunChainError,
        )

        typed_required = {
            "WORKFLOW_CYCLE_DETECTED": NullRunChainError,
            "WORKFLOW_DEPTH_EXCEEDED": NullRunChainError,
            "WORKFLOW_PARENT_BINDING_EXPIRED": NullRunChainError,
            "WORKFLOW_DEPTH_LOOKUP_FAILED": NullRunChainError,
            "INVOKE_PERSIST_FAILED": NullRunBackendError,
            "SUBWORKFLOW_INVOKE_DISABLED": NullRunChainError,
            "APPROVAL_ALREADY_DECIDED": NullRunApprovalReplayRejectedError,
            "LEGACY_GRANT_REJECTED": NullRunBlockedException,
        }
        for code, expected_cls in typed_required.items():
            actual_cls = _V3_ERROR_CODE_MAP[code]
            assert actual_cls is expected_cls, (
                f"{code} maps to {actual_cls.__name__}, expected "
                f"{expected_cls.__name__}. Cookbook recipes branch "
                f"on the typed exception class, so a generic "
                f"fallback (NullRunBackendError for non-infra "
                f"codes) silently breaks recipe dispatch."
            )


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
        # v0.16.2: set_chain_id now requires a UUID v4 string (see
        # src/nullrun/context.py::_validate_chain_id, F5 sdk_checks
        # 2026-08-21). The pre-validation `"chain-1"` literal here
        # would raise ValueError after ebbe4ef — use a real UUID v4
        # so the persistence contract is still asserted without
        # tripping the new strict validator.
        cid = str(uuid.uuid4())
        set_chain_id(cid)
        assert get_chain_id() == cid

    def test_chain_contextmanager_sets_and_resets(self):
        cid = str(uuid.uuid4())
        with chain(cid, op="start") as yielded:
            assert yielded == cid
            assert get_chain_id() == cid
            assert _chain_op_var.get() == "start"
        # Exit: contextvar reset to its pre-block value
        assert get_chain_id() is None

    def test_chain_contextmanager_rejects_invalid_op(self):
        # v0.16.2: chain() now validates chain_id BEFORE op. To test
        # op-rejection specifically, supply a syntactically valid
        # UUID v4 with an invalid op — otherwise the chain_id check
        # would fire first and shadow the op-rejection assertion.
        valid_cid = str(uuid.uuid4())
        with pytest.raises(ValueError, match="chain\\(\\) op must be"):
            with chain(valid_cid, op="garbage"):
                pass

    def test_chain_nested_restores_outer_on_exit(self):
        # v0.16.2: chain() requires UUID v4 per context._validate_chain_id.
        outer_cid = str(uuid.uuid4())
        inner_cid = str(uuid.uuid4())
        with chain(outer_cid, op="start"):
            with chain(inner_cid, op="continue"):
                assert get_chain_id() == inner_cid
            # Inner exited — outer restored.
            assert get_chain_id() == outer_cid
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
            t.chain_end("chain-1", organization_id="org-1")
            sent = route.calls.last.request
            body = sent.content.decode("utf-8")
            assert '"chain_id":"chain-1"' in body
            assert '"chain_op":"end"' in body
        finally:
            t.stop()

    @respx.mock
    def test_chain_end_sends_full_gate_request_body(self):
        # DEF-CHAIN-END-ORG-ID (2026-09-11): pre-fix ``chain_end`` POSTed
        # only ``{chain_id, chain_op, execution_id}`` to /gate. The
        # backend's ``GateRequest`` struct (see
        # backend/src/proxy/http/gate/internal.rs:156) marks
        # ``organization_id``, ``execution_id``, ``trace_id``, ``mode``
        # as REQUIRED — the deserializer rejects with 422
        # ``missing field 'organization_id'`` BEFORE chain_op-specific
        # dispatch runs. Live wire trace against api.nullrun.io
        # (capture [009] in
        # nullrun-examples/examples/_wire_trace_output.txt) confirms
        # the 422.
        #
        # Fix: ``Transport.chain_end`` accepts ``organization_id`` (and
        # optional ``trace_id``/``operation_id``) kwargs, builds a
        # full GateRequest body (incl. action_digest so the
        # Phase-1+ version-gate at backend::gate::gate.rs:148 passes),
        # and raises ``NullRunConfigError`` if a direct caller
        # forgets to pass organization_id.
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            route = respx.post(f"{BASE_URL}/api/v1/gate").mock(
                return_value=Response(200, json={"decision": "allow"})
            )
            t.chain_end("chain-1", organization_id="org-1")
            sent = route.calls.last.request
            body = sent.content.decode("utf-8")
            # All four GateRequest required fields must be on the wire.
            assert '"organization_id":"org-1"' in body
            assert '"execution_id":"' in body
            assert '"trace_id":"' in body
            assert '"mode":"auto"' in body
            assert '"chain_id":"chain-1"' in body
            assert '"chain_op":"end"' in body
            assert '"operation_id":"' in body
            # Phase-1+ version-gate rejects missing action_digest on
            # proto>=3 — chain_end emits the NoImpact sentinel digest
            # to match ``runtime.check_workflow_budget``.
            assert '"action_digest":"' in body
        finally:
            t.stop()

    @respx.mock
    def test_chain_end_missing_organization_id_raises(self):
        # DEF-CHAIN-END-ORG-ID: a direct Transport.chain_end call
        # without organization_id MUST raise ``NullRunConfigError``
        # before it hits the network — silent 422 from the backend
        # would surface as a confusing transport error.
        from nullrun.breaker.exceptions import NullRunConfigError

        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            with pytest.raises(NullRunConfigError) as excinfo:
                t.chain_end("chain-1")
            assert "organization_id" in str(excinfo.value)
        finally:
            t.stop()

    @respx.mock
    def test_chain_end_forwards_trace_id_when_provided(self):
        # When the caller (typically ``Runtime.chain_end``) supplies
        # a trace_id, it MUST land on the wire unchanged so the
        # chain-close event is correlated with the rest of the
        # chain's spans.
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            route = respx.post(f"{BASE_URL}/api/v1/gate").mock(
                return_value=Response(200, json={"decision": "allow"})
            )
            t.chain_end(
                "chain-1",
                organization_id="org-1",
                trace_id="trace-abc",
            )
            sent = route.calls.last.request
            body = sent.content.decode("utf-8")
            assert '"trace_id":"trace-abc"' in body
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
# SDK-1 — the in-process gate cache is GONE (regression pins)
# ─────────────────────────────────────────────────────────────────────
#
# There was a chain-mode short-circuit keyed on
# ``(workflow_id, chain_id, call_model, estimated_tokens)`` that served a
# previous ``/gate`` answer for up to five seconds. It was not a cache,
# it was a second authorisation path with no gate behind it:
#
#   * the key carried no org id, no api key id and no tool set, so one
#     chain's "allow" could be served to another chain, or to a
#     different tool inside the same chain, whose call was never
#     checked;
#   * the cached body carried call #1's ``reservation_id`` and the
#     runtime re-captured it for every later step, so every step of a
#     chain ran under ONE server-minted execution id. The backend
#     settles an execution id at most once (``consume_v3.lua`` answers
#     IDEMPOTENT_REPLAY for an execution that is already ``consumed``
#     and books nothing), so a three-step chain performed three tool
#     executions and produced ONE accounting event — the money for
#     steps 2 and 3 was never billed;
#   * the tool-block check never ran for a cached step either.
#
# The backend owns the chain: ``chain_op`` (start / continue / end) and
# the chain TTL are enforced there, and every ``/gate`` inside a chain
# reserves and settles its own envelope. The backend's own idempotency
# store (``idempotency_key`` = the SDK's ``operation_id``) dedupes a
# genuine RETRY of one logical action and nothing else.

# Three distinct server-minted reservation ids (uuidv7 layout — the
# version nibble is the first hex char of the third group).
_SDK1_RESERVATIONS = [
    "0190c5b5-7c9a-7def-8a1b-0123456789ab",
    "0190c5b5-7c9a-7def-8a1b-fedcba987654",
    "0190c5b5-7c9a-7def-8a1b-0123456789cd",
]


class TestNoInProcessGateCache:
    """The gate cache must not come back in any shape."""

    def test_the_runtime_module_has_no_gate_cache(self):
        from nullrun import runtime

        assert not hasattr(runtime, "_GATE_CACHE"), (
            "the in-process gate cache is gone (SDK-1): a client-side "
            "short-circuit is a second authorisation path with no gate "
            "behind it"
        )
        assert not hasattr(runtime, "_GATE_CACHE_TTL_SECONDS")
        assert not hasattr(runtime, "_invalidate_gate_cache_for_chain")

    def test_check_workflow_budget_has_no_use_gate_cache_switch(self):
        import inspect

        from nullrun.runtime import NullRunRuntime

        params = inspect.signature(NullRunRuntime.check_workflow_budget).parameters
        assert "use_gate_cache" not in params, (
            "the cache switch is gone with the cache: a switch that "
            "re-enables it is the same defect one level down"
        )

    def test_the_cache_disable_env_var_is_gone(self):
        # The opt-out env var is dead. It must not linger as a way to
        # re-enable (or, worse, to be mistaken for) the cache.
        import inspect

        from nullrun import runtime

        source = inspect.getsource(runtime)
        assert "NULLRUN_GATE_CACHE_DISABLE" not in source


class TestChainStepsAreAdmittedIndividually:
    """SDK-1 at the runtime level: every step of a chain is its own
    ``/gate`` call and its own reservation.
    """

    @respx.mock
    def test_three_chain_checks_make_three_gate_calls(self):
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
            with workflow("wf-sdk1"), chain(str(uuid.uuid4())):
                rt_inst.check_workflow_budget()
                rt_inst.check_workflow_budget()
                rt_inst.check_workflow_budget()
            gate_calls = [
                c for c in respx.calls if c.request.url.path.endswith("/gate")
            ]
            assert len(gate_calls) == 3, (
                f"each chain step must be admitted by its own /gate "
                f"call; got {len(gate_calls)}"
            )
        finally:
            try:
                rt_inst.shutdown()
            except Exception:
                pass

    @respx.mock
    def test_each_chain_step_gets_its_own_server_minted_reservation(self):
        # The money-relevant half of SDK-1. Pre-fix the cache served
        # call #1's ``reservation_id`` to steps 2 and 3, so the
        # backend settled ONE execution for THREE tool calls and the
        # money for steps 2 and 3 was never billed.
        respx.post(f"{BASE_URL}/api/v1/gate").mock(
            side_effect=[
                Response(
                    200,
                    json={
                        "decision": "allow",
                        "decision_source": "gateway",
                        "reservation_id": reservation,
                    },
                )
                for reservation in _SDK1_RESERVATIONS
            ]
        )
        rt_inst = NullRunRuntime(
            api_key="nr_live_abc123",
            api_url=BASE_URL,
            _test_mode=True,
            polling=False,
        )
        try:
            with workflow("wf-sdk1"), chain(str(uuid.uuid4())):
                got = [
                    rt_inst.check_workflow_budget() for _ in range(3)
                ]
            assert got == _SDK1_RESERVATIONS, (
                "every chain step must carry the reservation the server "
                "minted for THAT step; sharing one reservation id means "
                "the backend settles the chain once and bills one step"
            )
        finally:
            try:
                rt_inst.shutdown()
            except Exception:
                pass

    @respx.mock
    def test_every_chain_step_carries_the_tool_list(self):
        # The cache key had no tool set in it, so a cached step never
        # ran the tool-block check. Each step must put its tools on the
        # wire so the backend can block on them.
        import json as _json

        from nullrun.context import set_call_context

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
            with workflow("wf-sdk1"), chain(str(uuid.uuid4())):
                set_call_context(tools=["dangerous_tool"])
                rt_inst.check_workflow_budget()
                rt_inst.check_workflow_budget()
            gate_calls = [
                c for c in respx.calls if c.request.url.path.endswith("/gate")
            ]
            assert len(gate_calls) == 2
            for call in gate_calls:
                body = _json.loads(call.request.content)
                assert body.get("tools") == ["dangerous_tool"], (
                    "every step must present its tools to the gate"
                )
        finally:
            try:
                rt_inst.shutdown()
            except Exception:
                pass

    @respx.mock
    def test_the_dead_env_var_cannot_bring_the_cache_back(self):
        # The old opt-out was "the cache is disabled". Now that the
        # cache is gone, the variable must be inert: it may not change
        # the number of /gate calls.
        import os

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
                with workflow("wf-sdk1"), chain(str(uuid.uuid4())):
                    rt_inst.check_workflow_budget()
                    rt_inst.check_workflow_budget()
                gate_calls = [
                    c for c in respx.calls if c.request.url.path.endswith("/gate")
                ]
                assert len(gate_calls) == 2
            finally:
                try:
                    rt_inst.shutdown()
                except Exception:
                    pass
        finally:
            os.environ.pop("NULLRUN_GATE_CACHE_DISABLE", None)

    @respx.mock
    def test_chain_end_still_closes_the_chain_on_the_server(self):
        # The cache invalidation that used to hang off ``chain_end``
        # is gone with the cache. The server-side close must still
        # happen — that is the part that was never the SDK's to skip.
        rt_inst = NullRunRuntime(
            api_key="nr_live_abc123",
            api_url=BASE_URL,
            _test_mode=True,
            polling=False,
        )
        gate = respx.post(f"{BASE_URL}/api/v1/gate").mock(
            return_value=Response(
                200,
                json={"decision": "allow", "decision_source": "gateway"},
            )
        )
        try:
            # `_test_mode` skips the auth handshake, so the org binding
            # that ``chain_end`` forwards has to be set by hand.
            rt_inst.organization_id = "org-1"
            chain_id = str(uuid.uuid4())
            result = rt_inst.chain_end(chain_id)
            assert isinstance(result, dict)
            assert gate.call_count == 1
        finally:
            try:
                rt_inst.shutdown()
            except Exception:
                pass


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


import pytest
import respx

from nullrun.context import (
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
        # New ContextVar with no prior set → None (audit: "нет var
        # на старте"). Verifies the SDK doesn't ship with a stale
        # id baked into the context.
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
            "cost_source": "provisional",
        }
        # ADR-068 §4b: a full event is no exception — a populated
        # payload must still carry no price. See the minimal-event test
        # for why the exact-dict match is the assertion that matters.
        assert "cost_cents" not in out

    def test_minimal_event_only_required_fields(self):
        # workflow_id + tokens + reservation_id are the floor.
        #
        # ADR-068 §4b: no `cost_cents`. The SDK reports tokens and the
        # backend prices them. Asserted by ABSENCE below — an exact-dict
        # match is what makes "the SDK never offers a price" a checked
        # property rather than a comment.
        out = _build_v3_track_payload(
            {"type": "llm_call", "workflow_id": "wf-1", "tokens": 1},
            SERVER_MINTED_V1,
        )
        assert out == {
            "reservation_id": SERVER_MINTED_V1,
            "workflow_id": "wf-1",
            "tokens": 1,
            "cost_source": "provisional",
        }
        assert "cost_cents" not in out, (
            "ADR-068 §4b: the SDK must not put a price on the wire. It "
            "sends tokens; the backend prices them from its own catalog."
        )

    def test_missing_workflow_id_returns_none(self):
        # v0.16.0 (backend v3.66.2 alignment): caller now DROPS
        # llm_call events whose mapper refused (missing required
        # field), not batch — backend v3.66.2 wire-validation would
        # fail-CLOSE the batch anyway. Mapper returning None is the
        # signal for the caller to drop instead of fabricating a
        # malformed v3 payload.
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

    # ── ADR-068 §1 (backend NULLRUN): `provider` must reach the wire.
    #
    # The backend picks the cache-token convention off `provider`.
    # Anthropic reports the cached fraction OUTSIDE `input_tokens`;
    # OpenAI / Gemini / Mistral report it INSIDE `prompt_tokens`. If
    # the field is missing, the backend sees an unmapped provider and
    # applies the fail-CLOSED no-discount path — the full input rate,
    # which is what we billed before ADR-068 landed. So dropping this
    # key silently restores the overcharge for every cache-using call.
    #
    # The batch path already forwarded it (it ships the whole enriched
    # event); this is the single-event path, which whitelists keys.

    def test_provider_is_forwarded_when_present(self):
        out = _build_v3_track_payload(
            {
                "type": "llm_call",
                "workflow_id": "wf-1",
                "tokens": 100,
                "input_tokens": 60,
                "output_tokens": 40,
                "model": "claude-sonnet-4-6",
                "provider": "anthropic",
            },
            SERVER_MINTED_V1,
        )
        assert out["provider"] == "anthropic"

    def test_provider_absent_stays_absent(self):
        # No fabricated key: the backend distinguishes "provider we
        # could not determine" (fail-CLOSED, counted) from a provider
        # we named. Emitting `None` would make both look the same.
        out = _build_v3_track_payload(
            {"type": "llm_call", "workflow_id": "wf-1", "tokens": 1},
            SERVER_MINTED_V1,
        )
        assert "provider" not in out

    def test_empty_provider_is_not_sent(self):
        # `""` is the shape `_provider_label` returns for a host it
        # cannot classify down to a real name; forwarding it would
        # look like a known-but-unmapped provider to the backend.
        out = _build_v3_track_payload(
            {
                "type": "llm_call",
                "workflow_id": "wf-1",
                "tokens": 1,
                "provider": "",
            },
            SERVER_MINTED_V1,
        )
        assert "provider" not in out

    def test_cache_token_fields_survive_to_the_wire(self):
        # The companion half: the backend prices the cached fraction
        # and persists it (ADR-068 §D2). Both fields have to be here
        # for either to mean anything.
        out = _build_v3_track_payload(
            {
                "type": "llm_call",
                "workflow_id": "wf-1",
                "tokens": 9100,
                "input_tokens": 100,
                "output_tokens": 10,
                "provider": "anthropic",
                "cache_read_tokens": 9000,
                "cache_write_tokens": 0,
            },
            SERVER_MINTED_V1,
        )
        assert out["cache_read_tokens"] == 9000
        assert out["cache_write_tokens"] == 0


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
    def test_llm_call_without_smid_is_dropped(self, make_runtime):
        # v0.16.0 (backend v3.66.2 alignment): no /check in scope →
        # no smid → DROP. Previously (0.12.0–0.15.2) this fell back
        # to /track/batch (the legacy v1/v2 no-reservation consume
        # path). Backend v3.66.2 closed that path with per-event
        # type-aware wire validation: any ``llm_call`` event in a
        # batch WITHOUT ``reservation_id`` is rejected with 503
        # BUDGET_RECHECK_FAILED (whole-batch fail-CLOSED). Falling
        # back here would amplify into a tight retry loop producing
        # 503-storm for every call site that forgot to pair
        # ``track_llm`` with a prior ``check_workflow_budget`` (or
        # ``@protect`` / ``with workflow(...)``).
        from nullrun.observability import metrics

        rt = make_runtime()

        single_route = respx.post(f"{BASE_URL}/api/v1/track").mock(
            return_value=Response(200, json={"status": "ok"})
        )
        batch_route = respx.post(f"{BASE_URL}/api/v1/track/batch").mock(
            return_value=Response(200, json={"ok": True, "accepted": 1})
        )

        # Snapshot the drop counter BEFORE the call so concurrent
        # tests in the same suite can't make the assertion noisy.
        before = metrics.runtime.dropped_llm_call_no_reservation

        # No capture call here — contextvar stays empty.
        rt.track_llm(
            input_tokens=10,
            output_tokens=5,
            model="claude-sonnet-4-6",
        )
        # Buffer + flush — neither endpoint should fire.
        rt._transport.flush_now()

        assert single_route.call_count == 0
        assert batch_route.call_count == 0
        assert (
            metrics.runtime.dropped_llm_call_no_reservation == before + 1
        ), "drop counter must increment by 1 on a no-smid llm_call"

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

    @respx.mock
    def test_track_single_422_refused_consume_propagates(self, make_runtime):
        # 2026-09-12 (DEF-CACHE-STALE-ALLOW-AFTER-OVERBUDGET): when
        # ``/track`` returns HTTP 422 (CONSUME_OVERBUDGET) the chain
        # has hit its budget ceiling.
        #
        # Pre-fix the SDK's answer to a refused consume was to drop
        # an in-process gate cache, so the next step of the chain
        # could not be served a stale "allow". That cache is gone
        # (SDK-1) and the refusal is handled where it belongs: the
        # server refuses the next step's own /gate call, and this
        # refusal reaches the agent as a typed error.
        #
        # The property that matters is unchanged and is the one this
        # test pins: a refused consume is NOT laundered into an
        # allowed one (DEF-TC6-006).
        import uuid as _uuid

        rt = make_runtime()

        # Mock /track to return 422 CONSUME_OVERBUDGET.
        respx.post(f"{BASE_URL}/api/v1/track").mock(
            return_value=Response(
                422,
                json={
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
        )

        # Pin the chain_id in the contextvar (matches what
        # ``with chain(...)`` would set).
        from nullrun.context import set_chain_id

        chain_id = str(_uuid.uuid4())
        token = set_chain_id(chain_id)
        try:
            # Capture a server-minted id so _route_track reaches
            # track_single (instead of dropping on "no reservation").
            _capture_server_minted_execution_id(
                {"reservation_id": SERVER_MINTED_V1}
            )

            from nullrun.breaker.exceptions import NullRunConsumeOverbudgetError

            with pytest.raises(NullRunConsumeOverbudgetError):
                rt.track_llm(
                    input_tokens=60,
                    output_tokens=40,
                    model="claude-sonnet-4-6",
                )
        finally:
            _chain_id_var.reset(token)

    @respx.mock
    def test_track_single_402_refused_consume_propagates(self, make_runtime):
        # Same shape as the 422 test above: a 402 on the consume path
        # must reach the agent too. We use BUDGET_HARD_BLOCKED
        # (catalog-mapped → NullRunBudgetError with status_code=402).
        # Note: REDIS_UNAVAILABLE is absent from the v3 catalog
        # (removed 2026-09-10) and would fall through to the generic
        # NullRunBackendError, whose status_code lives in ``details``
        # not as a direct attribute.
        import uuid as _uuid

        rt = make_runtime()

        # Mock /track to return 402 (BUDGET_HARD_BLOCKED).
        respx.post(f"{BASE_URL}/api/v1/track").mock(
            return_value=Response(
                402,
                json={
                    "error_code": "BUDGET_HARD_BLOCKED",
                    "error_message": "consume-side authoritative check failed",
                    "details": {"endpoint": "consume"},
                },
            )
        )

        from nullrun.context import set_chain_id

        chain_id = str(_uuid.uuid4())
        token = set_chain_id(chain_id)
        try:
            _capture_server_minted_execution_id(
                {"reservation_id": SERVER_MINTED_V1}
            )

            # DEF-TC6-006: same shape as the 422 test above --
            # NullRunBudgetError is a NullRunBlockedException, which
            # is a NullRunDecision, so the 402 propagates too.
            from nullrun.breaker.exceptions import NullRunBudgetError

            with pytest.raises(NullRunBudgetError):
                rt.track_llm(
                    input_tokens=10,
                    output_tokens=10,
                    model="claude-sonnet-4-6",
                )
        finally:
            _chain_id_var.reset(token)


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

        from nullrun.breaker.exceptions import NullRunBudgetError
        from nullrun.context import workflow
        from nullrun.observability import metrics

        # Snapshot the drop counter BEFORE the block+track so the
        # assertion is isolated from concurrent tests in the same
        # suite.
        before = metrics.runtime.dropped_llm_call_no_reservation

        with workflow("wf-1"):
            # Block path raises — NullRunBudgetError is a
            # NullRunBlockedException (carries the policy decision
            # upstream). Catch it explicitly for this test which only
            # wants to verify contextvar hygiene after a block.
            try:
                rt.check_workflow_budget()
            except NullRunBudgetError:
                pass

            rt.track_llm(
                input_tokens=1,
                output_tokens=1,
                model="x",
            )
            rt._transport.flush_now()

        # v0.16.0 (backend v3.66.2 alignment): no reservation_id
        # was minted (block response carries no reservation_id; the
        # capture helper clears the contextvar) → event is DROPPED,
        # not batched. Pre-0.16.0 this fell back to /track/batch,
        # which v3.66.2 rejects with 503 BUDGET_RECHECK_FAILED.
        # The semantic intent of the test is preserved: no smid
        # leaks from the block into the subsequent track.
        assert single_route.call_count == 0
        assert batch_route.call_count == 0
        assert (
            metrics.runtime.dropped_llm_call_no_reservation == before + 1
        ), "drop counter must increment by 1 on a post-block llm_call"


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

import pytest
import respx

from nullrun.breaker import exceptions as exc
from nullrun.capabilities import (
    CAPABILITIES_PATH,
    probe_capabilities,
)

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


# ─── v3.53 wire-bilateral gap fix ─────────────────────────────
"""v3.53 (2026-08-13) closes the wire-contract bilateral gap between
backend ``backend/src/proxy/http/gate/internal.rs:3059-3108,
3115-3138`` (which emits distinct §13 wire codes for each of the
seven post-approval grant-consume outcomes) and the SDK
``_V3_ERROR_CODE_MAP`` (which collapsed every distinct outcome into
``NullRunBlockedException``).

These tests pin the SDK-side fix so a future refactor that drops any
of the six new entries or weakens the typed dispatcher gets caught
in CI rather than at first production /execute.

Pre-fix failure mode: SDK cookbook code could only catch the generic
``NullRunBlockedException`` and string-match the ``error_message`` to
distinguish "operator denied", "approval pending", "grant already
consumed", etc. — fragile, locale-sensitive, and brittle against
backend copy changes.
"""


@pytest.mark.parametrize(
    "wire_code,expected_exc_class",
    [
        ("APPROVAL_NOT_YET_APPROVED", exc.NullRunApprovalNotYetApprovedError),
        ("APPROVAL_DENIED", exc.NullRunApprovalDeniedError),
        ("APPROVAL_EXPIRED", exc.NullRunApprovalExpiredError),
        ("APPROVAL_DIGEST_MISMATCH", exc.NullRunApprovalDigestMismatchError),
        ("APPROVAL_TOOL_DIGEST_MISMATCH", exc.NullRunApprovalToolDigestMismatchError),
        ("APPROVAL_REPLAY_REJECTED", exc.NullRunApprovalReplayRejectedError),
        # DEC-5 / DEC-7 (audit 2026-11-02): the approval binds a TOOL
        # SET. The executing tool must be a member of the set the
        # /gate request declared — a money approval used to bind only
        # the AMOUNT, so an approval for tool A authorised tool B.
        ("APPROVAL_TOOL_MISMATCH", exc.NullRunApprovalToolMismatchError),
    ],
)
def test_v3_error_code_map_covers_all_post_approval_outcomes(
    wire_code, expected_exc_class
):
    """All six post-approval grant-consume outcomes must map to a
    distinct typed exception.

    Pre-v3.53 the SDK's ``_V3_ERROR_CODE_MAP`` only covered the
    /gate create-failure family (``APPROVAL_DB_UNAVAILABLE``,
    ``APPROVAL_PERSISTENCE_FAILED``, etc.) — all six /execute
    post-approval outcomes silently fell through to the generic
    HTTP-status fallback (NullRunBlockedException with no typed
    discrimination). Bilateral wire gap. Pins the catalog entry so
    a refactor that drops any of the six fails loudly here.
    """
    assert wire_code in _V3_ERROR_CODE_MAP, (
        f"v3.53 audit: backend emits {wire_code} on /execute grant-"
        "consume but SDK has no map entry. Cookbook recipes cannot "
        "branch on this outcome."
    )
    assert _V3_ERROR_CODE_MAP[wire_code] is expected_exc_class, (
        f"v3.53 audit: {wire_code} maps to "
        f"{_V3_ERROR_CODE_MAP[wire_code].__name__} in the SDK but "
        f"the typed exception is {expected_exc_class.__name__}. "
        "Cookbook code uses `except NullRunApprovalDeniedError:` "
        "(etc.) so the map must point at the typed class."
    )


@pytest.mark.parametrize(
    "wire_code,expected_nr_code",
    [
        ("APPROVAL_NOT_YET_APPROVED", "NR-A010"),
        ("APPROVAL_DENIED", "NR-A011"),
        ("APPROVAL_EXPIRED", "NR-A012"),
        ("APPROVAL_DIGEST_MISMATCH", "NR-A013"),
        ("APPROVAL_TOOL_DIGEST_MISMATCH", "NR-A014"),
        ("APPROVAL_REPLAY_REJECTED", "NR-A015"),
        # DEC-5 / DEC-7: the tool-binding refusal.
        ("APPROVAL_TOOL_MISMATCH", "NR-A017"),
    ],
)
def test_post_approval_exceptions_carry_stable_nr_codes(wire_code, expected_nr_code):
    """Each typed exception carries a stable ``error_code`` (NR-Axxx)
    so cookbook / Sentry rules can branch on it without parsing the
    message.

    Pins the catalog triple ``(wire_code, exception, NR-code)`` so
    a future renumbering shows up in code review / changelog
    (NR-codes are wire-stable — see audit P0 close for v3.52).
    """
    cls = _V3_ERROR_CODE_MAP[wire_code]
    assert cls.error_code == expected_nr_code, (
        f"{cls.__name__}.error_code is {cls.error_code!r}, expected "
        f"{expected_nr_code!r} (v3.53 catalog). NR-codes are wire-"
        "stable — renumbering is a breaking change."
    )
    # NR-Axxx family is the approval-block taxonomy.
    assert cls.error_code.startswith("NR-A"), (
        f"{cls.__name__}.error_code must start with NR-A (approval "
        f"taxonomy), got {cls.error_code!r}."
    )


def test_parse_v3_error_envelope_dispatches_approval_denied_to_typed_exception():
    """``APPROVAL_DENIED`` from /execute → ``NullRunApprovalDeniedError``
    with status_code=403 and wire details preserved.

    Pins the dispatcher arm in ``_parse_v3_error_envelope``. Pre-v3.53
    the catalog fallback path called ``catalog(full_message, **details)``
    which raised TypeError on NullRunBlockedException (needs
    workflow_id positional arg) — silent internal error instead of
    a typed exception.
    """
    response = httpx.Response(
        403,
        json={
            "error_code": "APPROVAL_DENIED",
            "error_message": "Operator denied the approval",
            "details": {
                "approval_id": "apr-uuid-1234",
                "workflow_id": "wf-uuid-5678",
                "denied_by": "operator@nullrun.io",
            },
        },
    )
    err = _parse_v3_error_envelope(response, "execute")
    assert isinstance(err, exc.NullRunApprovalDeniedError)
    assert err.status_code == 403
    assert err.error_code == "NR-A011"
    # Back-compat: existing ``except NullRunBlockedException:`` still
    # catches — every approval exception inherits from it.
    assert isinstance(err, exc.NullRunBlockedException)


def test_parse_v3_error_envelope_dispatches_approval_not_yet_approved():
    """``APPROVAL_NOT_YET_APPROVED`` → ``NullRunApprovalNotYetApprovedError``
    with ``retryable=True`` so SDK cookbook recipes can poll/await
    the operator decision without classifying it as terminal.
    """
    response = httpx.Response(
        403,
        json={
            "error_code": "APPROVAL_NOT_YET_APPROVED",
            "error_message": "Approval pending",
            "details": {"approval_id": "apr-uuid-9999"},
        },
    )
    err = _parse_v3_error_envelope(response, "execute")
    assert isinstance(err, exc.NullRunApprovalNotYetApprovedError)
    assert err.status_code == 403
    assert err.retryable is True
    assert err.error_code == "NR-A010"


def test_parse_v3_error_envelope_dispatches_approval_replay_rejected():
    """``APPROVAL_REPLAY_REJECTED`` → ``NullRunApprovalReplayRejectedError``
    with ``retryable=False`` — single-use grant, retrying with the
    same approval_id will keep failing.
    """
    response = httpx.Response(
        403,
        json={
            "error_code": "APPROVAL_REPLAY_REJECTED",
            "error_message": "Grant already consumed",
            "details": {"approval_id": "apr-uuid-1111"},
        },
    )
    err = _parse_v3_error_envelope(response, "execute")
    assert isinstance(err, exc.NullRunApprovalReplayRejectedError)
    assert err.status_code == 403
    assert err.retryable is False
    assert err.error_code == "NR-A015"


def test_parse_v3_error_envelope_dispatches_approval_expired():
    """``APPROVAL_EXPIRED`` → ``NullRunApprovalExpiredError`` — terminal,
    caller must request a fresh approval row with revised params.
    """
    response = httpx.Response(
        403,
        json={
            "error_code": "APPROVAL_EXPIRED",
            "error_message": "Grant TTL elapsed",
            "details": {
                "approval_id": "apr-uuid-2222",
                "expires_at": "2026-08-13T00:00:00Z",
            },
        },
    )
    err = _parse_v3_error_envelope(response, "execute")
    assert isinstance(err, exc.NullRunApprovalExpiredError)
    assert err.status_code == 403
    assert err.error_code == "NR-A012"
    # Operator-facing hint points at the per-code docs page.
    assert err.docs_url.startswith("https://")


def test_parse_v3_error_envelope_dispatches_approval_digest_mismatch():
    """``APPROVAL_DIGEST_MISMATCH`` → ``NullRunApprovalDigestMismatchError`` —
    action/business-impact digest drift, terminal (re-approval needed).
    """
    response = httpx.Response(
        403,
        json={
            "error_code": "APPROVAL_DIGEST_MISMATCH",
            "error_message": "Action digest drifted since approval",
            "details": {"approval_id": "apr-uuid-3333"},
        },
    )
    err = _parse_v3_error_envelope(response, "execute")
    assert isinstance(err, exc.NullRunApprovalDigestMismatchError)
    assert err.status_code == 403
    assert err.error_code == "NR-A013"


def test_parse_v3_error_envelope_dispatches_approval_tool_digest_mismatch():
    """``APPROVAL_TOOL_DIGEST_MISMATCH`` → ``NullRunApprovalToolDigestMismatchError`` —
    tool capability digest drift (T8 / ADR-008), terminal.
    """
    response = httpx.Response(
        403,
        json={
            "error_code": "APPROVAL_TOOL_DIGEST_MISMATCH",
            "error_message": "Tool capability digest drifted",
            "details": {"approval_id": "apr-uuid-4444"},
        },
    )
    err = _parse_v3_error_envelope(response, "execute")
    assert isinstance(err, exc.NullRunApprovalToolDigestMismatchError)
    assert err.status_code == 403
    assert err.error_code == "NR-A014"


def test_parse_v3_error_envelope_dispatches_approval_tool_mismatch():
    """``APPROVAL_TOOL_MISMATCH`` → ``NullRunApprovalToolMismatchError``.

    DEC-5 / DEC-7 (audit 2026-11-02): the approval binds a TOOL SET,
    and the executing tool must be a member. HTTP 409 — a conflict, not
    a permission denial: the approval is valid, it just covers a
    different tool than the one now running.
    """
    response = httpx.Response(
        409,
        json={
            "error_code": "APPROVAL_TOOL_MISMATCH",
            "error_message": "The executing tool is not covered by the operator's approval",
            "details": {"approval_id": "apr-uuid-5555"},
        },
    )
    err = _parse_v3_error_envelope(response, "execute")
    assert isinstance(err, exc.NullRunApprovalToolMismatchError)
    assert err.status_code == 409
    assert err.error_code == "NR-A017"


def test_approval_tool_mismatch_is_not_part_of_the_403_family():
    """``APPROVAL_TOOL_MISMATCH`` is 409, NOT 403.

    The other six grant-consume outcomes are 403 ("you are not allowed
    to do this"). The tool mismatch is a CONFLICT: the operator's
    approval is valid and unconsumed — it simply covers a different
    tool. A cookbook recipe that branches on the status code (403 →
    "permission problem", 409 → "state conflict") must be able to tell
    them apart, so the 409 has to survive the dispatch verbatim.
    """
    response = httpx.Response(
        409,
        json={
            "error_code": "APPROVAL_TOOL_MISMATCH",
            "error_message": "tool not covered",
            "details": {"approval_id": "apr-uuid-6666"},
        },
    )
    err = _parse_v3_error_envelope(response, "execute")
    assert err.status_code == 409, (
        "APPROVAL_TOOL_MISMATCH must dispatch with status_code=409 — the "
        f"approval covers a different tool. Got {err.status_code}."
    )


def test_approval_exceptions_inherit_from_nullrun_blocked_exception():
    """All six typed exceptions inherit from ``NullRunBlockedException``
    so legacy ``except NullRunBlockedException:`` clauses keep
    matching (back-compat invariant).

    Pins the base class so a future refactor that drops the
    inheritance breaks cookbook code that catches the broader class.
    """
    for cls in (
        exc.NullRunApprovalNotYetApprovedError,
        exc.NullRunApprovalDeniedError,
        exc.NullRunApprovalExpiredError,
        exc.NullRunApprovalDigestMismatchError,
        exc.NullRunApprovalToolDigestMismatchError,
        exc.NullRunApprovalReplayRejectedError,
        exc.NullRunApprovalToolMismatchError,
    ):
        assert issubclass(cls, exc.NullRunBlockedException), (
            f"{cls.__name__} must inherit from NullRunBlockedException "
            "so legacy `except NullRunBlockedException:` keeps matching."
        )


def test_approval_exceptions_inherit_from_nullrun_error():
    """All six typed exceptions inherit from ``NullRunError`` so the
    ``except NullRunError:`` catch-all keeps working.
    """
    for cls in (
        exc.NullRunApprovalNotYetApprovedError,
        exc.NullRunApprovalDeniedError,
        exc.NullRunApprovalExpiredError,
        exc.NullRunApprovalDigestMismatchError,
        exc.NullRunApprovalToolDigestMismatchError,
        exc.NullRunApprovalReplayRejectedError,
        exc.NullRunApprovalToolMismatchError,
    ):
        assert issubclass(cls, exc.NullRunError), (
            f"{cls.__name__} must inherit from NullRunError so "
            "`except NullRunError:` catches it."
        )


def test_post_approval_outcomes_use_403_status_code():
    """All six grant-consume outcomes are HTTP 403 on the wire per
    CLAUDE.md §13. The dispatcher preserves ``status_code=403`` on
    the typed exception so FastAPI / Starlette exception handlers
    can map to the right HTTP status without re-deriving from
    ``type(exc).__name__``.
    """
    wire_codes = [
        "APPROVAL_NOT_YET_APPROVED",
        "APPROVAL_DENIED",
        "APPROVAL_EXPIRED",
        "APPROVAL_DIGEST_MISMATCH",
        "APPROVAL_TOOL_DIGEST_MISMATCH",
        "APPROVAL_REPLAY_REJECTED",
    ]
    for wire_code in wire_codes:
        response = httpx.Response(
            403,
            json={
                "error_code": wire_code,
                "error_message": f"test for {wire_code}",
                "details": {"approval_id": "apr-test"},
            },
        )
        err = _parse_v3_error_envelope(response, "execute")
        assert err.status_code == 403, (
            f"{wire_code} must dispatch with status_code=403 per "
            f"CLAUDE.md §13 — got {err.status_code}."
        )


# ─────────────────────────────────────────────────────────────────────
# CLOSE-ORPHAN / ADR-047 (2026-09-21)
# — POST /api/v1/approvals/{approval_id}/consume wire contract
# — Runtime.consume_approval() auto-call from success path
# — _safe_cancel_active_exception auto-call from exception path
# ─────────────────────────────────────────────────────────────────────


class TestConsumeApprovalEndpoint:
    """The new SDK-side consume endpoint mirrors /cancel but is
    structurally distinct per ADR-046 (no execution_id binding).
    C2 closure: organization_id is REQUIRED on the wire so the
    backend can scope the UPDATE without trusting path-only data.
    """

    @respx.mock
    def test_consume_approval_sends_protocol_header(self):
        # Without this header the backend's protocol middleware rejects
        # with 400 + error_code PROTOCOL_HEADER_REQUIRED before the
        # consume SQL runs.
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            route = respx.post(
                f"{BASE_URL}/api/v1/approvals/apr-123/consume"
            ).mock(return_value=Response(200, json={"status": "consumed"}))
            t.consume_approval(
                "apr-123",
                body={"organization_id": "org-1"},
            )
            sent = route.calls.last.request
            assert (
                sent.headers.get("X-NULLRUN-PROTOCOL")
                == str(NULLRUN_PROTOCOL_VERSION)
            ), "consume_approval must carry X-NULLRUN-PROTOCOL header"
        finally:
            t.stop()

    @respx.mock
    def test_consume_approval_sends_organization_id_in_body(self):
        # C2 closure: organization_id is REQUIRED in the body so the
        # backend can scope the UPDATE without trusting path-only data.
        # Without it the backend's ApprovalConsumeRequest deserializer
        # returns 422 missing field 'organization_id'.
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            route = respx.post(
                f"{BASE_URL}/api/v1/approvals/apr-456/consume"
            ).mock(return_value=Response(200, json={"status": "consumed"}))
            t.consume_approval(
                "apr-456",
                body={"organization_id": "org-2", "execution_id": "exec-7"},
            )
            import json as _json

            body = _json.loads(route.calls.last.request.content)
            assert body["organization_id"] == "org-2"
            assert body["execution_id"] == "exec-7"
        finally:
            t.stop()

    @respx.mock
    def test_consume_approval_already_consumed_returns_200(self):
        # Idempotent replay: backend returns 200 + status=already_consumed
        # so the SDK does NOT raise on a double-consume.
        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            respx.post(f"{BASE_URL}/api/v1/approvals/apr-789/consume").mock(
                return_value=Response(
                    200,
                    json={"approval_id": "apr-789", "status": "already_consumed"},
                )
            )
            result = t.consume_approval(
                "apr-789", body={"organization_id": "org-1"}
            )
            assert result["status"] == "already_consumed"
        finally:
            t.stop()

    @respx.mock
    def test_consume_approval_non_2xx_raises_backend_error(self):
        # Auth failure (401) surfaces as NullRunAuthenticationError so
        # callers can branch on the typed exception without losing
        # diagnostic class. (Auth-required is mapped to the typed
        # auth-error subclass, not the generic NullRunBackendError.)
        from nullrun.breaker.exceptions import (
            NullRunAuthenticationError,
        )

        t = Transport(api_url=BASE_URL, api_key="nr_live_abc123")
        try:
            respx.post(f"{BASE_URL}/api/v1/approvals/apr-bad/consume").mock(
                return_value=Response(
                    401,
                    json={"error_code": "AUTH_REQUIRED"},
                )
            )
            with pytest.raises(NullRunAuthenticationError):
                t.consume_approval(
                    "apr-bad", body={"organization_id": "org-1"}
                )
        finally:
            t.stop()


class TestCheckWorkflowBudgetConsumeOnApproved:
    """When WS approval resolves to outcome=approved, the SDK must
    auto-call consume_approval so the row flips to CONSUMED on the
    success path. This closes the inline-mode orphan class.

    The test bypasses the WS-thread plumbing (which would block
    forever in unit tests without a real WS server) and drives
    ``check_workflow_budget`` directly with a require_approval
    response, then injects the WS push via
    ``_handle_approval_resolved`` to release the threading.Event.
    """

    @respx.mock
    def test_outcome_approved_triggers_consume_approval(
        self, make_runtime
    ):
        rt = make_runtime()

        # /gate returns require_approval — SDK must block on WS.
        #
        # `decision_source` / `explanation` / `policy_version` are the
        # three non-`Option` fields on the backend's `GateResponse`
        # (`gate/internal.rs:637-641`), so a real answer always
        # carries them. This fixture omitted all three, which the
        # provenance guard in `runtime.py::_require_gate_decision`
        # correctly rejects as "not a gate answer" — a test body is
        # not evidence of what the wire looks like, and this one had
        # drifted from it.
        respx.post(f"{BASE_URL}/api/v1/gate").mock(
            return_value=Response(
                200,
                json={
                    "decision": "require_approval",
                    "decision_source": "gateway",
                    "explanation": "operator approval required",
                    "policy_version": 1,
                    "approval_id": "apr-success",
                    "execution_id": "exec-success-1",
                    "approval_timeout_seconds": 60,
                },
            )
        )

        # /approvals/{id}/consume must be hit exactly once.
        consume_route = respx.post(
            f"{BASE_URL}/api/v1/approvals/apr-success/consume"
        ).mock(
            return_value=Response(
                200,
                json={"approval_id": "apr-success", "status": "consumed"},
            )
        )

        # Patch _wait_for_approval_resolution to fire the WS push
        # immediately. The original blocks on a threading.Event;
        # here we synthesise the approved outcome via the handler
        # the WS push would normally call, then return the entry.
        def _fake_wait(
            self, approval_id, workflow_id, execution_id, *args, **kwargs
        ):
            self._handle_approval_resolved(
                {
                    "approval_id": approval_id,
                    "execution_id": execution_id,
                    "outcome": "approved",
                }
            )
            # Return the entry shape _wait_for_approval_resolution
            # normally returns, so check_workflow_budget sees
            # outcome=approved.
            with self._approval_lock:
                # After _handle_approval_resolved, _approval_pending
                # is popped, but the entry has been mutated in-place
                # (outcome key set). Return a small dict so the
                # caller can read outcome.
                return {"outcome": "approved", "approval_id": approval_id}

        with patch.object(
            rt.__class__, "_wait_for_approval_resolution", _fake_wait
        ):
            try:
                from nullrun.context import workflow

                with workflow("wf-close-orphan"):
                    rt.check_workflow_budget()
            except Exception:
                pass  # WS-thread plumbing may still be pending

        # The SDK auto-consume must have landed exactly once.
        assert consume_route.call_count >= 1, (
            "check_workflow_budget on outcome=approved must call "
            "/approvals/{id}/consume to close the orphan."
        )


class TestSafeCancelCallsConsumeApproval:
    """_safe_cancel_active_execution must also consume the approval
    row after cancel_execution. Without this, an SDK crash between
    WS approval resolve and body execution leaves the row at
    APPROVED past expires_at — the orphan class that ADR-047
    explicitly closes.
    """

    @respx.mock
    def test_safe_cancel_calls_consume_when_approval_captured(
        self, make_runtime
    ):
        rt = make_runtime()

        # Pre-seed the reverse index: this execution_id has a pending
        # approval that the SDK captured before the exception fired.
        from nullrun.context import set_server_minted_execution_id

        execution_id = "exec-cancel-orphan-1"
        approval_id = "apr-cancel-orphan-1"
        with rt._approval_lock:
            rt._pending_approval_id_by_execution[execution_id] = approval_id

        set_server_minted_execution_id(execution_id)
        # Pin the runtime into the @protect decorator's module slot.
        import nullrun.decorators as _dec

        _dec._runtime = rt

        # Mock /cancel and /approvals/.../consume.
        cancel_route = respx.post(f"{BASE_URL}/api/v1/cancel").mock(
            return_value=Response(
                200,
                json={
                    "execution_id": execution_id,
                    "canceled_at": "2026-09-21T00:00:00Z",
                    "reservation_released_cents": 0,
                    "already_canceled": False,
                },
            )
        )
        consume_route = respx.post(
            f"{BASE_URL}/api/v1/approvals/{approval_id}/consume"
        ).mock(
            return_value=Response(
                200,
                json={"approval_id": approval_id, "status": "consumed"},
            )
        )

        try:
            from nullrun.decorators import _safe_cancel_active_execution

            _safe_cancel_active_execution(reason="exception path test")
        except Exception:
            pass

        assert cancel_route.call_count == 1, (
            "safe_cancel must call /cancel before /consume"
        )
        assert consume_route.call_count == 1, (
            "safe_cancel must call /approvals/{id}/consume after /cancel "
            "to close the orphan grant (ADR-047)."
        )

    @respx.mock
    def test_safe_cancel_skips_consume_when_no_approval_captured(
        self, make_runtime
    ):
        # No reverse-index entry → consume is a no-op. The reverse index
        # is populated only on the outcome=approved branch in
        # check_workflow_budget; if the SDK never reached that branch
        # (crashed earlier, sensitive-tool block, etc.) there is no
        # approval row to close.
        rt = make_runtime()

        from nullrun.context import set_server_minted_execution_id

        execution_id = "exec-no-approval"
        set_server_minted_execution_id(execution_id)
        import nullrun.decorators as _dec

        _dec._runtime = rt

        cancel_route = respx.post(f"{BASE_URL}/api/v1/cancel").mock(
            return_value=Response(
                200,
                json={
                    "execution_id": execution_id,
                    "canceled_at": "2026-09-21T00:00:00Z",
                    "reservation_released_cents": 0,
                    "already_canceled": False,
                },
            )
        )
        consume_route = respx.post(
            f"{BASE_URL}/api/v1/approvals/.+/consume"
        ).mock(
            return_value=Response(
                200,
                json={"approval_id": "x", "status": "consumed"},
            )
        )

        try:
            from nullrun.decorators import _safe_cancel_active_execution

            _safe_cancel_active_execution(reason="no approval captured")
        except Exception:
            pass

        assert cancel_route.call_count == 1
        assert consume_route.call_count == 0, (
            "safe_cancel must skip /consume when no approval_id was "
            "captured for this execution_id — the dict lookup is the "
            "gating check."
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])