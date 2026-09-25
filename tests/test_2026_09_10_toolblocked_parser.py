"""DEF-NR-TOOLBLOCKED-PARSER (2026-09-10) — TOOL_BLOCKED +
LOOP_DETECTED + 5 sibling catalog entries that map to
NullRunBlockedException subclasses were silently swallowed by the
parser's catalog-fallback branch.

Pre-fix (audit 2026-09-10):
  - ``nullrun/transport.py::_parse_v3_error_envelope`` had a
    final catalog-fallback branch at line ~2780:

      ``allowed = {"error_code", "user_action", ...}; forwarded = ...``
      ``instance = catalog(full_message, **forwarded)``

  - This generic fallback assumed ``catalog.__init__`` accepts
    a string as the first positional arg (the message). It
    worked for ``NullRunError``-base subclasses (Protocol,
    RateLimitRedis, Auth) which have ``(message, **kwargs)``
    signature.
  - It FAILED for ``NullRunBlockedException`` subclasses which
    require positional ``(workflow_id, reason, ...)`` — the
    string ``full_message`` ended up in ``workflow_id``, the
    ``reason`` arg was missing, ``TypeError`` was raised.
  - The TypeError escaped the parser and was caught by the
    catch-all ``except Exception: pass`` in Transport.execute
    (4xx handler) — surfacing the synthetic-block dict
    ``{"decision": "block", "explanation": "Gateway returned
    403"}`` instead of the typed NR-T001 / NR-Lxxx catalog
    line.
  - Affected catalog entries: TOOL_BLOCKED, LOOP_DETECTED,
    MODEL_REQUIRED, POLICY_UNCONFIGURED, TOO_MANY_PENDING_APPROVALS,
    BUSINESS_IMPACT_INVALID, VALIDATION_FAILED.
  - User-visible symptom: a /execute call returning
    ``{"error_code": "TOOL_BLOCKED", "details": {"workflow_id":
    "wf-1", "tool_name": "dangerous.tool"}}`` yielded a synthetic
    block dict with no NR-code, no ``tool_name`` to recover, no
    catalog line ("This tool is in the workflow's block list.
    Remove it ...").

Post-fix:
  - Added a dedicated dispatch branch BEFORE the final catalog
    fallback. Detects ``catalog is NullRunToolBlockedError`` or
    ``catalog is NullRunBlockedException`` and calls the
    constructor with the right (workflow_id, reason, status_code,
    tool_name) signature. ``forwarded`` is passed through so
    catalog value's defaults win.

These tests pin BOTH the source shape AND the runtime behavior
so a future refactor that reverts to the broken generic fallback
fails the test.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
import respx

from nullrun.breaker.exceptions import (
    NullRunBlockedException,
    NullRunToolBlockedError,
)
from nullrun.transport import FallbackMode, Transport, _parse_v3_error_envelope

SDK_ROOT = Path(__file__).resolve().parent.parent
TRANSPORT_PY = SDK_ROOT / "src" / "nullrun" / "transport.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _v3_envelope(error_code: str, status: int = 400, **details) -> httpx.Response:
    body = {
        "error_code": error_code,
        "error_message": f"Backend says {error_code}",
        "details": details,
    }
    return httpx.Response(status, json=body)


_EXECUTE_URL = "https://api.test.nullrun.io/api/v1/execute"


@pytest.fixture
def transport():
    t = Transport(
        api_url="https://api.test.nullrun.io",
        api_key="test-key-12345678",
    )
    yield t
    t.stop()


def _execute_kwargs():
    return dict(
        organization_id="ws-123",
        execution_id="exec-" + "a" * 32,
        trace_id="trace-789",
        tool="my.tool",
        input_data={},
        on_transport_error="raise",
        fallback_mode=FallbackMode.STRICT,
    )


# ─── Source-pin tests ─────────────────────────────────────────────


class TestDefNrToolblockedParserSourcePin:
    """Pin the shape of the fix so a refactor that removes the
    dedicated dispatch branch fails loudly."""

    def test_dedicated_branch_present(self):
        src = _read(TRANSPORT_PY)
        # The fix added an ``if catalog is NullRunToolBlockedError
        # or catalog is NullRunBlockedException:`` branch BEFORE
        # the generic ``catalog(full_message, **forwarded)``
        # fallback.
        assert re.search(
            r"catalog\s+is\s+NullRunToolBlockedError\s*\n\s+or\s+catalog\s+is\s+NullRunBlockedException",
            src,
        ), (
            "DEF-NR-TOOLBLOCKED-PARSER: the dedicated "
            "NullRunToolBlockedError / NullRunBlockedException "
            "dispatch branch must be present in "
            "_parse_v3_error_envelope. Pre-fix the generic "
            "catalog-fallback called "
            "``catalog(full_message, **forwarded)`` which "
            "raised TypeError because "
            "NullRunBlockedException.__init__ requires positional "
            "(workflow_id, reason)."
        )

    def test_branch_in_parser(self):
        """Pin that the fix lives in ``_parse_v3_error_envelope``,
        not somewhere else (defense against a refactor that moves
        it to a different layer where it can't intercept the
        TypeError)."""
        src = _read(TRANSPORT_PY)
        # Locate the _parse_v3_error_envelope function body and
        # confirm the dedicated branch lives inside it.
        fn_match = re.search(
            r"def _parse_v3_error_envelope\(.*?(?=\ndef |\nclass |\Z)",
            src,
            re.DOTALL,
        )
        assert fn_match, "could not locate _parse_v3_error_envelope"
        fn_body = fn_match.group(0)
        assert "NullRunToolBlockedError" in fn_body, (
            "DEF-NR-TOOLBLOCKED-PARSER: NullRunToolBlockedError "
            "must be referenced inside _parse_v3_error_envelope "
            "(the dedicated dispatch branch lives there)."
        )

    def test_import_includes_blocked_exception_classes(self):
        """The function-local import block in
        ``_parse_v3_error_envelope`` must include both
        ``NullRunToolBlockedError`` and
        ``NullRunBlockedException`` — otherwise NameError at
        runtime even though the branch is present."""
        src = _read(TRANSPORT_PY)
        # Locate the function-local import block (the one inside
        # _parse_v3_error_envelope, NOT the module-level one).
        fn_match = re.search(
            r"def _parse_v3_error_envelope\(.*?(?=\ndef |\nclass |\Z)",
            src,
            re.DOTALL,
        )
        assert fn_match
        fn_body = fn_match.group(0)
        # Find the first ``from nullrun.breaker.exceptions import``
        # inside the function body.
        import_block = re.search(
            r"from nullrun\.breaker\.exceptions import \((.*?)\)",
            fn_body,
            re.DOTALL,
        )
        assert import_block, (
            "DEF-NR-TOOLBLOCKED-PARSER: could not locate "
            "function-local import block inside "
            "_parse_v3_error_envelope"
        )
        imported = import_block.group(1)
        assert "NullRunToolBlockedError" in imported, (
            "DEF-NR-TOOLBLOCKED-PARSER: NullRunToolBlockedError "
            "must be imported in the function-local block — the "
            "dedicated dispatch branch references it."
        )
        assert "NullRunBlockedException" in imported, (
            "DEF-NR-TOOLBLOCKED-PARSER: NullRunBlockedException "
            "must be imported in the function-local block — the "
            "dedicated dispatch branch references it."
        )

    def test_branch_comment_tag_present(self):
        """The fix introduced a long comment naming
        DEF-NR-TOOLBLOCKED-PARSER. Pin so a future maintainer who
        deletes the comment is forced to read the code's
        history."""
        src = _read(TRANSPORT_PY)
        assert "DEF-NR-TOOLBLOCKED-PARSER" in src, (
            "DEF-NR-TOOLBLOCKED-PARSER: the explainer comment "
            "block must name the fix tag so future readers can "
            "grep for it."
        )

    def test_branch_uses_correct_constructor_signature(self):
        """The dedicated branch must call
        ``catalog(workflow_id=..., reason=..., status_code=...,
        tool_name=..., **forwarded)`` — not the broken
        ``catalog(full_message, **forwarded)`` form for these
        classes."""
        src = _read(TRANSPORT_PY)
        # Extract the body of the new branch (between
        # ``catalog is NullRunToolBlockedError`` and the next
        # ``return cast(Exception, instance)``).
        m = re.search(
            r"if \(\s*\n\s*catalog is NullRunToolBlockedError.*?"
            r"return cast\(Exception, instance\)",
            src,
            re.DOTALL,
        )
        assert m, (
            "DEF-NR-TOOLBLOCKED-PARSER: could not parse the "
            "dedicated branch body"
        )
        body = m.group(0)
        # Must pass workflow_id as kwarg, NOT as positional.
        assert "workflow_id=str(details.get(" in body, (
            "DEF-NR-TOOLBLOCKED-PARSER: dedicated branch must "
            "pass workflow_id via the details payload "
            "(``workflow_id=str(details.get('workflow_id') "
            "or 'unknown')``)."
        )
        assert "reason=full_message" in body, (
            "DEF-NR-TOOLBLOCKED-PARSER: dedicated branch must "
            "pass ``reason=full_message`` — the message is the "
            "second positional arg in NullRunBlockedException."
        )
        assert "status_code=status" in body, (
            "DEF-NR-TOOLBLOCKED-PARSER: dedicated branch must "
            "pass status_code so the typed exception carries "
            "the wire status (403 for TOOL_BLOCKED)."
        )
        # Must NOT call catalog(full_message, ...) directly
        # (that's the broken generic-fallback signature).
        assert "catalog(\n                full_message" not in body, (
            "DEF-NR-TOOLBLOCKED-PARSER: dedicated branch must "
            "NOT use ``catalog(full_message, ...)`` — that's "
            "the broken form that raises TypeError for "
            "NullRunBlockedException subclasses."
        )


# ─── Behavioral tests (parser-level) ──────────────────────────────


class TestDefNrToolblockedParserBehavior:
    """Pin the runtime behavior — each catalog entry now yields
    the correct typed exception with first-class attrs."""

    def test_tool_blocked_yields_tool_blocked_error(self):
        body = _v3_envelope(
            "TOOL_BLOCKED", status=403,
            workflow_id="wf-1",
            tool_name="dangerous.tool",
        )
        exc = _parse_v3_error_envelope(body, "execute")
        assert isinstance(exc, NullRunToolBlockedError), (
            f"DEF-NR-TOOLBLOCKED-PARSER: TOOL_BLOCKED must yield "
            f"NullRunToolBlockedError (NR-T001). Got "
            f"{type(exc).__name__}. Pre-fix the parser raised "
            f"TypeError which was swallowed by the catch-all "
            f"``except Exception: pass`` in Transport.execute."
        )
        assert exc.error_code == "NR-T001"
        assert exc.tool_name == "dangerous.tool", (
            "DEF-NR-TOOLBLOCKED-PARSER: NullRunToolBlockedError."
            "tool_name must be preserved for the cookbook "
            "recovery contract."
        )
        assert exc.workflow_id == "wf-1"
        assert exc.status_code == 403

    def test_loop_detected_yields_blocked_exception(self):
        body = _v3_envelope(
            "LOOP_DETECTED", status=403,
            workflow_id="wf-loop-1",
        )
        exc = _parse_v3_error_envelope(body, "execute")
        assert isinstance(exc, NullRunBlockedException)
        assert exc.workflow_id == "wf-loop-1"
        assert exc.status_code == 403

    def test_model_required_yields_blocked_exception(self):
        body = _v3_envelope(
            "MODEL_REQUIRED", status=403,
            workflow_id="wf-model-1",
        )
        exc = _parse_v3_error_envelope(body, "execute")
        assert isinstance(exc, NullRunBlockedException)
        assert exc.workflow_id == "wf-model-1"

    def test_policy_unconfigured_yields_blocked_exception(self):
        body = _v3_envelope(
            "POLICY_UNCONFIGURED", status=403,
            workflow_id="wf-unconfigured",
        )
        exc = _parse_v3_error_envelope(body, "execute")
        assert isinstance(exc, NullRunBlockedException)
        assert exc.workflow_id == "wf-unconfigured"

    def test_too_many_pending_approvals_yields_blocked_exception(self):
        body = _v3_envelope(
            "TOO_MANY_PENDING_APPROVALS", status=403,
            workflow_id="wf-busy-approvals",
        )
        exc = _parse_v3_error_envelope(body, "execute")
        assert isinstance(exc, NullRunBlockedException)
        assert exc.workflow_id == "wf-busy-approvals"

    def test_business_impact_invalid_yields_blocked_exception(self):
        body = _v3_envelope(
            "BUSINESS_IMPACT_INVALID", status=400,
            workflow_id="wf-impact-bad",
        )
        exc = _parse_v3_error_envelope(body, "execute")
        assert isinstance(exc, NullRunBlockedException)
        assert exc.workflow_id == "wf-impact-bad"
        assert exc.status_code == 400

    def test_validation_failed_with_no_workflow_id_defaults_to_unknown(self):
        """When ``workflow_id`` is missing from details (e.g. a
        bare VALIDATION_FAILED envelope), the parser must still
        produce a typed exception — defaulted to ``"unknown"``
        rather than raising TypeError or swallowing to a
        synthetic block."""
        body = _v3_envelope("VALIDATION_FAILED", status=400)
        exc = _parse_v3_error_envelope(body, "execute")
        assert isinstance(exc, NullRunBlockedException), (
            f"DEF-NR-TOOLBLOCKED-PARSER: VALIDATION_FAILED must "
            f"yield NullRunBlockedException even without "
            f"workflow_id in details. Got {type(exc).__name__}."
        )
        assert exc.workflow_id == "unknown", (
            "DEF-NR-TOOLBLOCKED-PARSER: missing workflow_id "
            "must default to ``'unknown'`` (mirrors the "
            "NullRunBudgetError pattern at line ~2729)."
        )
        assert exc.status_code == 400


# ─── Behavioral tests (end-to-end through Transport.execute) ────────


class TestDefNrToolblockedTransportEndToEnd:
    """Pin the end-to-end runtime behavior — Transport.execute
    propagates the typed exception instead of swallowing it into
    a synthetic block dict. Pre-fix the TypeError from the
    parser escaped the catch-fan-in (no typed arm matched
    ``TypeError``) and got caught by ``except Exception: pass``,
    returning ``{"decision": "block", "decision_source":
    "fallback", "explanation": "Gateway returned 403"}``."""

    @respx.mock
    def test_tool_blocked_does_not_swallow_to_synthetic_block(
        self, transport,
    ):
        respx.post(_EXECUTE_URL).mock(
            return_value=_v3_envelope(
                "TOOL_BLOCKED", status=403,
                workflow_id="wf-1",
                tool_name="dangerous.tool",
            )
        )
        # Pre-fix this returned a synthetic-block dict. Post-fix
        # the catch-fan-in's ``except NullRunBlockedException:
        # raise`` arm re-raises the typed exception.
        with pytest.raises(NullRunToolBlockedError) as excinfo:
            transport.execute(**_execute_kwargs())
        assert excinfo.value.error_code == "NR-T001"
        assert excinfo.value.tool_name == "dangerous.tool"
        assert excinfo.value.workflow_id == "wf-1"

    @respx.mock
    def test_loop_detected_does_not_swallow_to_synthetic_block(
        self, transport,
    ):
        respx.post(_EXECUTE_URL).mock(
            return_value=_v3_envelope(
                "LOOP_DETECTED", status=403,
                workflow_id="wf-loop",
            )
        )
        with pytest.raises(NullRunBlockedException) as excinfo:
            transport.execute(**_execute_kwargs())
        assert excinfo.value.workflow_id == "wf-loop"
