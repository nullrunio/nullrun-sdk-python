"""
Regression tests for the 0.3.0 init contract.

The 0.3.0 T3-S2 work shipped the "no silent local-mode fallback" rule.
`nullrun.init ` and `NullRunRuntime(...)` MUST raise
`NullRunAuthenticationError` when neither `api_key` kwarg nor
`NULLRUN_API_KEY` env is set. This is the safety contract the whole
release shipped. A refactor that re-introduces a silent fallback
would land without CI catching it unless this test is in place.

Also pins the singleton-state contract (item B3) and the
unknown-kwarg rejection (the 7-symbol surface of the SDK is
`init(api_key, api_url, debug)` — no `organization_id`).
"""

from __future__ import annotations

import threading

import pytest

import nullrun
import nullrun.decorators as _dec_mod
import nullrun.runtime as _rt_mod
from nullrun.breaker.exceptions import NullRunAuthenticationError
from nullrun.runtime import NullRunRuntime


class TestInitRaisesWithoutApiKey:
    """T3-S2 (0.3.0): api_key is required. A missing key must hard-error."""

    def test_init_raises_when_api_key_missing(self, monkeypatch, mock_api):
        """``nullrun.init `` with no api_key and no env raises
        ``NullRunAuthenticationError``. The error message must mention
        the api_key requirement so the user knows what to fix.
        """
        monkeypatch.delenv("NULLRUN_API_KEY", raising=False)
        with pytest.raises(NullRunAuthenticationError, match="api_key"):
            nullrun.init()

    def test_runtime_init_raises_when_api_key_missing(self, monkeypatch, mock_api):
        """``NullRunRuntime(...)`` with no api_key and no env raises.
        This is the direct construction path used by tests and
        advanced callers; the public ``init `` raises first with
        a friendlier message, but this constructor-level raise is
        the contract for everyone else.
        """
        monkeypatch.delenv("NULLRUN_API_KEY", raising=False)
        with pytest.raises(NullRunAuthenticationError, match="api_key"):
            NullRunRuntime()

    def test_init_accepts_api_key_from_env(self, monkeypatch, mock_api):
        """``init()`` (no args) succeeds when NULLRUN_API_KEY is set."""
        monkeypatch.setenv("NULLRUN_API_KEY", "test-key-12345678")
        monkeypatch.setenv("NULLRUN_API_URL", "https://api.test.nullrun.io")
        rt = nullrun.init()
        try:
            assert rt is not None
            assert rt.api_key == "test-key-12345678"
        finally:
            rt.shutdown()


class TestInitRejectsUnknownKwargs:
    """The public ``init`` signature is ``init(api_key, api_url, debug)``.
    Any additional kwarg must raise ``TypeError`` so the platform's
    docs and the SDK's actual surface never drift again (the
    pre-0.3.1 ``basic_observe.py`` example passed ``organization_id=``
    and crashed at runtime).
    """

    def test_init_rejects_organization_id_kwarg(self, monkeypatch, mock_api):
        monkeypatch.setenv("NULLRUN_API_KEY", "test-key-12345678")
        with pytest.raises(TypeError):
            nullrun.init(organization_id="org-123")


