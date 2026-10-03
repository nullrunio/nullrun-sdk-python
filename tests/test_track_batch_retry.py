"""
tests/test_track_batch_retry.py — regression coverage for P0 #2.

Pre-fix, _send_batch_with_retry_info issued a single self._client.post(...)
and immediately called raise_for_status. A backend 500 raised out of the
flush path; the in-memory buffer was cleared at the call site and every
event in the batch was lost. P0 #2 wraps the post in _retry_with_backoff
so a transient 5xx is retried (max 3 attempts, exponential backoff +
jitter, capped at 10s). 429s are also retried (the helper honors
Retry-After when present).

These tests pin the new contract:

* a single 5xx followed by 200 — batch is accepted, only one event-loss
  is observable by the caller.
* three consecutive 5xx — final call raises after exhausting retries
  the caller learns the batch was lost (acceptable: backend confirmed
  it could not accept).
* 429 with Retry-After — helper honors the header before the next
  attempt (we assert call count, not exact delay).
"""

from __future__ import annotations

import threading

import httpx
import pytest
import respx

from nullrun.breaker.exceptions import BreakerTransportError
from nullrun.observability import metrics
from nullrun.transport import Transport, _retry_with_backoff


@pytest.fixture
def transport():
    # Tighter retry params so tests run fast.
    t = Transport(api_url="https://api.test.nullrun.io", api_key="test-key-12345678")
    # Shorten the per-attempt delay to keep the suite snappy.
    t._track_max_retries = 3
    t._track_base_delay = 0.0
    t._track_max_delay = 0.0
    yield t
    t.stop()


class TestTrackBatchRetry:
    @respx.mock
    def test_single_5xx_then_200_eventually_succeeds(self, transport):
        route = respx.post("https://api.test.nullrun.io/api/v1/track/batch").mock(
            side_effect=[
                httpx.Response(500, json={"error": "internal"}),
                httpx.Response(200, json={"accepted_event_ids": ["e1"]}),
            ]
        )
        result = transport._send_batch_with_retry_info([{"event": "e1"}])
        assert route.call_count == 2
        assert "e1" in result.accepted_event_ids

    @respx.mock
    def test_three_consecutive_5xx_raises_after_retries(self, transport):
        route = respx.post("https://api.test.nullrun.io/api/v1/track/batch").mock(
            return_value=httpx.Response(500, json={"error": "boom"})
        )
        # _retry_with_backoff wraps the underlying HTTPStatusError into
        # BreakerTransportError so the caller can match a single exception
        # type without distinguishing 4xx vs 5xx vs network.
        with pytest.raises(BreakerTransportError):
            transport._send_batch_with_retry_info([{"event": "e1"}])
        # 1 initial + 3 retries = 4 total
        assert route.call_count == 4

    @respx.mock
    def test_429_is_retried_then_succeeds(self, transport):
        route = respx.post("https://api.test.nullrun.io/api/v1/track/batch").mock(
            side_effect=[
                httpx.Response(429, json={"error": "slow_down"}, headers={"Retry-After": "0"}),
                httpx.Response(200, json={"accepted_event_ids": ["e1"]}),
            ]
        )
        result = transport._send_batch_with_retry_info([{"event": "e1"}])
        assert route.call_count == 2
        assert "e1" in result.accepted_event_ids

    @respx.mock
    def test_4xx_other_than_429_is_not_retried(self, transport):
        """Client errors (400/401/403/404/422) are real bugs, not transients.
        The retry helper must NOT spin on a 401 — that just wastes the user's
        budget. _retry_with_backoff converts 401 into NullRunAuthenticationError
        before the helper's normal retry path. We expect exactly one attempt."""
        from nullrun.breaker.exceptions import NullRunAuthenticationError

        route = respx.post("https://api.test.nullrun.io/api/v1/track/batch").mock(
            return_value=httpx.Response(401, json={"error": "unauthorized"})
        )
        with pytest.raises(NullRunAuthenticationError):
            transport._send_batch_with_retry_info([{"event": "e1"}])
        assert route.call_count == 1

    @respx.mock
    def test_2xx_first_try_no_retry(self, transport):
        route = respx.post("https://api.test.nullrun.io/api/v1/track/batch").mock(
            return_value=httpx.Response(200, json={"accepted_event_ids": ["e1"]})
        )
        result = transport._send_batch_with_retry_info([{"event": "e1"}])
        assert route.call_count == 1
        assert "e1" in result.accepted_event_ids


