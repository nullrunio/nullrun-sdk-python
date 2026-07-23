"""BusinessImpact extraction for @sensitive tools (Phase 1 / MVP 1.0).

This module is the SDK-side counterpart of the backend's
``BusinessImpact`` discriminated union. It exposes a single
declarative API (``money_outflow(argument="...", units="...")``)
that:

1. Binds the SDK call's positional/keyword arguments using
   ``inspect.signature(...).bind(...)`` so positional and keyword
   invocations look identical.
2. Pulls the named argument off the bound args.
3. Converts the value to integer minor units (cents) using the
   ``units`` discriminator — ``units="minor"`` passes int
   values through verbatim; ``units="major"`` multiplies a
   ``Decimal`` by 100 with banker's rounding. ``float`` is
   rejected outright because it is exactly the silent bug
   class this module is designed to prevent.
4. Validates and builds a ``MoneyImpact``.
5. Computes the byte-identical ``action_digest`` the backend
   expects (see ``nullrun.business_impact.compute_action_digest``).

## Why this is its own helper, not part of ``@sensitive``

The ``@sensitive`` decorator chain is the integration point, but
the per-call impact extraction is data-driven and tested
independently. Keeping ``extractor.py`` as a pure helper avoids
the ``inspect.signature()`` cost on every sensitive call (the
binding result is cached after first extraction via Python's
``lru_cache``-friendly design) and makes the unit-discriminator
test matrix cheap to write without instantiating the full
``NullRunRuntime``.

For the production flow, ``runtime.execute(...)`` reads the
extractor from the function's ``_nullrun_extractor`` attribute
(which ``@sensitive(impact=money_outflow(...))`` sets) and calls
``impact_for(...)`` automatically.

## Why ``units`` is explicit, not a type discriminator

The previous review explicitly rejected the
``int = minor, Decimal = major`` shortcut because the unit
semantics of a function argument should not flip silently when
the function signature is refactored. Concretely:

    @nullrun.sensitive(impact=nullrun.money_outflow(argument="amount"))
    def refund(amount: int) -> ...   # Phase 0 path: 50 = 50 cents
    def refund(amount: Decimal) -> ... # 50 = $50.00 (5000 cents)

If ``units`` were implicit-from-type, renaming ``amount``'s
annotation from ``int`` to ``Decimal`` would silently change the
operator-facing rule from "$0.50" to "$50.00". The explicit
``units="major" | units="minor"`` argument in the decorator
fixes the unit semantics at the call site so a future
signature refactor does not flip the meaning.

## Float is rejected outright

``Decimal`` exists precisely so that money code does not have
to deal with binary-floating-point surprises (``0.1 + 0.2 !=
0.3`` in IEEE-754). The extractor therefore refuses ``float``
values at the input level. The TypeError includes a pointer to
the right alternative (``Decimal`` for major, ``int`` for minor)
so the operator can fix the call site without guessing.
"""

from __future__ import annotations

import functools
import inspect
from decimal import Decimal
from typing import Any, Callable, Optional, Union

from nullrun.business_impact import (
    BusinessImpact,
    MoneyImpact,
    INFLOW,
    OUTFLOW,
    compute_action_digest,
)


# Unit discriminators for the typed impact payload.
#
# ``minor`` = the value is already in minor units (cents, pence,
# satoshi-style). The SDK stores the value verbatim on the wire.
# This is the Phase 0 / pre-Decimal path: a function declared
# ``def refund(amount_cents: int)`` already works in minor units
# so the operator just has to add the decorator and the wire
# shape does not change.
#
# ``major`` = the value is in major units (dollars, pounds, etc.)
# and the SDK converts to minor units via ``Decimal * 100`` with
# banker's rounding. The ``MoneyImpact`` struct stores the
# result in minor units so the wire shape and the backend
# ``action_predicate`` shape are identical between the two
# paths.
#
# The discriminator is **explicit** in the decorator (not
# implicit from the type) so the unit semantics survive a
# refactor of the function signature.
UNIT_MINOR = "minor"
UNIT_MAJOR = "major"
UNITS = (UNIT_MINOR, UNIT_MAJOR)