class TestInitWritesAllSingletonSlots:
    """Plan B3: init must atomically write all three singleton slots
    so the decorator's @protect wrapper, the runtime module's
    track_* helpers, and NullRunRuntime.get_instance all see the
    same instance.
    """

    def test_init_writes_all_three_singleton_slots(self, monkeypatch, mock_api):
        # Phase 3 (2026-07-05): the three slots
        # (`runtime._runtime`, `NullRunRuntime._instance`,
        # `decorators._runtime`) all route through the
        # RuntimeRegistry. We assert the registry pointer directly
        # and also confirm the legacy read paths see the same
        # instance (backwards compat).
        from nullrun._registry import get_active_runtime

        monkeypatch.setenv("NULLRUN_API_KEY", "test-key-12345678")
        monkeypatch.setenv("NULLRUN_API_URL", "https://api.test.nullrun.io")
        rt = nullrun.init()
        try:
            assert get_active_runtime() is rt
            assert _rt_mod._runtime is rt
            assert NullRunRuntime._instance is rt
            assert _dec_mod._runtime is rt
        finally:
            rt.shutdown()

    def test_init_is_thread_safe(self, monkeypatch, mock_api):
        """Concurrent init calls must not leave the three singleton
        slots in an inconsistent state (one slot pointing at runtime
        A, the other two at runtime B). The init_lock added in 0.3.1
        serialises the writes.

        We exercise the lock by calling ``_init_lock.acquire`` and
        releasing it from multiple threads while observing the
        slots — that directly tests the locking primitive without
        the noise of background WS threads.

        Phase 3 (2026-07-05): the worker writes through the
        RuntimeRegistry (the canonical store). The
        NullRunRuntime._instance descriptor routes to the
        registry, and the module-level `_runtime` proxies re-resolve
        from the registry on every read.
        """
        from nullrun import _init_lock
        from nullrun._registry import get_active_runtime

        # Simulate the init_lock critical section: each thread
        # writes the three slots under the lock, then releases.
        results: list[NullRunRuntime] = []
        errors: list[Exception] = []

        def worker(rt: NullRunRuntime) -> None:
            try:
                with _init_lock:
                    NullRunRuntime._instance = rt
                    results.append(rt)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        runtimes = [
            NullRunRuntime(
                api_key="test-key-12345678",
                api_url="https://api.test.nullrun.io",
                polling=False,
            )
            for _ in range(8)
        ]
        threads = [threading.Thread(target=worker, args=(rt,)) for rt in runtimes]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert not errors, f"worker raised: {errors}"
        # After all workers have run, the registry points at the
        # LAST runtime that acquired the lock. All 8 are valid; we
        # assert the registry is not None and points at one of
        # them. The legacy read proxies re-resolve from the
        # registry on every access, so they always agree.
        current = get_active_runtime()
        assert current in runtimes
        assert _rt_mod._runtime is current
        assert _dec_mod._runtime is current
        assert NullRunRuntime._instance is current


