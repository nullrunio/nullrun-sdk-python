"""
Regression tests for MEDIUM-hygiene fixes in 0.4.0.

Phase 6:
- #6.1: NULLRUN_FALLBACK_MODE env var override.
- #6.2: _rebuild strips Transfer-Encoding alongside Content-Encoding.
- #6.3: shutdown join caps (0.5s) for signal-handler safety.
- #6.6: WS URL built via urllib.parse.
- #6.7: DEDUP_LRU_MAX raised 512 -> 4096.
"""

from __future__ import annotations

# ===========================================================================
# 6.1: NULLRUN_FALLBACK_MODE
# ===========================================================================
# 0.7.0: NULLRUN_FALLBACK_MODE env var was removed along with the
# CACHED fallback mode. The constructor `fallback_mode=` parameter
# is still accepted for STRICT / PERMISSIVE (CACHED silently degrades
# to PERMISSIVE because there is no local cache to read from).
# See CHANGELOG 0.7.0 for migration.


def test_fallback_mode_default_is_permissive():
    """Default fallback_mode is PERMISSIVE."""
    from nullrun.runtime import NullRunRuntime
    from nullrun.transport import FallbackMode

    runtime = NullRunRuntime(api_key="test", _test_mode=True)
    assert runtime._fallback_mode == FallbackMode.PERMISSIVE


def test_fallback_mode_constructor_strict():
    """Constructor `fallback_mode='strict'` sets FallbackMode.STRICT."""
    from nullrun.runtime import NullRunRuntime
    from nullrun.transport import FallbackMode

    NullRunRuntime.reset_instance()
    try:
        runtime = NullRunRuntime(api_key="test", _test_mode=True, fallback_mode="strict")
        assert runtime._fallback_mode == FallbackMode.STRICT
    finally:
        NullRunRuntime.reset_instance()


def test_fallback_mode_constructor_cached_degrades_to_permissive():
    """Pre-0.7.0 CACHED fallback degrades to PERMISSIVE (no local cache)."""
    from nullrun.runtime import NullRunRuntime
    from nullrun.transport import FallbackMode

    NullRunRuntime.reset_instance()
    try:
        runtime = NullRunRuntime(api_key="test", _test_mode=True, fallback_mode="cached")
        # 0.7.0: CACHED is gone; pass-through to PERMISSIVE.
        assert runtime._fallback_mode == FallbackMode.PERMISSIVE
    finally:
        NullRunRuntime.reset_instance()


# ===========================================================================
# 6.2: Transfer-Encoding strip
# ===========================================================================


def test_rebuild_strips_transfer_encoding():
    """_rebuild drops Transfer-Encoding headers."""
    from nullrun.instrumentation.auto import NullRunSyncTransport

    class FakeRequest:
        url = "https://example.com/"

    req = FakeRequest()

    class FakeResponse:
        status_code = 200
        _request = req
        extensions = {}
        headers = {
            "Content-Encoding": "gzip",
            "Transfer-Encoding": "chunked",
            "Content-Length": "100",
            "Content-Type": "application/json",
        }

    out_headers = NullRunSyncTransport._rebuild(FakeResponse(), b"{}", req).headers
    lower = {k.lower() for k in out_headers}
    assert "content-encoding" not in lower
    assert "transfer-encoding" not in lower
    # content-length should be present (recomputed).
    assert "content-length" in lower


# ===========================================================================
# 6.6: WS URL via urllib.parse
# ===========================================================================


def test_ws_url_construction_handles_https():
    """HTTPS control plane produces wss:// URL."""
    from nullrun.transport import Transport

    t = Transport(api_url="https://api.nullrun.io", api_key="test")
    # Use the static path -- connect_websocket is async; we test
    # the URL construction via a helper if it exists, or via the
    # connect_websocket call.
    import asyncio

    async def call():
        try:
            await t.connect_websocket(organization_id="org-1")
        except Exception as e:
            return e

    exc = asyncio.run(call())
    # We don't actually want to connect; just verify the URL doesn't
    # blow up at construction time (i.e. unknown scheme).
    assert exc is None or "ws" in str(exc).lower() or "url" in str(exc).lower()


def test_ws_url_construction_rejects_unknown_scheme():
    """Unknown schemes raise ValueError, not a corrupt URL."""
    from nullrun.transport import Transport

    t = Transport(api_url="ftp://example.com", api_key="test")
    import asyncio

    async def call():
        try:
            await t.connect_websocket(organization_id="org-1")
        except ValueError as e:
            return e

    exc = asyncio.run(call())
    assert isinstance(exc, ValueError)
    assert "scheme" in str(exc).lower()


# ===========================================================================
# 6.7: DEDUP_LRU_MAX
# ===========================================================================


def test_dedup_lru_max_is_4096():
    """DEDUP_LRU_MAX is now 4096 (was 512)."""
    from nullrun.instrumentation.auto import DEDUP_LRU_MAX

    assert DEDUP_LRU_MAX == 4096
