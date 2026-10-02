"""Cross-repo pin for the ``tool_call`` BusinessImpact envelope.

The backend does not trust the ``action_digest`` hex the SDK sends.
At ``/execute`` it RECOMPUTES the digest from the request's
``business_impact`` and compares it to the digest STORED on the
approval row at ``/gate`` time (``payload_binding.rs:163``,
``orchestrator.rs:1511``). So the canonical bytes below are a
contract between two repos, not an implementation detail of either:
an SDK-side canonicalisation difference is invisible in review and
fails CLOSED with ``APPROVAL_DIGEST_MISMATCH`` in production.

The counterparty tests are in
``backend/src/proxy/gate/business_impact.rs`` —
``tool_call_canonical_json_is_the_cross_repo_contract``,
``tool_call_canonical_json_sorts_nested_param_keys``,
``tool_call_canonical_json_handles_empty_params``,
``tool_call_canonical_json_handles_non_ascii_params`` and the
``DIGEST_FIXTURE_HEX_TOOL_CALL`` golden pin. A change to
canonicalisation on either side must change BOTH files in the same
commit, and the protocol version with it.

This file also restores a pin that the 0.18.5 deprecation sweep
(``aee8110``) deleted along with the ``money``/``tool_call``
constructors: ``tests/test_business_impact.py``. That commit left
``business_impact.py``'s module docstring claiming "The digest is
pinned by tests/test_business_impact.py" while the file no longer
existed — the same docstring/code disagreement that made
DEF-TC14-002 look like a backend bug instead of a missing contract.
"""

from __future__ import annotations

import pytest

from nullrun.business_impact import (
    EXTRACTOR_ID,
    EXTRACTOR_VERSION,
    KIND_NONE,
    KIND_TOOL_CALL,
    BusinessImpact,
    NoImpactPayload,
    ToolCallParams,
    canonical_bytes,
    compute_action_digest,
)

# The shared fixture. Mirrored verbatim by the backend's
# `DIGEST_FIXTURE_HEX_TOOL_CALL` and its `tool_call(tool)` helper
# (params `{"region": "EU", "amount": 500}`, tool `stripe.charge`).
GOLDEN_HEX_TOOL_CALL = (
    "9975a8b75a436fb78b9d141b9e0c0a90838c1243d78119b304ae6ed0526966a6"
)

# No backend counterpart: `enum BusinessImpact` has no `none` variant,
# so nothing server-side hashes this. It is pinned anyway because the
# SDK is currently putting this hex on the wire for every LLM check
# (ADR-065 decision step 3 keeps `no_impact()` for that shape), and a
# canonicalisation change would silently invalidate in-flight
# approvals with nothing to compare against.
GOLDEN_HEX_NONE = "0049d93a36f0710269a6deb733ca78d57a770ef640a2698d0fddaa9653b7c3de"


def _canonical(impact: BusinessImpact) -> str:
    """The production canonical form — NOT a copy of it.

    This used to re-serialise the payload inside the test file, which
    meant the `ensure_ascii` mutation left every canonical-bytes test
    green. It now calls `canonical_bytes`, the same function
    `compute_action_digest` hashes, so a canonicalisation change
    cannot hide from these tests.
    """
    return canonical_bytes(impact).decode("utf-8")


