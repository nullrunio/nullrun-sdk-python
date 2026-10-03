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

import httpx
import pytest
import respx

from nullrun.breaker.exceptions import BreakerTransportError
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
