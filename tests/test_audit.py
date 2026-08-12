"""
tests/test_audit.py — nullrun.audit dataclass parsing + AuditQuery serialisation.

ADR-009 P1 surface. These tests pin the wire-shape parsers; if a
backend field rename slips through, this file fails loudly.

No network access. The contract tests for the full transport round-trip
live in tests/contract/test_audit_wire.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nullrun.audit import (
    AuditEntry,
    AuditExportJob,
    AuditExportStatus,
    AuditLogMeta,
    AuditLogPage,
    AuditQuery,
    AuditVerifyResult,
)


# ---------------------------------------------------------------------------
# AuditEntry.from_wire
# ---------------------------------------------------------------------------


def _full_entry() -> dict:
    """A canonical backend-shaped audit row.

    All 13 ADR-009 governance columns populated; legacy fields
    present so the parser exercises both. Mirrors the wire shape
    backend/src/proxy/http/audit.rs::AuditEntryResponse serialises.
    """
    return {
        # Legacy fields
        "id": "11111111-1111-1111-1111-111111111111",
        "action": "policy.created",  # pre-ADR-009 alias
        "event_type": "authorization_decision",
        "actor": "user:abc",
        "actor_label": "Alice (alice@example.com)",
        "actor_type": "user",
        "actor_id": "abc",
        "resource_type": "policy",
        "resource_id": "pol-123",
        "outcome": "success",
        "timestamp": "2026-08-12T10:30:45.123456+00:00",
        "metadata": {"source": "ui"},
        "current_event_hash": "abc123",
        "previous_event_hash": None,
        # 13 governance columns (ADR-009)
        "agent_id": "ag-1",
        "principal_id": "pr-1",
        "decision": "allow",
        "policy_id": "22222222-2222-2222-2222-222222222222",
        "policy_version": 7,
        "policy_hash": "h7",
        "matched_rule": "budget_limit",
        "reason_code": "BUDGET_OK",
        "execution_id": "33333333-3333-3333-3333-333333333333",
        "action_digest": "d-action",
        "tool_name": "bash",
        "tool_version": "1.0.0",
        "tool_digest": "d-tool",
    }


def test_audit_entry_parses_full_row() -> None:
    raw = _full_entry()
    entry = AuditEntry.from_wire(raw)
    assert entry.id == "11111111-1111-1111-1111-111111111111"
    assert entry.event_type == "authorization_decision"
    assert entry.action == "policy.created"
    assert entry.decision == "allow"
    assert entry.policy_version == 7
    assert entry.tool_name == "bash"
    assert entry.tool_digest == "d-tool"
    assert entry.metadata == {"source": "ui"}
    assert entry.current_event_hash == "abc123"
    assert entry.previous_event_hash is None
    # Timestamp parsed into datetime with timezone.
    assert entry.timestamp == datetime(
        2026, 8, 12, 10, 30, 45, 123456, tzinfo=timezone.utc
    )


def test_audit_entry_tolerates_missing_governance_fields() -> None:
    """Pre-ADR-009 rows have all governance fields None; parsing must
    succeed without raising."""
    raw = {
        "id": "legacy",
        "action": "policy.created",
        "event_type": "policy.created",
        "actor": "user:1",
        "actor_label": "Bob",
        "actor_type": "user",
        "actor_id": "1",
        "resource_type": "policy",
        "resource_id": "p",
        "outcome": "success",
        "timestamp": "2026-08-12T10:30:45+00:00",
        "metadata": None,
        "current_event_hash": None,
        "previous_event_hash": None,
    }
    entry = AuditEntry.from_wire(raw)
    assert entry.event_type == "policy.created"
    # All 13 governance fields default to None.
    assert entry.agent_id is None
    assert entry.principal_id is None
    assert entry.decision is None
    assert entry.policy_id is None
    assert entry.policy_version is None
    assert entry.policy_hash is None
    assert entry.matched_rule is None
    assert entry.reason_code is None
    assert entry.execution_id is None
    assert entry.action_digest is None
    assert entry.tool_name is None
    assert entry.tool_version is None
    assert entry.tool_digest is None
    assert entry.is_governance is False


def test_audit_entry_is_governance_three_categories() -> None:
    raw = _full_entry()
    for et in ("authorization_decision", "approval_decision", "execution_lifecycle"):
        r = dict(raw, event_type=et)
        assert AuditEntry.from_wire(r).is_governance is True
    for et in ("policy.created", "sso.login", "plan.change", "tool.invoked"):
        r = dict(raw, event_type=et, action=et)
        assert AuditEntry.from_wire(r).is_governance is False


def test_audit_entry_action_event_type_alias() -> None:
    """When the wire carries only `action` (pre-ADR-009) or only
    `event_type` (post-ADR-009), the parser surfaces both fields
    populated."""
    only_action = _full_entry()
    del only_action["event_type"]
    e = AuditEntry.from_wire(only_action)
    assert e.event_type == "policy.created"
    assert e.action == "policy.created"

    only_event_type = _full_entry()
    del only_event_type["action"]
    e = AuditEntry.from_wire(only_event_type)
    assert e.event_type == "authorization_decision"
    assert e.action == "authorization_decision"


def test_audit_entry_policy_version_string_drift() -> None:
    """policy_version arrives as int today; defensive parse handles
    str-cast drift (e.g. JSON serialisation round-trip through
    another layer that stringified ints)."""
    raw = _full_entry()
    raw["policy_version"] = "7"
    entry = AuditEntry.from_wire(raw)
    assert entry.policy_version == 7


def test_audit_entry_timestamp_z_suffix() -> None:
    """Python 3.10's fromisoformat does not accept the Z suffix. The
    parser normalises Z -> +00:00 transparently."""
    raw = _full_entry()
    raw["timestamp"] = "2026-08-12T10:30:45Z"
    entry = AuditEntry.from_wire(raw)
    assert entry.timestamp == datetime(
        2026, 8, 12, 10, 30, 45, tzinfo=timezone.utc
    )


# ---------------------------------------------------------------------------
# AuditLogMeta + AuditLogPage
# ---------------------------------------------------------------------------


def test_audit_log_meta_parses() -> None:
    meta = AuditLogMeta.from_wire(
        {"total_returned": 10, "total_matching": 100, "filtered": True, "limit": 50}
    )
    assert meta.total_returned == 10
    assert meta.total_matching == 100
    assert meta.filtered is True
    assert meta.limit == 50


def test_audit_log_page_parses_entries_and_meta() -> None:
    raw = {
        "data": [_full_entry(), _full_entry()],
        "meta": {
            "total_returned": 2,
            "total_matching": 2,
            "filtered": False,
            "limit": 100,
        },
    }
    page = AuditLogPage.from_wire(raw)
    assert len(page.entries) == 2
    assert all(isinstance(e, AuditEntry) for e in page.entries)
    assert page.meta.total_returned == 2
    assert page.meta.total_matching == 2


def test_audit_log_page_handles_empty_data() -> None:
    raw = {"data": [], "meta": {"total_returned": 0, "total_matching": 0, "filtered": False, "limit": 0}}
    page = AuditLogPage.from_wire(raw)
    assert page.entries == []
    assert page.meta.total_returned == 0


# ---------------------------------------------------------------------------
# AuditQuery.to_query_string
# ---------------------------------------------------------------------------


def test_audit_query_to_query_string_drops_none() -> None:
    q = AuditQuery(event_type="authorization_decision", limit=50)
    qs = q.to_query_string()
    assert "event_type=authorization_decision" in qs
    assert "limit=50" in qs
    # Other fields dropped — use `=` boundary to avoid
    # `authorization_decision` matching the bare `decision` token.
    assert "action=" not in qs
    assert "decision=" not in qs
    assert "policy_id=" not in qs


def test_audit_query_to_query_string_serialises_datetime_as_rfc3339() -> None:
    q = AuditQuery(since=datetime(2026, 8, 1, tzinfo=timezone.utc))
    qs = q.to_query_string()
    # The serializer canonicalises +00:00 to Z for UTC datetimes
    # (audit.py:264-265), then urlencode percent-encodes the
    # colons. Either form is acceptable — what matters is that
    # the wire carries a parseable RFC3339 timestamp.
    assert "since=2026-08-01T00%3A00%3A00Z" in qs


def test_audit_query_to_query_string_handles_all_fields() -> None:
    q = AuditQuery(
        action="policy.created",
        event_type="authorization_decision",
        actor="user:1",
        resource_type="policy",
        resource_id="p1",
        decision="allow",
        policy_id="00000000-0000-0000-0000-000000000001",
        execution_id="00000000-0000-0000-0000-000000000002",
        since=datetime(2026, 8, 1, tzinfo=timezone.utc),
        until=datetime(2026, 8, 12, tzinfo=timezone.utc),
        limit=10,
    )
    qs = q.to_query_string()
    for key in (
        "action=policy.created",
        "event_type=authorization_decision",
        "actor=user%3A1",
        "decision=allow",
        "limit=10",
    ):
        assert key in qs, f"missing {key!r} in {qs!r}"


# ---------------------------------------------------------------------------
# AuditVerifyResult
# ---------------------------------------------------------------------------


def test_audit_verify_result_parses_valid_chain() -> None:
    raw = {
        "verified": True,
        "chain_valid": True,
        "record_count": 1234,
        "first_hash": "h0",
        "last_hash": "h1234",
        "first_failure_reason": None,
        "timestamp": "2026-08-12T10:30:45+00:00",
        "hmac_checked": False,
    }
    r = AuditVerifyResult.from_wire(raw)
    assert r.verified is True
    assert r.chain_valid is True
    assert r.record_count == 1234
    assert r.first_hash == "h0"
    assert r.last_hash == "h1234"
    assert r.first_failure_reason is None
    assert r.hmac_checked is False


def test_audit_verify_result_parses_failure() -> None:
    raw = {
        "verified": False,
        "chain_valid": False,
        "record_count": 500,
        "first_hash": "h0",
        "last_hash": "h500",
        "first_failure_reason": "previous_hash_mismatch",
        "timestamp": "2026-08-12T11:00:00Z",
        "hmac_checked": False,
    }
    r = AuditVerifyResult.from_wire(raw)
    assert r.verified is False
    assert r.first_failure_reason == "previous_hash_mismatch"


# ---------------------------------------------------------------------------
# AuditExportJob + AuditExportStatus
# ---------------------------------------------------------------------------


def test_audit_export_job_parses_completed() -> None:
    raw = {
        "id": "j-1",
        "status": "completed",
        "created_at": "2026-08-12T09:00:00+00:00",
        "completed_at": "2026-08-12T09:01:00+00:00",
        "record_count": 50000,
        "file_url": "https://s3.example.com/export-j-1.json",
        "error_message": None,
    }
    j = AuditExportJob.from_wire(raw)
    assert j.id == "j-1"
    assert j.status == "completed"
    assert j.record_count == 50000
    assert j.file_url == "https://s3.example.com/export-j-1.json"


def test_audit_export_job_accepts_job_id_alias() -> None:
    """The list endpoint uses `id`; the per-job status uses
    `job_id`. Both must parse into the same shape."""
    raw = {"id": "j-1", "status": "pending"}
    j = AuditExportJob.from_wire(raw)
    assert j.id == "j-1"

    raw = {"job_id": "j-2", "status": "pending"}
    j = AuditExportJob.from_wire(raw)
    assert j.id == "j-2"


def test_audit_export_status_parses_failed() -> None:
    raw = {
        "job_id": "j-3",
        "status": "failed",
        "file_url": None,
        "record_count": None,
        "created_at": "2026-08-12T09:00:00+00:00",
        "completed_at": "2026-08-12T09:01:00+00:00",
        "error_message": "S3 upload failed: timeout",
    }
    s = AuditExportStatus.from_wire(raw)
    assert s.status == "failed"
    assert s.error_message == "S3 upload failed: timeout"
    assert s.file_url is None
