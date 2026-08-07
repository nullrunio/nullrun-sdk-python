"""Regression tests for the v3.38 wire-drift fixes (2026-08-07).

These pin three contract-level fixes that were verified against
backend source code, not against comments or documentation:

* **capabilities probe route** — the SDK was probing
  ``/health`` (a generic liveness payload) instead of the
  canonical ``/api/v1/capabilities`` route. Pre-fix, every
  ``is_v3_ready()`` returned False because the probe never saw
  a v3 capability payload, leaving every flag a runtime no-op.

* **API_KEY_* error code granularity (v3.38)** — the backend
  split the v3.36 ``API_KEY_REVOKED`` bucket into five distinct
  wire codes (``API_KEY_EXPIRED`` / ``API_KEY_DISABLED`` /
  ``API_KEY_INVALID`` / ``API_KEY_MISSING`` /
  ``API_KEY_MALFORMED``) so SDKs can branch on each lifecycle
  state. Pre-fix, only ``API_KEY_REVOKED`` was mapped in
  ``_V3_ERROR_CODE_MAP`` — the other five silently fell through
  to the generic HTTP-status fallback (``NullRunAuthentication
  Error``) without ever becoming ``NullRunAuthError``, losing
  the diagnostic class. Wire codes are now surfaced on
  ``NullRunAuthError.wire_code``.

* **decision == "soft_pass" handling** — the backend returns
  ``soft_pass`` for soft-mode calls that proceed via the chain's
  overdraft cap (CLAUDE.md §5). Pre-fix, the runtime's
  ``check_workflow_budget`` had no branch for ``soft_pass`` —
  the ``decision == "allow"`` default fall-through meant the
  body proceeded (correct) but the operator saw no log line
  and no overdraft counter incremented (silent budget drift).

The tests pin the fixed behaviour so a future refactor that
breaks any of these three contracts gets caught in CI rather
than at first production /check.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from nullrun.breaker import exceptions as exc
from nullrun.capabilities import (
    CAPABILITIES_PATH,
    probe_capabilities,
)
from nullrun.transport import _V3_ERROR_CODE_MAP, _parse_v3_error_envelope

BASE_URL = "https://api.test.nullrun.io"

_RUNTIME_SRC_PATH = (
    Path(__file__).parent.parent / "src" / "nullrun" / "runtime.py"
)


# ---------------------------------------------------------------------------
# Fix #1 — capabilities probe route (/api/v1/capabilities, not /health)
# ---------------------------------------------------------------------------


def test_capabilities_path_constant_is_canonical_route():
    """``CAPABILITIES_PATH`` must point at ``/api/v1/capabilities``.

    The constant is the single source of truth — every
    ``probe_capabilities`` call builds ``{api_url}{CAPABILITIES_PATH}``
    (capabilities.py:290). Pinning the constant here catches a
    refactor that re-introduces the legacy ``/health`` route.
    """
    assert CAPABILITIES_PATH == "/api/v1/capabilities"


def test_probe_capabilities_against_canonical_route_with_v3_payload():
    """A v3 backend responding at /api/v1/capabilities with the
    nested ``capabilities:`` payload yields ``is_v3_ready() == True``.

    Pins the entire probe → parse → flag chain against the canonical
    route. Pre-fix the SDK probed /health and never saw this payload,
    so ``is_v3_ready()`` was always False.
    """
    payload = {
        "min_protocol_version": 3,
        "max_protocol_version": 3,
        "protocol_version": 3,
        "capabilities": {
            "server_minted_execution_id": True,
            "per_execution_reservations": True,
            "enforcement_modes_soft": True,
            "heartbeat_time_based": True,
        },
    }
    with respx.mock:
        respx.get(f"{BASE_URL}/api/v1/capabilities").mock(
            return_value=httpx.Response(200, json=payload)
        )
        # Negative pin — a stale /health mock returning 200 must
        # NOT satisfy the probe. This catches regressions where
        # someone re-adds /health as a fallback.
        respx.get(f"{BASE_URL}/health").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        parsed = probe_capabilities(BASE_URL)
    assert parsed is not None
    assert parsed.is_v3_ready()
    assert parsed.server_minted_execution_id is True
    assert parsed.per_execution_reservations is True
    assert parsed.heartbeat_time_based is True


# ---------------------------------------------------------------------------
# Fix #2 — v3.38 API_KEY_* codes in _V3_ERROR_CODE_MAP + wire_code attr
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wire_code",
    [
        "API_KEY_REVOKED",
        "API_KEY_EXPIRED",
        "API_KEY_DISABLED",
        "API_KEY_INVALID",
        "API_KEY_MISSING",
        "API_KEY_MALFORMED",
    ],
)
def test_v3_error_code_map_covers_all_api_key_states(wire_code):
    """All six v3.38 API_KEY_* wire codes must map to NullRunAuthError.

    Pre-fix the map only covered ``API_KEY_REVOKED`` — the other
    five silently fell through to the generic HTTP-status fallback
    (line ~2616 in transport.py), losing the diagnostic class.
    Pinning the map catches a refactor that drops any of the five
    new entries.
    """
    assert wire_code in _V3_ERROR_CODE_MAP
    assert _V3_ERROR_CODE_MAP[wire_code] is exc.NullRunAuthError


def test_parse_v3_error_envelope_surfaces_wire_code_on_auth_error():
    """A 401 with error_code=API_KEY_EXPIRED yields NullRunAuthError
    whose ``wire_code`` attribute exposes the granular backend code.

    Without ``wire_code``, callers have only the SDK-side NR-A003
    taxonomy and lose the granular lifecycle signal. Mirrors
    NullRunChainError.backend_code pattern (exceptions.py:448).
    """
    response = httpx.Response(
        401,
        json={
            "error_code": "API_KEY_EXPIRED",
            "error_message": "key TTL elapsed",
            "details": {"expires_at": "2026-08-01T00:00:00Z"},
        },
    )
    err = _parse_v3_error_envelope(response, "gate")
    assert isinstance(err, exc.NullRunAuthError)
    # SDK-side taxonomy preserved (NR-A003) — the fix adds wire_code
    # instead of clobbering error_code.
    assert err.error_code == "NR-A003"
    # Granular wire code surfaced for handler dispatch.
    assert err.wire_code == "API_KEY_EXPIRED"


def test_parse_v3_error_envelope_preserves_default_wire_code_for_revoked():
    """API_KEY_REVOKED continues to work — wire_code defaults to it
    when the constructor is called without an explicit value (e.g.
    a future refactor that bypasses the catalog dispatch).
    """
    err = exc.NullRunAuthError("revoked")
    assert err.wire_code == "API_KEY_REVOKED"
    assert err.error_code == "NR-A003"


def test_parse_v3_error_envelope_auth_error_does_not_clobber_unrelated_details():
    """The fix to filter ``details`` to known kwargs must not lose
    extras silently — unknown keys (e.g. ``expires_at``) must land
    on ``self.details`` for caller introspection. Pre-fix the
    envelope parser forwarded every detail as a kwarg, which threw
    TypeError on the first unknown key (e.g. when the backend
    started emitting ``expires_at`` for v3.38 EXPIRED responses).
    """
    response = httpx.Response(
        401,
        json={
            "error_code": "API_KEY_DISABLED",
            "error_message": "admin disabled this key",
            "details": {
                "disabled_at": "2026-08-01T00:00:00Z",
                "disabled_by": "admin@nullrun.io",
            },
        },
    )
    err = _parse_v3_error_envelope(response, "gate")
    assert isinstance(err, exc.NullRunAuthError)
    assert err.wire_code == "API_KEY_DISABLED"
    # The disabled_at / disabled_by fields land on self.details
    # (not lost, not raised).
    details = getattr(err, "details", {}) or {}
    assert details.get("disabled_at") == "2026-08-01T00:00:00Z"
    assert details.get("disabled_by") == "admin@nullrun.io"


# ---------------------------------------------------------------------------
# Fix #3 — decision == "soft_pass" handling in check_workflow_budget
# ---------------------------------------------------------------------------
#
# ``check_workflow_budget(self) -> None`` builds its own ``check_req``
# dict and fetches via ``self._transport.check()`` — the signature
# has no way to inject a response fixture without a full transport
# mock. The soft_pass branch is a pure decision switch (runtime.py
# ~1799-1830) so a source-level scan is the most reliable pin,
# matching the migration_drift_tests pattern used elsewhere in the
# SDK and backend.


def test_check_workflow_budget_handles_soft_pass_decision():
    """``check_workflow_budget`` must contain a ``decision ==
    "soft_pass"`` branch.

    Pre-fix, ``soft_pass`` fell through the ``decision == "allow"``
    default — body executed (correct) but no log line, no counter.
    Operators had zero visibility into "budget soft cap is biting".

    Static scan pins the runtime.py structure so a future refactor
    that drops the branch gets caught in CI rather than at first
    production /check.
    """
    runtime_src = _RUNTIME_SRC_PATH.read_text(encoding="utf-8")

    assert 'decision == "soft_pass"' in runtime_src, (
        "check_workflow_budget must branch on `decision == \"soft_pass\"`. "
        "Pre-fix the branch was missing — soft_pass fell through the "
        "default allow path and operators got no overdraft telemetry."
    )


def test_check_workflow_budget_soft_pass_branch_increments_overdraft_counter():
    """The soft_pass branch must increment ``soft_overdraft_used``
    so operators can graph soft-cap pressure in the dashboard —
    silent budget drift is the regression we are preventing.
    """
    runtime_src = _RUNTIME_SRC_PATH.read_text(encoding="utf-8")

    # Slice the soft_pass branch out of the file by anchoring on
    # the literal and the next known decision branch. The slice
    # must contain the counter increment.
    soft_pass_idx = runtime_src.find('decision == "soft_pass"')
    assert soft_pass_idx >= 0, "soft_pass branch not found"
    require_approval_idx = runtime_src.find(
        'decision == "require_approval"', soft_pass_idx
    )
    assert require_approval_idx >= 0, (
        "decision == require_approval marker not found after soft_pass — "
        "the runtime source structure has drifted from this pin's anchor."
    )
    branch_slice = runtime_src[soft_pass_idx:require_approval_idx]

    assert "soft_overdraft_used" in branch_slice, (
        "soft_pass branch must increment `soft_overdraft_used` so the "
        "dashboard can graph soft-cap pressure."
    )
    assert "metrics.inc_runtime" in branch_slice, (
        "soft_pass branch must call `metrics.inc_runtime(...)` to record "
        "the counter."
    )


def test_check_workflow_budget_soft_pass_branch_logs_overdraft_telemetry():
    """The soft_pass branch must log at WARNING level with the
    backend's ``overdraft_used_cents`` value — that's the operator's
    primary signal that the chain's overdraft cap is burning.
    """
    runtime_src = _RUNTIME_SRC_PATH.read_text(encoding="utf-8")

    soft_pass_idx = runtime_src.find('decision == "soft_pass"')
    require_approval_idx = runtime_src.find(
        'decision == "require_approval"', soft_pass_idx
    )
    branch_slice = runtime_src[soft_pass_idx:require_approval_idx]

    assert "overdraft_used_cents" in branch_slice, (
        "soft_pass branch must surface `overdraft_used_cents` from the "
        "backend response — silent loss of this value means operators "
        "have no visibility into which chains are burning overdraft."
    )
    assert "logger.warning" in branch_slice, (
        "soft_pass branch must log at WARNING level — overdraft pressure "
        "is operator-actionable, not informational."
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])