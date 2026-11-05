"""SDK-13 — an SDK-internal fault in the @protect policy gate is a REFUSAL.

The audit entry, verbatim:

    **SDK-13 — the decorator reports "policy engine unavailable" and
    proceeds.** *Location:* ``src/nullrun/decorators.py`` ≈L1082–1108
    (broad ``except Exception`` ⇒ ``NR-B001`` / "policy engine
    unavailable"). *Failure:* an engine error is converted into a
    pass. *Verify:* an exception inside the policy engine must refuse.
    *Fix:* re-raise, or make the error surface as a distinct refusal
    code.

What the audit got right and what it got wrong
----------------------------------------------

**Right:** the site is a broad ``except Exception``, and the refusal it
produced was not a *distinct* refusal — it wore the network code
``NR-B001`` and the policy-block type.

**Wrong:** "an engine error is converted into a pass" was FALSE for the
default path. The arm already raised ``NullRunBlockedException``, so the
decorated body did not run. The audit read the ``if fail_open: return``
arm — the documented dev/test opt-out — as the default.

**What was actually true, and is worse:** the arm's fail-OPEN branch is
reachable ONLY for SDK-internal faults. The ``/execute`` path converts
every transport failure into ``NullRunTransportError`` (a class the arm
above owns), so what reaches the catch-all is a fault in the SDK's OWN
code. The two-step opt-out is documented as covering "the policy engine
is UNREACHABLE"; under it, an SDK bug ran the body — which is exactly
the "converted into a pass" the audit describes, reached through the
only path where it can happen.

⇒ The defect was the LABEL, not the absence of a refusal: an SDK fault
was reported as a *network* error (``NR-B001``) and typed as a *policy
decision* (``NullRunBlockedException`` ⊂ ``NullRunDecision``), so the
FastAPI integration rendered it **HTTP 429 / category "decision"** — the
operator was told the policy refused (or rate-limited) when the truth is
that the SDK has a bug.

This is the /execute twin of ADR-129's row 29 on the /gate side: same
fact, same class (``NullRunGateIndeterminateError`` / ``NR-P004``), same
counter discipline.

What these tests pin
--------------------

1. The audit's Verify clause, literally: an exception inside the policy
   engine refuses, and the body does not run.
2. The refusal is TYPED as an indeterminate decision (``NR-P004``), not
   as a network error and not as a policy decision.
3. The two-step opt-out does NOT open an SDK-internal fault — and it
   still DOES open an unreachable engine (the documented bypass
   survives; this is the control that stops the fix from over-reaching).
4. The refusal is counted, logged at ERROR, and carries the original
   exception as its cause.
5. On the wire (FastAPI) it is a 503 / "infrastructure", not a 429 /
   "decision".
6. A decision the gateway NAMED is still honoured as a decision.
"""

from __future__ import annotations

import dataclasses
import logging

import httpx
import pytest
import respx

from nullrun import messages
from nullrun.breaker.exceptions import (
    NullRunBlockedException,
    NullRunDecision,
    NullRunGateIndeterminateError,
    NullRunInfrastructureError,
    NullRunTransportError,
)
from nullrun.observability import metrics

BASE_URL = "https://api.test.nullrun.io"
EXECUTE_URL = f"{BASE_URL}/api/v1/execute"

_FLAG = "NULLRUN_SENSITIVE_FAIL_OPEN"
_ACK = "NULLRUN_ALLOW_SENSITIVE_FAIL_OPEN"


@pytest.fixture(autouse=True)
def _clean_flag_env(monkeypatch):
    """Both vars start unset, so a developer's shell cannot decide
    whether these tests pass."""
    monkeypatch.delenv(_FLAG, raising=False)
    monkeypatch.delenv(_ACK, raising=False)
    monkeypatch.delenv("NULLRUN_ENV", raising=False)
    monkeypatch.delenv("NULLRUN_API_URL", raising=False)


