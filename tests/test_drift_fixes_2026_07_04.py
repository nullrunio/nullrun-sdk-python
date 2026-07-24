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

    def test_build_v3_track_payload_includes_parent_trace_id(self):
        """2026-07-12 (multi-agent span attachment): the v3 /track
        payload mapper must surface ``parent_trace_id`` on the wire
        when the enriched event carries it. Without this the backend's
        ``cost_events.parent_trace_id`` column stays NULL and the
        unified SELECT's third JOIN arm (``cs.join_kind =
        'parent_trace_id'``) misses the row — the dashboard falls
        back to the weaker ``trace_id`` arm and the workflow detail
        "Recent executions" panel shows empty Model / Tokens / Cost
        on the orchestration row that owns the LLM call.
        """
        from nullrun.runtime import _build_v3_track_payload

        payload = _build_v3_track_payload(
            {
                "workflow_id": "wf-123",
                "tokens": 100,
                "trace_id": "11111111-2222-3333-4444-555555555555",
                "span_id": "22222222-3333-4444-5555-666666666666",
                "parent_trace_id": "33333333-4444-5555-6666-777777777777",
            },
            "01926e7a-3b3b-7ddd-9bdd-7f0d3b3b7b3b",
        )

        assert payload is not None
        assert payload["parent_trace_id"] == "33333333-4444-5555-6666-777777777777"
        # Sanity: existing fields still surface.
        assert payload["trace_id"] == "11111111-2222-3333-4444-555555555555"
        assert payload["span_id"] == "22222222-3333-4444-5555-666666666666"

    def test_build_v3_track_payload_omits_parent_trace_id_when_absent(self):
        """Backward compat: when no parent chain / agent context is
        active (single-shot /track outside @protect), the field must
        be absent — not an empty string. Backend stores ``None`` /
        missing-field identically, so the omission is the right
        shape for the "no parent" case.
        """
        from nullrun.runtime import _build_v3_track_payload

        payload = _build_v3_track_payload(
            {
                "workflow_id": "wf-123",
                "tokens": 100,
                "trace_id": "11111111-2222-3333-4444-555555555555",
            },
            "01926e7a-3b3b-7ddd-9bdd-7f0d3b3b7b3b",
        )

        assert payload is not None
        assert "parent_trace_id" not in payload
        assert payload["trace_id"] == "11111111-2222-3333-4444-555555555555"

    def test_enrich_event_stamps_parent_trace_id_from_contextvar(self):
        """When the caller did not pass ``parent_trace_id`` explicitly
        on the event dict (e.g. plain httpx transport that does NOT
        go through ``langgraph.py::on_llm_end``), ``_enrich_event``
        must stamp the field from the active span contextvar so the
        wire shape is consistent regardless of caller integration.
        """
        from nullrun.context import clear_trace_id, set_trace_id
        from nullrun.runtime import NullRunRuntime

        # Pin the trace contextvar to a known value (mimics
        # ``@protect`` block / chain mode).
        set_trace_id("44444444-5555-6666-7777-888888888888")
        try:
            rt = NullRunRuntime(api_key="test-key-12345678", _test_mode=True)
            enriched = rt._enrich_event(
                {"type": "llm_call", "model": "gpt-4", "tokens": 100}
            )
            assert enriched["parent_trace_id"] == (
                "44444444-5555-6666-7777-888888888888"
            )
        finally:
            clear_trace_id()

    def test_enrich_event_contextvar_overrides_caller_set_parent_trace_id(self):
        """Hotfix #2 (2026-07-12): chain contextvar ALWAYS wins
        over caller-set parent_trace_id.

        Why override: the pre-hotfix code only filled the field
        when it was absent from the event dict, which broke when
        ``langgraph.py::on_llm_end``'s ``_active_runs[run_id]``
        lookup missed (run_id drift between the auto-injected
        chat_model callback and an explicit user-supplied one,
        or no matching ``on_llm_start`` because the user wrapped
        the LLM call in a non-langgraph stack). In that case
        ``on_llm_end`` leaves the field absent, the ``trace_id``
        fallback (line 2422) overwrites the event with the chain
        contextvar, but ``parent_trace_id`` stayed NULL because
        the previous condition was skipped.

        Override semantics: the chain contextvar is the single
        source of truth for "what chain does this event belong
        to". Both the langgraph callback's caller-set value AND
        a non-langgraph caller's absence resolve to the same
        contextvar; preferring the contextvar when present is
        idempotent for the happy path AND closes the drift in
        the unhappy path.

        See PR #64 hotfix #2 / diagnostic run 2026-07-12 08:51
        for the full regression context (sdk_diag.py output:
        trace_id=cccccccc-... parent_trace_id=NULL on backend
        cost_events).
        """
        from nullrun.context import clear_trace_id, set_trace_id
        from nullrun.runtime import NullRunRuntime

        # Contextvar holds the chain's trace. Even though the
        # event dict has a caller-set parent_trace_id, the
        # hotfix overrides it with the contextvar.
        set_trace_id("55555555-6666-7777-8888-999999999999")
        try:
            rt = NullRunRuntime(api_key="test-key-12345678", _test_mode=True)
            enriched = rt._enrich_event(
                {
                    "type": "llm_call",
                    "model": "gpt-4",
                    "tokens": 100,
                    "parent_trace_id": "explicit-from-callback",
                }
            )
            # Contextvar WINS over caller-set (hotfix #2).
            assert (
                enriched["parent_trace_id"]
                == "55555555-6666-7777-8888-999999999999"
            ), (
                f"contextvar must override caller-set parent_trace_id "
                f"(hotfix #2): got {enriched['parent_trace_id']!r}"
            )
        finally:
            clear_trace_id()

    def test_enrich_event_leaves_parent_trace_id_blank_when_no_contextvar(
        self,
    ):
        """Backward compat: legacy / pre-0.13.6 callers run with no
        ``@protect`` block and no chain contextvar set. In that case
        ``parent_trace_id`` MUST stay absent — never pick up a stale
        value from a previous test, never default to ``trace_id``
        (the backend's JOIN keys off the explicit value, not the
        trace_id column).
        """
        from nullrun.context import clear_trace_id, set_trace_id
        from nullrun.runtime import NullRunRuntime

        clear_trace_id()  # belt + braces
        try:
            set_trace_id(None)
        except Exception:
            pass
        try:
            clear_trace_id()
        except Exception:
            pass

        rt = NullRunRuntime(api_key="test-key-12345678", _test_mode=True)
        enriched = rt._enrich_event(
            {"type": "llm_call", "model": "gpt-4", "tokens": 100}
        )
        assert "parent_trace_id" not in enriched

    def test_enrich_event_omits_empty_string_parent_trace_id(self):
        """Empty string ``""`` is a falsy ``parent_trace_id``. Treat
        it like None so the wire payload stays clean (backend
        parser would otherwise reject the field or store empty
        string in a UUID column, depending on path).
        """
        from nullrun.context import clear_trace_id, set_trace_id
        from nullrun.runtime import NullRunRuntime

        set_trace_id("")  # boundary value
        try:
            rt = NullRunRuntime(api_key="test-key-12345678", _test_mode=True)
            enriched = rt._enrich_event(
                {"type": "llm_call", "model": "gpt-4", "tokens": 100}
            )
            # The contextvar was set to empty string; ``_enrich_event``
            # branches on truthy value, so the field is absent
            # (not propagated as empty string).
            assert "parent_trace_id" not in enriched
        finally:
            clear_trace_id()

    def test_enrich_event_parent_trace_id_matches_existing_trace_id_field(
        self,
    ):
        """Invariant (see SpanContext): a child span inherits
        ``trace_id`` from its parent and only differs in
        ``span_id``. When the contextvar is set, ``parent_trace_id``
        and ``trace_id`` MUST point at the same value. This protects
        the backend's JOIN from drifting — see
        ``db/mod.rs::get_execution_records_for_workflow``.
        """
        from nullrun.context import clear_trace_id, set_trace_id
        from nullrun.runtime import NullRunRuntime

        set_trace_id("77777777-8888-9999-aaaa-bbbbbbbbbbbb")
        try:
            rt = NullRunRuntime(api_key="test-key-12345678", _test_mode=True)
            enriched = rt._enrich_event(
                {"type": "llm_call", "model": "gpt-4", "tokens": 100}
            )
            assert enriched["trace_id"] == enriched["parent_trace_id"]
        finally:
            clear_trace_id()


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


