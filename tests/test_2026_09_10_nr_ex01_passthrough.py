"""DEF-NR-EX01-REWRAP-LOSS (2026-09-10) — `_enforce_sensitive_tool` must
let ``NullRunExecutionNotFoundError`` (NR-EX01) propagate unchanged.

Pre-fix (audit 2026-09-10):
  - ``nullrun/decorators.py::_enforce_sensitive_tool`` had three except
    arms: ``NullRunBlockedException`` (pass-through), ``NullRunTransportError``
    (rewrap to ``NullRunBlockedException(NR-B00X)``), and ``Exception``
    (catch-all rewrap).
  - ``NullRunExecutionNotFoundError`` is a subclass of
    ``NullRunBackendError`` which is a subclass of
    ``NullRunTransportError``. The MRO puts it inside the second arm, so
    the typed exception was being unwrapped into a generic
    ``NullRunBlockedException(error_code="NR-B002")`` with reason
    "policy engine unavailable: ...".
  - User-visible symptom (per ``langgraph_openai_approval_demo.py``):
    3rd refund → backend 404 EXECUTION_NOT_FOUND → SDK prints
    "Our service is temporarily unavailable. Please try again shortly."
    (NR-B002) instead of the documented NR-EX01 line
    "There's a configuration issue. Please contact support."
  - Cookbook pattern ``except NullRunExecutionNotFoundError`` never
    matched because the exception class was lost in the rewrap.

Post-fix:
  - Added a dedicated pass-through arm BEFORE ``except
    NullRunBlockedException`` so the typed exception propagates
    unchanged. Cookbook code can introspect ``exc.execution_id``,
    ``exc.endpoint``, and ``exc.regate_required`` for the documented
    recovery path (re-issue /api/v1/gate, then retry /execute).

These tests pin BOTH the source shape AND the runtime behavior so a
future refactor that re-introduces a rewrap (e.g. reorders the except
arms) fails the test.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nullrun.breaker.exceptions import (
    NullRunBlockedException,
    NullRunExecutionNotFoundError,
    NullRunTransportError,
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


class TestDefNrEx01SourcePin:
    """Pin the shape of the fix so a refactor that reorders / removes
    the pass-through arm fails loudly."""

    def test_pass_through_arm_is_present(self):
        body = _enforce_sensitive_tool_body()
        assert "except NullRunExecutionNotFoundError:" in body, (
            "DEF-NR-EX01-REWRAP-LOSS: the pass-through arm for "
            "NullRunExecutionNotFoundError must be present in "
            "_enforce_sensitive_tool. Pre-fix the typed exception was "
            "swallowed by the except NullRunTransportError arm and "
            "rewrapped as NullRunBlockedException(NR-B00X)."
        )

    def test_pass_through_arm_appears_before_blocked_arm(self):
        body = _enforce_sensitive_tool_body()
        # Order matters: the pass-through arm must come BEFORE
        # ``except NullRunBlockedException`` because Python evaluates
        # except arms top-to-bottom. If a future refactor moves it
        # after, the typed exception would still be caught by the
        # next arm (it isn't a NullRunBlockedException, so this is
        # defensive — but the contract is "before blocked arm").
        nr_ex01_idx = body.find("except NullRunExecutionNotFoundError:")
        blocked_idx = body.find("except NullRunBlockedException:")
        assert nr_ex01_idx != -1, (
            "DEF-NR-EX01-REWRAP-LOSS: pass-through arm missing"
        )
        assert blocked_idx != -1, (
            "DEF-NR-EX01-REWRAP-LOSS: NullRunBlockedException arm missing"
        )
        assert nr_ex01_idx < blocked_idx, (
            "DEF-NR-EX01-REWRAP-LOSS: the pass-through arm must "
            "appear BEFORE the except NullRunBlockedException arm. "
            "Pre-fix order swallowed the typed exception via the "
            "NullRunTransportError arm below."
        )

    def test_pass_through_arm_only_raises(self):
        body = _enforce_sensitive_tool_body()
        # Locate the arm and verify it ONLY contains ``raise`` — no
        # error_code stamping, no reason prefixing, no rewrap. Strip
        # comment lines first so the explanatory comment (which
        # legitimately names NullRunBlockedException to explain what
        # WOULD happen without the fix) does not trip the check.
        m = re.search(
            r"except NullRunExecutionNotFoundError:\s*\n(.*?)(?=\n    except |\Z)",
            body,
            re.DOTALL,
        )
        assert m, (
            "DEF-NR-EX01-REWRAP-LOSS: could not parse the pass-through "
            "arm body"
        )
        arm_body = m.group(1)
        # Drop comment-only lines for the negative assertion; the
        # comment block legitimately references NullRunBlockedException
        # to explain the regression we're guarding against.
        executable_lines = [
            ln for ln in arm_body.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        executable = "\n".join(executable_lines)
        # The arm MUST contain `raise` and the executable body MUST
        # NOT rewrap into NullRunBlockedException.
        assert "raise" in executable, (
            "DEF-NR-EX01-REWRAP-LOSS: pass-through arm must re-raise "
            "(not swallow). Empty arm would silently drop the typed "
            "exception."
        )
        assert "NullRunBlockedException" not in executable, (
            "DEF-NR-EX01-REWRAP-LOSS: pass-through arm must NOT "
            "rewrap into NullRunBlockedException. Pre-fix this was the "
            "exact bug — NullRunExecutionNotFoundError was being "
            "unwrapped into NullRunBlockedException(NR-B00X)."
        )

    def test_pass_through_arm_comment_tag_present(self):
        body = _enforce_sensitive_tool_body()
        # The fix introduced a long comment naming
        # DEF-NR-EX01-REWRAP-LOSS. Pin so a future maintainer who
        # deletes the comment is forced to read the code's history.
        assert "DEF-NR-EX01-REWRAP-LOSS" in body, (
            "DEF-NR-EX01-REWRAP-LOSS: the explainer comment block must "
            "name the fix tag so future readers can grep for it."
        )

    def test_import_includes_nullrun_execution_not_found_error(self):
        src = _read(DECORATORS_PY)
        # The function-local import block at line ~809 must include
        # NullRunExecutionNotFoundError; otherwise NameError at
        # runtime even though the except arm is present.
        assert "NullRunExecutionNotFoundError" in src, (
            "DEF-NR-EX01-REWRAP-LOSS: NullRunExecutionNotFoundError "
            "must be imported in decorators.py for the pass-through "
            "arm to bind. Check the function-local import block "
            "(around line 809)."
        )


# ─── Behavioral tests (mirror test_protect.py:651 style) ──────────────


class TestDefNrEx01Behavior:
    """Pin the runtime behavior — the typed exception propagates with
    error_code + execution_id + regate_required intact."""

    def _mock_runtime_raising(self, exc: Exception) -> MagicMock:
        rt = MagicMock()
        rt.is_sensitive_tool.return_value = True
        rt.execute.side_effect = exc
        return rt

    def test_execution_not_found_propagates_unchanged(self):
        """The core fix: NullRunExecutionNotFoundError reaches the
        caller WITHOUT being rewrapped."""
        exc = NullRunExecutionNotFoundError(
            "execution binding not found",
            execution_id="01a08b57-f176-79ce-ad1b-60b0184d1625",
            endpoint="/api/v1/execute",
            status_code=404,
        )
        rt = self._mock_runtime_raising(exc)
        with pytest.raises(NullRunExecutionNotFoundError) as excinfo:
            _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
        # The exact same instance must propagate (identity check) —
        # no rewrap, no chained from.
        assert excinfo.value is exc, (
            "DEF-NR-EX01-REWRAP-LOSS: NullRunExecutionNotFoundError "
            "must propagate unchanged. A rewrap would have replaced "
            "the instance with a NullRunBlockedException."
        )

    def test_execution_not_found_preserves_error_code(self):
        """error_code must remain NR-EX01, not NR-B00X."""
        exc = NullRunExecutionNotFoundError(
            "execution binding not found",
            execution_id="01a08b57-f176-79ce-ad1b-60b0184d1625",
            endpoint="/api/v1/execute",
            status_code=404,
        )
        rt = self._mock_runtime_raising(exc)
        with pytest.raises(NullRunExecutionNotFoundError) as excinfo:
            _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
        assert excinfo.value.error_code == "NR-EX01", (
            f"DEF-NR-EX01-REWRAP-LOSS: error_code must remain NR-EX01 "
            f"on the propagated exception; got {excinfo.value.error_code!r}. "
            "Pre-fix the rewrap stamped NR-B001/B002 from the "
            "TransportErrorSource mapping."
        )

    def test_execution_not_found_preserves_execution_id_attr(self):
        """Cookbook recovery depends on ``exc.execution_id`` being
        readable. Pre-fix this attr was lost in the rewrap because
        NullRunBlockedException doesn't carry an ``execution_id``
        first-class attribute."""
        exc = NullRunExecutionNotFoundError(
            "execution binding not found",
            execution_id="01a08b57-f176-79ce-ad1b-60b0184d1625",
            endpoint="/api/v1/execute",
            status_code=404,
        )
        rt = self._mock_runtime_raising(exc)
        with pytest.raises(NullRunExecutionNotFoundError) as excinfo:
            _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
        assert excinfo.value.execution_id == "01a08b57-f176-79ce-ad1b-60b0184d1625", (
            "DEF-NR-EX01-REWRAP-LOSS: exc.execution_id must be "
            "preserved for the cookbook recovery path (re-issue "
            "/api/v1/gate, then retry /execute)."
        )
        assert excinfo.value.regate_required is True, (
            "DEF-NR-EX01-REWRAP-LOSS: exc.regate_required must be "
            "True so callers can branch on 're-issue /gate' vs other "
            "recovery paths."
        )

    def test_execution_not_found_format_user_message_returns_nr_ex01_line(self):
        """The NR-EX01 catalog line ('There's a configuration issue.
        Please contact support.') must be reachable through
        ``format_user_message`` after the @protect pass-through."""
        from nullrun.messages import format_user_message

        exc = NullRunExecutionNotFoundError(
            "execution binding not found",
            execution_id="01a08b57-f176-79ce-ad1b-60b0184d1625",
            endpoint="/api/v1/execute",
            status_code=404,
        )
        rt = self._mock_runtime_raising(exc)
        with pytest.raises(NullRunExecutionNotFoundError) as excinfo:
            _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
        msg = format_user_message(excinfo.value)
        assert "configuration issue" in msg.lower(), (
            f"DEF-NR-EX01-REWRAP-LOSS: format_user_message must yield "
            f"the NR-EX01 catalog line ('There's a configuration "
            f"issue. Please contact support.'). Got: {msg!r}. "
            "Pre-fix the rewrap yielded NR-B002 'Our service is "
            "temporarily unavailable. Please try again shortly.' — "
            "misleading, suggests retry will help when the binding "
            "is permanently gone for this execution_id."
        )

    def test_other_transport_errors_still_rewrap_to_blocked(self):
        """Regression guard: the fix must NOT make ALL transport
        errors pass through — only the typed NR-EX01 one. Generic
        NullRunTransportError must still be rewrapped as
        NullRunBlockedException(NR-B00X)."""
        from nullrun.breaker.exceptions import TransportErrorSource

        exc = NullRunTransportError(
            "network blip",
            source=TransportErrorSource.NETWORK_ERROR,
            endpoint="/execute",
        )
        rt = self._mock_runtime_raising(exc)
        with pytest.raises(NullRunBlockedException) as excinfo:
            _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
        # Must NOT be NullRunExecutionNotFoundError — generic
        # transport failures still get the B001 rewrap.
        assert not isinstance(excinfo.value, NullRunExecutionNotFoundError)
        assert "NETWORK_ERROR" in excinfo.value.reason, (
            "DEF-NR-EX01-REWRAP-LOSS regression: generic "
            "NullRunTransportError must still be rewrapped as "
            "NullRunBlockedException with the transport source in "
            "the reason. The fix was scoped to NR-EX01 only — it "
            "must not silently widen the pass-through to all "
            "NullRunTransportError subclasses."
        )

    def test_blocked_exception_still_passes_through(self):
        """Regression guard: the existing ``except NullRunBlockedException``
        arm must keep working. Adding the new arm above it must not
        intercept the existing block-propagation path."""
        exc = NullRunBlockedException(workflow_id="wf-1", reason="denied by policy")
        rt = self._mock_runtime_raising(exc)
        with pytest.raises(NullRunBlockedException) as excinfo:
            _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
        assert excinfo.value is exc
        assert "denied by policy" in excinfo.value.reason
