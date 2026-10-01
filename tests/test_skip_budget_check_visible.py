"""A budget gate that was never consulted must not be invisible.

`NULLRUN_SKIP_BUDGET_CHECK=1` disables the pre-flight entirely. In
production the SDK already refuses it without
`NULLRUN_ALLOW_SKIP_BUDGET_CHECK=1`. Outside production it was a
silent `logger.debug` plus a bare `return` -- so a test suite running
the whole budget path with the bypass on left no trace at all: the
tests went green, the dashboard showed the org spending nothing, and
the only evidence the gate had never been consulted was the absence of
a block.

CLAUDE.md is explicit that a test which only passes with this flag set
is evidence of a broken gate, not evidence of a working one. For that
rule to be actionable the flag setting has to be loud enough to find.

These tests fail against the pre-fix code, which logged at DEBUG and
emitted nothing.
"""

from __future__ import annotations

import logging

import pytest

BASE_URL = "https://api.test.nullrun.io"
from tests.conftest import PROD_URL  # noqa: E402

_FLAG = "NULLRUN_SKIP_BUDGET_CHECK"
_ACK = "NULLRUN_ALLOW_SKIP_BUDGET_CHECK"


@pytest.fixture(autouse=True)
def _clean_flag_env(monkeypatch):
    monkeypatch.delenv(_FLAG, raising=False)
    monkeypatch.delenv(_ACK, raising=False)
    monkeypatch.delenv("NULLRUN_ENV", raising=False)
    monkeypatch.delenv("NULLRUN_API_URL", raising=False)


class TestNonProdSkipIsLoud:
    def test_logs_at_warning(self, make_runtime, mock_api, caplog, monkeypatch):
        """DEBUG is the level a developer's default handler drops."""
        rt = make_runtime(api_url=BASE_URL)
        monkeypatch.setenv(_FLAG, "1")

        with caplog.at_level(logging.DEBUG, logger="nullrun.runtime"):
            rt.check_workflow_budget()

        warnings = [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert warnings, (
            "bypassing the budget gate emitted no WARNING -- the skip is "
            "invisible at any default log level"
        )
        assert any(
            "NULLRUN_SKIP_BUDGET_CHECK" in r.getMessage() for r in warnings
        ), f"the WARNING does not name the flag: {[r.getMessage() for r in warnings]}"

    def test_warning_says_what_did_not_run(self, make_runtime, mock_api, caplog, monkeypatch):
        """The message must say the check was skipped, not just that a
        flag was set. 'BYPASSED ... no budget check ... ran' is
        actionable; 'skipping' reads like a normal fast path."""
        rt = make_runtime(api_url=BASE_URL)
        monkeypatch.setenv(_FLAG, "1")

        with caplog.at_level(logging.DEBUG, logger="nullrun.runtime"):
            rt.check_workflow_budget()

        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "BYPASSED" in text
        assert "No budget check" in text, (
            "the warning does not state that no check ran, so a reader "
            "cannot tell a bypass from a normal allow"
        )

    def test_increments_a_counter(self, make_runtime, mock_api, monkeypatch):
        """Silent in the logs is not enough; it must be graphable.

        The production refusal already emits
        `skip_budget_blocked_in_prod`. The dev/test path needs its own
        counter, because "bypass active in non-prod" is a signal a
        CI dashboard can alert on, which no log line provides.
        """
        from nullrun.observability import metrics

        rt = make_runtime(api_url=BASE_URL)
        monkeypatch.setenv(_FLAG, "1")

        before = getattr(metrics.runtime, "skip_budget_used_non_prod", 0)
        rt.check_workflow_budget()
        after = getattr(metrics.runtime, "skip_budget_used_non_prod", 0)

        assert after == before + 1, (
            "skip_budget_used_non_prod was not incremented -- the "
            "non-prod bypass is still unobservable in metrics"
        )


class TestProductionRefusalUnchanged:
    """The guard from the previous change must not have been softened
    by making the dev path louder."""

    def test_prod_still_raises_without_ack(self, make_runtime, mock_prod_api, monkeypatch):
        from nullrun.breaker.exceptions import NullRunInfrastructureError

        rt = make_runtime(api_url=PROD_URL)
        monkeypatch.setenv(_FLAG, "1")

        with pytest.raises(NullRunInfrastructureError):
            rt.check_workflow_budget()