# ──────────────────────────────────────────────────────────────
# Retry-After: the delay, not the call count
# ──────────────────────────────────────────────────────────────
#
# The test above says "429 with Retry-After — helper honors the header
# before the next attempt (we assert call count, not exact delay)". Asserting
# the call count is what let a dead parameter survive: `last_retry_after_seconds`
# was documented, threaded through the signature, and never passed by any
# caller, and the suite stayed green because the number of attempts does not
# depend on how long you wait between them. These assert the delay.


def test_a_429_is_not_retried_before_the_server_said_it_may_be(monkeypatch):
    """The floor is the server's, and the SDK must not undercut it.

    Pre-fix, a 429 fell through to plain exponential backoff: base_delay
    0.5s, so the retry landed well before the `Retry-After: 5` the server
    asked for, re-tripping the rate limit that had just answered. The retry
    then repeated at 1s, 2s, 4s — all inside the window the server had
    declared closed.
    """
    delays: list[float] = []
    monkeypatch.setattr(
        "nullrun.transport.time.sleep", lambda s: delays.append(s)
    )

    calls = {"n": 0}

    def _always_429() -> httpx.Response:
        # `raise_for_status` is what the real `_post_batch` does for a 429;
        # without it the loop sees a plain response and returns it, so no
        # retry (and no delay) happens at all.
        calls["n"] += 1
        resp = httpx.Response(
            200 if calls["n"] >= 2 else 429,
            **({} if calls["n"] >= 2 else {"headers": {"Retry-After": "5"}}),
            json={},
        )
        resp.request = httpx.Request("POST", "https://api.test.nullrun.io/api/v1/track/batch")
        resp.raise_for_status()
        return resp

    _retry_with_backoff(_always_429, max_retries=3, base_delay=0.5, max_delay=10.0)

    assert delays, "the retry happened without sleeping"
    assert min(delays) >= 5.0, (
        f"a retry went out before the server's floor: {delays}"
    )


def test_retry_after_jitter_never_reduces_the_wait_but_does_spread_it(monkeypatch):
    """Jitter is one-sided, and it is present.

    Two properties in one test because they are the same decision: a fleet
    that got limited together must not retry together, but no member of it
    may retry before the limit lifts. A symmetric jitter would satisfy the
    first and violate the second.
    """
    samples: list[float] = []
    monkeypatch.setattr(
        "nullrun.transport.time.sleep", lambda s: samples.append(s)
    )

    for _ in range(12):
        calls = {"n": 0}

        def _always_429() -> httpx.Response:
            calls["n"] += 1
            resp = httpx.Response(
                200 if calls["n"] >= 2 else 429,
                **({} if calls["n"] >= 2 else {"headers": {"Retry-After": "5"}}),
                json={},
            )
            resp.request = httpx.Request(
                "POST", "https://api.test.nullrun.io/api/v1/track/batch"
            )
            resp.raise_for_status()
            return resp

        _retry_with_backoff(_always_429, max_retries=1, base_delay=0.5, max_delay=10.0)

    assert all(s >= 5.0 for s in samples), f"jitter went under the floor: {samples}"
    assert len(set(samples)) > 1, f"every client retried at the same instant: {samples}"


def test_a_429_without_the_header_falls_back_to_exponential_backoff(monkeypatch):
    """No header is not an instruction to wait forever.

    A server that rate-limits without stating a wait should not be able to
    pin the SDK to a five-second floor per attempt; the backoff path is the
    right answer there, and it is jittered symmetrically because no floor
    applies.
    """
    delays: list[float] = []
    monkeypatch.setattr(
        "nullrun.transport.time.sleep", lambda s: delays.append(s)
    )
    calls = {"n": 0}

    def _always_429() -> httpx.Response:
        calls["n"] += 1
        resp = httpx.Response(200 if calls["n"] >= 2 else 429, json={})
        resp.request = httpx.Request(
            "POST", "https://api.test.nullrun.io/api/v1/track/batch"
        )
        resp.raise_for_status()
        return resp

    _retry_with_backoff(_always_429, max_retries=1, base_delay=0.5, max_delay=10.0)
    assert delays and delays[0] < 5.0, f"a headerless 429 was treated as a floor: {delays}"


