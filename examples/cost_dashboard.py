"""
Phase 2 example — read live cost from the dashboard.

NULLRUN is the single source of truth for AI workflow budgets: the
dashboard's policy wins, never a `max_cost=` kwarg. This example
reads the unified status payload for one workflow so the user can
see that the SDK and the dashboard agree.

Run:
    pip install -e ../sdk-python
    export NULLRUN_API_KEY=nr_live_...
    export NULLRUN_ORGANIZATION_ID=<real-org-uuid>
    export NULLRUN_WORKFLOW_ID=<real-workflow-uuid>
    python cost_dashboard.py

Sprint 2.8: the previous version used zero-UUID defaults for
``NULLRUN_ORGANIZATION_ID`` and ``NULLRUN_WORKFLOW_ID``, which
always 404 against the real backend. The example would import
and run, but the GET returned an error and the example printed
zeroed fields. Now we exit early with an actionable message if
either env var is missing.
"""

import os
import sys

import nullrun


def _require_env(name: str) -> str:
    """Return the env var value, or exit with an actionable message."""
    value = os.environ.get(name)
    if not value or value == "00000000-0000-0000-0000-000000000000":
        print(
            f"ERROR: {name} is required.\n"
            f"Set it to a real UUID from the NullRun dashboard. "
            f"Example:\n"
            f"  export {name}=<uuid>",
            file=sys.stderr,
        )
        sys.exit(1)
    return value


def main() -> None:
    # Sprint 2.8: validate required env vars BEFORE ``nullrun.init()``
    # so the user gets a clear "missing env var" error rather than
    # a confusing 401 from /auth/verify. ``init()`` will perform a
    # network call against the gateway; if the api_key is the demo
    # placeholder it will fail with 401. Better to fail at the
    # script's own validation step first.
    org_id = _require_env("NULLRUN_ORGANIZATION_ID")
    workflow_id = _require_env("NULLRUN_WORKFLOW_ID")
    api_key = os.environ.get("NULLRUN_API_KEY")
    if not api_key:
        print(
            "ERROR: NULLRUN_API_KEY is required.\n"
            "Set it to a real api_key from the NullRun dashboard.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Initialise the SDK so the example matches the typical setup
    # pattern. ``nullrun.init`` is not strictly required for the
    # raw ``/status`` GET below, but it makes the example feel
    # like a real-world wiring.
    nullrun.init(api_key=api_key)

    print(f"Reading status for org {org_id!r}, workflow {workflow_id!r}...")
    body = nullrun.get_runtime().get_org_status(org_id)

    usage_today = body.get("usage_today_cents", 0) / 100.0
    usage_month = body.get("usage_month_cents", 0) / 100.0
    budget_used = body.get("budget_used_cents", 0) / 100.0
    rate = body.get("rate")
    plan = body.get("plan")
    accuracy = body.get("cost_accuracy_hint", "approximate")

    print(f"  usage today:    ${usage_today:,.2f}")
    print(f"  usage month:    ${usage_month:,.2f}")
    print(f"  budget used:    ${budget_used:,.2f}")
    if rate is not None:
        print(f"  rate:           {rate}")
    if plan:
        print(f"  plan:           {plan}")
    print(f"  cost accuracy:  {accuracy}")

    print(
        "\nBudgets live in the Control Plane (UI/policy), not in code. "
        "Edit the workflow's policy in the dashboard to change the cap."
    )


if __name__ == "__main__":
    main()