class TestCanonicalBytesAreTheCrossRepoContract:
    """Byte layout, not just digest determinism."""

    def test_canonical_json_is_the_cross_repo_contract(self):
        impact = BusinessImpact.tool_call("refund_customer", {"region": "EU", "amount": 500})
        assert _canonical(impact) == (
            '{"extractor_id":"nullrun.tool_call.path","extractor_version":"1",'
            '"kind":"tool_call","params":{"amount":500,"region":"EU"},'
            '"tool_name":"refund_customer"}'
        )

    def test_canonical_json_sorts_nested_param_keys(self):
        # Recursive sort, not top-level only: a params map that
        # serialises in insertion order on one side and sorted on the
        # other is the silent drift the digest exists to catch.
        impact = BusinessImpact.tool_call("t", {"zebra": 1, "alpha": 2})
        canonical = _canonical(impact)
        assert canonical.index("alpha") < canonical.index("zebra")

    def test_canonical_json_handles_empty_params(self):
        # A tool with no arguments is the common case for e.g.
        # `list_invoices`; it must still produce a stable envelope.
        impact = BusinessImpact.tool_call("list_invoices")
        assert _canonical(impact) == (
            '{"extractor_id":"nullrun.tool_call.path","extractor_version":"1",'
            '"kind":"tool_call","params":{},"tool_name":"list_invoices"}'
        )

    def test_canonical_json_handles_non_ascii_params(self):
        # The sharp edge. serde emits raw UTF-8 on the Rust side, so
        # `ensure_ascii=False` is load-bearing here: escaping would
        # change the hashed bytes while still looking correct in
        # review. The backend mirrors this assertion.
        impact = BusinessImpact.tool_call("refund", {"note": "возврат"})
        canonical = _canonical(impact)
        assert "возврат" in canonical
        assert r"\u0432\u043e\u0437\u0432\u0440\u0430\u0442" not in canonical

    def test_extractor_fields_are_inside_the_hashed_bytes(self):
        # `extractor_id` / `extractor_version` have no
        # `skip_serializing_if` on the Rust struct, so they are part
        # of the canonical JSON. Omitting them on either side breaks
        # every digest for this variant.
        canonical = _canonical(BusinessImpact.tool_call("t"))
        assert f'"extractor_id":"{EXTRACTOR_ID}"' in canonical
        assert f'"extractor_version":"{EXTRACTOR_VERSION}"' in canonical


class TestDigestMatchesTheBackendFixture:
    def test_digest_equals_the_backend_golden_hex(self):
        impact = BusinessImpact.tool_call("stripe.charge", {"region": "EU", "amount": 500})
        assert compute_action_digest(impact) == GOLDEN_HEX_TOOL_CALL

    def test_digest_is_deterministic(self):
        impact = BusinessImpact.tool_call("stripe.charge", {"region": "EU", "amount": 500})
        first = compute_action_digest(impact)
        assert first == compute_action_digest(impact)
        assert len(first) == 64

    def test_digest_ignores_params_insertion_order(self):
        a = BusinessImpact.tool_call("stripe.charge", {"region": "EU", "amount": 500})
        b = BusinessImpact.tool_call("stripe.charge", {"amount": 500, "region": "EU"})
        assert compute_action_digest(a) == compute_action_digest(b)

    def test_digest_changes_when_a_param_changes(self):
        # The property NR-010 exists to protect: a replayed grant
        # with a tampered argument bag must not reproduce the
        # approved digest.
        approved = BusinessImpact.tool_call("refund_customer", {"amount": 500})
        tampered = BusinessImpact.tool_call("refund_customer", {"amount": 500_000})
        assert compute_action_digest(approved) != compute_action_digest(tampered)

    def test_digest_changes_when_the_tool_name_changes(self):
        # Swapping the tool while keeping every argument identical
        # must also break the binding.
        approved = BusinessImpact.tool_call("refund_customer", {"amount": 500})
        swapped = BusinessImpact.tool_call("charge_card", {"amount": 500})
        assert compute_action_digest(approved) != compute_action_digest(swapped)

    def test_digest_changes_when_a_nested_param_changes(self):
        approved = BusinessImpact.tool_call("t", {"order": {"total": 1}})
        tampered = BusinessImpact.tool_call("t", {"order": {"total": 2}})
        assert compute_action_digest(approved) != compute_action_digest(tampered)

    def test_tool_call_and_none_never_collide(self):
        # The exact hazard ADR-065 exists to remove: every tool call
        # used to hash the `none` sentinel, so a grant bound to one
        # tool was replayable as any other.
        assert compute_action_digest(BusinessImpact.tool_call("t")) != compute_action_digest(
            BusinessImpact.no_impact()
        )


