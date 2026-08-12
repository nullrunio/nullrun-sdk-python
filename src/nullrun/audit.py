"""Audit log wire types + helpers for the NullRun SDK.

The /api/v1/orgs/:org_id/audit-log endpoint surfaces every governance
event in the org's chain (ADR-009 canonical model — see ``docs/adr/``).
This module mirrors the wire shape into typed Python dataclasses so
SDK consumers can reason about the audit timeline without writing
JSON parsing glue:

    from nullrun.audit import AuditQuery, AuditEntry, AuditLogPage

    page = runtime.audit.list(AuditQuery(event_type="authorization_decision", limit=50))
    for entry in page.entries:
        if entry.decision == "deny":
            notify_security_team(entry)

Three event categories (ADR-009 §2):

    authorization_decision — gate allow / deny / require_approval
    approval_decision     — operator approved / denied
    execution_lifecycle    — cancel / chain_end (decision IS NULL)

Pre-ADR-009 audit rows (policy.created, sso.login, plan.change, …)
are returned with the legacy `action` field populated and all
governance fields None. They render unchanged on the timeline
because the backend re-projects the legacy `action` into the new
`event_type` column at read time.

Wire reference:
    backend/src/proxy/http/audit.rs::AuditEntryResponse (13 governance
    fields, plus the legacy `action` / `actor` / `actor_label` /
    `outcome` / `metadata` shape preserved for back-compat).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# Wire-shape dataclasses — one-to-one with AuditEntryResponse fields.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditEntry:
    """Single one audit_events row from /api/v1/orgs/:org_id/audit-log.

    All fields are `Optional[...]` for forward/backward compat — a
    pre-ADR-009 row carries None on every governance field, and a
    future backend revision can grow the row without breaking older
    SDK clients.
    """

    # Legacy fields (pre-ADR-009 wire).
    id: str
    action: str  # legacy alias for event_type
    event_type: str  # canonical (ADR-009)
    actor: str  # "type:id" raw form, kept for actor-filter compat
    actor_label: str  # human-friendly ("Name (email)" / "System")
    actor_type: str
    actor_id: str
    resource_type: str
    resource_id: str
    outcome: str  # success / failure / blocked / degraded / denied
    timestamp: datetime
    metadata: dict[str, Any] | None = None
    current_event_hash: str | None = None
    previous_event_hash: str | None = None

    # ADR-009 governance columns (all NULL on pre-ADR-009 rows).
    agent_id: str | None = None
    principal_id: str | None = None
    decision: str | None = None  # allow / deny / require_approval / approved / denied
    policy_id: str | None = None
    policy_version: int | None = None
    policy_hash: str | None = None
    matched_rule: str | None = None
    reason_code: str | None = None
    execution_id: str | None = None
    action_digest: str | None = None
    tool_name: str | None = None
    tool_version: str | None = None
    tool_digest: str | None = None

    @property
    def is_governance(self) -> bool:
        """True iff this row is a canonical ADR-009 governance event.

        Equivalent to ``event_type in {"authorization_decision",
        "approval_decision", "execution_lifecycle"}``. Pre-ADR-009
        rows + non-governance legacy rows return False.
        """
        return self.event_type in (
            "authorization_decision",
            "approval_decision",
            "execution_lifecycle",
        )

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> AuditEntry:
        """Parse a single dict out of the response `data` array.

        Tolerates missing keys (forward-compat) and string-vs-int
        type drift (defensive — backend stores `policy_version` as
        INTEGER but old SDKs may have serialised it as str).
        """
        ts_raw = raw.get("timestamp")
        # Backend emits RFC3339 (e.g. "2026-08-12T10:30:45.123456+00:00").
        # Python's `fromisoformat` accepts the +00:00 suffix in 3.11+
        # but not the Z short-form — normalise.
        if isinstance(ts_raw, str):
            ts_norm = ts_raw.replace("Z", "+00:00") if ts_raw.endswith("Z") else ts_raw
            timestamp = datetime.fromisoformat(ts_norm)
        else:
            timestamp = ts_raw  # type: ignore[assignment]

        pv = raw.get("policy_version")
        if pv is not None and not isinstance(pv, int):
            try:
                pv = int(pv)
            except (TypeError, ValueError):
                pv = None

        return cls(
            id=raw["id"],
            action=raw.get("action", raw.get("event_type", "")),
            event_type=raw.get("event_type", raw.get("action", "")),
            actor=raw.get("actor", ""),
            actor_label=raw.get("actor_label", ""),
            actor_type=raw.get("actor_type", ""),
            actor_id=raw.get("actor_id", ""),
            resource_type=raw.get("resource_type", ""),
            resource_id=raw.get("resource_id", ""),
            outcome=raw.get("outcome", ""),
            timestamp=timestamp,
            metadata=raw.get("metadata"),
            current_event_hash=raw.get("current_event_hash"),
            previous_event_hash=raw.get("previous_event_hash"),
            agent_id=raw.get("agent_id"),
            principal_id=raw.get("principal_id"),
            decision=raw.get("decision"),
            policy_id=raw.get("policy_id"),
            policy_version=pv,
            policy_hash=raw.get("policy_hash"),
            matched_rule=raw.get("matched_rule"),
            reason_code=raw.get("reason_code"),
            execution_id=raw.get("execution_id"),
            action_digest=raw.get("action_digest"),
            tool_name=raw.get("tool_name"),
            tool_version=raw.get("tool_version"),
            tool_digest=raw.get("tool_digest"),
        )


@dataclass(frozen=True)
class AuditLogMeta:
    """Pagination meta from AuditLogResponse.

    `total_matching` is the count the backend ran against the same
    filter set; on an unfiltered query it mirrors `total_returned`
    so the "showing N of M" math doesn't break for callers that
    treat 0-of-1 as an error.
    """

    total_returned: int
    total_matching: int
    filtered: bool
    limit: int

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> AuditLogMeta:
        return cls(
            total_returned=int(raw.get("total_returned", 0)),
            total_matching=int(raw.get("total_matching", 0)),
            filtered=bool(raw.get("filtered", False)),
            limit=int(raw.get("limit", 0)),
        )


@dataclass(frozen=True)
class AuditLogPage:
    """One page of audit entries + pagination meta.

    `entries` is the parsed list; `meta` describes how the page was
    derived (filter status + counts). When `meta.filtered` is False,
    the SDK is implicitly asking the backend for "all rows" — this
    is rarely what callers want because governance chains grow
    unbounded, but is supported for parity with the wire.
    """

    entries: list[AuditEntry]
    meta: AuditLogMeta

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> AuditLogPage:
        data = raw.get("data", []) or []
        return cls(
            entries=[AuditEntry.from_wire(d) for d in data],
            meta=AuditLogMeta.from_wire(raw.get("meta", {})),
        )


# ---------------------------------------------------------------------------
# Query parameters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditQuery:
    """Filter set for /api/v1/orgs/:org_id/audit-log.

    All fields are optional. The backend combines them with AND.
    `event_type` and `action` alias the same audit_events column —
    when both are set, `event_type` wins on the backend; the SDK
    surfaces both names so pre-ADR-009 callers that still pass
    `action=...` continue to work.

    Wire reference: backend/src/proxy/http/audit.rs::AuditLogQuery.
    """

    # Legacy filter (pre-ADR-009).
    action: str | None = None
    # Canonical (ADR-009).
    event_type: str | None = None
    actor: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    decision: str | None = None
    policy_id: str | None = None
    execution_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int | None = None  # default 100, max 1000

    def to_query_string(self) -> str:
        """Serialise to the backend's expected query string.

        Drops None values; serialises datetimes as RFC3339.
        Returns the body of the URL query (no leading '?') so the
        caller can append it to the canonical endpoint URL.
        """
        import urllib.parse

        params: list[tuple[str, str]] = []
        for key, value in (
            ("action", self.action),
            ("event_type", self.event_type),
            ("actor", self.actor),
            ("resource_type", self.resource_type),
            ("resource_id", self.resource_id),
            ("decision", self.decision),
            ("policy_id", self.policy_id),
            ("execution_id", self.execution_id),
            ("since", self.since),
            ("until", self.until),
            ("limit", self.limit),
        ):
            if value is None:
                continue
            if isinstance(value, datetime):
                ts = value.isoformat()
                if ts.endswith("+00:00"):
                    ts = ts[:-6] + "Z"
                params.append((key, ts))
            else:
                params.append((key, str(value)))
        return urllib.parse.urlencode(params)


# ---------------------------------------------------------------------------
# Verify + export wire types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditVerifyResult:
    """Outcome of /api/v1/orgs/:org_id/audit-log/verify.

    `verified` and `chain_valid` are the same value (the backend
    surfaces both for back-compat). `first_failure_reason` is one
    of `content_hash_mismatch` / `previous_hash_mismatch` /
    `empty_chain` (or None when verified=True).

    `hmac_checked` is currently always False — the response
    envelope is not yet signed. v4.4 honest-disclosure contract.
    """

    verified: bool
    chain_valid: bool
    record_count: int
    first_hash: str | None
    last_hash: str | None
    first_failure_reason: str | None
    # The wire contract allows an empty timestamp when the server
    # returns a row before a successful hash-chain completion; the
    # parser tolerates it as `None` rather than crashing dataclass
    # construction. Pre-ADR-009 rows also serialise without a
    # timestamp (the field was added in ADR-009 itself).
    timestamp: datetime | None = None
    hmac_checked: bool = False

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> AuditVerifyResult:
        ts_raw = raw.get("timestamp", "")
        ts_norm = ts_raw.replace("Z", "+00:00") if ts_raw.endswith("Z") else ts_raw
        return cls(
            verified=bool(raw.get("verified", False)),
            chain_valid=bool(raw.get("chain_valid", False)),
            record_count=int(raw.get("record_count", 0)),
            first_hash=raw.get("first_hash"),
            last_hash=raw.get("last_hash"),
            first_failure_reason=raw.get("first_failure_reason"),
            timestamp=datetime.fromisoformat(ts_norm) if ts_norm else None,
            hmac_checked=bool(raw.get("hmac_checked", False)),
        )


@dataclass(frozen=True)
class AuditExportJob:
    """Wire-shape summary of one audit export job.

    `status` is one of `pending` / `processing` / `uploading` /
    `completed` / `failed`. The file_url is populated only after
    `status="completed"` (S3 presigned URL when S3 is configured;
    `/tmp` path otherwise).
    """

    id: str
    status: str
    created_at: datetime | None
    completed_at: datetime | None
    record_count: int | None
    file_url: str | None = None
    error_message: str | None = None

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> AuditExportJob:
        def _parse_dt(s: str | None) -> datetime | None:
            if not s:
                return None
            ts = s.replace("Z", "+00:00") if s.endswith("Z") else s
            return datetime.fromisoformat(ts)

        return cls(
            id=raw.get("id") or raw.get("job_id", ""),
            status=raw.get("status", ""),
            created_at=_parse_dt(raw.get("created_at")),
            completed_at=_parse_dt(raw.get("completed_at")),
            record_count=(
                int(raw["record_count"]) if raw.get("record_count") is not None else None
            ),
            file_url=raw.get("file_url") or raw.get("download_url"),
            error_message=raw.get("error_message"),
        )


@dataclass(frozen=True)
class AuditExportStatus:
    """Status payload from /api/v1/orgs/:org_id/audit-log/export/:job_id/status.

    Thin wrapper around the same fields as AuditExportJob — kept
    as a separate dataclass because the wire shape differs between
    the list endpoint (summary) and the per-job status endpoint
    (full detail with file_url + error_message).
    """

    job_id: str
    status: str
    file_url: str | None
    record_count: int | None
    created_at: datetime | None
    completed_at: datetime | None
    error_message: str | None

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> AuditExportStatus:
        def _parse_dt(s: str | None) -> datetime | None:
            if not s:
                return None
            ts = s.replace("Z", "+00:00") if s.endswith("Z") else s
            return datetime.fromisoformat(ts)

        return cls(
            job_id=raw.get("job_id", ""),
            status=raw.get("status", ""),
            file_url=raw.get("file_url"),
            record_count=(
                int(raw["record_count"]) if raw.get("record_count") is not None else None
            ),
            created_at=_parse_dt(raw.get("created_at")),
            completed_at=_parse_dt(raw.get("completed_at")),
            error_message=raw.get("error_message"),
        )


__all__ = [
    "AuditEntry",
    "AuditLogMeta",
    "AuditLogPage",
    "AuditQuery",
    "AuditVerifyResult",
    "AuditExportJob",
    "AuditExportStatus",
]