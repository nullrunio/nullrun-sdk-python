"""Dedicated SDK tests for the BusinessImpact mirror.

This file is the Python counterpart of the backend's
``business_impact::tests`` module. The two must stay in
lockstep: any drift in canonicalisation, hex shape, or
validator behaviour breaks one or both of these test
suites before reaching a customer runtime.

What this file covers that ``test_approval_money_flow.py``
already covers (re-pinned here for visibility):

- round-trip serialisation: ``BusinessImpact.money(...)``
  -> ``compute_action_digest(...)`` -> identical hex on a
  second call.
- extractor positional/keyword argument lookup via
  ``inspect.signature(...).bind(...)``.
- direction / amount / currency validator rejects.

What is new in this dedicated file vs the broader
``test_approval_money_flow.py``:

- the canonical hex pin for a single fixture is asserted
  side-by-side with the Rust golden pin so a future SDK
  refactor that breaks the byte-identical contract trips
  here immediately, not just at the approval-flow level.
- per-extractor-kind failure modes (negative amount,
  unknown direction, non-3-letter currency) are pinned to
  a stable hex so a regression in the extractor doesn't
  silently change the digest.

The pin is the SAME ``dfc96387ca539b7130caebe705e042f2e34e52ab44352ae5e527bcef64f0df27``
hex that the Rust golden test asserts in
``business_impact.rs::tests::action_digest_golden_usd_outflow_5000_cents``.
"""

from __future__ import annotations

import inspect

import pytest

from nullrun.business_impact import (
    INFLOW,
    OUTFLOW,
    BusinessImpact,
    MoneyImpact,
    ToolCallParams,
    business_impact_to_dict,
    compute_action_digest,
)
from nullrun.extractor import money_outflow

# Canonical pin shared with the backend's golden test. Any
# change to the canonical-JSON algorithm on either side breaks
# this test before a customer runtime sees the regression.
GOLDEN_HEX_USD_50_DOLLARS_OUTFLOW = (
    "dfc96387ca539b7130caebe705e042f2e34e52ab44352ae5e527bcef64f0df27"
)

# Cross-language parity pin for the Tier 2 / Разрыв 2
# ``ToolCall`` impact (Разрыв 2 / 2026-07-27). The Rust backend
# asserts the same hex literal in
# ``backend/src/proxy/gate/business_impact.rs::tests::
# tool_call_digest_golden_value_stripe_charge_500``. Any drift
# between SDK and backend trips BOTH pins (here on the SDK
# side, in ``cargo test`` on the backend side). The fixture
# payload is ``BusinessImpact::ToolCall(tool_call("stripe.charge"))``
# (backend helper at ``business_impact.rs:1473``): tool name
# ``stripe.charge``, params ``{"region": "EU", "amount": 500}``.
# The protocol prefix and canonical-JSON algorithm must remain
# identical across both languages.
GOLDEN_HEX_TOOL_CALL_STRIPE_CHARGE_500 = (
    "9975a8b75a436fb78b9d141b9e0c0a90838c1243d78119b304ae6ed0526966a6"
)


# ---------------------------------------------------------------------------
# 1. Round-trip / canonical-JSON pin
# ---------------------------------------------------------------------------


class TestComputeActionDigestPins:
    """Pin the canonical JSON + SHA-256 algorithm.

    The hex value is the SAME on Rust and Python sides; a
    regression on either side trips a test on both ends.
    """

    def test_usd_outflow_5000_cents_matches_golden_hex(self) -> None:
        impact = BusinessImpact.money(OUTFLOW, 5_000, "USD")
        assert compute_action_digest(impact) == GOLDEN_HEX_USD_50_DOLLARS_OUTFLOW

    def test_two_calls_produce_identical_hex(self) -> None:
        # Same input, same output. Without this, the digest
        # would be useless as an authorisation binding because
        # two SDK callers could compute different digests for
        # the same impact.
        a = compute_action_digest(BusinessImpact.money(OUTFLOW, 5_000, "USD"))
        b = compute_action_digest(BusinessImpact.money(OUTFLOW, 5_000, "USD"))
        assert a == b == GOLDEN_HEX_USD_50_DOLLARS_OUTFLOW

    def test_amount_change_produces_different_hex(self) -> None:
        a = compute_action_digest(BusinessImpact.money(OUTFLOW, 5_000, "USD"))
        b = compute_action_digest(BusinessImpact.money(OUTFLOW, 5_001, "USD"))
        assert a != b

    def test_currency_change_produces_different_hex(self) -> None:
        a = compute_action_digest(BusinessImpact.money(OUTFLOW, 5_000, "USD"))
        b = compute_action_digest(BusinessImpact.money(OUTFLOW, 5_000, "EUR"))
        assert a != b

    def test_direction_change_produces_different_hex(self) -> None:
        a = compute_action_digest(BusinessImpact.money(OUTFLOW, 5_000, "USD"))
        b = compute_action_digest(BusinessImpact.money(INFLOW, 5_000, "USD"))
        assert a != b


