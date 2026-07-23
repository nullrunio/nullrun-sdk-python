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
   values through verbatim; ``units="major"`` validates the
   precision of a ``Decimal`` against the ISO-4217 minor-unit
   exponent for the currency, then multiplies by
   ``10**currency_digits``. ``float`` is rejected outright.
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

## Major-unit precision is validated, never rounded

The first version of this module used banker's rounding
(``ROUND_HALF_EVEN``) to convert ``Decimal("50.99")`` to
``5099`` minor units. That decision was rejected in review:
banker's rounding silently drops sub-cent precision
(``Decimal("50.005")`` becomes ``5000`` minor units), which
is the exact bug class the explicit ``units`` discriminator
is designed to prevent. The current contract validates the
precision of the ``Decimal`` against the ISO-4217 minor-unit
exponent for the currency and raises ``ValueError`` if the
caller supplied more precision than the currency supports.
The caller can explicitly truncate with
``value.quantize(Decimal('1E-N'))`` to opt in to rounding; the
SDK never rounds silently.
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
# and the SDK converts to minor units via ``Decimal * 10**N``
# where ``N = currency_minor_digits(currency)``. The
# ``MoneyImpact`` struct stores the result in minor units so
# the wire shape and the backend ``action_predicate`` shape
# are identical between the two paths.
#
# The discriminator is **explicit** in the decorator (not
# implicit from the type) so the unit semantics survive a
# refactor of the function signature.
UNIT_MINOR = "minor"
UNIT_MAJOR = "major"
UNITS = (UNIT_MINOR, UNIT_MAJOR)


# ISO-4217 minor-unit exponents for the currencies the SDK
# supports out of the box. The lookup is consulted by
# ``_to_minor_units`` to validate the precision of a
# ``Decimal`` in ``units="major"`` mode; a value with more
# fractional digits than the currency supports is a bug, not
# a rounding opportunity, and the SDK surfaces it as a
# ``ValueError`` so the operator can decide explicitly.
#
# Coverage is small by design: the SDK only enforces precision
# for currencies the form / wire shape already understands
# (USD/EUR/GBP/CHF/CAD/AUD = 2 fractional digits, JPY = 0,
# KWD/BHD/OMR = 3). An unknown currency falls back to 2 (the
# historical default) so a future addition does not silently
# round.
_CURRENCY_MINOR_DIGITS = {
    # 2 fractional digits (cents, pence, centimes)
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "CHF": 2,
    "CAD": 2,
    "AUD": 2,
    # 0 fractional digits (yen)
    "JPY": 0,
    # 3 fractional digits (fils)
    "KWD": 3,
    "BHD": 3,
    "OMR": 3,
}

DEFAULT_MINOR_DIGITS = 2


def currency_minor_digits(currency: str) -> int:
    """Return the number of fractional digits for ``currency``.

    ISO-4217 minor-unit exponent: ``2`` for USD/EUR/GBP, ``0``
    for JPY, ``3`` for KWD/BHD/OMR. Unknown codes fall back
    to ``DEFAULT_MINOR_DIGITS = 2`` so a future addition does
    not silently round.

    The fallback is conservative: an unknown currency is
    validated as if it had 2 fractional digits, which means a
    future ``Decimal("1.234")`` call for an unknown code
    would raise ``ValueError``. The operator adds the new
    code to ``_CURRENCY_MINOR_DIGITS`` to opt in.
    """
    return _CURRENCY_MINOR_DIGITS.get(currency, DEFAULT_MINOR_DIGITS)


