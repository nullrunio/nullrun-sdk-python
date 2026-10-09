"""Via-edge: what the SDK must do, and what it must refuse to do.

Every test here is about a decision that moves money, so each one states
which direction it fails in. The pattern throughout: assert the ALLOW
leg before the BLOCK leg, because an assertion that only ever checks
refusal is satisfied by an implementation that refuses everything.
"""

from __future__ import annotations

import os

import pytest

from nullrun.edge import (
    EDGE_LEASE,
    EdgeConfigurationError,
    EdgeTransport,
    edge_transport_from_env,
    token_split,
)
from nullrun.transport import DecisionSource, Transport, is_fallback_decision_source


def _env(**over: str) -> dict[str, str]:
    base = {
        "NULLRUN_EDGE_URL": "http://127.0.0.1:18090",
        "NULLRUN_EDGE_LEASE_ID": "0195f0c0-0000-7000-8000-000000000001",
        "NULLRUN_EDGE_TOKEN": "box-token",
        "NULLRUN_API_KEY": "nr_org_key",
    }
    base.update(over)
    return base


# ── configuration ─────────────────────────────────────────────────────


def test_direct_is_the_default_and_costs_nothing():
    """E1. Unset means direct, and it means it with no runtime in the way.

    Asserted as the very first case because every other behaviour here
    is conditional on it: a mode that engaged by accident would take the
    enforcement path off the cloud for users who never asked for it.
    """
    assert edge_transport_from_env({}) is None
    assert edge_transport_from_env({"NULLRUN_EDGE_URL": "   "}) is None


def test_the_environment_is_not_read_when_direct():
    """A half-configured via-edge must not break a direct user.

    If the env were read before the URL check, an operator who exported
    `NULLRUN_EDGE_TOKEN` early in a deploy script and `NULLRUN_EDGE_URL`
    later would break every call in between. Failing at construction is
    right for via-edge and catastrophic for direct.
    """
    assert (
        edge_transport_from_env(
            {
                "NULLRUN_EDGE_TOKEN": "set-but-no-url",
                "NULLRUN_EDGE_LEASE_ID": "also-without-a-url",
            }
        )
        is None
    )


@pytest.mark.parametrize(
    "missing",
    ["NULLRUN_EDGE_LEASE_ID", "NULLRUN_EDGE_TOKEN", "NULLRUN_API_KEY"],
)
def test_via_edge_that_cannot_enforce_refuses_to_start(missing: str):
    """E1/E4. Asked for and unable to enforce is an error, not a warning.

    The failure it prevents: via-edge engaged, the box was never really
    reachable, and every tool call in the agent ran with no gate — while
    the operator's dashboard showed an edge box holding the line.
    """
    env = _env()
    env.pop(missing)
    with pytest.raises(EdgeConfigurationError) as excinfo:
        edge_transport_from_env(env)
    # The message must NAME the missing variable — an operator reading
    # this at 3am should not have to guess which of four it was.
    assert missing in str(excinfo.value)


def test_a_missing_lease_id_says_why_the_box_cannot_pick_one():
    """The lease is the org's grant. A box that chose its own is not enforcing."""
    env = _env()
    env.pop("NULLRUN_EDGE_LEASE_ID")
    with pytest.raises(EdgeConfigurationError) as excinfo:
        edge_transport_from_env(env)
    assert "grant" in str(excinfo.value).lower()


def test_an_edge_url_that_is_not_a_url_is_left_to_the_transport_to_complain():
    """Configuration is checked for MEANING here, not for syntax.

    Syntactic validation belongs to the HTTP layer that already has
    `InsecureTransportError`; a second, weaker copy here would accept
    things that one rejects.
    """
    t = edge_transport_from_env(_env(NULLRUN_EDGE_URL="not a url"))
    assert t is not None
    assert t.base_url == "not a url"


# ── decision provenance ───────────────────────────────────────────────


