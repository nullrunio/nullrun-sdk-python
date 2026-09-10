"""DEF-NR-R001-REWRAP-LOSS (2026-09-10) — RateLimitError must propagate
through ``@protect`` / ``_enforce_sensitive_tool`` with retry_after,
upgrade_url, body, and error_code=NR-R001 intact.

Pre-fix (audit 2026-09-10):
  - ``nullrun/decorators.py::_enforce_sensitive_tool`` had four except
    arms around the ``runtime.execute(...)`` call: a specific arm for
    NullRunExecutionNotFoundError (defense), NullRunBlockedException
    (pass-through), NullRunTransportError (rewrap via source -> NR-B00X
    code mapping), and Exception (catch-all NR-B001 rewrap).
  - ``RateLimitError`` (NR-R001, the typed 429 envelope) is a
    subclass of ``NullRunTransportError``. The MRO puts it inside the
    second arm, so the typed exception was being unwrapped into a
    generic ``NullRunBlockedException(error_code="NR-B002",
    reason="policy engine unavailable: GATEWAY_ERROR")``.
  - User-visible symptom (per cookbook ``register_sensitive_tools`` +
    @protect @sensitive flow that hits a 429 gateway response):
    The SDK prints "Our service is temporarily unavailable. Please
    try again shortly." (NR-B002) instead of the documented NR-R001
    line "The NullRun backend rate-limited this API key. Wait
    ``retry_after`` seconds (or upgrade the plan) before retrying."
    The ``exc.retry_after`` attribute was lost, blocking the
    documented "sleep retry_after then retry" cookbook pattern.
  - Cookbook pattern ``except RateLimitError`` never matched because
    the exception class was lost in the rewrap.

Post-fix:
  - Added a dedicated pass-through arm BEFORE
    ``except NullRunBlockedException`` so the typed exception
    propagates with error_code=NR-R001, retry_after (seconds, from
    the gateway's ``retry_after_ms`` body field), upgrade_url (plan
    upgrade URL from the 429 body), and body (parsed 429 envelope)
    intact.

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
    NullRunBackendError,
    NullRunBlockedException,
    NullRunTransportError,
    RateLimitError,
    TransportErrorSource,
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


class TestDefNrR001SourcePin:
    """Pin the shape of the fix so a refactor that reorders / removes
    the pass-through arm fails loudly."""

    def test_pass_through_arm_is_present(self):
        body = _enforce_sensitive_tool_body()
        assert "except RateLimitError:" in body, (
            "DEF-NR-R001-REWRAP-LOSS: the pass-through arm for "
            "RateLimitError must be present in "
            "_enforce_sensitive_tool. Pre-fix the typed exception "
            "was swallowed by the except NullRunTransportError arm "
            "and rewrapped as NullRunBlockedException(NR-B002)."
        )

    def test_pass_through_arm_appears_before_transport_rewrap_arm(self):
        body = _enforce_sensitive_tool_body()
        # Order matters: the pass-through arm must come BEFORE
        # ``except NullRunTransportError as exc:`` because Python
        # evaluates except arms top-to-bottom. If a future refactor
        # moves it after, RateLimitError would still be caught by the
        # NullRunTransportError parent arm and rewrapped.
        r001_idx = body.find("except RateLimitError:")
        transport_idx = body.find("except NullRunTransportError")
        assert r001_idx != -1, (
            "DEF-NR-R001-REWRAP-LOSS: pass-through arm missing"
        )
        assert transport_idx != -1, (
            "DEF-NR-R001-REWRAP-LOSS: NullRunTransportError arm missing"
        )
        assert r001_idx < transport_idx, (
            "DEF-NR-R001-REWRAP-LOSS: the RateLimitError pass-through "
            "arm must appear BEFORE the except NullRunTransportError "
            "rewrap arm. Pre-fix order swallowed the typed exception."
        )

    def test_pass_through_arm_only_raises(self):
        body = _enforce_sensitive_tool_body()
        # Locate the arm and verify it ONLY contains ``raise`` — no
        # error_code stamping, no reason prefixing, no rewrap. Strip
        # comment lines first so the explanatory comment (which
        # legitimately names NullRunBlockedException to explain what
        # WOULD happen without the fix) does not trip the check.
        m = re.search(
            r"except RateLimitError:\s*\n(.*?)(?=\n    except |\Z)",
            body,
            re.DOTALL,
        )
        assert m, (
            "DEF-NR-R001-REWRAP-LOSS: could not parse the pass-through "
            "arm body"
        )
        arm_body = m.group(1)
        # Drop comment-only lines for the negative assertion.
        executable_lines = [
            ln for ln in arm_body.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        executable = "\n".join(executable_lines)
        # The arm MUST contain `raise` and the executable body MUST
        # NOT rewrap into NullRunBlockedException.
        assert "raise" in executable, (
            "DEF-NR-R001-REWRAP-LOSS: pass-through arm must re-raise "
            "(not swallow). Empty arm would silently drop the typed "
            "exception."
        )
        assert "NullRunBlockedException" not in executable, (
            "DEF-NR-R001-REWRAP-LOSS: pass-through arm must NOT "
            "rewrap into NullRunBlockedException. Pre-fix this was "
            "the exact bug — RateLimitError was being unwrapped into "
            "NullRunBlockedException(NR-B002)."
        )

    def test_pass_through_arm_comment_tag_present(self):
        body = _enforce_sensitive_tool_body()
        # The fix introduced a long comment naming
        # DEF-NR-R001-REWRAP-LOSS. Pin so a future maintainer who
        # deletes the comment is forced to read the code's history.
        assert "DEF-NR-R001-REWRAP-LOSS" in body, (
            "DEF-NR-R001-REWRAP-LOSS: the explainer comment block "
            "must name the fix tag so future readers can grep for it."
        )

    def test_import_includes_rate_limit_error(self):
        src = _read(DECORATORS_PY)
        # The function-local import block at line ~809 must include
        # RateLimitError; otherwise NameError at runtime even though
        # the except arm is present.
        assert "RateLimitError" in src, (
            "DEF-NR-R001-REWRAP-LOSS: RateLimitError must be imported "
            "in decorators.py for the pass-through arm to bind. Check "
            "the function-local import block (around line 809)."
        )


# ─── Behavioral tests (mirror test_protect.py:651 style) ─────────────


class TestDefNrR001Behavior:
    """Pin the runtime behavior — the typed exception propagates with
    error_code + retry_after + upgrade_url + body intact."""

    def _mock_runtime_raising(self, exc: Exception) -> MagicMock:
        rt = MagicMock()
        rt.is_sensitive_tool.return_value = True
        rt.execute.side_effect = exc
        return rt

    def test_rate_limit_propagates_unchanged(self):
        """The core fix: RateLimitError reaches the caller WITHOUT
        being rewrapped."""
        exc = RateLimitError(
            "rate limited",
            source=TransportErrorSource.GATEWAY_ERROR,
            endpoint="/api/v1/execute",
            retry_after=30.0,
            upgrade_url="https://app.nullrun.io/upgrade?key=abc",
            body={"error": "RATE_LIMIT_EXCEEDED", "retry_after_ms": 30000},
        )
        rt = self._mock_runtime_raising(exc)
        with pytest.raises(RateLimitError) as excinfo:
            _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
        # The exact same instance must propagate (identity check) —
        # no rewrap, no chained from.
        assert excinfo.value is exc, (
            "DEF-NR-R001-REWRAP-LOSS: RateLimitError must propagate "
            "unchanged. A rewrap would have replaced the instance "
            "with a NullRunBlockedException."
        )

    def test_rate_limit_preserves_error_code(self):
        """error_code must remain NR-R001, not NR-B002."""
        exc = RateLimitError(
            "rate limited",
            source=TransportErrorSource.GATEWAY_ERROR,
            endpoint="/api/v1/execute",
            retry_after=30.0,
        )
        rt = self._mock_runtime_raising(exc)
        with pytest.raises(RateLimitError) as excinfo:
            _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
        assert excinfo.value.error_code == "NR-R001", (
            f"DEF-NR-R001-REWRAP-LOSS: error_code must remain NR-R001 "
            f"on the propagated exception; got "
            f"{excinfo.value.error_code!r}. Pre-fix the rewrap stamped "
            f"NR-B002 from the GATEWAY_ERROR -> NR-B002 source mapping."
        )

    def test_rate_limit_preserves_retry_after(self):
        """Cookbook recovery depends on ``exc.retry_after`` being
        readable. Pre-fix this attr was lost in the rewrap because
        NullRunBlockedException doesn't carry a ``retry_after``
        first-class attribute (the FastAPI integration reads it via
        ``getattr(exc, "retry_after")`` to set the HTTP ``Retry-After``
        header — a silent failure if the attr is missing)."""
        exc = RateLimitError(
            "rate limited",
            source=TransportErrorSource.GATEWAY_ERROR,
            endpoint="/api/v1/execute",
            retry_after=42.5,
        )
        rt = self._mock_runtime_raising(exc)
        with pytest.raises(RateLimitError) as excinfo:
            _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
        assert excinfo.value.retry_after == 42.5, (
            "DEF-NR-R001-REWRAP-LOSS: exc.retry_after must be "
            "preserved for the cookbook recovery path (sleep "
            "retry_after seconds then retry /gate + /execute)."
        )

    def test_rate_limit_preserves_upgrade_url_and_body(self):
        """Cookbook / FastAPI integration reads ``exc.upgrade_url`` to
        surface a billing-upgrade prompt and ``exc.body`` for
        diagnostics. Both must survive the @protect flow."""
        body = {"error": "RATE_LIMIT_EXCEEDED", "retry_after_ms": 30000}
        exc = RateLimitError(
            "rate limited",
            source=TransportErrorSource.GATEWAY_ERROR,
            endpoint="/api/v1/execute",
            retry_after=30.0,
            upgrade_url="https://app.nullrun.io/upgrade?plan=pro",
            body=body,
        )
        rt = self._mock_runtime_raising(exc)
        with pytest.raises(RateLimitError) as excinfo:
            _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
        assert excinfo.value.upgrade_url == "https://app.nullrun.io/upgrade?plan=pro", (
            "DEF-NR-R001-REWRAP-LOSS: exc.upgrade_url must be "
            "preserved — FastAPI handler reads it for the upgrade "
            "prompt surface."
        )
        assert excinfo.value.body == body, (
            "DEF-NR-R001-REWRAP-LOSS: exc.body must be preserved "
            "for diagnostics."
        )

    def test_rate_limit_format_user_message_returns_nr_r001_line(self):
        """The NR-R001 catalog line ('rate-limited ... wait
        retry_after seconds ... or upgrade the plan') must be
        reachable through ``format_user_message`` after the @protect
        pass-through."""
        from nullrun.messages import format_user_message

        exc = RateLimitError(
            "rate limited",
            source=TransportErrorSource.GATEWAY_ERROR,
            endpoint="/api/v1/execute",
            retry_after=30.0,
        )
        rt = self._mock_runtime_raising(exc)
        with pytest.raises(RateLimitError) as excinfo:
            _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
        msg = format_user_message(excinfo.value)
        # The NR-R001 catalog line is rate-limit-themed
        # ("Too many requests. Please wait a moment and try again.")
        # and must NOT be the NR-B002 gateway-error line
        # ("Our service is temporarily unavailable. Please try again
        # shortly."), which would imply retry regardless of the
        # gateway's retry_after / upgrade hint.
        msg_lower = msg.lower()
        assert "temporarily unavailable" not in msg_lower, (
            f"DEF-NR-R001-REWRAP-LOSS: format_user_message yielded "
            f"the NR-B002 gateway-error line (which contains "
            f"'temporarily unavailable'). Got: {msg!r}. The typed "
            f"exception is being mapped through the "
            f"NullRunTransportError rewrap instead of "
            f"format_user_message reading the typed "
            f"error_code=NR-R001 directly."
        )
        # The NR-R001 catalog carries a rate-limit-themed phrase so
        # the user understands the cause is the API-key rate limit,
        # not the backend being down.
        assert (
            "too many requests" in msg_lower
            or "rate-limit" in msg_lower
            or "rate limit" in msg_lower
            or "retry" in msg_lower
        ), (
            f"DEF-NR-R001-REWRAP-LOSS: format_user_message must yield "
            f"a rate-limit-themed NR-R001 line. Got: {msg!r}."
        )

    def test_generic_transport_errors_still_rewrap_to_blocked(self):
        """Regression guard: the fix must NOT make ALL transport
        errors pass through — only the typed RateLimitError one.
        Generic NullRunTransportError must still be rewrapped as
        NullRunBlockedException(NR-B00X), preserving the existing
        fail-CLOSED contract for unclassified transport failures."""
        exc = NullRunTransportError(
            "network blip",
            source=TransportErrorSource.NETWORK_ERROR,
            endpoint="/execute",
        )
        rt = self._mock_runtime_raising(exc)
        with pytest.raises(NullRunBlockedException) as excinfo:
            _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
        # Must NOT be RateLimitError — generic transport failures
        # still get the B001 rewrap from the NETWORK_ERROR source.
        assert not isinstance(excinfo.value, RateLimitError)
        assert "NETWORK_ERROR" in excinfo.value.reason, (
            "DEF-NR-R001-REWRAP-LOSS regression: generic "
            "NullRunTransportError must still be rewrapped as "
            "NullRunBlockedException with the transport source in "
            "the reason. The fix was scoped to RateLimitError only."
        )

    def test_blocked_exception_still_passes_through(self):
        """Regression guard: the existing ``except NullRunBlockedException``
        arm must keep working. Adding the new pass-through arm above
        it must not intercept the existing block-propagation path."""
        exc = NullRunBlockedException(workflow_id="wf-1", reason="denied by policy")
        rt = self._mock_runtime_raising(exc)
        with pytest.raises(NullRunBlockedException) as excinfo:
            _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
        assert excinfo.value is exc
        assert "denied by policy" in excinfo.value.reason

    def test_execution_not_found_still_passes_through(self):
        """Regression guard: the prior DEF-NR-EX01-REWRAP-LOSS fix
        (pass-through for ``except NullRunExecutionNotFoundError``)
        must keep working. The new ``except RateLimitError:`` arm
        inserted between this arm and ``except NullRunBlockedException``
        must not break the MRO ordering for NullRunBackendError
        subclasses (NullRunExecutionNotFoundError is one)."""
        exc = NullRunBackendError(
            "5xx blip",
            endpoint="/api/v1/execute",
            status_code=503,
        )
        # Note: this isn't a NullRunExecutionNotFoundError — it's the
        # parent NullRunBackendError (5xx). Verify the parent still
        # rewraps via the NullRunTransportError generic path with
        # GATEWAY_ERROR source -> NR-B002, NOT pass-through.
        rt = self._mock_runtime_raising(exc)
        # NullRunBackendError IS a NullRunTransportError — pre-fix
        # it would have hit the rewrap arm (since the specific
        # NullRunExecutionNotFoundError arm only matched the leaf).
        # Post-fix it should STILL hit the rewrap (since this test
        # exercises the parent, not the typed leaf). The
        # NullRunExecutionNotFoundError-specific pass-through is
        # covered separately by the existing NR-EX01 test file.
        with pytest.raises(NullRunBlockedException) as excinfo:
            _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
        assert not isinstance(excinfo.value, RateLimitError)
        assert excinfo.value.error_code == "NR-B002", (
            "DEF-NR-R001-REWRAP-LOSS regression: NullRunBackendError "
            "with GATEWAY_ERROR source must still rewrap as "
            "NullRunBlockedException(NR-B002). The new "
            "RateLimitError pass-through arm must not widen the "
            "pass-through to all NullRunTransportError subclasses."
        )
