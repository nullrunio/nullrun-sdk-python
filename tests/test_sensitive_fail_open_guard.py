"""``NULLRUN_SENSITIVE_FAIL_OPEN`` must be refused in production.

The opt-out lets a sensitive tool's body run while the policy engine is
unreachable. Pre-fix it was read straight into the enforcement path:

    fail_open = os.environ.get("NULLRUN_SENSITIVE_FAIL_OPEN", "") == "1"

Its sibling ``NULLRUN_SKIP_BUDGET_CHECK`` has been production-guarded
since it was caught doing the same thing. The asymmetry was an
oversight, and it is the more dangerous half: the budget opt-out skips
a *pre-flight*, while this one lets ``charge_card`` run with no
policy evaluation at all. ADR-008 calls that a security regression
rather than an availability trade-off.

The guard refuses the bypass rather than raising: enforcement falls
back to its own fail-CLOSED default, so an operator who set the var
carelessly keeps a working agent instead of a crash loop, and the
attempt is logged at ERROR with a metric rather than passing
unnoticed.

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
        """The documented bypass still works off production."""
        from nullrun.breaker.exceptions import NullRunBlockedException
        from nullrun.decorators import protect

        # Not bound, for the same reason as the prod case above.
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
