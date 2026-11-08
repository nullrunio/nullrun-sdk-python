"""SDK-1 (security audit 2026-11-06) — the money-bearing ``/track`` path.

The SDK's v3 single-event ``/track`` carries a CONSUME: it settles the
reservation the matching ``/gate`` made. When that call does not land,
the tool call has ALREADY run and the money has ALREADY been spent —
but nothing booked it. The pre-fix SDK swallowed the failure, logged
"event dropped", and returned ``{"allowed": True}`` to the agent.

Two properties are asserted here, and they are the two halves of the
same invariant: **a consume is either BOOKED or RECOVERABLE, and a
refusal is never reported as a success.**

1. **Recoverable.** Every failure that did not get an answer leaves the
   payload in the WAL, so a restart replays it to ``/api/v1/track``
   (single) and the money lands.
2. **Visible.** When the backend ANSWERED and refused the consume, the
   SDK raises.

Property 2 is the one the pre-fix taxonomy got wrong. The SDK decided
open-vs-closed by exception CLASS, and the class hierarchy puts a
backend refusal (``NullRunBackendError``) under
``NullRunInfrastructureError`` — a SIBLING of ``NullRunDecision`` — so
``isinstance(exc, NullRunDecision)`` could never catch a 503
``RESERVATION_NOT_FOUND``. A later attempt narrowed the rule to three
classes and still dropped the nine backend codes outside them (the
``PROTOCOL_*`` family, the two idempotency codes and the three
``RATE_LIMIT_*`` codes) — and the backend rate-limits ``/track``, so a
429 there was a dropped spend.

The rule now is one question asked of the WIRE: **did the backend
answer?** ADR-008's fail-OPEN covers exactly one thing — the gate could
not be REACHED, so there is no answer to honour and a dead backend must
not freeze the agent loop. Every other case is a refusal.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from httpx import Response

BASE_URL = "https://api.test.nullrun.io"
TRACK_URL = f"{BASE_URL}/api/v1/track"
BATCH_URL = f"{BASE_URL}/api/v1/track/batch"

RESERVATION = "0190c5b5-7c9a-7def-8a1b-0123456789ab"
IDEMPOTENCY_KEY = "9f0c1a44-1d2e-4a3b-8c5d-6e7f8091a2b3"

# The backend's real 422 body (`handlers.rs`, ConsumeOverbudget arm):
# the reservation is in MILLICENTS and the actual cost in cents.
CONSUMED_TOO_MUCH = {
    "error_code": "CONSUME_OVERBUDGET",
    "error_message": "actual > reserved + epsilon",
    "details": {
        "execution_id": RESERVATION,
        "reserved_millicents": 1000,
        "max_allowed_millicents": 1010,
        "actual_cost_cents": 150,
        "soft_pass": False,
        "reservation_recorded": True,
    },
}


@pytest.fixture
def wal_runtime(monkeypatch, tmp_path, make_runtime):
    """A runtime whose WAL lives in ``tmp_path``.

    The WAL path is resolved ONCE, in the Transport constructor, so the
    environment variable has to be in place BEFORE the runtime exists.
    """
    wal = tmp_path / "sdk.wal"
    monkeypatch.setenv("NULLRUN_WAL_PATH", str(wal))
    rt = make_runtime()
    yield rt, Path(f"{wal}.v3")


@pytest.fixture
def reservation():
    """Bind a server-minted reservation and its idempotency key.

    The capture goes through the runtime's own helper, because the
    runtime also captures the MOMENT of the /check: a /track whose
    /check has aged past the reservation TTL is refused, and a capture
    with no timestamp reads as infinitely old.

    The idempotency key is not decoration: the backend DELETEs the
    reservation key after the first successful consume, so a replay
    that arrives without the key is answered 503
    ``RESERVATION_NOT_FOUND`` and the row could never leave the WAL.
    """
    from nullrun.context import (
        clear_server_minted_execution_id,
        reset_operation_id,
        set_operation_id,
    )
    from nullrun.runtime import _capture_server_minted_execution_id

    token_op = set_operation_id(IDEMPOTENCY_KEY)
    _capture_server_minted_execution_id({"reservation_id": RESERVATION})
    yield RESERVATION, IDEMPOTENCY_KEY
    clear_server_minted_execution_id()
    reset_operation_id(token_op)


def _wal_rows(path: Path) -> list[dict]:
    """The rows of the v3 WAL, parsed."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _track(rt, **kwargs) -> None:
    """Send one llm_call through the v3 single-track path."""
    rt.track_llm(
        input_tokens=kwargs.pop("input_tokens", 60),
        output_tokens=kwargs.pop("output_tokens", 40),
        model="claude-sonnet-4-6",
        **kwargs,
    )


