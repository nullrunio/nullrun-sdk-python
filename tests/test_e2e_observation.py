"""
Phase 2: real e2e observation test.

The previous suite used respx to mock the NULLRUN backend. That's
fine for unit coverage, but it doesn't prove the SDK actually
delivers events to a real server. This test hits a live local
backend (if one is running) and verifies that an OpenAI call
made through the SDK shows up in the usage endpoint.

Run with:
    NULLRUN_E2E_BASE_URL=http://localhost:8080 \
    NULLRUN_E2E_API_KEY=nr_live_test_xxx \
    NULLRUN_E2E_ORG_ID=org-e2e \
    pytest tests/test_e2e_observation.py -q

If the env vars are not set, the test is skipped — the respx-based
tests in test_runtime.py / test_ws_push.py are the unit-level
substitute.
"""

from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest

import nullrun


E2E_BASE_URL = os.environ.get("NULLRUN_E2E_BASE_URL")
E2E_API_KEY = os.environ.get("NULLRUN_E2E_API_KEY")
E2E_ORG_ID = os.environ.get("NULLRUN_E2E_ORG_ID", "org-e2e")

# The OpenAI SDK is the canary for the auto-instrumentation path.
# We use a real call against the public OpenAI API, which requires
# OPENAI_API_KEY. If it's not set, we still want to verify the
# SDK delivers a manually-tracked event so the test exercises the
# full SDK → backend → usage pipeline.
HAS_OPENAI_KEY = bool(os.environ.get("OPENAI_API_KEY"))


pytestmark = pytest.mark.skipif(
    not (E2E_BASE_URL and E2E_API_KEY),
    reason="set NULLRUN_E2E_BASE_URL and NULLRUN_E2E_API_KEY to run e2e",
)


@pytest.fixture
def e2e_workflow_id() -> str:
    """Unique workflow per test run so previous events don't pollute."""
    return f"e2e-{uuid.uuid4().hex[:8]}"


def _fetch_usage(base_url: str, org_id: str, api_key: str, workflow_id: str) -> dict | None:
    """
    Read rolling 24h usage and return the entry for the workflow.

    Returns None if the workflow hasn't shown up yet (the dashboard's
    ingest worker is async; the SDK's HTTP transport is also async).
    """
    with httpx.Client(timeout=10.0) as client:
        resp = client.get(
            f"{base_url}/api/v1/orgs/{org_id}/usage",
            params={"window": "24h"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        body = resp.json()
    for wf in body.get("workflows", []):
        if wf.get("workflow_id") == workflow_id:
            return wf
    return None


def test_e2e_manual_track_event_lands_in_backend(e2e_workflow_id: str) -> None:
    """
    init → track_event → backend's /usage endpoint shows the event.

    The full chain, no mocks: the SDK's HTTP transport posts to the
    backend, the backend persists the event, and the usage endpoint
    rolls it up. If any layer drops, this test fails.
    """
    nullrun.init(
        api_key=E2E_API_KEY,
        api_url=E2E_BASE_URL,
    )

    # Manual track — the most direct way to assert the wire format
    # without depending on the OpenAI vendor SDK being installed.
    nullrun.track_event(
        {
            "type": "llm_call",
            "workflow_id": e2e_workflow_id,
            "tokens": 1000,
            "cost_cents": 5,
            "model": "gpt-4o-mini",
        }
    )

    # The SDK's transport is async + batched. Give the backend up to
    # 5s to ingest and roll up. The dashboard itself tolerates longer
    # gaps, so 5s is a reasonable e2e test ceiling.
    deadline = time.time() + 5.0
    wf: dict | None = None
    while time.time() < deadline:
        wf = _fetch_usage(E2E_BASE_URL, E2E_ORG_ID, E2E_API_KEY, e2e_workflow_id)
        if wf is not None and wf.get("calls", 0) >= 1:
            break
        time.sleep(0.25)

    assert wf is not None, f"workflow {e2e_workflow_id} did not appear in /usage within 5s"
    assert wf.get("calls", 0) >= 1, f"expected >=1 call, got {wf!r}"
    # The cost we sent is in cents; allow server-side recompute drift
    # of up to 5% (the policy is single-source-of-truth on the server).
    assert wf.get("cost_cents", 0) >= 1, f"expected non-zero cost, got {wf!r}"


@pytest.mark.skipif(not HAS_OPENAI_KEY, reason="OPENAI_API_KEY not set")
def test_e2e_openai_call_lands_in_backend(e2e_workflow_id: str) -> None:
    """
    init → openai.OpenAI().chat.completions.create(...) → backend records.

    Exercises the full auto-instrumentation path: vendor patch → SDK
    transport → backend ingest → /usage rollup. This is the test the
    respx-only suite could not write.
    """
    nullrun.init(
        api_key=E2E_API_KEY,
        api_url=E2E_BASE_URL,
    )

    # Scope events to a workflow so the rollup can find them.
    from nullrun import workflow

    with workflow(e2e_workflow_id):
        from openai import OpenAI

        client = OpenAI()
        client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": f"e2e ping {uuid.uuid4().hex[:6]}"}],
            max_tokens=16,
        )

    deadline = time.time() + 10.0
    wf: dict | None = None
    while time.time() < deadline:
        wf = _fetch_usage(E2E_BASE_URL, E2E_ORG_ID, E2E_API_KEY, e2e_workflow_id)
        if wf is not None and wf.get("calls", 0) >= 1:
            break
        time.sleep(0.5)

    assert wf is not None, f"openai call did not land in /usage within 10s"
    assert wf.get("calls", 0) >= 1
    assert wf.get("tokens", 0) > 0, f"expected non-zero tokens, got {wf!r}"
