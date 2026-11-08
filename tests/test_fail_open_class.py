"""tests/test_fail_open_class.py — the fail-open switch is bounded per tool (SDK-8).

The audit's finding, verbatim
(``RELIABILITY_AUDIT_2026-11-02_EVIDENCE.md`` §SDK-8):

    **SDK-8 — `sensitive_fail_open_enabled` is one global boolean.**
    *Location:* `src/nullrun/runtime.py` ≈L2159–2236. *Mechanism:* a
    single switch decides whether "sensitive" operations fail open, with
    no classification of which operation is sensitive. *Failure:*
    enabling it for one tool disables enforcement for **every** tool.
    *Verify:* enable it and check a non-sensitive tool — it must still
    fail closed. *Fix:* classify per tool; the switch is a kill-switch
    for the whole system, not a per-tool option.

What the SDK actually had
-------------------------

The switch was read in exactly ONE place — ``decorators.py``'s
``_run_tool_policy_gate``, on the local ``@protect`` path — and it was
read as a bare boolean: ``fail_open = runtime.sensitive_fail_open_enabled()``.
Turning it on therefore opened EVERY protected tool, whatever the tool
was. The MCP path never consulted it (it fails CLOSED through
``fallback_mode=STRICT``), so the switch's reach was "every ``@protect``
tool in the process".

The SDK *did* have a per-tool registry — ``is_sensitive_tool`` /
``add_sensitive_tool`` / ``register_sensitive_tools`` — but it answers a
DIFFERENT question: "is this tool destructive?". Its built-in list holds
25 names, four of which are plain Python identifiers
(``send_email``, ``send_sms``, ``send_slack``, ``send_discord``) and 21
of which are dotted MCP-style names that can never match a local
function's ``__name__``. Reusing it as the fail-open reach would have
made the SDK's own list of destructive operations an allowlist for
running without a policy decision.

The design (ADR-126)
---------------------

A NEW, explicitly-named classification bounds the switch:

* ``register_fail_open_tools(names)`` / ``remove_fail_open_tool(name)``
  / ``get_fail_open_tools()`` / ``is_fail_open_allowed(name)``;
* the set is EMPTY by default, so the switch alone opens NOTHING;
* the classification is CODE-level on purpose — it cannot be inherited
  by a deploy pipeline or a CI environment the way an env var can.

The switch alone now satisfies the audit's verify clause in its
strongest form: with it ON, a tool that is not classified still fails
CLOSED.

The doubles here are shallow — a mock MCP client and the real runtime
with the real transport — so a change that removes the classification
bound turns these red rather than being stubbed around.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from nullrun.toolbox.mcp import MCPAdapter

BASE_URL = "https://api.test.nullrun.io"
EXECUTE_URL = f"{BASE_URL}/api/v1/execute"
GATE_URL = f"{BASE_URL}/api/v1/gate"

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


# ---------------------------------------------------------------------------
# The classification itself
# ---------------------------------------------------------------------------


class TestTheClassificationIsEmptyByDefault:
    def test_a_fresh_runtime_classifies_nothing(self, make_runtime):
        rt = make_runtime(api_url=BASE_URL)
        assert rt.get_fail_open_tools() == set()
        assert rt.is_fail_open_allowed("charge_card") is False

    def test_the_lookup_does_not_read_the_environment(self, make_runtime, monkeypatch):
        """The switch being ON is not a classification. Two separate
        things, and the audit's finding was that they were one."""
        rt = make_runtime(api_url=BASE_URL)
        monkeypatch.setenv(_FLAG, "1")
        assert rt.is_fail_open_allowed("charge_card") is False

    def test_the_builtin_sensitive_list_is_not_a_fail_open_allowlist(self, make_runtime):
        """The sensitivity registry answers "is this destructive?"; the
        fail-open registry answers "may this run unchecked?". Merging
        them would put the SDK's own destructive-tool list into the
        allowlist for running without a policy decision."""
        rt = make_runtime(api_url=BASE_URL)

        assert rt.is_sensitive_tool("send_email") is True, (
            "precondition: the built-in registry still knows the "
            "destructive tools"
        )
        assert rt.is_fail_open_allowed("send_email") is False, (
            "a destructive tool must never be fail-open-allowed by "
            "default -- the two registries must stay separate"
        )


