"""A ``/gate`` body with no usable ``decision`` must never read as "allowed".

ADR-008 grants fail-OPEN to *transport* failures — the case where the
gate never got to rule on the call. A body that arrives but carries no
verdict is the opposite case: something answered, and it was not a
policy engine. Pre-fix ``check_workflow_budget`` read it as allowed:

    decision = response.get("decision", "allow")

That default converted four distinct situations into "the call may
proceed":

* a JSON object from a non-NULLRUN responder (proxy error page,
  captive portal, TLS interception box),
* a body that is not a JSON object at all,
* a ``decision`` the SDK has no arm for,
* ``decision="deny"`` — a live variant of the backend's ``GateDecision``
  (``gate/internal.rs:579``) reserved by ADR-046, which had no arm and
  fell off the end of the method.

The last one matters most: a *reserved refusal* was the one refusal
that executed. Each test below fails against the pre-fix code.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from nullrun.breaker.exceptions import (
    NullRunInfrastructureError,
    NullRunMalformedGateResponseError,
    NullRunProtocolError,
)

BASE_URL = "https://api.test.nullrun.io"
GATE_URL = f"{BASE_URL}/api/v1/gate"


def _gate_json(**overrides) -> dict:
    """A well-formed gate answer, with `overrides` applied."""
    body = {
        "decision": "allow",
        "decision_source": "gateway",
        "explanation": "",
        "policy_version": 1,
        "explanations": [],
    }
    body.update(overrides)
    return body


class TestMalformedGateDecisionRaises:
    """Every shape that is not a verdict must raise, not allow."""

    def test_absent_decision_field_raises(self, make_runtime, mock_api):
        """No `decision` key at all.

        The backend's `decision` is a non-`Option` field with no
        `skip_serializing_if` (`gate/internal.rs:637`), so a real
        NULLRUN response always carries one. Its absence means the
        responder was not NULLRUN.
        """
        payload = _gate_json()
        del payload["decision"]
        respx.post(GATE_URL).mock(return_value=httpx.Response(200, json=payload))

        rt = make_runtime()
        with pytest.raises(NullRunMalformedGateResponseError):
            rt.check_workflow_budget()

    def test_null_decision_raises(self, make_runtime, mock_api):
        """`decision: null` is not a verdict either."""
        respx.post(GATE_URL).mock(
            return_value=httpx.Response(200, json=_gate_json(decision=None))
        )
        rt = make_runtime()
        with pytest.raises(NullRunMalformedGateResponseError):
            rt.check_workflow_budget()

    def test_non_string_decision_raises(self, make_runtime, mock_api):
        """A numeric or object `decision` cannot be compared to "block"."""
        respx.post(GATE_URL).mock(
            return_value=httpx.Response(200, json=_gate_json(decision={"code": 1}))
        )
        rt = make_runtime()
        with pytest.raises(NullRunMalformedGateResponseError):
            rt.check_workflow_budget()

    def test_unknown_decision_string_raises(self, make_runtime, mock_api):
        """An unrecognised verdict is a contract this SDK does not implement.

        Reading it as "allow" is how a future backend addition would
        silently become a bypass.
        """
        respx.post(GATE_URL).mock(
            return_value=httpx.Response(200, json=_gate_json(decision="allow_anyway"))
        )
        rt = make_runtime()
        with pytest.raises(NullRunMalformedGateResponseError):
            rt.check_workflow_budget()

    def test_deny_decision_raises(self, make_runtime, mock_api):
        """`deny` is a refusal, and refusals do not execute.

        `GateDecision::Deny` exists on the wire (`gate/internal.rs:579`),
        reserved by ADR-046 with no production producer yet. Pre-fix it
        matched no arm and fell off the end of `check_workflow_budget`,
        which the caller reads as "no block raised, proceed".
        """
        respx.post(GATE_URL).mock(
            return_value=httpx.Response(
                200,
                json=_gate_json(
                    decision="deny",
                    explanation="policy denies this capability",
                ),
            )
        )
        rt = make_runtime()
        with pytest.raises(Exception) as exc_info:
            rt.check_workflow_budget()
        # Not the malformed-shape error: the body was well-formed, the
        # decision was just a refusal the SDK must honour.
        assert not isinstance(exc_info.value, NullRunMalformedGateResponseError)
        assert "policy denies this capability" in str(exc_info.value)


class TestMalformedGateBodyRaises:
    """Bodies that are not gate answers at all."""

    def test_non_json_body_raises_typed_error(self, make_runtime, mock_api):
        """An HTML interception page must not escape as JSONDecodeError.

        `Transport.check` ends in `response.json()`. Pre-fix the
        resulting ValueError matched neither the auth arm nor the
        fail-OPEN arm in the cached branch, and in the uncached branch
        was swallowed by `except Exception` as "gate unavailable" —
        i.e. a non-NULLRUN responder was read as an outage and the call
        proceeded.
        """
        respx.post(GATE_URL).mock(
            return_value=httpx.Response(
                200,
                text="<html><body>Corporate proxy block page</body></html>",
                headers={"content-type": "text/html"},
            )
        )
        rt = make_runtime()
        with pytest.raises(NullRunMalformedGateResponseError):
            rt.check_workflow_budget()

    def test_json_array_body_raises_typed_error(self, make_runtime, mock_api):
        """A JSON array is not a gate answer; `.get` on it would AttributeError."""
        respx.post(GATE_URL).mock(
            return_value=httpx.Response(200, json=["decision", "allow"])
        )
        rt = make_runtime()
        with pytest.raises(NullRunMalformedGateResponseError):
            rt.check_workflow_budget()


class TestMalformedErrorHierarchy:
    """The new error must be catchable by the handlers that already exist."""

    def test_is_a_protocol_error(self):
        """Callers already catching version mismatch catch this too."""
        assert issubclass(NullRunMalformedGateResponseError, NullRunProtocolError)

    def test_is_infrastructure_error(self):
        """And it is not a *decision*, so it must not be swallowed as one."""
        assert issubclass(NullRunMalformedGateResponseError, NullRunInfrastructureError)

    def test_has_distinct_error_code(self):
        """NR-P002, not the NR-P001 of "upgrade the SDK"."""
        assert NullRunMalformedGateResponseError.error_code == "NR-P002"


class TestAllowUnchanged:
    """The fail-CLOSED work must not have broken the ordinary allow path."""

    def test_allow_still_returns(self, make_runtime, mock_api):
        respx.post(GATE_URL).mock(
            return_value=httpx.Response(200, json=_gate_json(decision="allow"))
        )
        rt = make_runtime()
        rt.check_workflow_budget()  # must not raise
