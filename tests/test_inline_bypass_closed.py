"""tests/test_inline_bypass_closed.py — `mode="inline"` is gone.

B1, 2026-09-30 (ADR-037). Part of the DEF-MP-TS12-ENF-01 cluster that
followed RUN_ID 20260929T1338.

The bypass
----------
``runtime.execute(..., mode="inline")`` returned a synthesised local
``allow`` without contacting the gateway::

    return {
        "decision": "allow",
        "decision_source": DecisionSource.LOCAL,
        "explanation": "Inline mode: local enforcement only. Caller
                        explicitly opted out of /execute — budget /
                        rate / tool-block policies bypassed.",
        ...
    }

So budget, rate limit and tool_block were all skipped, and the only
guard was a sensitivity check — meaning whether a call was enforced
depended on whether someone had remembered to mark the tool sensitive.
``DecisionSource.LOCAL`` is deliberately not a "synthetic" source
(it is a real local decision as far as every consumer of
``is_fallback_decision_source`` is concerned), so downstream code
honoured it exactly as it would honour a gateway allow.

The parameter was vestigial in the other direction too: ``mode`` goes
on the wire but the backend does not read it (``transport.py:1223``,
"Wire-present but unused by backend"). Its only real function was
deciding whether to skip enforcement.

The fix raises instead of silently coercing to "strict": a caller who
asked for inline believes they have a fast local path, and quietly
handing them a round-trip is a semantic change they cannot see.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from nullrun.breaker.exceptions import NullRunConfigError
from nullrun.runtime import NullRunRuntime


@pytest.fixture
def rt():
    """A runtime with no transport — inline must be refused before any I/O.

    Built via ``object.__new__`` deliberately: the point of the test is
    that the refusal happens at the TOP of ``execute``, before the
    runtime touches its transport, so a real constructor (which
    authenticates over the network) would test nothing.
    """
    return object.__new__(NullRunRuntime)


class TestInlineRefused:
    def test_inline_raises(self, rt):
        with pytest.raises(NullRunConfigError):
            rt.execute("some_tool", {"args": {}}, mode="inline")

    def test_error_names_the_removal_and_the_absence_of_a_replacement(self, rt):
        """The message is the migration path — a user has to read it.

        A bare TypeError or "invalid mode" would leave the caller with
        no idea what to do instead, which is how people end up
        reaching for a bypass.
        """
        with pytest.raises(NullRunConfigError) as exc:
            rt.execute("some_tool", {"args": {}}, mode="inline")
        msg = str(exc.value)
        assert "inline" in msg
        # Must say what the thing WAS, so the user understands the
        # security consequence of what they were getting.
        assert "bypass" in msg.lower(), (
            f"the error must state that inline bypassed enforcement, "
            f"not just that the value is invalid: {msg!r}"
        )
        # Must point somewhere.
        assert "auto" in msg, f"the error must name the replacement: {msg!r}"

    def test_error_carries_a_code(self, rt):
        with pytest.raises(NullRunConfigError) as exc:
            rt.execute("some_tool", {"args": {}}, mode="inline")
        assert exc.value.error_code, (
            "a config error without an error_code cannot be triaged from a log"
        )

    @pytest.mark.parametrize("tool", ["charge_card", "send_email", "x"])
    def test_refused_for_every_tool_including_sensitive_ones(self, rt, tool):
        """The old guard let SENSITIVE tools through to /execute.

        That is precisely why the check was the problem: enforcement
        of an ordinary tool was a configuration detail, and a tool
        that nobody remembered to mark ran ungated. Now the refusal
        does not depend on the tool at all.
        """
        with pytest.raises(NullRunConfigError):
            rt.execute(tool, {"args": {}}, mode="inline")

    @pytest.mark.parametrize("mode", ["auto", "strict"])
    def test_surviving_modes_are_not_refused(self, rt, mode):
        """Only "inline" is gone — the other two must still get through.

        These two proceed to the /execute round-trip, which the bare
        runtime object cannot perform. Reaching the transport rather
        than raising `NullRunConfigError` is exactly the proof wanted:
        the mode check let them past.
        """
        with pytest.raises(Exception) as exc:
            rt.execute("some_tool", {"args": {}}, mode=mode)
        assert not isinstance(exc.value, NullRunConfigError), (
            f"mode={mode!r} must still be accepted; only 'inline' is removed. Got {exc.value!r}"
        )


class TestNoBypassRemains:
    """The bypass must be gone from the CODE, not just unreachable.

    A test that only checks the raise would pass while a commented-out
    or refactored copy of the local-allow dict sat in the file — which
    is exactly the shape of the defect being closed.
    """

    def _execute_source(self) -> str:
        import inspect

        return inspect.getsource(NullRunRuntime.execute)

    def test_no_local_decision_source_synthesis(self):
        """`DecisionSource.LOCAL` was how a local allow was labelled.

        A synthesised local allow must not be constructible in
        `execute` any more.
        """
        assert "DecisionSource.LOCAL" not in self._execute_source(), (
            "`execute` must not synthesise a local allow — that was the "
            "inline bypass. Every decision must come from the gateway."
        )

    def test_no_allow_execution_short_circuit(self):
        """The bypass returned `allow_execution: True` without a call."""
        src = self._execute_source()
        assert '"allow_execution": True' not in src, (
            "`execute` must not return a hard-coded allow_execution=True "
            "before consulting the gateway — that was the inline branch"
        )

    def test_inline_is_only_ever_refused_not_honoured(self):
        """`inline` may appear, but only in a refusal.

        Parsed rather than grepped, so the comment explaining the
        removal cannot satisfy the pin — the self-defeating-pin hazard
        this SDK has hit twice now.
        """
        tree = ast.parse(_dedent(self._execute_source()))
        inline_comparisons = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and "inline" in ast.unparse(node):
                inline_comparisons.append((node.lineno, ast.unparse(node)))
        assert inline_comparisons, (
            'the `mode == "inline"` check must still be present — it is '
            "what raises. If it was deleted, callers get a silent "
            "behaviour change instead of a named error."
        )
        # Every mention must be a comparison (the guard), never an
        # assignment or a pass-through into the wire body.
        for lineno, snippet in inline_comparisons:
            assert snippet.startswith("mode == 'inline'") or snippet.startswith(
                'mode == "inline"'
            ), (
                f"line {lineno}: unexpected use of 'inline' — {snippet!r}. "
                "It must only be compared against, never assigned or "
                "forwarded."
            )


def _dedent(src: str) -> str:
    import textwrap

    return textwrap.dedent(src)


class TestOrphanedRegistryRemoved:
    """The strict-mode registry existed only to serve the bypass.

    `register_strict_mode_forced` had zero callers even before this
    change (its only documented writer, a `@sensitive` decorator, no
    longer exists). `is_strict_mode_forced` was reachable only from
    the inline branch. Left in place they read as a live mechanism and
    invite someone to wire them back up.
    """

    def test_strict_mode_forced_symbols_are_gone(self):
        import nullrun.runtime as runtime_mod

        for name in (
            "register_strict_mode_forced",
            "is_strict_mode_forced",
            "_STRICT_MODE_FORCED",
        ):
            assert not hasattr(runtime_mod, name), (
                f"{name} is dead after the inline bypass was closed — it "
                "existed only to force strict mode past inline. Dead "
                "security machinery is how a bypass gets reintroduced."
            )

    def test_sensitivity_registry_is_untouched(self):
        """The per-runtime registry is a separate, still-public surface.

        Whether it should outlive the inline bypass is a separate
        decision. This test exists so that removing it later is a
        deliberate act rather than an accident of B1's cleanup.
        """
        for name in (
            "add_sensitive_tool",
            "register_sensitive_tools",
            "remove_sensitive_tool",
            "is_sensitive_tool",
            "get_sensitive_tools",
        ):
            assert hasattr(NullRunRuntime, name), (
                f"{name} is still part of the documented public surface — "
                "B1 removed the inline bypass, not the sensitivity API"
            )


class TestPackageHasNoStaleReferences:
    def test_no_module_still_honours_inline(self):
        """No other module may reintroduce the bypass.

        `transport.py` still accepts and forwards `mode` on the wire
        (the backend ignores it). What must not exist anywhere is code
        that TREATS inline as a reason to skip the round-trip.
        """
        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for path in (root / "src" / "nullrun").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "inline" not in text:
                continue
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Compare) and ast.unparse(node).startswith(
                    'mode == "inline"'
                ):
                    # The single legitimate site: the refusal in
                    # runtime.execute. Anything else is a bypass.
                    if path.name == "runtime.py":
                        continue
                    offenders.append(f"{path.name}:{node.lineno}")
        assert not offenders, (
            "modules other than runtime.py compare mode to 'inline' — "
            "that is a second bypass site:\n  " + "\n  ".join(offenders)
        )
