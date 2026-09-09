"""DEFS-SDKEXEC-GATE-FIRST (2026-09-08) — /execute must reuse /gate's execution_id.

Pre-fix (per audit 2026-09-08):
  - `runtime.execute()` minted a fresh `uuid7_str()` for the wire body.
  - Backend `/api/v1/execute` (`backend/src/proxy/http/gate/execute.rs:46-208`,
    DEF-SDKK-022-EXEC-BYPASS, 2026-09-04, RUN_ID=20260904T1500) requires the
    request's `execution_id` to have a live `execution:{id}` ownership
    binding in Redis (HGET ORG_FIELD). Without a prior /gate that registered
    the binding, /execute returned 404 EXECUTION_NOT_FOUND and the SDK
    translated the 404 into a synthetic block ("Gateway returned 404").
  - User-visible symptom: every `@protect @sensitive` call from
    `langgraph_openai_approval_demo.py` (and similar flows) returned
    `Workflow __nullrun_unknown__ blocked: Gateway returned 404 (action=block,
    tool=<tool>, status_code=None, details=<redacted>)`.

Post-fix:
  - `runtime.execute()` reads `_server_minted_execution_id_var` (set by
    `_capture_server_minted_execution_id` from the /gate response's
    `reservation_id` field) and reuses it. Only mint a fresh uuid7 when
    the contextvar is empty (direct callers without a prior /gate).
  - `_enforce_sensitive_tool` displays the API key's bound workflow
    (resolved via `runtime._resolve_workflow_id`) instead of the literal
    `__nullrun_unknown__` sentinel when the user did not open an explicit
    `with workflow(...)` block. The wire still carries the same workflow
    (server-side binding); only the displayed label changes.

These tests pin the post-fix shape so a future refactor that re-introduces
a fresh-mint in `execute()` (or restores the sentinel-first display)
fails the test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from nullrun.context import (
    clear_server_minted_execution_id,
    set_server_minted_execution_id,
)

SDK_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_PY = SDK_ROOT / "src" / "nullrun" / "runtime.py"
DECORATORS_PY = SDK_ROOT / "src" / "nullrun" / "decorators.py"
TRANSPORT_PY = SDK_ROOT / "src" / "nullrun" / "transport.py"
LANGGRAPH_INSTR_PY = SDK_ROOT / "src" / "nullrun" / "instrumentation" / "langgraph.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _reset_server_minted():
    """Reset the contextvar before AND after each test so leakage
    between tests doesn't masquerade as a hoist pass."""
    clear_server_minted_execution_id()
    yield
    clear_server_minted_execution_id()


