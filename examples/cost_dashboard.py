"""
Phase 2 example — read live cost from the dashboard.

NULLRUN is the single source of truth for AI workflow budgets: the
dashboard's policy wins, never a `max_cost=` kwarg. This example
prints the spend for the last 24 hours of one workflow so the user
can see that the SDK and the dashboard agree.

Run:
    pip install -e .
    export NULLRUN_API_KEY=nr_live_...
    python examples/cost_dashboard.py
"""

import os

import httpx
import nullrun


def fetch_last_24h_spend(api_url: str, org_id: str, api_key: str, workflow_id: str) -> dict:
    """
    Read the rolling 24h spend for one workflow from the backend.

    The canonical endpoint is `/api/v1/orgs/{org_id}/quota` (per
    `contracts/openapi.yaml:2306-2321`). The legacy `/usage` path
    was removed when the dashboard migrated to the unified
    `OrgStatusResponse` shape; this example uses the
    dashboard-friendly status endpoint and projects a 24h window
    from the `usage_today_cents` field.

    Authentication: ``X-API-Key`` header (per
    `contracts/openapi.yaml:59-74`). The SDK never sends a
    ``Authorization: Bearer`` token on the user's behalf.
    """
    headers = {"X-API-Key": api_key}
    with httpx.Client(timeout=10.0) as client:
        resp = client.get(
            f"{api_url}/api/v1/orgs/{org_id}/quota",
            headers=headers,
        )
        resp.raise_for_status()
        body = resp.json()

    return {
        "workflow_id": workflow_id,
        "cost_cents": body.get("usage_today_cents", 0),
        "tokens": body.get("tokens_today", 0),
        "calls": body.get("calls_today", 0),
        "budget_remaining_cents": body.get("budget_remaining_cents"),
    }


def main() -> None:
    api_url = os.environ.get("NULLRUN_API_URL", "https://api.nullrun.io")
    api_key = os.environ.get("NULLRUN_API_KEY", "nr_live_demo_key")
    workflow_id = os.environ.get("NULLRUN_WORKFLOW_ID", "research-agent")

    nullrun.init(
        api_key=api_key,
        api_url=api_url,
    )

    # Organization ID is returned by /auth/verify on init and is
    # available on the runtime singleton — fetch it after init.
    from nullrun import get_runtime
    org_id = get_runtime().organization_id or "unknown"

    print(f"Reading today for workflow {workflow_id!r} in org {org_id!r}...")
    wf = fetch_last_24h_spend(api_url, org_id, api_key, workflow_id)

    cost_dollars = wf.get("cost_cents", 0) / 100.0
    print(f"  cost:   ${cost_dollars:,.2f}")
    print(f"  tokens: {wf.get('tokens', 0):,}")
    print(f"  calls:  {wf.get('calls', 0):,}")
    if wf.get("budget_remaining_cents") is not None:
        remaining = wf["budget_remaining_cents"] / 100.0
        print(f"  remaining budget: ${remaining:,.2f}")

    # The same number is the truth the dashboard shows — there is no
    # second source of truth in code. The policy in the Control
    # Plane decides the budget; the SDK just records spend.
    print(
        "\nBudgets live in the Control Plane (UI/policy), not in code. "
        "Edit the workflow's policy in the dashboard to change the cap."
    )


if __name__ == "__main__":
    main()
