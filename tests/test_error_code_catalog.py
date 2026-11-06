"""The wire-code catalog must name a class the dispatcher can build.

Audit 2026-11-03, **SDK-15**: the SDK's v3 error-code catalog lagged
the backend's registry by thirty-four codes. The parity guard in
``backend/tests/nr007_sdk_error_code_parity.rs`` could not see the
drift because it was wired into no workflow.

A catalog entry is a PROMISE: "when the backend sends this code, the
caller gets this class". Two ways the promise breaks:

1. **The code is missing.** The dispatcher falls back to the HTTP
   status, so a money refusal (402) arrives as a generic backend error
   and a refusal (403) arrives as an authentication error. The caller
   cannot branch on what happened.
2. **The code is present but the dispatcher cannot build its class.**
   The typed classes have heterogeneous constructors, so a class whose
   constructor the dispatcher does not know raises ``TypeError`` inside
   the parser. The ``TypeError`` escapes into the catch-all in
   ``Transport.execute``, and the caller receives a synthetic
   ``{"decision": "block"}`` dict instead of the typed refusal. The
   refusal is not lost — it is flattened into "the gateway said no",
   which is exactly the information the caller needed.

The second failure mode is what the SDK-15 dispatch fix closed: the
blocked-family arm tested ``catalog is NullRunBlockedException`` by
IDENTITY, so every typed SUBCLASS of the blocked family (the approval
family, the MCP family) fell through to the generic fallback and
raised ``TypeError``.

The property under test, over the whole catalog:

    for every code in the catalog, the real dispatcher returns an
    instance of the catalog's class — for every status, because the
    code names the refusal and the status is only its HTTP rendering.

That last clause is the anti-vacuity canary. A check that only
asserted "the catalog is non-empty" would pass against a dispatcher
that returned the same class for everything. A check that only
asserted "an exception came back" would pass against the synthetic
block. This one asserts the exact class.
"""

from __future__ import annotations

import httpx
import pytest

from nullrun.breaker.exceptions import (
    NullRunApprovalDbUnavailableError,
    NullRunMcpApprovalRequiredError,
)
from nullrun.transport import _V3_ERROR_CODE_MAP, _parse_v3_error_envelope

# Every status the backend's own registry uses
# (``GateErrorCode::http_status``). The class a wire code maps to must
# NOT depend on the status: a code names the refusal, the status is
# the HTTP rendering of it. If a status ever changed the class, the
# caller would have to know the status to know the class — which is
# the status-based fallback the catalog exists to replace.
STATUSES = (402, 403, 404, 409, 413, 422, 429, 500, 503)

# The thirty-four codes SDK-15 added. Pinned by name so a later
# removal of any one of them is a RED test, not a silent regression.
# The cross-repo guard
# (``backend/tests/nr007_sdk_error_code_parity.rs``) would catch it
# too, but that guard is not wired into any workflow yet — a guard
# that does not run is not a guard.
SDK_15_CODES = (
    "APPROVAL_TOOL_ARGUMENT_MISMATCH",
    "APPROVAL_TOOL_ARGUMENT_UNAVAILABLE",
    "AUTH_BACKEND_UNAVAILABLE",
    "BATCH_TOO_LARGE",
    "BUDGET_EXCEEDED",
    "BUDGET_PROJECTION_UNAVAILABLE",
    "CHAIN_ENDED",
    "CHAIN_NOT_FOUND",
    "CONTROL_CHARS_IN_FIELD",
    "EXECUTION_BINDING_MISSING",
    "EXECUTION_BINDING_READ_FAILED",
    "EXECUTION_BINDING_WRITE_FAILED",
    "EXECUTION_NOT_BOUND",
    "EXECUTION_RESOLVE_REDIS_ERROR",
    "IDEMPOTENCY_CONFLICT",
    "IDEMPOTENCY_IN_FLIGHT",
    "INGESTION_BACKPRESSURE",
    "INGESTION_CLOSED",
    "INSUFFICIENT_SCOPE",
    "MCP_DEFINITION_DRIFTED",
    "MCP_REGISTRY_UNAVAILABLE",
    "MCP_TOOL_APPROVAL_REQUIRED",
    "MCP_TOOL_BLOCKED",
    "MCP_TOOL_DECISION_REQUIRED",
    "MCP_TOOL_REMOVED",
    "ORG_CONTEXT_UNRESOLVED",
    "RATE_LIMITER_UNAVAILABLE",
    "RATE_LIMIT_PLAN_TIER",
    "RESERVATION_NOT_FOUND",
    "RESERVED_SCANNED_PARTIAL",
    "RESERVE_DISABLED",
    "TOOL_BLOCKED_BACKSTOP_UNAVAILABLE",
    "WAITLIST_RATE_LIMITED",
    "WORKFLOW_BUDGET_CONFIG_INVALID",
)