def _boom(*_args, **_kwargs):
    """An SDK-internal fault: not a transport error, not a decision.

    This is the shape the catch-all is for — the SDK's own code faulted
    while asking the policy engine a question. A ``TypeError`` is used
    because it is the shape a real SDK bug has: nothing in the SDK
    raises it on purpose.
    """
    raise TypeError("the request body could not be serialised")


def _install_fault(rt, monkeypatch):
    """Put the fault at the seam the decorator's ``try`` wraps.

    ``runtime.execute`` only catches ``NullRunBlockedException`` around
    the transport call, so the fault reaches the decorator exactly as
    an in-SDK fault would.
    """
    monkeypatch.setattr(rt._transport, "execute", _boom)


def _protected():
    """A protected function plus the list that records whether it ran."""
    from nullrun.decorators import protect

    ran: list[int] = []

    @protect
    def charge_card(amount: int) -> str:
        ran.append(amount)
        return "charged"

    return charge_card, ran


# ---------------------------------------------------------------------------
# 1. The audit's Verify clause
# ---------------------------------------------------------------------------


def test_an_exception_inside_the_policy_engine_refuses(make_runtime, monkeypatch):
    """The audit's clause, verbatim: it must refuse."""
    rt = make_runtime(api_url=BASE_URL)
    _install_fault(rt, monkeypatch)
    charge_card, ran = _protected()

    with pytest.raises(Exception) as info:
        charge_card(100)

    assert ran == [], "the body ran — the engine error became a pass"
    assert info.value is not None


def test_the_refusal_carries_the_original_exception_as_its_cause(
    make_runtime, monkeypatch
):
    """The cause is what makes the refusal actionable."""
    rt = make_runtime(api_url=BASE_URL)
    _install_fault(rt, monkeypatch)
    charge_card, _ran = _protected()

    with pytest.raises(NullRunGateIndeterminateError) as info:
        charge_card(100)

    assert isinstance(info.value.cause, TypeError)
    assert "could not be serialised" in str(info.value.cause)
    assert info.value.__cause__ is info.value.cause


# ---------------------------------------------------------------------------
# 2. The refusal is a DISTINCT refusal
# ---------------------------------------------------------------------------


def test_the_refusal_is_the_indeterminate_class(make_runtime, monkeypatch):
    """``NR-P004`` — the same class the /gate side uses (ADR-129)."""
    rt = make_runtime(api_url=BASE_URL)
    _install_fault(rt, monkeypatch)
    charge_card, _ran = _protected()

    with pytest.raises(NullRunGateIndeterminateError) as info:
        charge_card(100)

    assert info.value.error_code == "NR-P004"


def test_the_refusal_is_not_reported_as_a_network_error(make_runtime, monkeypatch):
    """Pre-fix it wore ``NR-B001`` — a NETWORK code.

    Every network failure is classified by the arm above (network /
    gateway / auth / breaker-open), so a fault that reaches the
    catch-all is by construction NOT a network failure. Reporting it
    as one sends the operator to the network.
    """
    rt = make_runtime(api_url=BASE_URL)
    _install_fault(rt, monkeypatch)
    charge_card, _ran = _protected()

    with pytest.raises(NullRunGateIndeterminateError) as info:
        charge_card(100)

    assert info.value.error_code != "NR-B001"


def test_the_refusal_is_not_a_policy_decision(make_runtime, monkeypatch):
    """A decision reads as "the policy engine refused this".

    The SDK could not obtain a decision at all — the workflow is not
    blocked BY POLICY, and a cookbook branch on
    ``except NullRunDecision`` must not treat an SDK bug as a verdict.
    """
    rt = make_runtime(api_url=BASE_URL)
    _install_fault(rt, monkeypatch)
    charge_card, _ran = _protected()

    with pytest.raises(NullRunGateIndeterminateError) as info:
        charge_card(100)

    assert not isinstance(info.value, NullRunDecision)
    assert not isinstance(info.value, NullRunBlockedException)


