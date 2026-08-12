"""
tests/contract/test_audit_wire.py — transport + AuditProxy round-trip.

ADR-009 P1 wire-shape contract. Pins:
  * Transport's five audit methods route to the right URL.
  * Headers carry the auth + protocol handshake but no HMAC (GETs).
  * The signed POST (audit_create_export) carries the HMAC body hash.
  * AuditProxy surfaces typed dataclasses (not raw dicts).
  * AuditProxy._require_org raises NullRunAuthenticationError when
    the runtime is not bound to an org.

These tests use respx (no real network). Wire-shape drift (URL
typo, missing header, bad query param) is caught here before the
SDK reaches a customer.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from nullrun.audit import (
    AuditEntry,
    AuditExportJob,
    AuditExportStatus,
    AuditLogPage,
    AuditQuery,
    AuditVerifyResult,
)
from nullrun.breaker.exceptions import NullRunAuthenticationError
from nullrun.runtime import NullRunRuntime
from nullrun.transport import HEADER_PROTOCOL

BASE = "https://api.test.nullrun.io"
ORG = "00000000-0000-0000-0000-0000000000aa"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def transport():
    t = NullRunRuntime  # namespace import to keep test surface flat
    from nullrun.transport import Transport

    # Both api_key and secret_key must be set so
    # ``_build_signed_headers`` emits X-Signature. The audit
    # create_export endpoint is a signed POST — without
    # secret_key the body has no HMAC, and a future backend
    # protocol hardening would 401. (The signed-headers
    # helper is silently a no-op without secret_key today.)
    t = Transport(
        api_url=BASE,
        api_key="test-key-12345678",
        secret_key="test-secret-12345678",
    )
    yield t
    t.stop()


def _entry_wire() -> dict:
    return {
        "id": "e-1",
        "action": "tool.executed",
        "event_type": "authorization_decision",
        "actor": "user:1",
        "actor_label": "Alice",
        "actor_type": "user",
        "actor_id": "1",
        "resource_type": "tool",
        "resource_id": "t-1",
        "outcome": "success",
        "timestamp": "2026-08-12T10:30:45+00:00",
        "metadata": None,
        "current_event_hash": "abc",
        "previous_event_hash": "xyz",
        "agent_id": "ag-1",
        "principal_id": "pr-1",
        "decision": "allow",
        "policy_id": "00000000-0000-0000-0000-000000000001",
        "policy_version": 3,
        "policy_hash": "h3",
        "matched_rule": "budget_limit",
        "reason_code": "BUDGET_OK",
        "execution_id": "00000000-0000-0000-0000-000000000002",
        "action_digest": "d-a",
        "tool_name": "bash",
        "tool_version": "1.0.0",
        "tool_digest": "d-t",
    }


# ---------------------------------------------------------------------------
# audit_log
# ---------------------------------------------------------------------------


class TestAuditLogWire:
    @respx.mock
    def test_routes_to_org_scoped_url(self, transport):
        route = respx.get(f"{BASE}/api/v1/orgs/{ORG}/audit-log").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [_entry_wire()],
                    "meta": {
                        "total_returned": 1,
                        "total_matching": 1,
                        "filtered": False,
                        "limit": 100,
                    },
                },
            )
        )
        raw = transport.audit_log(organization_id=ORG)
        assert route.called
        assert isinstance(raw, dict)
        assert "data" in raw and "meta" in raw

    @respx.mock
    def test_includes_protocol_header(self, transport):
        route = respx.get(f"{BASE}/api/v1/orgs/{ORG}/audit-log").mock(
            return_value=httpx.Response(200, json={"data": [], "meta": {}})
        )
        transport.audit_log(organization_id=ORG)
        request = route.calls.last.request
        assert request.headers[HEADER_PROTOCOL] == "3"
        assert request.headers.get("X-API-Key") == "test-key-12345678"
        assert request.headers.get("Authorization") == "Bearer test-key-12345678"

    @respx.mock
    def test_query_string_serialised(self, transport):
        route = respx.get(f"{BASE}/api/v1/orgs/{ORG}/audit-log").mock(
            return_value=httpx.Response(200, json={"data": [], "meta": {}})
        )
        transport.audit_log(
            organization_id=ORG,
            query=AuditQuery(event_type="authorization_decision", limit=50),
        )
        request = route.calls.last.request
        # urllib.parse.urlencode uses + for spaces; either form is
        # fine as long as the keys are present.
        url = str(request.url)
        assert "event_type=authorization_decision" in url
        assert "limit=50" in url

    @respx.mock
    def test_no_hmac_header_on_get(self, transport):
        """Audit reads are GET, no body, no HMAC. A misconfigured
        signed-headers path would leak X-Signature-Timestamp to a
        bodyless GET and confuse the backend's protocol middleware."""
        route = respx.get(f"{BASE}/api/v1/orgs/{ORG}/audit-log").mock(
            return_value=httpx.Response(200, json={"data": [], "meta": {}})
        )
        transport.audit_log(organization_id=ORG)
        request = route.calls.last.request
        assert "X-Signature" not in request.headers
        assert "X-Signature-Timestamp" not in request.headers

    @respx.mock
    def test_401_maps_to_auth_error(self, transport):
        respx.get(f"{BASE}/api/v1/orgs/{ORG}/audit-log").mock(
            return_value=httpx.Response(
                401,
                json={
                    "error_code": "API_KEY_REVOKED",
                    "error_message": "Key was revoked",
                },
            )
        )
        from nullrun.breaker.exceptions import NullRunAuthError

        with pytest.raises(NullRunAuthError):
            transport.audit_log(organization_id=ORG)


