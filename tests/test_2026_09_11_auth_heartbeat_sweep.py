"""Regression tests for AUTH-01 and HEART-01 (2026-09-11 sweep).

AUTH-01 (LOW): ``NullRunRuntime.__init__`` arms in the auth path misclassified
``httpx.RequestError`` (DNS failure, connection refused, TLS handshake, request
timeout) as ``NullRunAuthenticationError``. The transport layer already used
the correct class — ``NullRunTransportError(NETWORK_ERROR, "heartbeat")`` for
the same condition on /heartbeat. Only the auth path was wrong.

Fix: replace arm B in ``_authenticate`` with ``NullRunTransportError`` and
delete the redundant defensive arm A in ``__init__``.

HEART-01 (LOW): ``Runtime.heartbeat()`` was missing from the public API; only
private ``Transport.heartbeat()`` and the scheduler ``Runtime.ping_chain()``
existed. Long-running chains without ``ping_chain`` had no way to extend the
chain's idle TTL.

Fix: add ``Runtime.heartbeat(chain_id)`` thin forwarder (mirrors the
``chain_end`` / ``cancel_execution`` pattern).

Test isolation: each test uses ``make_test_runtime`` (test-mode runtime) so
network calls are skipped, and patches the appropriate ``_transport.heartbeat``
/ ``_authenticate`` method to inject the error path under test.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from httpx import Response

from nullrun.breaker.exceptions import (
    NullRunAuthenticationError,
    NullRunInfrastructureError,
    NullRunTransportError,
    TransportErrorSource,
)
from nullrun.runtime import NullRunRuntime

BASE_URL = "https://api.test.nullrun.io"


# ──────────────────────────────────────────────────────────────
# AUTH-01: auth-path transport-error reclassification
# ──────────────────────────────────────────────────────────────


class TestAuthNetworkErrorReclassification:
    """AUTH-01: httpx.RequestError on auth path raises NullRunTransportError."""

    def test_auth_connect_error_raises_transport_error_not_auth(
        self,
        monkeypatch,
        make_runtime,
    ):
        """A connection refused on /api/v1/auth/verify must surface as
        NullRunTransportError(NETWORK_ERROR, "auth"), NOT
        NullRunAuthenticationError. The previous wrap was misleading —
        the API key may be valid, the backend is just unreachable.
        """
        monkeypatch.setenv("NULLRUN_API_KEY", "test-key-12345678")
        monkeypatch.setenv("NULLRUN_API_URL", BASE_URL)
        with respx.mock:
            # Force httpx.ConnectError on /auth/verify
            respx.post(f"{BASE_URL}/api/v1/auth/verify").mock(
                side_effect=httpx.ConnectError("connection refused")
            )
            with pytest.raises(NullRunTransportError) as exc_info:
                make_runtime(api_url=BASE_URL, polling=False)

        err = exc_info.value
        assert err.source == TransportErrorSource.NETWORK_ERROR
        assert err.endpoint == "auth"
        assert err.error_code == "NR-B001"  # default for NullRunTransportError

    def test_auth_timeout_raises_transport_error_not_auth(
        self,
        monkeypatch,
    ):
        """httpx.TimeoutException on auth path also reclassified."""
        monkeypatch.setenv("NULLRUN_API_KEY", "test-key-12345678")
        monkeypatch.setenv("NULLRUN_API_URL", BASE_URL)
        with respx.mock:
            respx.post(f"{BASE_URL}/api/v1/auth/verify").mock(
                side_effect=httpx.TimeoutException("timed out")
            )
            with pytest.raises(NullRunTransportError) as exc_info:
                NullRunRuntime(api_key="test-key-12345678", api_url=BASE_URL, polling=False)

        assert exc_info.value.source == TransportErrorSource.NETWORK_ERROR

    def test_auth_error_class_no_longer_catches_network_error(
        self,
        monkeypatch,
    ):
        """Back-compat check: user code with ``except NullRunAuthenticationError``
        will NOT silently swallow the network error case after this fix.
        The new NullRunTransportError is a sibling under
        NullRunInfrastructureError — only the parent class catches both.
        """
        monkeypatch.setenv("NULLRUN_API_KEY", "test-key-12345678")
        monkeypatch.setenv("NULLRUN_API_URL", BASE_URL)
        with respx.mock:
            respx.post(f"{BASE_URL}/api/v1/auth/verify").mock(
                side_effect=httpx.ConnectError("nope")
            )
            with pytest.raises(NullRunTransportError) as exc_info:
                NullRunRuntime(api_key="test-key-12345678", api_url=BASE_URL, polling=False)

            # Parent-class catch still works (NullRunInfrastructureError).
            try:
                raise exc_info.value
            except NullRunInfrastructureError as e:
                # Confirm the catch reaches the new error.
                assert e is exc_info.value
            else:
                pytest.fail("NullRunInfrastructureError should catch the new transport error")

            # Sibling-class (NullRunAuthenticationError) does NOT catch it.
            try:
                raise exc_info.value
            except NullRunAuthenticationError:
                pytest.fail(
                    "NullRunAuthenticationError must NOT catch the new "
                    "NullRunTransportError — they're siblings, not parent/child"
                )
            except NullRunTransportError:
                pass  # Expected


# ──────────────────────────────────────────────────────────────
# HEART-01: public Runtime.heartbeat() wrapper
# ──────────────────────────────────────────────────────────────


class TestRuntimeHeartbeatForwarder:
    """HEART-01: NullRunRuntime.heartbeat forwards to Transport.heartbeat."""

    def test_heartbeat_method_exists_on_public_api(self, make_test_runtime):
        """The heartbeat method must be present on the Runtime public surface."""
        rt = make_test_runtime()
        assert hasattr(rt, "heartbeat"), "Runtime.heartbeat missing from public API"
        assert callable(getattr(rt, "heartbeat"))

    def test_heartbeat_forwards_chain_id_to_transport(self, make_test_runtime):
        """Runtime.heartbeat(chain_id) calls Transport.heartbeat with the same chain_id."""
        rt = make_test_runtime()
        captured: dict = {}

        def fake_transport_heartbeat(chain_id_arg: str) -> dict:
            captured["chain_id"] = chain_id_arg
            return {
                "status": "ok",
                "chain_id": chain_id_arg,
                "last_active": "2026-09-11T12:00:00Z",
            }

        rt._transport.heartbeat = fake_transport_heartbeat  # type: ignore[method-assign]
        result = rt.heartbeat("chain-test-abc-123")

        assert captured["chain_id"] == "chain-test-abc-123"
        assert result == {
            "status": "ok",
            "chain_id": "chain-test-abc-123",
            "last_active": "2026-09-11T12:00:00Z",
        }

    def test_heartbeat_passes_through_transport_error(self, make_test_runtime):
        """Transport-layer NullRunTransportError must propagate unchanged
        (not be rewrapped to NullRunAuthenticationError — AUTH-01 analog)."""
        rt = make_test_runtime()
        sentinel = NullRunTransportError(
            "network error on /heartbeat",
            source=TransportErrorSource.NETWORK_ERROR,
            endpoint="heartbeat",
        )

        def fake_transport_heartbeat_raises(chain_id_arg: str) -> dict:
            raise sentinel

        rt._transport.heartbeat = fake_transport_heartbeat_raises  # type: ignore[method-assign]
        with pytest.raises(NullRunTransportError) as exc_info:
            rt.heartbeat("chain-test-abc-123")

        assert exc_info.value is sentinel
        assert exc_info.value.source == TransportErrorSource.NETWORK_ERROR
        assert exc_info.value.endpoint == "heartbeat"

    def test_ping_chain_still_works_after_heartbeat_added(self, make_test_runtime):
        """Adding Runtime.heartbeat must not break Runtime.ping_chain (scheduler)."""
        rt = make_test_runtime()
        assert hasattr(rt, "ping_chain")
        # Don't actually run the scheduler (it spawns a daemon thread);
        # just confirm the callable is intact and takes the expected kwargs.
        import inspect

        sig = inspect.signature(rt.ping_chain)
        assert "chain_id" in sig.parameters
        assert "interval" in sig.parameters