def _response(code: str, status: int) -> httpx.Response:
    """A refusal in the shape the gate actually sends.

    The v3 envelope is ``{"error_code", "error_message", "details"}``
    and the gate's ``details`` block carries its own ``error_code``
    (see the refusal construction in ``gate/orchestrator.rs``). The
    test reproduces that, because a ``details`` block WITHOUT an
    ``error_code`` would skip the forwarding path and make the test
    pass for the wrong reason.
    """
    return httpx.Response(
        status,
        json={
            "error_code": code,
            "error_message": "boom",
            "details": {"error_code": code},
        },
    )


def test_the_thirty_four_sdk_15_codes_are_catalogued():
    """Every code SDK-15 added is still in the catalog."""
    missing = [code for code in SDK_15_CODES if code not in _V3_ERROR_CODE_MAP]
    assert not missing, f"SDK-15 codes dropped from the catalog: {missing}"


@pytest.mark.parametrize("code", sorted(_V3_ERROR_CODE_MAP))
def test_every_catalog_code_dispatches_to_its_mapped_class(code: str):
    """The catalog's promise holds: code in, that class out.

    A ``TypeError`` from a mismatched constructor fails this test as an
    ERROR, which is the point: the TypeError is the defect the
    dispatch fix closed, and it used to be swallowed further down by
    the transport's catch-all.
    """
    expected = _V3_ERROR_CODE_MAP[code]
    for status in STATUSES:
        exc = _parse_v3_error_envelope(_response(code, status), "/api/v1/gate")
        assert isinstance(exc, expected), (
            f"{code} (status {status}) dispatched to "
            f"{type(exc).__name__}, catalog says {expected.__name__}"
        )


@pytest.mark.parametrize("code", sorted(_V3_ERROR_CODE_MAP))
def test_every_catalog_code_keeps_its_wire_code_on_the_exception(code: str):
    """The wire code survives on the exception.

    ``exc.wire_error_code`` is the granular string the backend sent.
    It is the field a cookbook branches on when it needs the exact
    refusal rather than the class.
    """
    exc = _parse_v3_error_envelope(_response(code, 403), "/api/v1/gate")
    assert getattr(exc, "wire_error_code", None) == code


def test_a_typed_subclass_of_the_blocked_family_is_not_flattened():
    """The specific defect SDK-15's dispatch fix closed.

    ``APPROVAL_DB_UNAVAILABLE`` maps to
    ``NullRunApprovalDbUnavailableError``, a SUBCLASS of
    ``NullRunBlockedException``. The blocked-family arm used to test
    the class by identity, so the subclass fell through to the generic
    fallback, which called ``catalog(full_message, ...)`` against a
    constructor that requires ``workflow_id`` — a ``TypeError`` that
    the transport's catch-all turned into a synthetic block.

    The canary is the sibling class in the same family: if the arm
    matched nothing at all, both would be flattened and this test
    would still pass on the first assertion alone. The second
    assertion pins that the two classes are DISTINCT — so the first
    one cannot pass by accident.
    """
    exc = _parse_v3_error_envelope(_response("APPROVAL_DB_UNAVAILABLE", 503), "/api/v1/gate")
    assert isinstance(exc, NullRunApprovalDbUnavailableError)

    sibling = _parse_v3_error_envelope(_response("MCP_APPROVAL_REQUIRED", 403), "/api/v1/gate")
    assert isinstance(sibling, NullRunMcpApprovalRequiredError)
    assert not isinstance(sibling, NullRunApprovalDbUnavailableError)


def test_the_plan_tier_rate_limit_carries_the_retry_and_the_upgrade_url():
    """A 429 that is NOT ``RATE_LIMIT_EXCEEDED`` still builds a
    ``RateLimitError``.

    ``RateLimitError`` has no message-only constructor: it requires
    ``source`` and ``endpoint``. A catalog entry for a 429 code that
    the dispatcher routes to the generic fallback therefore raises
    ``TypeError`` instead of building the class — the same
    "the catalog promises a class the dispatcher cannot build" defect,
    one family over.
    """
    response = httpx.Response(
        429,
        json={
            "error_code": "RATE_LIMIT_PLAN_TIER",
            "error_message": "plan aggregate exceeded",
            "details": {"error_code": "RATE_LIMIT_PLAN_TIER"},
            "retry_after_ms": 2000,
            "upgrade_url": "https://app.nullrun.io/settings/billing",
        },
    )
    exc = _parse_v3_error_envelope(response, "/api/v1/gate")
    assert isinstance(exc, _V3_ERROR_CODE_MAP["RATE_LIMIT_PLAN_TIER"])
    assert exc.retry_after == 2.0
    assert exc.upgrade_url == "https://app.nullrun.io/settings/billing"
