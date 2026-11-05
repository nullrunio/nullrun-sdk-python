"""``NULLRUN_SENSITIVE_FAIL_OPEN`` must be refused in production.

The opt-out lets a sensitive tool's body run while the policy engine is
unreachable. Pre-fix it was read straight into the enforcement path:

    fail_open = os.environ.get("NULLRUN_SENSITIVE_FAIL_OPEN", "") == "1"

Its sibling ``NULLRUN_SKIP_BUDGET_CHECK`` has been production-guarded
since it was caught doing the same thing. The asymmetry was an
oversight, and it is the more dangerous half: the budget opt-out skips
a *pre-flight*, while this one lets ``charge_card`` run with no
policy evaluation at all. ADR-063 §2.4 calls that a security regression
rather than an availability trade-off.

The guard refuses the bypass rather than raising: enforcement falls
back to its own fail-CLOSED default, so an operator who set the var
carelessly keeps a working agent instead of a crash loop, and the
attempt is logged at ERROR with a metric rather than passing
unnoticed.

**SDK-8 (2026-11-05) — the switch is now bounded per tool.** The guard
above answered "may this switch be used against production?", but the
switch itself was a single global boolean: turning it on opened EVERY
protected tool, so enabling it for one tool disabled enforcement for
all of them. ADR-126 bounds the switch with an explicit per-tool
classification (``runtime.register_fail_open_tools([...])``): the
switch may open a tool ONLY if that tool was classified, and the
classification is empty by default. So the switch alone opens nothing
— which is what the two tests below pin.

Every test below fails against the pre-fix code.
"""

from __future__ import annotations

import httpx
import pytest
import respx

BASE_URL = "https://api.test.nullrun.io"
EXECUTE_URL = f"{BASE_URL}/api/v1/execute"
from tests.conftest import CUSTOM_NONPROD_URL as CUSTOM_URL  # noqa: E402
from tests.conftest import PROD_URL  # noqa: E402

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


class TestProductionGuard:
    """The flag alone must not open the gate against production."""

    def test_flag_ignored_in_prod_without_ack(self, make_runtime, mock_prod_api, monkeypatch):
        monkey_api = make_runtime(api_url=PROD_URL)
        monkeypatch.setenv(_FLAG, "1")
        assert monkey_api.sensitive_fail_open_enabled() is False

    def test_flag_honoured_in_prod_with_ack(self, make_runtime, mock_prod_api, monkeypatch):
        rt = make_runtime(api_url=PROD_URL)
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setenv(_ACK, "1")
        assert rt.sensitive_fail_open_enabled() is True

    def test_ack_alone_does_nothing(self, make_runtime, mock_prod_api, monkeypatch):
        """The ack is a second signature, not a substitute."""
        rt = make_runtime(api_url=PROD_URL)
        monkeypatch.setenv(_ACK, "1")
        assert rt.sensitive_fail_open_enabled() is False

    def test_flag_ignored_when_nullrun_env_is_production(self, make_runtime, mock_prod_api, monkeypatch):
        """`NULLRUN_ENV=production` marks prod even on a custom host."""
        rt = make_runtime(api_url=CUSTOM_URL)
        monkeypatch.setenv("NULLRUN_ENV", "production")
        monkeypatch.setenv(_FLAG, "1")
        assert rt.sensitive_fail_open_enabled() is False

    def test_flag_honoured_outside_prod(self, make_runtime, monkeypatch):
        """The documented dev / test use keeps working."""
        rt = make_runtime(api_url=BASE_URL)
        monkeypatch.setenv(_FLAG, "1")
        assert rt.sensitive_fail_open_enabled() is True

    def test_absent_flag_is_false(self, make_runtime, monkeypatch):
        assert make_runtime(api_url=BASE_URL).sensitive_fail_open_enabled() is False

    def test_blank_flag_is_false(self, make_runtime, monkeypatch):
        """Whitespace is not a yes. The `.strip()` matters."""
        rt = make_runtime(api_url=BASE_URL)
        monkeypatch.setenv(_FLAG, "  ")
        assert rt.sensitive_fail_open_enabled() is False