def test_the_refusal_is_not_a_transport_error(make_runtime, monkeypatch):
    """ADR-126's two-step opt-out opens TRANSPORT errors only.

    "The body runs" is not an acceptable answer to "the SDK could not
    determine the answer".
    """
    rt = make_runtime(api_url=BASE_URL)
    _install_fault(rt, monkeypatch)
    charge_card, _ran = _protected()

    with pytest.raises(NullRunGateIndeterminateError) as info:
        charge_card(100)

    assert not isinstance(info.value, NullRunTransportError)
    assert isinstance(info.value, NullRunInfrastructureError)


# ---------------------------------------------------------------------------
# 3. The opt-out does not open an SDK bug — but still opens an outage
# ---------------------------------------------------------------------------


def test_the_opt_out_does_not_open_an_sdk_internal_fault(make_runtime, monkeypatch):
    """The audit's "converted into a pass", reached the only way it can.

    The two-step opt-out is documented as covering "the policy engine is
    UNREACHABLE". An SDK bug is a different fact: nothing downstream
    corrects it, and ``/track`` does not gate on the policy answer.
    Pre-fix this arm returned and the body ran.
    """
    rt = make_runtime(api_url=BASE_URL)
    rt.register_fail_open_tools(["charge_card"])
    monkeypatch.setenv(_FLAG, "1")
    _install_fault(rt, monkeypatch)
    charge_card, ran = _protected()

    with pytest.raises(NullRunGateIndeterminateError):
        charge_card(100)

    assert ran == [], (
        "the opt-out opened an SDK-internal fault — the body ran with no "
        "policy decision"
    )


