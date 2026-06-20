"""
tests/test_track_batch_retry.py — regression coverage for P0 #2.

Pre-fix, _send_batch_with_retry_info issued a single self._client.post(...)
and immediately called raise_for_status(). A backend 500 raised out of the
flush path; the in-memory buffer was cleared at the call site and every
event in the batch was lost. P0 #2 wraps the post() in _retry_with_backoff
so a transient 5xx is retried (max 3 attempts, exponential backoff +
jitter, capped at 10s). 429s are also retried (the helper honors
Retry-After when present).

These tests pin the new contract:

* a single 5xx followed by 200 — batch is accepted, only one event-loss
  is observable by the caller.
* three consecutive 5xx — final call raises after exhausting retries;
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
from nullrun.transport import Transport


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
        route = respx.post(
            "https://api.test.nullrun.io/api/v1/track/batch"
        ).mock(side_effect=[
            httpx.Response(500, json={"error": "internal"}),
            httpx.Response(200, json={"accepted_event_ids": ["e1"]}),
        ])
        result = transport._send_batch_with_retry_info([{"event": "e1"}])
        assert route.call_count == 2
        assert "e1" in result.accepted_event_ids

    @respx.mock
    def test_three_consecutive_5xx_raises_after_retries(self, transport):
        route = respx.post(
            "https://api.test.nullrun.io/api/v1/track/batch"
        ).mock(return_value=httpx.Response(500, json={"error": "boom"}))
        # _retry_with_backoff wraps the underlying HTTPStatusError into
        # BreakerTransportError so the caller can match a single exception
        # type without distinguishing 4xx vs 5xx vs network.
        with pytest.raises(BreakerTransportError):
            transport._send_batch_with_retry_info([{"event": "e1"}])
        # 1 initial + 3 retries = 4 total
        assert route.call_count == 4

    @respx.mock
    def test_429_is_retried_then_succeeds(self, transport):
        route = respx.post(
            "https://api.test.nullrun.io/api/v1/track/batch"
        ).mock(side_effect=[
            httpx.Response(429, json={"error": "slow_down"}, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"accepted_event_ids": ["e1"]}),
        ])
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
        route = respx.post(
            "https://api.test.nullrun.io/api/v1/track/batch"
        ).mock(return_value=httpx.Response(401, json={"error": "unauthorized"}))
        with pytest.raises(NullRunAuthenticationError):
            transport._send_batch_with_retry_info([{"event": "e1"}])
        assert route.call_count == 1

    @respx.mock
    def test_2xx_first_try_no_retry(self, transport):
        route = respx.post(
            "https://api.test.nullrun.io/api/v1/track/batch"
        ).mock(return_value=httpx.Response(200, json={"accepted_event_ids": ["e1"]}))
        result = transport._send_batch_with_retry_info([{"event": "e1"}])
        assert route.call_count == 1
        assert "e1" in result.accepted_event_ids
