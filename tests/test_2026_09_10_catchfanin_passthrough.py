"""DEF-NR-TRANSPORT-CATCHFANIN-GAP (2026-09-10) — typed Decision and
Infrastructure subclasses parsed by ``_parse_v3_error_envelope`` must
propagate through ``Transport.execute``'s 4xx catch-fan-in, not be
silently swallowed into a synthetic ``{"decision": "block",
"decision_source": FALLBACK, ...}`` dict.

Pre-fix (audit 2026-09-10):
  - ``nullrun/transport.py::Transport.execute`` had a 4xx handler
    with five except arms that re-raised typed subclasses:
    NullRunApprovalReplayRejectedError,
    NullRunBlockedException, NullRunBackendError,
    NullRunAuthenticationError, NullRunTransportError.
  - Any other typed exception raised by
    ``_parse_v3_error_envelope`` (specifically:
    NullRunProtocolError, NullRunRateLimitRedisError,
    NullRunChainError, NullRunWorkflowInactiveError,
    NullRunConsumeOverbudgetError) fell through to
    ``except Exception: pass`` and was replaced with the synthetic
    block shape `{"decision": "block", "decision_source":
    FALLBACK, "explanation": f"Gateway returned {status_code}"}`.
  - User-visible symptom: a user calling /execute with a wire code
    of CHAIN_ORG_MISMATCH (NR-CH001) got back a synthetic block
    dict with no error_code, no chain_id, no diagnostic — the
    cookbook code that expected an except NullRunChainError path
    to fire never saw it; runtime.execute returned a dict instead
    of raising.

Post-fix:
  - Two umbrella pass-through arms added BEFORE the
    ``except Exception: pass`` fallback:
    ``except NullRunDecision: raise`` (covers
    NullRunChainError, NullRunWorkflowInactiveError,
    NullRunConsumeOverbudgetError, WorkflowPausedException) and
    ``except NullRunInfrastructureError: raise`` (covers
    NullRunProtocolError, NullRunRateLimitRedisError,
    NullRunConfigError; NullRunAuthError is also covered in
    addition to its existing NullRunAuthenticationError parent
    arm, which keeps the documented recovery contract intact
    even if a future refactor reorders the prior arms).

These tests pin BOTH the source shape (the two arms are present,
in the right order — after the specific Arms, before
``except Exception:`` — and match the catalog of typed exceptions
that were previously lost) AND the runtime behavior (each wire
code yields the matching typed subclass instance with first-class
attrs preserved, instead of a synthetic block dict).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest
import respx

from nullrun.breaker.exceptions import (
    NullRunAuthError,
    NullRunBackendError,
    NullRunChainError,
    NullRunConsumeOverbudgetError,
    NullRunProtocolError,
    NullRunRateLimitRedisError,
    NullRunWorkflowInactiveError,
)
from nullrun.transport import Transport

SDK_ROOT = Path(__file__).resolve().parent.parent
TRANSPORT_PY = SDK_ROOT / "src" / "nullrun" / "transport.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _execute_body() -> str:
    """Return the source of ``Transport.execute`` so source-pin tests
    can grep for the expected arms / ordering without depending on
    Python AST parsing.

    The signature is multiline
    (``def execute(\n        self,\n        organization_id: ...``)
    so we anchor on ``def execute(`` and walk forward to the next
    top-level ``def`` (4-space indent) inside the same class."""
    src = _read(TRANSPORT_PY)
    start = src.find("    def execute(\n")
    assert start != -1, "could not locate Transport.execute header"
    after_header = src.index("    def execute(\n", start) + len("    def execute(\n")
    # Walk forward from after_header; we're inside a class (4-space
    # indent). The next sibling ``def`` or ``@`` decorator at 4-space
    # indent terminates execute.
    m = re.search(
        r"^    (?:def |@|class )",
        src[after_header:],
        re.MULTILINE,
    )
    assert m, "could not locate end of Transport.execute body"
    end = after_header + m.start()
    return src[start:end]


def _v3_envelope(error_code: str, status: int = 400, **details) -> httpx.Response:
    """Build a v3-shaped 4xx response envelope that exercises the
    catch-fan-in.

    The canonical v3 envelope uses ``error_code`` (NOT ``error`` —
    that's the legacy slug shape, which has weaker details
    semantics and would silently drop our first-class attrs).
    Fields passed as ``**details`` go under ``details`` so the
    parser's per-class dispatchers can read them off the typed
    exception (`chain_id`, ``workflow_id``, ``reserved_cents``,
    etc.)."""
    body = {
        "error_code": error_code,
        "error_message": f"Backend says {error_code}",
        "details": details,
    }
    return httpx.Response(status, json=body)


# Wire endpoints we exercise — same shape as the real backend.
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
    """Standard kwargs for Transport.execute that match the
    contract (org_id, execution_id, tool, input, mode)."""
    return dict(
        organization_id="ws-123",
        execution_id="exec-" + "a" * 32,
        trace_id="trace-789",
        tool="my.tool",
        input_data={},
        on_transport_error="raise",
        fallback_mode="strict",
    )


# ─── Source-pin tests (mirror cancel.rs / orchestrator.rs pin style) ───


class TestDefNrCatchfaninSourcePin:
    """Pin the shape of the fix so a refactor that reorders / removes
    either umbrella arm fails loudly."""

    def _fallback_index(self, body: str) -> int:
        # Anchor on the next ``except Exception:`` arm — that's the
        # silent-swallow fallback that the new umbrella arms must
        # precede.
        idx = body.find(
            "except Exception:\n                    # Unrecognised envelope"
        )
        assert idx != -1, (
            "TRANSPORT test fixture broken: silent "
            "`except Exception: pass` fallback not found"
        )
        # Walk back to the `except` keyword.
        except_idx = body.rfind("except Exception", 0, idx + 1)
        assert except_idx != -1
        return except_idx

    def test_decision_umbrella_arm_present(self):
        body = _execute_body()
        assert "except NullRunDecision as exc:" in body, (
            "DEF-NR-TRANSPORT-CATCHFANIN-GAP: the pass-through arm "
            "for NullRunDecision must be present in "
            "Transport.execute. Pre-fix NullRunChainError / "
            "NullRunWorkflowInactiveError / "
            "NullRunConsumeOverbudgetError were silently "
            "swallowed into the synthetic-block shape."
        )

    def test_infrastructure_umbrella_arm_present(self):
        body = _execute_body()
        assert "except NullRunInfrastructureError as exc:" in body, (
            "DEF-NR-TRANSPORT-CATCHFANIN-GAP: the pass-through arm "
            "for NullRunInfrastructureError must be present in "
            "Transport.execute. Pre-fix NullRunProtocolError / "
            "NullRunRateLimitRedisError were silently swallowed."
        )

    def test_decision_arm_after_blocked_exception_arm(self):
        """``except NullRunDecision`` MUST come AFTER
        ``except NullRunBlockedException`` so the typed-block path
        (budget / tool / 6 approval exceptions) still wins on MRO
        specificity. Reorder: order Blocked first, then Decision."""
        body = _execute_body()
        blocked_idx = body.find("except NullRunBlockedException as exc:")
        decision_idx = body.find("except NullRunDecision as exc:")
        assert blocked_idx != -1, "NullRunBlockedException arm missing"
        assert decision_idx != -1, "NullRunDecision arm missing"
        assert blocked_idx < decision_idx, (
            "DEF-NR-TRANSPORT-CATCHFANIN-GAP: NullRunDecision "
            "umbrella must come AFTER NullRunBlockedException so "
            "the typed-block path stays MRO-specific."
        )

    def test_decision_arm_before_fallback(self):
        body = _execute_body()
        decision_idx = body.find("except NullRunDecision as exc:")
        fallback_idx = self._fallback_index(body)
        assert decision_idx != -1
        assert decision_idx < fallback_idx, (
            "DEF-NR-TRANSPORT-CATCHFANIN-GAP: NullRunDecision "
            "umbrella must come BEFORE `except Exception: pass` "
            "fallback."
        )

    def test_infrastructure_arm_after_backend_auth_transport(self):
        """``except NullRunInfrastructureError`` MUST come AFTER
        ``except NullRunBackendError`` / ``except
        NullRunAuthenticationError`` / ``except
        NullRunTransportError`` so wire-classified exceptions still
        match by MRO specificity. The umbrella arm catches what's
        left (Protocol / RateLimitRedis / Config), not everything
        InfrastructureError-shaped."""
        body = _execute_body()
        backend_idx = body.find("except NullRunBackendError as exc:")
        auth_idx = body.find("except NullRunAuthenticationError as exc:")
        transport_idx = body.find("except NullRunTransportError as exc:")
        infra_idx = body.find("except NullRunInfrastructureError as exc:")
        assert backend_idx != -1 and auth_idx != -1 and transport_idx != -1
        assert infra_idx != -1
        # The umbrella must come AFTER all three specific Arms so
        # they win by MRO. We don't constrain the relative order
        # between the three specific Arms themselves.
        assert (
            backend_idx < infra_idx
            and auth_idx < infra_idx
            and transport_idx < infra_idx
        ), (
            "DEF-NR-TRANSPORT-CATCHFANIN-GAP: "
            "NullRunInfrastructureError umbrella must come AFTER "
            "NullRunBackendError / NullRunAuthenticationError / "
            "NullRunTransportError arms."
        )

    def test_infrastructure_arm_before_fallback(self):
        body = _execute_body()
        infra_idx = body.find("except NullRunInfrastructureError as exc:")
        fallback_idx = self._fallback_index(body)
        assert infra_idx != -1
        assert infra_idx < fallback_idx, (
            "DEF-NR-TRANSPORT-CATCHFANIN-GAP: "
            "NullRunInfrastructureError umbrella must come BEFORE "
            "`except Exception: pass` fallback."
        )

    def test_decision_arm_only_raises(self):
        body = _execute_body()
        m = re.search(
            r"except NullRunDecision as exc:\s*\n(.*?)(?=\n                except |\Z)",
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
        # Must not synthesize a synthetic dict (that's the bug we're
        # fixing).
        assert "decision" not in executable.lower() or "metrics" in executable, (
            "DEF-NR-TRANSPORT-CATCHFANIN-GAP: NullRunDecision arm "
            "must only raise; the pre-fix behavior was a 'pass' "
            "followed by a synthetic dict return."
        )
        # Sanity: the arm should NOT swallow the exception.
        assert "pass" not in executable.split("\n")[0], (
            "DEF-NR-TRANSPORT-CATCHFANIN-GAP: NullRunDecision "
            "arm's first executable line must not be a `pass` "
            "(that was the pre-fix swallow)."
        )

    def test_infrastructure_arm_only_raises(self):
        body = _execute_body()
        m = re.search(
            r"except NullRunInfrastructureError as exc:\s*\n(.*?)(?=\n                except |\Z)",
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

    def test_imports_include_umbrella_classes(self):
        src = _read(TRANSPORT_PY)
        # The import block at ~line 2480 must include both
        # NullRunDecision and NullRunInfrastructureError;
        # otherwise NameError at runtime even though the except
        # arms are present.
        assert "NullRunDecision" in src
        assert "NullRunInfrastructureError" in src


# ─── Behavioral tests (mirror test_transport.py:1226 style) ─────────


class TestDefNrCatchfaninBehavior:
    """Pin the runtime behavior — a wire envelope carrying one of
    the previously-lost codes now propagates as a typed exception,
    not a synthetic block dict."""

    @respx.mock
    def test_chain_org_mismatch_propagates_as_chain_error(self, transport):
        """CHAIN_ORG_MISMATCH (NR-CH001) → ``_parse_v3_error_envelope``
        raises NullRunChainError. Catch-fan-in re-raises it; we see
        it, not a synthetic block."""
        respx.post(_EXECUTE_URL).mock(
            return_value=_v3_envelope(
                "CHAIN_ORG_MISMATCH",
                status=403,
                chain_id="01a08b57-f176-79ce-ad1b-60b0184d1625",
            )
        )
        with pytest.raises(NullRunChainError) as excinfo:
            transport.execute(**_execute_kwargs())
        assert excinfo.value.error_code == "NR-CH001", (
            "DEF-NR-TRANSPORT-CATCHFANIN-GAP: CHAIN_ORG_MISMATCH "
            "must raise NullRunChainError (NR-CH001), not be "
            "swallowed into a synthetic block dict."
        )
        assert (
            excinfo.value.chain_id
            == "01a08b57-f176-79ce-ad1b-60b0184d1625"
        ), (
            "DEF-NR-TRANSPORT-CATCHFANIN-GAP: NullRunChainError "
            ".chain_id must be preserved (was lost in the "
            "synthetic-block pre-fix)."
        )

    @respx.mock
    def test_workflow_inactive_propagates_as_workflow_inactive_error(
        self, transport
    ):
        """WORKFLOW_INACTIVE (NR-W004) → NullRunWorkflowInactiveError."""
        respx.post(_EXECUTE_URL).mock(
            return_value=_v3_envelope(
                "WORKFLOW_INACTIVE",
                status=403,
                workflow_id="wf-soft-deleted-789",
            )
        )
        with pytest.raises(NullRunWorkflowInactiveError) as excinfo:
            transport.execute(**_execute_kwargs())
        assert excinfo.value.error_code == "NR-W004"
        assert excinfo.value.workflow_id == "wf-soft-deleted-789", (
            "DEF-NR-TRANSPORT-CATCHFANIN-GAP: NullRunWorkflowInactiveError"
            ".workflow_id must be preserved."
        )

    @respx.mock
    def test_consume_overbudget_propagates_with_counter_attrs(
        self, transport
    ):
        """CONSUME_OVERBUDGET (NR-O001) → NullRunConsumeOverbudgetError."""
        respx.post(_EXECUTE_URL).mock(
            return_value=_v3_envelope(
                "CONSUME_OVERBUDGET",
                status=422,
                execution_id="exec-consume-overrun",
                reserved_cents=100,
                max_allowed_cents=101,
                actual_cost_cents=1000,
                epsilon_cents=1,
            )
        )
        with pytest.raises(NullRunConsumeOverbudgetError) as excinfo:
            transport.execute(**_execute_kwargs())
        assert excinfo.value.error_code == "NR-O001", (
            "DEF-NR-TRANSPORT-CATCHFANIN-GAP: CONSUME_OVERBUDGET "
            "must raise NullRunConsumeOverbudgetError (NR-O001), "
            "not be swallowed."
        )
        assert excinfo.value.reserved_cents == 100
        assert excinfo.value.max_allowed_cents == 101
        assert excinfo.value.actual_cost_cents == 1000
        assert excinfo.value.epsilon_cents == 1

    @respx.mock
    def test_protocol_too_old_propagates_as_protocol_error(self, transport):
        """PROTOCOL_TOO_OLD (NR-P001) → NullRunProtocolError."""
        respx.post(_EXECUTE_URL).mock(
            return_value=_v3_envelope(
                "PROTOCOL_TOO_OLD", status=400,
            )
        )
        with pytest.raises(NullRunProtocolError) as excinfo:
            transport.execute(**_execute_kwargs())
        assert excinfo.value.error_code == "NR-P001", (
            "DEF-NR-TRANSPORT-CATCHFANIN-GAP: PROTOCOL_TOO_OLD "
            "must raise NullRunProtocolError, not be swallowed. "
            "The catalog line for NR-P001 ('Upgrade the SDK to "
            "a version that supports protocol "
            "X-NULLRUN-PROTOCOL: 4') is unreachable without this "
            "fix — pre-fix the user saw 'Gateway returned 400'."
        )

    @respx.mock
    def test_rate_limit_redis_unavailable_propagates_typed(self, transport):
        """RATE_LIMIT_REDIS_UNAVAILABLE (NR-R002) → NullRunRateLimitRedisError.

        The parser dispatches on ``backend_code`` first — status
        is irrelevant for the catalog branch. Status 503 would
        normally hit the retry-on-5xx early-raise path in
        ``_retry_with_backoff`` (with ``on_transport_error='raise'``),
        so use 400 here so the response reaches the parser
        unmolested. The fix being tested is the catch-fan-in's
        ability to propagate the typed exception, not the retry
        layer."""
        respx.post(_EXECUTE_URL).mock(
            return_value=_v3_envelope(
                "RATE_LIMIT_REDIS_UNAVAILABLE",
                status=400,
            )
        )
        with pytest.raises(NullRunRateLimitRedisError) as excinfo:
            transport.execute(**_execute_kwargs())
        assert excinfo.value.error_code == "NR-R002", (
            "DEF-NR-TRANSPORT-CATCHFANIN-GAP: "
            "RATE_LIMIT_REDIS_UNAVAILABLE must raise "
            "NullRunRateLimitRedisError (NR-R002), not be "
            "swallowed. The NR-R002 catalog line ('Redis "
            "outage for aggregate rate limit, fail-CLOSED') is "
            "unreachable without this fix."
        )


# ─── Regression guards (typed classes still flow upstream) ────────────


class TestDefNrCatchfaninRegressionGuards:
    """Pre-existing fan-in arms must still work — these are the
    typed branches that this fix did NOT touch but must verify
    didn't move."""

    @respx.mock
    def test_blocked_exception_still_propagates(self, transport):
        """Budget / 6 approval typed exceptions
        (NullRunBlockedException subclasses) must still flow
        through the ``except NullRunBlockedException`` arm, NOT
        through the new ``except NullRunDecision`` umbrella.

        Use BUDGET_HARD_BLOCKED (which has explicit parser
        dispatch with ``workflow_id`` + ``reason`` — see
        ``_parse_v3_error_envelope`` line ~2726) — this maps to
        NullRunBudgetError and exercises the
        ``NullRunBlockedException`` arm.

        Note: TOOL_BLOCKED currently has a separate pre-existing
        parser bug (catalog fallback uses generic dispatcher
        which doesn't pass ``workflow_id`` / ``reason`` to
        NullRunBlockedException constructor — see parser
        TypeError at line ~2782). That bug is OUT OF SCOPE for
        DEF-NR-TRANSPORT-CATCHFANIN-GAP, which is specifically
        about the catch-fan-in rewrap-loss, not parser
        correctness. A future fix can address the parser."""
        from nullrun.breaker.exceptions import NullRunBudgetError

        respx.post(_EXECUTE_URL).mock(
            return_value=_v3_envelope(
                "BUDGET_HARD_BLOCKED",
                status=402,
                workflow_id="wf-budget-1",
                current_spend_cents=5000,
                budget_cents=1000,
            )
        )
        with pytest.raises(NullRunBudgetError) as excinfo:
            transport.execute(**_execute_kwargs())
        assert excinfo.value.error_code == "NR-B004", (
            "DEF-NR-TRANSPORT-CATCHFANIN-GAP regression: "
            "BUDGET_HARD_BLOCKED must still propagate as "
            "NullRunBudgetError through the NullRunBlockedException "
            "arm. The new NullRunDecision umbrella arm must not "
            "have intercepted this code path."
        )
        assert excinfo.value.workflow_id == "wf-budget-1"

    @respx.mock
    def test_auth_error_still_propagates(self, transport):
        """API_KEY_REVOKED (NR-A003) → NullRunAuthError → must
        propagate via the existing NullRunAuthenticationError arm,
        not be silently swallowed."""
        respx.post(_EXECUTE_URL).mock(
            return_value=_v3_envelope(
                "API_KEY_REVOKED", status=401,
            )
        )
        with pytest.raises(NullRunAuthError) as excinfo:
            transport.execute(**_execute_kwargs())
        assert excinfo.value.error_code == "NR-A003", (
            "DEF-NR-TRANSPORT-CATCHFANIN-GAP regression: "
            "API_KEY_REVOKED must still propagate as NullRunAuthError, "
            "reaching the cookbook branch on wire_code."
        )

    @respx.mock
    def test_unknown_envelope_does_not_match_umbrella_classes(self, transport):
        """The catch-all ``except Exception: pass`` in the inner
        try/except is INTENTIONAL for unrecognized envelopes
        (plaintext body, legacy slug, malformed JSON). It must NOT
        match the new umbrella arms for an unknown cause — if it
        did, the legacy fallback contract would break (cookbook
        code expecting a synthetic dict would see a typed
        exception instead).

        We don't pin the exact outcome (synthetic dict OR
        NullRunBackendError fallback) — both are acceptable
        post-fix — but we DO pin that it is NOT classified as one
        of the umbrella-arm classes (Protocol / RateLimitRedis /
        Chain / WorkflowInactive / ConsumeOverbudget).
        """
        respx.post(_EXECUTE_URL).mock(
            return_value=httpx.Response(400, text="plaintext body")
        )
        kwargs = _execute_kwargs()
        kwargs["fallback_mode"] = "strict"
        try:
            result = transport.execute(**kwargs)
            assert isinstance(result, dict), (
                "DEF-NR-TRANSPORT-CATCHFANIN-GAP regression: unknown "
                "envelopes must still return a dict via fallback. "
                "Catch-all `except Exception: pass` is intentional."
            )
            assert result["decision"] == "block"
            assert result["decision_source"] == "fallback"
        except NullRunBackendError as exc:
            # NullRunBackendError is the catalog-fallback for
            # unknown envelopes per _parse_v3_error_envelope.
            # Pre-fix the catch-all swallowed this and returned
            # a dict. Post-fix the NullRunBackendError arm
            # re-raises. NullRunBackendError is NOT one of the
            # umbrella-arm classes (it's NullRunBackendError,
            # caught earlier in the chain). Confirm we're not
            # accidentally routing through Decision / Infra
            # umbrella.
            assert not isinstance(exc, NullRunProtocolError)
            assert not isinstance(exc, NullRunRateLimitRedisError)
            assert not isinstance(exc, NullRunChainError)
            assert not isinstance(exc, NullRunWorkflowInactiveError)
            assert not isinstance(exc, NullRunConsumeOverbudgetError)
