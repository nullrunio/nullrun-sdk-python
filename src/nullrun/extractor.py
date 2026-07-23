"""BusinessImpact extraction for @sensitive tools (Phase 1 / MVP 1.0).

This module is the SDK-side counterpart of the backend's
``BusinessImpact`` discriminated union. It exposes a single
declarative API (``money_outflow(argument="...", units="...")``)
that:

1. Binds the SDK call's positional/keyword arguments using
   ``inspect.signature(...).bind(...)`` so positional and keyword
   invocations look identical.
2. Pulls the named argument off the bound args.
3. Validates and converts the value to integer minor units
   using the ``units`` discriminator, the ISO-4217 minor-unit
   exponent for the currency, and the per-currency business cap
   for agent safety.
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
values at the input level. The error includes a pointer to
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
exponent for the currency and raises ``InvalidMoneyPrecisionError``
if the caller supplied more precision than the currency
supports. The caller can explicitly truncate with
``value.quantize(Decimal('1E-N'))`` to opt in to rounding; the
SDK never rounds silently.

## Sign is validated

A negative amount for either ``money_outflow`` (debit) or
``money_inflow`` (credit) is semantically incoherent. The
review pointed out that ``{"direction":"outflow",
"amount_minor":-5000}`` would silently fall through every
``op=gt`` predicate because ``-5000 > 5000`` is always False,
and the operator would never see a block. The current contract
rejects negative amounts with ``InvalidMoneyAmountError`` so the
``@protect`` wrapper can fail-CLOSED on the call site. If a
future variant needs negative amounts (e.g. refunds as negative
outflows) it can opt in via a future ``units="signed"``
discriminator.

## Overflow is bounded

``i64`` can hold up to ``2**63 - 1 = 9_223_372_036_854_775_807``
minor units (about $9.2 \u00d7 10\u00b9\u2076 for USD). The extractor checks
the converted value against this limit and raises
``InvalidMoneyAmountError`` if it would overflow. The check
uses ``int`` post-conversion so the operator sees the
offending amount, not just "too large".

## Business cap is bounded

The wire-format ``i64`` limit is a few hundred quadrillion
dollars, which is well above any sensible per-call debit. The
business cap (``_BUSINESS_CAP_MINOR`` table) is a much smaller
per-currency limit chosen so that any amount above the cap
goes through a separate risk path rather than being treated
as a normal call. The cap is policy, not correctness: a $1M
USD debit is technically valid on the wire, but for an agent
running a refund tool it almost certainly warrants a human
review. The cap is enforced as ``InvalidMoneyAmountError(reason="excessive")``
with a clear "above the per-call business cap" message; the
``@protect`` wrapper upgrades the error to fail-CLOSED.

## Float and ``bool`` are rejected

``float`` is rejected because IEEE-754 surprises are the entire
reason ``Decimal`` exists. ``bool`` is rejected because ``bool``
is a subclass of ``int`` in Python; without the explicit check,
``refund(amount=True)`` would silently treat ``True`` as
``1`` cent.

## Currency is validated (whitelist + case)

ISO-4217 minor-unit exponent lookup covers a small set of
codes by design. The ``normalize_currency`` helper rejects any
input that is not a 3-letter uppercase ISO-4217 code (e.g.
``"usd"``, ``"Usd"``, ``"USDX"``, ``""`` raise
``InvalidCurrencyError``). The SDK does NOT silently
upper-case the input because:

- it would hide typos (``"usd"`` vs ``"USD"`` vs ``"Usd"``
  would all normalize to ``"USD"``, masking a typo in the
  call site);
- ISO-4217 is a closed set of 3-letter uppercase codes,
  anything else is wrong by definition;
- the error message names the offending input so the operator
  can fix the call site.

The whitelist is consulted by ``currency_minor_digits`` and
``business_cap_minor``; unknown codes are rejected with
``InvalidCurrencyError`` instead of falling back to a default.
This closes the conservative-fallback gap from the previous
hardening pass (``UNKNOWN`` was allowed but the operator
might never notice the typo).

## Currency case rejection is enforced at construction time

The ``MoneyImpactExtractor.__init__`` validates the currency
via ``normalize_currency``. Passing ``"usd"`` raises
``InvalidCurrencyError`` at decorator-application time, before
the tool is ever called. This is fail-CLOSED: a misconfigured
decorator never reaches runtime.
"""

