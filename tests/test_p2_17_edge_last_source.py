"""P2-17: `last_source` must tell a box ALLOW from a box REFUSAL.

The bug this pins: `EdgeTransport.last_source` was set to the single
constant `EDGE_LEASE` on BOTH the allow path and the refusal path, so
the two collapsed into one string. A caller reading the attribute could
not tell which way the box went — the attribute said "the lease spoke"
when the truth was "the lease said no".

Direction of the failure: this is a *reporting* defect, not a money
one. The decision itself was never wrong — the refusal path returned
`decision: "block"` with the box's own `error_code`. What was wrong is
the provenance the SDK reported about it. So the assertions below are
about the SHAPE of the report, and every one of them also asserts the
decision still goes the way it went before.

The wire value is deliberately NOT changed. `decision_source` on the
refusal path stays `EDGE_LEASE`: the lease was the authority that
spoke either way, the direction is carried by `decision`, and the
runtime validates `decision_source` against a known set — a source it
does not know is a malformed response, not a refusal. That is why the
new constant is a `last_source` marker only and never reaches the
wire.

The fake box is the same shape `tests/test_edge_via_edge.py` uses: the
transport's `_post` is replaced by a responder, so the real
`enforce` → `_ask` → decision-building path runs end to end.
"""

from __future__ import annotations

import nullrun.edge as edge
from nullrun.edge import (
    EDGE_LEASE,
    EDGE_NOT_ENFORCED,
    EDGE_UNREACHABLE,
    EdgeTransport,
)
from nullrun.transport import is_fallback_decision_source


class _FakeResponse:
    """What a box answers with. Same shape as the other edge tests."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _transport(responder):
    t = EdgeTransport(
        base_url="http://127.0.0.1:1",
        lease_id="0195f0c0-0000-7000-8000-000000000001",
        box_token="box-token",
        api_key="nr_org_key",
        timeout=0.05,
        max_retries=0,
    )
    t._post = responder  # type: ignore[method-assign]
    return t


def _allowing_box(body):
    return _FakeResponse(
        {
            "allowed": True,
            "lease_id": body["lease_id"],
            "granted_millicents": 500_000,
            "spent_millicents": 10,
            "remaining_millicents": 499_990,
        }
    )


def _refusing_box(body):
    return _FakeResponse({"allowed": False, "error_code": "EDGE_LEASE_EXHAUSTED"})


# ── the collapse itself ───────────────────────────────────────────────


def test_the_allow_path_and_the_refusal_path_do_not_collapse_into_one_source():
    """The defect, stated as a fact about the two paths.

    Both paths are driven for real — a box that says yes and a box that
    says no — and the assertion is that the attribute a caller reads
    afterwards tells them apart. Pre-fix both read `edge_lease`.
    """
    allow_t = _transport(_allowing_box)
    allow_result = allow_t.enforce("gpt-4o", 0, 10)

    refuse_t = _transport(_refusing_box)
    refuse_result = refuse_t.enforce("gpt-4o", 0, 10)

    # The decisions were never wrong — assert them first, so a test that
    # only ever checks provenance cannot be satisfied by an
    # implementation that stops deciding.
    assert allow_result["decision"] == "allow"
    assert refuse_result["decision"] == "block"

    assert allow_t.last_source != refuse_t.last_source, (
        "last_source must distinguish a box ALLOW from a box REFUSAL; "
        f"both paths read {allow_t.last_source!r}"
    )


def test_the_refusal_source_is_its_own_named_constant():
    """Not merely "different" — named, and the same on both sides.

    A caller cannot act on a value it has no name for, so the refusal
    source is a constant the SDK exports, and the refusal path reports
    exactly it.
    """
    refused = getattr(edge, "EDGE_LEASE_REFUSED", None)
    assert refused is not None, (
        "nullrun.edge must export EDGE_LEASE_REFUSED — the source that "
        "says the lease REFUSED, as distinct from EDGE_LEASE"
    )
    assert refused != EDGE_LEASE

    t = _transport(_refusing_box)
    t.enforce("gpt-4o", 0, 10)
    assert t.last_source == refused


def test_every_edge_source_is_distinct_from_every_other():
    """Four outcomes, four strings. A collapse anywhere is a collapse."""
    refused = getattr(edge, "EDGE_LEASE_REFUSED", None)
    assert refused is not None, "nullrun.edge must export EDGE_LEASE_REFUSED"

    sources = {EDGE_LEASE, refused, EDGE_UNREACHABLE, EDGE_NOT_ENFORCED}
    assert len(sources) == 4


# ── what the fix must NOT change ──────────────────────────────────────


def test_the_refusal_still_carries_the_lease_as_its_wire_decision_source():
    """The wire value is untouched, and that is deliberate.

    `decision_source` says WHO decided. On a refusal the lease was the
    authority that spoke — the box answered, with "no" — so the source
    is still `EDGE_LEASE`, and `decision` says which way it went.

    Changing it would need a new entry in the runtime's known-source
    set; until the runtime knows the name, a body carrying it is a
    MALFORMED response, which turns a clean refusal into a crash.
    """
    t = _transport(_refusing_box)
    result = t.enforce("gpt-4o", 0, 10)

    assert result["decision"] == "block"
    assert result["decision_source"] == EDGE_LEASE
    assert result["error_code"] == "EDGE_LEASE_EXHAUSTED"
    assert "grant" in result["explanation"].lower()


def test_the_refusal_still_flows_through_the_runtime_unchanged():
    """The refusal is still one the runtime parses and honours.

    `_require_gate_decision` runs before the decision is read and
    rejects any body whose `decision_source` it does not know. This
    asserts the refusal survives that check as a `block` — which is
    the whole reason the wire value was left alone.
    """
    from nullrun.runtime import NullRunRuntime

    rt = NullRunRuntime.__new__(NullRunRuntime)  # no I/O, no client
    t = _transport(_refusing_box)
    result = t.enforce("gpt-4o", 0, 10)

    assert rt._require_gate_decision(result) == "block"


def test_the_refusal_source_is_not_read_as_a_synthetic_answer():
    """A box that said no is a verdict, not a degraded transport.

    `is_fallback_decision_source` is the runtime's fail-OPEN predicate:
    a source that reads as synthetic means "the engine never answered",
    and the refusal would be discarded before the caller saw it. The new
    marker must not be mistaken for one.
    """
    refused = edge.EDGE_LEASE_REFUSED
    assert is_fallback_decision_source(refused) is False


# ── the neighbouring sources did not move ──────────────────────────────


def test_the_unreachable_box_still_reports_the_unreachable_source():
    """The failure paths keep their own markers."""

    def boom(body):
        raise OSError("connection refused")

    t = _transport(boom)
    result = t.enforce("gpt-4o", 0, 10)

    assert result["decision"] == "block"
    assert result["decision_source"] == EDGE_UNREACHABLE
    assert t.last_source == EDGE_UNREACHABLE


def test_the_never_asked_box_still_reports_the_not_enforced_source():
    t = _transport(_allowing_box)
    result = t.not_enforced()

    assert result["decision"] == "block"
    assert result["decision_source"] == EDGE_NOT_ENFORCED
    assert t.last_source == EDGE_NOT_ENFORCED


def test_the_new_constant_is_exported_like_its_siblings():
    """Named in `__all__`, so a caller can import it by name."""
    assert "EDGE_LEASE_REFUSED" in edge.__all__