class TestARefusalIsNotASuccess:
    """The backend answered and refused. The SDK must not say "allowed"."""

    @respx.mock
    def test_a_503_reservation_not_found_raises(self, wal_runtime, reservation):
        """The audit's named case: a 503 the SDK used to swallow."""
        from nullrun.breaker.exceptions import NullRunBackendError

        rt, _wal = wal_runtime
        respx.post(TRACK_URL).mock(
            return_value=Response(
                503,
                json={
                    "error_code": "RESERVATION_NOT_FOUND",
                    "error_message": "no reservation for this id",
                },
            )
        )

        with pytest.raises(NullRunBackendError):
            _track(rt)

    @respx.mock
    def test_a_429_rate_limit_raises(self, wal_runtime, reservation):
        """The backend rate-limits ``/track``. A 429 here was one of
        the nine codes the pre-fix class list dropped — a live lost
        spend.
        """
        from nullrun.breaker.exceptions import RateLimitError

        rt, _wal = wal_runtime
        respx.post(TRACK_URL).mock(
            return_value=Response(
                429,
                json={
                    "error_code": "RATE_LIMIT_EXCEEDED",
                    "error_message": "too many requests",
                },
                headers={"Retry-After": "1"},
            )
        )

        with pytest.raises(RateLimitError):
            _track(rt)

    @respx.mock
    def test_a_500_internal_error_raises(self, wal_runtime, reservation):
        rt, _wal = wal_runtime
        respx.post(TRACK_URL).mock(
            return_value=Response(
                500,
                json={"error_code": "INTERNAL_ERROR", "error_message": "boom"},
            )
        )

        with pytest.raises(Exception):
            _track(rt)

    @respx.mock
    def test_a_refusal_without_an_error_code_raises(self, wal_runtime, reservation):
        """The body is not the question. The ANSWER is.

        A bare 502 from a proxy names no enforcement failure, but it is
        still an answer, and the consume still did not land. The
        pre-fix rule asked "does this code name an enforcement
        failure?" — that question let a refused consume through
        whenever the body happened to be plain.
        """
        rt, _wal = wal_runtime
        respx.post(TRACK_URL).mock(return_value=Response(502, text="<html>bad gateway</html>"))

        with pytest.raises(Exception):
            _track(rt)


class TestTheRefusalRule:
    """The rule itself, asked of the wire and not of the class."""

    def test_a_stamped_wire_status_is_a_refusal(self):
        from nullrun.breaker.exceptions import NullRunBackendError
        from nullrun.transport import is_backend_refusal

        exc = NullRunBackendError("nope", endpoint="track", status_code=503)
        exc.wire_status_code = 503  # type: ignore[attr-defined]

        assert is_backend_refusal(exc) is True

    def test_a_transport_error_is_not_a_refusal(self):
        """No answer is not an answer. ADR-008's fail-OPEN row."""
        from nullrun.breaker.exceptions import NullRunTransportError
        from nullrun.transport import is_backend_refusal

        exc = NullRunTransportError(
            "network error on /track",
            source=__import__(
                "nullrun.breaker.exceptions", fromlist=["TransportErrorSource"]
            ).TransportErrorSource.NETWORK_ERROR,
            endpoint="track",
        )

        assert is_backend_refusal(exc) is False

    def test_the_nine_codes_the_pre_fix_list_dropped_are_refusals(self):
        """Every code the narrowed class list left out is a refusal.

        The list named three exception classes and left nine backend
        codes outside them. The backend rate-limits ``/track``, so
        three of the nine were live lost spends.
        """
        from nullrun.breaker.exceptions import (
            NullRunAuthError,
            NullRunBackendError,
            NullRunProtocolError,
            NullRunRateLimitRedisError,
            RateLimitError,
        )
        from nullrun.transport import is_backend_refusal

        cases = [
            NullRunProtocolError("bad protocol header"),
            NullRunProtocolError("protocol too new"),
            NullRunBackendError("conflict", endpoint="track", status_code=409),
            NullRunBackendError("key mismatch", endpoint="track", status_code=409),
            RateLimitError(
                "rate limited",
                source=__import__(
                    "nullrun.breaker.exceptions", fromlist=["TransportErrorSource"]
                ).TransportErrorSource.GATEWAY_ERROR,
                endpoint="track",
            ),
            NullRunRateLimitRedisError("limiter redis down"),
            NullRunAuthError("bad key"),
        ]
        for exc in cases:
            exc.wire_status_code = 400  # type: ignore[attr-defined]
            assert is_backend_refusal(exc) is True, exc

    def test_a_decision_and_an_infrastructure_error_are_siblings(self):
        """The fact that made the pre-fix rule unsound.

        ``NullRunDecision`` and ``NullRunInfrastructureError`` are
        SIBLINGS, so ``isinstance(exc, NullRunDecision)`` can never
        catch a backend refusal — no matter how the refusal is
        classified.
        """
        from nullrun.breaker.exceptions import (
            NullRunBackendError,
            NullRunDecision,
            NullRunInfrastructureError,
        )

        assert issubclass(NullRunBackendError, NullRunInfrastructureError)
        assert not issubclass(NullRunInfrastructureError, NullRunDecision)
        assert not issubclass(NullRunDecision, NullRunInfrastructureError)