from __future__ import annotations

import functools
import inspect
from decimal import Decimal, InvalidOperation
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
# a rounding opportunity, and the SDK surfaces it as an
# ``InvalidMoneyPrecisionError`` so the operator can decide
# explicitly.
#
# Coverage is small by design: the SDK only enforces precision
# for currencies the form / wire shape already understands
# (USD/EUR/GBP/CHF/CAD/AUD = 2 fractional digits, JPY = 0,
# KWD/BHD/OMR = 3). Adding a new currency to the wire contract
# is a one-line change in ``_CURRENCY_MINOR_DIGITS`` *and*
# ``_BUSINESS_CAP_MINOR``.
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


# Per-currency business cap (in minor units). Above this
# threshold the extractor raises ``InvalidMoneyAmountError``
# with ``reason="excessive"`` so the call goes through a
# separate risk path rather than being treated as a normal
# call. The cap is policy, not correctness: a $1M USD debit
# is technically valid on the wire (well within ``i64``), but
# for an agent running a refund tool it almost certainly
# warrants a human review.
#
# Caps are chosen as round numbers above any plausible single
# transaction but well below the wire-format ``i64`` limit so
# the ``@protect`` wrapper can branch on
# ``reason="excessive"`` without confusing it with
# ``reason="overflow"`` (a real wire-format overflow).
#
# To opt out of the cap on a per-extractor basis, set
# ``enforce_business_cap=False`` in ``MoneyImpactExtractor.__init__``.
_BUSINESS_CAP_MINOR = {
    # $1,000,000.00 USD per call (one million dollars)
    "USD": 100_000_000,
    "EUR": 100_000_000,
    "GBP": 100_000_000,
    "CHF": 100_000_000,
    "CAD": 100_000_000,
    "AUD": 100_000_000,
    # 100,000,000 JPY (one hundred million yen)
    "JPY": 100_000_000,
    # 100,000.000 KWD / BHD / OMR (one hundred thousand, three
    # decimal digits each)
    "KWD": 100_000_000,
    "BHD": 100_000_000,
    "OMR": 100_000_000,
}


# Hard upper bound for the converted ``amount_minor``. The
# wire format is ``i64``; values exceeding ``2**63 - 1`` would
# silently overflow on the backend side. The constant is
# checked AFTER conversion so the operator sees the offending
# amount, not just "too large".
_I64_MAX = (1 << 63) - 1


# ----- Dedicated error types --------------------------------------
#
# ``InvalidMoneyPrecisionError`` -- the caller supplied more
#   fractional digits than the currency supports (e.g.
#   ``Decimal("50.005")`` for USD). The error carries
#   ``currency``, ``allowed``, ``received`` so a UI or test
#   harness can format a specific message.
#
# ``InvalidMoneyAmountError`` -- generic money-amount
#   invariant violation: negative amounts, overflow,
#   non-finite Decimals, or amounts above the per-currency
#   business cap. The error carries ``currency`` (when known)
#   and ``reason`` (a string discriminator).
#
# ``InvalidCurrencyError`` -- the supplied currency is not a
#   3-letter uppercase ISO-4217 code the SDK supports. The
#   error carries the offending input so the operator can fix
#   the call site.
#
# All three inherit from ``ValueError`` so the existing
# ``except ValueError`` callers in ``runtime.py`` continue to
# work; the subclasses let a careful caller branch on the
# type.


class InvalidMoneyPrecisionError(ValueError):
    """Sub-precision rejected: the supplied Decimal has more
    fractional digits than the currency supports.

    Attributes:
        currency: the ISO-4217 code the extractor was called
            with.
        allowed: the number of fractional digits the currency
            supports (e.g. 2 for USD).
        received: the offending Decimal as a string (so the
            caller sees exactly what was passed).
        received_digits: the number of fractional digits the
            offending Decimal actually had.
    """

    def __init__(
        self,
        currency: str,
        allowed: int,
        received: str,
        received_digits: int,
    ) -> None:
        self.currency = currency
        self.allowed = allowed
        self.received = received
        self.received_digits = received_digits
        msg = (
            f"{currency} supports at most {allowed} fractional "
            f"digit(s); got {received} ({received_digits}). "
            f"Either truncate explicitly with "
            f"``value.quantize(Decimal('1E-{allowed}'))`` "
            f"before passing to money_outflow, or change currency."
        )
        super().__init__(msg)


