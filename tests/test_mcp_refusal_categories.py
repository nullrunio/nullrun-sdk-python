"""ADR-062 §2.2 categories on the MCP path — the real adapter.

`MCPAdapter.call_tool` is a different enforcement path from
`check_workflow_budget`. It calls `runtime.execute(...)` against
`/api/v1/execute`, not `/gate`, so anything the category work added
to the `/gate` block site is, by default, absent here.

That is the specific gap this file covers. Two properties, and they
are independent:

1. **The refusal is classified and fails CLOSED on the MCP path.**
   `runtime.execute` has no fail-OPEN `except` — an unclassifiable
   refusal raised by the transport propagates — but that is an
   argument from reading the code, not evidence, and the reason
   DEF-MP-TS12-ENF-01 shipped at all is that reading code was not
   enough.

2. **`on_denied` reaches the MCP path.** The flag is documented as
   selecting the shape of a `denied` refusal. If it silently does
   nothing for MCP tools — a whole class of tool calls — then a host
   that set `on_denied="message"` is getting a promise it does not
   keep, and the model-facing text it expected to relay never
   arrives.

The doubles here are deliberately shallow: a mock MCP client with a
tool inventory, and a real `MCPAdapter`, a real `NullRunRuntime`,
and a respx-mocked `/execute`. Nothing between the adapter and the
wire is faked, so a change that bypasses the gate on this path turns
these red rather than being stubbed around.

`test_mcp_adapter_gate_closed.py` deliberately has no autouse
runtime fixture so it can observe the real resolution order; this
file binds the runtime explicitly and does not care about order.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx


from nullrun.breaker.exceptions import (
    NullRunBlockedException,
    NullRunDeniedError,
    NullRunError,
)
from nullrun.toolbox.mcp import MCPAdapter
from nullrun.runtime import NullRunRuntime

EXECUTE_URL = "https://api.test.nullrun.io/api/v1/execute"


class _Ann:
    def __init__(self, read=None, destructive=None, open_world=None):
        self.readOnlyHint = read
        self.destructiveHint = destructive
        self.openWorldHint = open_world


class _Tool:
    def __init__(self, name, annotations=None):
        self.name = name
        self.annotations = annotations


class _MockMcpClient:
    """Minimal MCP client surface. Records calls so a test can assert
    the server was NOT reached after a block."""

    def __init__(self, tools):
        self._tools = {t.name: t for t in tools}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_tools(self):
        return list(self._tools.values())

    def call_tool(self, name, arguments=None, **kwargs):
        self.calls.append((name, arguments or {}))
        if name not in self._tools:
            raise KeyError(f"unknown tool {name!r}")
        return f"ok:{name}"


def _refusal_body(
    code: str,
    category: str | None,
    *,
    agent_message: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "decision": "block",
        "decision_source": "gateway",
        "details": {"error_code": code},
        "explanation": f"refused: {code}",
        "explanations": [f"refused: {code}"],
        "policy_id": None,
        "policy_version": 0,
        "projected_cost_cents": None,
        "remaining_budget_cents": None,
        "reservation_id": None,
        "staleness_ms": None,
        "user_message": "operator-facing text",
    }
    if category is not None:
        body["category"] = category
    if agent_message is not None:
        body["agent_message"] = agent_message
    return body


def _adapter(runtime) -> tuple[MCPAdapter, _MockMcpClient]:
    client = _MockMcpClient(
        [
            _Tool(
                "create_issue",
                _Ann(read=False, destructive=True, open_world=True),
            )
        ]
    )
    return (
        MCPAdapter(server_name="github", mcp_client=client, runtime=runtime),
        client,
    )


def _runtime(**kwargs) -> NullRunRuntime:
    return NullRunRuntime(
        api_key="test-key-12345678",
        secret_key="test-secret-deterministic",
        api_url="https://api.test.nullrun.io",
        polling=False,
        **kwargs,
    )


class TestMcpRefusalFailsClosed:
    """Property 1: the gate's answer stands on the MCP path."""

    def test_classified_denial_blocks_and_the_server_is_not_called(self, mock_api):
        respx.post(EXECUTE_URL).mock(
            return_value=httpx.Response(
                403, json=_refusal_body("TOOL_BLOCKED", "denied")
            )
        )
        adapter, client = _adapter(_runtime())

        with pytest.raises(NullRunBlockedException):
            adapter.call_tool("create_issue", {"repo": "acme/api"})

        assert client.calls == [], (
            "the MCP server must not be reached after the gate refused"
        )

    def test_unclassifiable_refusal_raises_rather_than_calling_the_server(
        self, mock_api
    ):
        """The strict rule holds on the second enforcement path too.

        A refusal with no `category` must not reach the MCP server by
        way of a permissive default. Whether it raises the specific
        `NullRunUnclassifiedRefusalError` or a broader
        `NullRunError` is not the property — the property is that it
        raises at all, and that the server was not called.
        """
        respx.post(EXECUTE_URL).mock(
            return_value=httpx.Response(
                403, json=_refusal_body("TOOL_BLOCKED", None)
            )
        )
        adapter, client = _adapter(_runtime())

        with pytest.raises(NullRunError):
            adapter.call_tool("create_issue", {"repo": "acme/api"})

        assert client.calls == [], (
            "an unclassifiable refusal must not be laundered into a call"
        )

    def test_budget_refusal_keeps_its_own_exception(self, mock_api):
        """A `budget` refusal is not a `denied` one, on this path too."""
        respx.post(EXECUTE_URL).mock(
            return_value=httpx.Response(
                402, json=_refusal_body("BUDGET_HARD_BLOCKED", "budget")
            )
        )
        adapter, client = _adapter(_runtime())

        with pytest.raises(NullRunError) as exc_info:
            adapter.call_tool("create_issue", {"repo": "acme/api"})

        assert not isinstance(exc_info.value, NullRunDeniedError)
        assert client.calls == []


