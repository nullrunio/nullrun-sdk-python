"""
Tests for the zero-activity diagnostic (2026-09-22).

When ``@protect`` is invoked many times but no LLM-call event has ever
been recorded, the runtime emits a one-time WARNING so operators can
diagnose the "the gate is enforced but cost tracking shows nothing"
class of silent failures. The diagnostic is implemented on
``NullRunRuntime``:

  * ``_protect_call_count`` — bumped by ``_bump_protect_count()``,
    called from ``@protect`` on every invocation.
  * ``_llm_call_event_count`` — bumped by ``track_llm()`` on every
    successful call.
  * ``_zero_activity_warned`` — set when the warning fires so we
    never spam on long-lived processes.

These tests do NOT exercise the full ``@protect`` wrapping flow; they
poke the counter methods directly to keep the diagnostic logic
isolated from the gate / span / cancel machinery that surrounds it.
"""

from __future__ import annotations

import logging

import pytest

from nullrun.runtime import NullRunRuntime


class _StubRuntime:
    """Drop-in for ``NullRunRuntime`` that exposes only the diagnostic
    surface (``_protect_call_count`` / ``_llm_call_event_count`` /
    ``_zero_activity_warned`` / ``_zero_activity_lock`` /
    ``_bump_protect_count`` / ``_maybe_warn_zero_activity``).

    The real ``NullRunRuntime.__init__`` opens a network connection,
    so we mirror the relevant attribute set on a plain instance and
    bind the diagnostic methods directly. This keeps the test
    hermetic — no backend, no httpx mocks, no async fixtures.
    """

    def __init__(self) -> None:
        import threading

        # Mirror the exact attribute names the diagnostic uses so the
        # implementation runs unchanged.
        self._protect_call_count = 0
        self._llm_call_event_count = 0
        self._zero_activity_warned = False
        self._zero_activity_lock = threading.Lock()

    def _bump_protect_count(self) -> None:  # type: ignore[no-untyped-def]
        self._protect_call_count += 1
        self._maybe_warn_zero_activity()

    def _maybe_warn_zero_activity(self) -> None:  # type: ignore[no-untyped-def]
        # Bind the real implementation from NullRunRuntime so the
        # test exercises the production code path, not a copy.
        NullRunRuntime._maybe_warn_zero_activity(self)


@pytest.fixture
def stub():
    """Fresh stub runtime for each test."""
    return _StubRuntime()


class TestZeroActivityDiagnostic:
    def test_no_warning_below_threshold(self, stub, caplog):
        """49 @protect calls with zero LLM events MUST NOT warn — the
        threshold is 50 so the operator is given a few cycles to wire
        up an LLM call before the warning fires."""
        with caplog.at_level(logging.WARNING, logger="nullrun.runtime"):
            for _ in range(49):
                stub._bump_protect_count()
        assert stub._protect_call_count == 49
        assert not stub._zero_activity_warned
        assert not any(
            "no LLM-call event has been recorded" in rec.message
            for rec in caplog.records
        )

    def test_warns_at_threshold_when_no_llm_events(self, stub, caplog):
        """50 @protect calls with zero LLM-call events MUST warn once."""
        with caplog.at_level(logging.WARNING, logger="nullrun.runtime"):
            for _ in range(50):
                stub._bump_protect_count()
        assert stub._protect_call_count == 50
        assert stub._zero_activity_warned is True
        matching = [
            r for r in caplog.records
            if "no LLM-call event has been recorded" in r.message
        ]
        assert len(matching) == 1, (
            f"expected exactly one warning at threshold, got "
            f"{len(matching)}: {[r.message for r in matching]}"
        )
        assert matching[0].levelno == logging.WARNING

    def test_warn_once_only(self, stub, caplog):
        """After the warning fires, additional @protect calls MUST NOT
        spam the log. The ``_zero_activity_warned`` flag prevents
        log spam on long-lived processes."""
        with caplog.at_level(logging.WARNING, logger="nullrun.runtime"):
            for _ in range(50):
                stub._bump_protect_count()
            # 100 more calls after the warn-once fired.
            for _ in range(100):
                stub._bump_protect_count()
        assert stub._protect_call_count == 150
        assert stub._zero_activity_warned is True
        matching = [
            r for r in caplog.records
            if "no LLM-call event has been recorded" in r.message
        ]
        assert len(matching) == 1, (
            f"warn-once violation: {len(matching)} warnings after 150 "
            f"calls (expected exactly 1)"
        )

    def test_no_warning_after_first_llm_event(self, stub, caplog):
        """The first LLM-call event resets the warning condition. Even
        after 1000 @protect calls, the diagnostic MUST stay silent
        once at least one LLM event has been recorded."""
        stub._llm_call_event_count = 1  # simulate one observed LLM call
        with caplog.at_level(logging.WARNING, logger="nullrun.runtime"):
            for _ in range(1000):
                stub._bump_protect_count()
        assert stub._protect_call_count == 1000
        assert stub._zero_activity_warned is False
        assert not any(
            "no LLM-call event has been recorded" in rec.message
            for rec in caplog.records
        )

    def test_warning_message_mentions_root_causes(self, stub, caplog):
        """The warning text MUST name the three most likely root
        causes so the operator can self-diagnose without consulting
        external docs immediately."""
        with caplog.at_level(logging.WARNING, logger="nullrun.runtime"):
            for _ in range(50):
                stub._bump_protect_count()
        warning = next(
            r for r in caplog.records
            if "no LLM-call event has been recorded" in r.message
        )
        msg = warning.message
        # The message must enumerate the three operational causes so
        # the operator can match their setup against the list.
        assert "httpx" in msg.lower(), (
            "warning text should mention httpx (the most common cause)"
        )
        assert "transport" in msg.lower() or "grpc" in msg.lower(), (
            "warning text should mention custom transports / gRPC"
        )
        assert "langgraph" in msg.lower(), (
            "warning text should mention framework auto-detection"
        )

    def test_concurrent_bumps_warn_at_most_once(self, stub, caplog):
        """Concurrent ``@protect`` calls (e.g. asyncio fanout) MUST NOT
        produce multiple warnings. The lock around the
        read-flag sequence keeps the warn-once invariant under
        concurrent bumps."""

        import threading

        def bump_many() -> None:
            for _ in range(20):
                stub._bump_protect_count()

        threads = [threading.Thread(target=bump_many) for _ in range(10)]
        with caplog.at_level(logging.WARNING, logger="nullrun.runtime"):
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        assert stub._protect_call_count == 200
        assert stub._zero_activity_warned is True
        matching = [
            r for r in caplog.records
            if "no LLM-call event has been recorded" in r.message
        ]
        assert len(matching) == 1, (
            f"concurrent bumps produced {len(matching)} warnings "
            f"(expected exactly 1)"
        )
