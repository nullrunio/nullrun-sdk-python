"""tests/test_s008_resign_per_retry.py — S008 replay-guard regression.

DEF-MP-TS12-ENF-01 (RUN_ID 20260929T1338, 2026-09-29).

The backend's S008 replay guard stores ``hmac:replay:{key_fp}:{sig_hash}``
on first sight of a signature and rejects every repeat as
``HMAC_REPLAY`` (fail-CLOSED). The SDK built its signed headers ONCE,
outside the retry closure, on the three paths that retry:

  * ``Transport.check``             (/gate,           3 retries)
  * ``Transport.execute``           (/execute,       10 retries)
  * ``_send_batch_with_retry_info`` (/track/batch,  10 retries)

Attempt 1 registered the signature; every retry replayed it
byte-for-byte and was rejected. One transient 5xx therefore consumed
the whole retry budget on replay rejections, and the resulting 401 was
indistinguishable from a genuinely invalid API key — which is how the
TS-12 cycle logged ``HMAC_REPLAY x684`` in production.

The fix signs inside the retry closure. These tests assert the
DISTINCTNESS of the signature across attempts, not merely that a
signature is present: a test that only checked "the request was
signed" would pass against the broken code too.
"""

from __future__ import annotations

import time

import httpx
import pytest
import respx

from nullrun.transport import Transport


@pytest.fixture
def signed_transport():
    t = Transport(
        api_url="https://api.test.nullrun.io",
        api_key="test-key-12345678",
        secret_key="test-secret-abcdefgh",
    )
    yield t
    t.stop()


@pytest.fixture
def ticking_clock(monkeypatch):
    """Make `time.time()` advance on every call.

    ``_build_signed_headers`` signs with ``int(time.time())`` — a
    second-resolution clock, which is exactly why a retry inside the
    same second reproduces the previous signature. The real retry path
    sleeps between attempts; rather than a wall-clock wait (the suite
    caps sleeps), we advance the clock per call so successive
    signatures necessarily differ. If the SDK signs once outside the
    closure, this fixture makes no difference and the signature is
    still identical — which is what the assertions below catch.
    """
    real = time.time
    state = {"n": 0}

    def _fake() -> float:
        state["n"] += 1
        return real() + state["n"]

    monkeypatch.setattr("nullrun.transport.time.time", _fake, raising=True)
    return state


def _signatures(route) -> list[str | None]:
    return [call.request.headers.get("X-Signature") for call in route.calls]


class TestS008ResignPerRetry:
    @respx.mock
    def test_gate_retry_signs_each_attempt_freshly(self, signed_transport, ticking_clock):
        route = respx.post("https://api.test.nullrun.io/api/v1/gate").mock(
            side_effect=[
                httpx.Response(503, json={"error_code": "REDIS_UNAVAILABLE"}),
                httpx.Response(200, json={"decision": "allow"}),
            ]
        )
        result = signed_transport.check({"workflow_id": "wf-s008-test"})

        assert route.call_count == 2, "the 503 should have triggered exactly one retry"
        assert result.get("decision") == "allow"
        sigs = _signatures(route)
        assert sigs[0] and sigs[1], "both attempts must be signed"
        assert sigs[0] != sigs[1], (
            "S008 regression: both /gate attempts carried the SAME X-Signature. "
            "The backend replay guard rejects the retry as HMAC_REPLAY, turning "
            "one transient 5xx into a credential error."
        )

    @respx.mock
    def test_execute_retry_signs_each_attempt_freshly(self, signed_transport, ticking_clock):
        signed_transport._execute_max_retries = 2
        route = respx.post("https://api.test.nullrun.io/api/v1/execute").mock(
            side_effect=[
                httpx.Response(500, json={"error_code": "INTERNAL"}),
                httpx.Response(200, json={"decision": "allow"}),
            ]
        )
        signed_transport.execute(
            organization_id="org-s008-test",
            execution_id="exec-s008-test",
            trace_id="trace-s008-test",
            tool="search",
            input_data={"q": "s008"},
        )

        assert route.call_count == 2
        sigs = _signatures(route)
        assert sigs[0] and sigs[1]
        assert sigs[0] != sigs[1], "S008 regression: /execute replayed one signature"

    @respx.mock
    def test_track_batch_retry_signs_each_attempt_freshly(self, signed_transport, ticking_clock):
        signed_transport._track_max_retries = 2
        route = respx.post("https://api.test.nullrun.io/api/v1/track/batch").mock(
            side_effect=[
                httpx.Response(500, json={"error_code": "INTERNAL"}),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        signed_transport._send_batch_with_retry_info([{"event": "test"}])

        assert route.call_count == 2
        sigs = _signatures(route)
        assert sigs[0] and sigs[1]
        assert sigs[0] != sigs[1], "S008 regression: /track/batch replayed one signature"

    def test_signing_not_dropped_anywhere(self, signed_transport):
        """Constraint: only the three retrying sites move.

        Signing must still happen everywhere — the fix relocates the
        call, it does not remove it.
        """
        headers = signed_transport._build_signed_headers(body='{"a":1}')
        assert headers.get("X-Signature")
        assert headers.get("X-Signature-Timestamp")
        assert headers.get("X-NULLRUN-PROTOCOL")