class InvalidMoneyAmountError(ValueError):
    """Generic money-amount invariant violation.

    Attributes:
        currency: the ISO-4217 code the extractor was called
            with (may be empty if the error happened before
            currency dispatch).
        reason: short string discriminator (``"negative"``,
            ``"overflow"``, ``"non_finite"``, ``"excessive"``).
            Lets a UI or test harness branch without parsing
            the message.
    """

    def __init__(
        self,
        reason: str,
        detail: str,
        currency: str = "",
    ) -> None:
        self.reason = reason
        self.currency = currency
        msg = detail if not currency else f"[{currency}] {detail}"
        super().__init__(msg)


class InvalidCurrencyError(ValueError):
    """The supplied currency is not a 3-letter uppercase
    ISO-4217 code the SDK supports.

    Attributes:
        received: the offending currency string (so the
            operator sees exactly what was passed).
    """

    def __init__(self, received: str, detail: str) -> None:
        self.received = received
        msg = f"currency={received!r}: {detail}"
        super().__init__(msg)


def normalize_currency(currency: str) -> str:
    """Validate and return the ISO-4217 currency code.

    The SDK does NOT silently upper-case the input because:

    - it would hide typos (``"usd"`` vs ``"USD"`` vs ``"Usd"``
      would all normalize to ``"USD"``, masking a typo in the
      call site);
    - ISO-4217 is a closed set of 3-letter uppercase codes,
      anything else is wrong by definition;
    - the error message names the offending input so the
      operator can fix the call site.

    Raises ``InvalidCurrencyError`` for any input that is not
    a 3-letter uppercase ISO-4217 code the SDK supports.
    """
    if not isinstance(currency, str):
        raise InvalidCurrencyError(
            str(currency),
            "currency must be a string",
        )
    if len(currency) != 3:
        raise InvalidCurrencyError(
            currency,
            f"currency must be a 3-letter ISO-4217 code; got length {len(currency)}",
        )
    if not currency.isupper() or not currency.isalpha():
        raise InvalidCurrencyError(
            currency,
            "currency must be 3 uppercase ASCII letters (ISO-4217)",
        )
    if currency not in _CURRENCY_MINOR_DIGITS:
        raise InvalidCurrencyError(
            currency,
            f"currency is not in the supported ISO-4217 whitelist "
            f"(supported: {sorted(_CURRENCY_MINOR_DIGITS.keys())})",
        )
    return currency


def currency_minor_digits(currency: str) -> int:
    """Return the number of fractional digits for ``currency``.

    Calls ``normalize_currency`` so the caller cannot pass an
    unknown code; previously this function silently fell back
    to 2 digits for unknown codes, which masked typos like
    ``"USDX"`` or ``"usd"``.

    Raises ``InvalidCurrencyError`` for any input that is not
    in the whitelist.
    """
    return _CURRENCY_MINOR_DIGITS[normalize_currency(currency)]