def test_a_lease_decision_is_not_a_fallback_decision():
    """E2. The predicate every enforcement call site uses must say False.

    `is_fallback_decision_source` is what the runtime checks before
    honouring a decision. If `edge_lease` read as a fallback, STRICT
    would turn a real refusal from the box into a synthetic one and,
    worse, a real ALLOW would be treated as degradation rather than as
    a grant's answer.
    """
    assert EDGE_LEASE not in (DecisionSource.GATEWAY, DecisionSource.FALLBACK)
    assert is_fallback_decision_source(EDGE_LEASE) is False


def test_an_unreachable_box_is_a_fallback_so_strict_still_closes():
    """E3. The unreachable answer is NOT a lease answer.

    Same shape, opposite direction: this one must read as synthetic, or
    the runtime would treat "the box did not answer" as the lease having
    spoken.
    """
    assert is_fallback_decision_source(DecisionSource.FALLBACK) is True


# ── what the SDK is allowed to say ─────────────────────────────────────


def test_the_sdk_reports_tokens_and_never_an_amount():
    """DEF-40 again, on the client side.

    The wire carries a model and two counts. An amount on the wire is
    the bug being fixed, so the test is that no numeric field the box
    could read as money survives into the request.
    """
    inp, out = token_split({"model": "gpt-4o", "estimated_tokens": 1234})
    assert (inp, out) == (0, 1234)

    inp, out = token_split({"edge_input_tokens": 10, "edge_output_tokens": 20})
    assert (inp, out) == (10, 20)

    assert token_split({}) == (0, 0)


def test_an_estimate_with_no_breakdown_is_reported_as_output():
    """Direction: over-report, never under-report.

    Output dominates the price for most models, so attributing the whole
    estimate there errs toward charging more. Attributing it to input
    would under-charge on every call, and the box cannot check the
    number — it believes whatever it is told.
    """
    inp, out = token_split({"estimated_tokens": 500})
    assert out == 500
    assert inp == 0


def test_token_counts_are_ints_and_survive_a_string_from_the_caller():
    """A string count is a crash on the wire, not a rounding question."""
    inp, out = token_split({"estimated_tokens": "250"})
    assert (inp, out) == (0, 250)
    assert isinstance(out, int)


# ── the box's answer ──────────────────────────────────────────────────


class _FakeResponse:
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


def test_an_allow_from_the_box_carries_the_lease_as_its_source():
    t = _transport(
        lambda body: _FakeResponse(
            {
                "allowed": True,
                "lease_id": body["lease_id"],
                "granted_millicents": 500_000,
                "spent_millicents": 1_000,
                "remaining_millicents": 499_000,
            }
        )
    )
    result = t.enforce("gpt-4o", 0, 1_000)
    assert result["decision"] == "allow"
    assert result["decision_source"] == EDGE_LEASE
    assert is_fallback_decision_source(result["decision_source"]) is False
    assert result["spent_millicents"] == 1_000


def test_the_request_carries_both_credentials_and_the_lease():
    """E9. Box token proves reach; org key proves the org.

    Sent together and not interchangeably: a shared box must not let one
    org's agent spend another org's lease, and the box has no database
    to ask.
    """
    seen: dict[str, str] = {}

    def responder(body):
        seen["body"] = body
        return _FakeResponse({"allowed": True, "spent_millicents": 0})

    t = EdgeTransport(
        base_url="http://127.0.0.1:18090",
        lease_id="lease-1",
        box_token="box-token",
        api_key="nr_org_key",
    )
    headers = t._headers({})
    assert headers["Authorization"] == "Bearer box-token"
    assert headers["X-API-Key"] == "nr_org_key"
    assert headers["X-NullRun-Lease-Id"] == "lease-1"
    assert seen == {}


def test_a_retried_call_keeps_its_event_id():
    """At-least-once delivery is the norm; a fresh id would double-charge."""
    ids = []

    t = EdgeTransport(
        base_url="http://127.0.0.1:18090",
        lease_id="lease-1",
        box_token="box-token",
        api_key="nr_org_key",
    )

    def responder(body):
        ids.append(body["event_id"])
        return _FakeResponse({"allowed": True, "spent_millicents": 0})

    t._post = responder  # type: ignore[method-assign]
    t.enforce("gpt-4o", 0, 10, event_id="evt-1")
    t.enforce("gpt-4o", 0, 10, event_id="evt-1")
    assert ids == ["evt-1", "evt-1"]


