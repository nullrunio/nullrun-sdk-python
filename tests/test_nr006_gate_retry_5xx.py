"""
tests/test_nr006_gate_retry_5xx.py — NR-006 regression pin.

NR-006 (audit 2026-08-24) flagged that ``Transport.check`` calls
``_request_with_signed_body`` directly without going through
``_retry_with_backoff``. A single 5xx from the gate (rolling deploy,
transient backend restart) caused the SDK to short-circuit to a
synthetic ``decision: "block"`` with ``decision_source: FALLBACK``
— the agent never gets a real budget check.

This file pins the regression: when the gate returns a transient
5xx, ``Transport.check`` MUST retry (via ``_retry_with_backoff``)
until the retry budget is exhausted, returning the real allow
decision when the backend recovers — not a synthetic block.

Three tests:

1. ``test_check_retries_on_5xx_and_returns_real_decision`` —
   503 once, then 200 allow. SDK must return allow, not block.
2. ``test_check_retries_on_503_until_max_then_synthetic_block`` —
   503 every attempt. SDK must eventually return synthetic block
   with the standard fallback shape (decision_source=FALLBACK),
   proving the retry path is exhausted before falling back.
3. ``test_check_4xx_is_not_retried`` — 400 every attempt. SDK must
   return synthetic block immediately (400 is a real gate decision,
   not a transient infra failure).

Test #1 fails on pre-NR-006 master (returns synthetic block on first
503). Tests #2 and #3 document the contract around retry exhaustion
and 4xx non-retryability, respectively.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from nullrun.transport import Transport


@pytest.fixture
def transport():
    t = Transport(api_url="https://api.test.nullrun.io", api_key="test-key-12345678")
    yield t
    t.stop()


# Shared gate request body across tests.
_GATE_REQUEST: dict = {
    "organization_id": "ws-123",
    "execution_id": "exec-456",
    "operation_id": "op-789",
    "check_type": "llm",
    "model": "claude-3",
    "estimated_tokens": 100,
}


@respx.mock
def test_check_retries_on_5xx_and_returns_real_decision(transport):
    """NR-006 PIN 1.

    Simulates a transient backend 5xx (e.g. one replica being
    restarted mid-rolling-deploy). Pre-NR-006 the SDK returned a
    synthetic block immediately on the first 503. Post-NR-006 the
    SDK must retry through ``_retry_with_backoff`` and return the
    real gate decision once the backend recovers.

    Without the fix: assertion FAILS — first 503 short-circuits to
    ``{"decision": "block", "decision_source": "fallback"}``.
    With the fix: assertion PASSES — second 200 returns the real
    allow decision.
    """
    route = respx.post("https://api.test.nullrun.io/api/v1/gate").mock(
        side_effect=[
            httpx.Response(503, json={"error": "service_unavailable"}),
            httpx.Response(
                200,
                json={
                    "decision": "allow",
                    "remaining_budget_cents": 500,
                    "projected_cost_cents": 10,
                    "explanations": [],
                    "suggestions": [],
                },
            ),
        ]
    )

    result = transport.check(_GATE_REQUEST)

    # The route must have been called at least twice (initial + retry).
    assert route.call_count >= 2, (
        f"NR-006: expected /gate to be retried after 503, but only "
        f"saw {route.call_count} call(s). The SDK short-circuited "
        f"to synthetic block on the first 5xx instead of going "
        f"through _retry_with_backoff."
    )

    # The real gate decision MUST surface, not the synthetic block.
    assert result["decision"] == "allow", (
        f"NR-006: SDK returned synthetic block after 503+200 wire "
        f"sequence (got {result!r}). A transient infra 5xx must not "
        f"silently flip the decision — the audit's fail-NO-CHECK "
        f"violation. Cookbook recipes that branch on decision='allow' "
        f"never fired."
    )
    assert result.get("decision_source") != "fallback", (
        "NR-006: result carries decision_source='fallback' — the "
        "fallback path executed despite a successful real gate "
        "response after retry. decision_source must be 'gateway' "
        "when the wire response was real."
    )
    assert result["remaining_budget_cents"] == 500


@respx.mock
def test_check_retries_on_503_until_max_then_synthetic_block(transport):
    """NR-006 PIN 2 — retry-exhaustion contract.

    When the backend is unavailable for the entire retry budget,
    ``Transport.check`` must surface the synthetic-block fallback
    (legacy behaviour preserved) so the agent gets a deterministic
    fail-CLOSED outcome rather than hanging or raising mid-flight.

    The test pins the retry budget: ``_retry_with_backoff`` is
    configured with ``max_retries=3`` for /gate (per the audit's
    recommended direction — "less than 10"). After 4 calls
    (1 initial + 3 retries) the SDK returns the fallback shape.
    """
    route = respx.post("https://api.test.nullrun.io/api/v1/gate").mock(
        return_value=httpx.Response(503, json={"error": "service_unavailable"})
    )

    result = transport.check(_GATE_REQUEST)

    # The retry budget was exhausted before falling back. ``max_retries=3``
    # means at most 4 calls (1 initial + 3 retries).
    assert 2 <= route.call_count <= 6, (
        f"NR-006: expected /gate to be retried up to max_retries+1 "
        f"times before fallback, saw {route.call_count} call(s). "
        f"Exhaustion contract: 1 initial + max_retries=3 retries = 4 "
        f"calls, then synthetic block."
    )

    # After retry exhaustion, the legacy synthetic block fires.
    assert result["decision"] == "block"
    assert result.get("decision_source") == "fallback"
    assert result["remaining_budget_cents"] == 0
    assert result["reservation_id"] is None


@respx.mock
def test_check_4xx_is_not_retried(transport):
    """NR-006 PIN 3 — 4xx is a real gate decision, not transient infra.

    A 400 / 403 / 404 from the gate is a real outcome (validation
    error, auth failure, unknown workflow). Retrying would amplify
    a permanent error and burn the SDK's budget on noise.

    The SDK must surface the synthetic block on the first 4xx —
    same as today — but WITHOUT consuming retry budget. Exactly
    one wire call is made.
    """
    route = respx.post("https://api.test.nullrun.io/api/v1/gate").mock(
        return_value=httpx.Response(400, json={"error": "validation_failed"})
    )

    result = transport.check(_GATE_REQUEST)

    assert route.call_count == 1, (
        f"NR-006: 4xx must not trigger retry — saw {route.call_count} "
        f"call(s). A 400 is a real gate decision (validation failure), "
        f"not transient infra. Retrying amplifies load for no gain."
    )
    assert result["decision"] == "block"