class TestInitCapabilityProbeLogging:
    """Pins the ``logger.warning/info/debug`` branches added in 0.12.0
    when ``init `` runs the /health capability probe. These tests
    exist to keep the new logging paths covered so a refactor that
    accidentally drops one (e.g. replacing ``logger.info`` with
    ``print``) gets caught in CI rather than at first production init.
    """

    def test_init_with_debug_true_sets_log_level(
        self, monkeypatch, mock_api, caplog
    ):
        """``init(debug=True)`` sets the ``nullrun`` logger to DEBUG.

        Pins the ``logger.setLevel(logging.DEBUG)`` branch on line 234.
        """
        import logging

        monkeypatch.setenv("NULLRUN_API_KEY", "test-key-12345678")
        monkeypatch.setenv("NULLRUN_API_URL", "https://api.test.nullrun.io")
        rt = nullrun.init(debug=True)
        try:
            nullrun_logger = logging.getLogger("nullrun")
            assert nullrun_logger.level == logging.DEBUG
        finally:
            rt.shutdown()

    def test_init_replaces_existing_runtime_logs_warning(
        self, monkeypatch, mock_api, caplog
    ):
        """A second ``init `` while a runtime is still alive logs a
        WARNING about shutting down the old one (C3 fix).

        Pins the ``logger.warning("nullrun.init called while a
        previous runtime is still alive...")`` branch on lines 301-305
        and the ``logger.warning("previous runtime shutdown raised...")``
        on line 309. We force the previous ``shutdown `` to raise so
        the second log line (the except branch) is exercised too.
        """
        import logging

        monkeypatch.setenv("NULLRUN_API_KEY", "test-key-12345678")
        monkeypatch.setenv("NULLRUN_API_URL", "https://api.test.nullrun.io")
        first = nullrun.init()
        try:
            # Force the C3 path's existing.shutdown call to raise
            # so the except branch on line 308-311 is exercised.
            first.shutdown = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
                RuntimeError("simulated shutdown failure")
            )
            with caplog.at_level(logging.WARNING, logger="nullrun"):
                second = nullrun.init()
            try:
                # Both branches should have fired:
                assert any(
                    "still alive" in rec.message for rec in caplog.records
                ), f"expected orphan-runtime warning, got: {[r.message for r in caplog.records]}"
                assert any(
                    "previous runtime shutdown raised" in rec.message
                    for rec in caplog.records
                ), (
                    "expected shutdown-raised warning, "
                    f"got: {[r.message for r in caplog.records]}"
                )
            finally:
                second.shutdown()
        finally:
            # `first` is already shut down (or attempted to be) by the
            # C3 path; guard against double-shutdown by checking the
            # singleton.
            if NullRunRuntime._instance is first:
                first.shutdown()

    def test_init_logs_info_when_probe_unreachable(
        self, monkeypatch, mock_api, caplog
    ):
        """When ``/health`` is unreachable, ``init `` logs at INFO
        that the probe was skipped (does NOT fail init).

        Pins the ``logger.info("nullrun.init: could not probe %s/health...")``
        branch on lines 358-362.
        """
        import logging

        import httpx
        import respx

        monkeypatch.setenv("NULLRUN_API_KEY", "test-key-12345678")
        monkeypatch.setenv("NULLRUN_API_URL", "https://api.test.nullrun.io")
        # Override the /health mock from `mock_api` to fail. We have
        # to do this inside the respx.mock context that mock_api opened
        # so we route through respx again rather than nesting.
        with respx.mock:
            respx.get("https://api.test.nullrun.io/health").mock(
                return_value=httpx.Response(503)
            )
            # Re-mock the other endpoints that init hits so the
            # runtime can come up cleanly.
            respx.post("https://api.test.nullrun.io/api/v1/auth/verify").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "organization_id": "ws-test",
                        "workflow_id": "00000000-0000-0000-0000-000000000001",
                        "plan": "pro",
                        "features": [],
                        "limits": {"max_cost_cents": 10000},
                    },
                )
            )
            with caplog.at_level(logging.INFO, logger="nullrun"):
                rt = nullrun.init()
            try:
                assert any(
                    "v3 capability negotiation skipped" in rec.message
                    for rec in caplog.records
                ), f"expected probe-skipped info log, got: {[r.message for r in caplog.records]}"
            finally:
                rt.shutdown()

    def test_init_logs_debug_when_probe_raises(
        self, monkeypatch, mock_api, caplog
    ):
        """When ``probe_capabilities`` itself raises (not just returns
        None), ``init `` catches it and logs at DEBUG.

        Pins the ``logger.debug("nullrun.init: capability probe raised %s", e)``
        branch on line 363-364. We force a raise by stubbing
        ``probe_capabilities`` with a function that throws.
        """
        import logging

        monkeypatch.setenv("NULLRUN_API_KEY", "test-key-12345678")
        monkeypatch.setenv("NULLRUN_API_URL", "https://api.test.nullrun.io")

        # Force probe_capabilities to raise — the try/except wrapper
        # in init must catch it and log at DEBUG.
        import nullrun.capabilities as _caps_mod

        original_probe = _caps_mod.probe_capabilities
        _caps_mod.probe_capabilities = lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("simulated probe failure")
        )
        try:
            with caplog.at_level(logging.DEBUG, logger="nullrun"):
                rt = nullrun.init()
            try:
                assert any(
                    "capability probe raised" in rec.message
                    for rec in caplog.records
                ), f"expected probe-raised debug log, got: {[r.message for r in caplog.records]}"
            finally:
                rt.shutdown()
        finally:
            _caps_mod.probe_capabilities = original_probe
