"""
Regression tests for the new framework auto-instrumentation patches
in 0.4.0.

Phase 7 of the production-readiness plan adds three new patches:
- llama-index (LLMChatEndEvent + FunctionCallEvent via Dispatcher)
- crewai (Crew.kickoff + Crew.kickoff_async + post-run usage_metrics)
- autogen (BaseChatAgent.on_messages + OpenAIChatCompletionClient.create)

The 6 placeholder tests removed on 2026-06-28 were
``@pytest.mark.skipif(True, ...)`` stubs with empty bodies — they
provided no coverage and gave a false sense of green-on-arrival.
Real coverage for these frameworks lives in the framework-specific
integration suites (one per repo, gated on the framework being
installed). See Sprint 2.9 ticket.
"""

from __future__ import annotations

# ===========================================================================
# Common: graceful no-op when packages absent
# ===========================================================================


def test_patch_llama_index_returns_false_when_missing(monkeypatch):
    """patch_llama_index returns False (no-op) when llama-index not installed."""
    import importlib
    import sys

    # Force ImportError
    monkeypatch.setitem(sys.modules, "llama_index.core.instrumentation", None)
    monkeypatch.setitem(sys.modules, "llama_index", None)
    monkeypatch.setitem(sys.modules, "llama_index.core", None)

    # Reload to clear cached imports
    if "nullrun.instrumentation.llama_index" in sys.modules:
        importlib.reload(sys.modules["nullrun.instrumentation.llama_index"])

    from nullrun.instrumentation.llama_index import patch_llama_index

    assert patch_llama_index(None) is False


def test_patch_crewai_returns_false_when_missing(monkeypatch):
    """patch_crewai returns False (no-op) when crewai not installed."""
    import sys

    monkeypatch.setitem(sys.modules, "crewai", None)
    if "nullrun.instrumentation.crewai" in sys.modules:
        import importlib

        importlib.reload(sys.modules["nullrun.instrumentation.crewai"])

    from nullrun.instrumentation.crewai import patch_crewai

    assert patch_crewai(None) is False


def test_patch_autogen_returns_false_when_missing(monkeypatch):
    """patch_autogen returns False (no-op) when autogen not installed."""
    import sys

    monkeypatch.setitem(sys.modules, "autogen_agentchat", None)
    monkeypatch.setitem(sys.modules, "autogen_agentchat.agents", None)
    if "nullrun.instrumentation.autogen" in sys.modules:
        import importlib

        importlib.reload(sys.modules["nullrun.instrumentation.autogen"])

    from nullrun.instrumentation.autogen import patch_autogen

    assert patch_autogen(None) is False


# ===========================================================================
# Common: modules importable + registered in auto_instrument
# ===========================================================================


def test_new_framework_modules_importable():
    """The three new patch modules are importable from `nullrun.instrumentation`."""
    from nullrun.instrumentation import autogen, crewai, llama_index

    assert hasattr(llama_index, "patch_llama_index")
    assert hasattr(llama_index, "unpatch_llama_index")
    assert hasattr(crewai, "patch_crewai")
    assert hasattr(crewai, "unpatch_crewai")
    assert hasattr(autogen, "patch_autogen")
    assert hasattr(autogen, "unpatch_autogen")


# ===========================================================================
# Sprint 2.9 (B47): safe_patch wrapper for centralised error visibility
# ===========================================================================
# Pre-fix: the auto-instrumentation modules had 25+ scattered
# ``try/except Exception: pass  # pragma: no cover`` blocks. A
# patch failure (e.g. a vendor SDK signature change) would
# silently disable cost tracking. The operator would only find
# out when the bill arrived.
#
# Post-fix: every patch call in `auto_instrument` is wrapped in
# ``safe_patch()`` which logs at WARNING with the patch name +
# exception. These tests pin the wrapper contract.


class TestSafePatchWrapper:
    """``safe_patch`` must surface real failures and skip benign ones."""

    def test_returns_true_on_success(self):
        from nullrun.instrumentation._safe_patch import safe_patch

        def _ok():
            return True

        assert safe_patch("ok_patch", _ok) is True

    def test_returns_true_on_none_result(self):
        """``None`` is treated as success (patcher had nothing to report)."""
        from nullrun.instrumentation._safe_patch import safe_patch

        def _noop():
            return None

        assert safe_patch("noop_patch", _noop) is True

    def test_returns_false_on_false_result(self):
        from nullrun.instrumentation._safe_patch import safe_patch

        def _benign_noop():
            return False  # vendor class not found, etc.

        assert safe_patch("benign_patch", _benign_noop) is False

    def test_import_error_is_debug_not_warning(self, caplog):
        """Optional dep missing is debug-level, not warning."""
        import logging

        from nullrun.instrumentation._safe_patch import safe_patch

        def _missing_dep():
            raise ImportError("optional dep not installed")

        with caplog.at_level(logging.DEBUG, logger="nullrun.instrumentation._safe_patch"):
            result = safe_patch("missing_dep_patch", _missing_dep)
        assert result is False
        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert not warning_records, (
            f"ImportError must not be logged at WARNING level; "
            f"got: {[r.getMessage() for r in warning_records]}"
        )

    def test_other_exception_logs_at_warning(self, caplog):
        """Real patch failure must be visible at WARNING level (B47)."""
        import logging

        from nullrun.instrumentation._safe_patch import safe_patch

        def _broken():
            raise RuntimeError("vendor SDK signature changed")

        with caplog.at_level(logging.WARNING, logger="nullrun.instrumentation._safe_patch"):
            result = safe_patch("broken_patch", _broken)
        assert result is False
        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("broken_patch" in r.getMessage() for r in warning_records), (
            f"Patch failure must log at WARNING with patch name; "
            f"got: {[r.getMessage() for r in warning_records]}"
        )
        # The exception type must be in the log so the operator
        # can search the vendor SDK changelog.
        assert any("RuntimeError" in r.getMessage() for r in warning_records), (
            "Exception type must be included in the WARNING log so "
            "the operator can correlate with vendor SDK changelogs."
        )