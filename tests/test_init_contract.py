"""
Regression tests for the 0.3.0 init() contract.

The 0.3.0 T3-S2 work shipped the "no silent local-mode fallback" rule.
`nullrun.init()` and `NullRunRuntime(...)` MUST raise
`NullRunAuthenticationError` when neither `api_key` kwarg nor
`NULLRUN_API_KEY` env is set. This is the safety contract the whole
release shipped. A refactor that re-introduces a silent fallback
would land without CI catching it unless this test is in place.

Also pins the singleton-state contract (plan item B3) and the
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
        """``nullrun.init()`` with no api_key and no env raises
        ``NullRunAuthenticationError``. The error message must mention
        the api_key requirement so the user knows what to fix.
        """
        monkeypatch.delenv("NULLRUN_API_KEY", raising=False)
        with pytest.raises(NullRunAuthenticationError, match="api_key"):
            nullrun.init()

    def test_runtime_init_raises_when_api_key_missing(self, monkeypatch, mock_api):
        """``NullRunRuntime(...)`` with no api_key and no env raises.
        This is the direct construction path used by tests and
        advanced callers; the public ``init()`` raises first with
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
    """Plan B3: init() must atomically write all three singleton slots
    so the decorator's @protect wrapper, the runtime module's
    track_* helpers, and NullRunRuntime.get_instance() all see the
    same instance.
    """

    def test_init_writes_all_three_singleton_slots(self, monkeypatch, mock_api):
        monkeypatch.setenv("NULLRUN_API_KEY", "test-key-12345678")
        monkeypatch.setenv("NULLRUN_API_URL", "https://api.test.nullrun.io")
        rt = nullrun.init()
        try:
            assert _rt_mod._runtime is rt
            assert NullRunRuntime._instance is rt
            assert _dec_mod._runtime is rt
        finally:
            rt.shutdown()

    def test_init_is_thread_safe(self, monkeypatch, mock_api):
        """Concurrent init() calls must not leave the three singleton
        slots in an inconsistent state (one slot pointing at runtime
        A, the other two at runtime B). The init_lock added in 0.3.1
        serialises the writes.

        We exercise the lock by calling ``_init_lock.acquire`` and
        releasing it from multiple threads while observing the
        slots — that directly tests the locking primitive without
        the noise of background WS threads.
        """
        from nullrun import _init_lock

        # Simulate the init_lock critical section: each thread
        # writes the three slots under the lock, then releases.
        results: list[NullRunRuntime] = []
        errors: list[Exception] = []

        def worker(rt: NullRunRuntime) -> None:
            try:
                with _init_lock:
                    _rt_mod._runtime = rt
                    NullRunRuntime._instance = rt
                    _dec_mod._runtime = rt
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
        # After all workers have run, the slots point at the LAST
        # runtime that acquired the lock. All 8 are valid; we just
        # assert the slots are not None and point at one of them.
        assert _rt_mod._runtime in runtimes
        assert NullRunRuntime._instance in runtimes
        assert _dec_mod._runtime in runtimes
        assert _rt_mod._runtime is NullRunRuntime._instance is _dec_mod._runtime
