"""
Разрыв 1c (2026-07-21) — SDK reads `approval_timeout_seconds`
from the /gate response, not its own env default.

До этой правки SDK использовал `NULLRUN_APPROVAL_TIMEOUT_SECONDS`
env default (300s) как единственный источник wait duration. Если
backend row имел другой `expires_in_seconds` (например, 20s для
коротких approval-правил или 1800s для длинных), SDK timeout'ил
раньше или позже чем backend sweeper — exactly the Разрыв 3 desync
class of bug.

Backend commit 0ad03b9 добавил `approval_timeout_seconds: Option<i64>`
поле в `GateResponse`. SDK теперь:
- prefers `response["approval_timeout_seconds"]` (server-authoritative)
- falls back to env default только когда поле отсутствует/невалидно

Тесты ниже пинят этот контракт: при валидном server timeout
используется он, при отсутствующем — env default, при
невалидном — env default + WARN log.

# Test mechanics (Разрыв 1c, 2026-07-21)

`_wait_for_approval_resolution` creates a NEW `threading.Event()`
inside the function and waits on it. Pre-setting an event from
the outside does not work — the function replaces it. The only
way to release the wait from a test is to call
`_handle_approval_resolved` (the WS push handler), which pops
the pending entry AND sets the event. We exercise this in
every test below: register entry, start a thread that calls
the wait, then from the main thread call
`_handle_approval_resolved` to release.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import pytest

from nullrun.runtime import NullRunRuntime


def _make_runtime(env_timeout: float | None) -> NullRunRuntime:
    """Build a runtime with a specific env default timeout.

    Mirrors the `_test_mode=True` pattern from
    test_init_contract.py — skips auth but lets us exercise the
    approval wait path without a real backend.
    """
    if env_timeout is None:
        os.environ.pop("NULLRUN_APPROVAL_TIMEOUT_SECONDS", None)
    else:
        os.environ["NULLRUN_APPROVAL_TIMEOUT_SECONDS"] = str(env_timeout)
    return NullRunRuntime(
        api_key="test-key-razriv1c-12345678",
        _test_mode=True,
        polling=False,
    )


def _run_wait_and_release(
    rt: NullRunRuntime,
    approval_id: str,
    timeout_seconds: float | None,
    release_after_ms: int = 50,
    outcome: str = "approved",
) -> dict[str, Any]:
    """Spawn a thread that calls _wait_for_approval_resolution;
    from the main thread, simulate the WS push by calling
    _handle_approval_resolved after ``release_after_ms`` ms.

    Returns the entry dict (with ``outcome`` populated) if the
    wait released on the signal, or a ``{timed_out: True, ...}``
    sentinel if the timeout fired first.
    """
    result_box: dict[str, Any] = {}

    def target() -> None:
        result_box["result"] = rt._wait_for_approval_resolution(
            approval_id=approval_id,
            workflow_id="wf-1",
            execution_id="exec-1",
            timeout_seconds=timeout_seconds,
        )

    t = threading.Thread(target=target, daemon=True)
    started = time.monotonic()
    t.start()
    # Release the wait via the WS push handler. This is what
    # would happen in production when the operator clicks
    # Approve/Deny on the dashboard.
    time.sleep(release_after_ms / 1000.0)
    rt._handle_approval_resolved(
        {
            "approval_id": approval_id,
            "outcome": outcome,
            "note": "test release",
            "resolved_at": 1700000000,
        }
    )
    t.join(timeout=5.0)
    result_box["elapsed"] = time.monotonic() - started
    return result_box


def _run_wait_and_timeout(
    rt: NullRunRuntime,
    approval_id: str,
    timeout_seconds: float | None,
) -> dict[str, Any]:
    """Spawn a thread that calls _wait_for_approval_resolution;
    do NOT release the event — let the timeout fire."""
    result_box: dict[str, Any] = {}

    def target() -> None:
        result_box["result"] = rt._wait_for_approval_resolution(
            approval_id=approval_id,
            workflow_id="wf-1",
            execution_id="exec-1",
            timeout_seconds=timeout_seconds,
        )

    t = threading.Thread(target=target, daemon=True)
    started = time.monotonic()
    t.start()
    t.join(timeout=5.0)
    result_box["elapsed"] = time.monotonic() - started
    return result_box


class TestApprovalTimeoutResolution:
    """Pin the Разрыв 1c contract: server timeout wins, env is fallback."""

    def test_server_timeout_used_when_response_has_valid_value(self):
        """DoD #1: server-supplied timeout=15s is the value passed
        to event.wait(), NOT the env default 300s. We assert by
        releasing the event and checking the entry's stored
        timeout_seconds field — this is what `_wait_for_approval_resolution`
        would have passed to event.wait().
        """
        rt = _make_runtime(env_timeout=300.0)
        try:
            assert rt._approval_timeout_seconds == 300.0

            result_box = _run_wait_and_release(
                rt, "appr-server-15", timeout_seconds=15.0,
                release_after_ms=50,
            )

            assert result_box.get("result") is not None
            assert result_box["result"].get("outcome") == "approved", (
                "wait should have released on the WS push, not timed out"
            )
            assert result_box["result"]["timeout_seconds"] == 15.0, (
                "Разрыв 1c: server timeout (15s) must be stored on the "
                f"entry; got {result_box['result']['timeout_seconds']}"
            )
            # Sanity: the wait did NOT consume 15s.
            assert result_box["elapsed"] < 1.0, (
                f"wait took {result_box['elapsed']:.2f}s; expected near-instant"
            )
        finally:
            rt.shutdown(flush=False)

    def test_env_fallback_when_response_omits_field(self):
        """DoD #2 (regression): legacy /gate response WITHOUT
        approval_timeout_seconds -> SDK falls back to env default.
        """
        rt = _make_runtime(env_timeout=42.0)
        try:
            assert rt._approval_timeout_seconds == 42.0

            result_box = _run_wait_and_release(
                rt, "appr-legacy", timeout_seconds=None,
                release_after_ms=50,
            )

            assert result_box.get("result") is not None
            assert result_box["result"]["timeout_seconds"] == 42.0, (
                "Missing server timeout must fall back to env default; "
                f"got {result_box['result']['timeout_seconds']}"
            )
        finally:
            rt.shutdown(flush=False)

    def test_env_fallback_when_server_value_is_zero(self):
        # DoD #3 (regression): response with
        # approval_timeout_seconds=0 or negative -> treat as
        # "missing" and fall back. A zero would deadlock the SDK
        # on the very first event.wait(), so we explicitly reject
        # non-positive values.
        #
        # Sprint 0 (coverage): this test is rare-flaky under
        # pytest-xdist on CI (linux, Python 3.12) — the spawned
        # wait thread occasionally misses the release_after_ms
        # window when the main thread is mid-test-collection, and
        # the entry stays empty so ``result_box.get("result")``
        # is None. ``pytest-rerunfailures`` (already in dev-deps)
        # retries up to 2 times. Local pytest on Windows is
        # unaffected; the failure mode is xdist worker scheduling
        # under load.
        @pytest.mark.rerunfailures(max_retries=2)
        def _check_zero(bad_value: float) -> None:
            rt = _make_runtime(env_timeout=120.0)
            try:
                result_box = _run_wait_and_release(
                    rt, "appr-zero", timeout_seconds=bad_value,
                    release_after_ms=50,
                )
                assert result_box.get("result") is not None
                assert result_box["result"]["timeout_seconds"] == 120.0, (
                    f"Non-positive server timeout ({bad_value}) must fall "
                    f"back to env default 120; got "
                    f"{result_box['result']['timeout_seconds']}"
                )
            finally:
                rt.shutdown(flush=False)

        for bad_value in (0, 0.0, -1, -100.0):
            _check_zero(bad_value)

    def test_env_fallback_when_server_value_is_non_numeric(self):
        """DoD #4: malformed server value -> fall back to env
        default. The check_workflow_budget caller in
        runtime.py:1710-1718 logs a warning and sets
        server_timeout=None before calling
        _wait_for_approval_resolution; this test pins that
        contract from the callee side.
        """
        rt = _make_runtime(env_timeout=90.0)
        try:
            result_box = _run_wait_and_release(
                rt, "appr-bad", timeout_seconds=None,  # pre-validated to None
                release_after_ms=50,
            )
            assert result_box["result"]["timeout_seconds"] == 90.0
        finally:
            rt.shutdown(flush=False)

    def test_timeout_sentinel_returned_when_no_ws_push(self):
        """Regression: when the WS push never arrives, the wait
        hits the timeout and returns the ``{outcome: 'timeout',
        timed_out: True}`` sentinel — NOT raise, NOT block
        forever. The test verifies that with a small
        server_timeout and NO release, the function returns the
        sentinel within that timeout + overhead. Note that the
        timeout sentinel does NOT carry `timeout_seconds` (it's
        a fresh dict, not the entry) — only `outcome`,
        `timed_out`, `approval_id`.

        Phase 0 review (2026-07-23): the test used
        `timeout_seconds=0.1` to keep the suite fast. After the
        clamp to `[MIN_APPROVAL_TIMEOUT_SECONDS=1,
        MAX_APPROVAL_TIMEOUT_SECONDS=3600]`, sub-1s values now
        fall back to the env default 300s. The test instead
        pins a 1.5s timeout (in-range) and a 5s upper bound on
        the elapsed wait. Production coverage of the validator
        itself lives in `test_validate_approval_timeout_*`.
        """
        rt = _make_runtime(env_timeout=300.0)
        try:
            result_box = _run_wait_and_timeout(
                rt, "appr-silent", timeout_seconds=1.5,
            )
            assert result_box.get("result") is not None
            assert result_box["result"]["outcome"] == "timeout"
            assert result_box["result"]["timed_out"] is True
            assert result_box["result"]["approval_id"] == "appr-silent"
            # Sanity: the wait elapsed near 1.5s (the new minimum
            # in-range timeout that the validator accepts), not the
            # env default 300s.
            assert 1.0 < result_box["elapsed"] < 5.0, (
                f"timeout took {result_box['elapsed']:.2f}s; "
                "expected near 1.5s (in-range server timeout), not 300s (env)"
            )
        finally:
            rt.shutdown(flush=False)

    def test_diverging_server_value_logs_at_debug(self, caplog):
        """When the server timeout diverges from the env
        default, _wait_for_approval_resolution logs a DEBUG
        line so an operator inspecting logs can see which value
        drove the wait.
        """
        rt = _make_runtime(env_timeout=300.0)
        try:
            with caplog.at_level("DEBUG", logger="nullrun.runtime"):
                _run_wait_and_release(
                    rt, "appr-debug", timeout_seconds=15.0,
                    release_after_ms=50,
                )
            debug_messages = [
                r.message for r in caplog.records
                if r.levelname == "DEBUG" and "using server timeout" in r.message
            ]
            assert len(debug_messages) >= 1, (
                "Разрыв 1c: diverging server timeout should emit a DEBUG log. "
                f"Got caplog records: {[r.message for r in caplog.records]}"
            )
        finally:
            rt.shutdown(flush=False)


# ---------------------------------------------------------------------------
# Phase 0 review (2026-07-23): server-timeout clamp to
# [MIN_APPROVAL_TIMEOUT_SECONDS, MAX_APPROVAL_TIMEOUT_SECONDS].
# Pre-fix only `> 0` was rejected, so a server advertising
# 1e9 seconds would lock the calling thread for years. The
# helper now refuses any out-of-range value.
# ---------------------------------------------------------------------------


def _validate_approval_timeout(value, log_prefix):
    """Mirror the runtime.py helper for direct unit testing."""
    from nullrun.runtime import _validate_approval_timeout as helper

    return helper(value, log_prefix)


def test_validate_approval_timeout_accepts_in_range_value():
    from nullrun.runtime import MAX_APPROVAL_TIMEOUT_SECONDS, MIN_APPROVAL_TIMEOUT_SECONDS

    for in_range in (1.0, 5.0, 60.0, 3600.0, MAX_APPROVAL_TIMEOUT_SECONDS):
        assert _validate_approval_timeout(in_range, "t") == in_range
    for in_range in (1, 60, 3600):
        # ints must coerce to float
        assert _validate_approval_timeout(in_range, "t") == float(in_range)


def test_validate_approval_timeout_rejects_below_min():
    for below in (0, 0.0, -1, -100.0, 0.99):
        assert _validate_approval_timeout(below, "t") is None


def test_validate_approval_timeout_rejects_above_max():
    from nullrun.runtime import MAX_APPROVAL_TIMEOUT_SECONDS

    for above in (MAX_APPROVAL_TIMEOUT_SECONDS + 1, 1e9, 10_000_000.0):
        assert _validate_approval_timeout(above, "t") is None


def test_validate_approval_timeout_rejects_non_numeric():
    for bad in ("abc", "5x", [], {}, [1, 2, 3]):
        assert _validate_approval_timeout(bad, "t") is None


def test_validate_approval_timeout_rejects_none():
    assert _validate_approval_timeout(None, "t") is None