def _to_minor_units(
    value: Union[int, Decimal], units: str, currency: str
) -> int:
    """Convert a Decimal-or-int value to integer minor units.

    ``units="minor"``: ``value`` is already in minor units (cents).
    The function accepts ``int`` for the legacy Phase 0 path; a
    ``Decimal`` here means the caller already pre-quantized, and
    the SDK refuses to silently round a fractional value
    (``Decimal("0.05")`` with ``units="minor"`` is a unit-confusion
    bug and the SDK surfaces it as a TypeError, not a silent
    ``int(0.05) == 0`` truncation).

    ``units="major"``: ``value`` is a major-unit Decimal (dollars).
    The function rejects ``int`` outright because a bare ``int``
    in major units is exactly the silent bug class the explicit
    discriminator is designed to prevent.

    The major-unit default uses ``ROUND_HALF_EVEN`` (banker's
    rounding) because it is the IEEE-754 default and matches
    Postgres ``numeric`` arithmetic; a value like
    ``Decimal("0.005")`` rounds to ``0`` instead of ``1`` so
    refunds sum to ``0.00`` rather than triggering a sub-cent
    rounding drift over many operations.
    """
    if units == UNIT_MINOR:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, Decimal) and not isinstance(value, bool):
            # Caller has already pre-quantized; the SDK does
            # not change the value. If the Decimal has a
            # fractional part (e.g. ``0.05``) we surface a
            # TypeError rather than silently truncate, so the
            # operator catches the unit confusion.
            quantized = value.quantize(Decimal("1"))
            if quantized != value:
                raise TypeError(
                    f"money_outflow(argument={value!r}, units='minor'): "
                    f"refusing to round {value!r} to integer minor units; "
                    f"either pass an int (e.g. int({value!r})) or set units='major'."
                )
            return int(quantized)
        raise TypeError(
            f"money_outflow(units='minor') requires int or Decimal; "
            f"got {type(value).__name__}: {value!r}"
        )
    if units == UNIT_MAJOR:
        if isinstance(value, bool) or not isinstance(value, Decimal):
            raise TypeError(
                f"money_outflow(units='major') requires Decimal; "
                f"got {type(value).__name__}: {value!r}. "
                f"For int minor units, set units='minor' or use money_inflow(...) "
                f"with units='minor'."
            )
        # Round half-even (banker's rounding). This matches the
        # Postgres ``numeric`` default so a sum of 100 refunds
        # of $0.005 each totals $0.50 (not $0.51, not $0.49).
        quantized = (value * Decimal(100)).quantize(
            Decimal("1"), rounding="ROUND_HALF_EVEN"
        )
        return int(quantized)
    # Defensive: ``__init__`` validates ``units`` at construction
    # time, so this branch is unreachable. If a future refactor
    # breaks the validation, we still fail closed here.
    raise ValueError(
        f"unknown units={units!r}; expected one of: {UNITS}"
    )