class TestTheConsumeIsRecoverable:
    """Nothing is dropped. Every undelivered payload is on disk."""

    @respx.mock
    def test_a_transport_failure_leaves_the_payload_in_the_wal(self, wal_runtime, reservation):
        """The audit's named verification: an ``httpx.ConnectError`` on
        ``/track`` must leave the event recoverable in the WAL.
        """
        rt, wal = wal_runtime
        respx.post(TRACK_URL).mock(side_effect=httpx.ConnectError("boom"))

        # A dead backend must not freeze the agent — ADR-008's
        # fail-OPEN row, unchanged.
        _track(rt)

        rows = _wal_rows(wal)
        assert len(rows) == 1, "the payload was dropped"
        assert rows[0]["_nullrun_wal"] == "v3_track"

    @respx.mock
    def test_the_wal_row_carries_the_reservation_and_the_idempotency_key(
        self, wal_runtime, reservation
    ):
        """Verbatim. The replay sends back exactly what was built.

        Without the idempotency key the backend DELETEs the
        reservation key after the first consume, so the replay would be
        answered 503 ``RESERVATION_NOT_FOUND`` and the row could never
        leave the file.

        The row is compared against the bytes that went on the wire,
        not against a constant: the idempotency key is compound with
        the span id by design, so the value that matters is the one
        the backend saw.
        """
        rt, wal = wal_runtime
        route = respx.post(TRACK_URL).mock(side_effect=httpx.ConnectError("boom"))

        _track(rt)

        sent = json.loads(route.calls[0].request.content)
        payload = _wal_rows(wal)[0]["payload"]
        assert payload["reservation_id"] == RESERVATION
        assert payload["tokens"] == 100
        assert payload["idempotency_key"] == sent["idempotency_key"]
        assert payload["idempotency_key"].startswith(IDEMPOTENCY_KEY)

    @respx.mock
    def test_a_refusal_leaves_the_payload_in_the_wal(self, wal_runtime, reservation):
        """A refusal is raised AND recoverable.

        The backend answered, so the consume did not land, so the
        payload is the only copy of an unbooked spend. Raising tells
        the operator; the WAL tells the next process.
        """
        rt, wal = wal_runtime
        respx.post(TRACK_URL).mock(
            return_value=Response(
                503,
                json={"error_code": "RESERVATION_NOT_FOUND", "error_message": "no"},
            )
        )

        with pytest.raises(Exception):
            _track(rt)

        assert len(_wal_rows(wal)) == 1

    @respx.mock
    def test_a_verdict_is_not_persisted(self, wal_runtime, reservation):
        """A ``NullRunDecision`` is the backend's ANSWER, not a failure.

        Re-sending a verdict produces the same verdict, so a row for
        it could never leave the WAL — it would grow the file forever
        and prove nothing. The consume it refuses was ACCOUNTED for
        (the backend charges the overbudget cost), so there is no
        unbooked spend to recover.
        """
        from nullrun.breaker.exceptions import NullRunConsumeOverbudgetError

        rt, wal = wal_runtime
        respx.post(TRACK_URL).mock(return_value=Response(422, json=CONSUMED_TOO_MUCH))

        with pytest.raises(NullRunConsumeOverbudgetError):
            _track(rt)

        assert _wal_rows(wal) == []

    @respx.mock
    def test_a_successful_track_leaves_no_row(self, wal_runtime, reservation):
        rt, wal = wal_runtime
        respx.post(TRACK_URL).mock(return_value=Response(200, json={"ok": True}))

        _track(rt)

        assert _wal_rows(wal) == []