class TestEndToEndAgainstProductionUrl:
    """The guard must hold on the real enforcement path, not just the
    predicate.

    This is the test that would have caught the original defect: the
    helper can be right while the caller still reads the raw env var.
    """

    def test_sensitive_body_does_not_run_in_prod(self, make_runtime, mock_prod_api, monkeypatch):
        """With /execute unreachable, the body must NOT run in prod
        even though the operator set the flag."""
        from nullrun.decorators import protect

        # Not bound: `@protect` resolves the runtime from the context,
        # so the constructor call is the load-bearing part here.
        make_runtime(api_url=PROD_URL)
        monkeypatch.setenv(_FLAG, "1")

        respx.post(f"{PROD_URL}/api/v1/execute").mock(
            side_effect=httpx.ConnectError("connection refused")
        )

        ran: list[int] = []

        @protect
        def charge_card(amount: int) -> str:
            ran.append(amount)
            return "charged"

        with pytest.raises(Exception):
            charge_card(100)

        assert ran == [], (
            "sensitive body ran while the policy engine was unreachable, "
            "with NULLRUN_SENSITIVE_FAIL_OPEN=1 set against production"
        )

    def test_sensitive_body_runs_in_dev_with_flag(self, make_runtime, mock_api, monkeypatch):
        """The documented bypass still works off production — but it is
        a TWO-STEP opt-out now (SDK-8 / ADR-126): the switch AND the
        per-tool classification.

        The assertion is unchanged: the body ran. What changed is the
        setup, because "the switch alone opens every tool" was the
        defect.
        """
        from nullrun.breaker.exceptions import NullRunBlockedException
        from nullrun.decorators import protect

        # Not bound, for the same reason as the prod case above.
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

        # The bypass is a `return`, not a swallow of a raised error --
        # whichever the transport does, the observable contract is that
        # the body ran. Guard against the opposite regression too.
        try:
            charge_card(100)
        except (NullRunBlockedException, Exception):
            pass

        assert ran == [100], (
            "the documented dev/test bypass stopped working -- "
            "NULLRUN_SENSITIVE_FAIL_OPEN=1 against a non-prod api_url "
            "must let the body run"
        )


class TestTheSwitchAloneOpensNothing:
    """SDK-8 / ADR-126 — the switch is bounded per tool.

    The audit's verify clause, verbatim: *"enable it and check a
    non-sensitive tool — it must still fail closed."* Here the bound
    is stronger than the clause asks for: with the switch ON and NO
    classification, EVERY tool fails closed — sensitive or not.
    """

    def test_the_switch_alone_does_not_run_the_body(self, make_runtime, mock_api, monkeypatch):
        """The switch ON, the tool NOT classified: the body must not
        run. This is the defect the audit found, restated as a test."""
        from nullrun.breaker.exceptions import NullRunBlockedException
        from nullrun.decorators import protect

        make_runtime(api_url=BASE_URL)
        monkeypatch.setenv(_FLAG, "1")

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

        assert ran == [], (
            "the switch alone opened the tool -- the switch must be "
            "bounded by the per-tool classification "
            "(runtime.register_fail_open_tools)"
        )

    def test_a_classified_tool_does_not_open_another_tool(
        self, make_runtime, mock_api, monkeypatch
    ):
        """Classifying tool A must not open tool B. The old switch had
        no notion of "which tool" at all."""
        from nullrun.breaker.exceptions import NullRunBlockedException
        from nullrun.decorators import protect

        rt = make_runtime(api_url=BASE_URL)
        rt.register_fail_open_tools(["charge_card"])
        monkeypatch.setenv(_FLAG, "1")

        respx.post(EXECUTE_URL).mock(
            side_effect=httpx.ConnectError("connection refused")
        )

        ran: list[int] = []

        @protect
        def read_balance(account: str) -> str:
            ran.append(account)
            return "42"

        with pytest.raises(NullRunBlockedException):
            read_balance("acc-1")

        assert ran == [], (
            "classifying one tool opened a DIFFERENT tool -- the "
            "classification is per tool, not a global permission"
        )

    def test_the_fail_closed_message_names_both_steps(self, make_runtime, mock_api, monkeypatch):
        """The operator who wants the bypass must be told BOTH steps, or
        they will set the env var, see no effect, and file a bug."""
        from nullrun.breaker.exceptions import NullRunBlockedException
        from nullrun.decorators import protect

        make_runtime(api_url=BASE_URL)
        monkeypatch.setenv(_FLAG, "1")

        respx.post(EXECUTE_URL).mock(
            side_effect=httpx.ConnectError("connection refused")
        )

        @protect
        def charge_card(amount: int) -> str:
            return "charged"

        with pytest.raises(NullRunBlockedException) as exc:
            charge_card(100)

        message = exc.value.user_action
        assert "NULLRUN_SENSITIVE_FAIL_OPEN" in message
        assert "register_fail_open_tools" in message, (
            "the refusal must name the per-tool classification -- "
            "the switch alone opens nothing"
        )

    def test_prod_opens_only_the_classified_tool(self, make_runtime, mock_prod_api, monkeypatch):
        """In production the switch needs the ack as well (row 9 of
        ADR-063 §2.4), and even then it opens ONLY the classified
        tool. So the prod reach is strictly narrower than it was
        before SDK-8: the ack used to unlock EVERY tool."""
        from nullrun.breaker.exceptions import NullRunBlockedException
        from nullrun.decorators import protect

        rt = make_runtime(api_url=PROD_URL)
        rt.register_fail_open_tools(["charge_card"])
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setenv(_ACK, "1")

        respx.post(f"{PROD_URL}/api/v1/execute").mock(
            side_effect=httpx.ConnectError("connection refused")
        )

        ran: list[int] = []

        @protect
        def read_balance(account: str) -> str:
            ran.append(account)
            return "42"

        with pytest.raises(NullRunBlockedException):
            read_balance("acc-1")

        assert ran == [], (
            "in production, the double-ack opened a tool that was NOT "
            "classified -- the reach is bounded per tool there too"
        )