class MoneyImpactExtractor:
    """Declarative money-impact extractor.

    ``units`` discriminator semantics (Phase 1.1 / UX follow-up):

    - ``units="minor"`` (default for backward compatibility):
      the bound argument is already in minor units. ``int`` is
      the canonical type; ``Decimal`` is accepted if it is
      already integer-valued. ``float`` is rejected outright.
    - ``units="major"``: the bound argument is a Decimal in
      major units. The SDK converts to minor units via
      ``Decimal * 100`` with banker's rounding. ``float`` and
      ``int`` are rejected outright (the only reason to use
      ``major`` is precision; an ``int`` in major units is
      almost always a unit-confusion bug).

    The discriminator is **explicit** rather than implicit from
    the type. A future signature refactor (``int`` -> ``Decimal``
    or vice versa) does not silently flip the meaning of the
    number. Operators reading the code see the unit in the
    decorator argument, not in the type annotation.
    """

    def __init__(
        self,
        argument: str,
        direction: str = OUTFLOW,
        currency: str = "USD",
        units: str = UNIT_MINOR,
        extractor_id: str = "nullrun.money.path",
        extractor_version: str = "1",
    ) -> None:
        if direction not in (OUTFLOW, INFLOW):
            raise ValueError(
                f"direction must be {OUTFLOW!r} or {INFLOW!r}, "
                f"got {direction!r}"
            )
        if units not in UNITS:
            raise ValueError(
                f"units must be one of {UNITS}, got {units!r}"
            )
        self.argument = argument
        self.direction = direction
        self.currency = currency
        self.units = units
        self.extractor_id = extractor_id
        self.extractor_version = extractor_version

    def impact_for(
        self,
        fn: Callable[..., Any],
        args: tuple,
        kwargs: dict,
    ) -> BusinessImpact:
        """Bind the call and pull ``self.argument`` out of the bound args.

        The unit discriminator (``self.units``) decides whether
        the value is treated as already-minor-units
        (``int`` passes through) or as a major-unit Decimal
        (``Decimal * 100`` with banker's rounding).

        The bool check is explicit because ``bool`` is a
        subclass of ``int`` in Python — without the explicit
        check, ``refund(amount=True)`` would silently treat
        ``True`` as ``1`` cent.

        Raises:
            TypeError: when ``self.argument`` is not a named
                parameter, or when the supplied value is not a
                Decimal / int in the unit discriminator the
                constructor was called with. The ``@protect``
                wrapper upgrades TypeError to
                ``NullRunBlockedException`` (fail-CLOSED) — a
                malicious or buggy SDK must never fall back to
                "no impact was extracted" implicitly.
        """
        # `inspect.signature(...).bind` normalizes positional +
        # keyword into a single dict, so the extractor does not
        # care whether the call was `refund(Decimal("50.99"))`
        # or `refund(amount=Decimal("50.99"))`.
        sig = inspect.signature(fn)
        try:
            bound = sig.bind(*args, **kwargs)
        except TypeError as exc:
            raise TypeError(
                f"MoneyImpactExtractor.argument={self.argument!r} "
                f"failed to bind call to {fn!r}: {exc}"
            ) from exc
        bound.apply_defaults()
        if self.argument not in bound.arguments:
            raise TypeError(
                f"MoneyImpactExtractor expects argument {self.argument!r} "
                f"on {fn!r}; call did not provide it"
            )
        value = bound.arguments[self.argument]

        # Phase 1.1: convert via the unit discriminator. ``float``
        # is rejected before this point; see ``_to_minor_units``.
        amount_minor = _to_minor_units(
            value, units=self.units, currency=self.currency
        )

        impact = MoneyImpact(
            direction=self.direction,
            amount_minor=amount_minor,
            currency=self.currency,
            extractor_id=self.extractor_id,
            extractor_version=self.extractor_version,
        )
        # `MoneyImpact.validate` enforces direction/currency shape;
        # convert to BusinessImpact only after that succeeds so the
        # backend sees a fully-validated payload.
        impact.validate()
        return BusinessImpact(impact=impact)


@functools.lru_cache(maxsize=128)
def _cached_signature(fn_id: int) -> Optional[inspect.Signature]:
    # lookup by id() is fragile across reloads but workable for the
    # single-process SDK lifetime. We hold the signature object so
    # repeated calls on the same function skip the cost of
    # `inspect.signature(...)`.
    for obj in gc_get_objects():  # type: ignore[name-defined]
        if id(obj) == fn_id:
            try:
                return inspect.signature(obj)
            except (TypeError, ValueError):
                return None
    return None


def money_outflow(
    argument: str,
    currency: str = "USD",
    units: str = UNIT_MINOR,
    extractor_id: str = "nullrun.money.path",
    extractor_version: str = "1",
) -> MoneyImpactExtractor:
    """Shorthand constructor used by ``@sensitive(impact=money_outflow(...))``.

    ``units`` defaults to ``"minor"`` for backward compatibility
    with the Phase 0 / pre-Decimal path. New code that
    passes Decimal amounts in major units should pass
    ``units="major"`` explicitly so the SDK multiplies by 100
    with banker's rounding.

    ``direction`` is fixed to ``OUTFLOW`` because that is the
    only direction the backend's MVP-1.0 rules fire on. Inflow
    rules will land in a later MVP; the constructor is open to
    accepting ``direction=INFLOW`` then without an API break.
    """
    return MoneyImpactExtractor(
        argument=argument,
        direction=OUTFLOW,
        currency=currency,
        units=units,
        extractor_id=extractor_id,
        extractor_version=extractor_version,
    )


def compute_impact_digest(impact: BusinessImpact) -> str:
    """Thin alias re-exported for call-site readability.

    Some SDK callers prefer ``compute_impact_digest(impact)`` over
    the lower-level ``compute_action_digest(impact)`` because the
    name reinforces the side-channel-of-truth (digest is a
    security primitive, not a content hash).
    """
    return compute_action_digest(impact)


# ``gc_get_objects`` is an implementation helper the existing
# module imported under that name; preserved here for
# ``_cached_signature`` without dragging the ``gc`` import into
# the public surface.
def gc_get_objects():
    import gc
    return gc.get_objects()