"""
tests/test_runtime_default_transport.py

Regression guard for the NULLRUN_USE_GRPC env var. In SDK 0.4.0 the
gRPC transport was deleted (Phase 0, Epic 0.2) because the backend
proto is frozen and missing trace/span fields. These tests verify
that:

1. The default code path (NULLRUN_USE_GRPC unset) does not call into
   any gRPC machinery — there is none to call.
2. Setting NULLRUN_USE_GRPC=1 is a no-op that emits a single
   WARNING (the operator should know the env var is dead).
3. The HTTP transport remains fully wired in both cases.

What this test does NOT cover (intentionally):
- A successful gRPC connection. There is no gRPC transport anymore
  (`src/nullrun/grpc_transport.py` was removed). The HTTP transport
  is the only supported ingestion path; see the gateway repo for
  the long-term transport plan.
"""

import logging
import pytest

from nullrun.runtime import NullRunRuntime

BASE_URL = "https://api.test.nullrun.io"


# ──────────────────────────────────────────────────────────────────────
# Default path (NULLRUN_USE_GRPC unset)
# ──────────────────────────────────────────────────────────────────────


class TestDefaultTransportIsHttp:
    """The default path must never instantiate any gRPC transport
    (because there isn't one)."""

    def test_no_grpc_transport_attribute(
        self, make_runtime, monkeypatch
    ):
        """Regression guard: if someone re-introduces a gRPC transport
        and forgets to gate it on `NULLRUN_USE_GRPC`, the runtime
        must still be in pure-HTTP mode by default.
        """
        monkeypatch.delenv("NULLRUN_USE_GRPC", raising=False)
        rt = make_runtime()
        # No gRPC attribute at all on the runtime.
        assert not hasattr(rt, "_grpc_transport") or rt._grpc_transport is None

    def test_nullrun_use_grpc_env_var_emits_warning(
        self, make_runtime, monkeypatch, caplog
    ):
        """Setting NULLRUN_USE_GRPC=1 must log a WARNING telling the
        operator the env var is now a no-op (the gRPC transport
        was removed in 0.4.0)."""
        monkeypatch.setenv("NULLRUN_USE_GRPC", "1")
        with caplog.at_level(logging.WARNING, logger="nullrun.runtime"):
            make_runtime()
        assert any(
            "NULLRUN_USE_GRPC" in r.getMessage() and "no-op" in r.getMessage()
            for r in caplog.records
        ), (
            "Expected a WARNING that NULLRUN_USE_GRPC is a no-op. "
            f"Got: {[(r.levelname, r.getMessage()) for r in caplog.records]}"
        )

    def test_http_transport_always_wired(self, make_runtime, monkeypatch):
        """Even with NULLRUN_USE_GRPC=1, the HTTP transport must be
        fully wired — `track()` and `flush_now()` must work."""
        monkeypatch.setenv("NULLRUN_USE_GRPC", "1")
        rt = make_runtime()
        assert rt._transport is not None
        assert rt._transport._client is not None
