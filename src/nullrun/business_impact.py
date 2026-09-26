"""
BusinessImpact + action_digest — minimal wire helpers.

Every ``@protect`` call computes a single canonical ``NoImpact``
envelope and forwards it to /execute. The backend's ToolParameters
Approval Rules read values out of ``tool_kwargs`` directly via
the rule's ``param_name`` field, so the SDK emits a single
canonical envelope rather than per-tool typed impacts. This
module exists so the gate can send a valid (kind, action_digest)
pair.

Field contract mirrored by the backend at
``backend/src/proxy/gate/business_impact.rs``:

  - ``business_impact`` disciminator: ``{"kind": "none"}``
    (the only wire variant the SDK ships post-0.18.2).
  - ``action_digest``: SHA-256 over ``DIGEST_PREFIX + compact
    canonical JSON of the impact envelope``, lowercase hex.

The digest is pinned by ``tests/test_business_impact.py`` so the
SDK ↔ backend canonicalisation can't drift silently.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

DIGEST_PREFIX = b"nullrun/v1/business_impact:"

# ToolCall envelopes) but the SDK only mints NoImpact.
KIND_NONE = "none"


@dataclass
class NoImpactPayload:
    """Sentinel payload for non-impact calls. The ONLY variant the

    The canonical JSON of this payload is ``{"kind":"none"}``.
    The corresponding digest is the SHA-256 of
    ``nullrun/v1/business_impact:{"kind":"none"}`` and is pinned
    by tests as a literal hex so SDK ↔ backend canonicalisation
    can't drift silently.
    """

    def validate(self) -> None:
        """No-op: NoImpact carries no field constraints."""

    def to_wire_dict(self) -> dict[str, Any]:
        return {"kind": KIND_NONE}


@dataclass
class BusinessImpact:
    """Top-level BusinessImpact envelope.

    Only the ``NoImpact`` payload variant is constructed on the
    SDK side. The ``money`` and ``tool_call`` factories are not
    part of the SDK surface — every call sends NoImpact and the
    backend reads live values out of ``tool_kwargs`` via the
    rule's ``param_name``.
    """

    impact: NoImpactPayload  # Only NoImpactPayload in 0.18.2.

    @property
    def kind(self) -> str:
        if isinstance(self.impact, NoImpactPayload):
            return KIND_NONE
        raise TypeError(
            f"unknown impact type: {type(self.impact)!r} — only "
        )

    def validate(self) -> None:
        self.impact.validate()

    def to_wire_dict(self) -> dict[str, Any]:
        return self.impact.to_wire_dict()

    @classmethod
    def no_impact(cls) -> BusinessImpact:
        """Construct the canonical ``kind="none"`` envelope."""
        n = NoImpactPayload()
        n.validate()
        return cls(impact=n)


def _canonicalize_json(value: Any) -> Any:
    """Sort object keys recursively before serialization.

    Mirrors ``BusinessImpact::canonical_json()`` in the backend.
    """
    if isinstance(value, dict):
        items = [(k, _canonicalize_json(v)) for k, v in value.items()]
        items.sort(key=lambda kv: kv[0])
        return {k: v for k, v in items}
    if isinstance(value, list):
        return [_canonicalize_json(v) for v in value]
    return value


def compute_action_digest(impact: BusinessImpact) -> str:
    """Compute the SHA-256 digest the backend expects.

    Algorithm (must match ``backend/src/proxy/gate/business_impact.rs``
    byte-for-byte):

      1. Validate the impact (``NoImpactPayload`` is the only
         post-0.18.2 variant — fail-fast on bad input).
      2. Convert to wire dict (``{"kind":"none"}``).
      3. Canonicalize (sort object keys recursively).
      4. Serialize to compact JSON (no spaces).
      6. Return lowercase hex (64 chars).
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


__all__ = [
    "DIGEST_PREFIX",
    "KIND_NONE",
    "NoImpactPayload",
    "BusinessImpact",
    "compute_action_digest",
]
