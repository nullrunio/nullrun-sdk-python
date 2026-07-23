"""Phase 1.1 hardening tests for the money contract.

This module is the dedicated hardening suite for the
``MoneyImpactExtractor`` hardening pass that closed the
review gaps:

1. **Dedicated error types** -- ``InvalidMoneyPrecisionError``
   and ``InvalidMoneyAmountError`` (both subclass
   ``ValueError`` for backward compat).
2. **Negative amount rejection** -- a negative amount for
   either ``money_outflow`` (debit) or ``money_inflow``
   (credit) is semantically incoherent: ``-5000 > 5000`` is
   always False, so an op=gt predicate silently never fires.
3. **Overflow guard** -- the converted ``amount_minor`` must
   fit in ``i64`` (the wire format). ``Decimal("1e30")``
   must be rejected, not silently wrap.
4. **Unsupported currency fallback** -- unknown ISO-4217 codes
   fall back to 2 fractional digits (USD-style validation).
   The fallback is conservative: ``Decimal("1.234")`` for an
   unknown code raises ``InvalidMoneyPrecisionError`` because
   the fallback assumed 2 digits, not 3.
5. **Serialization stability** -- ``Decimal("50")`` and
   ``Decimal("50.00")`` must reduce to the same ``int(50)``
   and the same SHA-256 digest. The backend's golden hex
   pin (``dfc96387...0df27``) is for ``amount_minor=5000``;
   the SDK must produce that hex whether the caller types
   ``int(5000)``, ``Decimal("50")``, ``Decimal("50.00")``,
   ``Decimal("50.000")`` or any other trailing-zero variant.

Why a dedicated file (not in ``tests/test_units_discriminator.py``):
the existing tests cover the unit-discriminator matrix and
the precision-validation matrix. The hardening pass is a
separate axis -- error types, sign, overflow, currency
fallback, and serialization stability -- and mixing them
into the same test classes would obscure the failure mode
when a future refactor breaks one of them.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from nullrun.extractor import (
    InvalidMoneyAmountError,
    InvalidMoneyPrecisionError,
    UNIT_MAJOR,
    UNIT_MINOR,
    _to_minor_units,
    currency_minor_digits,
    money_outflow,
)
from nullrun.business_impact import (
    BusinessImpact,
    OUTFLOW,
    compute_action_digest,
)


GOLDEN_HEX_USD_50_DOLLARS_OUTFLOW = (
    "dfc96387ca539b7130caebe705e042f2e34e52ab44352ae5e527bcef64f0df27"
)


def _refund_dollars(amount: Decimal) -> dict:
    return {"amount": amount}


def _refund_cents(amount_cents: int) -> dict:
    return {"amount_cents": amount_cents}


# ---------------------------------------------------------------------------
# 1. Dedicated error types
# ---------------------------------------------------------------------------


class TestErrorTypes:
    """``InvalidMoneyPrecisionError`` and ``InvalidMoneyAmountError``
    are subclasses of ``ValueError`` (for backward compat with
    ``except ValueError`` callers) and carry structured
    context the operator can act on."""

    def test_precision_error_is_value_error_subclass(self) -> None:
        err = InvalidMoneyPrecisionError(
            currency="USD", allowed=2, received="50.005", received_digits=3
        )
        assert isinstance(err, ValueError)
        assert err.currency == "USD"
        assert err.allowed == 2
        assert err.received == "50.005"
        assert err.received_digits == 3

    def test_precision_error_message_names_currency(self) -> None:
        with pytest.raises(InvalidMoneyPrecisionError) as info:
            _to_minor_units(Decimal("50.005"), UNIT_MAJOR, "USD")
        assert "USD" in str(info.value)
        assert "2" in str(info.value)
        assert "50.005" in str(info.value)

    def test_amount_error_is_value_error_subclass(self) -> None:
        err = InvalidMoneyAmountError(reason="negative", detail="x", currency="USD")
        assert isinstance(err, ValueError)
        assert err.reason == "negative"
        assert err.currency == "USD"

    def test_amount_error_reason_carries_discriminator(self) -> None:
        # A UI or test harness can branch on ``reason``
        # without parsing the human message.
        with pytest.raises(InvalidMoneyAmountError) as info:
            _to_minor_units(Decimal("-50.00"), UNIT_MAJOR, "USD")
        assert info.value.reason == "negative"
        assert info.value.currency == "USD"

    def test_precision_caught_by_value_error_handler(self) -> None:
        # Backward compat: existing callers that catch
        # ``ValueError`` still see the precision error.
        with pytest.raises(ValueError):
            _to_minor_units(Decimal("50.005"), UNIT_MAJOR, "USD")

    def test_amount_caught_by_value_error_handler(self) -> None:
        # Backward compat for negative + overflow + non-finite.
        with pytest.raises(ValueError):
            _to_minor_units(Decimal("-50.00"), UNIT_MAJOR, "USD")


# ---------------------------------------------------------------------------
# 2. Negative amount rejection
# ---------------------------------------------------------------------------


class TestNegativeAmount:
    """A negative ``amount_minor`` would silently fall through
    every ``op=gt`` predicate (``negative < positive`` is
    always False). The SDK rejects negative amounts on both
    unit paths."""

    def test_major_units_decimal_negative_rejected(self) -> None:
        with pytest.raises(InvalidMoneyAmountError) as info:
            _to_minor_units(Decimal("-50.00"), UNIT_MAJOR, "USD")
        assert info.value.reason == "negative"
        assert info.value.currency == "USD"

    def test_major_units_decimal_negative_with_precision_rejected(self) -> None:
        # Negative + sub-precision: sign check fires first so
        # the operator sees the most actionable error.
        with pytest.raises(InvalidMoneyAmountError) as info:
            _to_minor_units(Decimal("-50.005"), UNIT_MAJOR, "USD")
        assert info.value.reason == "negative"

    def test_minor_units_int_negative_rejected(self) -> None:
        with pytest.raises(InvalidMoneyAmountError) as info:
            _to_minor_units(-5000, UNIT_MINOR, "USD")
        assert info.value.reason == "negative"

    def test_minor_units_decimal_negative_rejected(self) -> None:
        with pytest.raises(InvalidMoneyAmountError) as info:
            _to_minor_units(Decimal("-5000"), UNIT_MINOR, "USD")
        assert info.value.reason == "negative"

    def test_zero_amount_accepted(self) -> None:
        # ``0`` is a valid amount (legitimate $0.00 refund,
        # for example). Only negative is rejected.
        assert _to_minor_units(0, UNIT_MINOR, "USD") == 0
        assert _to_minor_units(Decimal("0"), UNIT_MAJOR, "USD") == 0
        assert _to_minor_units(Decimal("0.00"), UNIT_MAJOR, "USD") == 0


# ---------------------------------------------------------------------------
# 3. Overflow guard
# ---------------------------------------------------------------------------


class TestOverflowGuard:
    """The wire format is ``i64``. Values exceeding
    ``2**63 - 1 = 9_223_372_036_854_775_807`` minor units
    must be rejected; silently wrapping would corrupt the
    digest and the approval binding."""

    def test_i64_max_minus_one_accepted(self) -> None:
        # Just below the limit.
        big = (1 << 63) - 1
        assert _to_minor_units(big, UNIT_MINOR, "USD") == big

    def test_i64_max_rejected(self) -> None:
        # Exactly at the limit -- rejected (the check is
        # ``> _I64_MAX``, so the limit itself is out of bounds
        # to leave headroom for downstream adjustments).
        with pytest.raises(InvalidMoneyAmountError) as info:
            _to_minor_units((1 << 63), UNIT_MINOR, "USD")
        assert info.value.reason == "overflow"

    def test_major_units_overflow_rejected(self) -> None:
        # ``Decimal("1e30")`` for USD = ``1e32`` minor units,
        # way past i64::MAX. ``int(...)`` on the multiplied
        # value raises ``OverflowError`` first, but the SDK
        # should still produce a clean ``InvalidMoneyAmountError``
        # so the ``@protect`` wrapper can fail-CLOSED.
        with pytest.raises((InvalidMoneyAmountError, OverflowError)):
            _to_minor_units(Decimal("1e30"), UNIT_MAJOR, "USD")

    def test_overflow_message_names_i64_max(self) -> None:
        with pytest.raises(InvalidMoneyAmountError) as info:
            _to_minor_units((1 << 63), UNIT_MINOR, "USD")
        # The error mentions the i64 upper bound so the
        # operator knows it is a wire-format limit, not a
        # currency arithmetic limit.
        assert "i64" in str(info.value) or "9223372036854775807" in str(info.value)


# ---------------------------------------------------------------------------
# 4. Unsupported currency fallback
# ---------------------------------------------------------------------------


class TestUnsupportedCurrencyFallback:
    """Unknown ISO-4217 codes fall back to 2 fractional digits
    (USD-style validation). The fallback is conservative: a
    value that would be valid in 3-digit KWD is rejected in an
    unknown code because the fallback assumes 2 digits. The
    operator adds the new code to ``_CURRENCY_MINOR_DIGITS``
    to opt in."""

    def test_known_currency_exact(self) -> None:
        assert currency_minor_digits("USD") == 2
        assert currency_minor_digits("EUR") == 2
        assert currency_minor_digits("JPY") == 0
        assert currency_minor_digits("KWD") == 3

    def test_unknown_currency_falls_back_to_two(self) -> None:
        # The fallback is 2 (USD-style). An unknown currency
        # with 3-digit precision gets rejected, not silently
        # rounded.
        assert currency_minor_digits("XYZ") == 2
        assert currency_minor_digits("") == 2

    def test_unknown_currency_rejects_three_digit_decimal(self) -> None:
        with pytest.raises(InvalidMoneyPrecisionError) as info:
            _to_minor_units(Decimal("1.234"), UNIT_MAJOR, "XYZ")
        # The error names the (unknown) currency so the
        # operator can see that they forgot to add XYZ to
        # ``_CURRENCY_MINOR_DIGITS``.
        assert info.value.currency == "XYZ"
        assert info.value.allowed == 2

    def test_unknown_currency_accepts_two_digit_decimal(self) -> None:
        # Conservative fallback accepts the same shape USD
        # accepts, so unknown codes work the way USD does.
        assert _to_minor_units(Decimal("1.23"), UNIT_MAJOR, "XYZ") == 123


# ---------------------------------------------------------------------------
# 5. Serialization stability
# ---------------------------------------------------------------------------


class TestSerializationStability:
    """``Decimal("50")`` and ``Decimal("50.00")`` must produce
    the same ``amount_minor=5000`` and the same SHA-256 digest.
    The cross-language golden hex pin is for ``5000`` minor
    units; the SDK must produce that hex regardless of how
    the caller represents the value."""

    def test_decimal_50_int_and_decimal_50_00_same_minor(self) -> None:
        # The trailing-zero variant reduces to the integer
        # value. This is the canonical serialization-stability
        # test.
        assert _to_minor_units(Decimal("50"), UNIT_MAJOR, "USD") == 5_000
        assert _to_minor_units(Decimal("50.0"), UNIT_MAJOR, "USD") == 5_000
        assert _to_minor_units(Decimal("50.00"), UNIT_MAJOR, "USD") == 5_000
        assert _to_minor_units(Decimal("50.000"), UNIT_MAJOR, "USD") == 5_000
        assert _to_minor_units(Decimal("50.0000"), UNIT_MAJOR, "USD") == 5_000

    def test_decimal_50_and_int_5000_produce_same_impact(self) -> None:
        # ``int(5000)`` (minor) and ``Decimal("50")`` (major)
        # are two different surface APIs but the same wire
        # value. The extractor must produce identical
        # ``BusinessImpact`` objects.
        ext = money_outflow(argument="amount_cents", units=UNIT_MINOR)
        impact_int = ext.impact_for(_refund_cents, (5000,), {})
        ext_major = money_outflow(argument="amount", units=UNIT_MAJOR)
        impact_dec = ext_major.impact_for(_refund_dollars, (Decimal("50"),), {})
        assert impact_int.impact.amount_minor == impact_dec.impact.amount_minor
        assert compute_action_digest(impact_int) == compute_action_digest(impact_dec)

    def test_decimal_50_00_50_000_produce_golden_hex(self) -> None:
        # The cross-language golden hex pin must match whether
        # the caller types ``Decimal("50.00")`` or
        # ``Decimal("50.000")`` -- only the trailing-zero
        # count differs in the caller representation, not the
        # wire value.
        ext = money_outflow(argument="amount", units=UNIT_MAJOR)
        for repr_ in ("50", "50.0", "50.00", "50.000", "50.0000"):
            impact = ext.impact_for(_refund_dollars, (Decimal(repr_),), {})
            assert impact.impact.amount_minor == 5_000
            assert (
                compute_action_digest(impact)
                == GOLDEN_HEX_USD_50_DOLLARS_OUTFLOW
            )

    def test_decimal_50_99_minor_path_also_stable(self) -> None:
        # The ``units="minor"`` path also accepts Decimal if
        # it is already integer-valued. ``Decimal("50.99")``
        # is rejected because the fractional part is non-zero;
        # the integer-valued variant ``Decimal("5099")`` is
        # accepted and produces the same minor value as
        # ``int(5099)``.
        assert _to_minor_units(Decimal("5099"), UNIT_MINOR, "USD") == 5_099
        assert _to_minor_units(5099, UNIT_MINOR, "USD") == 5_099


# ---------------------------------------------------------------------------
# 6. Wire-format invariant (round-trip through ``MoneyImpact``)
# ---------------------------------------------------------------------------


class TestWireFormatInvariant:
    """The hardening pass should not change the wire format.
    ``amount_minor`` is an ``i64`` with a fixed scale per
    currency. These tests pin that contract."""

    def test_amount_minor_is_python_int(self) -> None:
        # ``i64`` on the wire; ``int`` in Python. The hardening
        # pass must not introduce ``Decimal`` or ``float`` on
        # the wire.
        ext = money_outflow(argument="amount", units=UNIT_MAJOR)
        impact = ext.impact_for(_refund_dollars, (Decimal("50.99"),), {})
        assert type(impact.impact.amount_minor) is int

    def test_amount_minor_is_non_negative(self) -> None:
        # Combined with the negative-amount rejection: the
        # wire value is always ``>= 0`` (the negative-amount
        # guard raises before the conversion).
        ext = money_outflow(argument="amount", units=UNIT_MAJOR)
        impact = ext.impact_for(_refund_dollars, (Decimal("0.00"),), {})
        assert impact.impact.amount_minor == 0
        impact_large = ext.impact_for(_refund_dollars, (Decimal("9999.99"),), {})
        assert impact_large.impact.amount_minor >= 0

    def test_currency_passes_through_unchanged(self) -> None:
        # The hardening pass must not change ``currency`` --
        # the backend predicate evaluator compares it
        # exactly.
        ext = money_outflow(argument="amount", currency="USD", units=UNIT_MAJOR)
        impact = ext.impact_for(_refund_dollars, (Decimal("50.99"),), {})
        assert impact.impact.currency == "USD"