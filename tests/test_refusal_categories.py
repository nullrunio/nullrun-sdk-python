"""ADR-062 §2.2 refusal categories and the `on_denied` opt-in.

The gate does not send a bare "no" — it sends *why*, in exactly four
values (`DecisionCategory` on the backend). The SDK's job is to
classify the refusal, and the rule that matters is what it does when
it *cannot*:

    An absent or unrecognised category RAISES. It is never guessed.

Guessing is the dangerous direction, and specifically: if the SDK
rounds an unknown refusal to something permissive, a `budget` or
`halt` refusal can be rendered as a friendly, model-readable "that's
not allowed" — and an agent handed that will adapt and retry against
a stop an operator deliberately placed. That is the bypass ADR-061
closed on the server side, re-opened client-side.

"""

from __future__ import annotations

import httpx
import pytest
import respx

from nullrun.breaker.categories import (
    DecisionCategory,
    NullRunUnclassifiedRefusalError,
    is_gate_refusal,
    resolve_refusal_category,
)
from nullrun.breaker.exceptions import (
    NullRunBlockedException,
    NullRunBudgetError,
    NullRunError,
    NullRunInfrastructureError,
)
from nullrun.runtime import NullRunRuntime
from nullrun.transport import Transport

BASE_URL = "https://api.test.nullrun.io"
GATE_URL = f"{BASE_URL}/api/v1/gate"

_CHECK_KWARGS = {
    "check_request": {
        "organization_id": "ws-123",
        "workflow_id": "wf-" + "a" * 32,
        "trace_id": "trace-789",
        "tool": "read_file",
    },
    "on_transport_error": "raise",
}

# The categories the backend actually emits, with the error code it
# pairs with in production. Kept as data so the table itself is
# readable next to the assertions that consume it.
REAL_REFUSALS = {
    # code: (category, http status) — both read off the backend's
    # own `GateErrorCode` (category() / http_status()), not guessed.
    "TOOL_BLOCKED": ("denied", 403),
    "APPROVAL_DENIED": ("denied", 403),
    "LOOP_DETECTED": ("denied", 403),
    "MCP_DESTRUCTIVE_BLOCKED": ("denied", 403),
    "BUDGET_HARD_BLOCKED": ("budget", 402),
    "BUDGET_WORKFLOW_BLOCKED": ("budget", 402),
    "RATE_LIMIT_EXCEEDED": ("budget", 429),
    "WORKFLOW_PAUSED": ("halt", 403),
    "WORKFLOW_INACTIVE": ("halt", 403),
    "CIRCUIT_BREAKER_TRIPPED": ("halt", 403),
    # The two ADR-063 §4.7 503 groups. Both arrive as 503 with
    # decision="block"; the category is what separates them.
    "BUDGET_DATA_UNAVAILABLE": ("infra", 503),
    "CIRCUIT_BREAKER_STATE_LOOKUP_FAILED": ("infra", 503),
}


def _refusal(
    error_code: str,
    category: str | None,
    *,
    status: int = 402,
    agent_message: str | None = None,
) -> httpx.Response:
    """A gate refusal body shaped like the real wire.

    `agent_message` is only populated for `denied`, because that is
    the only category the backend writes model-safe text for
    (`gate.rs::attach_refusal_surface`). Callers pass
    `category=None` to model the NR-005 path, where no code resolved
    and the backend therefore sent no category at all.
    """
    body: dict = {
        "decision": "block",
        "decision_source": "gateway",
        "explanation": error_code,
        "error_code": error_code,
        "user_message": f"operator note for {error_code}",
    }
    if category is not None:
        body["category"] = category
        if category == "denied":
            body["agent_message"] = agent_message or f"{error_code} is not permitted."
    return httpx.Response(status, json=body)