class TestOnDeniedReachesMcp:
    """Property 2: the flag applies to MCP tool calls."""

    def test_denied_with_message_enabled_raises_denied_error(self, mock_api):
        respx.post(EXECUTE_URL).mock(
            return_value=httpx.Response(
                403,
                json=_refusal_body(
                    "TOOL_BLOCKED",
                    "denied",
                    agent_message="The operator has not allowed this tool.",
                ),
            )
        )
        adapter, client = _adapter(_runtime(on_denied="message"))

        with pytest.raises(NullRunDeniedError) as exc_info:
            adapter.call_tool("create_issue", {"repo": "acme/api"})

        assert exc_info.value.agent_message == (
            "The operator has not allowed this tool."
        ), (
            "the server-authored agent_message must survive the round trip "
            "— it is the only text the SDK guarantees is safe for a model"
        )
        assert client.calls == []

    def test_denied_with_message_disabled_raises_the_catalogue_error(
        self, mock_api
    ):
        respx.post(EXECUTE_URL).mock(
            return_value=httpx.Response(
                403, json=_refusal_body("TOOL_BLOCKED", "denied")
            )
        )
        adapter, client = _adapter(_runtime())

        with pytest.raises(NullRunError) as exc_info:
            adapter.call_tool("create_issue", {"repo": "acme/api"})

        assert not isinstance(exc_info.value, NullRunDeniedError), (
            "the default is 'raise' with the code-specific catalogue "
            "exception, not the message path"
        )
        assert client.calls == []

    @pytest.mark.parametrize(
        "category,status,code",
        [
            ("budget", 402, "BUDGET_HARD_BLOCKED"),
            ("halt", 403, "LOOP_DETECTED"),
        ],
    )
    def test_non_denied_categories_never_become_a_message(
        self, mock_api, category, status, code
    ):
        """The negative set, on the MCP path.

        Same reasoning as on `/gate`, and it matters more here: an MCP
        tool is far more likely to be retried with a different
        argument or a different tool than a local function is, so a
        model told "that tool is not allowed" has an obvious next move
        that walks straight into a budget wall or an operator's stop.

        The wire codes are the load-bearing part of this test and were
        the first thing to get it wrong. An earlier version used a
        synthetic `SOME_CODE`, which `_parse_v3_error_envelope` maps
        to a non-block class — so the exception never reached the
        `except NullRunBlockedException` arm, and the parameters
        passed for a reason that had nothing to do with the guard.
        Widening the guard to consult `on_denied` alone left them
        green. That is the self-defeating-test failure mode this
        branch has already hit five times, caught here only by
        running the mutation and reading which tests actually went
        red.

        Every code below is real, and each one was checked to map to a
        `NullRunBlockedException` subclass, so each genuinely enters
        the arm and is rejected by the category check rather than
        never arriving.

        `infra` is deliberately NOT in this set — see
        `test_infra_refusal_is_a_gateway_error_not_a_message`. There is
        no 4xx infra refusal to put here: ADR-063 §4.7 sends infra
        refusals as 503, and 503 never reaches the envelope parser on
        this endpoint.
        """
        respx.post(EXECUTE_URL).mock(
            return_value=httpx.Response(
                status, json=_refusal_body(code, category)
            )
        )
        adapter, client = _adapter(_runtime(on_denied="message"))

        with pytest.raises(NullRunError) as exc_info:
            adapter.call_tool("create_issue", {"repo": "acme/api"})

        assert not isinstance(exc_info.value, NullRunDeniedError), (
            f"category={category!r} must keep its own exception even with "
            f"on_denied='message'"
        )
        assert client.calls == []

    def test_infra_refusal_is_a_gateway_fallback_not_a_message(self, mock_api):
        """`infra` is covered by a different mechanism, so say which.

        An infra-category refusal arrives as a 503 (ADR-063 §4.7).
        `_retry_with_backoff` maps the whole 5xx band on `/execute` to
        `NullRunTransportError` / GATEWAY_ERROR before
        `_parse_v3_error_envelope` is ever called, so the `category`
        field in the body is never read. The runtime then converts the
        transport error into a STRICT fallback block — raised at
        `runtime.py:3670`, downstream of the `on_denied` arm, which
        only wraps the `self._transport.execute(...)` call itself.

        Two things follow, and both are asserted rather than assumed.

        First, the right outcome: an infrastructure fault is not a
        permission answer, and `on_denied="message"` must not turn one
        into model-readable "that tool is not allowed" text. This is
        the product decision — ordinary unavailability is not a
        denial.

        Second, the mechanism: the property here is enforced by the 5xx
        band and the STRICT fallback, NOT by the category check. A
        future change that let a 5xx through to the envelope parser
        would move `infra` from this mechanism to the parametrised
        set's with nothing here going red. Asserting the concrete
        class and the fallback marker in the reason pins which
        mechanism is actually in play, so that move would break this
        test instead of passing silently.
        """
        respx.post(EXECUTE_URL).mock(
            return_value=httpx.Response(
                503, json=_refusal_body("REDIS_UNAVAILABLE", "infra")
            )
        )
        adapter, client = _adapter(_runtime(on_denied="message"))

        with pytest.raises(NullRunBlockedException) as exc_info:
            adapter.call_tool("create_issue", {"repo": "acme/api"})

        assert not isinstance(exc_info.value, NullRunDeniedError)
        assert "Gateway unavailable" in str(exc_info.value), (
            "the 5xx must still read as a gateway fault; if this becomes a "
            "policy denial the product decision has been inverted"
        )
        assert client.calls == []

    def test_absent_category_raises_even_with_message_enabled(self, mock_api):
        """`on_denied="message"` must not launder an ABSENT category.

        The companion to
        `test_non_denied_categories_never_become_a_message`, and it
        guards a different mistake: there the category is present and
        is not `denied`; here it is simply missing. Both must reach
        the same outcome, because a handler that guesses "no category
        means it was not a real denial" reads an unclassifiable
        refusal as a permission answer and hands the model text the
        server never wrote.

        The assertion is `NullRunError`, not
        `NullRunUnclassifiedRefusalError`, and the difference is
        deliberate. `/execute` raises a code-specific exception
        derived from the server's own ``details.error_code``
        (``TOOL_BLOCKED`` → ``NullRunToolBlockedError``) rather than
        routing through the `/gate` path's
        ``resolve_refusal_category``. That is not a guess — the code
        is server-authored — and it is a pre-existing shape of this
        endpoint that a category feature has no business changing
        wholesale. The ADR-062 §2.2 property is "absent or
        unrecognised category RAISES, never guessed", and it holds
        here: what is asserted is the raise, not its class. Asserting
        the `/gate`-path subclass instead would pin a behaviour
        `/execute` never had and fail for the wrong reason.

        What must be true either way: it raises, it is not
        `NullRunDeniedError`, and the MCP server was not reached.
        """
        respx.post(EXECUTE_URL).mock(
            return_value=httpx.Response(
                403, json=_refusal_body("TOOL_BLOCKED", None)
            )
        )
        adapter, client = _adapter(_runtime(on_denied="message"))

        with pytest.raises(NullRunError) as exc_info:
            adapter.call_tool("create_issue", {"repo": "acme/api"})

        assert not isinstance(exc_info.value, NullRunDeniedError), (
            "an absent category must not be read as 'denied' — the flag "
            "is for a server that SAID denied"
        )
        assert client.calls == []