class TestExecuteReusesGateExecutionId:
    """Pin `runtime.execute()` so a future refactor that re-mints
    a fresh `uuid7_str()` regardless of /gate context fails the test."""

    def _execute_body(self) -> str:
        runtime = _read(RUNTIME_PY)
        # Match the second `def execute(` (the public enforcement
        # entry point), not `runtime._execute` or `Transport.execute`.
        m = re.search(
            r"    def execute\(\s*self,\s*tool_name: str,.*?\)\s*->\s*"
            r"dict\[str, Any\]:.*?(?=\n    def |\nclass |\Z)",
            runtime,
            re.DOTALL,
        )
        assert m, "could not locate runtime.execute method body"
        return m.group(0)

    def test_execute_reads_server_minted_contextvar(self):
        body = self._execute_body()
        assert "get_server_minted_execution_id()" in body, (
            "DEFS-SDKEXEC-GATE-FIRST: runtime.execute() must read the "
            "server-minted execution_id from the contextvar (set by "
            "check_workflow_budget's /gate round-trip) before minting "
            "a fresh uuid7. Pre-fix the body unconditionally minted "
            "uuid7_str(), so /execute's execution_id never matched "
            "the binding /gate registered and the backend returned "
            "404 EXECUTION_NOT_FOUND."
        )

    def test_execute_reuses_captured_id_when_present(self):
        body = self._execute_body()
        # The hoist pattern: read contextvar, fall back to uuid7_str()
        # only when the contextvar is None.
        assert "prior_execution_id = get_server_minted_execution_id()" in body, (
            "DEFS-SDKEXEC-GATE-FIRST: runtime.execute() must alias the "
            "contextvar read into a local so the same value flows into "
            "the wire body."
        )
        assert "if prior_execution_id is not None:" in body, (
            "DEFS-SDKEXEC-GATE-FIRST: when the contextvar is populated, "
            "execute() must reuse it directly — no fresh uuid7 mint."
        )
        assert "execution_id = prior_execution_id" in body, (
            "DEFS-SDKEXEC-GATE-FIRST: the reused execution_id must be "
            "threaded into the wire body under the `execution_id` key."
        )

    def test_execute_falls_back_to_uuid7_only_when_contextvar_empty(self):
        body = self._execute_body()
        # Locate the fallback block — must live INSIDE an `if ... is None:` arm.
        fallback_block = re.search(
            r"if prior_execution_id is not None:\s*\n\s*execution_id = "
            r"prior_execution_id\s*\n\s*else:\s*\n\s*execution_id = "
            r"uuid7_str\(\)",
            body,
        )
        assert fallback_block, (
            "DEFS-SDKEXEC-GATE-FIRST: the uuid7_str() mint must live "
            "INSIDE the `else:` arm of the `if prior_execution_id is "
            "not None:` check. Pre-fix an unconditional "
            "`execution_id = uuid7_str()` line at this site minted "
            "every time, breaking the /gate ↔ /execute binding."
        )
        body_without_fallback = body.replace(fallback_block.group(0), "")
        # Defensive: the wire body MUST consume the resolved
        # `execution_id` (the one with the prior_id fallback applied).
        assert '"execution_id": execution_id' in body, (
            "DEFS-SDKEXEC-GATE-FIRST: the wire body must consume the "
            "resolved `execution_id` variable (not a freshly-minted "
            "uuid7 inline)."
        )
        assert 'execution_id": uuid7_str()' not in body_without_fallback, (
            "DEFS-SDKEXEC-GATE-FIRST: a top-level `execution_id = "
            "uuid7_str()` (outside the fallback arm) must not survive. "
            "A pre-fix leftover would silently bypass the /gate reuse."
        )

    def test_execute_carries_comment_explaining_drift(self):
        body = self._execute_body()
        # The fix introduced a long comment naming DEF-SDKK-022 +
        # DEFS-SDKEXEC-GATE-FIRST. Pin so a future maintainer who
        # deletes the comment is forced to read the code's history.
        assert "DEFS-SDKEXEC-GATE-FIRST" in body, (
            "DEFS-SDKEXEC-GATE-FIRST: the explainer comment block must "
            "name the fix tag so future readers can grep for it."
        )
        assert "DEF-SDKK-022-EXEC-BYPASS" in body, (
            "DEFS-SDKEXEC-GATE-FIRST: the explainer must reference the "
            "backend fix (DEF-SDKK-022-EXEC-BYPASS) that introduced the "
            "/execute existence check, so readers see the round-trip "
            "contract without searching."
        )


class TestDecoratorWorkflowLabelUsesRuntimeBinding:
    """Pin `_enforce_sensitive_tool` so the displayed workflow_id
    label shows the API key's bound workflow when no `with workflow(...)`
    block is active (instead of the literal `__nullrun_unknown__` sentinel)."""

    def _enforce_body(self) -> str:
        decorators = _read(DECORATORS_PY)
        m = re.search(
            r"def _enforce_sensitive_tool\(.*?\).*?(?=\ndef |\nclass |\Z)",
            decorators,
            re.DOTALL,
        )
        assert m, "could not locate _enforce_sensitive_tool method body"
        return m.group(0)

    def test_enforce_resolves_via_runtime_bound_workflow(self):
        body = self._enforce_body()
        # Two sites in the function (extract failure path + main path).
        occurrences = body.count(
            "runtime._resolve_workflow_id(get_workflow_id()) or UNKNOWN_WORKFLOW_ID"
        )
        assert occurrences >= 2, (
            f"DEFS-SDKEXEC-WORKFLOW-LABEL: _enforce_sensitive_tool must "
            f"prefer the runtime's bound workflow via "
            f"runtime._resolve_workflow_id(...) at both display sites "
            f"(extract failure + main path). Found {occurrences} "
            f"occurrences; expected >= 2."
        )

    def test_enforce_does_not_use_contextvar_only_fallback(self):
        body = self._enforce_body()
        # Pre-fix: `workflow_id = get_workflow_id() or UNKNOWN_WORKFLOW_ID`
        # (contextvar-only). Post-fix: that literal pattern must not
        # survive at the top-level assignment site.
        #
        # We allow the literal only as a substring INSIDE the longer
        # `runtime._resolve_workflow_id(...)` call (which is what we
        # want). Strip those out first, then check the residue.
        resolved_call = "runtime._resolve_workflow_id(get_workflow_id()) or UNKNOWN_WORKFLOW_ID"
        body_without_resolved = body.replace(resolved_call, "")
        assert "workflow_id = get_workflow_id() or UNKNOWN_WORKFLOW_ID" not in (
            body_without_resolved
        ), (
            "DEFS-SDKEXEC-WORKFLOW-LABEL: pre-fix contextvar-only "
            "fallback `workflow_id = get_workflow_id() or "
            "UNKNOWN_WORKFLOW_ID` must be replaced by the runtime-aware "
            "resolver everywhere. The pre-fix pattern displayed "
            "`__nullrun_unknown__` for every API-key-bound key."
        )


