"""DEF-NR-RUNTIME-BLOCK-TYPED (2026-09-10) — ``Runtime.execute``
block path MUST dispatch via ``_V3_ERROR_CODE_MAP`` so typed catalog
exceptions (``NullRunApprovalReplayRejectedError`` / NR-A015,
``NullRunBudgetError`` / NR-B004, ``NullRunToolBlockedError`` /
NR-T001, etc.) actually surface — NOT a base
``NullRunBlockedException`` with the wire SCREAMING_SNAKE code
attached as ``error_code``.

Pre-fix (runtime.py:3053-3140, before this commit):

  When ``self._transport.execute(**execute_kwargs)`` returned
  ``{"decision": "block", "details": {"error_code":
  "APPROVAL_REPLAY_REJECTED"}}``, the runtime ALWAYS raised the
  base ``NullRunBlockedException`` with
  ``error_code="APPROVAL_REPLAY_REJECTED"`` (the wire code) — a
  SCREAMING_SNAKE string, NOT the catalog ``NR-A015`` that
  ``format_user_message`` looks up. Cookbook recipes that branched
  on the typed catalog arm (``except
  NullRunApprovalReplayRejectedError:``) NEVER matched, fell
  through to the generic ``NullRunError`` arm, and the user saw
  ``FALLBACK_MESSAGE`` ("Something went wrong. Please try again.")
  instead of the typed catalog wording.

Post-fix (this commit):

  Layer-1 dispatch factored into
  ``Runtime._build_block_exception``. The helper imports
  ``_V3_ERROR_CODE_MAP`` from ``nullrun.transport`` and dispatches
  via ``typed_cls = _V3_ERROR_CODE_MAP.get(wire_error_code)``. If
  the wire code is in the catalog, the helper raises the typed
  class (e.g. ``NullRunApprovalReplayRejectedError`` for
  ``APPROVAL_REPLAY_REJECTED``), so cookbook ``except`` arms
  match. The class's ``error_code`` attribute is the catalog
  ``NR-A015`` (NOT the wire code), so ``format_user_message``
  yields the friendly catalog wording.

These tests pin BOTH the source shape (the runtime block path
imports + uses ``_V3_ERROR_CODE_MAP``) AND the runtime behavior
(wire-coded reasons propagate as the typed catalog class).

Wire payload convention (preserved by the fix): the constructor
captures ``**details`` into ``self.details`` and nests the wire
payload under ``self.details["details"]`` via the ``details=...``
kwarg. Cookbook code reads ``exc.details["details"]["..."]`` for
typed introspection; ``exc.details["details"]["mapped_class"]``
exposes the catalog class name as a back-compat shim.
"""
from __future__ import annotations

import re
from pathlib import Path

from nullrun.breaker.exceptions import (
    NullRunApprovalReplayRejectedError,
    NullRunBlockedException,
    NullRunBudgetError,
    NullRunToolBlockedError,
)
from nullrun.runtime import NullRunRuntime

SDK_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_PY = SDK_ROOT / "src" / "nullrun" / "runtime.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _build_block_exception_slice() -> str:
    """Return the source of ``_build_block_exception`` for the
    source-pin tests. Anchor on ``def _build_block_exception`` and
    walk to the next sibling ``def`` (4-space indent) inside the
    same class."""
    src = _read(RUNTIME_PY)
    start = src.find("    def _build_block_exception(")
    assert start != -1, (
        "DEF-NR-RUNTIME-BLOCK-TYPED: cannot locate "
        "Runtime._build_block_exception"
    )
    after_header = start + len("    def _build_block_exception(\n")
    m = re.search(
        r"^    (?:def |@|class )",
        src[after_header:],
        re.MULTILINE,
    )
    assert m, "could not locate end of _build_block_exception body"
    end = after_header + m.start()
    return src[start:end]


# ─── Source-pin tests ──────────────────────────────────────────────────────