def test_retry_after_as_an_http_date_is_honoured(monkeypatch):
    """The other RFC 7231 form. nginx and hand-rolled limiters differ here."""
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    delays: list[float] = []
    monkeypatch.setattr(
        "nullrun.transport.time.sleep", lambda s: delays.append(s)
    )
    when = format_datetime(
        datetime.now(timezone.utc) + timedelta(seconds=4), usegmt=True
    )
    calls = {"n": 0}

    def _always_429() -> httpx.Response:
        calls["n"] += 1
        resp = httpx.Response(
            200 if calls["n"] >= 2 else 429,
            **({} if calls["n"] >= 2 else {"headers": {"Retry-After": when}}),
            json={},
        )
        resp.request = httpx.Request(
            "POST", "https://api.test.nullrun.io/api/v1/track/batch"
        )
        resp.raise_for_status()
        return resp

    _retry_with_backoff(_always_429, max_retries=1, base_delay=0.5, max_delay=10.0)
    assert delays and delays[0] >= 3.0, f"the HTTP-date form was not honored: {delays}"


def test_a_retry_after_already_in_the_past_is_not_a_floor(monkeypatch):
    """A stale date means "no floor stated", not "wait negative seconds"."""
    delays: list[float] = []
    monkeypatch.setattr(
        "nullrun.transport.time.sleep", lambda s: delays.append(s)
    )
    calls = {"n": 0}

    def _always_429() -> httpx.Response:
        calls["n"] += 1
        resp = httpx.Response(
            200 if calls["n"] >= 2 else 429,
            **(
                {}
                if calls["n"] >= 2
                else {"headers": {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}}
            ),
            json={},
        )
        resp.request = httpx.Request(
            "POST", "https://api.test.nullrun.io/api/v1/track/batch"
        )
        resp.raise_for_status()
        return resp

    _retry_with_backoff(_always_429, max_retries=1, base_delay=0.5, max_delay=10.0)
    assert delays and delays[0] > 0, f"a past date produced a non-positive wait: {delays}"


# ──────────────────────────────────────────────────────────────
# The ceiling: an 86400 must not hang a flush, and must not
# be undercut either
# ──────────────────────────────────────────────────────────────
#
# The previous code clamped with `min(server_wait, max_delay)`. That
# bounds the wait — and it retries an hour early, which is exactly
# what the floor tests above forbid at a smaller scale. A cap that
# makes the SDK ignore a long Retry-After is not a safety feature; it
# is the sustained-429 bug with a different number.


def _reply(status: int, headers: dict | None = None) -> httpx.Response:
    def _handler() -> httpx.Response:
        resp = httpx.Response(
            status, headers=headers or {}, json={"error": "rate_limited"}
        )
        resp.request = httpx.Request(
            "POST", "https://api.test.nullrun.io/api/v1/track/batch"
        )
        resp.raise_for_status()
        return resp

    return _handler


def test_a_retry_after_of_a_day_does_not_sit_through_the_day(monkeypatch):
    """`Retry-After: 86400` must not park a flush thread for 24 hours."""
    slept: list[float] = []
    monkeypatch.setattr("nullrun.transport.time.sleep", lambda s: slept.append(s))
    monkeypatch.setattr("nullrun.transport.threading.Event.wait", lambda self, t=None: False)

    calls = {"n": 0}

    def _always_429() -> httpx.Response:
        calls["n"] += 1
        resp = httpx.Response(429, headers={"Retry-After": "86400"}, json={})
        resp.request = httpx.Request(
            "POST", "https://api.test.nullrun.io/api/v1/track/batch"
        )
        resp.raise_for_status()
        return resp

    with pytest.raises(BreakerTransportError):
        _retry_with_backoff(_always_429, max_retries=10, base_delay=0.5, max_delay=30.0)

    assert not slept, f"a 24h Retry-After was waited out: {slept}"
    assert calls["n"] == 1, (
        f"the SDK retried {calls['n']} time(s) inside a window the server "
        f"declared closed for a day"
    )


def test_an_over_ceiling_retry_after_stops_the_cycle_instead_of_retrying_early(
    monkeypatch,
):
    """The distinction that matters: no wait, no retry, no data loss.

    Refusing to retry is safe here only because the events are already
    durable. Asserting the call count alone would pass against a version
    that dropped them, so the exhaustion path is checked too.
    """
    # The metrics registry is process-global and this file has no reset
    # fixture, so counters are asserted as DELTAS. An absolute assert here
    # would fail on suite ordering and hide a real regression behind a
    # bookkeeping error.
    before = metrics.transport.retry_after_deferred
    calls = {"n": 0}

    def _always_429() -> httpx.Response:
        calls["n"] += 1
        resp = httpx.Response(429, headers={"Retry-After": "3600"}, json={})
        resp.request = httpx.Request(
            "POST", "https://api.test.nullrun.io/api/v1/track/batch"
        )
        resp.raise_for_status()
        return resp

    with pytest.raises(BreakerTransportError):
        _retry_with_backoff(
            _always_429, max_retries=10, base_delay=0.5, max_delay=30.0
        )

    assert calls["n"] == 1
    assert metrics.transport.retry_after_deferred == before + 1


