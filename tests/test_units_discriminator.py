"""Phase 1.1 UX follow-up: explicit units discriminator + Decimal support.

These tests pin the behavior the previous review explicitly
called out: the unit semantics (major / minor) must be
**explicit** in the decorator, not implicit from the value
type. ``float`` is rejected outright because the entire point
of the ``Decimal``-first path is to avoid binary-floating-point
surprises in money code.

Why this lives in a dedicated file (not as another case in
``tests/test_business_impact.py``): the unit-discriminator
matrix has eight cases (two unit values x four value types
x the two rejection paths) and the existing
``TestExtractorArgumentLookup`` class is about
positional/keyword lookup, not unit semantics. A focused
test class keeps the failure messages close to the failure
mode.

The cross-language golden hex pin (``dfc96387ca539b7130caebe705e042f2e34e52ab44352ae5e527bcef64f0df27``)
is unchanged: the canonical wire format is still minor units
(``amount_minor=5000`` for $50.00), regardless of which
``units`` the operator chose. The SDK converts in
``_to_minor_units`` before reaching ``BusinessImpact``, so the
wire shape is identical between the two paths.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from nullrun.extractor import (
    UNIT_MAJOR,
    UNIT_MINOR,
    _to_minor_units,
    money_outflow,
)
from nullrun.business_impact import BusinessImpact, OUTFLOW, INFLOW


# Golden cross-language pin shared with the backend's golden
# test (and pinned in tests/test_business_impact.py). The wire
# shape is in minor units regardless of the SDK's units
# discriminator.
GOLDEN_HEX_USD_50_DOLLARS_OUTFLOW = (
    "dfc96387ca539b7130caebe705e042f2e34e52ab44352ae5e527bcef64f0df27"
)


def _refund_dollars(amount: Decimal) -> dict:
    return {"amount": amount}


def _refund_cents(amount_cents: int) -> dict:
    return {"amount": amount_cents}


# ---------------------------------------------------------------------------
# 1. units="major" -- Decimal -> minor units conversion
# ---------------------------------------------------------------------------


class TestMajorUnitsDecimalConversion:
    """``units='major'`` accepts ``Decimal`` and multiplies by 100
    with banker's rounding. ``float`` and ``int`` are rejected
    outright."""

    def test_decimal_50_99_minor_5099(self) -> None:
        ext = money_outflow(
            argument="amount",
            currency="USD",
            units=UNIT_MAJOR,
        )
        impact = ext.impact_for(_refund_dollars, (Decimal("50.99"),), {})
        # The wire stores minor units (cents) regardless of the
        # input unit.
        assert impact.impact.amount_minor == 5_099

    def test_decimal_50_minor_5000(self) -> None:
        ext = money_outflow(
            argument="amount",
            currency="USD",
            units=UNIT_MAJOR,
        )
        impact = ext.impact_for(_refund_dollars, (Decimal("50"),), {})
        assert impact.impact.amount_minor == 5_000

    def test_decimal_50_005_rejected_for_usd(self) -> None:
        # Phase 1.1 production-grade contract: precision must be
        # supplied correctly by the caller. ``Decimal("50.005")``
        # is a sub-cent precision that USD does not support, so
        # the SDK raises ``ValueError`` rather than silently
        # rounding (no banker's rounding; no ROUND_HALF_UP; the
        # previous "drop half-cent silently" behaviour is the
        # exact bug class this contract prevents).
        ext = money_outflow(
            argument="amount",
            currency="USD",
            units=UNIT_MAJOR,
        )
        with pytest.raises(ValueError, match="USD supports at most 2"):
            ext.impact_for(_refund_dollars, (Decimal("50.005"),), {})

    def test_decimal_50_999_rejected_for_usd(self) -> None:
        ext = money_outflow(
            argument="amount",
            currency="USD",
            units=UNIT_MAJOR,
        )
        with pytest.raises(ValueError, match="USD supports at most 2"):
            ext.impact_for(_refund_dollars, (Decimal("50.999"),), {})

    def test_decimal_0_005_rejected_for_usd(self) -> None:
        # Sub-cent precision for any USD amount is rejected.
        ext = money_outflow(
            argument="amount",
            currency="USD",
            units=UNIT_MAJOR,
        )
        with pytest.raises(ValueError, match="USD supports at most 2"):
            ext.impact_for(_refund_dollars, (Decimal("0.005"),), {})

    def test_decimal_0_01_accepted_for_usd(self) -> None:
        # The boundary: 0.01 has exactly 2 fractional digits.
        ext = money_outflow(
            argument="amount",
            currency="USD",
            units=UNIT_MAJOR,
        )
        impact = ext.impact_for(_refund_dollars, (Decimal("0.01"),), {})
        assert impact.impact.amount_minor == 1

    def test_jpy_decimal_with_fractional_digits_rejected(self) -> None:
        # JPY has 0 fractional digits (yen). ``Decimal("100.5")``
        # is rejected because the caller has supplied sub-yen
        # precision.
        def _refund_jpy(amount: Decimal) -> dict:
            return {"a": amount}

        ext = money_outflow(
            argument="amount",
            currency="JPY",
            units=UNIT_MAJOR,
        )
        with pytest.raises(ValueError, match="JPY supports at most 0"):
            ext.impact_for(_refund_jpy, (Decimal("100.5"),), {})

    def test_jpy_decimal_integer_accepted(self) -> None:
        def _refund_jpy(amount: Decimal) -> dict:
            return {"a": amount}

        ext = money_outflow(
            argument="amount",
            currency="JPY",
            units=UNIT_MAJOR,
        )
        impact = ext.impact_for(_refund_jpy, (Decimal("1000"),), {})
        # JPY has 0 fractional digits, so the wire stores the
        # same integer; no conversion needed.
        assert impact.impact.amount_minor == 1000

    def test_kwd_three_fractional_digits_accepted(self) -> None:
        # KWD has 3 fractional digits (fils). ``Decimal("1.234")``
        # is exactly within the supported precision.
        def _refund_kwd(amount: Decimal) -> dict:
            return {"a": amount}

        ext = money_outflow(
            argument="amount",
            currency="KWD",
            units=UNIT_MAJOR,
        )
        impact = ext.impact_for(_refund_kwd, (Decimal("1.234"),), {})
        assert impact.impact.amount_minor == 1_234

    def test_kwd_four_fractional_digits_rejected(self) -> None:
        # ``Decimal("1.2345")`` is sub-fil precision for KWD.
        def _refund_kwd(amount: Decimal) -> dict:
            return {"a": amount}

        ext = money_outflow(
            argument="amount",
            currency="KWD",
            units=UNIT_MAJOR,
        )
        with pytest.raises(ValueError, match="KWD supports at most 3"):
            ext.impact_for(_refund_kwd, (Decimal("1.2345"),), {})

    def test_int_rejected_in_major_units(self) -> None:
        # A bare int in major units is the silent bug class
        # the explicit discriminator is designed to prevent.
        ext = money_outflow(
            argument="amount",
            currency="USD",
            units=UNIT_MAJOR,
        )
        with pytest.raises(TypeError, match="requires Decimal"):
            ext.impact_for(_refund_dollars, (50,), {})

    def test_float_rejected_outright(self) -> None:
        # ``float`` is the entire reason ``Decimal`` exists.
        ext = money_outflow(
            argument="amount",
            currency="USD",
            units=UNIT_MAJOR,
        )
        with pytest.raises(TypeError, match="requires Decimal"):
            ext.impact_for(_refund_dollars, (50.99,), {})

    def test_bool_rejected_in_major_units(self) -> None:
        # ``bool`` is a subclass of ``int`` in Python; the
        # explicit check rejects it so a hostile caller can't
        # smuggle ``True`` as ``amount=1`` cent.
        ext = money_outflow(
            argument="amount",
            currency="USD",
            units=UNIT_MAJOR,
        )
        with pytest.raises(TypeError, match="requires Decimal"):
            ext.impact_for(_refund_dollars, (True,), {})


# ---------------------------------------------------------------------------
# 2. units="minor" -- int passes through, Decimal needs quantization
# ---------------------------------------------------------------------------


class TestMinorUnitsIntAndDecimal:
    """``units='minor'`` accepts ``int`` (canonical) and ``Decimal``
    if it is already integer-valued. ``float`` is rejected."""

    def test_int_50_minor_50(self) -> None:
        ext = money_outflow(
            argument="amount_cents",
            currency="USD",
            units=UNIT_MINOR,
        )
        impact = ext.impact_for(_refund_cents, (50,), {})
        assert impact.impact.amount_minor == 50

    def test_decimal_50_minor_50(self) -> None:
        # The caller has already pre-quantized; the SDK does not
        # change the value. This path supports legacy code that
        # had been using ``Decimal("50.00")`` everywhere and
        # later adopts the decorator.
        ext = money_outflow(
            argument="amount_cents",
            currency="USD",
            units=UNIT_MINOR,
        )
        impact = ext.impact_for(_refund_cents, (Decimal("50"),), {})
        assert impact.impact.amount_minor == 50

    def test_decimal_50_00_minor_50(self) -> None:
        ext = money_outflow(
            argument="amount_cents",
            currency="USD",
            units=UNIT_MINOR,
        )
        impact = ext.impact_for(_refund_cents, (Decimal("50.00"),), {})
        assert impact.impact.amount_minor == 50

    def test_decimal_with_fractional_part_rejected_in_minor(self) -> None:
        # ``Decimal("0.05")`` with units="minor" is a unit-
        # confusion bug (the caller is passing major units
        # under a minor decorator). The SDK surfaces a
        # TypeError pointing the operator at the right
        # alternative.
        ext = money_outflow(
            argument="amount_cents",
            currency="USD",
            units=UNIT_MINOR,
        )
        with pytest.raises(TypeError, match="refusing to round"):
            ext.impact_for(_refund_cents, (Decimal("0.05"),), {})

    def test_float_rejected_in_minor_units(self) -> None:
        ext = money_outflow(
            argument="amount_cents",
            currency="USD",
            units=UNIT_MINOR,
        )
        with pytest.raises(TypeError, match="requires int or Decimal"):
            ext.impact_for(_refund_cents, (50.99,), {})

    def test_str_rejected_in_minor_units(self) -> None:
        ext = money_outflow(
            argument="amount_cents",
            currency="USD",
            units=UNIT_MINOR,
        )
        with pytest.raises(TypeError, match="requires int or Decimal"):
            ext.impact_for(_refund_cents, ("50",), {})

    def test_bool_rejected_in_minor_units(self) -> None:
        ext = money_outflow(
            argument="amount_cents",
            currency="USD",
            units=UNIT_MINOR,
        )
        with pytest.raises(TypeError, match="requires int or Decimal"):
            ext.impact_for(_refund_cents, (True,), {})


# ---------------------------------------------------------------------------
# 3. The cross-language golden hex pin survives the new path
# ---------------------------------------------------------------------------


class TestGoldenHexSurvivesNewPath:
    """The wire shape is in minor units regardless of the SDK's
    units discriminator. The cross-language golden hex must
    match whether the operator passed ``int(5000)``,
    ``Decimal('50')``, or ``Decimal('50.00')``."""

    def test_minor_int_5000_matches_golden(self) -> None:
        ext = money_outflow(
            argument="amount_cents",
            currency="USD",
            units=UNIT_MINOR,
        )
        impact = ext.impact_for(_refund_cents, (5_000,), {})
        # The canonical wire form is identical to the legacy
        # Phase 0 path. The golden hex is the SAME on the
        # backend side (see ``business_impact.rs::tests::
        # action_digest_golden_usd_outflow_5000_cents``).
        from nullrun.business_impact import compute_action_digest
        assert compute_action_digest(impact) == GOLDEN_HEX_USD_50_DOLLARS_OUTFLOW

    def test_major_decimal_50_matches_golden(self) -> None:
        # The operator writes ``Decimal("50.00")`` in major
        # units; the SDK converts to 5000 minor units; the
        # digest is byte-identical to the int(5000) path
        # above.
        ext = money_outflow(
            argument="amount",
            currency="USD",
            units=UNIT_MAJOR,
        )
        impact = ext.impact_for(_refund_dollars, (Decimal("50.00"),), {})
        from nullrun.business_impact import compute_action_digest
        assert compute_action_digest(impact) == GOLDEN_HEX_USD_50_DOLLARS_OUTFLOW


# ---------------------------------------------------------------------------
# 4. The unit discriminator is a constructor argument, not a type
# ---------------------------------------------------------------------------


class TestUnitDiscriminatorIsExplicit:
    """A signature refactor (``int`` -> ``Decimal`` or vice
    versa) does NOT silently flip the unit semantics. The
    operator must pass ``units='major'`` to opt into Decimal
    conversion."""

    def test_int_in_decimal_typed_arg_with_minor_units_passes_through(self) -> None:
        # Function declares ``amount: Decimal`` but the
        # decorator is configured with ``units="minor"``.
        # The int(50) value passes through verbatim because
        # the operator explicitly opted into minor units.
        # The amount is 50 minor = $0.50, NOT $50.00.
        def _dec(amount: Decimal) -> dict:
            return {"a": amount}

        ext = money_outflow(
            argument="amount",
            currency="USD",
            units=UNIT_MINOR,
        )
        impact = ext.impact_for(_dec, (50,), {})
        assert impact.impact.amount_minor == 50

    def test_decimal_in_int_typed_arg_with_major_units_converts(self) -> None:
        # Function declares ``amount_cents: int`` but the
        # operator passes ``Decimal("50.00")`` with
        # ``units="major"``. The SDK converts 50.00 to 5000
        # minor units. The type annotation is overridden by
        # the explicit unit discriminator.
        def _int(amount_cents: int) -> dict:
            return {"a": amount_cents}

        ext = money_outflow(
            argument="amount_cents",
            currency="USD",
            units=UNIT_MAJOR,
        )
        impact = ext.impact_for(_int, (Decimal("50.00"),), {})
        assert impact.impact.amount_minor == 5_000

    def test_unknown_units_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="units must be one of"):
            money_outflow(
                argument="amount",
                currency="USD",
                units="micros",
            )


# ---------------------------------------------------------------------------
# 5. Direct unit test for ``_to_minor_units``
# ---------------------------------------------------------------------------


class TestToMinorUnitsHelper:
    """``_to_minor_units`` is the conversion primitive. These
    tests pin the behaviour independent of the ``MoneyImpact``
    struct so a future refactor of the impact struct does not
    silently change the conversion semantics."""

    def test_minor_int_passes_through(self) -> None:
        assert _to_minor_units(50, UNIT_MINOR, "USD") == 50
        assert _to_minor_units(0, UNIT_MINOR, "USD") == 0
        assert _to_minor_units(1_000_000, UNIT_MINOR, "USD") == 1_000_000

    def test_minor_decimal_integer_passes_through(self) -> None:
        assert _to_minor_units(Decimal("50"), UNIT_MINOR, "USD") == 50
        assert _to_minor_units(Decimal("50.00"), UNIT_MINOR, "USD") == 50

    def test_major_decimal_multiplied_by_100(self) -> None:
        assert _to_minor_units(Decimal("50"), UNIT_MAJOR, "USD") == 5_000
        assert _to_minor_units(Decimal("50.99"), UNIT_MAJOR, "USD") == 5_099
        assert _to_minor_units(Decimal("1000.00"), UNIT_MAJOR, "USD") == 100_000

    def test_major_decimal_rejects_sub_cent_precision(self) -> None:
        # ``Decimal("0.005")`` for USD has 3 fractional digits
        # but USD supports 2; the helper raises ``ValueError``
        # rather than silently rounding. This is the
        # production-grade contract that replaced banker's
        # rounding.
        with pytest.raises(ValueError, match="USD supports at most 2"):
            _to_minor_units(Decimal("0.005"), UNIT_MAJOR, "USD")
        with pytest.raises(ValueError, match="USD supports at most 2"):
            _to_minor_units(Decimal("50.005"), UNIT_MAJOR, "USD")
        with pytest.raises(ValueError, match="USD supports at most 2"):
            _to_minor_units(Decimal("0.999"), UNIT_MAJOR, "USD")

    def test_major_decimal_rejects_sub_yen_precision(self) -> None:
        with pytest.raises(ValueError, match="JPY supports at most 0"):
            _to_minor_units(Decimal("100.5"), UNIT_MAJOR, "JPY")

    def test_major_decimal_accepts_three_digit_kwd(self) -> None:
        # KWD has 3 fractional digits; ``Decimal("1.234")`` is
        # accepted.
        assert _to_minor_units(Decimal("1.234"), UNIT_MAJOR, "KWD") == 1_234

    def test_major_decimal_rejects_four_digit_kwd(self) -> None:
        with pytest.raises(ValueError, match="KWD supports at most 3"):
            _to_minor_units(Decimal("1.2345"), UNIT_MAJOR, "KWD")

    def test_major_rejects_int(self) -> None:
        with pytest.raises(TypeError, match="requires Decimal"):
            _to_minor_units(50, UNIT_MAJOR, "USD")

    def test_minor_rejects_float(self) -> None:
        with pytest.raises(TypeError, match="requires int or Decimal"):
            _to_minor_units(50.99, UNIT_MINOR, "USD")

    def test_minor_rejects_fractional_decimal(self) -> None:
        with pytest.raises(TypeError, match="refusing to round"):
            _to_minor_units(Decimal("0.05"), UNIT_MINOR, "USD")

    def test_rejects_bool_everywhere(self) -> None:
        with pytest.raises(TypeError, match="requires int or Decimal"):
            _to_minor_units(True, UNIT_MINOR, "USD")
        with pytest.raises(TypeError, match="requires Decimal"):
            _to_minor_units(True, UNIT_MAJOR, "USD")

    def test_unknown_units_is_defensive_branch(self) -> None:
        # ``__init__`` validates ``units`` at construction time,
        # so this branch is unreachable from the public API.
        # We test it directly to lock the safety net.
        with pytest.raises(ValueError, match="unknown units"):
            _to_minor_units(50, "micros", "USD")


# ---------------------------------------------------------------------------
# 6. The ``BusinessImpact`` direction is unaffected
# ---------------------------------------------------------------------------


class TestDirectionIsUnaffected:
    """``units`` does not interact with ``direction`` (outflow /
    inflow). The default direction is OUTFLOW, matching the
    Phase 0 / pre-Decimal path."""

    def test_major_units_default_direction_is_outflow(self) -> None:
        ext = money_outflow(
            argument="amount",
            currency="USD",
            units=UNIT_MAJOR,
        )
        impact = ext.impact_for(_refund_dollars, (Decimal("50.00"),), {})
        assert impact.impact.direction == OUTFLOW
        assert impact.impact.currency == "USD"
        assert impact.impact.amount_minor == 5_000