class TestTransportCommentReflectsPostFixContract:
    """Pin the `Transport.execute` docstring so the legacy
    pre-2026-09-04 contract (`/execute MUST be called rather than
    /gate`) doesn't drift back into the source."""

    def test_transport_execute_docstring_references_post_fix_contract(self):
        transport = _read(TRANSPORT_PY)
        m = re.search(
            r"def execute\(\s*self,.*?\)\s*->\s*dict\[str, Any\]:.*?(?=\n    def |\nclass |\Z)",
            transport,
            re.DOTALL,
        )
        assert m, "could not locate Transport.execute method body"
        body = m.group(0)
        assert "DEFS-SDKEXEC-GATE-FIRST" in body, (
            "transport.py: Transport.execute docstring must name the "
            "post-fix tag so the contract is grep-able."
        )
        assert "DEF-SDKK-022-EXEC-BYPASS" in body, (
            "transport.py: Transport.execute docstring must reference "
            "the backend fix that introduced the existence check."
        )
        # The legacy misleading claim must be gone (or explicitly
        # marked as pre-fix).
        assert (
            "MUST call /api/v1/execute (which checks the ``execute`` "
            "scope on the API key) rather than /api/v1/gate"
        ) not in body, (
            "transport.py: pre-fix misleading claim that /execute MUST "
            "be called rather than /gate must be removed — that contract "
            "was the legacy pre-2026-09-04 shape and was the root "
            "cause of the 404 EXECUTION_NOT_FOUND drift."
        )


class TestLanggraphCallbackPairsLlmSpanWithReservation:
    """Pin `NullRunCallback.on_llm_start` so the LLM span /track
    pairing path stays alive (check_workflow_budget is fire-and-forget
    but the call site must survive)."""

    def test_on_llm_start_calls_check_workflow_budget(self):
        instr = _read(LANGGRAPH_INSTR_PY)
        m = re.search(
            r"def on_llm_start\(self,.*?\)\s*->\s*None:.*?(?=\n    def |\nclass |\Z)",
            instr,
            re.DOTALL,
        )
        assert m, "could not locate NullRunCallback.on_llm_start"
        body = m.group(0)
        assert "self.runtime.check_workflow_budget()" in body, (
            "DEFS-SDKEXEC-LLM-RESERVATION: on_llm_start must call "
            "runtime.check_workflow_budget() to pair the LLM span "
            "with a server-minted reservation_id. Without this the "
            "matching on_llm_end llm_call cost event is silently "
            "dropped by runtime._route_track (no reservation_id in "
            "scope)."
        )
        assert "DEFS-SDKEXEC-LLM-RESERVATION" in body, (
            "DEFS-SDKEXEC-LLM-RESERVATION: the explainer comment block "
            "must name the fix tag so future readers can grep."
        )
        # Defensive: the call must be guarded so a backend outage
        # never breaks the LangChain callback chain.
        assert "except BaseException" in body, (
            "DEFS-SDKEXEC-LLM-RESERVATION: the check_workflow_budget "
            "call must be wrapped in a never-raise guard so a "
            "WorkflowKilledInterrupt / WorkflowPausedException / "
            "transport error does not break the LangChain callback "
            "contract (callbacks must never raise)."
        )


class TestServerMintedExecutionIdContract:
    """Drive the contextvar to confirm the round-trip shape used by
    `runtime.execute()` works as advertised."""

    def test_set_then_get_round_trips(self):
        from nullrun.context import (
            get_server_minted_execution_id,
            reset_server_minted_execution_id,
        )

        sentinel = "01936f8e-1234-7abc-9def-0123456789ab"
        token = set_server_minted_execution_id(sentinel)
        try:
            assert get_server_minted_execution_id() == sentinel
        finally:
            reset_server_minted_execution_id(token)