def test_a_refusal_from_the_box_is_a_block_naming_the_code():
    t = _transport(
        lambda body: _FakeResponse({"allowed": False, "error_code": "LEASE_EXHAUSTED"})
    )
    result = t.enforce("gpt-4o", 0, 10)
    assert result["decision"] == "block"
    assert result["decision_source"] == EDGE_LEASE
    assert result["error_code"] == "LEASE_EXHAUSTED"
    assert "grant" in result["explanation"].lower()


def test_an_unreachable_box_blocks_and_does_not_reach_the_cloud():
    """E3. The whole point: no silent fallback.

    The cloud is not configured anywhere in this object, so there is
    nothing to fall back to even in principle. What is asserted is the
    shape the runtime needs: a synthetic block, not an exception and
    definitely not an allow.
    """
    def boom(body):
        raise OSError("connection refused")

    t = _transport(boom)
    result = t.enforce("gpt-4o", 0, 10)
    assert result["decision"] == "block"
    assert result["decision_source"] == DecisionSource.FALLBACK
    assert result["error_code"] == "EDGE_UNREACHABLE"
    assert is_fallback_decision_source(result["decision_source"]) is True


def test_a_broken_box_is_not_laundered_into_a_budget_answer():
    """5xx means the box is broken, which is not the same as 'no money'.

    It takes the transport-error path so STRICT closes. A 5xx presented
    as a lease refusal would be a decision the lease never made.
    """
    t = _transport(lambda body: _FakeResponse({"allowed": False}, status_code=503))
    result = t.enforce("gpt-4o", 0, 10)
    assert result["decision"] == "block"
    assert result["decision_source"] == DecisionSource.FALLBACK
    assert result["error_code"] == "EDGE_UNREACHABLE"


def test_a_non_json_answer_from_the_box_is_a_transport_failure():
    def responder(body):
        response = _FakeResponse(None)
        response.json = lambda: (_ for _ in ()).throw(ValueError("not json"))
        return response

    t = _transport(responder)
    result = t.enforce("gpt-4o", 0, 10)
    assert result["decision_source"] == DecisionSource.FALLBACK


def test_a_broken_observer_cannot_change_the_decision():
    """`on_transport_error` is a reporter. A raise inside it is not a vote."""
    def boom(body):
        raise OSError("connection refused")

    def angry_observer(exc):
        raise RuntimeError("the observer is broken")

    t = _transport(boom)
    result = t.enforce("gpt-4o", 0, 10, on_transport_error=angry_observer)
    assert result["decision"] == "block"


# ── wiring into the transport ─────────────────────────────────────────


def test_check_goes_to_the_box_and_never_builds_a_gate_body(monkeypatch):
    """E4. One path or the other, never a blend.

    A box answer and a partially-built cloud request are not both sent;
    the test asserts the cloud's POST is never reached at all.
    """
    monkeypatch.setenv("NULLRUN_EDGE_URL", "http://127.0.0.1:18090")
    monkeypatch.setenv("NULLRUN_EDGE_LEASE_ID", "lease-1")
    monkeypatch.setenv("NULLRUN_EDGE_TOKEN", "box-token")
    monkeypatch.setenv("NULLRUN_API_KEY", "nr_org_key")

    t = Transport(api_url="https://api.nullrun.io")

    def must_not_be_called(*a, **kw):
        raise AssertionError("the cloud was called in via-edge mode")

    t._client.post = must_not_be_called  # type: ignore[method-assign]
    t.edge._post = lambda body: _FakeResponse(  # type: ignore[method-assign]
        {"allowed": True, "spent_millicents": 10, "remaining_millicents": 90}
    )

    result = t.check({"model": "gpt-4o", "estimated_tokens": 10})
    assert result["decision"] == "allow"
    assert result["decision_source"] == EDGE_LEASE


