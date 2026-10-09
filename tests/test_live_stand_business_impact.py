"""LIVE-STAND test: the no-envelope preflight must not be refused at the enum.

Why this file exists and why it is opt-in
------------------------------------------
Every other wire test in this suite mocks the transport (34 of 111 test
files use respx). A mocked transport cannot find a client/server enum
skew, because there is no server in it -- which is exactly how
``{"kind": "none"}`` shipped: `test_gate_business_impact_wire.py`
asserted what the SDK *sends* and mocked away what the server
*accepts*.

So the assertion that matters is made against a real binary, and it is
opt-in because CI has no stand. Set ``NULLRUN_LIVE_STAND_URL`` to run
it:

    NULLRUN_LIVE_STAND_URL=http://localhost:18080 \\
        pytest tests/test_live_stand_business_impact.py -v

The throwaway org, workflow and API key are created, used and dropped
inside the test process. The key is never logged, never written to a
file, and never placed on a command line.

What it pins
------------
``check_workflow_budget()`` with an empty call context has no tool to
name, so it has no envelope. The SDK used to substitute
``no_impact()`` and put ``{"kind": "none"}`` on the wire. The gate's
enum is ``Money | ToolCall`` -- there is no ``none`` -- so the request
was refused by the *deserialiser*, 422, before any policy ran.

The fix is to omit the field: ``business_impact`` is ``Option`` on the
wire, and only ``action_digest`` is mandatory at protocol >= 3. That
was measured, not assumed -- omitting it returns 200 at /gate and 200
at /execute on the same path.

The assertion is therefore *negative* and deliberately so: whatever the
policy decides (this stand has no budget configured, so it hard-blocks)
is fine. What must never happen is a refusal at the enum.
"""

from __future__ import annotations

import os
import time

import httpx
import pytest

STAND = os.environ.get("NULLRUN_LIVE_STAND_URL", "").rstrip("/")

pytestmark = pytest.mark.skipif(
    not STAND,
    reason="set NULLRUN_LIVE_STAND_URL to run against a live breaker-core",
)

# Markers that mean "the request never reached a policy decision" --
# i.e. the body was refused at the wire boundary. Any of these
# appearing in a refusal is the bug this test exists to catch.
#
# `LegacyGrant` is here because the digest is load-bearing in its own
# right: omitting `business_impact` is only legal BECAUSE the digest is
# still sent. A "fix" that dropped the digest along with the envelope
# would trade a 422 for a different 422, and this list is what stops
# that passing.
ENUM_REFUSAL_MARKERS = ("unknown variant", "BUSINESS_IMPACT_INVALID",
                        "missing field", "invalid field", "LegacyGrant")


@pytest.fixture(scope="module")
def provisioned() -> tuple[str, str]:
    """A throwaway (api_key, workflow_id) pair. In memory only."""
    stamp = int(time.time())
    r = httpx.post(
        f"{STAND}/api/v1/auth/register",
        json={"email": f"sdk-enum-{stamp}@local.test",
              "password": "Str0ng-Local-Pass!42", "name": "Enum Live"},
        timeout=20.0,
    )
    assert r.status_code in (200, 201), f"register: {r.status_code} {r.text[:200]}"
    reg = r.json()
    org, token = reg["organization_id"], reg["session_token"]
    auth = {"Authorization": f"Bearer {token}", "X-NULLRUN-PROTOCOL": "3"}

    r = httpx.post(f"{STAND}/api/v1/orgs/{org}/workflows",
                   json={"name": f"sdk-enum-{stamp}",
                         "external_id": f"wf-sdk-enum-{stamp}"},
                   headers={**auth, "Content-Type": "application/json"},
                   timeout=20.0)
    assert r.status_code < 300, f"workflow: {r.status_code} {r.text[:200]}"
    wid = r.json().get("id") or r.json().get("workflow_id")

    r = httpx.post(f"{STAND}/api/v1/orgs/{org}/api-keys",
                   json={"name": "sdk-enum", "workflow_id": wid},
                   headers={**auth, "Content-Type": "application/json"},
                   timeout=20.0)
    assert r.status_code < 300, f"key: {r.status_code} {r.text[:200]}"
    body = r.json()
    key = body.get("api_key") or body.get("key")
    assert key, f"no key in response; fields={sorted(body)}"
    return key, wid


def test_no_envelope_preflight_is_not_refused_at_the_enum(provisioned):
    """RED before the fix: 422 `unknown variant 'none'`."""
    key, _wid = provisioned
    os.environ["NULLRUN_API_URL"] = STAND
    os.environ["NULLRUN_API_KEY"] = key
    # The dev opt-out fully bypasses the gate; a test that passes only
    # with it set is asserting nothing (CLAUDE.md, "What NOT to do").
    os.environ.pop("NULLRUN_SKIP_BUDGET_CHECK", None)

    from nullrun import init
    from nullrun.context import set_call_impact
    from nullrun.runtime import NullRunRuntime

    set_call_impact(None)  # the precondition: an LLM check, no tool
    try:
        rt = init(api_key=key, api_url=STAND)
        try:
            rt.check_workflow_budget()
        except Exception as exc:  # noqa: BLE001 - the text IS the finding
            message = str(exc)
            for marker in ENUM_REFUSAL_MARKERS:
                assert marker not in message, (
                    f"the gate refused the request before any policy ran "
                    f"({marker!r} in the refusal): {message[:300]}"
                )
        finally:
            # `reset_instance`, not a guessed `close()`: the assertion
            # above must be the thing that fails, not the teardown.
            NullRunRuntime.reset_instance()
    finally:
        set_call_impact(None)