def test_a_retry_after_under_the_ceiling_is_still_honoured(monkeypatch):
    """The ceiling is a ceiling, not a replacement for the floor.

    Capping everything at 30s would satisfy the previous test too — and
    would make every 429 immediate, which is the bug this whole file
    exists to pin. A 5s floor must still produce a >=5s wait.
    """
    delays: list[float] = []
    monkeypatch.setattr("nullrun.transport.time.sleep", lambda s: delays.append(s))
    calls = {"n": 0}

    def _then_ok() -> httpx.Response:
        calls["n"] += 1
        resp = httpx.Response(
            200 if calls["n"] >= 2 else 429,
            **({} if calls["n"] >= 2 else {"headers": {"Retry-After": "5"}}),
            json={},
        )
        resp.request = httpx.Request(
            "POST", "https://api.test.nullrun.io/api/v1/track/batch"
        )
        resp.raise_for_status()
        return resp

    _retry_with_backoff(_then_ok, max_retries=2, base_delay=0.5, max_delay=30.0)
    assert delays and min(delays) >= 5.0, f"a sub-ceiling floor was cut: {delays}"


def test_the_ceiling_is_configurable(monkeypatch):
    """An operator whose backend rate-limits for 5 minutes needs to say so."""
    slept: list[float] = []
    monkeypatch.setattr("nullrun.transport.time.sleep", lambda s: slept.append(s))
    monkeypatch.setenv("NULLRUN_RETRY_AFTER_CEILING", "600")
    before = metrics.transport.retry_after_deferred
    calls = {"n": 0}

    def _always_429() -> httpx.Response:
        calls["n"] += 1
        resp = httpx.Response(429, headers={"Retry-After": "300"}, json={})
        resp.request = httpx.Request(
            "POST", "https://api.test.nullrun.io/api/v1/track/batch"
        )
        resp.raise_for_status()
        return resp

    with pytest.raises(BreakerTransportError):
        _retry_with_backoff(_always_429, max_retries=2, base_delay=0.5, max_delay=30.0)

    assert metrics.transport.retry_after_deferred == before, (
        "a raised ceiling did not let a 300s floor through"
    )
    assert slept, "the 300s floor produced no wait at all"


def test_a_retry_wait_is_cut_short_by_shutdown(monkeypatch):
    """`Transport.stop()` must not wait out a `Retry-After`.

    A process that is exiting and is told to stop should stop. The wait it
    is in is the one place it cannot currently choose to leave, because
    `time.sleep` is not wakeable — which is why the wait became an
    `Event.wait`. The SDK's own stop event is the one a shutdown sets.
    """
    cancel = threading.Event()
    real_wait = threading.Event.wait
    interrupted_before = metrics.transport.retries_interrupted_by_shutdown

    def _wait_then_cancel(self, timeout=None):
        # A shutdown arriving 20ms into a 30s wait.
        cancel.set()
        return real_wait(self, 0.02)

    monkeypatch.setattr("nullrun.transport.threading.Event.wait", _wait_then_cancel)
    monkeypatch.setattr("nullrun.transport.time.sleep", lambda s: pytest.fail("slept"))

    calls = {"n": 0}

    def _always_429() -> httpx.Response:
        calls["n"] += 1
        resp = httpx.Response(429, headers={"Retry-After": "30"}, json={})
        resp.request = httpx.Request(
            "POST", "https://api.test.nullrun.io/api/v1/track/batch"
        )
        resp.raise_for_status()
        return resp

    with pytest.raises(BreakerTransportError):
        _retry_with_backoff(
            _always_429, max_retries=10, base_delay=0.5, max_delay=30.0, cancel=cancel
        )

    assert calls["n"] == 1, (
        f"shutdown did not stop the retry loop; it made {calls['n']} attempts"
    )
    assert metrics.transport.retries_interrupted_by_shutdown == interrupted_before + 1
