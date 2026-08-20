"""BusinessImpact + action_digest (SDK mirror of backend).

The SDK must produce the *exact* same SHA-256 hex digest the Rust
backend computes, so the digest re-check on /execute re-check
matches byte-for-byte. Drift between SDK and backend would be
caught at the first mismatch attack on a real customer.

Wire format mirrors `backend::proxy::gate::business_impact`:
- discriminated union with a single variant `kind="money"`
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


# `money` kind for per-call flat amounts.
# `tool_call` kind for free-form tool-call argument bags matched
# against ToolParameters Approval Rules on the backend.
# `none` kind for non-impact LLM/tool calls that still need an
# `action_digest` on the wire (per `backend/src/proxy/http/gate/gate.rs:56`
# v3.62.1 / ADR-023 P1-6 — Phase-1+ SDKs MUST supply `action_digest`
# even when there is no typed business impact to extract).
KIND_MONEY = "money"
KIND_TOOL_CALL = "tool_call"
KIND_NONE = "none"


# Mirrors the backend constant at
# ``backend/src/proxy/gate/business_impact.rs`` (the same value
# caps both the SDK-side mirror's ``tool_name`` and per-key name
# length). Kept in sync manually; a backend-side bump is a one-line
# edit here.
TOOL_PARAMETERS_MAX_PARAM_NAME = 64


@dataclass
class MoneyImpact:
    """Flat per-call money amount.

    Attributes:
        direction: "outflow" (refund/payout) or "inflow" (charge/invoice).
            Approval rules only fire on outflow.
        amount_minor: integer cents for USD, MUST be non-negative.
            Negatives are rejected at validate() time. Sign convention
            is `direction`, not `+/- amount` — do not switch.
        currency: ISO-4217 (3 uppercase letters). Default is "USD". The
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
            raise ValueError(f"direction must be {OUTFLOW!r} or {INFLOW!r}, got {self.direction!r}")
        if not isinstance(self.amount_minor, int) or isinstance(self.amount_minor, bool):
            # bool is a subclass of int in Python — explicit exclude.
            raise ValueError(f"amount_minor must be int, got {type(self.amount_minor).__name__}")
        if self.amount_minor < 0:
            raise ValueError(f"amount_minor must be non-negative, got {self.amount_minor}")
        if (
            not isinstance(self.currency, str)
            or len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isupper()
        ):
            raise ValueError(
                f"currency must be a 3-letter uppercase ISO-4217 code, got {self.currency!r}"
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


@dataclass
class ToolCallParams:
    """Free-form tool-call argument bag.

    Mirrors the backend ``BusinessImpact::ToolCall(ToolCallParams)``
    variant at ``backend/src/proxy/gate/business_impact.rs:62-307``.
    The backend matches ``params`` against ToolParameters Approval
    Rules (``ValueMatcher``: Equals / OneOf / NumericRange / Regex /
    Exists; ``TriggerLogic``: Any / All / DNF groups).

    Why this exists as a separate dataclass (rather than reusing the
    raw ``dict[str, Any]`` that the runtime already passes around):
    - the validator enforces ``tool_name`` shape and the
      canonical-JSON digest layer needs a stable, sortable struct
      to produce a byte-identical digest with the backend
      ``canonical_json()`` implementation
    - the ``extractor_*`` fields mirror the ``MoneyImpact``
      provenance pattern: self-reported by the SDK, treated as
      advisory metadata. The trust boundary is the digest
      round-trip — the SDK and backend both canonicalise the
      same payload to the same bytes, and a mismatch on /execute
      re-check is a 403 DIGEST_MISMATCH

    Attributes:
        tool_name: canonical name of the tool the SDK is about to
            call. Must be non-empty and <= 128 bytes.
        params: free-form argument bag the operator wrote the rule
            against. Keyed by the rule's ``param_name`` field.
        extractor_id: self-reported SDK extractor id (e.g.
            "nullrun.tool_call.path").
        extractor_version: self-reported version.
    """

    tool_name: str
    params: dict[str, Any] = field(default_factory=dict)
    extractor_id: str = "nullrun.tool_call.path"
    extractor_version: str = "1"

    def validate(self) -> None:
        """Reject malformed impacts at extraction time (fail-fast).

        Mirrors ``ToolCallParams::validate()`` in the backend so a
        tool with bad extractor args fails locally before the wire
        round-trip (one error class, one user_action message).
        """
        if not isinstance(self.tool_name, str) or not self.tool_name:
            raise ValueError("tool_name must be a non-empty string")
        if len(self.tool_name) > 128:
            raise ValueError(f"tool_name length {len(self.tool_name)} exceeds max 128")
        if not self.tool_name.isascii():
            raise ValueError("tool_name must be printable ASCII")
        for k in self.params:
            if not isinstance(k, str):
                raise ValueError(f"params key {k!r} must be a string")
            if len(k) > TOOL_PARAMETERS_MAX_PARAM_NAME:
                raise ValueError(
                    f"params['{k}'] key length {len(k)} exceeds "
                    f"max {TOOL_PARAMETERS_MAX_PARAM_NAME}"
                )
            _validate_param_value(self.params[k], path=f"params['{k}']")

    def to_wire_dict(self) -> dict[str, Any]:
        """Serialize to the JSON shape the backend expects.

        Key order is NOT significant — the backend's
        ``canonical_json()`` re-sorts keys before hashing.
        """
        return {
            "kind": KIND_TOOL_CALL,
            "tool_name": self.tool_name,
            "params": dict(self.params),
            "extractor_id": self.extractor_id,
            "extractor_version": self.extractor_version,
        }


def _validate_param_value(value: Any, path: str) -> None:
    """Reject values that the digest layer cannot round-trip.

    Backend mirror at ``business_impact.rs:310-318``: the canonical
    JSON layer accepts the four JSON kinds (null/bool/number/string/
    object/array) but rejects f64 and non-finite numbers because
    ``serde_json::Number`` cannot losslessly represent them. We do
    the same here so the SDK fails at extraction time rather than
    producing a digest that the backend will reject.
    """
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        # int round-trips through JSON losslessly. NOTE: bool is a
        # subclass of int in Python; we explicitly handle it above.
        return
    if isinstance(value, str):
        return
    if isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _validate_param_value(item, path=f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for k, v in value.items():
            _validate_param_value(v, path=f"{path}['{k}']")
        return
    if isinstance(value, float):
        # Reject explicitly -- we DO NOT round to int because the
        # operator might be relying on sub-cent precision (this is
        # the same rationale as MoneyImpactExtractor rejecting
        # ``float`` for money amounts).
        raise ValueError(
            f"{path}: float values are not supported on the wire "
            f"(JSON round-trip is not lossless for IEEE-754); pass "
            f"an int (minor units) or a str (operator-defined format)"
        )
    raise ValueError(
        f"{path}: value of type {type(value).__name__!r} is not "
        f"supported on the wire; pass int / str / bool / None / "
        f"list / dict"
    )


def business_impact_to_dict(impact: BusinessImpact) -> dict[str, Any]:
    """Top-level wire dict for `GateRequest.business_impact`.

    Returns an empty string key discriminator for the backend's
    `serde(tag = "kind", rename_all = "snake_case")` shape.
    """
    return impact.to_wire_dict()


# Dataclasses that mirror the Rust backend's discriminated union via
# `kind` discriminator. In Python we represent the union as a
# tagged dict at the wire layer and a small class hierarchy at the
# in-process layer. The SDK validates the variant at construction
# time so the backend never sees malformed output.
@dataclass
class NoImpactPayload:
    """Sentinel payload for non-impact calls (plain LLM chat, etc.).

    Phase-1+ SDKs MUST populate `action_digest` on every `/gate`
    call (per `backend/src/proxy/http/gate/gate.rs:56` v3.62.1 /
    ADR-023 P1-6 — fail-CLOSED wire-shape version-gate). Calls
    that have no typed business impact (regular LLM chat,
    read-only tool calls without an approval rule) need a
    deterministic digest that the wire-shape check accepts.

    The canonical JSON of this payload is ``{"kind":"none"}``
    (compact, key-sorted). The corresponding digest is the SHA-256
    of ``nullrun/v1/business_impact:{"kind":"none"}`` and is
    pinned as a literal in
    ``tests/test_business_impact.py::test_no_impact_digest_pins_hex``
    so a drift between SDK and backend (or a stray canonicalisation
    change) is caught at unit-test time.

    This variant exists ONLY on the SDK side. The backend's
    `GateRequestBody.business_impact` field stays ``None`` for
    non-impact calls — the `action_digest` field is the one the
    wire-shape gate checks. Adding a NoImpact arm to the backend's
    ``BusinessImpact`` enum is a follow-up if / when the digest
    re-check path needs to reverse-hash the impact (currently
    it doesn't — the re-check only fires when an approval row
    is involved, which requires a typed impact by definition).
    """

    def validate(self) -> None:
        """No-op: NoImpact carries no field constraints."""

    def to_wire_dict(self) -> dict[str, Any]:
        return {"kind": KIND_NONE}


@dataclass
class BusinessImpact:
    """Top-level BusinessImpact union.

    Variants:
        `Money`: flat per-call money amount (cents, USD-centric).
        `ToolCall`: free-form tool-call argument bag matched
            against ToolParameters Approval Rules on the backend.
        `NoImpact`: sentinel for non-impact calls (regular LLM
            chat, tool calls without a typed approval rule).
            Wire shape: ``{"kind":"none"}``. Lets the SDK
            compute an `action_digest` that satisfies the
            Phase-1+ wire-shape version-gate without inventing
            a fake typed impact.

    The SDK validates the variant at construction time so the
    backend never sees malformed output.
    """

    impact: Any  # MoneyImpact | ToolCallParams | NoImpactPayload

    @property
    def kind(self) -> str:
        if isinstance(self.impact, MoneyImpact):
            return KIND_MONEY
        if isinstance(self.impact, ToolCallParams):
            return KIND_TOOL_CALL
        if isinstance(self.impact, NoImpactPayload):
            return KIND_NONE
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

    @classmethod
    def tool_call(
        cls,
        tool_name: str,
        params: dict[str, Any] | None = None,
        extractor_id: str = "nullrun.tool_call.path",
        extractor_version: str = "1",
    ) -> BusinessImpact:
        """Construct a ``kind="tool_call"`` BusinessImpact.

        Used by the ToolParamsExtractor; callers building impacts
        by hand should use this factory rather than constructing
        ``ToolCallParams`` and wrapping themselves -- the factory
        validates before returning so a misuse fails locally
        instead of after a wire round-trip.
        """
        p = ToolCallParams(
            tool_name=tool_name,
            params=params or {},
            extractor_id=extractor_id,
            extractor_version=extractor_version,
        )
        p.validate()
        return cls(impact=p)

    @classmethod
    def no_impact(cls) -> BusinessImpact:
        """Sentinel ``kind="none"`` BusinessImpact for non-impact calls.

        Use this in ``runtime.check_workflow_budget`` and other
        /gate sites that don't extract a typed impact but still
        need to compute an `action_digest` to satisfy the
        backend's Phase-1+ wire-shape version-gate.
        """
        n = NoImpactPayload()
        n.validate()
        return cls(impact=n)


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
