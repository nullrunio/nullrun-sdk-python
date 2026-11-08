"""SDK-2 — the production detector must not be a substring test.

`_is_production_environment` decides whether the two fail-open guards
(`NULLRUN_SKIP_BUDGET_CHECK`, `NULLRUN_SENSITIVE_FAIL_OPEN`) arm. Before
the fix it asked "is any marker a SUBSTRING of the hostname?". `"test"`
is a substring of `latest`, `contest`, `attest`, `greatest`; `"localhost"`
is a substring of `notlocalhost.example.com`. So a host that merely
CONTAINED a dev marker was classified non-prod, and the operator's
`NULLRUN_ENV=production` was suppressed by the substring match. The
billing bypass then ran against production with a WARNING instead of a
refusal (L1 / L4).

The rule after the fix: a host is non-prod only when it is EXACTLY a
loopback / a `.localhost` suffix, or when it carries an explicit
dev-environment LABEL (`staging.`, `test.`). Never by substring.

The table below is the audit's, evaluated in the context the audit's
exploit skeleton uses -- the operator declared production and the host
merely looks dev:

    latest.nullrun.io        -> production
    contest.nullrun.io       -> production
    notlocalhost.example.com -> production
    attest.example.com       -> production
    localhost                -> NOT production
    127.0.0.1                -> NOT production
    staging.internal         -> NOT production

The last check is the one that matters for the money: with the flag
set against a prod-classified URL the SDK must RAISE NR-S001, not log a
warning.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from nullrun.runtime import _is_production_environment

# A prod-classified host the SDK's own fixtures do not mock.
PROD_LIKE_URL = "https://latest.nullrun.io"

# Hosts the audit names. Every one of them CONTAINS a dev marker as a
# substring; none of them carries one as a label.
PROD_HOSTS = [
    "latest.nullrun.io",
    "contest.nullrun.io",
    "notlocalhost.example.com",
    "attest.example.com",
]

# Hosts that really are local / dev.
NON_PROD_HOSTS = [
    "localhost",
    "127.0.0.1",
    "staging.internal",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """A developer's shell must not decide whether these pass."""
    monkeypatch.delenv("NULLRUN_ENV", raising=False)
    monkeypatch.delenv("NULLRUN_API_URL", raising=False)


@pytest.fixture
def mock_prod_like_api(mock_api):
    """The auth route for the prod-like host used below.

    `mock_api` only covers `api.test.nullrun.io`, so a runtime built
    with any other `api_url` authenticates against an unmocked host and
    respx fails the test before the guard under test is consulted.
    """

    def _verify(request) -> httpx.Response:
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

    respx.post(f"{PROD_LIKE_URL}/api/v1/auth/verify").mock(side_effect=_verify)
    return mock_api


class TestTheAuditTable:
    """The operator declared production. The host must not veto it."""

    @pytest.mark.parametrize("host", PROD_HOSTS)
    def test_a_marker_inside_the_name_does_not_veto_production(
        self, monkeypatch, host
    ):
        monkeypatch.setenv("NULLRUN_ENV", "production")
        assert _is_production_environment(f"https://{host}") is True, (
            f"{host} was classified non-prod -- a dev marker found as a "
            "SUBSTRING of the hostname suppressed NULLRUN_ENV=production"
        )

    @pytest.mark.parametrize("host", NON_PROD_HOSTS)
    def test_a_real_dev_host_stays_non_prod(self, monkeypatch, host):
        monkeypatch.setenv("NULLRUN_ENV", "production")
        assert _is_production_environment(f"https://{host}") is False, (
            f"{host} was classified production -- the local / staging "
            "escape hatch must keep working"
        )


class TestTheCanonicalProdHost:
    def test_the_default_host_is_production(self):
        assert _is_production_environment("https://api.nullrun.io") is True

    def test_no_api_url_uses_the_default_host(self):
        """The constructor's default is the prod endpoint."""
        assert _is_production_environment(None) is True

    def test_the_api_url_env_var_is_used_when_no_url_is_given(self, monkeypatch):
        monkeypatch.setenv("NULLRUN_API_URL", "https://latest.nullrun.io")
        monkeypatch.setenv("NULLRUN_ENV", "production")
        assert _is_production_environment(None) is True

    def test_a_trailing_slash_does_not_change_the_verdict(self):
        assert _is_production_environment("https://api.nullrun.io/") is True


class TestNoSubstringMatching:
    """Each row is a host whose name CONTAINS a marker as a substring."""

    @pytest.mark.parametrize(
        "host",
        [
            "latest.nullrun.io",
            "greatest.nullrun.io",
            "protest.nullrun.io",
            "notlocalhost.example.com",
            "mytesthost.internal",
            "attest.example.com",
            "contest.nullrun.io",
        ],
    )
    def test_a_marker_inside_a_label_is_not_a_dev_marker(self, monkeypatch, host):
        monkeypatch.setenv("NULLRUN_ENV", "production")
        assert _is_production_environment(f"https://{host}") is True

    @pytest.mark.parametrize(
        "host",
        [
            "localhost",
            "127.0.0.1",
            "0.0.0.0",
            "foo.localhost",
            "staging.internal",
            "test.internal",
            "staging.example.com",
        ],
    )
    def test_an_exact_or_labelled_dev_host_is_non_prod(self, monkeypatch, host):
        monkeypatch.setenv("NULLRUN_ENV", "production")
        assert _is_production_environment(f"https://{host}") is False


class TestProdWinsOverUnknownHosts:
    """A host the SDK does not know is still production when the
    operator says so -- the guard must not stay disarmed because the
    SDK failed to recognise the deployment."""

    def test_an_unknown_host_is_production_when_declared(self, monkeypatch):
        monkeypatch.setenv("NULLRUN_ENV", "production")
        assert _is_production_environment("https://nullrun.acme.example") is True

    def test_an_unknown_host_is_non_prod_without_a_declaration(self):
        """The pre-existing behaviour for a custom host is unchanged:
        it needs an explicit declaration to arm the guard."""
        assert _is_production_environment("https://nullrun.acme.example") is False


class TestTheGuardRaisesAgainstAProdUrl:
    """The integration half: the predicate must arm the real guard."""

    def test_skip_budget_check_raises_nr_s001_against_a_prod_url(
        self, make_runtime, mock_prod_like_api, monkeypatch
    ):
        from nullrun.breaker.exceptions import NullRunInfrastructureError

        monkeypatch.setenv("NULLRUN_ENV", "production")
        rt = make_runtime(api_url=PROD_LIKE_URL)
        monkeypatch.setenv("NULLRUN_SKIP_BUDGET_CHECK", "1")

        with pytest.raises(NullRunInfrastructureError) as exc:
            rt.check_workflow_budget()

        assert exc.value.error_code == "NR-S001"

    def test_the_sensitive_fail_open_switch_is_refused_against_a_prod_url(
        self, make_runtime, mock_prod_like_api, monkeypatch
    ):
        monkeypatch.setenv("NULLRUN_ENV", "production")
        rt = make_runtime(api_url=PROD_LIKE_URL)
        monkeypatch.setenv("NULLRUN_SENSITIVE_FAIL_OPEN", "1")

        assert rt.sensitive_fail_open_enabled() is False
