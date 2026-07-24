"""Phase 0 regression tests for human approval on the live /execute path."""

from __future__ import annotations

import threading
import time

import pytest

from nullrun.breaker.exceptions import NullRunBlockedException
from nullrun.observability import metrics


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset()
    yield
    metrics.reset()


def _approval_response(approval_id: str = "approval-1") -> dict[str, object]:
    return {
        "decision": "require_approval",
        "decision_source": "gateway",
        "approval_id": approval_id,
        "approval_timeout_seconds": 1,
        "approval_expires_at": "2026-07-23T15:00:00Z",
        "explanation": "Refund requires approval",
        "policy_version": 1,
    }


def _release_when_registered(runtime, approval_id: str, outcome: str) -> threading.Thread:
    def release() -> None:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with runtime._approval_lock:
                if approval_id in runtime._approval_pending:
                    break
            time.sleep(0.001)
        runtime._handle_approval_resolved(
            {
                "approval_id": approval_id,
                "outcome": outcome,
                "note": "operator decision",
                "resolved_at": 1_700_000_000,
            }
        )

    thread = threading.Thread(target=release, daemon=True)
    thread.start()
    return thread


def test_execute_waits_for_approval_then_rechecks_same_action(make_test_runtime):
    runtime = make_test_runtime()
    runtime.add_sensitive_tool("refund_customer")
    calls: list[dict[str, object]] = []

    def execute_transport(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return _approval_response()
        return {
            "decision": "allow",
            "decision_source": "gateway",
            "policy_version": 1,
        }

    runtime._transport.execute = execute_transport
    release = _release_when_registered(runtime, "approval-1", "approved")

    result = runtime.execute(
        "refund_customer",
        {"kwargs": {"amount_cents": "120000"}},
        mode="strict",
    )
    release.join(timeout=1.0)

    assert result["decision"] == "allow"
    assert len(calls) == 2
    assert calls[0]["tool"] == calls[1]["tool"] == "refund_customer"
    assert calls[0]["input_data"] == calls[1]["input_data"]
    assert calls[1]["approval_id"] == "approval-1"
    assert calls[0]["operation_id"] == calls[1]["operation_id"]
    assert metrics.runtime.execute_allowed == 1


def test_execute_denied_does_not_recheck(make_test_runtime):
    runtime = make_test_runtime()
    runtime.add_sensitive_tool("refund_customer")
    calls: list[dict[str, object]] = []

    def execute_transport(**kwargs):
        calls.append(kwargs)
        return _approval_response("approval-denied")

    runtime._transport.execute = execute_transport
    release = _release_when_registered(runtime, "approval-denied", "denied")

    with pytest.raises(NullRunBlockedException) as exc_info:
        runtime.execute(
            "refund_customer",
            {"kwargs": {"amount_cents": "120000"}},
            mode="strict",
        )
    release.join(timeout=1.0)

    assert len(calls) == 1
    assert "approval denied" in exc_info.value.reason.lower()
    assert metrics.runtime.execute_blocked == 1


def test_execute_require_approval_without_id_fails_closed(make_test_runtime):
    runtime = make_test_runtime()
    runtime.add_sensitive_tool("refund_customer")
    runtime._transport.execute = lambda **_: {
        "decision": "require_approval",
        "decision_source": "gateway",
        "approval_timeout_seconds": 1,
    }

    with pytest.raises(NullRunBlockedException) as exc_info:
        runtime.execute("refund_customer", {}, mode="strict")

    assert "approval_id" in exc_info.value.reason
    assert metrics.runtime.execute_blocked == 1
