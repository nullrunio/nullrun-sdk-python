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
PROD_URL = "https://api.nullrun.io"
# A non-prod-looking host that is NOT one of the hosts
# `_is_production_environment` treats as a dev/staging escape hatch
# (localhost / 127.0.0.1 / staging / test), so `NULLRUN_ENV=production`
# is the only thing marking it production.
CUSTOM_URL = "https://nullrun.internal.example.com"

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


@pytest.fixture
def mock_prod_api(mock_api):
    """Mirror the conftest auth route onto the non-test hosts used here.

    `mock_api` only mocks `BASE_URL`, so a runtime built with any other
    `api_url` authenticates against an unmocked host and respx fails the
    test before the guard is ever consulted. Depends on `mock_api` so
    it registers inside that fixture's `with respx.mock:` context
    rather than opening a second one.

    Deliberately registers `/execute` for NO host: each test below
    needs `/execute` to fail, and a permissive default here would
    shadow the failure they are asserting on.
    """
    def _verify(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "organization_id": "ws-test",
                "workflow_id": "00000000-0000-0000-0000-000000000001",
                "plan": "pro",
                "features": [],
                "limits": {"max_cost_cents": 10000},
                "secret_key": "test-secret-deterministic",
            },
        )

    respx.post(f"{PROD_URL}/api/v1/auth/verify").mock(side_effect=_verify)
    respx.post(f"{CUSTOM_URL}/api/v1/auth/verify").mock(side_effect=_verify)
    return mock_api


class TestProductionGuard:
    """The flag alone must not open the gate against production."""

    def test_flag_ignored_in_prod_without_ack(self, make_runtime, mock_prod_api):
        monkey_api = make_runtime(api_url=PROD_URL)
        import os

        os.environ[_FLAG] = "1"
        assert monkey_api.sensitive_fail_open_enabled() is False

    def test_flag_honoured_in_prod_with_ack(self, make_runtime, mock_prod_api):
        rt = make_runtime(api_url=PROD_URL)
        import os

        os.environ[_FLAG] = "1"
        os.environ[_ACK] = "1"
        assert rt.sensitive_fail_open_enabled() is True

    def test_ack_alone_does_nothing(self, make_runtime, mock_prod_api):
        """The ack is a second signature, not a substitute."""
        rt = make_runtime(api_url=PROD_URL)
        import os

        os.environ[_ACK] = "1"
        assert rt.sensitive_fail_open_enabled() is False

    def test_flag_ignored_when_nullrun_env_is_production(self, make_runtime, mock_prod_api):
        """`NULLRUN_ENV=production` marks prod even on a custom host."""
        rt = make_runtime(api_url=CUSTOM_URL)
        import os

        os.environ["NULLRUN_ENV"] = "production"
        os.environ[_FLAG] = "1"
        assert rt.sensitive_fail_open_enabled() is False

    def test_flag_honoured_outside_prod(self, make_runtime):
        """The documented dev / test use keeps working."""
        rt = make_runtime(api_url=BASE_URL)
        import os

        os.environ[_FLAG] = "1"
        assert rt.sensitive_fail_open_enabled() is True

    def test_absent_flag_is_false(self, make_runtime):
        assert make_runtime(api_url=BASE_URL).sensitive_fail_open_enabled() is False

    def test_blank_flag_is_false(self, make_runtime):
        """Whitespace is not a yes. The `.strip()` matters."""
        rt = make_runtime(api_url=BASE_URL)
        import os

        os.environ[_FLAG] = "  "
        assert rt.sensitive_fail_open_enabled() is False


class TestEndToEndAgainstProductionUrl:
    """The guard must hold on the real enforcement path, not just the
    predicate.

    This is the test that would have caught the original defect: the
    helper can be right while the caller still reads the raw env var.
    """

    def test_sensitive_body_does_not_run_in_prod(self, make_runtime, mock_prod_api):
        """With /execute unreachable, the body must NOT run in prod
        even though the operator set the flag."""
        from nullrun.decorators import protect

        rt = make_runtime(api_url=PROD_URL)
        import os

        os.environ[_FLAG] = "1"

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

    def test_sensitive_body_runs_in_dev_with_flag(self, make_runtime, mock_api):
        """The documented bypass still works off production."""
        from nullrun.breaker.exceptions import NullRunBlockedException
        from nullrun.decorators import protect

        rt = make_runtime(api_url=BASE_URL)
        import os

        os.environ[_FLAG] = "1"

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
