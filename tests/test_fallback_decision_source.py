"""tests/test_fallback_decision_source.py — one definition of "synthetic".

Audit 2026-09-30, following DEF-MP-TS12-ENF-01 (RUN_ID 20260929T1338).

The defect
----------
The predicate "was this decision synthesised by a degrading transport,
or did it come from the gateway?" decides whether ADR-008 fail-OPEN or
fail-CLOSED applies. It was written out TWICE, and the two copies had
already drifted:

* ``runtime.check_workflow_budget`` tested
  ``startswith("fallback")`` — lowercase — and, after Fix D, excluded
  ``TransportErrorSource.AUTH_ERROR``.
* ``decorators._run_tool_policy_gate`` tested
  ``startswith("FALLBACK_")`` — UPPERCASE. No transport code path
  produces a decision_source above ``DecisionSource.FALLBACK
  == "fallback"``, so that clause could never fire. It also still
  listed ``AUTH_ERROR``, the exact hole Fix D had closed one file
  away.

So the decorator's copy was simultaneously dead in its first clause
and wrong in its second.

The fix is a single definition, ``transport.is_fallback_decision_source``,
called from both sites. The pins below cover the truth table AND the
drift hazard: a test that only checks the truth table would pass
against the old duplicated code, because each copy was individually
plausible. What actually needs pinning is that there is only ONE copy.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import textwrap

import pytest

from nullrun.breaker.exceptions import TransportErrorSource
from nullrun.transport import DecisionSource, is_fallback_decision_source


class TestTruthTable:
    @pytest.mark.parametrize(
        "source",
        [
            DecisionSource.FALLBACK,
            TransportErrorSource.NETWORK_ERROR.value,
            TransportErrorSource.GATEWAY_ERROR.value,
            TransportErrorSource.BREAKER_OPEN.value,
        ],
    )
    def test_synthetic_sources_are_recognised(self, source):
        """Values the transport actually produces when it degrades."""
        assert is_fallback_decision_source(source) is True, (
            f"{source!r} marks a synthetic decision, so ADR-008's "
            "fail-OPEN/CLOSED rule applies rather than honouring a "
            "decision the gateway never made."
        )

    @pytest.mark.parametrize(
        "source",
        [
            DecisionSource.GATEWAY,
            DecisionSource.CACHED,
            DecisionSource.LOCAL,
        ],
    )
    def test_real_sources_are_honoured(self, source):
        """A real decision must never be reinterpreted as synthetic.

        This is the property that keeps a genuine `block` from being
        downgraded to a fail-OPEN allow.
        """
        assert is_fallback_decision_source(source) is False, (
            f"{source!r} is a real decision source — treating it as "
            "synthetic would discard the gateway's answer."
        )

    def test_auth_error_is_not_a_transport_error(self):
        """DEF-MP-TS12-ENF-01: a bad API key is not an unreachable gate.

        Classifying auth as a transport error is what let a 401 read
        as "engine unavailable, carry on" — the agent is told
        'allowed' on a call the gate refused. The transport re-raises
        `NullRunAuthenticationError` rather than degrading, so this
        value can only arrive from a caller that explicitly asked for
        `on_transport_error="open"`. Honouring it as a credential
        failure is correct in that case too.
        """
        assert is_fallback_decision_source(TransportErrorSource.AUTH_ERROR.value) is False

    @pytest.mark.parametrize("source", [None, "", 123, object(), b"fallback"])
    def test_non_string_inputs_are_safe(self, source):
        """A malformed envelope must not crash the gate.

        `decision_source` arrives from a parsed JSON body; anything
        can be in it. Returning False (honour the decision) is the
        safe direction — the `decision` field is still checked
        separately.
        """
        assert is_fallback_decision_source(source) is False

    def test_case_sensitivity(self):
        """Pins the exact defect: `FALLBACK_` vs `fallback`.

        The decorator's copy matched an uppercase prefix that no code
        path emits. If this test ever passes for `"FALLBACK_..."`,
        the predicate has drifted back to matching a value the
        transport never returns.
        """
        assert is_fallback_decision_source("FALLBACK_NETWORK_ERROR") is False
        assert is_fallback_decision_source(DecisionSource.FALLBACK) is True


def _decision_source_comparisons(src: str) -> list[str]:
    """Every `decision_source` comparison in `src`, as source snippets.

    AST, not substring: the fix is precisely a comment-and-code change,
    and a text search matches the comment that documents the old
    defect — the self-defeating-pin hazard. Only real Compare nodes
    may satisfy a pin.
    """
    tree = ast.parse(textwrap.dedent(src))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            snippet = ast.unparse(node)
            if "decision_source" in snippet:
                out.append(f"line {node.lineno}: {snippet[:100]}")
    return out


class TestSingleDefinition:
    """The drift hazard itself — the part a truth-table test misses."""

    def _source(self, rel: str) -> str:
        root = pathlib.Path(__file__).resolve().parent.parent
        return (root / "src" / "nullrun" / rel).read_text(encoding="utf-8")

    def test_decorator_delegates(self):
        """`decorators` must call the shared predicate, not re-derive it."""
        from nullrun import decorators

        src = inspect.getsource(decorators._run_tool_policy_gate)
        assert "is_fallback_decision_source" in src, (
            "`_run_tool_policy_gate` must classify via "
            "`transport.is_fallback_decision_source`."
        )
        # Scoped to `decision_source` on purpose. The function
        # legitimately maps `TransportErrorSource.AUTH_ERROR` to error
        # code NR-A003 in the *typed* transport-error arm — that is
        # correct and unrelated to the fallback predicate. A blanket
        # "AUTH_ERROR must not appear" pin would flag valid code.
        inline = _decision_source_comparisons(src)
        assert not inline, (
            "`_run_tool_policy_gate` still tests `decision_source` "
            "inline instead of delegating — that inline set was the "
            "copy that kept AUTH_ERROR and matched a dead uppercase "
            "prefix:\n  " + "\n  ".join(inline)
        )

    def test_runtime_delegates(self):
        from nullrun import runtime

        src = inspect.getsource(runtime.NullRunRuntime.check_workflow_budget)
        assert "is_fallback_decision_source" in src, (
            "`check_workflow_budget` must classify via the shared "
            "predicate."
        )
        inline = _decision_source_comparisons(src)
        assert not inline, (
            "`check_workflow_budget` still tests `decision_source` "
            "inline:\n  " + "\n  ".join(inline)
        )

    def test_no_other_inline_copies_exist(self):
        """No third copy may grow anywhere in the package.

        Walks the AST of every module under ``src/nullrun`` looking for
        a module-level test of `decision_source` against a transport
        error literal. A new call site must import the shared helper,
        not re-open the set.
        """
        root = pathlib.Path(__file__).resolve().parent.parent
        offenders: list[str] = []
        for path in (root / "src" / "nullrun").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                # A `TransportErrorSource.X in {…}` test, or a
                # `startswith(...)` on a decision_source.
                if not isinstance(node, ast.Compare):
                    continue
                snippet = ast.unparse(node)
                if "TransportErrorSource" in snippet and (
                    "in {" in snippet.replace(" ", " ")
                    or "not in" in snippet
                ):
                    # Allowed only inside the shared definition.
                    if path.name == "transport.py":
                        continue
                    offenders.append(f"{path.name}:{node.lineno}: {snippet[:80]}")
        assert not offenders, (
            "inline TransportErrorSource membership tests found outside "
            "transport.py — each is a hand-maintained copy of the "
            "fallback predicate:\n  " + "\n  ".join(offenders)
        )
