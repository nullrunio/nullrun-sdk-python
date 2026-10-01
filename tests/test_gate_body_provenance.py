"""The discriminator is only trustworthy if the body is genuine.

The refusal classifier decides whether a 5xx was an ANSWER or an
OUTAGE by reading fields out of the response body:

    ``decision == "block"``      → the gate ruled; honour it
    anything else on a 5xx       → nothing ruled; ADR-008 fail-OPEN

Every other piece of category work on this branch rests on the same
premise — that a body arriving on the SDK's HTTPS connection was
written by NULLRUN. This file tests that premise instead of taking
it, because the failure mode when it is false is not a wrong error
message: it is a call authorised that no policy engine ever
evaluated.

The threat is an on-path responder — captive portal on a hotel or
airport network, corporate TLS-interception proxy, a compromised
sidecar — that returns well-formed JSON on ``/api/v1/gate``. It does
not need the API key. It does not need to defeat HMAC. It only needs
to answer faster than the real backend.

Two directions, and they are not the same risk:

* **A forged body that reads as ``block`` is safe.** The discriminator
  makes it more restrictive, not less. Worth pinning anyway, because
  the naive fix for "a foreign body must not become permission" is to
  start trusting unknown fields, and that would open this direction.

* **A forged body that reads as ``allow`` is the real hole.** The
  pre-existing guard (`runtime.py::_require_gate_decision`) catches a
  non-object, a missing ``decision``, and an unrecognised ``decision``
  string. It does not catch ``{"decision": "allow"}`` — which passes
  every check it applies. And the missing ``decision_source`` is
  currently read as *more* trustworthy than ``"fallback"``: the
  runtime's rule is ``decision_source != FALLBACK → honour the wire
  decision``, so a body with no provenance at all is honoured as if
  it came from the gateway.

The second half of the file pins the property the fix must not break:
a refusal the SDK cannot classify raises, and raises a type the
fail-open arms cannot catch. A captive portal that returns
``{"decision": "block"}`` with no category must not be laundered into
an allow by an exception handler, which is the same bypass reached
through the error path rather than the success path.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from nullrun.breaker.categories import (
    NullRunUnclassifiedRefusalError,
    is_gate_refusal,
    resolve_refusal_category,
)
from nullrun.breaker.exceptions import (
    NullRunError,
    NullRunInfrastructureError,
    NullRunMalformedGateResponseError,
    NullRunTransportError,
)
from nullrun.runtime import NullRunRuntime

BASE_URL = "https://api.test.nullrun.io"
GATE_URL = f"{BASE_URL}/api/v1/gate"


def _html_portal() -> httpx.Response:
    """The ordinary captive portal: a login page, HTTP 200."""
    return httpx.Response(
        200,
        text="<html><head><title>Sign in to continue</title></head>"
        "<body><form action='/login'>username password</form></body></html>",
        headers={"content-type": "text/html"},
    )


def _json_echo(body: dict, *, status: int = 200) -> httpx.Response:
    """A JSON answer from something that is not NULLRUN."""
    return httpx.Response(status, json=body)


class TestForeignBodyCannotBecomePermission:
    """The success path. A body is permission only if it is a verdict."""

    def test_captive_portal_html_does_not_authorise(self, make_runtime, mock_api):
        respx.post(GATE_URL).mock(return_value=_html_portal())
        rt = make_runtime()
        with pytest.raises(NullRunError):
            rt.check_workflow_budget()

    def test_bare_allow_object_without_provenance_does_not_authorise(
        self, make_runtime, mock_api
    ):
        """The hole this file was written for.

        `{"decision": "allow"}` satisfies every check
        `_require_gate_decision` applies today: it is a dict, and
        `decision` is a known string. What it is missing is any
        statement of WHO decided — and the SDK's own rule reads a
        missing `decision_source` as "not fallback", i.e. as a
        gateway decision to be honoured.

        A real `/gate` answer always carries `decision_source`:
        `GateResponse.decision_source` is a non-`Option` `String`
        with no `skip_serializing_if` (`gate/internal.rs:638`), so
        serde cannot omit it. The same holds for `decision` itself
        (`:637`). `GateResponseBody` in `schemas.rs` does make
        `decision_source` optional, but that struct is referenced
        only from `openapi.rs` — it is the documentation schema, not
        the wire — so it is not evidence that a real answer can lack
        the field.

        An on-path responder that gets this JSON in front of the SDK
        authorises a call no policy engine evaluated, and every
        downstream signal agrees it was authorised: the runtime sees
        a gateway decision, `/track` books the cost against a policy
        that was never consulted, and the audit trail records an
        allow. There is no later point at which it can be caught.
        """
        respx.post(GATE_URL).mock(return_value=_json_echo({"decision": "allow"}))
        rt = make_runtime()
        with pytest.raises(NullRunError):
            rt.check_workflow_budget()

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({}, id="empty-object"),
            pytest.param({"status": "ok"}, id="unrelated-json"),
            pytest.param({"decision": None}, id="decision-null"),
            pytest.param({"decision": "ok"}, id="unknown-decision-string"),
            pytest.param({"decision": "ALLOW"}, id="wrong-case-decision"),
            pytest.param({"decision": ["allow"]}, id="decision-wrong-type"),
            pytest.param([{"decision": "allow"}], id="json-array"),
            pytest.param("allow", id="bare-json-string"),
        ],
    )
    def test_shapes_without_a_verdict_all_raise(
        self, body, make_runtime, mock_api
    ):
        """The cases the existing guard already covers.

        Pinned so the provenance check added for the `decision`-only
        body is not mistaken for the whole defence, and so a future
        loosening of one arm is visible.
        """
        respx.post(GATE_URL).mock(return_value=_json_echo(body))
        rt = make_runtime()
        with pytest.raises(NullRunMalformedGateResponseError):
            rt.check_workflow_budget()

    def test_a_real_allow_still_authorises(self, make_runtime, mock_api):
        """Counter-test. The guard must not cost a single real call.

        Shaped from the live-captured envelope the SDK's own
        `test_adr063_infra_refusal_is_not_fail_closed.py` documents:
        the mandatory keys, with `remaining_budget_cents` left off
        because it is `Option` on `GateResponse` and absent is
        legitimate.

        `check_workflow_budget` returns `None` on success — it is a
        "raise if refused" pre-flight, not a query — so the property
        asserted here is that it does not raise.
        """
        respx.post(GATE_URL).mock(
            return_value=_json_echo(
                {
                    "decision": "allow",
                    "decision_source": "gateway",
                    "explanation": "within budget",
                    "policy_version": 3,
                    "policy_id": "0f5a0e1c-2b6d-4c1a-9f3e-7d8b2a4c6e90",
                    "remaining_budget_cents": 120_000,
                }
            )
        )
        rt = make_runtime()
        assert rt.check_workflow_budget() is None

    def test_fallback_decision_source_still_authorises(
        self, make_runtime, mock_api
    ):
        """The synthetic SDK-made fallback keeps working.

        It is produced by this process, not the wire, so it is
        exactly the body a naive "trust only gateway bodies" rule
        would break — and ADR-008's fail-OPEN on a dead backend
        depends on it working.
        """
        respx.post(GATE_URL).mock(side_effect=httpx.ConnectError("connection refused"))
        rt = make_runtime()
        assert rt.check_workflow_budget() is None


class TestForgedRefusalCannotBecomePermissionEither:
    """The error path. A forged `block` is safe; a forged one that
    raises must not be laundered into an allow by a handler."""

    def test_forged_block_is_classified_not_guessed(self, make_runtime, mock_api):
        """A forged refusal is treated exactly like a real one.

        It has to be. The discriminator cannot distinguish them, and
        trying to would mean the SDK trusts *some* bodies more than
        others — which is the defect, not the fix. A forged `block`
        stops the agent, which is the safe direction.
        """
        respx.post(GATE_URL).mock(
            return_value=_json_echo(
                {"decision": "block", "decision_source": "gateway"}, status=503
            )
        )
        rt = make_runtime()
        with pytest.raises(NullRunUnclassifiedRefusalError):
            rt.check_workflow_budget()

    def test_forged_block_never_reaches_the_agent(self, make_runtime, mock_api):
        respx.post(GATE_URL).mock(
            return_value=_json_echo(
                {
                    "decision": "block",
                    "decision_source": "gateway",
                    "category": "denied",
                    "agent_message": "not permitted",
                },
                status=200,
            )
        )
        rt = make_runtime(on_denied="message")
        with pytest.raises(NullRunError):
            rt.check_workflow_budget()

    def test_unclassified_refusal_is_not_a_transport_error(self):
        """Structural pin on the bypass that makes the above moot.

        `check_workflow_budget` fails OPEN in its `except
        NullRunTransportError` arms. If
        `NullRunUnclassifiedRefusalError` were a subclass of that,
        every one of the tests above would still pass — the exception
        is raised, caught, converted to `None`, and the agent
        proceeds — and only this assertion would notice.

        They are siblings today: both descend from
        `NullRunInfrastructureError`, neither from the other. That is
        load-bearing and cheap to break by reordering the class
        hierarchy, so it is asserted rather than assumed.
        """
        assert issubclass(NullRunUnclassifiedRefusalError, NullRunInfrastructureError)
        assert not issubclass(
            NullRunUnclassifiedRefusalError, NullRunTransportError
        ), (
            "an unclassifiable refusal would be swallowed by the fail-OPEN "
            "arm and become an allow — the exact bypass this pins"
        )
        assert not issubclass(
            NullRunMalformedGateResponseError, NullRunTransportError
        ), (
            "a body that is not a verdict would likewise be swallowed; "
            "`NullRunProtocolError` must stay a sibling of the transport "
            "errors, not a subclass"
        )


class TestClassifierNeedsNoProvenanceOfItsOwn:
    """`is_gate_refusal` is a pure predicate and must stay one.

    It runs before any provenance check, on whatever body arrived.
    Adding a provenance requirement here would be the tempting fix and
    the wrong one: this function's job is to answer "did the gate
    rule?", and it is called on the *wire* body before the transport
    has decided whether that wire is trustworthy. Widening it would
    turn the classifier into a second, differently-behaving gate.
    """

    def test_it_answers_only_about_the_decision_field(self):
        assert is_gate_refusal({"decision": "block"}) is True
        assert is_gate_refusal({"decision": "allow"}) is False
        assert is_gate_refusal({}) is False
        assert is_gate_refusal(None) is False

    def test_a_missing_category_raises_rather_than_returning_none(self):
        """A body that IS a refusal must not degrade to "not one".

        `resolve_refusal_category` returning `None` means "this was
        never a gate refusal, keep your existing handling" — and the
        caller's existing handling for a 5xx is fail-open. So the
        absent-category arm has to raise, which is what it does. The
        case is pinned because collapsing it to a `None` return is a
        one-word change that silently reinstates the allow.
        """
        with pytest.raises(NullRunUnclassifiedRefusalError):
            resolve_refusal_category({"decision": "block", "decision_source": "gateway"})


class TestRuntimeIsNotTheOnlyEntrypoint:
    """The same question on the `/execute` path.

    `MCPAdapter.call_tool` is the second enforcement path and it reads
    `/api/v1/execute` refusals through a different parser. A captive
    portal in front of that endpoint is the same threat, so the
    negative direction is pinned there too: a refusal must stop the
    call, whatever it is.
    """

    def test_execute_refusal_stops_the_mcp_server(self, mock_api):
        from nullrun.toolbox.mcp import MCPAdapter

        class _Ann:
            readOnlyHint = False
            destructiveHint = True
            openWorldHint = True

        class _Tool:
            name = "create_issue"
            annotations = _Ann()

        class _Client:
            def __init__(self):
                self.calls: list[str] = []

            def list_tools(self):
                return [_Tool()]

            def call_tool(self, name, arguments=None, **kwargs):
                self.calls.append(name)
                return "ok"

        execute_url = f"{BASE_URL}/api/v1/execute"
        respx.post(execute_url).mock(
            return_value=_json_echo(
                {
                    "decision": "block",
                    "decision_source": "gateway",
                    "details": {"error_code": "TOOL_BLOCKED"},
                },
                status=403,
            )
        )
        client = _Client()
        adapter = MCPAdapter(
            server_name="github",
            mcp_client=client,
            runtime=NullRunRuntime(
                api_key="test-key-12345678",
                secret_key="test-secret-deterministic",
                api_url=BASE_URL,
                polling=False,
            ),
        )
        with pytest.raises(NullRunError):
            adapter.call_tool("create_issue", {"repo": "acme/api"})
        assert client.calls == [], (
            "a refusal must stop the MCP server, and a foreign responder "
            "must not be able to authorise the tool call"
        )