class TestRegisterAndRemove:
    def test_registering_makes_the_tool_allowed(self, make_runtime):
        rt = make_runtime(api_url=BASE_URL)
        rt.register_fail_open_tools(["charge_card"])
        assert rt.is_fail_open_allowed("charge_card") is True
        assert rt.get_fail_open_tools() == {"charge_card"}

    def test_registration_is_case_insensitive(self, make_runtime):
        """Same as the sensitivity registry: the tool's name comes from
        ``fn.__name__`` and the operator may write it either way."""
        rt = make_runtime(api_url=BASE_URL)
        rt.register_fail_open_tools(["Charge_Card"])
        assert rt.is_fail_open_allowed("charge_card") is True
        assert rt.is_fail_open_allowed("CHARGE_CARD") is True

    def test_removing_takes_the_tool_back_out(self, make_runtime):
        rt = make_runtime(api_url=BASE_URL)
        rt.register_fail_open_tools(["charge_card"])
        rt.remove_fail_open_tool("charge_card")
        assert rt.is_fail_open_allowed("charge_card") is False
        assert rt.get_fail_open_tools() == set()

    def test_removing_an_unclassified_tool_is_a_no_op(self, make_runtime):
        """``discard`` semantics: no KeyError on a tool that was never
        registered."""
        rt = make_runtime(api_url=BASE_URL)
        rt.remove_fail_open_tool("never_registered")
        assert rt.get_fail_open_tools() == set()

    def test_the_returned_set_is_a_copy(self, make_runtime):
        """A caller that mutates the returned set must not reach into
        the runtime's state."""
        rt = make_runtime(api_url=BASE_URL)
        rt.register_fail_open_tools(["charge_card"])

        snapshot = rt.get_fail_open_tools()
        snapshot.add("smuggle_in")

        assert rt.is_fail_open_allowed("smuggle_in") is False
        assert rt.get_fail_open_tools() == {"charge_card"}

    def test_the_classification_is_per_runtime(self, make_runtime):
        """Two runtimes are two operators' decisions. A shared
        class-level registry would leak one deployment's opt-out into
        another's."""
        first = make_runtime(api_url=BASE_URL)
        second = make_runtime(api_url=BASE_URL)

        first.register_fail_open_tools(["charge_card"])

        assert first.is_fail_open_allowed("charge_card") is True
        assert second.is_fail_open_allowed("charge_card") is False


# ---------------------------------------------------------------------------
# The switch + the classification, end to end on the @protect path
# ---------------------------------------------------------------------------


class TestTheTwoStepOptOut:
    def test_the_classification_alone_does_not_open_the_body(
        self, make_runtime, mock_api, monkeypatch
    ):
        """Classified, but the switch is OFF: the body must not run.

        The classification is the BOUND, not the grant. Without the
        grant the tool stays fail-CLOSED even though the operator
        named it."""
        from nullrun.breaker.exceptions import NullRunBlockedException
        from nullrun.decorators import protect

        rt = make_runtime(api_url=BASE_URL)
        rt.register_fail_open_tools(["charge_card"])

        respx.post(EXECUTE_URL).mock(
            side_effect=httpx.ConnectError("connection refused")
        )

        ran: list[int] = []

        @protect
        def charge_card(amount: int) -> str:
            ran.append(amount)
            return "charged"

        with pytest.raises(NullRunBlockedException):
            charge_card(100)

        assert ran == [], "the classification alone opened the tool"

    def test_switch_plus_classification_runs_the_body(
        self, make_runtime, mock_api, monkeypatch
    ):
        """The documented dev/test bypass: both steps together."""
        from nullrun.decorators import protect

        rt = make_runtime(api_url=BASE_URL)
        rt.register_fail_open_tools(["charge_card"])
        monkeypatch.setenv(_FLAG, "1")

        respx.post(EXECUTE_URL).mock(
            side_effect=httpx.ConnectError("connection refused")
        )

        ran: list[int] = []

        @protect
        def charge_card(amount: int) -> str:
            ran.append(amount)
            return "charged"

        charge_card(100)

        assert ran == [100]


# ---------------------------------------------------------------------------
# The MCP path
# ---------------------------------------------------------------------------


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
    def __init__(self, tools):
        self._tools = {t.name: t for t in tools}
        self.calls: list[tuple[str, dict]] = []

    def list_tools(self):
        return list(self._tools.values())

    def call_tool(self, name, arguments=None, **kwargs):
        self.calls.append((name, arguments or {}))
        return f"ok:{name}"


class TestTheMcpPathIsNotOpened:
    """The switch is a LOCAL-path switch. The MCP path fails CLOSED
    through ``fallback_mode=STRICT`` and never consults it — so the
    switch must not open the MCP path either, however the operator
    configures it."""

    def test_the_mcp_client_is_not_called_when_the_engine_is_unreachable(
        self, make_runtime, mock_api, monkeypatch
    ):
        rt = make_runtime(api_url=BASE_URL)
        rt.register_fail_open_tools(["create_issue"])
        monkeypatch.setenv(_FLAG, "1")

        # The pre-flight /gate answers (the conftest's mock); the
        # /execute the gate that decides the call is unreachable.
        respx.post(EXECUTE_URL).mock(
            side_effect=httpx.ConnectError("connection refused")
        )

        client = _MockMcpClient([_Tool("create_issue", _Ann(destructive=True))])
        adapter = MCPAdapter(server_name="github", mcp_client=client, runtime=rt)

        with pytest.raises(Exception):
            adapter.call_tool("create_issue", {"repo": "acme/api"})

        assert client.calls == [], (
            "the fail-open switch opened the MCP path -- the switch is "
            "consulted only on the local @protect path"
        )
