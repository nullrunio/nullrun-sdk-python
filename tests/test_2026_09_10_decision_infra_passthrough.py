"""DEF-NR-A003-REWRAP-LOSS (2026-09-10, broader scope) — typed
Decision and Infrastructure subclasses that fall outside the
NullRunBlockedException / NullRunBackendError / NullRunAuthenticationError
/ NullRunTransportError arms of ``@protect`` /
``_enforce_sensitive_tool`` must propagate unchanged through the
catch-all rewrap arm.

Pre-fix (audit 2026-09-10):
  - ``nullrun/decorators.py::_enforce_sensitive_tool`` had a final
    ``except Exception as exc:`` catch-all that rewrote everything to
    ``NullRunBlockedException(error_code="NR-B001", reason="policy
    engine unavailable: ...")``.
  - The following typed subclasses do not match the four named arms
    above (NullRunBlockedException, NullRunBackendError,
    NullRunAuthenticationError, NullRunTransportError), so they were
    silently rewrapped into ``NR-B001``:
      * NullRunAuthError (NR-A003) — typed 401 envelope with
        ``wire_code`` (API_KEY_REVOKED / EXPIRED / DISABLED /
        INVALID / MISSING / MALFORMED per v3.38) — rewrap loses the
        wire_code and the catalog line for "API key rejected.
        Verify ... rotate if revoked."
      * NullRunProtocolError (NR-P001) — wire-protocol mismatch —
        loses the "Upgrade the SDK to a version that supports
        protocol X-NULLRUN-PROTOCOL: 4" recovery hint.
      * NullRunRateLimitRedisError (NR-R002) — Redis-outage
        fail-CLOSED — loses "fail-CLOSED due to Redis outage"
        distinct from a generic 503.
      * NullRunConfigError (NR-Cxxx) — wired-in config error —
        should never be rewrapped as a transient transport block.
      * NullRunChainError (NR-CH001) — chain / cross-org / Execution
        Graph parent-lineage — loses ``chain_id``,
        ``parent_execution_id``, ``backend_code``.
      * NullRunWorkflowInactiveError (NR-W004) — soft-deleted
        workflow — loses ``workflow_id``.
      * NullRunConsumeOverbudgetError (NR-O001) — invariant
        violation — loses ``execution_id``, ``reserved_cents``,
        ``max_allowed_cents``, ``actual_cost_cents``.
      * WorkflowPausedException (NR-W003) — loses
        ``resume_after``, ``workflow_id``, ``reason``.

Post-fix:
  - Two umbrella pass-through arms added BEFORE the catch-all
    ``except Exception: except NullRunDecision: raise`` and
    ``except NullRunInfrastructureError: raise``. Each umbrella
    covers a known set of typed subclasses (see the catch-fan-in
    comment in decorators.py for the full enumeration). Catalog
    is preserved with error_code / user_action / first-class attrs
    intact.

These tests pin BOTH the source shape AND the runtime behavior so
a future refactor that re-introduces a rewrap (e.g. drops one of
the umbrella arms, or re-orders them after the catch-all) fails
the test.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nullrun.breaker.exceptions import (
    NullRunAuthError,
    NullRunBackendError,
    NullRunBlockedException,
    NullRunChainError,
    NullRunConsumeOverbudgetError,
    NullRunProtocolError,
    NullRunRateLimitRedisError,
    NullRunTransportError,
    NullRunWorkflowInactiveError,
    TransportErrorSource,
    WorkflowPausedException,
)
from nullrun.decorators import _enforce_sensitive_tool

SDK_ROOT = Path(__file__).resolve().parent.parent
DECORATORS_PY = SDK_ROOT / "src" / "nullrun" / "decorators.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _enforce_sensitive_tool_body() -> str:
    """Return the source of ``_enforce_sensitive_tool`` so source-pin
    tests can grep for the expected arms / ordering without depending
    on Python AST parsing."""
    src = _read(DECORATORS_PY)
    m = re.search(
        r"def _enforce_sensitive_tool\(.*?\n(?=def |\nclass |\Z)",
        src,
        re.DOTALL,
    )
    assert m, "could not locate _enforce_sensitive_tool body"
    return m.group(0)


# ─── Source-pin tests (mirror cancel.rs / orchestrator.rs pin style) ───


class TestDefNrA003SourcePin:
    """Pin the shape of the fix so a refactor that reorders / removes
    the umbrella arms fails loudly."""

    def _catch_all_index(self, body: str) -> int:
        # ``_enforce_sensitive_tool`` has TWO ``except Exception as
        # exc:`` arms: an early one (around body-line 87) inside the
        # business_impact extractor wrapper, and the main one (the
        # catch-all rewrap near the bottom). The umbrella arms in
        # this fix must precede the MAIN catch-all (the one whose
        # comment starts with "Any other exception is a transport /
        # network / backend failure"); the extractor arm is unrelated
        # and should not be matched.
        # Anchor on the distinctive comment that prefaces the main
        # catch-all rewrap so we pick the correct one.
        marker = "Any other exception is a transport"
        marker_idx = body.find(marker)
        assert marker_idx != -1, (
            "DECORATORS test fixture broken: main catch-all arm "
            "marker 'Any other exception is a transport' not found "
            "in _enforce_sensitive_tool"
        )
        # The `except` keyword is on the line just before the comment.
        except_idx = body.rfind("except Exception as exc:", 0, marker_idx)
        assert except_idx != -1, (
            "DECORATORS test fixture broken: catch-all `except "
            "Exception as exc:` arm not found near the marker"
        )
        return except_idx

    def test_decision_umbrella_arm_present(self):
        body = _enforce_sensitive_tool_body()
        assert "except NullRunDecision:" in body, (
            "DEF-NR-A003-REWRAP-LOSS (umbrella): the pass-through arm "
            "for NullRunDecision must be present in "
            "_enforce_sensitive_tool. Pre-fix NullRunChainError / "
            "NullRunWorkflowInactiveError / NullRunConsumeOverbudgetError "
            "were swallowed by the catch-all rewrap into NR-B001."
        )

    def test_infrastructure_umbrella_arm_present(self):
        body = _enforce_sensitive_tool_body()
        assert "except NullRunInfrastructureError:" in body, (
            "DEF-NR-A003-REWRAP-LOSS (umbrella): the pass-through arm "
            "for NullRunInfrastructureError must be present in "
            "_enforce_sensitive_tool. Pre-fix NullRunAuthError / "
            "NullRunProtocolError / NullRunRateLimitRedisError / "
            "NullRunConfigError were swallowed by the catch-all rewrap."
        )

    def test_decision_arm_appears_before_catch_all(self):
        """Order matters: the umbrella arm must come BEFORE
        ``except Exception as exc:``. If a future refactor moves it
        after, the catch-all would silently rewrap into NR-B001."""
        body = _enforce_sensitive_tool_body()
        decision_idx = body.find("except NullRunDecision:")
        catch_all_idx = self._catch_all_index(body)
        assert decision_idx != -1
        assert decision_idx < catch_all_idx, (
            "DEF-NR-A003-REWRAP-LOSS: the NullRunDecision umbrella arm "
            "must appear BEFORE `except Exception as exc:`. Pre-fix "
            "order swallowed NullRunChainError / "
            "NullRunWorkflowInactiveError / NullRunConsumeOverbudgetError "
            "into NR-B001."
        )

    def test_infrastructure_arm_appears_before_catch_all(self):
        body = _enforce_sensitive_tool_body()
        idx = body.find("except NullRunInfrastructureError:")
        catch_all_idx = self._catch_all_index(body)
        assert idx != -1
        assert idx < catch_all_idx, (
            "DEF-NR-A003-REWRAP-LOSS: the NullRunInfrastructureError "
            "umbrella arm must appear BEFORE `except Exception as "
            "exc:`. Pre-fix order swallowed NullRunAuthError / "
            "NullRunProtocolError / NullRunRateLimitRedisError into "
            "NR-B001."
        )

    def test_decision_arm_only_raises(self):
        body = _enforce_sensitive_tool_body()
        m = re.search(
            r"except NullRunDecision:\s*\n(.*?)(?=\n    except |\Z)",
            body,
            re.DOTALL,
        )
        assert m, "could not parse NullRunDecision arm body"
        executable_lines = [
            ln for ln in m.group(1).splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        executable = "\n".join(executable_lines)
        assert "raise" in executable
        assert "NullRunBlockedException" not in executable, (
            "DEF-NR-A003-REWRAP-LOSS: NullRunDecision arm must not "
            "rewrap into NullRunBlockedException"
        )

    def test_infrastructure_arm_only_raises(self):
        body = _enforce_sensitive_tool_body()
        m = re.search(
            r"except NullRunInfrastructureError:\s*\n(.*?)(?=\n    except |\Z)",
            body,
            re.DOTALL,
        )
        assert m, "could not parse NullRunInfrastructureError arm body"
        executable_lines = [
            ln for ln in m.group(1).splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        executable = "\n".join(executable_lines)
        assert "raise" in executable
        assert "NullRunBlockedException" not in executable, (
            "DEF-NR-A003-REWRAP-LOSS: NullRunInfrastructureError arm "
            "must not rewrap into NullRunBlockedException"
        )

    def test_decision_arm_comment_tag_present(self):
        body = _enforce_sensitive_tool_body()
        assert "DEF-NR-A003-REWRAP-LOSS" in body, (
            "DEF-NR-A003-REWRAP-LOSS: the umbrella-arm explainer "
            "comment block must name the fix tag so future readers "
            "can grep for it."
        )

    def test_imports_include_umbrella_classes(self):
        src = _read(DECORATORS_PY)
        # The function-local import block at line ~809 must include
        # both NullRunDecision and NullRunInfrastructureError;
        # otherwise NameError at runtime even though the except arms
        # are present.
        assert "NullRunDecision" in src
        assert "NullRunInfrastructureError" in src


# ─── Behavioral tests (mirror test_protect.py:651 style) ─────────────


def _mock_runtime_raising(exc: Exception) -> MagicMock:
    rt = MagicMock()
    rt.is_sensitive_tool.return_value = True
    rt.execute.side_effect = exc
    return rt


class TestDefNrA003Behavior:
    """Pin the runtime behavior — typed exception propagates with
    error_code + first-class attrs intact."""

    # ── NullRunInfrastructureError subclass coverage ─────────────────

    def test_auth_error_propagates_unchanged_with_wire_code(self):
        """NullRunAuthError is the canonical case that prompted this
        fix: an authorized cookbook user gets a 401 with
        wire_code=API_KEY_REVOKED (or one of five other lifecycle
        codes). Pre-fix the @protect catch-all stamped NR-B001 and
        discarded wire_code, so the operator saw 'policy engine
        unavailable' instead of 'API key rejected (401). Verify at
        ... and rotate if revoked.'"""
        exc = NullRunAuthError(
            "API key rejected", wire_code="API_KEY_REVOKED"
        )
        rt = _mock_runtime_raising(exc)
        with pytest.raises(NullRunAuthError) as excinfo:
            _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
        assert excinfo.value is exc, (
            "DEF-NR-A003-REWRAP-LOSS: NullRunAuthError must propagate "
            "unchanged (identity check)."
        )
        assert excinfo.value.error_code == "NR-A003"
        assert excinfo.value.wire_code == "API_KEY_REVOKED", (
            "DEF-NR-A003-REWRAP-LOSS: exc.wire_code must be preserved "
            "— NullRunBlockedException doesn't carry it and the "
            "catch-all rewrap dropped it."
        )

    def test_protocol_error_propagates_unchanged(self):
        exc = NullRunProtocolError("PROTOCOL_TOO_OLD")
        rt = _mock_runtime_raising(exc)
        with pytest.raises(NullRunProtocolError) as excinfo:
            _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
        assert excinfo.value is exc
        assert excinfo.value.error_code == "NR-P001", (
            "DEF-NR-A003-REWRAP-LOSS: NullRunProtocolError must keep "
            "its NR-P001 error_code; pre-fix the catch-all stamped "
            "NR-B001 and lost the SDK-upgrade catalog line."
        )

    def test_rate_limit_redis_error_propagates_unchanged(self):
        exc = NullRunRateLimitRedisError(
            "Redis unavailable for aggregate rate limit"
        )
        rt = _mock_runtime_raising(exc)
        with pytest.raises(NullRunRateLimitRedisError) as excinfo:
            _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
        assert excinfo.value is exc
        assert excinfo.value.error_code == "NR-R002", (
            "DEF-NR-A003-REWRAP-LOSS: NullRunRateLimitRedisError must "
            "keep its NR-R002 error_code; pre-fix the catch-all "
            "stamped NR-B001 and lost the 'Redis outage for "
            "aggregate rate limit (fail-CLOSED)' message."
        )

    # ── NullRunDecision subclass coverage ────────────────────────────

    def test_chain_error_propagates_with_chain_id(self):
        exc = NullRunChainError(
            "CHAIN_ORG_MISMATCH",
            chain_id="01a08b57-f176-79ce-ad1b-60b0184d1625",
            parent_execution_id=None,
            backend_code="CHAIN_ORG_MISMATCH",
            status_code=403,
        )
        rt = _mock_runtime_raising(exc)
        with pytest.raises(NullRunChainError) as excinfo:
            _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
        assert excinfo.value is exc
        assert excinfo.value.error_code == "NR-CH001"
        assert (
            excinfo.value.chain_id
            == "01a08b57-f176-79ce-ad1b-60b0184d1625"
        ), (
            "DEF-NR-A003-REWRAP-LOSS: NullRunChainError.chain_id "
            "must be preserved for the cookbook recovery path."
        )
        assert excinfo.value.backend_code == "CHAIN_ORG_MISMATCH"

    def test_workflow_inactive_error_propagates_with_workflow_id(self):
        exc = NullRunWorkflowInactiveError(
            "Workflow soft-deleted",
            workflow_id="wf-soft-deleted-123",
            status_code=403,
        )
        rt = _mock_runtime_raising(exc)
        with pytest.raises(NullRunWorkflowInactiveError) as excinfo:
            _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
        assert excinfo.value is exc
        assert excinfo.value.error_code == "NR-W004"
        assert excinfo.value.workflow_id == "wf-soft-deleted-123"

    def test_consume_overbudget_error_propagates_with_counter_attrs(self):
        """The CONSUME_OVERBUDGET invariant (NR-O001) carries
        ``execution_id`` / ``reserved_cents`` / ``max_allowed_cents``
        / ``actual_cost_cents`` for the cookbook recovery contract.
        Pre-fix the catch-all rewrap discarded every attr."""
        exc = NullRunConsumeOverbudgetError(
            "actual > reserved + epsilon",
            execution_id="01a08b57-f176-79ce-ad1b-60b0184d1625",
            reserved_cents=100,
            max_allowed_cents=101,
            actual_cost_cents=1000,
            epsilon_cents=1,
            status_code=422,
        )
        rt = _mock_runtime_raising(exc)
        with pytest.raises(NullRunConsumeOverbudgetError) as excinfo:
            _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
        assert excinfo.value is exc
        assert excinfo.value.error_code == "NR-O001"
        assert (
            excinfo.value.execution_id
            == "01a08b57-f176-79ce-ad1b-60b0184d1625"
        )
        assert excinfo.value.reserved_cents == 100
        assert excinfo.value.max_allowed_cents == 101
        assert excinfo.value.actual_cost_cents == 1000
        assert excinfo.value.epsilon_cents == 1

    def test_workflow_paused_propagates_with_resume_after(self):
        exc = WorkflowPausedException(
            workflow_id="wf-paused-1",
            reason="cooldown",
            resume_after=120.0,
        )
        rt = _mock_runtime_raising(exc)
        with pytest.raises(WorkflowPausedException) as excinfo:
            _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
        assert excinfo.value is exc
        assert excinfo.value.error_code == "NR-W003"
        assert excinfo.value.resume_after == 120.0
        assert excinfo.value.workflow_id == "wf-paused-1"

    # ── Regression guards ───────────────────────────────────────────

    def test_blocked_exception_still_passes_through(self):
        """NullRunBlockedException is a NullRunDecision subclass; the
        new umbrella arm MUST come AFTER the existing ``except
        NullRunBlockedException: raise`` arm so the typed-block
        path keeps propagating unchanged. This regression guard
        pins that ordering."""
        exc = NullRunBlockedException(
            workflow_id="wf-1", reason="denied by policy"
        )
        rt = _mock_runtime_raising(exc)
        with pytest.raises(NullRunBlockedException) as excinfo:
            _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
        assert excinfo.value is exc
        assert "denied by policy" in excinfo.value.reason

    def test_generic_transport_error_still_rewraps(self):
        """The fix must NOT make ALL NullRunTransportError pass
        through — only the typed Decision/Infrastructure subclasses
        that don't match the more specific arms. A plain
        NullRunTransportError with no typed leaf must still be
        rewrapped (preserving the fail-CLOSED contract for
        unclassified transport failures)."""
        exc = NullRunTransportError(
            "network blip",
            source=TransportErrorSource.NETWORK_ERROR,
            endpoint="/execute",
        )
        rt = _mock_runtime_raising(exc)
        with pytest.raises(NullRunBlockedException) as excinfo:
            _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
        # Not the typed Decision/Infrastructure leaf.
        assert not isinstance(excinfo.value, NullRunProtocolError)
        assert not isinstance(excinfo.value, NullRunRateLimitRedisError)
        assert not isinstance(excinfo.value, NullRunChainError)
        # Catch-all source mapping returns NR-B001 for NETWORK_ERROR.
        assert excinfo.value.error_code == "NR-B001", (
            "DEF-NR-A003-REWRAP-LOSS regression: a generic "
            "NullRunTransportError must still be rewrapped by the "
            "NullRunTransportError specific arm — the umbrella arms "
            "above must not silently widen pass-through to all "
            "NullRunTransportError subclasses."
        )

    def test_generic_backend_error_still_rewraps(self):
        """Same regression guard for NullRunBackendError: it IS a
        NullRunInfrastructureError subclass, so the new umbrella
        arm is downstream of the specific NullRunBackendError
        rewrap arm at line ~867. Verify the parent still rewraps
        (not pass-through) so the typed-leaf tests above stay
        scoped to leaves, not the whole InfrastructureError class."""
        exc = NullRunBackendError(
            "5xx blip", endpoint="/execute", status_code=503
        )
        rt = _mock_runtime_raising(exc)
        with pytest.raises(NullRunBlockedException) as excinfo:
            _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
        # Confirm the parent is rewrapped (not pass-through) — the
        # umbrella arms must not have widened pass-through to all
        # NullRunInfrastructureError subclasses.
        assert not isinstance(excinfo.value, NullRunBackendError)
        assert "GATEWAY_ERROR" in excinfo.value.reason
