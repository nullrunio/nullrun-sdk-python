"""
Regression tests for the P0 InsecureTransportError check.

Pre-fix: ``Transport.__init__`` used a ``startswith("http:/127.0.0.1")``
chain. That had three classes of bugs:
  1. Homograph attacks — ``http:/127.0.0.1.attacker.com`` matched
     the prefix and was allowed.
  2. Case sensitivity — ``http:/LOCALHOST:8080`` was rejected.
  3. IPv6 miss — ``http:/[::1]:8080`` was rejected even though
     ``[::1]`` is the IPv6 loopback.

The fix replaces the startswith chain with a ``urllib.parse.urlparse``
check that extracts the canonical hostname, lowercases it, and
compares against an allow-list of ``localhost``, ``::1``, and the
``127.0.0.0/8`` IPv4 loopback range.
"""

from __future__ import annotations

import pytest

from nullrun.breaker.exceptions import InsecureTransportError
from nullrun.transport import Transport


class TestInsecureTransportBlocksNonLocalhost:
    """Non-localhost HTTP URLs MUST raise InsecureTransportError."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com",
            "http://api.example.com",
            "http://192.168.1.1",
            "http://10.0.0.1",
            "http://8.8.8.8",
        ],
    )
    def test_remote_http_url_rejected(self, url):
        with pytest.raises(InsecureTransportError):
            Transport(api_url=url, api_key="test-key-12345678")


class TestInsecureTransportBlocksHomographs:
    """URLs that look like localhost but aren't MUST be rejected."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1.attacker.com",
            "http://localhost.evil.com",
            "http://127.0.0.2.evil.com",
            "http://localhost:8080@evil.com",
        ],
    )
    def test_homograph_rejected(self, url):
        with pytest.raises(InsecureTransportError):
            Transport(api_url=url, api_key="test-key-12345678")


class TestInsecureTransportAllowsLegitimateLocalhost:
    """Localhost variants MUST be allowed (case-insensitive, IPv4 loopback range, IPv6)."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost",
            "http://localhost:8080",
            "http://LOCALHOST",
            "http://Localhost:8443",
            "http://127.0.0.1",
            "http://127.0.0.1:8080",
            "http://127.0.0.2",  # 127.0.0.0/8 — full loopback range
            "http://127.255.255.254",
            "http://[::1]",  # IPv6 loopback, compressed
            "http://[::1]:8080",  # IPv6 loopback with port
        ],
    )
    def test_localhost_allowed(self, url):
        # Should not raise.
        t = Transport(api_url=url, api_key="test-key-12345678")
        assert t is not None
        # Make sure we do not actually start a flush thread (we did
        # not call start ), so the test does not hit a real network.
        assert t._client is not None


class TestInsecureTransportAllowsHttps:
    """HTTPS URLs are always allowed — TLS is the protection."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://api.nullrun.io",
            "https://example.com",
            "https://localhost:8443",
        ],
    )
    def test_https_always_allowed(self, url):
        t = Transport(api_url=url, api_key="test-key-12345678")
        assert t is not None
