"""Contract tests for backend BatchTrackResponse parsing.

Audit 2026-06-28: backend renamed `BatchTrackResponse.actions_taken`
(Vec<String> of debug names) → `BatchTrackResponse.actions`
(Vec<ActionTaken> structured) + `messages` (Vec<String> display-only).
Single /track still uses `TrackResponse.actions_taken` (Vec<ActionTaken>)
— separate endpoint, separate schema.

These tests pin both schemas so a future backend rename can't silently
break the SDK. Forward-compat path (legacy `actions_taken` dropped in
SDK 0.8.0 per CHANGELOG.0) is documented but no longer parsed.
"""

from __future__ import annotations

import pytest
import respx
from httpx import Response

import nullrun.actions as _act
import nullrun.decorators as _dec
from nullrun.runtime import NullRunRuntime

BASE_URL = "https://api.test.nullrun.io"


@pytest.fixture
def mock_api():
    """Activate the global respx mock for the duration of the test and
    pre-register /api/v1/auth/verify (the auth handshake that
    NullRunRuntime.__init__ triggers). Each test pins its own
    /api/v1/track/batch response via ``respx.post(...).mock(...)``.

    Why ``with respx.mock:`` and not ``with respx.mock(...) as router:``:
    parenthesised ``respx.mock(...)`` creates a *local* MockRouter, but
    ``respx.post(...)`` is a module-level helper that always writes to
    the *global* router. The two don't share state, so per-test
    ``respx.post(...)`` mocks wouldn't be visible to the local router.
    Bare ``respx.mock:`` re-uses the global router so module-level
    ``respx.post(...)`` calls land on the active context — same pattern
    used by ``tests/conftest.py``.
    """
    with respx.mock:
        respx.post(f"{BASE_URL}/api/v1/auth/verify").mock(
            return_value=Response(
                200,
                json={
                    "organization_id": "ws-test",
                    "workflow_id": "00000000-0000-0000-0000-000000000001",
                    "plan": "pro",
                    "features": [],
                    "limits": {"max_cost_cents": 10000},
                },
            )
        )
        yield


def _runtime(api_key: str = "test-key"):
    """Build a runtime with the test API key and base URL.

    Returns the constructed instance directly. ``NullRunRuntime.__init__``
    does NOT assign ``_instance`` — only ``get_instance `` does that —
    so reading ``NullRunRuntime._instance`` after a direct constructor
    call returns ``None``.
    """
    NullRunRuntime._instance = None
    _dec._runtime = None
    _act._action_handler = None
    rt = NullRunRuntime(
        api_key=api_key,
        api_url=BASE_URL,
        debug=True,
        polling=False,
    )
    # Keep _instance in sync with conftest.py's `make_runtime` so the
    # @protect decorator's lazy resolver finds this runtime too.
    NullRunRuntime._instance = rt
    _dec._runtime = rt
    return rt


# -----------------------------------------------------------------------------
# New schema (post 2026-06-27 backend rename)
# -----------------------------------------------------------------------------


def test_new_schema_actions_and_messages_processed(mock_api):
    """Backend 2026-06-27+ sends `actions` (structured) + `messages` (strings)."""
    rt = _runtime()
    route = respx.post(f"{BASE_URL}/api/v1/track/batch").mock(
        return_value=Response(
            200,
            json={
                "processed": 3,
                "actions": [
                    {"type": "rate_limit", "reason": "exceeded"},
                    {"type": "budget_cap", "reason": "100 cents over"},
                ],
                "messages": [
                    "1 event rejected: budget cap exceeded",
                ],
                "snapshots": [],
                "accepted_event_ids": ["e1", "e2"],
                "rejected_count": 0,
                "rejection_details": [],
            },
        )
    )

    # Trigger a batch send (track_llm triggers batch flush internally)
    # NOTE: the fixture already wraps the test in `respx.mock(...)`, so
    # we must NOT add another `with mock_api:` here — re-entering the
    # router clears the auth/verify mock registered by the fixture
    # (respx's `__exit__` calls `rollback ` + `reset `).
    rt._transport._send_batch_with_retry_info(
        batch=[
            {
                "event_type": "llm_call",
                "workflow_id": "wf-1",
                "model": "gpt-4",
                "tokens": 100,
                "cost_cents": 1,
            }
        ]
    )

    assert route.called


def test_new_schema_messages_only_no_actions(mock_api):
    """Display-only messages with empty actions should not raise."""
    rt = _runtime()
    respx.post(f"{BASE_URL}/api/v1/track/batch").mock(
        return_value=Response(
            200,
            json={
                "processed": 1,
                "actions": [],
                "messages": ["event accepted with warnings"],
                "snapshots": [],
                "accepted_event_ids": ["e1"],
                "rejected_count": 0,
                "rejection_details": [],
            },
        )
    )

    rt._transport._send_batch_with_retry_info(
        batch=[
            {
                "event_type": "llm_call",
                "workflow_id": "wf-1",
                "model": "gpt-4",
                "tokens": 100,
                "cost_cents": 1,
            }
        ]
    )
    # No exception = pass


def test_new_schema_empty_actions_no_messages(mock_api):
    """All-accepted batch with empty actions/messages must not raise."""
    rt = _runtime()
    respx.post(f"{BASE_URL}/api/v1/track/batch").mock(
        return_value=Response(
            200,
            json={
                "processed": 1,
                "actions": [],
                "messages": [],
                "snapshots": [],
                "accepted_event_ids": ["e1"],
                "rejected_count": 0,
                "rejection_details": [],
            },
        )
    )

    rt._transport._send_batch_with_retry_info(
        batch=[
            {
                "event_type": "llm_call",
                "workflow_id": "wf-1",
                "model": "gpt-4",
                "tokens": 100,
                "cost_cents": 1,
            }
        ]
    )


# -----------------------------------------------------------------------------
# Backward-compat: legacy `actions_taken` (Vec<String>) is intentionally dropped
# -----------------------------------------------------------------------------


def test_legacy_actions_taken_string_field_does_not_crash(mock_api):
    """Old backend (pre-2026-06-27) sent `actions_taken: Vec<String>` of
    debug names. SDK 0.8.0 reads `actions` only. A legacy response must
    not crash — missing `actions` is treated as empty."""
    rt = _runtime()
    respx.post(f"{BASE_URL}/api/v1/track/batch").mock(
        return_value=Response(
            200,
            json={
                "processed": 1,
                # Legacy field — SDK should ignore it.
                "actions_taken": ["rate_limit_exceeded"],
                # New fields absent → empty defaults.
                "snapshots": [],
                "accepted_event_ids": ["e1"],
                "rejected_count": 0,
                "rejection_details": [],
            },
        )
    )

    # Should NOT raise. The legacy `actions_taken` field is ignored
    # (transport.py:1176-1177 comment, "legacy actions_taken
    # fallback was removed"). `actions = data.get("actions") or []`
    # returns [].
    rt._transport._send_batch_with_retry_info(
        batch=[
            {
                "event_type": "llm_call",
                "workflow_id": "wf-1",
                "model": "gpt-4",
                "tokens": 100,
                "cost_cents": 1,
            }
        ]
    )