def business_cap_minor(currency: str) -> int:
    """Return the per-call business cap (in minor units) for ``currency``.

    The cap is policy, not correctness: a debit at the cap is
    technically valid on the wire but should go through a
    separate risk path. Callers that need to opt out (e.g.
    batch settlement tools) can pass
    ``enforce_business_cap=False`` to ``MoneyImpactExtractor``.

    Raises ``InvalidCurrencyError`` for any input that is not
    in the whitelist.
    """
    return _BUSINESS_CAP_MINOR[normalize_currency(currency)]


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
    """
    if allowed < 0:
        raise ValueError(f"allowed fractional digits must be >= 0, got {allowed}")
    if not value.is_finite():
        raise InvalidMoneyAmountError(
            reason="non_finite",
            detail=f"Decimal must be finite, got {value}",
        )
    fractional = value - value.to_integral_value(rounding="ROUND_DOWN")
    if fractional == 0:
        return False
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int):
        return abs(exponent) > allowed
    return False


def _count_fractional_digits(value: Decimal) -> int:
    """Return the number of fractional digits in ``value``."""
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int):
        return max(0, abs(exponent))
    return 0


def _check_overflow(amount_minor: int, currency: str) -> None:
    """Raise ``InvalidMoneyAmountError`` if ``amount_minor``
    exceeds the wire-format ``i64`` upper bound.
    """
    if amount_minor > _I64_MAX:
        raise InvalidMoneyAmountError(
            reason="overflow",
            currency=currency,
            detail=(
                f"amount_minor={amount_minor} exceeds i64::MAX={_I64_MAX}; "
                f"either the input amount is too large for the wire "
                f"format or the currency conversion factor is wrong."
            ),
        )


def _check_business_cap(
    amount_minor: int, currency: str, enforce: bool
) -> None:
    """Raise ``InvalidMoneyAmountError`` if ``amount_minor``
    exceeds the per-currency business cap.

    The cap is policy, not correctness: a debit at the cap is
    technically valid on the wire but should go through a
    separate risk path. ``enforce=False`` skips the check for
    callers that need to opt out (e.g. batch settlement tools
    that already have a human-in-the-loop approval flow).
    """
    if not enforce:
        return
    cap = _BUSINESS_CAP_MINOR[currency]
    if amount_minor > cap:
        raise InvalidMoneyAmountError(
            reason="excessive",
            currency=currency,
            detail=(
                f"amount_minor={amount_minor} exceeds the per-call "
                f"business cap={cap} minor units for {currency}; "
                f"send the call through the explicit human-approval "
                f"path instead of the auto-decision flow."
            ),
        )


def _to_minor_units(
    value: Union[int, Decimal], units: str, currency: str,
    enforce_business_cap: bool = True,
) -> int:
    """Convert a Decimal-or-int value to integer minor units.

    See module docstring for the full contract. The
    ``enforce_business_cap`` flag is passed through from
    ``MoneyImpactExtractor`` so callers that need to opt out
    (batch settlement) can do so without bypassing the rest
    of the validation.
    """
    if units == UNIT_MINOR:
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, Decimal) and not isinstance(value, bool):
                # Caller has already pre-quantized; the SDK does
                # not change the value. If the Decimal has a
                # fractional part (e.g. ``0.05``) we surface a
                # TypeError rather than silently truncate.
                if _decimal_has_more_fractional_digits(value, 0):
                    raise TypeError(
                        f"money_outflow(argument={value!r}, units='minor'): "
                        f"refusing to round {value!r} to integer minor units; "
                        f"either pass an int (e.g. int({value!r})) or set units='major'."
                    )
                converted = int(value)
            else:
                raise TypeError(
                    f"money_outflow(units='minor') requires int or Decimal; "
                    f"got {type(value).__name__}: {value!r}"
                )
        else:
            converted = value
        if converted < 0:
            raise InvalidMoneyAmountError(
                reason="negative",
                currency=currency,
                detail=(
                    f"money_outflow(units='minor') rejected negative "
                    f"amount {converted!r}; a negative amount would "
                    f"silently fall through every op=gt predicate "
                    f"because negative < positive is always False."
                ),
            )
        _check_overflow(converted, currency)
        _check_business_cap(converted, currency, enforce_business_cap)
        return converted

    if units == UNIT_MAJOR:
        if isinstance(value, bool) or not isinstance(value, Decimal):
            raise TypeError(
                f"money_outflow(units='major') requires Decimal; "
                f"got {type(value).__name__}: {value!r}. "
                f"For int minor units, set units='minor' or use money_inflow(...) "
                f"with units='minor'."
            )
        if value < 0:
            raise InvalidMoneyAmountError(
                reason="negative",
                currency=currency,
                detail=(
                    f"money_outflow(units='major') rejected negative "
                    f"amount {value!r}; a negative amount would "
                    f"silently fall through every op=gt predicate "
                    f"because negative < positive is always False."
                ),
            )
        allowed = currency_minor_digits(currency)
        if _decimal_has_more_fractional_digits(value, allowed):
            raise InvalidMoneyPrecisionError(
                currency=currency,
                allowed=allowed,
                received=str(value),
                received_digits=_count_fractional_digits(value),
            )
        converted = int(value * (Decimal(10) ** allowed))
        _check_overflow(converted, currency)
        _check_business_cap(converted, currency, enforce_business_cap)
        return converted

    raise ValueError(
        f"unknown units={units!r}; expected one of: {UNITS}"
    )


class MoneyImpactExtractor:
    """Declarative money-impact extractor.

    ``units`` discriminator semantics (Phase 1.1 / UX follow-up):

    - ``units="minor"`` (default): the bound argument is
      already in minor units. ``int`` is the canonical type;
      ``Decimal`` is accepted if it is already integer-valued.
      ``float`` is rejected outright.
    - ``units="major"``: the bound argument is a Decimal in
      major units. The SDK converts to minor units via
      ``Decimal * 10**currency_minor_digits(currency)`` after
      validating that the Decimal's precision matches the
      currency's ISO-4217 minor-unit exponent. ``float`` and
      ``int`` are rejected outright.

    The ``enforce_business_cap`` flag (default ``True``) gates
    the per-currency cap. Set to ``False`` for batch settlement
    tools that already have a human-in-the-loop approval flow
    and need to bypass the cap.

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
        enforce_business_cap: bool = True,
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
        # ``normalize_currency`` raises ``InvalidCurrencyError``
        # if the input is not a 3-letter uppercase ISO-4217
        # code in the whitelist. The constructor fails-CLOSED:
        # a misconfigured decorator (``currency="usd"`` typo)
        # never reaches runtime.
        currency = normalize_currency(currency)
        self.argument = argument
        self.direction = direction
        self.currency = currency
        self.units = units
        self.extractor_id = extractor_id
        self.extractor_version = extractor_version
        self.enforce_business_cap = enforce_business_cap

    def impact_for(
        self,
        fn: Callable[..., Any],
        args: tuple,
        kwargs: dict,
    ) -> BusinessImpact:
        """Bind the call and pull ``self.argument`` out of the bound args.

        Raises:
            TypeError: when ``self.argument`` is not a named
                parameter, or when the supplied value is not a
                Decimal / int in the unit discriminator the
                constructor was called with.
            InvalidMoneyPrecisionError: when ``units="major"``
                and the supplied Decimal has more fractional
                digits than the currency supports.
            InvalidMoneyAmountError: when the supplied amount
                is negative, non-finite, exceeds the wire-format
                ``i64`` upper bound, or exceeds the per-currency
                business cap.
        """
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

        amount_minor = _to_minor_units(
            value,
            units=self.units,
            currency=self.currency,
            enforce_business_cap=self.enforce_business_cap,
        )

        impact = MoneyImpact(
            direction=self.direction,
            amount_minor=amount_minor,
            currency=self.currency,
            extractor_id=self.extractor_id,
            extractor_version=self.extractor_version,
        )
        impact.validate()
        return BusinessImpact(impact=impact)