class TestEnrichEventParentTraceOverride:
    """Hotfix #2: the chain contextvar ALWAYS wins over caller-set
    parent_trace_id. Regression coverage for the drift bug where
    cost_events.parent_trace_id stayed NULL even though
    cost_events.trace_id carried the chain contextvar (chain
    contextvar was honored for trace_id via the fallback at line
    2422, but parent_trace_id's "if not in enriched" condition was
    skipped when the event arrived without the field set).
    """

    def test_enrich_event_sets_parent_trace_id_when_chain_contextvar_set(self):
        """Real-world drift scenario: SDK runtime.track() called
        with no parent_trace_id field, chain contextvar set.
        Pre-hotfix: parent_trace_id stays absent. Post-hotfix: it
        is set to the chain contextvar.

        This is the path that produced trace_id=cccccccc-... /
        parent_trace_id=NULL on the prod VPS during the diagnostic
        run on 2026-07-12 08:51 UTC.
        """
        from nullrun.context import clear_trace_id, set_trace_id
        from nullrun.runtime import NullRunRuntime
        set_trace_id("cccccccc-1111-2222-3333-444444444444")
        try:
            rt = NullRunRuntime(api_key="test-key-12345678", _test_mode=True)
            # Event WITHOUT parent_trace_id field at all.
            enriched = rt._enrich_event(
                {
                    "type": "llm_call",
                    "model": "gpt-4",
                    "tokens": 100,
                }
            )
            assert (
                enriched["parent_trace_id"]
                == "cccccccc-1111-2222-3333-444444444444"
            ), (
                f"parent_trace_id MUST be stamped from chain contextvar "
                f"even when caller did not set it: got "
                f"{enriched.get('parent_trace_id')!r}"
            )
            # Sanity: trace_id also comes from the same contextvar.
            assert enriched["trace_id"] == "cccccccc-1111-2222-3333-444444444444"
        finally:
            clear_trace_id()

    def test_enrich_event_parent_trace_id_matches_trace_id_in_chain_mode(self):
        """SpanContext invariant: parent_trace_id == trace_id when
        the event sits inside the chain contextvar (chain trace
        spans share the same trace_id across child spans).
        """
        from nullrun.context import clear_trace_id, set_trace_id
        from nullrun.runtime import NullRunRuntime
        set_trace_id("99999999-aaaa-bbbb-cccc-000000000000")
        try:
            rt = NullRunRuntime(api_key="test-key-12345678", _test_mode=True)
            enriched = rt._enrich_event(
                {
                    "type": "llm_call",
                    "model": "gpt-4",
                    "tokens": 100,
                }
            )
            assert enriched["parent_trace_id"] == enriched["trace_id"], (
                f"parent_trace_id should equal trace_id when chain "
                f"contextvar is the source: parent={enriched.get('parent_trace_id')!r}, "
                f"trace={enriched.get('trace_id')!r}"
            )
        finally:
            clear_trace_id()



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