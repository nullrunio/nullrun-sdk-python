"""
Additional transport branch tests covering gaps in
``tests/test_transport.py``:

  - ``verify_hmac_signature`` expired / mismatch branches
  - ``_extract_retry_after`` int / HTTP-date / garbage / None
  - ``Transport.execute`` fallback modes (STRICT / CACHED hit / CACHED miss
    / PERMISSIVE)
  - ``Transport.execute`` ``on_transport_error`` callable / "raise" /
    "open" / "closed"
  - ``Transport.check`` 5xx + "raise" / network + "raise" / 4xx fallback
  - ``clear_policy_cache``
  - ``_parse_error_envelope`` for 401 / 403 / 429 / 500 / 502 / 400
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from nullrun.breaker.exceptions import (
    NullRunAuthenticationError,
    NullRunTransportError,
    RateLimitError,
    TransportErrorSource,
)
from nullrun.transport import (
    FlushConfig,
    Transport,
    _parse_error_envelope,
    verify_hmac_signature,
)


def _extract_retry_after(response):
    """Module-level shim: ``_extract_retry_after`` is an instance
    method on Transport (not a free function), so reach it through a
    throwaway instance.
    """
    return Transport._extract_retry_after(Transport.__new__(Transport), response)


# ─── verify_hmac_signature ───────────────────────────────────────────


def test_verify_hmac_signature_fresh_and_matching():
    """Fresh timestamp + correct signature → True."""
    import hashlib
    import hmac as _hmac
    import json as _json

    body = '{"x":1}'
    ts = int(time.time())
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    msg = f"{ts}:key:{body_hash}"
    sig = _hmac.new(b"secret", msg.encode("utf-8"), hashlib.sha256).hexdigest()

    assert verify_hmac_signature("key", "secret", ts, body, sig) is True


def test_verify_hmac_signature_expired_returns_false():
    """Timestamp far in the past → False (and bumps the expired counter)."""
    body = "{}"
    ts = int(time.time()) - 400  # > 5 min
    sig = "00" * 32
    assert verify_hmac_signature("key", "secret", ts, body, sig) is False


def test_verify_hmac_signature_future_returns_false():
    """Timestamp far in the future → False (clock skew / replay)."""
    body = "{}"
    ts = int(time.time()) + 400
    sig = "00" * 32
    assert verify_hmac_signature("key", "secret", ts, body, sig) is False


def test_verify_hmac_signature_mismatch_returns_false():
    """Fresh timestamp but wrong signature → False."""
    body = "{}"
    ts = int(time.time())
    assert verify_hmac_signature("key", "secret", ts, body, "0" * 64) is False


# ─── _extract_retry_after ───────────────────────────────────────────


def test_extract_retry_after_no_header_returns_none():
    response = MagicMock()
    response.headers.get.return_value = None
    assert _extract_retry_after(response) is None


def test_extract_retry_after_seconds_int():
    response = MagicMock()
    response.headers.get.return_value = "30"
    assert _extract_retry_after(response) == 30.0


def test_extract_retry_after_seconds_float():
    response = MagicMock()
    response.headers.get.return_value = "2.5"
    assert _extract_retry_after(response) == 2.5


def test_extract_retry_after_http_date():
    """HTTP-date → float seconds delta to now (positive or negative)."""
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    response = MagicMock()
    future = datetime.now(timezone.utc) + timedelta(seconds=120)
    response.headers.get.return_value = format_datetime(future)
    result = _extract_retry_after(response)
    assert result is not None
    assert 100 <= result <= 130


def test_extract_retry_after_garbage_returns_none():
    response = MagicMock()
    response.headers.get.return_value = "not-a-date"
    assert _extract_retry_after(response) is None


# ─── Transport.execute fallback modes ──────────────────────────────


def _build_transport() -> Transport:
    """Build a transport with a stub client (no network)."""
    return Transport(
        api_url="https://api.nullrun.io",
        api_key="key",
        secret_key="secret",
        config=FlushConfig(),
    )


def test_execute_200_with_cache_write():
    """200 → caches the decision for CACHED mode and returns gateway decision."""
    t = _build_transport()
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "decision": "allow",
        "policy_id": "p1",
        "policy_version": 3,
    }
    t._client.post = MagicMock(return_value=fake_response)

    result = t.execute(
        organization_id="org-1",
        execution_id="wf-1",
        trace_id="t-1",
        tool="safe.tool",
        input_data={},
    )
    assert result["decision"] == "allow"
    assert result["decision_source"] == "gateway"


def test_execute_4xx_returns_block():
    """4xx (no special handling) → block-dict, decision_source FALLBACK."""
    t = _build_transport()
    fake_response = MagicMock()
    fake_response.status_code = 400
    fake_response.json.return_value = {"error": "bad_request"}
    t._client.post = MagicMock(return_value=fake_response)

    result = t.execute(
        organization_id="org-1",
        execution_id="wf-1",
        trace_id="t-1",
        tool="safe.tool",
        input_data={},
    )
    assert result["decision"] == "block"
    assert "400" in result["explanation"]


def test_execute_breaker_error_with_raise():
    """Transport raises BreakerTransportError + on_transport_error='raise'
    → re-raised as classified NullRunTransportError(NETWORK_ERROR).
    """
    from nullrun.breaker.exceptions import BreakerTransportError

    t = _build_transport()
    t._client.post = MagicMock(side_effect=BreakerTransportError("down"))
    with pytest.raises(NullRunTransportError) as excinfo:
        t.execute(
            organization_id="org-1",
            execution_id="wf-1",
            trace_id="t-1",
            tool="x",
            input_data={},
            on_transport_error="raise",
        )
    assert excinfo.value.source == TransportErrorSource.NETWORK_ERROR


def test_execute_breaker_error_with_open_string():
    """Transport raises + on_transport_error='open' → synthetic allow."""
    from nullrun.breaker.exceptions import BreakerTransportError

    t = _build_transport()
    t._client.post = MagicMock(side_effect=BreakerTransportError("down"))
    result = t.execute(
        organization_id="org-1",
        execution_id="wf-1",
        trace_id="t-1",
        tool="x",
        input_data={},
        on_transport_error="open",
    )
    assert result["decision"] == "allow"
    assert result["decision_source"] == TransportErrorSource.NETWORK_ERROR


def test_execute_breaker_error_with_closed_string():
    """Transport raises + on_transport_error='closed' → synthetic block."""
    from nullrun.breaker.exceptions import BreakerTransportError

    t = _build_transport()
    t._client.post = MagicMock(side_effect=BreakerTransportError("down"))
    result = t.execute(
        organization_id="org-1",
        execution_id="wf-1",
        trace_id="t-1",
        tool="x",
        input_data={},
        on_transport_error="closed",
    )
    assert result["decision"] == "block"
    assert result["decision_source"] == TransportErrorSource.NETWORK_ERROR


def test_execute_breaker_error_with_callable_callback():
    """Transport raises + on_transport_error=callable → callback receives exc."""
    from nullrun.breaker.exceptions import BreakerTransportError

    t = _build_transport()
    t._client.post = MagicMock(side_effect=BreakerTransportError("down"))
    seen: list = []

    def _cb(exc):
        seen.append(exc)
        return {"decision": "custom", "decision_source": "callback"}

    result = t.execute(
        organization_id="org-1",
        execution_id="wf-1",
        trace_id="t-1",
        tool="x",
        input_data={},
        on_transport_error=_cb,
    )
    assert result["decision"] == "custom"
    assert isinstance(seen[0], BreakerTransportError)


def test_execute_fallback_strict_returns_block():
    """fallback_mode=STRICT → synthetic block on transport failure."""
    from nullrun.breaker.exceptions import BreakerTransportError

    t = _build_transport()
    t._client.post = MagicMock(side_effect=BreakerTransportError("down"))
    result = t.execute(
        organization_id="org-1",
        execution_id="wf-1",
        trace_id="t-1",
        tool="x",
        input_data={},
        fallback_mode="strict",
    )
    assert result["decision"] == "block"
    assert "STRICT" in result["explanation"]


# 0.7.0: fallback_mode=CACHED + the local PolicyCache path were
# removed. The thin-client SDK has no local cache to consult on
# gateway failure. CACHED now degrades to PERMISSIVE.


def test_execute_fallback_cached_degrades_to_permissive():
    """fallback_mode=CACHED → degrade to PERMISSIVE (no local cache)."""
    from nullrun.breaker.exceptions import BreakerTransportError

    t = _build_transport()
    t._client.post = MagicMock(side_effect=BreakerTransportError("down"))
    result = t.execute(
        organization_id="org-1",
        execution_id="wf-1",
        trace_id="t-1",
        tool="x",
        input_data={},
        fallback_mode="cached",
    )
    # 0.7.0: CACHED silently degrades to PERMISSIVE (allow).
    assert result["decision"] == "allow"
    assert result["decision_source"] == "fallback"


def test_execute_fallback_permissive_default():
    """fallback_mode=PERMISSIVE → synthetic allow on transport failure."""
    from nullrun.breaker.exceptions import BreakerTransportError

    t = _build_transport()
    t._client.post = MagicMock(side_effect=BreakerTransportError("down"))
    result = t.execute(
        organization_id="org-1",
        execution_id="wf-1",
        trace_id="t-1",
        tool="x",
        input_data={},
    )
    assert result["decision"] == "allow"
    assert "PERMISSIVE" in result["explanation"]


def test_execute_httpx_network_error_with_raise():
    """httpx.RequestError + on_transport_error='raise' → classified error."""
    import httpx

    t = _build_transport()
    t._client.post = MagicMock(side_effect=httpx.ConnectError("nope"))
    with pytest.raises(NullRunTransportError) as excinfo:
        t.execute(
            organization_id="org-1",
            execution_id="wf-1",
            trace_id="t-1",
            tool="x",
            input_data={},
            on_transport_error="raise",
        )
    assert excinfo.value.source == TransportErrorSource.NETWORK_ERROR


def test_execute_auth_error_propagates():
    """NullRunAuthenticationError is re-raised without fallback handling."""
    t = _build_transport()
    t._client.post = MagicMock(side_effect=NullRunAuthenticationError("bad key"))
    with pytest.raises(NullRunAuthenticationError):
        t.execute(
            organization_id="org-1",
            execution_id="wf-1",
            trace_id="t-1",
            tool="x",
            input_data={},
        )


# ─── Transport.check ────────────────────────────────────────────────


def test_check_200_returns_payload():
    t = _build_transport()
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {"decision": "allow", "remaining_budget_cents": 500}
    t._client.post = MagicMock(return_value=fake)

    result = t.check({"organization_id": "org-1"})
    assert result["decision"] == "allow"


def test_check_5xx_with_raise_raises_classified():
    t = _build_transport()
    fake = MagicMock()
    fake.status_code = 503
    fake.json.return_value = {"error": "unavailable"}
    t._client.post = MagicMock(return_value=fake)

    with pytest.raises(NullRunTransportError) as excinfo:
        t.check({"organization_id": "org-1"}, on_transport_error="raise")
    assert excinfo.value.source == TransportErrorSource.GATEWAY_ERROR


def test_check_5xx_without_raise_returns_block():
    t = _build_transport()
    fake = MagicMock()
    fake.status_code = 503
    fake.json.return_value = {}
    t._client.post = MagicMock(return_value=fake)

    result = t.check({"organization_id": "org-1"})
    assert result["decision"] == "block"


def test_check_4xx_returns_block():
    t = _build_transport()
    fake = MagicMock()
    fake.status_code = 400
    fake.json.return_value = {"error": "bad"}
    t._client.post = MagicMock(return_value=fake)

    result = t.check({"organization_id": "org-1"})
    assert result["decision"] == "block"


def test_check_network_error_with_raise_raises_classified():
    import httpx

    t = _build_transport()
    t._client.post = MagicMock(side_effect=httpx.ConnectError("nope"))
    with pytest.raises(NullRunTransportError) as excinfo:
        t.check({"organization_id": "org-1"}, on_transport_error="raise")
    assert excinfo.value.source == TransportErrorSource.NETWORK_ERROR


def test_check_network_error_without_raise_returns_block():
    import httpx

    t = _build_transport()
    t._client.post = MagicMock(side_effect=httpx.ConnectError("nope"))
    result = t.check({"organization_id": "org-1"})
    assert result["decision"] == "block"


# ─── clear_policy_cache ──────────────────────────────────────────────
# 0.7.0: Transport.clear_policy_cache and Transport._policy_cache
# were removed. The SDK is a thin client; there is no local cache
# to clear.

# ─── _parse_error_envelope ───────────────────────────────────────────


def _make_response(status: int, body, headers: dict | None = None):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    if isinstance(body, (dict, list)):
        resp.json.return_value = body
        resp.text = ""
    else:
        resp.json.side_effect = Exception("not json")
        resp.text = body or ""
    return resp


def test_parse_error_envelope_401_raises_auth_error():
    resp = _make_response(401, {"error": "unauthorized", "message": "bad key"})
    exc = _parse_error_envelope(resp, "/execute")
    assert isinstance(exc, NullRunAuthenticationError)


def test_parse_error_envelope_403_raises_auth_error():
    resp = _make_response(403, {"error": "forbidden"})
    exc = _parse_error_envelope(resp, "/gate")
    assert isinstance(exc, NullRunAuthenticationError)


def test_parse_error_envelope_429_raises_rate_limit():
    resp = _make_response(
        429,
        {"error": "rate_limit", "message": "slow down", "upgrade_url": "https://x"},
        headers={"Retry-After": "30"},
    )
    exc = _parse_error_envelope(resp, "/execute")
    assert isinstance(exc, RateLimitError)
    assert exc.retry_after == 30.0
    assert exc.upgrade_url == "https://x"


def test_parse_error_envelope_429_http_date():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    resp = _make_response(
        429,
        {"error": "rate_limit"},
        headers={"Retry-After": format_datetime(future)},
    )
    exc = _parse_error_envelope(resp, "/execute")
    assert isinstance(exc, RateLimitError)
    assert exc.retry_after is not None


def test_parse_error_envelope_5xx_raises_gateway_error():
    resp = _make_response(502, {"error": "bad_gateway"})
    exc = _parse_error_envelope(resp, "/execute")
    assert isinstance(exc, NullRunTransportError)
    assert exc.source == TransportErrorSource.GATEWAY_ERROR
    # status_code is forwarded as a detail kwarg (see NullRunTransportError.__init__).
    assert exc.details.get("status_code") == 502


def test_parse_error_envelope_4xx_other_raises_client_error():
    """4xx other than 401/403/429 → NullRunTransportError with GATEWAY_ERROR."""
    resp = _make_response(400, {"error": "bad_request"})
    exc = _parse_error_envelope(resp, "/execute")
    assert isinstance(exc, NullRunTransportError)
    assert exc.details.get("status_code") == 400


def test_parse_error_envelope_non_json_body_uses_text():
    resp = _make_response(503, "raw error text")
    exc = _parse_error_envelope(resp, "/execute")
    assert isinstance(exc, NullRunTransportError)
    assert "raw error text" in str(exc)


# ─── connect_websocket URL parsing ───────────────────────────────────


def test_connect_websocket_rejects_non_http_scheme():
    t = _build_transport()
    t.api_url = "ftp://api.nullrun.io"

    import asyncio

    with pytest.raises(ValueError, match="Unsupported scheme"):
        asyncio.run(t.connect_websocket(organization_id="org-1"))


def test_connect_websocket_uses_wss_for_https(monkeypatch):
    t = _build_transport()
    t.api_url = "https://api.nullrun.io"

    # Patch WebSocketConnection.connect to capture the constructed URL.
    from nullrun import transport_websocket as tw_mod

    captured: dict = {}

    class _FakeConn:
        def __init__(self, url, **kwargs):
            captured["url"] = url

        async def connect(self):
            return self

    monkey_url = "wss://api.nullrun.io/ws/control/org-1"
    # monkeypatch restores the original WebSocketConnection on test
    # teardown — without it, the leaked fake class breaks every later
    # test that imports ``WebSocketConnection`` from the module
    # (e.g. test_reconnect_cap.py's ``inspect.getsource`` assertions).
    monkeypatch.setattr(tw_mod, "WebSocketConnection", _FakeConn)

    import asyncio

    asyncio.run(t.connect_websocket(organization_id="org-1"))
    assert captured["url"] == monkey_url


def test_connect_websocket_uses_ws_for_http_localhost(monkeypatch):
    """Loopback http:// → ws:// (not wss://) for local dev."""
    t = Transport(
        api_url="http://localhost:8080",
        api_key="key",
        secret_key="secret",
        config=FlushConfig(),
    )

    from nullrun import transport_websocket as tw_mod

    captured: dict = {}

    class _FakeConn:
        def __init__(self, url, **kwargs):
            captured["url"] = url

        async def connect(self):
            return self

    # Same leak fix as the wss test above — monkeypatch auto-restores.
    monkeypatch.setattr(tw_mod, "WebSocketConnection", _FakeConn)

    import asyncio

    asyncio.run(t.connect_websocket(organization_id="org-1"))
    assert captured["url"] == "ws://localhost:8080/ws/control/org-1"