class TestCategoryParsing:
    def test_only_denied_is_model_message_safe(self):
        """The safety property, stated as a table.

        `agent_message` is the only field allowed to reach a model.
        It exists on the wire for exactly one category, and this
        predicate is the only thing that says so on this side.
        """
        safe = {c for c in DecisionCategory if c.is_model_message_safe()}
        assert safe == {DecisionCategory.DENIED}
        assert len(DecisionCategory) == 4, "a fifth category needs SDK work"

    def test_absent_category_raises(self):
        with pytest.raises(NullRunUnclassifiedRefusalError) as exc:
            resolve_refusal_category({"decision": "block", "error_code": "TOOL_BLOCKED"})
        err = exc.value
        assert err.wire_category is None
        assert err.wire_error_code == "TOOL_BLOCKED", (
            "the operator needs the code to report the drift"
        )
        assert err.error_code == "NR-P003"
        assert isinstance(err, NullRunInfrastructureError), (
            "an unclassifiable refusal is a system fault, not a policy outcome — "
            "host code branches on the marker classes"
        )

    def test_unrecognised_category_raises_rather_than_rounding(self):
        """The rounding failure mode, pinned.

        A permissive `except ValueError: return INFRA` would pass a
        test that only checks "it raised something", and would make
        every future category silently read as a backend fault.
        """
        with pytest.raises(NullRunUnclassifiedRefusalError) as exc:
            resolve_refusal_category({"decision": "block", "category": "teapot"})
        assert exc.value.wire_category == "teapot"

    def test_non_string_category_raises(self):
        with pytest.raises(NullRunUnclassifiedRefusalError):
            resolve_refusal_category({"decision": "block", "category": 42})

    @pytest.mark.parametrize("category", sorted({c for c, _ in REAL_REFUSALS.values()}))
    def test_every_real_category_parses(self, category):
        assert resolve_refusal_category(
            {"decision": "block", "category": category}
        ) is DecisionCategory(category)

    def test_non_refusal_bodies_are_left_alone(self):
        """Strictness is scoped to refusals.

        A protocol mismatch, an admin 422, a heartbeat 404 — none
        carry a refusal category, and turning them into
        infrastructure faults would be its own bug.
        """
        for body in (
            {},
            {"error_code": "PROTOCOL_TOO_OLD"},
            {"decision": "allow"},
            {"decision": "soft_pass"},
            {"decision": "require_approval"},
            "not even a dict",
        ):
            assert is_gate_refusal(body) is False
            assert resolve_refusal_category(body) is None


class TestTransportBoundary:
    """The classification happens once, at the wire."""

    def test_absent_category_escapes_check(self):
        t = Transport(api_url=BASE_URL, api_key="test-key-12345678")
        with respx.mock(assert_all_called=False) as mock:
            mock.post(GATE_URL).mock(return_value=_refusal("BUDGET_HARD_BLOCKED", None))
            with pytest.raises(NullRunUnclassifiedRefusalError):
                t.check(**_CHECK_KWARGS)

    def test_category_and_text_are_carried_through(self):
        t = Transport(api_url=BASE_URL, api_key="test-key-12345678")
        with respx.mock(assert_all_called=False) as mock:
            mock.post(GATE_URL).mock(
                return_value=_refusal("TOOL_BLOCKED", "denied", status=403)
            )
            result = t.check(**_CHECK_KWARGS)
        assert result["category"] is DecisionCategory.DENIED
        assert result["agent_message"]

    def test_non_refusal_4xx_carries_no_category(self):
        """A 4xx with no `decision` field keeps its old handling."""
        t = Transport(api_url=BASE_URL, api_key="test-key-12345678")
        with respx.mock(assert_all_called=False) as mock:
            mock.post(GATE_URL).mock(
                return_value=httpx.Response(400, json={"error_code": "PROTOCOL_TOO_OLD"})
            )
            result = t.check(**_CHECK_KWARGS)
        assert result["decision"] == "block"
        assert result["category"] is None


class TestFailOpenDoesNotSwallowIt:
    """The hole the whole feature exists to close.

    `check_workflow_budget` has two `except` arms that fail OPEN
    (ADR-008). An unclassifiable refusal is neither a transport
    failure nor an auth failure — the gate answered — so if either
    arm catches it, the caller gets `None` and the agent proceeds on
    a call the backend refused. That is DEF-MP-TS12-ENF-01's shape,
    with a different trigger.
    """

    def test_cached_branch_does_not_fail_open(self, make_runtime, mock_api):
        import uuid

        from nullrun.context import chain

        respx.post(GATE_URL).mock(
            return_value=_refusal("BUDGET_HARD_BLOCKED", None, status=402)
        )
        rt = make_runtime()
        with chain(str(uuid.uuid4())):
            with pytest.raises(NullRunUnclassifiedRefusalError):
                rt.check_workflow_budget()

    def test_uncached_branch_does_not_fail_open(self, make_runtime, mock_api):
        respx.post(GATE_URL).mock(
            return_value=_refusal("BUDGET_HARD_BLOCKED", None, status=402)
        )
        rt = make_runtime()
        with pytest.raises(NullRunUnclassifiedRefusalError):
            rt.check_workflow_budget()

    def test_real_transport_failure_still_fails_open(self, make_runtime, mock_api):
        """Counter-test: the fail-OPEN policy itself is untouched.

        ADR-008 promises a dead backend does not freeze the agent.
        Narrowing it for refusals must not narrow it for outages.
        """
        respx.post(GATE_URL).mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        rt = make_runtime()
        assert rt.check_workflow_budget() is None
