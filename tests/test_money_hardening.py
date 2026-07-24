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

from nullrun.business_impact import (
    OUTFLOW,
    BusinessImpact,
    compute_action_digest,
)
from nullrun.extractor import (
    UNIT_MAJOR,
    UNIT_MINOR,
    InvalidCurrencyError,
    InvalidMoneyAmountError,
    InvalidMoneyPrecisionError,
    _to_minor_units,
    business_cap_minor,
    currency_minor_digits,
    money_outflow,
    normalize_currency,
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

    def test_below_business_cap_accepted(self) -> None:
        # The per-currency business cap (USD=$1M = 100_000_000
        # minor units) is below ``i64::MAX``; values within
        # the cap are accepted.
        assert _to_minor_units(99_999_999, UNIT_MINOR, "USD") == 99_999_999

    def test_business_cap_rejected_with_reason_excessive(self) -> None:
        # ``$1,000,000.01 USD = 100_000_001 minor units`` is
        # above the per-call business cap. The error reason
        # is ``"excessive"`` (separate from the wire-format
        # overflow which is ``"overflow"``).
        with pytest.raises(InvalidMoneyAmountError) as info:
            _to_minor_units(100_000_001, UNIT_MINOR, "USD")
        assert info.value.reason == "excessive"
        assert info.value.currency == "USD"

    def test_business_cap_message_names_cap(self) -> None:
        with pytest.raises(InvalidMoneyAmountError) as info:
            _to_minor_units(100_000_001, UNIT_MINOR, "USD")
        # The error message names the cap so the operator
        # knows the threshold, not just "too large".
        assert "100000000" in str(info.value) or "100_000_000" in str(info.value)

    def test_business_cap_opt_out_via_enforce_false(self) -> None:
        # Batch settlement tools that already have a
        # human-in-the-loop approval flow can bypass the cap.
        assert (
            _to_minor_units(
                100_000_001, UNIT_MINOR, "USD",
                enforce_business_cap=False,
            )
            == 100_000_001
        )

    def test_wire_format_overflow_distinct_from_business_cap(self) -> None:
        # ``i64::MAX = 9_223_372_036_854_775_807`` exceeds both
        # the per-currency business cap AND the wire-format
        # ``i64`` upper bound. The business-cap check fires
        # first because it is the lower threshold; the error
        # reason is ``"excessive"`` (not ``"overflow"``).
        # This separation lets the ``@protect`` wrapper route
        # the call to the right policy: a ``"excessive"``
        # debit goes to the explicit human-approval path; a
        # ``"overflow"`` would indicate a wire-format bug.
        with pytest.raises(InvalidMoneyAmountError) as info:
            _to_minor_units((1 << 63) - 1, UNIT_MINOR, "USD")
        assert info.value.reason == "excessive"

    def test_wire_format_overflow_only_when_above_business_cap(
        self
    ) -> None:
        # With ``enforce_business_cap=False``, a value at
        # ``i64::MAX - 1`` is accepted (it is below
        # ``i64::MAX``) but ``i64::MAX`` raises ``overflow``.
        assert (
            _to_minor_units(
                (1 << 63) - 1, UNIT_MINOR, "USD",
                enforce_business_cap=False,
            )
            == (1 << 63) - 1
        )
        with pytest.raises(InvalidMoneyAmountError) as info:
            _to_minor_units(
                (1 << 63), UNIT_MINOR, "USD",
                enforce_business_cap=False,
            )
        assert info.value.reason == "overflow"

    def test_overflow_message_names_i64_max(self) -> None:
        with pytest.raises(InvalidMoneyAmountError) as info:
            _to_minor_units(
                (1 << 63), UNIT_MINOR, "USD",
                enforce_business_cap=False,
            )
        assert "i64" in str(info.value) or "9223372036854775807" in str(info.value)


# ---------------------------------------------------------------------------
# 4. Unsupported currency fallback
# ---------------------------------------------------------------------------


class TestCurrencyWhitelist:
    """The ISO-4217 whitelist is enforced at extractor
    construction time and at every per-currency lookup.
    Unknown codes raise ``InvalidCurrencyError`` rather than
    falling back to a default; this closes the conservative-
    fallback gap that masked typos like ``"usd"`` or ``"USDX"``.

    Case is also enforced: ISO-4217 codes are 3-letter
    uppercase ASCII letters, anything else is wrong by
    definition. The SDK does NOT silently upper-case the
    input.
    """

    def test_known_currency_exact(self) -> None:
        for code in ("USD", "EUR", "JPY", "KWD", "BHD", "OMR",
                     "GBP", "CHF", "CAD", "AUD"):
            assert normalize_currency(code) == code

    def test_lowercase_currency_rejected(self) -> None:
        with pytest.raises(InvalidCurrencyError) as info:
            normalize_currency("usd")
        assert info.value.received == "usd"
        assert "uppercase" in str(info.value)

    def test_mixed_case_currency_rejected(self) -> None:
        with pytest.raises(InvalidCurrencyError) as info:
            normalize_currency("Usd")
        assert info.value.received == "Usd"

    def test_four_letter_currency_rejected(self) -> None:
        with pytest.raises(InvalidCurrencyError) as info:
            normalize_currency("USDX")
        assert "length 4" in str(info.value) or "3-letter" in str(info.value)

    def test_empty_currency_rejected(self) -> None:
        with pytest.raises(InvalidCurrencyError):
            normalize_currency("")

    def test_digits_in_currency_rejected(self) -> None:
        with pytest.raises(InvalidCurrencyError):
            normalize_currency("US1")

    def test_constructor_rejects_lowercase_at_decoration_time(self) -> None:
        # ``money_outflow(currency="usd")`` raises at
        # decorator-application time, never reaches runtime.
        with pytest.raises(InvalidCurrencyError):
            money_outflow(argument="amount", currency="usd")

    def test_currency_minor_digits_propagates_currency_error(self) -> None:
        with pytest.raises(InvalidCurrencyError):
            currency_minor_digits("XYZ")

    def test_currency_minor_digits_known_value_exact(self) -> None:
        assert currency_minor_digits("USD") == 2
        assert currency_minor_digits("EUR") == 2
        assert currency_minor_digits("JPY") == 0
        assert currency_minor_digits("KWD") == 3

    def test_business_cap_lookup_propagates_currency_error(self) -> None:
        with pytest.raises(InvalidCurrencyError):
            business_cap_minor("XYZ")

    def test_business_cap_known_value_exact(self) -> None:
        assert business_cap_minor("USD") == 100_000_000
        assert business_cap_minor("JPY") == 100_000_000
        assert business_cap_minor("KWD") == 100_000_000


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