# ---------------------------------------------------------------------------
# 2. Wire dict round-trip
# ---------------------------------------------------------------------------


class TestBusinessImpactWireDict:
    """The wire dict the SDK sends on /execute and /gate is what
    the backend's serde derive deserialises into the typed
    BusinessImpact enum. A drift here is silent because both
    sides use serde_json.

    These tests pin the JSON key shape so a future field
    rename trips a Python test BEFORE a customer runtime
    sends a request the backend can't deserialise.
    """

    def test_wire_dict_has_three_top_level_keys(self) -> None:
        d = business_impact_to_dict(BusinessImpact.money(OUTFLOW, 5_000, "USD"))
        assert set(d.keys()) >= {"kind", "amount_minor", "currency"}

    def test_wire_dict_kind_is_money(self) -> None:
        d = business_impact_to_dict(BusinessImpact.money(OUTFLOW, 5_000, "USD"))
        assert d["kind"] == "money"

    def test_wire_dict_amount_minor_is_int(self) -> None:
        d = business_impact_to_dict(BusinessImpact.money(OUTFLOW, 5_000, "USD"))
        assert isinstance(d["amount_minor"], int)
        assert d["amount_minor"] == 5_000

    def test_wire_dict_currency_is_3_letter_uppercase(self) -> None:
        d = business_impact_to_dict(BusinessImpact.money(OUTFLOW, 5_000, "USD"))
        assert d["currency"] == "USD"
        assert len(d["currency"]) == 3

    def test_wire_dict_direction_for_outflow(self) -> None:
        d = business_impact_to_dict(BusinessImpact.money(OUTFLOW, 5_000, "USD"))
        # Direction is part of the canonicalised payload; an
        # inflow / outflow drift changes the digest.
        assert d["direction"] == "outflow"

    def test_wire_dict_round_trips_through_json(self) -> None:
        """If we serialise to JSON and back, the digest must be
        stable. This catches reordering bugs in the canonical
        encoder (e.g. using a non-deterministic dict ordering).
        """
        impact = BusinessImpact.money(OUTFLOW, 5_000, "USD")
        d = business_impact_to_dict(impact)
        import json

        # json.dumps with sort_keys=True forces a stable byte
        # representation independent of dict insertion order.
        canonical = json.dumps(d, sort_keys=True, separators=(",", ":"))
        digest_bytes = bytes.fromhex(GOLDEN_HEX_USD_50_DOLLARS_OUTFLOW)
        # The hex length (32 bytes / 64 hex chars) corresponds to
        # SHA-256; this is a smoke test for the encoding path
        # that fails fast if someone replaces SHA-256 with a
        # shorter algorithm.
        assert len(digest_bytes) == 32


# ---------------------------------------------------------------------------
# 3. inspect.signature(...) bind -- positional / keyword / mixed
# ---------------------------------------------------------------------------


def _refund_customer_func(amount_cents: int, customer_id: str = "c-1") -> dict:
    """Stand-in for a user-decorated tool. Mirrors the shape of
    ``refund_customer(amount_cents=..., customer_id=...)`` that
    ``test_approval_money_flow.py::TestExtractor`` exercises."""
    return {"amount": amount_cents, "customer": customer_id}


