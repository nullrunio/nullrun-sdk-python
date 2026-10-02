"""DEF-TC6-006 (QA RUN_ID 20261002T0826, 2026-10-02, SDK 0.20.0):
``_route_track`` laundered an ADR-005 enforcement rejection into a
transport warning and reported success to the agent.

## What was observed

TC-15 ``consume_overbudget`` against production. The probe reserved
via ``@protect`` (``/gate``) then consumed 1M tokens, far past the
reservation. The backend did exactly the right thing::

    _route_track: track_single failed for execution_id=01a0fb42-…
      (track: /track: actual cost exceeds the reservation + epsilon
      (ADR-005 fixed-cents invariant). Re-issue /api/v1/gate with a
      larger token estimate to expand the reservation, or reduce the
      work-unit cost.) — event dropped

    TRACK_OK={'allowed': True, 'actions': [], 'local_cost_cents': 0}

A 422 ``CONSUME_OVERBUDGET`` on the wire, and ``track_llm`` returned
``allowed: True``.

## Root cause

``NullRunRuntime._route_track`` wraps ``self._transport.track_single``
in a bare ``except Exception`` that logs at WARNING and returns
(``runtime.py:4290-4330``). The transport layer has already done the
right classification by then — ``_error_to_exception`` maps a
``CONSUME_OVERBUDGET`` body to a typed
``NullRunConsumeOverbudgetError`` carrying ``reserved_cents``,
``actual_cost_cents``, ``max_allowed_cents`` and ``epsilon_cents``
(``transport.py:2939-2949``) — and ``_route_track`` throws that
typing away.

Two things make the bare catch wrong rather than merely blunt:

1. **The policy it applies is scoped to a different path.** The
   ADR-008 table's only ``/track`` row reads ``/track batch path
   (legacy) | OPEN-on-network-error (event dropped, no retry)``
   (``runtime.py:25``). The swallow lives in the v3 *single*-event
   path, which has no such row.
2. **It contradicts the table's own enforcement rule.** The same
   docstring says the SDK "does NOT silently fail-OPEN on a wire
   4xx/5xx that names an enforcement failure" and names ``/track``
   in the list of handlers whose rejection the SDK "raises the
   corresponding exception" for. A 422 whose body is
   ``CONSUME_OVERBUDGET`` is the paradigm case.

The exception's own docstring closes the loop: "the reservation is
NOT silently re-reserved — the caller MUST reconcile the delta
manually before retrying" (``exceptions.py:481-486``). A caller
that never sees the exception cannot reconcile anything, and
``decorators.py:966-970`` explicitly preserves
``NullRunConsumeOverbudgetError`` as first-class "for cookbook
recovery" — recovery that was unreachable from ``track_llm``.

## Why it matters beyond the log line

A swallowed rejection is not a lost log line. The consume is
*refused*, so the reservation is never released and the real cost is
never billed; the agent is told ``allowed: True`` and continues. The
SDK's own mitigation for that half of the problem (invalidating the
chain's cached ``allow`` on 402/422) still runs — it is above the
re-raise point — so the blast radius is bounded to the current
chain. But the *reporting* is wrong in the way CLAUDE.md invariant
#4 and ADR-013 exist to prevent: an enforcement outcome is
re-presented to the caller as a successful transport.

## The fix

Re-raise ``NullRunDecision`` after the existing invalidation and
telemetry. Everything else — network errors, 5xx, protocol errors —
keeps the drop-and-log behavior the table documents.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from httpx import Response

BASE_URL = "https://api.test.nullrun.io"
SERVER_MINTED_V1 = "0190c5b5-7c9a-7def-8a1b-0123456789ab"

TRACK_URL = f"{BASE_URL}/api/v1/track"

CONSUMED_TOO_MUCH = {
    "error_code": "CONSUME_OVERBUDGET",
    "error_message": "actual > reserved + epsilon",
    "details": {
        "execution_id": SERVER_MINTED_V1,
        "reserved_cents": 100,
        "max_allowed_cents": 101,
        "actual_cost_cents": 150,
        "epsilon_cents": 1,
    },
}


def _capture_smid() -> None:
    """Bind a server-minted id so ``_route_track`` reaches
    ``track_single`` instead of dropping on "no reservation"."""
    from nullrun.runtime import _capture_server_minted_execution_id

    _capture_server_minted_execution_id({"reservation_id": SERVER_MINTED_V1})


class TestEnforcementRejectionPropagates:
    """An ADR-005 business rejection must reach the caller."""

    @respx.mock
    def test_consume_overbudget_raises_typed_error(self, make_runtime):
        from nullrun.breaker.exceptions import NullRunConsumeOverbudgetError

        rt = make_runtime()
        respx.post(TRACK_URL).mock(
            return_value=Response(422, json=CONSUMED_TOO_MUCH)
        )
        _capture_smid()

        with pytest.raises(NullRunConsumeOverbudgetError):
            rt.track_llm(input_tokens=60, output_tokens=40, model="claude-sonnet-4-6")
        rt._transport.flush_now()

    @respx.mock
    def test_raised_error_keeps_reconciliation_attributes(self, make_runtime):
        """The point of the typed error is that the caller can
        reconcile. Re-raising a bare ``RuntimeError`` would satisfy
        the first test and defeat the fix, so the attribute payload
        is asserted directly.
        """
        from nullrun.breaker.exceptions import NullRunConsumeOverbudgetError

        rt = make_runtime()
        respx.post(TRACK_URL).mock(
            return_value=Response(422, json=CONSUMED_TOO_MUCH)
        )
        _capture_smid()

        with pytest.raises(NullRunConsumeOverbudgetError) as ei:
            rt.track_llm(input_tokens=60, output_tokens=40, model="claude-sonnet-4-6")
        rt._transport.flush_now()

        err = ei.value
        assert err.reserved_cents == 100
        assert err.actual_cost_cents == 150
        assert err.epsilon_cents == 1
        assert err.status_code == 422

    @respx.mock
    def test_budget_block_402_also_propagates(self, make_runtime):
        """402 is the same laundering with a different class:
        ``NullRunBudgetError`` is a ``NullRunBlockedException``,
        which is a ``NullRunDecision``. A fix that only special-cased
        ``CONSUME_OVERBUDGET`` by name would miss it.
        """
        from nullrun.breaker.exceptions import NullRunBudgetError

        rt = make_runtime()
        respx.post(TRACK_URL).mock(
            return_value=Response(
                402,
                json={
                    "error_code": "BUDGET_HARD_BLOCKED",
                    "error_message": "budget exhausted",
                },
            )
        )
        _capture_smid()

        with pytest.raises(NullRunBudgetError):
            rt.track_llm(input_tokens=60, output_tokens=40, model="claude-sonnet-4-6")
        rt._transport.flush_now()


class TestTransportFailureStillDrops:
    """The other half of the contract. A network failure is what the
    ``/track batch path (legacy)`` row describes, and the table has no
    row authorising a raise for the v3 single path on transport
    errors — so it must keep dropping. Widening the fix to
    ``Exception`` would freeze the agent loop on a dead backend,
    which is the exact failure the fail-OPEN rows exist to prevent.
    """

    @respx.mock
    def test_connection_error_does_not_raise(self, make_runtime):
        rt = make_runtime()
        respx.post(TRACK_URL).mock(side_effect=httpx.ConnectError("boom"))
        _capture_smid()

        rt.track_llm(input_tokens=60, output_tokens=40, model="claude-sonnet-4-6")
        rt._transport.flush_now()

    @respx.mock
    def test_server_5xx_does_not_raise(self, make_runtime):
        """A 5xx names no enforcement failure, so it stays in the
        transport class. The ADR-008 text scopes the "raises the
        corresponding exception" rule to responses that *name* an
        enforcement failure.
        """
        rt = make_runtime()
        respx.post(TRACK_URL).mock(
            return_value=Response(500, json={"error_message": "internal"})
        )
        _capture_smid()

        rt.track_llm(input_tokens=60, output_tokens=40, model="claude-sonnet-4-6")
        rt._transport.flush_now()
