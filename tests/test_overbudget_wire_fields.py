"""The 422 CONSUME_OVERBUDGET fields the SDK was reading, and the ones it was not.

The backend's overage body is `handlers.rs`:

    "reserved_millicents":     reserved,        // from the Lua decision
    "max_allowed_millicents":  max_allowed,
    "actual_cost_cents":       req.cost_cents,
    "soft_pass":               soft_pass,
    "reservation_recorded":    reservation_recorded,

The SDK's dispatcher read `reserved_cents`, `max_allowed_cents` and
`epsilon_cents` — none of which the backend has ever sent. Every one of
them was permanently `None`, and `runtime.py`'s fail-closed table told
callers to reconcile the delta from precisely those three. A caller
following the documentation computed `None - None + None`.

Two things are pinned here. First, that the numbers the wire actually
carries reach the exception. Second, that epsilon is NOT reconstructed:
the backend does not publish its configured tolerance on this body, and
a conversion that appears to supply it (`// 10` on the millicents, then
"epsilon = actual - max_allowed") would be arithmetic dressed as
evidence. The honest recovery is `actual_millicents - reserved`, and the
caller does it.
"""

from __future__ import annotations

import httpx

from nullrun.breaker.exceptions import NullRunConsumeOverbudgetError
from nullrun.transport import _parse_v3_error_envelope_uncategorised

# The real body shape, copied from handlers.rs (`"reserved_millicents"`
# at the ConsumeOverbudget 422). `reserved` is 2500 millicents = 250c,
# `max_allowed` is 2500 + 10 (a 1c epsilon), `actual` is 2600c.
WIRE_422 = {
    "error_code": "CONSUME_OVERBUDGET",
    "error_message": "consume exceeds reservation by more than epsilon",
    "details": {
        "endpoint": "/track",
        "execution_id": "0199-exec-over",
        "reserved_millicents": 2500,
        "max_allowed_millicents": 2510,
        "actual_cost_cents": 2600,
        "soft_pass": False,
        "reservation_recorded": True,
    },
}


def _parse() -> NullRunConsumeOverbudgetError:
    err = _parse_v3_error_envelope_uncategorised(httpx.Response(422, json=WIRE_422), "/track")
    assert isinstance(err, NullRunConsumeOverbudgetError)
    return err


def test_the_reservation_reaches_the_caller_in_the_unit_the_wire_uses():
    err = _parse()
    assert err.reserved_millicents == 2500
    assert err.max_allowed_millicents == 2510


def test_the_actual_spend_reaches_the_caller_in_cents():
    err = _parse()
    assert err.actual_cost_cents == 2600


def test_soft_pass_is_surfaced():
    err = _parse()
    assert err.soft_pass is False


def test_the_old_cents_fields_are_absent_rather_than_silently_wrong():
    """They stay None because nothing populates them — not because a guess failed.

    Asserting `reserved_cents is None` is a statement about the wire: the
    backend has no such key, so any value here would be the SDK's
    invention. A caller that reads them must see the absence.
    """
    err = _parse()
    assert err.reserved_cents is None
    assert err.max_allowed_cents is None
    assert err.epsilon_cents is None


def test_the_documented_reconciliation_is_computable_from_what_arrives():
    """The numbers the fail-closed table promises are enough on their own.

    The reservation is in MILLICENTS and the actual in CENTS, so the
    subtraction needs the x10 — a caller who skips it reads an overshoot
    three orders of magnitude too large. `actual - max_allowed` is the
    overshoot past the ceiling; `max_allowed - reserved` is the epsilon
    the backend actually applied to this call. Both are recoverable, and
    neither needs a field the backend does not send.
    """
    err = _parse()
    overshoot_milli = err.actual_cost_cents * 10 - err.max_allowed_millicents
    epsilon_applied_milli = err.max_allowed_millicents - err.reserved_millicents
    # 2600c spent against a 251c ceiling is 2349c over — 23490 millicents.
    assert overshoot_milli == 23_490
    assert epsilon_applied_milli == 10  # 1 cent, the platform default


def test_a_malformed_number_does_not_become_a_typed_one():
    """A string where a number belongs stays absent rather than being coerced."""
    body = {
        "error_code": "CONSUME_OVERBUDGET",
        "error_message": "over",
        "details": {"reserved_millicents": "2500", "reservation_recorded": "true"},
    }
    err = _parse_v3_error_envelope_uncategorised(httpx.Response(422, json=body), "/track")
    assert isinstance(err, NullRunConsumeOverbudgetError)
    assert err.reserved_millicents is None
    assert err.recorded is None