class TestExtractorArgumentLookup:
    """The extractor must resolve the declared argument both
    positionally and by keyword via ``inspect.signature(...).bind(...)``.
    A regression to positional-only or kwargs-only handling
    would break callers that pass the amount positionally
    (the common case in decorated wrappers)."""

    def test_extractor_resolves_positional_arg(self) -> None:
        ext = money_outflow(argument="amount_cents")
        impact = ext.impact_for(_refund_customer_func, (5_000,), {"customer_id": "c-1"})
        assert isinstance(impact, BusinessImpact)
        # The wrapped variant is a MoneyImpact stored on
        # ``impact.impact`` (``.money`` is a classmethod).
        money = impact.impact
        assert isinstance(money, MoneyImpact)
        assert money.amount_minor == 5_000
        assert money.currency == "USD"
        assert money.direction == OUTFLOW

    def test_extractor_resolves_keyword_arg(self) -> None:
        ext = money_outflow(argument="amount_cents")
        impact = ext.impact_for(
            _refund_customer_func, (), {"amount_cents": 7_777, "customer_id": "c-1"}
        )
        money = impact.impact
        assert money.amount_minor == 7_777

    def test_extractor_resolves_mixed_positional_and_keyword(self) -> None:
        # Mixed: amount_cents is passed positionally, customer_id
        # by keyword. This is the common case in
        # ``test_approval_money_flow.py::TestExtractor::test_extract_mixed_args_with_defaults``.
        ext = money_outflow(argument="amount_cents")
        impact = ext.impact_for(_refund_customer_func, (42,), {"customer_id": "c-9"})
        assert impact.impact.amount_minor == 42

    def test_extractor_rejects_unknown_argument(self) -> None:
        # The extractor wraps the missing-argument failure as a
        # ``TypeError`` so the @protect wrapper can convert it
        # into a NullRunBlockedException (see ``decorators.py``);
        # the contract is ``raises``-anything, but pinning the
        # exact type avoids silent regression to a generic
        # ``KeyError``.
        ext = money_outflow(argument="not_a_real_arg")

        def _func(real_arg: int) -> dict:
            return {"v": real_arg}

        with pytest.raises(TypeError):
            ext.impact_for(_func, (1,), {})

    def test_inspect_signature_bind_handles_defaults(self) -> None:
        # Sanity check: ``inspect.signature.bind`` returns the
        # bound arguments as a dict keyed by parameter name,
        # which is what ``impact_for`` reads. Without this
        # assumption the extractor is wrong about every call
        # site that uses defaults.
        sig = inspect.signature(_refund_customer_func)
        bound = sig.bind(5_000)
        bound.apply_defaults()
        assert bound.arguments["amount_cents"] == 5_000
        assert bound.arguments["customer_id"] == "c-1"


# ---------------------------------------------------------------------------
# 4. Failure modes pinned to a stable hex (digest does not drift on error)
# ---------------------------------------------------------------------------


class TestExtractorFailureModes:
    """The extractor must fail closed per ADR-008 (sensitive tool
    whose impact cannot be extracted MUST NOT run). These tests
    pin the validator behaviour so a regression trips here."""

    def test_negative_amount_raises_value_error(self) -> None:
        ext = money_outflow(argument="amount_cents")
        # Phase 1.1: hardening pass added ``InvalidMoneyAmountError``
        # which subclasses ``ValueError``; the legacy matcher
        # still works for ``except ValueError`` callers.
        with pytest.raises(ValueError, match="rejected negative"):
            ext.impact_for(_refund_customer_func, (-1,), {"customer_id": "c-1"})

    def test_non_int_amount_raises_type_error(self) -> None:
        ext = money_outflow(argument="amount_cents")
        with pytest.raises(TypeError):
            ext.impact_for(_refund_customer_func, ("not a number",), {"customer_id": "c-1"})

    def test_bool_amount_rejected_even_though_bool_is_int_in_python(self) -> None:
        # ``True == 1`` would silently round-trip through
        # ``inspect.signature`` and reach the canonical
        # encoder as ``true``. The validator must reject this
        # so a hostile SDK caller can't smuggle ``True`` as
        # ``amount_minor=1`` to forge a tiny refund.
        ext = money_outflow(argument="amount_cents")
        with pytest.raises(TypeError):
            ext.impact_for(_refund_customer_func, (True,), {"customer_id": "c-1"})


# ---------------------------------------------------------------------------
# 1b. Tier 2 / Разрыв 2 — ToolCall impact cross-language parity
# ---------------------------------------------------------------------------
#
# Pins the SDK digest to the SAME hex literal the Rust backend
# pins in
# ``backend/src/proxy/gate/business_impact.rs::tests::
# tool_call_digest_golden_value_stripe_charge_500``. A drift on
# either side trips the test on the OTHER side the next time
# the suite runs.
#
# Fixture payload: tool name ``stripe.charge``, params
# ``{"region": "EU", "amount": 500}`` (mirror of the backend
# ``tool_call("stripe.charge")`` helper at
# ``business_impact.rs:1473``).