def _decimal_has_more_fractional_digits(
    value: Decimal, allowed: int
) -> bool:
    """Return True iff ``value`` has more fractional digits than
    ``allowed``.

    The check uses ``value % 1`` so that a value like
    ``Decimal("50.00")`` (which ``as_tuple()`` reports as having
    two fractional digits) is correctly classified as an
    integer-valued decimal with zero effective fractional
    digits. ``Decimal("50.005")`` has a non-zero fractional
    part and is rejected.

    This is the precision contract that replaced banker's
    rounding: the caller must supply a value whose fractional
    part fits within the currency's ISO-4217 minor-unit
    exponent. ``Decimal("50.00")`` for USD is fine because
    the fractional part is zero; ``Decimal("50.005")`` for
    USD is not fine because the fractional part exceeds the
    2-digit limit.

    The check is purely structural (``% 1`` and integer
    comparison) and does NOT do any rounding, so the SDK
    never silently drops precision.
    """
    if allowed < 0:
        raise ValueError(f"allowed fractional digits must be >= 0, got {allowed}")
    if not value.is_finite():
        # ``Infinity`` / ``NaN`` are not money values; the
        # downstream ``_to_minor_units`` would raise on
        # ``int(...)`` anyway. We surface a cleaner error
        # here so the caller sees ``ValueError`` instead of
        # ``InvalidOperation``.
        raise ValueError(f"Decimal must be finite, got {value}")
    fractional = value - value.to_integral_value(rounding="ROUND_DOWN")
    if fractional == 0:
        # Integer-valued decimals (``50``, ``50.00``,
        # ``50.0000``) all reduce to ``Decimal("0")`` for the
        # fractional part and pass any allowed limit.
        return False
    # Non-zero fractional part: count the digits after the
    # decimal point. ``Decimal("0.005")`` has ``as_tuple()``
    # exponent ``-3`` and we report 3 fractional digits.
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int):
        return abs(exponent) > allowed
    return False


def _count_fractional_digits(value: Decimal) -> int:
    """Return the number of fractional digits in ``value``.

    Used by the ``ValueError`` message in
    ``_to_minor_units`` so the operator sees the offending
    precision, not just a generic "too many digits" error.
    """
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int):
        return max(0, abs(exponent))
    return 0


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
    discriminator is designed to prevent. The SDK validates the
    precision of the Decimal against the ISO-4217 minor-unit
    exponent for ``currency``: ``Decimal("50.005")`` for USD
    (``minor_digits = 2``) raises ``ValueError("USD supports at
    most 2 fractional digits; got 50.005")`` rather than silently
    rounding. This is the production-grade contract: precision
    must be supplied correctly by the caller; the SDK never
    rounds silently.
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
            if _decimal_has_more_fractional_digits(value, 0):
                raise TypeError(
                    f"money_outflow(argument={value!r}, units='minor'): "
                    f"refusing to round {value!r} to integer minor units; "
                    f"either pass an int (e.g. int({value!r})) or set units='major'."
                )
            return int(value)
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
        # Precision validation: refuse a Decimal with more
        # fractional digits than the currency supports. This
        # is the production-grade contract: precision must be
        # supplied correctly by the caller; the SDK never
        # rounds silently. Banker's rounding was rejected in
        # review because ``Decimal("50.005")`` for USD would
        # silently drop to ``5000`` and surprise the operator;
        # a ``ValueError`` is unambiguous and matches the
        # payment-system convention.
        allowed = currency_minor_digits(currency)
        if _decimal_has_more_fractional_digits(value, allowed):
            raise ValueError(
                f"{currency} supports at most {allowed} fractional "
                f"digit(s); got {value} ({_count_fractional_digits(value)}). "
                f"Either truncate explicitly with "
                f"``value.quantize(Decimal('1E-{allowed}'))`` "
                f"before passing to money_outflow, or change currency."
            )
        # Conversion is exact because precision was validated
        # above. No rounding. No truncation. No silent loss.
        return int(value * (Decimal(10) ** allowed))
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
      ``Decimal * 10**currency_minor_digits(currency)`` after
      validating that the Decimal's precision matches the
      currency's ISO-4217 minor-unit exponent. ``float`` and
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
        (``Decimal * 10**currency_minor_digits`` after precision
        validation).

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
            ValueError: when ``units="major"`` and the
                supplied Decimal has more fractional digits than
                the currency supports (e.g.
                ``Decimal("50.005")`` for USD).
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
    ``units="major"`` explicitly so the SDK multiplies by
    ``10**currency_minor_digits(currency)`` after validating
    precision.

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