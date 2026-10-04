"""A 422 CONSUME_OVERBUDGET that the backend already charged is DELIVERED.

What the wire actually says. ``/track/batch`` settles each event against
the period counter. When a settle lands past ``reserved + epsilon_cents``
the Lua script returns the ``overage`` branch. That branch applies the
real spend to the counter and writes the ``cost_events`` row — the
reservation is NOT re-reserved, and the money IS gone — and the handler
answers 422 with ``details.reservation_recorded: true`` plus the
offending ``event_id``.

What the SDK did with it. ``CONSUME_OVERBUDGET`` is in
``_DETERMINIC_ERROR_CODES``, so the 422 became a
``DeterministicBackendRefusal``: the batch was bisected and the
offending singleton was written to ``.dlq`` with the note "will NOT be
retried". For a refusal where nothing was charged that is right. For a
refusal where the backend has already booked the spend it is a lie in
two directions at once — the event is filed as undelivered when the
server has a row for it, and the operator's only durable record of that
event is a DLQ entry describing a rejection that did not happen.

The rule these tests pin: **the backend's own ``recorded`` marker
decides, and the SDK never second-guesses it.**

* ``reservation_recorded: true``  → delivered. Drop the event from the
  retry path, do not bisect, do not DLQ, and re-queue the *rest* of the
  batch, which was never processed and is genuinely outstanding.
* ``reservation_recorded: false`` or absent → the old quarantine path,
  untouched. A refusal that charged nothing must still park the data.
* No ``details`` at all (an older backend) → fall back to bisecting,
  then apply the same rule to the isolated singleton. The bisect
  converges; the singleton carries the marker.

Also pinned here: the single-``/track`` typed error surfaces ``recorded``
so a caller that catches ``NullRunConsumeOverbudgetError`` can tell
"the server refused and charged you" from "the server refused and left
your reservation alone" — the two demand opposite caller behaviour
(reconcile vs. retry under a higher ceiling), and the SDK docstring
promises the caller the numbers to do the first.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest
import respx

from nullrun.breaker.exceptions import NullRunConsumeOverbudgetError
from nullrun.transport import Transport


@pytest.fixture
def transport(tmp_path, monkeypatch):
    monkeypatch.setenv("NULLRUN_WAL_PATH", str(tmp_path / "sdk.wal"))
    t = Transport(api_url="https://api.test.nullrun.io", api_key="test-key-12345678")
    yield t
    t._client.close()


def _event(i: int) -> dict:
    return {"event_id": f"evt-{i}", "type": "llm_call", "cost_cents": 1}


def _overbudget_body(*, recorded: bool | None, event_id: str | None = "evt-2") -> dict:
    details: dict = {
        "endpoint": "/track/batch",
        "execution_id": "0199-exec-over",
        "reserved_millicents": 1000,
        "actual_cost_cents": 2100,
        "soft_pass": False,
    }
    if recorded is not None:
        details["reservation_recorded"] = recorded
    if event_id is not None:
        details["event_id"] = event_id
    return {
        "error_code": "CONSUME_OVERBUDGET",
        "error_message": "consume exceeds reservation by more than epsilon",
        "details": details,
    }


def _dlq_rows(t: Transport) -> list[dict]:
    path = t._wal_dlq_path()
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _post_422(body: dict):
    return respx.post("https://api.test.nullrun.io/api/v1/track/batch").mock(
        return_value=httpx.Response(422, json=body)
    )


def _post_seq(*responses: httpx.Response):
    return respx.post("https://api.test.nullrun.io/api/v1/track/batch").mock(
        side_effect=list(responses)
    )


def _accepted(*event_ids: str) -> httpx.Response:
    return httpx.Response(
        200, json={"accepted_event_ids": list(event_ids), "rejection_details": []}
    )


# ---------------------------------------------------------------------------
# recorded: the spend is booked. The event is delivered.
# ---------------------------------------------------------------------------


@respx.mock
def test_a_recorded_overage_is_not_dead_lettered(transport):
    """The headline case: 3 events, one overage, and the marker says charged."""
    _post_422(_overbudget_body(recorded=True, event_id="evt-2"))
    transport._buffer.extend([_event(1), _event(2), _event(3)])

    transport._do_flush()

    assert _dlq_rows(transport) == [], "a charged event was filed as undelivered"
    # The other two were never processed — they must still be outstanding.
    assert sorted(e["event_id"] for e in transport._buffer) == ["evt-1", "evt-3"]


@respx.mock
def test_a_recorded_overage_does_not_bisect(transport):
    """Bisecting to isolate a refusal the backend already resolved wastes sends."""
    route = _post_422(_overbudget_body(recorded=True, event_id="evt-2"))
    transport._buffer.extend([_event(1), _event(2), _event(3)])

    transport._do_flush()

    assert route.call_count == 1, "the batch was split even though the body named the offender"


@respx.mock
def test_a_recorded_overage_releases_its_inflight_marker(transport):
    """`.inflight` is the crash-recovery copy. A delivered event must not be replayed."""
    for i in (1, 2, 3):
        transport.track(_event(i))
    _post_422(_overbudget_body(recorded=True, event_id="evt-2"))

    transport._do_flush()

    # evt-1 / evt-3 are re-queued and stay in flight; evt-2 is settled.
    assert "evt-2" not in transport._in_flight
    assert "evt-1" in transport._in_flight


@respx.mock
def test_a_recorded_overage_counts_as_delivered(transport):
    """A metric, because a silently-dropped charged event is invisible otherwise."""
    from nullrun.observability import metrics

    before = getattr(metrics.transport, "events_dead_lettered", 0)
    before_recorded = metrics.transport.events_recorded_overage
    _post_422(_overbudget_body(recorded=True, event_id="evt-2"))
    transport._buffer.extend([_event(1), _event(2), _event(3)])

    transport._do_flush()

    assert getattr(metrics.transport, "events_dead_lettered", 0) == before
    assert metrics.transport.events_recorded_overage == before_recorded + 1


# ---------------------------------------------------------------------------
# recorded: false / absent — the old quarantine path must be untouched.
# ---------------------------------------------------------------------------


@respx.mock
def test_an_unrecorded_overage_is_still_dead_lettered(transport):
    """Nothing was charged, so parking the data is still the only safe move."""
    _post_seq(
        # [1,2,3] → refused
        httpx.Response(422, json=_overbudget_body(recorded=False)),
        # bisect halves the batch as [1] + [2,3]
        _accepted("evt-1"),
        httpx.Response(422, json=_overbudget_body(recorded=False)),
        # [2,3] → bisected as [2] + [3]; the offender lands in the singleton
        httpx.Response(422, json=_overbudget_body(recorded=False)),
        _accepted("evt-3"),
    )
    transport._buffer.extend([_event(1), _event(2), _event(3)])

    transport._do_flush()

    rows = _dlq_rows(transport)
    assert [r["event"]["event_id"] for r in rows] == ["evt-2"]
    assert transport._buffer == []


@respx.mock
def test_a_refusal_with_no_details_at_all_still_dead_letters(transport):
    """An older backend, or a proxy page: no marker means no licence to drop."""
    _post_seq(
        httpx.Response(422, json={"error_code": "CONSUME_OVERBUDGET", "error_message": "over"}),
        _accepted("evt-1"),
        httpx.Response(422, json={"error_code": "CONSUME_OVERBUDGET", "error_message": "over"}),
        httpx.Response(422, json={"error_code": "CONSUME_OVERBUDGET", "error_message": "over"}),
        _accepted("evt-3"),
    )
    transport._buffer.extend([_event(1), _event(2), _event(3)])

    transport._do_flush()

    assert [r["event"]["event_id"] for r in _dlq_rows(transport)] == ["evt-2"]


@respx.mock
def test_a_recorded_marker_on_an_unrelated_code_is_ignored(transport):
    """The rule is scoped to CONSUME_OVERBUDGET; no other 422 grows a bypass."""
    _post_seq(
        httpx.Response(
            422,
            json={
                "error_code": "EXECUTION_NOT_BOUND",
                "error_message": "no such execution",
                "details": {"event_id": "evt-2", "reservation_recorded": True},
            },
        ),
        _accepted("evt-1"),
        httpx.Response(
            422,
            json={
                "error_code": "EXECUTION_NOT_BOUND",
                "error_message": "no such execution",
                "details": {"event_id": "evt-2", "reservation_recorded": True},
            },
        ),
        httpx.Response(
            422,
            json={
                "error_code": "EXECUTION_NOT_BOUND",
                "error_message": "no such execution",
                "details": {"event_id": "evt-2", "reservation_recorded": True},
            },
        ),
        _accepted("evt-3"),
    )
    transport._buffer.extend([_event(1), _event(2), _event(3)])

    transport._do_flush()

    assert [r["event"]["event_id"] for r in _dlq_rows(transport)] == ["evt-2"]


# ---------------------------------------------------------------------------
# The singleton case: a batch of one, and the bisect fallback.
# ---------------------------------------------------------------------------


@respx.mock
def test_a_lone_recorded_overage_is_delivered(transport):
    _post_422(_overbudget_body(recorded=True, event_id="evt-1"))
    transport._buffer.extend([_event(1)])

    transport._do_flush()

    assert _dlq_rows(transport) == []
    assert transport._buffer == []


@respx.mock
def test_a_recorded_overage_found_by_bisecting_is_delivered(transport):
    """No event_id in the body: the cascade isolates it, and the rule then fires."""
    _post_seq(
        # batch of 3 → refused, no event_id to go on
        httpx.Response(422, json=_overbudget_body(recorded=True, event_id=None)),
        # [evt-1, evt-2] → refused
        httpx.Response(422, json=_overbudget_body(recorded=True, event_id=None)),
        _accepted("evt-1"),
        # [evt-2] → refused in isolation, now named
        httpx.Response(422, json=_overbudget_body(recorded=True, event_id="evt-2")),
        _accepted("evt-3"),
    )
    transport._buffer.extend([_event(1), _event(2), _event(3)])

    transport._do_flush()

    assert _dlq_rows(transport) == []
    assert transport._buffer == []


# ---------------------------------------------------------------------------
# The single-/track typed error: `recorded` must reach the caller.
# ---------------------------------------------------------------------------


def test_the_typed_overage_error_carries_the_recorded_marker():
    from nullrun.transport import _parse_v3_error_envelope_uncategorised

    err = _parse_v3_error_envelope_uncategorised(
        httpx.Response(
            422,
            json={
                "error_code": "CONSUME_OVERBUDGET",
                "error_message": "over",
                "details": {"reservation_recorded": True, "execution_id": "exec-1"},
            },
        ),
        "/track",
    )
    assert isinstance(err, NullRunConsumeOverbudgetError)
    assert err.recorded is True


def test_the_typed_overage_error_reports_absence_rather_than_guessing():
    """No marker from an older backend is ``None``, never a silent ``False``."""
    from nullrun.transport import _parse_v3_error_envelope_uncategorised

    err = _parse_v3_error_envelope_uncategorised(
        httpx.Response(
            422,
            json={
                "error_code": "CONSUME_OVERBUDGET",
                "error_message": "over",
                "details": {},
            },
        ),
        "/track",
    )
    assert isinstance(err, NullRunConsumeOverbudgetError)
    assert err.recorded is None