def test_the_opt_out_still_opens_an_unreachable_engine(make_runtime, monkeypatch):
    """The control: the documented dev/test bypass must survive.

    A transport failure is the case the opt-out exists for: the engine
    could not be REACHED, so there is no answer to honour. This test
    is what stops the fix above from over-reaching into the bypass.
    """
    rt = make_runtime(api_url=BASE_URL)
    rt.register_fail_open_tools(["charge_card"])
    monkeypatch.setenv(_FLAG, "1")

    respx.post(EXECUTE_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    charge_card, ran = _protected()

    charge_card(100)

    assert ran == [100], (
        "the documented dev/test bypass stopped working — the fix must "
        "not touch the TRANSPORT failure path"
    )


def test_an_unreachable_engine_without_the_opt_out_still_refuses(
    make_runtime, monkeypatch
):
    """The control for the other side: no opt-out, no body."""
    make_runtime(api_url=BASE_URL)

    respx.post(EXECUTE_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    charge_card, ran = _protected()

    with pytest.raises(NullRunBlockedException):
        charge_card(100)

    assert ran == []


# ---------------------------------------------------------------------------
# 4. The refusal is visible
# ---------------------------------------------------------------------------


def test_the_refusal_is_counted(make_runtime, monkeypatch):
    """A refusal that is not counted is invisible to the operator."""
    before = metrics.runtime.policy_gate_indeterminate_total
    rt = make_runtime(api_url=BASE_URL)
    _install_fault(rt, monkeypatch)
    charge_card, _ran = _protected()

    with pytest.raises(NullRunGateIndeterminateError):
        charge_card(100)

    assert metrics.runtime.policy_gate_indeterminate_total == before + 1


def test_the_refusal_is_logged_at_error(make_runtime, monkeypatch, caplog):
    """An SDK fault is a fault: it belongs on the ERROR channel."""
    rt = make_runtime(api_url=BASE_URL)
    _install_fault(rt, monkeypatch)
    charge_card, _ran = _protected()

    with caplog.at_level(logging.INFO, logger="nullrun.decorators"):
        with pytest.raises(NullRunGateIndeterminateError):
            charge_card(100)

    errors = [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.ERROR and "charge_card" in r.getMessage()
    ]
    assert errors, (
        "the refusal must be logged at ERROR with the tool name — "
        f"records: {[r.getMessage() for r in caplog.records]}"
    )


def test_the_refusal_fires_the_error_hook(make_runtime, monkeypatch):
    """The hook is how an operator's agent learns the call was stopped."""
    import nullrun

    seen: list[tuple[object, object]] = []
    unregister = nullrun.on_error(lambda err, ctx: seen.append((err, ctx)))

    try:
        rt = make_runtime(api_url=BASE_URL)
        _install_fault(rt, monkeypatch)
        charge_card, _ran = _protected()

        with pytest.raises(NullRunGateIndeterminateError):
            charge_card(100)
    finally:
        unregister()

    assert seen, "the error hook did not fire for the refusal"
    err, ctx = seen[-1]
    assert isinstance(err, NullRunGateIndeterminateError)
    assert getattr(ctx, "tool_name", None) == "charge_card", (
        "the hook context must name the tool — the refusal class carries "
        "no tool_name field of its own"
    )


# ---------------------------------------------------------------------------
# 5. On the wire
# ---------------------------------------------------------------------------


def test_the_refusal_is_a_503_not_a_429(make_runtime, monkeypatch):
    """The mislabelling, made visible.

    Pre-fix the refusal was a ``NullRunBlockedException`` with
    ``NR-B001``: the FastAPI integration mapped it through
    ``_DECISION_STATUS`` and, finding no entry, fell back to the
    generic decision status — **429 Too Many Requests**, category
    "decision". The operator was told the request was rate-limited.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from nullrun.integrations import fastapi as nr_fastapi

    rt = make_runtime(api_url=BASE_URL)
    _install_fault(rt, monkeypatch)
    charge_card, _ran = _protected()

    app = FastAPI()
    nr_fastapi.install(app)

    @app.get("/charge")
    def charge():
        return charge_card(100)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/charge")

    assert resp.status_code == 503, (
        "an indeterminate decision is an infrastructure failure, not a "
        f"rate limit — got {resp.status_code}"
    )
    body = resp.json()
    assert body["error_code"] == "NR-P004"
    assert body["category"] == "infrastructure"
    assert body["retryable"] is True


def test_the_refusal_has_an_operator_message():
    """Not the generic fallback.

    ``NR-P004`` had NO entry in ``DEFAULT_MESSAGES``, so the operator
    saw "Something went wrong. Please try again." for the one failure
    that STOPS the call.
    """
    err = NullRunGateIndeterminateError("no decision")
    out = messages.format_user_message(err)

    assert out == messages.DEFAULT_MESSAGES["NR-P004"]
    assert out != messages.FALLBACK_MESSAGE


# ---------------------------------------------------------------------------
# 6. The decision path is untouched
# ---------------------------------------------------------------------------


def test_a_gateway_block_is_still_a_decision(make_runtime):
    """The control: a decision the gateway NAMED is still a decision."""
    make_runtime(api_url=BASE_URL)

    respx.post(EXECUTE_URL).mock(
        return_value=httpx.Response(
            402,
            json={
                "error_code": "BUDGET_HARD_BLOCKED",
                "error_message": "the period budget is exhausted",
                "details": {"error_code": "BUDGET_HARD_BLOCKED"},
            },
        )
    )
    charge_card, ran = _protected()

    with pytest.raises(NullRunBlockedException) as info:
        charge_card(100)

    assert ran == []
    assert not isinstance(info.value, NullRunGateIndeterminateError)


def test_a_good_answer_still_runs_the_body(make_runtime):
    """The control: the fix must not over-refuse."""
    make_runtime(api_url=BASE_URL)
    charge_card, ran = _protected()

    charge_card(100)

    assert ran == [100]


# ---------------------------------------------------------------------------
# 7. The metrics export is complete
# ---------------------------------------------------------------------------


def test_every_counter_is_exported():
    """A counter in the dataclass but not in ``to_dict()`` is invisible.

    Pre-fix three counters were in that state — two runtime counters
    and one transport counter — so the operator could not see them even
    though the SDK was counting them.
    """
    exported = metrics.to_dict()
    for half in ("runtime", "transport"):
        fields = {f.name for f in dataclasses.fields(getattr(metrics, half))}
        missing = fields - set(exported[half])
        assert not missing, (
            f"the {half} counters {sorted(missing)} are counted but never "
            "exported — they are invisible to the operator"
        )
