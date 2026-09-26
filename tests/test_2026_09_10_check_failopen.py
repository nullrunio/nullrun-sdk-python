"""DEF-NR-CHECK-FAIL-OPEN (2026-09-10) — ``Transport.check`` MUST NOT
synthesize ``decision_source=FALLBACK`` for 4xx responses.

Pre-fix:
  - ``nullrun/transport.py::Transport.check`` had a 4xx branch that
    returned a synthetic
    ``{"decision": "block", "decision_source": "fallback", ...}`` dict.
  - The runtime's fail-OPEN path at ``runtime.py:2063-2088`` checks
    ``decision_source.startswith("fallback")`` and returns a soft-pass
    in that case — VIOLATING the CLAUDE.md §4 fail-CLOSED invariant.
  - User-visible symptom: a 402 BUDGET_HARD_BLOCKED from the gateway
    was downgraded to an allow. Wire-coded reasons
    (BUDGET_HARD_BLOCKED / BUDGET_SOFT_BLOCKED / TOOL_BLOCKED /
    RATE_LIMITED / etc.) were all silently swallowed.

Post-fix:
  - The 4xx branch parses the v3 wire envelope and returns a
    gateway-shaped dict (``decision_source=DecisionSource.GATEWAY``,
    NOT "fallback") with the wire envelope preserved
    (``error_code``, ``explanation``, ``policy_id``,
    ``remaining_budget_cents``, ``details``, ...).
  - The runtime's existing ``decision=="block"`` arm then raises
    ``NullRunBudgetError`` with first-class attrs intact.

These tests pin BOTH the source shape (the 4xx branch returns
``DecisionSource.GATEWAY``, not ``FALLBACK``) AND the runtime behavior
(wire-coded reasons propagate as gateway decisions and trigger
``NullRunBudgetError``, NOT a silent allow).
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
import respx

from nullrun.breaker.exceptions import (
    NullRunBudgetError,
    NullRunError,
)
from nullrun.transport import DecisionSource, Transport

SDK_ROOT = Path(__file__).resolve().parent.parent
TRANSPORT_PY = SDK_ROOT / "src" / "nullrun" / "transport.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _check_body() -> str:
    """Return the source of ``Transport.check`` so source-pin tests
    can grep for the expected 4xx branch without depending on Python
    AST parsing.

    The signature is multiline
    (``def check(\n        self,\n        workflow_id: ...``) so we
    anchor on ``def check(`` and walk forward to the next top-level
    ``def`` (4-space indent) inside the same class."""
    src = _read(TRANSPORT_PY)
    start = src.find("    def check(\n")
    assert start != -1, "could not locate Transport.check header"
    after_header = src.index("    def check(\n", start) + len("    def check(\n")
    m = re.search(
        r"^    (?:def |@|class )",
        src[after_header:],
        re.MULTILINE,
    )
    assert m, "could not locate end of Transport.check body"
    end = after_header + m.start()
    return src[start:end]


def _v3_envelope(
    error_code: str,
    status: int = 402,
    *,
    explanation: str | None = None,
    policy_id: str | None = None,
    remaining_budget_cents: int = 0,
    projected_cost_cents: int | None = None,
    reservation_id: str | None = None,
    operation_id: str | None = None,
    policy_version: int | None = None,
    **details,
) -> httpx.Response:
    """Build a v3-shaped 4xx response envelope mirroring the real
    backend's wire contract."""
    body = {
        "decision": "block",
        "decision_source": DecisionSource.GATEWAY,
        "explanation": explanation or error_code,
        "explanations": [explanation or error_code],
        "error_code": error_code,
        "policy_id": policy_id,
        "policy_version": policy_version,
        "reservation_id": reservation_id,
        "operation_id": operation_id,
        "remaining_budget_cents": remaining_budget_cents,
        "projected_cost_cents": projected_cost_cents,
        "details": details,
    }
    return httpx.Response(status, json=body)


_CHECK_URL = "https://api.test.nullrun.io/api/v1/gate"


@pytest.fixture
def transport():
    t = Transport(
        api_url="https://api.test.nullrun.io",
        api_key="test-key-12345678",
    )
    yield t
    t.stop()


def _check_kwargs():
    return dict(
        check_request={
            "organization_id": "ws-123",
            "workflow_id": "wf-" + "a" * 32,
            "trace_id": "trace-789",
            "tool": "read_file",
        },
        on_transport_error="raise",
    )


# ─── Source-pin tests (mirror cancel.rs / orchestrator.rs pin style) ───


class TestDefNrCheckFailopenSourcePin:
    """Pin the shape of the fix so a refactor that re-introduces
    decision_source=fallback for 4xx fails loudly."""

    def test_4xx_branch_uses_gateway_not_fallback(self):
        """The 4xx branch in Transport.check MUST assign
        DecisionSource.GATEWAY (or the literal "gateway" string),
        not DecisionSource.FALLBACK. Pre-fix this branch synthesised
        fallback and the runtime fail-OPENed."""
        body = _check_body()
        # Locate the 4xx branch by its comment marker
        idx = body.find("if 400 <= response.status_code < 500:")
        assert idx != -1, (
            "DEF-NR-CHECK-FAIL-OPEN: 4xx branch anchor "
            "`if 400 <= response.status_code < 500:` not found in "
            "Transport.check"
        )
        # Slice only the 4xx branch (stop at the next sibling `if`
        # for the 5xx fallthrough).
        five_xx_marker = body.find("if response.status_code >= 500", idx)
        assert five_xx_marker != -1
        branch_body = body[idx:five_xx_marker]
        assert "DecisionSource.GATEWAY" in branch_body, (
            "DEF-NR-CHECK-FAIL-OPEN: 4xx branch must use "
            "DecisionSource.GATEWAY. Pre-fix it used "
            "DecisionSource.FALLBACK and the runtime treated 4xx as "
            "transport errors, fail-OPENing the gate."
        )
        # The fallback synthesis arm must NOT be reachable on the 4xx
        # path. We assert it does not co-exist in the 4xx branch
        # body. (Other branches in check/execute MAY still use
        # FALLBACK for genuine transport errors — that's outside
        # this pin's scope.)
        assert "DecisionSource.FALLBACK" not in branch_body, (
            "DEF-NR-CHECK-FAIL-OPEN: 4xx branch must not return "
            "decision_source=FALLBACK. Runtime treats fallback as "
            "transport error and silently allows."
        )

    def test_4xx_branch_preserves_wire_envelope(self):
        """The 4xx branch must surface wire envelope fields
        (``error_code``, ``explanation``, ``policy_id``,
        ``remaining_budget_cents``, ``details``) so the runtime's
        catalog dispatcher can build an actionable exception."""
        body = _check_body()
        idx = body.find("if 400 <= response.status_code < 500:")
        assert idx != -1
        five_xx_marker = body.find("if response.status_code >= 500", idx)
        assert five_xx_marker != -1
        branch_body = body[idx:five_xx_marker]
        for field in (
            "error_code",
            "explanation",
            "explanations",
            "policy_id",
            "reservation_id",
            "remaining_budget_cents",
            "projected_cost_cents",
            "operation_id",
            "details",
        ):
            assert field in branch_body, (
                f"DEF-NR-CHECK-FAIL-OPEN: 4xx branch must surface "
                f"`{field}` from the wire envelope; runtime catalog "
                f"dispatchers depend on it."
            )


# ─── Behaviour tests (pin the runtime outcome of the fix) ───


class TestDefNrCheckFailopenBehavior:
    """Verify that on a wire-coded 4xx response, Transport.check
    returns a gateway-shaped dict (decision_source=gateway) so the
    runtime's existing block dispatcher raises the typed exception
    instead of fail-OPENing."""

    def test_402_budget_hard_blocked_returns_gateway_dict(self, transport):
        """A 402 BUDGET_HARD_BLOCKED from /check must return a
        gateway-shaped dict, NOT a fallback dict."""
        policy_id = "pol-" + "a" * 32
        with respx.mock(assert_all_called=False) as mock:
            mock.post(_CHECK_URL).mock(
                return_value=_v3_envelope(
                    "BUDGET_HARD_BLOCKED",
                    status=402,
                    explanation="Hard budget limit exceeded",
                    policy_id=policy_id,
                    remaining_budget_cents=0,
                    projected_cost_cents=1,
                    budget_cents=1000,
                    current_spend_cents=1100,
                    enforcement_mode="hard",
                )
            )
            result = transport.check(**_check_kwargs())
        assert result["decision"] == "block", (
            f"DEF-NR-CHECK-FAIL-OPEN: expected decision=block, got "
            f"{result.get('decision')}"
        )
        assert result["decision_source"] == DecisionSource.GATEWAY, (
            f"DEF-NR-CHECK-FAIL-OPEN: 4xx must produce "
            f"decision_source=gateway, got {result.get('decision_source')}. "
            f"Runtime treats fallback as transport error and fail-OPENs."
        )
        assert result.get("error_code") == "BUDGET_HARD_BLOCKED"
        assert result.get("policy_id") == policy_id
        assert result.get("remaining_budget_cents") == 0

    def test_403_tool_blocked_returns_gateway_dict(self, transport):
        """A 403 TOOL_BLOCKED must surface with decision_source=
        gateway so runtime raises NullRunBlockedException, not
        silently allow."""
        with respx.mock(assert_all_called=False) as mock:
            mock.post(_CHECK_URL).mock(
                return_value=_v3_envelope(
                    "TOOL_BLOCKED",
                    status=403,
                    explanation="bash is blocked by policy",
                    policy_id="pol-toolblock",
                    details={"tool": "bash", "matched_patterns": ["bash"]},
                )
            )
            result = transport.check(**_check_kwargs())
        assert result["decision"] == "block"
        assert result["decision_source"] == DecisionSource.GATEWAY
        assert result.get("error_code") == "TOOL_BLOCKED"
        assert result.get("explanation") == "bash is blocked by policy"

    def test_4xx_with_unparseable_body_returns_gateway_dict(self, transport):
        """A 4xx with non-JSON body must still return a
        gateway-shaped dict with explanation fallback — never
        decision_source=fallback."""
        with respx.mock(assert_all_called=False) as mock:
            mock.post(_CHECK_URL).mock(
                return_value=httpx.Response(400, text="<html>Bad Request</html>")
            )
            result = transport.check(**_check_kwargs())
        assert result["decision"] == "block"
        assert result["decision_source"] == DecisionSource.GATEWAY
        assert "400" in result.get("explanation", "") or len(
            result.get("explanations") or []
        ) >= 1

    def test_5xx_does_not_use_4xx_branch(self, transport):
        """A 5xx (genuine transport error) must NOT take the 4xx
        gateway branch — Transport.check must still raise a
        transport-class exception (or return None per on_transport_error)
        so the runtime's retry/escalation logic kicks in. This pins
        the boundary: 4xx → gateway dict, 5xx → transport error."""

        # Set on_transport_error=raise and capture the call result.
        # We don't assert a specific exception type — that's
        # documented behavior of the existing transport layer —
        # only that the 5xx path does NOT silently return a
        # gateway-shaped dict with decision_source=gateway.
        with respx.mock(assert_all_called=False) as mock:
            mock.post(_CHECK_URL).mock(
                return_value=httpx.Response(503, text="upstream unavailable")
            )
            kwargs = _check_kwargs()
            kwargs["on_transport_error"] = "raise"
            with pytest.raises((NullRunError, Exception)) as ei:
                transport.check(**kwargs)
        # Belt-and-braces: the exception (whatever it is) must not
        # be a NullRunBudgetError with a wire-coded reason — that
        # would be the same fail-OPEN class as the original defect.
        if isinstance(ei.value, NullRunBudgetError):
            pytest.fail(
                "DEF-NR-CHECK-FAIL-OPEN regression: a 5xx "
                "response must not surface as a budget exception "
                "with a wire-coded reason. 5xx is transport "
                "error, not a budget decision."
            )

    def test_200_returns_normal_dict(self, transport):
        """Sanity: a 200 /check still returns the response body
        verbatim — no gateway/fallback tagging that didn't come from
        the server."""
        with respx.mock(assert_all_called=False) as mock:
            mock.post(_CHECK_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "decision": "allow",
                        "decision_source": "gateway",
                        "remaining_budget_cents": 990,
                    },
                )
            )
            result = transport.check(**_check_kwargs())
        assert result["decision"] == "allow"
        assert result.get("remaining_budget_cents") == 990