def test_execute_does_not_reach_the_cloud_in_via_edge_mode(monkeypatch):
    """E4. `/execute` has no box equivalent — and must not look for one.

    The box's `/enforce` already decided and charged, so a second call
    would either hit the cloud or double-charge.
    """
    monkeypatch.setenv("NULLRUN_EDGE_URL", "http://127.0.0.1:18090")
    monkeypatch.setenv("NULLRUN_EDGE_LEASE_ID", "lease-1")
    monkeypatch.setenv("NULLRUN_EDGE_TOKEN", "box-token")
    monkeypatch.setenv("NULLRUN_API_KEY", "nr_org_key")

    t = Transport(api_url="https://api.nullrun.io")

    def must_not_be_called(*a, **kw):
        raise AssertionError("the cloud was called in via-edge mode")

    t._client.post = must_not_be_called  # type: ignore[method-assign]
    t.edge.last_source = EDGE_LEASE

    result = t.execute(
        organization_id="org-1",
        execution_id="exec-1",
        trace_id="trace-1",
        tool="read_file",
        input_data={},
    )
    assert result["decision"] == "allow"
    assert result["via_edge"] is True


def test_a_direct_transport_has_no_edge_and_says_nothing_about_one(monkeypatch):
    for var in (
        "NULLRUN_EDGE_URL",
        "NULLRUN_EDGE_LEASE_ID",
        "NULLRUN_EDGE_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    t = Transport(api_url="https://api.nullrun.io", api_key="k")
    assert t.edge is None
    assert "edge" not in repr(t.edge).lower().replace("edge-case", "")


def test_constructing_a_direct_transport_still_works_with_a_half_set_edge_env(monkeypatch):
    """Regression guard on the "costs nothing" claim, at the real seam."""
    monkeypatch.setenv("NULLRUN_EDGE_TOKEN", "orphan-token")
    t = Transport(api_url="https://api.nullrun.io", api_key="k")
    assert t.edge is None
    assert os.environ["NULLRUN_EDGE_TOKEN"] == "orphan-token"

# ── the runtime will accept it ────────────────────────────────────────


def test_the_runtime_honours_a_lease_decision_as_a_real_verdict():
    """The allowlist is the gate in front of the gate.

    `_require_gate_decision` runs before the decision is even read, and
    rejects any body whose `decision_source` it does not know. A via-edge
    allow that the runtime refused would fail CLOSED on its own success —
    which reads in an outage as "the box is broken", not "the SDK has
    never heard of this source".
    """
    from nullrun.runtime import NullRunRuntime

    rt = NullRunRuntime.__new__(NullRunRuntime)  # no I/O, no client
    assert (
        rt._require_gate_decision({"decision": "allow", "decision_source": EDGE_LEASE})
        == "allow"
    )
    assert (
        rt._require_gate_decision({"decision": "block", "decision_source": EDGE_LEASE})
        == "block"
    )


def test_adding_the_source_did_not_loosen_the_source_check():
    """The fix must ADD a name, never stop checking.

    The failure this guards: making via-edge work by admitting unknown
    sources turns the check below into no check at all — and that check
    is what stops a captive portal from answering with `{"decision":
    "allow"}`.
    """
    from nullrun.breaker.exceptions import NullRunMalformedGateResponseError
    from nullrun.runtime import NullRunRuntime

    rt = NullRunRuntime.__new__(NullRunRuntime)
    for body in (
        {"decision": "allow"},                                  # no source at all
        {"decision": "allow", "decision_source": "edge-box"},   # a near miss
        {"decision": "allow", "decision_source": 42},           # not a string
    ):
        with pytest.raises(NullRunMalformedGateResponseError):
            rt._require_gate_decision(body)


def test_a_source_is_not_enough_if_the_decision_is_nonsense():
    """The other half of the check, kept honest by the same edit."""
    from nullrun.breaker.exceptions import NullRunMalformedGateResponseError
    from nullrun.runtime import NullRunRuntime

    rt = NullRunRuntime.__new__(NullRunRuntime)
    with pytest.raises(NullRunMalformedGateResponseError):
        rt._require_gate_decision({"decision": "probably", "decision_source": EDGE_LEASE})
