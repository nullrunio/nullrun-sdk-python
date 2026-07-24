"""BusinessImpact + action_digest (SDK mirror of backend).

Phase 1 / MVP 1.0 (Разрыв 1c follow-up). The Python SDK
must produce the *exact* same SHA-256 hex digest the Rust
backend computes, so the digest re-check on /execute re-check
matches byte-for-byte. Drift between SDK and backend would be
caught at the first mismatch attack on a real customer.

Wire format mirrors `backend::proxy::gate::business_impact`:
- discriminated union with a single MVP variant `kind="money"`
- `MoneyImpact(direction, amount_minor, currency, ...)`
- `Condition(MoneyAmount(direction, operator, threshold_minor,
  currency))` lives on the **rule side** in the backend; the
  SDK never constructs Conditions directly — operators write
  them in the dashboard. The SDK only ever produces Impact
  payloads.

JSON canonicalization (backend reference, Rust):

  1. Serialize via `serde_json::to_value(self)`.
  2. Recursively sort every object key.
  3. Serialize back to compact JSON.
  4. SHA-256 over `b"nullrun/v1/business_impact:" || canonical`
     (prefix is part of the digest domain — keeps the v2
     protocol from accidentally matching v1 digests).

The Python mirror below must match step-for-step. Any drift is
a P0 security bug — see `tests/test_business_impact.py`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional

DIGEST_PREFIX = b"nullrun/v1/business_impact:"


# Direction enum (mirror Rust MoneyDirection; lowercase string on wire).
OUTFLOW = "outflow"
INFLOW = "inflow"


# Operator enum (mirror Rust ConditionOperator; lowercase string on wire).
GT = "gt"
GTE = "gte"
EQ = "eq"


# MVP: only `money` kind is supported; the discriminated union is
# shaped forward-compat for record_count / resource_quantity etc.
# when they land in MVPs 1.1+.
KIND_MONEY = "money"


@dataclass
class MoneyImpact:
    """Flat per-call money amount, USD-centric in MVP 1.0.

    Attributes:
        direction: "outflow" (refund/payout) or "inflow" (charge/invoice).
            MVP approval rules only fire on outflow.
        amount_minor: integer cents for USD, MUST be non-negative.
            Negatives are rejected at validate() time. Sign convention
            is `direction`, not `+/- amount` — do not switch.
        currency: ISO-4217 (3 uppercase letters). MVP is "USD". The
            backend treats any other currency as a no-match against a
            USD-only rule (separate per-currency rule needed by author).
        extractor_id: self-reported SDK extractor id (e.g. "nullrun.money.path").
        extractor_version: self-reported version.
    """

    direction: str
    amount_minor: int
    currency: str
    extractor_id: str = "nullrun.money.path"
    extractor_version: str = "1"

    def validate(self) -> None:
        """Reject malformed impacts at extraction time (fail-fast).

        Raises ValueError with a human-readable reason. The
        backend's `MoneyImpact::validate()` mirrors these checks.
        """
        if self.direction not in (OUTFLOW, INFLOW):
            raise ValueError(
                f"direction must be {OUTFLOW!r} or {INFLOW!r}, "
                f"got {self.direction!r}"
            )
        if not isinstance(self.amount_minor, int) or isinstance(
            self.amount_minor, bool
        ):
            # bool is a subclass of int in Python — explicit exclude.
            raise ValueError(
                f"amount_minor must be int, got {type(self.amount_minor).__name__}"
            )
        if self.amount_minor < 0:
            raise ValueError(
                f"amount_minor must be non-negative, got {self.amount_minor}"
            )
        if (
            not isinstance(self.currency, str)
            or len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isupper()
        ):
            raise ValueError(
                f"currency must be a 3-letter uppercase ISO-4217 code, "
                f"got {self.currency!r}"
            )

    def to_wire_dict(self) -> dict[str, Any]:
        """Serialize to the JSON shape the backend expects.

        Key order is NOT significant here — the backend's
        `BusinessImpact::canonical_json()` re-sorts keys before
        hashing. We still emit a stable Python order so debug
        logs read top-to-bottom the way the operator wrote them.
        """
        return {
            "kind": KIND_MONEY,
            "direction": self.direction,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "extractor_id": self.extractor_id,
            "extractor_version": self.extractor_version,
        }


def business_impact_to_dict(impact: BusinessImpact) -> dict[str, Any]:
    """Top-level wire dict for `GateRequest.business_impact`.

    Returns an empty string key discriminator for the backend's
    `serde(tag = "kind", rename_all = "snake_case")` shape.
    """
    return impact.to_wire_dict()


# Dataclasses that mirror the Rust backend's discriminated union via
# `kind` discriminator. In Python we represent the union as a
# tagged dict at the wire layer and a small class hierarchy at the
# in-process layer. MVP 1.0 only materializes MoneyImpact.
@dataclass
class BusinessImpact:
    """Top-level BusinessImpact union.

    For MVP 1.0 the only supported variant is `Money`. Future
    kinds land by adding new subclasses and a `kind` value.
    The SDK validates the variant at construction time so the
    backend never sees malformed output.
    """

    impact: Any  # MoneyImpact in MVP.

    @property
    def kind(self) -> str:
        if isinstance(self.impact, MoneyImpact):
            return KIND_MONEY
        raise TypeError(f"unknown impact type: {type(self.impact)}")

    def validate(self) -> None:
        self.impact.validate()

    def to_wire_dict(self) -> dict[str, Any]:
        return business_impact_to_dict(self.impact)

    @classmethod
    def money(
        cls,
        direction: str,
        amount_minor: int,
        currency: str = "USD",
    ) -> BusinessImpact:
        m = MoneyImpact(
            direction=direction,
            amount_minor=amount_minor,
            currency=currency,
        )
        m.validate()
        return cls(impact=m)


def _canonicalize_json(value: Any) -> Any:
    """Sort object keys recursively before serialization.

    Mirrors `BusinessImpact::canonical_json()` in the backend.
    """
    if isinstance(value, dict):
        items = []
        for k, v in value.items():
            items.append((k, _canonicalize_json(v)))
        items.sort(key=lambda kv: kv[0])
        return {k: v for k, v in items}
    if isinstance(value, list):
        return [_canonicalize_json(v) for v in value]
    return value


def compute_action_digest(impact: BusinessImpact) -> str:
    """Compute the SHA-256 digest the backend expects.

    Algorithm (must match backend/src/proxy/gate/business_impact.rs
    byte-for-byte):
      1. Validate the impact at extract time (fail-fast).
      2. Convert to wire dict.
      3. Canonicalize (sort object keys recursively).
      4. Serialize to compact JSON (no spaces).
      5. Hash with the protocol-prefix bytes as a salt.
      6. Return lowercase hex.

    Returns 64 lowercase hex characters. The backend's
    `compute_action_digest` is byte-identical; any drift is a
    P0 security regression covered by
    `tests/test_business_impact.py::test_digest_matches_backend`.
    """
    impact.validate()
    canonical_value = _canonicalize_json(impact.to_wire_dict())
    canonical_bytes = json.dumps(
        canonical_value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=False,
    ).encode("utf-8")
    hasher = hashlib.sha256()
    hasher.update(DIGEST_PREFIX)
    hasher.update(canonical_bytes)
    return hasher.hexdigest()


# Backwards-compat: a thin class wrapper for the discriminated union
# is exposed via `BusinessImpact.kind` and `BusinessImpact.to_wire_dict`,
# but tests and runtime code that already uses dict literals continue
# to work. The validator at extract time catches malformed payloads.
