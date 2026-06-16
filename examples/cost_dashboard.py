"""
Phase 2 example — read live cost from the dashboard.

NULLRUN is the single source of truth for AI workflow budgets: the
dashboard's policy wins, never a `max_cost=` kwarg. This example
prints the spend for the last 24 hours of one workflow so the user
can see that the SDK and the dashboard agree.

Run:
    pip install -e ../sdk-python
    export NULLRUN_API_KEY=nr_live_...
    export NULLRUN_ORGANIZATION_ID=org-123
    python cost_dashboard.py
"""

import os

import httpx
import nullrun


def fetch_last_24h_spend(api_url: str, org_id: str, api_key: str, workflow_id: str) -> dict:
    """
    Read the rolling 24h spend for one workflow from the backend.

    The backend exposes this as `/api/v1/orgs/{org_id}/usage`. The
    response shape is `{"workflows": [{...}], "totals": {...}}` —
    filter to the workflow of interest on the client side because
    the server-side filter is a Phase 4 follow-up.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(timeout=10.0) as client:
        resp = client.get(
            f"{api_url}/api/v1/orgs/{org_id}/usage",
            params={"window": "24h"},
            headers=headers,
        )
        resp.raise_for_status()
        body = resp.json()

    for wf in body.get("workflows", []):
        if wf.get("workflow_id") == workflow_id:
            return wf

    return {
        "workflow_id": workflow_id,
        "cost_cents": 0,
        "tokens": 0,
        "calls": 0,
        "note": "no events in window",
    }


def main() -> None:
    api_url = os.environ.get("NULLRUN_API_URL", "http://localhost:8080")
    org_id = os.environ.get("NULLRUN_ORGANIZATION_ID", "org-demo")
    api_key = os.environ.get("NULLRUN_API_KEY", "demo-key")
    workflow_id = os.environ.get("NULLRUN_WORKFLOW_ID", "research-agent")

    nullrun.init(
        organization_id=org_id,
        api_key=api_key,
        api_url=api_url,
    )

    print(f"Reading last 24h for workflow {workflow_id!r} in org {org_id!r}...")
    wf = fetch_last_24h_spend(api_url, org_id, api_key, workflow_id)

    cost_dollars = wf.get("cost_cents", 0) / 100.0
    print(f"  cost:   ${cost_dollars:,.2f}")
    print(f"  tokens: {wf.get('tokens', 0):,}")
    print(f"  calls:  {wf.get('calls', 0):,}")
    if "note" in wf:
        print(f"  note:   {wf['note']}")

    # The same number is the truth the dashboard shows — there is no
    # second source of truth in code. The policy in the Control
    # Plane decides the budget; the SDK just records spend.
    print(
        "\nBudgets live in the Control Plane (UI/policy), not in code. "
        "Edit the workflow's policy in the dashboard to change the cap."
    )


if __name__ == "__main__":
    main()