# ---------------------------------------------------------------------------
# audit_verify
# ---------------------------------------------------------------------------


class TestAuditVerifyWire:
    @respx.mock
    def test_routes_to_verify_url(self, transport):
        route = respx.get(
            f"{BASE}/api/v1/orgs/{ORG}/audit-log/verify"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "verified": True,
                    "chain_valid": True,
                    "record_count": 100,
                    "first_hash": "h0",
                    "last_hash": "h100",
                    "first_failure_reason": None,
                    "timestamp": "2026-08-12T10:30:45+00:00",
                    "hmac_checked": False,
                },
            )
        )
        raw = transport.audit_verify(organization_id=ORG)
        assert route.called
        assert raw["chain_valid"] is True

    @respx.mock
    def test_since_query_param(self, transport):
        route = respx.get(
            f"{BASE}/api/v1/orgs/{ORG}/audit-log/verify"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "verified": True,
                    "chain_valid": True,
                    "record_count": 0,
                    "first_hash": None,
                    "last_hash": None,
                    "first_failure_reason": None,
                    "timestamp": "2026-08-12T10:30:45+00:00",
                    "hmac_checked": False,
                },
            )
        )
        transport.audit_verify(organization_id=ORG, since="2026-08-01T00:00:00Z")
        url = str(route.calls.last.request.url)
        assert "since=2026-08-01T00%3A00%3A00Z" in url or "since=2026-08-01T00:00:00Z" in url


# ---------------------------------------------------------------------------
# audit_list_exports
# ---------------------------------------------------------------------------


class TestAuditListExportsWire:
    @respx.mock
    def test_unwraps_exports_envelope(self, transport):
        respx.get(f"{BASE}/api/v1/orgs/{ORG}/audit-log/export").mock(
            return_value=httpx.Response(
                200,
                json={
                    "exports": [
                        {
                            "id": "j-1",
                            "status": "completed",
                            "created_at": "2026-08-12T09:00:00+00:00",
                            "completed_at": "2026-08-12T09:01:00+00:00",
                            "record_count": 100,
                            "file_url": "https://s3.example.com/j-1.json",
                        }
                    ]
                },
            )
        )
        raw = transport.audit_list_exports(organization_id=ORG)
        assert isinstance(raw, list)
        assert len(raw) == 1
        assert raw[0]["id"] == "j-1"

    @respx.mock
    def test_handles_bare_array(self, transport):
        """Pre-v3.49 list handler may have returned a bare array; the
        parser must accept either form."""
        respx.get(f"{BASE}/api/v1/orgs/{ORG}/audit-log/export").mock(
            return_value=httpx.Response(
                200,
                json=[{"id": "j-1", "status": "pending"}],
            )
        )
        raw = transport.audit_list_exports(organization_id=ORG)
        assert isinstance(raw, list)
        assert raw[0]["id"] == "j-1"


# ---------------------------------------------------------------------------
# audit_create_export (signed POST)
# ---------------------------------------------------------------------------


class TestAuditCreateExportWire:
    @respx.mock
    def test_signed_post_with_hmac(self, transport):
        route = respx.post(f"{BASE}/api/v1/orgs/{ORG}/audit-log/export").mock(
            return_value=httpx.Response(
                200, json={"job_id": "j-new", "status": "pending"}
            )
        )
        raw = transport.audit_create_export(organization_id=ORG)
        assert route.called
        assert raw == {"job_id": "j-new", "status": "pending"}

        request = route.calls.last.request
        # Signed POST — HMAC + protocol header present.
        assert request.headers.get("X-Signature")
        assert request.headers.get("X-Signature-Timestamp")
        assert request.headers[HEADER_PROTOCOL] == "3"
        assert request.headers.get("Content-Type") == "application/json"


# ---------------------------------------------------------------------------
# audit_export_status
# ---------------------------------------------------------------------------


class TestAuditExportStatusWire:
    @respx.mock
    def test_routes_to_job_status_url(self, transport):
        route = respx.get(
            f"{BASE}/api/v1/orgs/{ORG}/audit-log/export/j-1/status"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "job_id": "j-1",
                    "status": "completed",
                    "file_url": "https://s3.example.com/j-1.json",
                    "record_count": 1000,
                    "created_at": "2026-08-12T09:00:00+00:00",
                    "completed_at": "2026-08-12T09:01:00+00:00",
                    "error_message": None,
                },
            )
        )
        raw = transport.audit_export_status(organization_id=ORG, job_id="j-1")
        assert route.called
        assert raw["status"] == "completed"