class TestDefNrRuntimeBlockTypedSourcePin:
    """Pin the shape of the fix so a refactor that re-introduces
    the always-NRError wrap fails loudly."""

    def test_helper_imports_v3_error_code_map(self):
        """The helper MUST import ``_V3_ERROR_CODE_MAP`` from
        ``nullrun.transport`` so it can dispatch typed catalog
        classes."""
        body = _build_block_exception_slice()
        assert "from nullrun.transport import _V3_ERROR_CODE_MAP" in body, (
            "DEF-NR-RUNTIME-BLOCK-TYPED: _build_block_exception "
            "must import _V3_ERROR_CODE_MAP from nullrun.transport. "
            "Pre-fix the runtime always raised base "
            "NullRunBlockedException with the wire code as "
            "error_code, hiding the typed catalog from cookbook "
            "recipes."
        )

    def test_helper_dispatches_typed_cls(self):
        """The helper MUST look up
        ``typed_cls = _V3_ERROR_CODE_MAP.get(wire_error_code)``
        and instantiate it for the wire-coded reason. Pre-fix
        it always instantiated the base class."""
        body = _build_block_exception_slice()
        assert re.search(
            r"typed_cls\s*=\s*_V3_ERROR_CODE_MAP\.get\(", body
        ), (
            "DEF-NR-RUNTIME-BLOCK-TYPED: helper must dispatch via "
            "`_V3_ERROR_CODE_MAP.get(wire_error_code)` so the "
            "wire-coded reason maps to the typed catalog class."
        )
        assert "typed_cls(" in body, (
            "DEF-NR-RUNTIME-BLOCK-TYPED: helper must instantiate "
            "the typed class — pre-fix it only constructed the "
            "base NullRunBlockedException."
        )

    def test_helper_does_not_overwrite_catalog_code_with_wire_code(self):
        """Pre-fix, the runtime passed
        ``error_code=block_code=wire_error_code`` — overriding the
        catalog ``NR-A015`` with the wire
        ``APPROVAL_REPLAY_REJECTED``. The fix must NOT pass
        ``error_code=wire_error_code`` to the typed class. The
        typed class's class attribute (e.g. ``NR-A015``) must
        stay intact so ``format_user_message`` can look it up."""
        body = _build_block_exception_slice()
        m = re.search(r"return\s+typed_cls\(", body)
        assert m is not None, "typed_cls(...) return not found"
        # Walk forward from the return to the closing paren (allow
        # nested parens for type_specific_kwargs etc.).
        call_start = m.end() - len("typed_cls(")
        depth = 0
        end = None
        for i in range(call_start, len(body)):
            ch = body[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        assert end is not None, "could not find end of typed_cls(...) call"
        call_region = body[call_start:end]
        assert "error_code=" not in call_region, (
            "DEF-NR-RUNTIME-BLOCK-TYPED: typed_cls(...) call must "
            "NOT pass error_code=... — that would override the "
            "typed class's catalog code (NR-A015, NR-B004, etc.) "
            "with the wire SCREAMING_SNAKE code and defeat "
            "format_user_message."
        )


# ─── Behaviour tests (pin the runtime outcome of the fix) ─────────────────


class TestDefNrRuntimeBlockTypedBehavior:
    """Verify that a wire-coded ``decision: block`` with
    ``details.error_code`` in the catalog dispatch path raises the
    typed catalog class, NOT a base NullRunBlockedException."""

    def test_approval_replay_rejected_raises_typed(self):
        """A /execute response with ``decision: block`` and
        ``details.error_code: APPROVAL_REPLAY_REJECTED`` must
        raise ``NullRunApprovalReplayRejectedError`` (NR-A015),
        NOT the base ``NullRunBlockedException``."""
        result = {
            "decision": "block",
            "explanation": "approval grant already consumed",
            "details": {
                "error_code": "APPROVAL_REPLAY_REJECTED",
                "approval_id": "apr-test-123",
            },
        }
        exc = NullRunRuntime._build_block_exception(
            result=result,
            workflow_id="wf-abc",
            tool_name="refund_customer",
        )
        assert isinstance(exc, NullRunApprovalReplayRejectedError), (
            f"expected NullRunApprovalReplayRejectedError, got "
            f"{type(exc).__name__}"
        )
        assert exc.error_code == "NR-A015", (
            f"expected NR-A015 (catalog), got {exc.error_code!r} "
            "(wire code?)"
        )
        assert exc.approval_id == "apr-test-123", (
            f"expected approval_id forwarded from wire_details, "
            f"got {exc.approval_id!r}"
        )
        # CRITICAL: must NOT be the base class
        assert type(exc) is not NullRunBlockedException, (
            "DEF-NR-RUNTIME-BLOCK-TYPED: wire APPROVAL_REPLAY_REJECTED "
            "must surface as NullRunApprovalReplayRejectedError, not "
            "the base NullRunBlockedException."
        )

    def test_budget_hard_blocked_raises_typed(self):
        """A /execute response with
        ``details.error_code: BUDGET_HARD_BLOCKED`` must raise
        ``NullRunBudgetError`` (NR-B004), not the base."""
        result = {
            "decision": "block",
            "explanation": "Hard budget exceeded",
            "details": {
                "error_code": "BUDGET_HARD_BLOCKED",
                "budget_cents": 5000,
                "current_spend_cents": 5100,
            },
        }
        exc = NullRunRuntime._build_block_exception(
            result=result,
            workflow_id="wf-abc",
            tool_name="refund_customer",
        )
        assert isinstance(exc, NullRunBudgetError)
        assert exc.error_code == "NR-B004"
        # The wire payload is nested under exc.details["details"]
        # (back-compat convention — the constructor captures the
        # ``details=`` kwarg as ``self.details["details"]``).
        # Cookbook code reads budget_cents / current_spend_cents via
        # exc.details["details"]; only
        # NullRunBudgetRecheckFailedError (NR-B006) promotes them
        # to first-class attributes.
        wire_payload = exc.details.get("details") or {}
        assert wire_payload.get("budget_cents") == 5000
        assert wire_payload.get("current_spend_cents") == 5100
        assert wire_payload.get("mapped_class") == "NullRunBudgetError"

    def test_tool_blocked_raises_typed(self):
        """A /execute response with
        ``details.error_code: TOOL_BLOCKED`` must raise
        ``NullRunToolBlockedError``, not the base."""
        result = {
            "decision": "block",
            "explanation": "Tool bash is blocked",
            "details": {"error_code": "TOOL_BLOCKED"},
        }
        exc = NullRunRuntime._build_block_exception(
            result=result,
            workflow_id="wf-abc",
            tool_name="bash",
        )
        assert isinstance(exc, NullRunToolBlockedError)
        assert exc.error_code == "NR-T001"

    def test_unknown_wire_code_falls_back_to_base(self):
        """A wire code that is NOT in ``_V3_ERROR_CODE_MAP`` (drift
        between backend and SDK) must still surface on the base
        ``NullRunBlockedException`` with the wire code as
        ``error_code`` — the operator / cookbook code can still
        branch on ``exc.error_code``."""
        result = {
            "decision": "block",
            "explanation": "Unknown rejection",
            "details": {"error_code": "BRAND_NEW_CODE_FROM_BACKEND"},
        }
        exc = NullRunRuntime._build_block_exception(
            result=result,
            workflow_id="wf-abc",
            tool_name="refund_customer",
        )
        assert type(exc) is NullRunBlockedException
        # The wire code (NOT a catalog code) is the
        # ``error_code`` for drift visibility.
        assert exc.error_code == "BRAND_NEW_CODE_FROM_BACKEND"
        # mapped_class shim is preserved for back-compat
        wire_payload = exc.details.get("details") or {}
        assert wire_payload.get("mapped_class") == "NullRunBlockedException"

    def test_legacy_keyword_path_budget(self):
        """A /execute response with no wire code but with
        ``explanation: 'budget exceeded'`` must fall back to the
        legacy keyword path. Pre-fix the comment said this raised
        ``NullRunBudgetError`` but the construction always used
        the base ``NullRunBlockedException``; this test pins that
        the legacy path stays on the base class (the wire-code
        path is the one that dispatches via ``_V3_ERROR_CODE_MAP``)
        while ``error_code`` is set to ``NR-B004`` for back-compat
        branches on ``exc.error_code == "NR-B004"``."""
        result = {
            "decision": "block",
            "explanation": "Your budget was exceeded",
            "details": {},
        }
        exc = NullRunRuntime._build_block_exception(
            result=result,
            workflow_id="wf-abc",
            tool_name="refund_customer",
        )
        assert type(exc) is NullRunBlockedException
        assert exc.error_code == "NR-B004"
        wire_payload = exc.details.get("details") or {}
        assert wire_payload.get("mapped_class") == "NullRunBudgetError"

    def test_legacy_keyword_path_unknown_explanation_falls_back_to_x001(self):
        """A /execute response with no wire code AND no keyword
        match falls through to ``NR-X001`` on the base class."""
        result = {
            "decision": "block",
            "explanation": "Some unparseable reason",
            "details": {},
        }
        exc = NullRunRuntime._build_block_exception(
            result=result,
            workflow_id="wf-abc",
            tool_name="refund_customer",
        )
        assert type(exc) is NullRunBlockedException
        assert exc.error_code == "NR-X001"

    def test_format_user_message_yields_catalog_wording(self):
        """End-to-end: with the fix in place,
        ``format_user_message(NullRunApprovalReplayRejectedError)``
        returns the friendly NR-A015 wording — NOT the generic
        FALLBACK_MESSAGE that the user saw before the fix."""
        from nullrun.messages import FALLBACK_MESSAGE, format_user_message

        result = {
            "decision": "block",
            "explanation": "approval grant already consumed",
            "details": {
                "error_code": "APPROVAL_REPLAY_REJECTED",
                "approval_id": "apr-test-123",
            },
        }
        exc = NullRunRuntime._build_block_exception(
            result=result,
            workflow_id="wf-abc",
            tool_name="refund_customer",
        )
        msg = format_user_message(exc)
        assert msg != FALLBACK_MESSAGE, (
            "DEF-NR-RUNTIME-BLOCK-TYPED: format_user_message must "
            "yield the catalog wording, not FALLBACK_MESSAGE. "
            "Pre-fix the runtime hid the typed exception behind a "
            "base NullRunBlockedException(error_code='APPROVAL_REPLAY_REJECTED'), "
            "which the catalog could not resolve."
        )
        assert "approval has already been used" in msg
