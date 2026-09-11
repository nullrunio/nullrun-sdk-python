"""Regression tests for DEF-EXECUTE-CAPTURE-WIRING (2026-09-11).

The /execute require_approval arm mints a FRESH server-side
execution_id for the approval row (backend v3.79 echo via
``reservation_id``). Pre-fix ``runtime.execute`` did NOT call
``_capture_server_minted_execution_id`` on the /execute response,
so the contextvar stayed at the previous /gate-captured value.
On the post-approval /execute re-fire, the SDK then sent the OLD
execution_id; ``consume_approved``'s ``WHERE execution_id = $3``
missed the row stamped with the freshly-minted id and fell
through to the terminal ReplayRejected branch
(``APPROVAL_REPLAY_REJECTED`` → SDK NR-A015).

These tests pin both halves of the fix:

  1. ``runtime.execute`` captures ``reservation_id`` into the
     contextvar immediately after ``_transport.execute`` returns.
  2. ``runtime.execute`` passes the captured id (not the
     ``workflow_id`` sentinel) to ``_wait_for_approval_resolution``.

Test isolation: each test resets the contextvar at setup (the
conftest fixture does this globally) and uses
``make_test_runtime`` so the WS state is fresh.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

import pytest

from nullrun.context import (
    get_server_minted_execution_id,
    set_server_minted_execution_id,
)
from nullrun.observability import metrics


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset()
    yield
    metrics.reset()


def _approval_response(reservation_id: str, approval_id: str) -> dict[str, Any]:
    """Wire shape for /execute require_approval (v3.79+)."""
    return {
        "decision": "require_approval",
        "decision_source": "gateway",
        "approval_id": approval_id,
        "approval_timeout_seconds": 1,
        "approval_expires_at": "2026-09-11T10:30:40Z",
        "reservation_id": reservation_id,
        "execution_id": reservation_id,  # mirror — used by some SDK paths
        "explanation": "Approval required",
        "policy_version": 1,
    }


def _release_when_registered(
    runtime, approval_id: str, outcome: str
) -> threading.Thread:
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


def test_execute_captures_reservation_id_from_response(make_test_runtime):
    """Pin #1: ``runtime.execute`` MUST call
    ``_capture_server_minted_execution_id`` on the result so the
    contextvar tracks the freshly-minted id.

    Pre-fix the contextvar would stay at whatever was set before
    the call (here: the previous /gate-minted value).
    """
    runtime = make_test_runtime()
    runtime.add_sensitive_tool("refund_customer")

    prior_gate_eid = "01a08ffa-1234-7700-8000-000000000001"
    fresh_eid = "01a0900b-aaaa-7fff-8000-000000000099"
    set_server_minted_execution_id(prior_gate_eid)
    assert get_server_minted_execution_id() == prior_gate_eid

    calls: list[dict[str, Any]] = []

    def execute_transport(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return _approval_response(reservation_id=fresh_eid, approval_id="ap-1")
        return {
            "decision": "allow",
            "decision_source": "gateway",
            "policy_version": 1,
        }

    runtime._transport.execute = execute_transport
    release = _release_when_registered(runtime, "ap-1", "approved")

    result = runtime.execute(
        "refund_customer",
        {"kwargs": {"amount_cents": "120000"}},
        mode="strict",
    )
    release.join(timeout=1.0)

    # The contextvar MUST now reflect the freshly-minted reservation_id.
    assert get_server_minted_execution_id() == fresh_eid, (
        "DEF-EXECUTE-CAPTURE-WIRING: runtime.execute did NOT capture the "
        "freshly-minted reservation_id from the /execute response. "
        f"contextvar={get_server_minted_execution_id()!r}, "
        f"expected={fresh_eid!r}"
    )
    # Re-fire MUST have used the captured (fresh) execution_id, not the
    # stale prior one.
    assert len(calls) == 2
    assert calls[1]["execution_id"] == fresh_eid, (
        "DEF-EXECUTE-CAPTURE-WIRING: re-fire /execute used stale "
        f"execution_id={calls[1]['execution_id']!r} instead of the "
        f"freshly-captured one={fresh_eid!r}"
    )
    assert calls[1]["approval_id"] == "ap-1"
    assert result["decision"] == "allow"


def test_execute_wait_for_approval_receives_captured_eid(make_test_runtime):
    """Pin #2: ``_wait_for_approval_resolution`` MUST receive the
    captured execution_id, not the workflow_id sentinel.

    Pre-fix the SDK passed ``str(workflow_id or UNKNOWN_WORKFLOW_ID)``
    which degenerated to ``"__nullrun_unknown__"`` and surfaced into
    demo exception messages via ``exc.workflow_id``. The handler
    ignores the value (matches on approval_id only), so this is
    diagnostic — but a regression test pins the wire-shape so the
    log lines + entry metadata stay accurate.
    """
    runtime = make_test_runtime()
    runtime.add_sensitive_tool("refund_customer")

    fresh_eid = "01a0900b-bbbb-7fff-8000-000000000abc"
    set_server_minted_execution_id(fresh_eid)
    assert get_server_minted_execution_id() == fresh_eid

    observed_entries: dict[str, dict[str, Any]] = {}
    real_wait = runtime._wait_for_approval_resolution

    def spy_wait_for_approval_resolution(
        *, approval_id, workflow_id, execution_id, timeout_seconds=None
    ):
        observed_entries[approval_id] = {
            "workflow_id": workflow_id,
            "execution_id": execution_id,
        }
        # Synthesize a fast outcome so the test returns immediately.
        return {"outcome": "approved"}

    runtime._wait_for_approval_resolution = spy_wait_for_approval_resolution

    def execute_transport(**_):
        return _approval_response(reservation_id=fresh_eid, approval_id="ap-2")

    runtime._transport.execute = execute_transport

    # Bypass the real re-fire: when the spied wait returns "approved",
    # runtime.execute calls _transport.execute again to consume the
    # grant. Provide an allow response for that second call.
    real_transport_execute = execute_transport
    calls: list[dict[str, Any]] = []

    def two_phase_execute(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return _approval_response(
                reservation_id=fresh_eid, approval_id="ap-2"
            )
        return {
            "decision": "allow",
            "decision_source": "gateway",
            "policy_version": 1,
        }

    runtime._transport.execute = two_phase_execute

    result = runtime.execute(
        "refund_customer",
        {"kwargs": {"amount_cents": "120000"}},
        mode="strict",
    )

    assert observed_entries, (
        "_wait_for_approval_resolution was never called from runtime.execute"
    )
    entry = observed_entries["ap-2"]
    assert entry["execution_id"] == fresh_eid, (
        "DEF-EXECUTE-CAPTURE-WIRING: _wait_for_approval_resolution was "
        f"passed execution_id={entry['execution_id']!r}; expected the "
        f"captured server-minted id={fresh_eid!r}"
    )
    assert result["decision"] == "allow"
    # Sanity: the second /execute call (re-fire with approval_id) used
    # the fresh execution_id, not a stale one.
    assert calls[1]["execution_id"] == fresh_eid