class TestNoneSentinelPin:
    def test_none_digest_is_pinned(self):
        assert compute_action_digest(BusinessImpact.no_impact()) == GOLDEN_HEX_NONE

    def test_none_kind_and_payload(self):
        impact = BusinessImpact.no_impact()
        assert impact.kind == KIND_NONE
        assert isinstance(impact.impact, NoImpactPayload)
        assert _canonical(impact) == '{"kind":"none"}'


class TestKindDiscrimination:
    def test_kind_is_tool_call(self):
        assert BusinessImpact.tool_call("t").kind == KIND_TOOL_CALL

    def test_unknown_payload_type_raises(self):
        # Never return null / never fail silently.
        with pytest.raises(TypeError):
            BusinessImpact(impact=object()).kind  # type: ignore[arg-type]


class TestValidatorMirrorsTheBackend:
    """``ToolCallParams::validate`` (business_impact.rs:308-351)."""

    def test_rejects_empty_tool_name(self):
        with pytest.raises(ValueError, match="non-empty"):
            BusinessImpact.tool_call("")

    def test_rejects_overlong_tool_name(self):
        with pytest.raises(ValueError, match="exceeds max 128"):
            BusinessImpact.tool_call("a" * 129)

    def test_accepts_tool_name_at_the_cap(self):
        assert BusinessImpact.tool_call("a" * 128).kind == KIND_TOOL_CALL

    def test_rejects_non_ascii_tool_name(self):
        with pytest.raises(ValueError, match="printable ASCII"):
            BusinessImpact.tool_call("refund_ü")

    def test_rejects_control_characters_in_tool_name(self):
        with pytest.raises(ValueError, match="printable ASCII"):
            BusinessImpact.tool_call("refund\n")

    def test_rejects_overlong_param_name(self):
        with pytest.raises(ValueError, match="exceeds max 64"):
            BusinessImpact.tool_call("t", {"k" * 65: 1})

    def test_rejects_float_param_value(self):
        # serde_json parses 1.0 as f64 and Python as float; the two
        # would serialise the same logical value differently.
        with pytest.raises(ValueError, match="unsupported value kind"):
            BusinessImpact.tool_call("t", {"amount": 1.0})

    def test_rejects_nested_float_param_value(self):
        # The backend recurses into arrays and objects
        # (`check_value_kind`, business_impact.rs:365-392).
        with pytest.raises(ValueError, match="unsupported value kind"):
            BusinessImpact.tool_call("t", {"items": [1, 2.5]})
        with pytest.raises(ValueError, match="unsupported value kind"):
            BusinessImpact.tool_call("t", {"order": {"total": 1.5}})

    @pytest.mark.parametrize(
        "value",
        [None, True, False, "s", 0, -1, 2**63, [], {}, [1, "a", None], {"k": [1, 2]}],
    )
    def test_accepts_round_trippable_values(self, value):
        assert BusinessImpact.tool_call("t", {"v": value}).kind == KIND_TOOL_CALL

    def test_bool_is_not_treated_as_a_float(self):
        # bool subclasses int in Python; a naive numeric check would
        # either accept it by accident or reject it, and the backend
        # has a distinct `Value::Bool` arm.
        assert BusinessImpact.tool_call("t", {"v": True}).kind == KIND_TOOL_CALL

    def test_rejects_non_serialisable_value(self):
        with pytest.raises(ValueError, match="not JSON-serialisable"):
            BusinessImpact.tool_call("t", {"v": object()})

    def test_validate_is_idempotent(self):
        params = ToolCallParams("t", {"a": 1})
        params.validate()
        params.validate()


class TestConstructionIsIsolated:
    def test_caller_params_are_copied(self):
        # The envelope is carried on the call context and hashed at
        # /gate; a caller mutating its own dict afterwards must not
        # retroactively change the bytes the approval was bound to.
        params = {"amount": 500}
        impact = BusinessImpact.tool_call("refund_customer", params)
        before = compute_action_digest(impact)
        params["amount"] = 500_000
        assert compute_action_digest(impact) == before