# ─── _refetch_credentials ──────────────────────────────────────────


def test_refetch_credentials_updates_secret_key():
    """``_refetch_credentials`` updates ``self.secret_key`` on 200."""
    t = _build_transport()
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {"secret_key": "new-secret"}
    t._client.post = MagicMock(return_value=fake)

    import asyncio

    asyncio.run(t._refetch_credentials())
    assert t.secret_key == "new-secret"


def test_refetch_credentials_handles_non_200():
    t = _build_transport()
    fake = MagicMock()
    fake.status_code = 401
    fake.json.return_value = {}
    t._client.post = MagicMock(return_value=fake)

    import asyncio

    asyncio.run(t._refetch_credentials())  # must not raise


def test_refetch_credentials_handles_network_error():
    import httpx

    t = _build_transport()
    t._client.post = MagicMock(side_effect=httpx.ConnectError("nope"))
    import asyncio

    asyncio.run(t._refetch_credentials())  # must not raise


def test_refetch_credentials_missing_secret_key_logs_warning(caplog):
    """200 response without secret_key → WARNING logged, no update."""
    import logging

    t = _build_transport()
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {}  # no secret_key
    t._client.post = MagicMock(return_value=fake)

    original_secret = t.secret_key
    import asyncio

    with caplog.at_level(logging.WARNING, logger="nullrun.transport"):
        asyncio.run(t._refetch_credentials())
    assert t.secret_key == original_secret
    assert any("secret_key" in r.getMessage() for r in caplog.records)


# ─── InsecureTransportError on http:// non-loopback ──────────────────


def test_transport_rejects_insecure_http():
    """Non-loopback HTTP URL raises InsecureTransportError."""
    with pytest.raises(Exception) as excinfo:
        Transport(api_url="http://example.com", api_key="key", config=FlushConfig())
    # Subclass of BreakerTransportError (via InsecureTransportError).
    assert "Insecure URL" in str(excinfo.value) or "insecure" in str(excinfo.value).lower()


def test_transport_accepts_loopback_http():
    """http://127.0.0.1 / http://[::1] / http://localhost are accepted."""
    Transport(api_url="http://127.0.0.1:8080", api_key="key", config=FlushConfig())
    Transport(api_url="http://[::1]:8080", api_key="key", config=FlushConfig())
    Transport(api_url="http://localhost:8080", api_key="key", config=FlushConfig())