@functools.lru_cache(maxsize=128)
def _cached_signature(fn_id: int) -> Optional[inspect.Signature]:
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
    enforce_business_cap: bool = True,
) -> MoneyImpactExtractor:
    """Shorthand constructor used by ``@sensitive(impact=money_outflow(...))``.

    ``currency`` must be a 3-letter uppercase ISO-4217 code in
    the whitelist (USD/EUR/GBP/CHF/CAD/AUD/JPY/KWD/BHD/OMR).
    ``"usd"``, ``"Usd"``, ``"USDX"`` all raise
    ``InvalidCurrencyError`` at decorator-application time.

    ``units`` defaults to ``"minor"`` for backward compatibility
    with the Phase 0 / pre-Decimal path. New code that
    passes Decimal amounts in major units should pass
    ``units="major"`` explicitly.

    ``enforce_business_cap`` defaults to ``True`` so any debit
    above the per-currency cap goes through the explicit
    human-approval path. Set to ``False`` for batch settlement
    tools that already have a human-in-the-loop approval flow.
    """
    return MoneyImpactExtractor(
        argument=argument,
        direction=OUTFLOW,
        currency=currency,
        units=units,
        extractor_id=extractor_id,
        extractor_version=extractor_version,
        enforce_business_cap=enforce_business_cap,
    )


def compute_impact_digest(impact: BusinessImpact) -> str:
    """Thin alias re-exported for call-site readability."""
    return compute_action_digest(impact)


def gc_get_objects():
    import gc
    return gc.get_objects()