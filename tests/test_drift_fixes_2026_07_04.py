"""
Contract tests for the 2026-07-04 fixes.

Background
----------
 (NULLRUN/, 2026-07-04) flagged three real
SDK gaps whose wire effect was observable to customers:

  F1 / open Q4: /track v3 single-event
      payload did NOT carry a wire ``idempotency_key``. Backend
      (handlers.rs:4654-4725) supports replay on hit, but
      without the field the SDK's transport-level retry either
      re-ran CONSUME_SCRIPT (→ 503 ``RESERVATION_NOT_FOUND``)
      or double-billed. Fix: ``_capture_server_minted_execution_id``
      now captures ``operation_id`` from the /check response
      into a contextvar (``get_server_minted_idempotency_key``)
      ``_enrich_event`` stamps it on the wire_event, and
      ``_build_v3_track_payload`` propagates it onto the v3
      /track payload.

  F2: NR-B004 → 402 not 429. The wire envelope
      parser preserved the HTTP status on ``NullRunBackendError``
      but not on ``NullRunBudgetError`` /
      ``NullRunWorkflowInactiveError`` /
      ``NullRunChainError`` /
      ``NullRunConsumeOverbudgetError``. FastAPI exception
      handlers reading ``exc.status_code`` would fall back to 500
      (or None). Fix: each class now accepts ``status_code`` and
      ``_parse_v3_error_envelope`` populates it from
      ``response.status_code``.

  F3: SDK_README "Fail-OPEN на инфраструктурных
      сбоях" is half-wrong. The honest split (now in the
      runtime module-top docstring):
        * SDK-side transport error (network/5xx/breaker open):
          /check path is fail-OPEN, /track legacy path drops.
        * Wire 4xx/5xx that names an enforcement failure
          (``BUDGET_REDIS_UNAVAILABLE``, ``RATE_LIMIT_REDIS_UNAVAILABLE``):
          fail-CLOSED on the SDK side — the exception is
          raised exactly as the backend returned it.

This file pins each fix with focused unit tests so future
refactors trip CI rather than silently re-introducing the
drift.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
import respx
from httpx import Response

from nullrun import context as nullrun_context
from nullrun.breaker.exceptions import (
    NullRunBudgetError,
    NullRunChainError,
    NullRunConsumeOverbudgetError,
    NullRunWorkflowInactiveError,
)

# ---------------------------------------------------------------------------
# F1: wire idempotency_key propagation
# ---------------------------------------------------------------------------

class TestIdempotencyKeyOnTrackPayload:
    """F1: /track v3 single-event carries the
    /check operation_id as the wire ``idempotency_key`` so the
    backend's replay branch returns 200 + ``idempotent_replay:
    true`` on hit.
    """

    def setup_method(self) -> None:
        # Defensive: clear any leftover capture between tests so
        # assertions aren't poisoned by an earlier /check mock.
        nullrun_context.clear_server_minted_execution_id()

    def teardown_method(self) -> None:
        nullrun_context.clear_server_minted_execution_id()

    def test_idempotency_key_captured_from_check_response(self):
        """``_capture_server_minted_execution_id`` should now also
        read ``response["operation_id"]`` and store it via
        ``set_server_minted_idempotency_key``.
        """
        from nullrun.runtime import _capture_server_minted_execution_id

        captured = _capture_server_minted_execution_id(
            {
                "reservation_id": "01926e7a-3b3b-7ddd-9bdd-7f0d3b3b7b3b",
                "operation_id": "11111111-2222-3333-4444-555555555555",
            }
        )

        assert captured == "01926e7a-3b3b-7ddd-9bdd-7f0d3b3b7b3b"
        assert (
            nullrun_context.get_server_minted_idempotency_key()
            == "11111111-2222-3333-4444-555555555555"
        )

    def test_idempotency_key_missing_when_operation_id_absent(self):
        """Backward compat: legacy /check responses without
        ``operation_id`` should leave the contextvar at None —
        ``_build_v3_track_payload`` then omits the field on the
        wire.
        """
        from nullrun.runtime import _capture_server_minted_execution_id

        _capture_server_minted_execution_id(
            {
                "reservation_id": "01926e7a-3b3b-7ddd-9bdd-7f0d3b3b7b3b",
            }
        )

        assert nullrun_context.get_server_minted_execution_id() is not None
        assert nullrun_context.get_server_minted_idempotency_key() is None

    def test_clear_drops_idempotency_key(self):
        """``clear_server_minted_execution_id`` must also clear the
        idempotency_key (symmetric lifetime — ).
        """
        from nullrun.runtime import _capture_server_minted_execution_id

        _capture_server_minted_execution_id(
            {
                "reservation_id": "01926e7a-3b3b-7ddd-9bdd-7f0d3b3b7b3b",
                "operation_id": "abcdef00-0000-0000-0000-000000000000",
            }
        )
        assert nullrun_context.get_server_minted_idempotency_key() is not None

        nullrun_context.clear_server_minted_execution_id()
        assert nullrun_context.get_server_minted_idempotency_key() is None

    def test_build_v3_track_payload_includes_idempotency_key(self):
        """The v3 /track payload mapper must surface the captured
        idempotency_key on the wire_event so /track can carry the
        same anchor as the matching /check.
        """
        from nullrun.runtime import _build_v3_track_payload

        nullrun_context._server_minted_idempotency_key_var.set(
            "11111111-2222-3333-4444-555555555555"
        )

        try:
            payload = _build_v3_track_payload(
                {
                    "workflow_id": "wf-123",
                    "tokens": 100,
                    "model": "claude-sonnet-4-6",
                },
                "01926e7a-3b3b-7ddd-9bdd-7f0d3b3b7b3b",
            )
        finally:
            nullrun_context.clear_server_minted_execution_id()

        assert payload is not None
        assert (
            payload["idempotency_key"]
            == "11111111-2222-3333-4444-555555555555"
        )
        # Sanity: the rest of the v3 payload shape is preserved.
        assert (
            payload["reservation_id"]
            == "01926e7a-3b3b-7ddd-9bdd-7f0d3b3b7b3b"
        )
        assert payload["workflow_id"] == "wf-123"
        assert payload["tokens"] == 100

    def test_build_v3_track_payload_omits_idempotency_key_when_absent(
        self,
    ):
        """Backward compat: when no /check ran (legacy / track-by-batch
        fall-through), the field must be absent (not an empty
        string — that would set a stale anchor on the backend).
        """
        from nullrun.runtime import _build_v3_track_payload

        nullrun_context._server_minted_idempotency_key_var.set(None)

        payload = _build_v3_track_payload(
            {
                "workflow_id": "wf-123",
                "tokens": 100,
            },
            "01926e7a-3b3b-7ddd-9bdd-7f0d3b3b7b3b",
        )

        assert payload is not None
        assert "idempotency_key" not in payload


# ---------------------------------------------------------------------------
# F2: HTTP status_code on every decision exception
# ---------------------------------------------------------------------------

class TestStatusCodeOnExceptions:
    """F2: the wire envelope parser preserves
    ``response.status_code`` on every decision exception so FastAPI
    exception handlers reading ``exc.status_code`` don't fall back
    to 500.
    """

    def _build_envelope(self, error_code: str, body_extra: dict | None = None) -> dict:
        body: dict = {
            "error_code": error_code,
            "error_message": f"synthetic {error_code}",
            "details": body_extra or {"workflow_id": "wf-123"},
            "retry_after_ms": None,
        }
        return body

    def _raise_via_parser(
        self, error_code: str, status: int, body_extra: dict | None = None
    ):
        """Drive ``_parse_v3_error_envelope`` through a synthetic
        httpx.Response — the real path the transport uses.
        """
        from nullrun.transport import _parse_v3_error_envelope

        body = self._build_envelope(error_code, body_extra)
        response = Response(
            status_code=status,
            content=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        return _parse_v3_error_envelope(response, endpoint="check")

    def test_budget_hard_blocked_preserves_402(self):
        exc = self._raise_via_parser("BUDGET_HARD_BLOCKED", 402)
        assert isinstance(exc, NullRunBudgetError)
        assert exc.status_code == 402

    def test_budget_soft_blocked_preserves_402(self):
        exc = self._raise_via_parser("BUDGET_SOFT_BLOCKED", 402)
        assert isinstance(exc, NullRunBudgetError)
        assert exc.status_code == 402

    def test_budget_overdraft_exceeded_preserves_402(self):
        exc = self._raise_via_parser("BUDGET_OVERDRAFT_EXCEEDED", 402)
        assert isinstance(exc, NullRunBudgetError)
        assert exc.status_code == 402

    def test_redis_unavailable_preserves_402(self):
        """BUDGET_REDIS_UNAVAILABLE is fail-CLOSED on the wire
 — the SDK raises exactly as the backend
        returned it (P1-2 honesty).
        """
        exc = self._raise_via_parser("REDIS_UNAVAILABLE", 402)
        assert isinstance(exc, NullRunBudgetError)
        assert exc.status_code == 402

    def test_workflow_inactive_preserves_403(self):
        exc = self._raise_via_parser(
            "WORKFLOW_INACTIVE", 403, body_extra={"workflow_id": "wf-abc"}
        )
        assert isinstance(exc, NullRunWorkflowInactiveError)
        assert exc.status_code == 403

    def test_chain_cross_org_preserves_403(self):
        exc = self._raise_via_parser(
            "CHAIN_CROSS_ORG", 403, body_extra={"chain_id": "c-1"}
        )
        assert isinstance(exc, NullRunChainError)
        assert exc.status_code == 403

    def test_chain_max_duration_preserves_402(self):
        exc = self._raise_via_parser(
            "CHAIN_MAX_DURATION_EXCEEDED", 402, body_extra={"chain_id": "c-1"}
        )
        assert isinstance(exc, NullRunChainError)
        assert exc.status_code == 402

    def test_consume_overbudget_preserves_422(self):
        exc = self._raise_via_parser(
            "CONSUME_OVERBUDGET",
            422,
            body_extra={
                "execution_id": "ex-1",
                "reserved_cents": 10,
                "max_allowed_cents": 11,
                "actual_cost_cents": 100,
                "epsilon_cents": 1,
            },
        )
        assert isinstance(exc, NullRunConsumeOverbudgetError)
        assert exc.status_code == 422


# ---------------------------------------------------------------------------
# F3: fail-CLOSED / fail-OPEN honesty
# ---------------------------------------------------------------------------

class TestFailClosedHonesty:
    """F3: the SDK reads backend enforcement
    responses as fail-CLOSED even when they're named with the word
    "Redis" — wire 4xx/5xx that names an enforcement failure must
    NOT be silently treated as a transport blip.
    """

    def test_redis_unavailable_is_fail_closed_402(self):
        """``REDIS_UNAVAILABLE`` / ``BUDGET_REDIS_UNAVAILABLE`` →
        NullRunBudgetError (fail-CLOSED). The SDK must not turn
        this into a silent ALLOW — explicitly
        flagged the SDK_README claim that contradicted this.
        """
        from nullrun.transport import _parse_v3_error_envelope

        response = Response(
            status_code=402,
            content=json.dumps(
                {
                    "error_code": "REDIS_UNAVAILABLE",
                    "error_message": "Redis unreachable for budget counter",
                    "details": {"workflow_id": "wf-1"},
                    "retry_after_ms": None,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

        exc = _parse_v3_error_envelope(response, endpoint="check")
        assert isinstance(exc, NullRunBudgetError)
        # Fail-CLOSED: the SDK raised the exception, it did NOT
        # silently return a soft allow to the caller. status_code
        # is preserved so the caller's HTTP layer sees 402.
        assert exc.status_code == 402
        assert exc.retryable is False

    def test_rate_limit_redis_unavailable_is_fail_closed_503(self):
        """``RATE_LIMIT_REDIS_UNAVAILABLE`` → NullRunRateLimitRedisError
        (fail-CLOSED per — aggregate rate limit is
        the authoritative gate)."""
        from nullrun.breaker.exceptions import NullRunRateLimitRedisError
        from nullrun.transport import _parse_v3_error_envelope

        response = Response(
            status_code=503,
            content=json.dumps(
                {
                    "error_code": "RATE_LIMIT_REDIS_UNAVAILABLE",
                    "error_message": "Redis unreachable for aggregate rate limit",
                    "details": {},
                    "retry_after_ms": None,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

        exc = _parse_v3_error_envelope(response, endpoint="check")
        assert isinstance(exc, NullRunRateLimitRedisError)
        # Fail-CLOSED: the SDK raised, no silent allow.
        assert exc.retryable is True