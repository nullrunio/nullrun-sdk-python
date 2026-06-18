"""
Regression test for the legacy-API-key kill-switch warning.

Pre-Phase-139 API keys do not return ``workflow_id`` from
``/auth/verify``. When the SDK has no workflow bound, every
``check_control_plane`` call is a silent no-op — the dashboard's
KILL/PAUSE button has no effect on the running agent. This is a
real safety hole for users on legacy keys.

The fix in 0.3.1: when ``_authenticate`` sees a missing
``workflow_id``, the runtime emits a one-time WARNING with a
clear message. This test pins the contract.
"""
from __future__ import annotations

import logging

import respx
from httpx import Response

from nullrun.runtime import NullRunRuntime

BASE_URL = "https://api.test.nullrun.io"


class TestLegacyApiKeyWarning:

    def test_legacy_key_emits_kill_switch_warning(
        self, monkeypatch, caplog
    ):
        """A pre-Phase-139 key (no workflow_id in auth response)
        must emit a WARNING explaining that kill/pause will not
        be honoured.
        """
        monkeypatch.setenv("NULLRUN_USE_GRPC", "")
        with respx.mock:
            respx.post(f"{BASE_URL}/api/v1/auth/verify").mock(
                return_value=Response(
                    200,
                    json={
                        "organization_id": "00000000-0000-0000-0000-000000000000",
                        # NO workflow_id — pre-Phase-139 key
                        "plan": "pro",
                        "features": [],
                        "limits": {"max_cost_cents": 10000},
                    },
                )
            )
            respx.post(f"{BASE_URL}/api/v1/policies").mock(
                return_value=Response(200, json=[{
                    "budget_cents": 1000,
                    "rate_limit": 100,
                    "loop_threshold": 6,
                    "retry_threshold": 5,
                }])
            )
            with caplog.at_level(logging.WARNING, logger="nullrun.runtime"):
                rt = NullRunRuntime(
                    api_key="legacy-key-12345",
                    api_url=BASE_URL,
                    polling=False,
                )
            assert rt.workflow_id is None
            warning_records = [
                r for r in caplog.records
                if r.levelno == logging.WARNING
                and r.name == "nullrun.runtime"
            ]
            assert any(
                "legacy key" in r.getMessage()
                and "kill/pause" in r.getMessage()
                for r in warning_records
            ), (
                "Expected a WARNING from nullrun.runtime mentioning "
                "legacy key + kill/pause. Got: "
                f"{[(r.levelname, r.getMessage()) for r in caplog.records]}"
            )
            rt.shutdown()
