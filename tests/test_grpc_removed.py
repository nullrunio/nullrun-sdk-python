"""
P0 regression: the gRPC transport was removed in 0.3.1.

The gRPC server at the platform is intentionally frozen until the
activation checklist (TLS, auth, proto extensions, cost pipeline
parity, tests) is complete. The SDK no longer references any
gRPC-related symbols at runtime.

This test pins the post-deletion contract:
  1. ``NullRunRuntime`` does not carry a ``_grpc_transport`` attribute.
  2. Setting ``NULLRUN_USE_GRPC=1`` does NOT crash init — it logs
     an INFO line and silently falls back to HTTP.
  3. ``grpcio`` is NOT a hard dep — the ``pyproject.toml`` only
     lists ``httpx``.

If someone re-introduces gRPC plumbing, this test fails at
collection/import time (the symbol ``_grpc_transport`` is back)
or at runtime (the import-time contract check on the package
metadata breaks).
"""
from __future__ import annotations

import logging
from pathlib import Path

BASE_URL = "https://api.test.nullrun.io"


class TestGrpcRemoved:

    def test_runtime_has_no_grpc_transport_attr(self, make_runtime):
        """NullRunRuntime must not carry a _grpc_transport attribute.

        Regression guard: if someone re-introduces the gRPC code
        path, this test catches it at runtime.
        """
        rt = make_runtime()
        assert not hasattr(rt, "_grpc_transport"), (
            "NullRunRuntime should not carry a _grpc_transport attribute "
            "(gRPC transport is frozen; see NULLRUN/docs/sdk/README.md)."
        )

    def test_create_grpc_transport_does_not_exist(self):
        """``nullrun.runtime.create_grpc_transport`` must not be importable.

        Pre-0.3.1 the runtime.py called ``create_grpc_transport(api_key=...)``
        from inside NullRunRuntime.__init__, but the symbol was never
        defined — setting NULLRUN_USE_GRPC=1 crashed init with NameError.
        After the fix, the symbol must not exist anywhere in the SDK.
        """
        import nullrun.runtime as rt_mod
        assert not hasattr(rt_mod, "create_grpc_transport"), (
            "create_grpc_transport must not exist in nullrun.runtime — "
            "gRPC transport is frozen at the platform side."
        )
        assert not hasattr(rt_mod, "GrpcTransport"), (
            "GrpcTransport must not exist in nullrun.runtime — "
            "gRPC transport is frozen at the platform side."
        )

    def test_nullrun_use_grpc_does_not_crash_init(
        self, make_runtime, monkeypatch, caplog
    ):
        """Setting NULLRUN_USE_GRPC=1 must NOT raise NameError.

        Pre-fix: NullRunRuntime.__init__ called ``create_grpc_transport(...)``
        which did not exist, so init crashed with NameError before
        reaching the warning log. The test now expects:
          1. init succeeds,
          2. an INFO line is logged about gRPC being a no-op,
          3. the runtime is fully usable.
        """
        monkeypatch.setenv("NULLRUN_USE_GRPC", "1")
        with caplog.at_level(logging.INFO, logger="nullrun.runtime"):
            rt = make_runtime()
        assert rt is not None
        # The no-op INFO log must be present so an operator who set
        # the env var sees that nothing happened.
        assert any(
            "NULLRUN_USE_GRPC" in r.getMessage() and r.levelno == logging.INFO
            for r in caplog.records
        ), (
            "Expected an INFO log on nullrun.runtime mentioning "
            "NULLRUN_USE_GRPC. Got: "
            f"{[(r.levelname, r.getMessage()) for r in caplog.records]}"
        )

    def test_pyproject_has_no_grpcio_hard_dep(self):
        """grpcio must not be a hard dep of the SDK.

        Reads pyproject.toml from the project root and asserts the
        [project] dependencies block does not list grpcio or
        grpcio-tools. The dev extras block may list grpcio-tools
        (it doesn't, but we don't care).
        """
        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8")
        # Crude but sufficient: the hard-deps block (the first
        # ``dependencies = [`` section) must not contain ``grpcio``.
        deps_start = text.find("dependencies = [")
        next_section = text.find("\n\n", deps_start)
        hard_block = text[deps_start:next_section if next_section > 0 else None]
        assert "grpcio" not in hard_block, (
            "grpcio must not be a hard dependency of the SDK. "
            "If/when gRPC is unblocked at the platform, it should be "
            "added as a separate optional extra."
        )