class TestTheReplayRoute:
    """A restart replays to the SINGLE endpoint.

    The batch endpoint does not run the CONSUME script: a reservation
    replayed through it is never settled and the money is never
    booked.
    """

    @respx.mock
    def test_the_replay_posts_to_the_single_track_endpoint(self, wal_runtime, reservation):
        rt, wal = wal_runtime
        route = respx.post(TRACK_URL).mock(side_effect=httpx.ConnectError("boom"))
        _track(rt)
        assert len(_wal_rows(wal)) == 1

        # The next process starts and replays. The route is the SAME
        # respx route (one pattern, one route), so the calls before
        # the replay are counted off before it starts.
        route.mock(return_value=Response(200, json={"idempotent_replay": True}))
        before = len(route.calls)
        rt._transport._replay_v3_from_wal()

        replayed = route.calls[before:]
        assert len(replayed) == 1, "the replay did not reach /api/v1/track"
        assert replayed[0].request.url.path == "/api/v1/track"
        sent = json.loads(replayed[0].request.content)
        assert sent["reservation_id"] == RESERVATION
        assert sent["idempotency_key"].startswith(IDEMPOTENCY_KEY)

        # The row cleared: the spend is booked, not pending.
        assert _wal_rows(wal) == []

    @respx.mock
    def test_the_replay_never_uses_the_batch_endpoint(self, wal_runtime, reservation):
        rt, wal = wal_runtime
        respx.post(TRACK_URL).mock(side_effect=httpx.ConnectError("boom"))
        _track(rt)

        batch = respx.post(BATCH_URL).mock(return_value=Response(200, json={}))
        rt._transport._replay_v3_from_wal()

        assert not batch.called, "the batch endpoint does not settle a reservation"

    @respx.mock
    def test_a_refused_row_stays_and_does_not_block_the_rows_behind_it(
        self, wal_runtime, reservation
    ):
        """One stuck row must not block every spend behind it.

        The rows are independent reservations. The refusal is about
        this consume, not about the backend, so the next row still
        gets its chance.
        """
        rt, wal = wal_runtime

        def _answer(request):
            body = json.loads(request.content)
            if body.get("reservation_id") == RESERVATION:
                return Response(
                    503,
                    json={
                        "error_code": "RESERVATION_NOT_FOUND",
                        "error_message": "no",
                    },
                )
            return Response(200, json={"idempotent_replay": True})

        route = respx.post(TRACK_URL).mock(side_effect=_answer)
        with pytest.raises(Exception):
            _track(rt)

        # A second, different spend lands behind the stuck one.
        rt._transport.persist_v3_track(
            {
                "reservation_id": "0190c5b5-7c9a-7def-8a1b-000000000000",
                "workflow_id": "wf-1",
                "tokens": 10,
                "idempotency_key": "key-2",
            }
        )

        before = len(route.calls)
        rt._transport._replay_v3_from_wal()

        assert len(route.calls) - before == 2, "the stuck row blocked the row behind it"
        rows = _wal_rows(wal)
        assert len(rows) == 1
        assert rows[0]["payload"]["reservation_id"] == RESERVATION


class TestTheWalPathIsFixed:
    def test_the_path_does_not_move_while_the_transport_lives(
        self, monkeypatch, tmp_path, make_runtime
    ):
        """The path is resolved ONCE, in the constructor.

        A path that moves mid-life would split one agent's WAL across
        two files, and the recovery would find neither half.
        """
        wal = tmp_path / "sdk.wal"
        monkeypatch.setenv("NULLRUN_WAL_PATH", str(wal))
        rt = make_runtime()

        before = rt._transport._wal_path()
        rt._transport.persist_v3_track(
            {"reservation_id": RESERVATION, "workflow_id": "wf-1", "tokens": 1}
        )
        after = rt._transport._wal_path()

        assert before == after
        assert Path(f"{wal}.v3").exists()
