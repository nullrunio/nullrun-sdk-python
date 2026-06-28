"""
P0 regression: the gRPC transport was removed in 0.3.1.

The gRPC server at the platform is intentionally frozen until the
activation checklist (TLS, auth, proto extensions, cost pipeline
parity, tests) is complete. The SDK no longer references any
gRPC-related symbols at runtime.

This test pins the post-deletion contract:
  1. ``NullRunRuntime`` does not carry a ``_grpc_transport`` attribute.
  2. Setting ``NULLRUN_USE_GRPC=1`` raises ``RuntimeError`` at SDK
     init (was: silent no-op + INFO log in 0.3.1–0.7.7; fail-LOUD
     as of 0.7.8 so customers can't silently ship a non-functional
     SDK to prod).
  3. ``grpcio`` is NOT a hard dep — the ``pyproject.toml`` only
     lists ``httpx``.

If someone re-introduces gRPC plumbing, this test fails at
collection/import time (the symbol ``_grpc_transport`` is back)
or at runtime (the import-time contract check on the package
metadata breaks).
"""

from __future__ import annotations

from pathlib import Path

import pytest

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

    def test_nullrun_use_grpc_raises_runtime_error(self, make_runtime, monkeypatch):
        """Setting NULLRUN_USE_GRPC=1 must raise RuntimeError at SDK init.

        Contract evolution:
          * 0.3.1: NullRunRuntime.__init__ called ``create_grpc_transport(...)``
            which did not exist, so init crashed with NameError before
            reaching any user code. Silent broken prod.
          * 0.3.1 – 0.7.7: silent no-op + INFO log on nullrun.runtime.
            Still broken, just harder to diagnose from a missing proto
            trace in the dashboard.
          * 0.7.8: explicit RuntimeError so the misconfiguration is
            visible at startup. The CHANGELOG entry under "Deprecated"
            tells the operator to unset the env var.

        The test pins the 0.7.8 contract: setting the env var must
        raise with a message that names the offending variable and
        points the operator at the docs page.
        """
        monkeypatch.setenv("NULLRUN_USE_GRPC", "1")
        with pytest.raises(RuntimeError) as exc_info:
            make_runtime()
        msg = str(exc_info.value)
        assert "NULLRUN_USE_GRPC" in msg, (
            f"RuntimeError must name the offending env var. Got: {msg!r}"
        )
        assert "https://docs.nullrun.io" in msg, (
            "RuntimeError must point operators at the docs page that "
            "explains the migration. Got: " + repr(msg)
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
        hard_block = text[deps_start : next_section if next_section > 0 else None]
        assert "grpcio" not in hard_block, (
            "grpcio must not be a hard dependency of the SDK. "
            "If/when gRPC is unblocked at the platform, it should be "
            "added as a separate optional extra."
        )
