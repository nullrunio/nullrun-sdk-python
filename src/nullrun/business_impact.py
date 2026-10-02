"""
BusinessImpact + action_digest — minimal wire helpers.

``@protect`` builds one ``BusinessImpact`` per logical action and
forwards it to /execute with the matching ``action_digest``. The
backend recomputes that digest server-side from the request body
and compares it to the digest STORED on the approval row at /gate
time (``payload_binding.rs:163``, ``orchestrator.rs:1511``), so the
canonical bytes below are a cross-repo contract, not an
implementation detail.

Field contract mirrored by the backend at
``backend/src/proxy/gate/business_impact.rs``:

  - ``business_impact`` discriminator: ``{"kind": "none"}`` for a
    call with no tool impact, ``{"kind": "tool_call", ...}`` for a
    tool invocation.
  - ``action_digest``: SHA-256 over ``DIGEST_PREFIX + compact
    canonical JSON of the impact envelope``, lowercase hex.

The digests are pinned by ``tests/test_business_impact.py`` and
``tests/test_business_impact_tool_call.py`` so the SDK ↔ backend
canonicalisation can't drift silently. That pin is the only thing
that noticed the drift behind DEF-TC14-002, so it stays.

ADR-065: the ``tool_call`` variant was removed in 0.18.5 and the
SDK emitted a constant ``{"kind": "none"}`` sentinel for every call.
A constant hashes to a constant, so the stored and recomputed
digests always agreed — while binding the approval to nothing at
all, which is exactly the weakness NR-010 was raised to close. See
``docs/adr/ADR-065-business-impact-reentry-unreachable.md``.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

DIGEST_PREFIX = b"nullrun/v1/business_impact:"

KIND_NONE = "none"
KIND_TOOL_CALL = "tool_call"

# Mirrors ``ToolCallParams::new`` (backend business_impact.rs:297-307).
# Both fields lack ``skip_serializing_if`` on the Rust side, so they
# ARE inside the hashed bytes — omitting them on this side makes
# every digest mismatch.
EXTRACTOR_ID = "nullrun.tool_call.path"
EXTRACTOR_VERSION = "1"

# Mirrors the backend's caps (business_impact.rs:792 and
# ``ToolCallParams::validate``). The SDK validates locally so a bad
# envelope fails at construction, before it can be stored on an
# approval row the server will then refuse to match.
MAX_TOOL_NAME_BYTES = 128
MAX_PARAM_NAME_BYTES = 64


@dataclass
class NoImpactPayload:
    """Sentinel payload for a call with no business impact.

    The canonical JSON of this payload is ``{"kind":"none"}``.
    The corresponding digest is the SHA-256 of
    ``nullrun/v1/business_impact:{"kind":"none"}`` and is pinned
    by tests as a literal hex so SDK ↔ backend canonicalisation
    can't drift silently.

    Legitimate for an LLM check (``check_workflow_budget`` on a model
    with no tool to name). NOT legitimate for a tool call: a tool
    that names nothing binds its approval to nothing.
    """

    def validate(self) -> None:
        """No-op: NoImpact carries no field constraints."""

    def to_wire_dict(self) -> dict[str, Any]:
        return {"kind": KIND_NONE}


@dataclass
class ToolCallParams:
    """Argument bag for a tool invocation.

    Mirrors ``ToolCallParams`` in the backend. ``params`` is
    operator-defined and carries no per-key schema; the validator
    only rejects what the digest layer could not round-trip
    byte-identically across the two implementations.
    """

    tool_name: str
    params: dict[str, Any] = field(default_factory=dict)
    extractor_id: str = EXTRACTOR_ID
    extractor_version: str = EXTRACTOR_VERSION

    def validate(self) -> None:
        if not self.tool_name:
            raise ValueError("ToolCallParams.tool_name must be non-empty")
        name_bytes = self.tool_name.encode("utf-8")
        if len(name_bytes) > MAX_TOOL_NAME_BYTES:
            raise ValueError(
                f"ToolCallParams.tool_name length {len(name_bytes)} exceeds "
                f"max {MAX_TOOL_NAME_BYTES}"
            )
        # Rust checks `.bytes().all(|b| b.is_ascii() && !b.is_ascii_control())`,
        # which is why this is a byte test and not `str.isprintable()`.
        if not all(0x20 <= b < 0x7F for b in name_bytes):
            raise ValueError("ToolCallParams.tool_name must be printable ASCII")
        for key, value in self.params.items():
            key_bytes = key.encode("utf-8")
            if len(key_bytes) > MAX_PARAM_NAME_BYTES:
                raise ValueError(
                    f"ToolCallParams.params[{key!r}] key length {len(key_bytes)} "
                    f"exceeds max {MAX_PARAM_NAME_BYTES}"
                )
            _reject_unsupported_value(key, value)

    def to_wire_dict(self) -> dict[str, Any]:
        return {
            "kind": KIND_TOOL_CALL,
            "tool_name": self.tool_name,
            "params": self.params,
            "extractor_id": self.extractor_id,
            "extractor_version": self.extractor_version,
        }


def _reject_unsupported_value(key: str, value: Any) -> None:
    """Mirror the backend's ``check_value_kind`` (business_impact.rs:365-392).

    serde_json parses ``1.0`` as f64 and Python parses it as float, so
    the two would serialise the same logical value differently and
    every digest would mismatch. ``bool`` is a subclass of ``int`` in
    Python, so it has to be tested first — the backend's
    ``Value::Bool`` arm is likewise distinct from ``Value::Number``.
    """
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        raise ValueError(
            f"ToolCallParams.params[{key!r}] has an unsupported value kind "
            f"(f64 or non-finite number); use string-encoded values or a "
            f"new extractor to round-trip the digest"
        )
    if isinstance(value, list):
        for item in value:
            _reject_unsupported_value(key, item)
        return
    if isinstance(value, dict):
        for nested_key, nested in value.items():
            nested_bytes = nested_key.encode("utf-8")
            if len(nested_bytes) > MAX_PARAM_NAME_BYTES:
                raise ValueError(
                    f"ToolCallParams.params[{key!r}][{nested_key!r}] key length "
                    f"{len(nested_bytes)} exceeds max {MAX_PARAM_NAME_BYTES}"
                )
            _reject_unsupported_value(key, nested)
        return
    raise ValueError(
        f"ToolCallParams.params[{key!r}] has an unsupported value kind "
        f"({type(value).__name__}); it is not JSON-serialisable"
    )


@dataclass
class BusinessImpact:
    """Top-level BusinessImpact envelope."""

    impact: NoImpactPayload | ToolCallParams

    @property
    def kind(self) -> str:
        if isinstance(self.impact, NoImpactPayload):
            return KIND_NONE
        if isinstance(self.impact, ToolCallParams):
            return KIND_TOOL_CALL
        raise TypeError(f"unknown impact type: {type(self.impact)!r}")

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

    @classmethod
    def tool_call(
        cls, tool_name: str, params: dict[str, Any] | None = None
    ) -> BusinessImpact:
        """Construct a ``kind="tool_call"`` envelope for a tool call.

        One envelope per LOGICAL ACTION, built once and carried on
        the call context — not rebuilt per HTTP call. ``/gate`` and
        ``/execute`` must hash the same bytes or the server's
        recompute will not match the digest stored at /gate time
        (ADR-065, decision step 2).
        """
        t = ToolCallParams(tool_name=tool_name, params=dict(params or {}))
        t.validate()
        return cls(impact=t)


def _canonicalize_json(value: Any) -> Any:
    """Sort object keys recursively before serialization.

    Mirrors ``canonicalize_json`` in the backend
    (``business_impact.rs:411-439``).
    """
    if isinstance(value, dict):
        items = [(k, _canonicalize_json(v)) for k, v in value.items()]
        items.sort(key=lambda kv: kv[0])
        return {k: v for k, v in items}
    if isinstance(value, list):
        return [_canonicalize_json(v) for v in value]
    return value


def canonical_bytes(impact: BusinessImpact) -> bytes:
    """The exact bytes :func:`compute_action_digest` hashes.

    Split out so tests can assert the canonical form itself rather
    than a hex digest. A digest is opaque: it tells you that two
    payloads disagree, never *how*. Asserting on a reimplementation
    of the serializer inside the test file instead is worse than
    useless — a mutation of the production ``ensure_ascii`` flag
    still leaves that copy green, which is precisely how the
    non-ASCII regression this function exists to prevent would ship.

    Mirrors ``BusinessImpact::canonical_json()`` in the backend:

      1. Validate the impact (fail-fast on bad input).
      2. Convert to wire dict.
      3. Canonicalize (sort object keys recursively).
      4. Serialize to compact JSON with ``ensure_ascii=False`` — the
         backend emits raw UTF-8, so escaping non-ASCII here would
         change the hashed bytes.
    """
    impact.validate()
    canonical_value = _canonicalize_json(impact.to_wire_dict())
    return json.dumps(
        canonical_value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=False,
    ).encode("utf-8")


def compute_action_digest(impact: BusinessImpact) -> str:
    """Compute the SHA-256 digest the backend expects.

    Algorithm (must match ``backend/src/proxy/gate/business_impact.rs``
    byte-for-byte): SHA-256 over ``DIGEST_PREFIX + canonical_bytes``.
    Returns lowercase hex (64 chars).
    """
    hasher = hashlib.sha256()
    hasher.update(DIGEST_PREFIX)
    hasher.update(canonical_bytes(impact))
    return hasher.hexdigest()


__all__ = [
    "DIGEST_PREFIX",
    "KIND_NONE",
    "KIND_TOOL_CALL",
    "EXTRACTOR_ID",
    "EXTRACTOR_VERSION",
    "NoImpactPayload",
    "ToolCallParams",
    "BusinessImpact",
    "canonical_bytes",
    "compute_action_digest",
]
