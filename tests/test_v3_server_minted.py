"""
Contract tests for the v3 server-minted execution_id wiring
(CLAUDE.md §24, §29).

Background
----------
Pre-0.12.0 the SDK read ``decision`` + ``decision_source`` from
the /check response and IGNORED ``reservation_id``, the
server-minted uuidv7 the backend's ``gate_reserve_v3`` writes
to ``reservation:{execution_id}`` (TTL 300s) and surfaces on
``GateResponse.reservation_id``. Without the round-trip:

  - /track had no way to find the matching reservation key →
    v3 ``consume_budget_v3`` rejected with 503
    ``RESERVATION_NOT_FOUND`` (CLAUDE.md §33, fail-CLOSED).
  - /track kept using the legacy ``/api/v1/track/batch``
    path that writes to ``monthly_cost`` (drift with the
    dashboard's period counter, see §0 G1).

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
``tests/test_v3_wire_contract.py`` — same respx-based pattern,
strict-URL assertions, no live backend required.
"""

from __future__ import annotations

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
# backend's mint_execution_id (RFC 9562 §5.7 — version nibble
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
    ``set_server_minted_execution_id(value) -> Token``,
    ``get_server_minted_execution_id() -> str | None``,
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
        # Pre-populate to verify clear() actually clears.
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
        # Backend is the source of truth and only emits strings,
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
    past the 300s TTL (§29).
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
        # cookbook scenario. Force int() so wire is int.
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

        # track() buffers; tool_call events don't trip the v3
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

        # Drive /gate (which captures) ...
        from nullrun.context import workflow
        with workflow("wf-1"):
            rt.check_workflow_budget()

            # ... then drive /track within the same scope.
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
            # BaseException (carries the kill signal; per CLAUDE.md
            # §3 must propagate honestly). Catch it explicitly for
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