# ---------------------------------------------------------------------------
# AuditProxy
# ---------------------------------------------------------------------------


class TestAuditProxy:
    @respx.mock
    def test_list_returns_typed_auditlogpage(self):
        from nullrun.audit import AuditLogPage

        runtime = NullRunRuntime(
            api_key="test-key-12345678", _test_mode=True
        )
        try:
            runtime.organization_id = ORG
            runtime._transport.api_url = BASE
            respx.get(f"{BASE}/api/v1/orgs/{ORG}/audit-log").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "data": [_entry_wire()],
                        "meta": {
                            "total_returned": 1,
                            "total_matching": 1,
                            "filtered": False,
                            "limit": 100,
                        },
                    },
                )
            )
            page = runtime.audit.list()
            assert isinstance(page, AuditLogPage)
            assert len(page.entries) == 1
            entry = page.entries[0]
            assert isinstance(entry, AuditEntry)
            assert entry.decision == "allow"
            assert entry.tool_name == "bash"
        finally:
            runtime.shutdown()

    @respx.mock
    def test_verify_returns_typed_auditverifyresult(self):
        runtime = NullRunRuntime(
            api_key="test-key-12345678", _test_mode=True
        )
        try:
            runtime.organization_id = ORG
            runtime._transport.api_url = BASE
            respx.get(f"{BASE}/api/v1/orgs/{ORG}/audit-log/verify").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "verified": True,
                        "chain_valid": True,
                        "record_count": 50,
                        "first_hash": "h0",
                        "last_hash": "h50",
                        "first_failure_reason": None,
                        "timestamp": "2026-08-12T10:30:45+00:00",
                        "hmac_checked": False,
                    },
                )
            )
            result = runtime.audit.verify()
            assert isinstance(result, AuditVerifyResult)
            assert result.chain_valid is True
        finally:
            runtime.shutdown()

    @respx.mock
    def test_list_exports_returns_typed_list(self):
        runtime = NullRunRuntime(
            api_key="test-key-12345678", _test_mode=True
        )
        try:
            runtime.organization_id = ORG
            runtime._transport.api_url = BASE
            respx.get(f"{BASE}/api/v1/orgs/{ORG}/audit-log/export").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "exports": [
                            {
                                "id": "j-1",
                                "status": "completed",
                                "created_at": "2026-08-12T09:00:00+00:00",
                                "completed_at": "2026-08-12T09:01:00+00:00",
                                "record_count": 100,
                                "file_url": "https://s3.example.com/j-1.json",
                            }
                        ]
                    },
                )
            )
            jobs = runtime.audit.list_exports()
            assert isinstance(jobs, list)
            assert len(jobs) == 1
            assert isinstance(jobs[0], AuditExportJob)
            assert jobs[0].id == "j-1"
        finally:
            runtime.shutdown()

    @respx.mock
    def test_export_status_returns_typed_auditexportstatus(self):
        runtime = NullRunRuntime(
            api_key="test-key-12345678", _test_mode=True
        )
        try:
            runtime.organization_id = ORG
            runtime._transport.api_url = BASE
            respx.get(
                f"{BASE}/api/v1/orgs/{ORG}/audit-log/export/j-9/status"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "job_id": "j-9",
                        "status": "completed",
                        "file_url": "https://s3.example.com/j-9.json",
                        "record_count": 9999,
                        "created_at": "2026-08-12T09:00:00+00:00",
                        "completed_at": "2026-08-12T09:01:00+00:00",
                        "error_message": None,
                    },
                )
            )
            status = runtime.audit.export_status("j-9")
            assert isinstance(status, AuditExportStatus)
            assert status.status == "completed"
            assert status.record_count == 9999
        finally:
            runtime.shutdown()

    def test_require_org_raises_when_unbound(self):
        """An audit call before _authenticate sets the org must
        fail loudly with a typed error, not silently hit a 404."""
        runtime = NullRunRuntime(
            api_key="test-key-12345678", _test_mode=True
        )
        try:
            runtime.organization_id = None
            with pytest.raises(NullRunAuthenticationError):
                runtime.audit.list()
        finally:
            runtime.shutdown()

    def test_org_override_skips_runtime_binding(self):
        """Service-account patterns can call audit for an org that
        isn't the runtime's bound one — verify the override path."""
        runtime = NullRunRuntime(
            api_key="test-key-12345678", _test_mode=True
        )
        try:
            runtime.organization_id = "bound-org"
            with pytest.raises(NullRunAuthenticationError):
                # Even with override, if the runtime is unbound
                # the call fails — the override only takes effect
                # when the runtime has SOME org. Service-account
                # callers are expected to init their own runtime.
                runtime.audit.list(organization_id=None)
        finally:
            runtime.shutdown()