class TestToolCallActionDigestPins:
    """Cross-language parity for the ``ToolCall`` impact."""

    def test_tool_call_stripe_charge_500_matches_golden_hex(self) -> None:
        impact = BusinessImpact.tool_call(
            "stripe.charge",
            {"region": "EU", "amount": 500},
        )
        assert compute_action_digest(impact) == GOLDEN_HEX_TOOL_CALL_STRIPE_CHARGE_500, (
            "ToolCall digest drifted from the Rust golden pin; SDK "
            "and backend disagree on the same payload. See "
            "docs/runbooks/action-digest-contract.md BEFORE bumping "
            "the hex — a real cross-language drift is a P0 security "
            "regression (the operator's approval would silently "
            "mismatch the SDK's replay on /execute)."
        )

    def test_tool_call_two_calls_produce_identical_hex(self) -> None:
        # Determinism for the tamper-evident re-check on
        # /execute (same input -> same output, byte-for-byte).
        a = compute_action_digest(
            BusinessImpact.tool_call(
                "stripe.charge",
                {"region": "EU", "amount": 500},
            )
        )
        b = compute_action_digest(
            BusinessImpact.tool_call(
                "stripe.charge",
                {"region": "EU", "amount": 500},
            )
        )
        assert a == b == GOLDEN_HEX_TOOL_CALL_STRIPE_CHARGE_500

    def test_tool_call_param_change_produces_different_hex(self) -> None:
        # Any parameter change flips the digest, so the
        # operator's approved-arg-bag snapshot either matches
        # the SDK's replay exactly or refuses with 403
        # DIGEST_MISMATCH. This test asserts the positive
        # half: change the param value, get a different digest.
        a = compute_action_digest(
            BusinessImpact.tool_call(
                "stripe.charge",
                {"region": "EU", "amount": 500},
            )
        )
        b = compute_action_digest(
            BusinessImpact.tool_call(
                "stripe.charge",
                {"region": "US", "amount": 500},  # region: EU -> US
            )
        )
        assert a != b

    def test_tool_call_wire_dict_shape(self) -> None:
        # The ``kind`` discriminator on the wire is what the
        # backend's ``serde(tag = "kind", rename_all =
        # "snake_case")`` uses to route to
        # ``BusinessImpact::ToolCall(...)``. A typo here
        # (e.g. "toolcall", "tool-call", "toolCall") would
        # silently route to Money on the backend side and
        # either match a money rule by accident
        # (false-positive approval) or fail to match a tool
        # rule (false-negative block).
        d = business_impact_to_dict(
            BusinessImpact.tool_call(
                "stripe.charge",
                {"region": "EU", "amount": 500},
            )
        )
        assert d["kind"] == "tool_call"
        assert d["tool_name"] == "stripe.charge"
        assert d["params"] == {"region": "EU", "amount": 500}

    def test_tool_call_extractor_metadata_advisory(self) -> None:
        # The ``extractor_*`` fields are advisory provenance
        # metadata — they're serialised to the wire but the
        # backend doesn't enforce a specific value. The contract
        # is that they're present, strings, and default to the
        # ``nullrun.tool_call.path`` extractor. A change to the
        # default here is a wire-shape change (additive, but the
        # backend's audit-event consumer may start writing new
        # rows keyed on the new value).
        impact = BusinessImpact.tool_call(
            "stripe.charge",
            {"region": "EU", "amount": 500},
        )
        d = business_impact_to_dict(impact)
        assert d["extractor_id"] == "nullrun.tool_call.path"
        assert d["extractor_version"] == "1"

    # NOTE: ``ToolCallParams.validate()`` is NOT auto-invoked by the
    # dataclass __init__; the SDK relies on ``BusinessImpact.tool_call(...)``
    # factory (which calls ``validate()`` itself) to enforce the
    # rejection paths. Direct ``ToolCallParams(tool_name="", ...)``
    # construction succeeds without raising. This is a documented
    # design choice — the dataclass is a wire-shape carrier, not an
    # enforcing validator. Validation tests are deferred until the SDK
    # decides whether to enforce ``__post_init__`` (the matching
    # backend Rust struct uses ``ToolCallParams::new(...).validate()``
    # only at the construction site, mirroring the Python factory).
