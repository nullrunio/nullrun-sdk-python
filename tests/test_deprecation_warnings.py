"""
Sprint 3 follow-up: regression tests for the deprecation warnings
emitted by the SDK.

The only deprecation warning currently in the SDK is for
``NULLRUN_FALLBACK_MODE``, which is scheduled for removal in 0.5.0
in favour of the typed ``on_transport_error`` parameter on
``Transport.execute()``.

These tests pin the warning contract:
  - The warning fires once when ``NULLRUN_FALLBACK_MODE`` is set
    at NullRunRuntime construction time.
  - The warning does NOT fire when the user passes
    ``fallback_mode=`` to the constructor (the new path).
  - The warning does NOT fire when no env var is set (the default
    PERMISSIVE path is silent).
  - The warning's message points to ``on_transport_error`` so an
    operator can grep and find the migration path.
"""
from __future__ import annotations

import os
import warnings


class TestNullRunFallbackModeDeprecation:
    """``NULLRUN_FALLBACK_MODE`` env var must emit a DeprecationWarning."""

    def _build_runtime(self, monkeypatch, env_value):
        """Construct a NullRunRuntime with the env var set/cleared.

        Uses ``_test_mode=True`` to skip the auth handshake and
        policy fetch (otherwise the test would hit the real
        gateway). Returns the runtime and the list of
        DeprecationWarnings captured during construction.
        """
        from nullrun.runtime import NullRunRuntime

        if env_value is None:
            monkeypatch.delenv("NULLRUN_FALLBACK_MODE", raising=False)
        else:
            monkeypatch.setenv("NULLRUN_FALLBACK_MODE", env_value)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            rt = NullRunRuntime(
                api_key="test-key-12345678",
                api_url="https://api.test.nullrun.io",
                _test_mode=True,
            )
            rt.shutdown()

        dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        return rt, dep

    def test_env_var_emits_deprecation_warning(self, monkeypatch):
        """Setting ``NULLRUN_FALLBACK_MODE`` must emit a DeprecationWarning."""
        _, dep = self._build_runtime(monkeypatch, "strict")
        assert dep, (
            "No DeprecationWarning emitted when NULLRUN_FALLBACK_MODE is set. "
            "Sprint 3.2 wiring: runtime.py:328-335 should emit one."
        )
        msg = str(dep[0].message)
        assert "NULLRUN_FALLBACK_MODE" in msg
        assert "on_transport_error" in msg, (
            f"DeprecationWarning message must point to the migration path "
            f"``on_transport_error``; got: {msg}"
        )

    def test_env_var_still_works_for_backward_compat(self, monkeypatch):
        """The env var must still set the fallback mode despite the warning."""
        from nullrun.transport import FallbackMode

        _, _ = self._build_runtime(monkeypatch, "strict")
        # Re-build to read the runtime's _fallback_mode after
        # construction completed successfully. (The previous
        # _build_runtime shut down the runtime, so we
        # construct again here, suppressing the warning.)
        from nullrun.runtime import NullRunRuntime
        monkeypatch.setenv("NULLRUN_FALLBACK_MODE", "strict")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            rt = NullRunRuntime(
                api_key="test-key-12345678",
                api_url="https://api.test.nullrun.io",
                _test_mode=True,
            )
            try:
                assert rt._fallback_mode == FallbackMode.STRICT, (  # noqa: SLF001
                    f"NULLRUN_FALLBACK_MODE=strict should set STRICT mode; "
                    f"got {rt._fallback_mode!r}"  # noqa: SLF001
                )
            finally:
                rt.shutdown()

    def test_no_env_var_no_warning(self, monkeypatch):
        """Without the env var, no DeprecationWarning must fire."""
        _, dep = self._build_runtime(monkeypatch, None)
        assert not dep, (
            f"Unexpected DeprecationWarning(s) with no env var: "
            f"{[str(w.message) for w in dep]}"
        )

    def test_constructor_arg_does_not_emit_warning(self, monkeypatch):
        """The new ``fallback_mode=`` constructor arg must not warn.

        The whole point of Sprint 3.2 is to give the user a
        non-deprecated path. If passing ``fallback_mode=strict``
        to the constructor also emits the warning, the
        migration story is broken (the user can't escape the
        warning by adopting the new API).
        """
        from nullrun.runtime import NullRunRuntime

        monkeypatch.delenv("NULLRUN_FALLBACK_MODE", raising=False)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            rt = NullRunRuntime(
                api_key="test-key-12345678",
                api_url="https://api.test.nullrun.io",
                fallback_mode="strict",  # new constructor arg
                _test_mode=True,
            )
            rt.shutdown()

        dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        # No DeprecationWarning must mention NULLRUN_FALLBACK_MODE
        # (the warning is specifically about the env var).
        relevant = [w for w in dep if "NULLRUN_FALLBACK_MODE" in str(w.message)]
        assert not relevant, (
            f"Constructor arg path emitted the env-var deprecation warning: "
            f"{[str(w.message) for w in relevant]}"
        )

    def test_warning_message_mentions_removal_version(self, monkeypatch):
        """The warning must tell the user when the env var is going away."""
        _, dep = self._build_runtime(monkeypatch, "permissive")
        assert dep, "Expected DeprecationWarning for NULLRUN_FALLBACK_MODE"
        msg = str(dep[0].message)
        assert "0.5.0" in msg, (
            f"DeprecationWarning should mention the removal version "
            f"(0.5.0); got: {msg}"
        )
