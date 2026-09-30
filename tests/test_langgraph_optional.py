"""tests/test_langgraph_optional.py — langchain-core is an optional extra.

DEF-MP-TS12-SDK-05 (RUN_ID 20260929T1338, 2026-09-29).

`langchain-core` is a `dev` extra, not a core dependency — but
``instrumentation/langgraph.py`` imported it unconditionally, and the
chain is:

    NullRunRuntime.__init__ -> instrumentation.auto (make_dedup_state)
        -> instrumentation.langgraph -> langchain_core.callbacks

So a clean ``pip install nullrun`` followed by ``init()`` died with
``ModuleNotFoundError: No module named 'langchain_core'`` for every
consumer who does not use LangChain — i.e. most of them.

Why this test runs in a SUBPROCESS
----------------------------------
A ``sys.meta_path`` blocker inserted in-process cannot model a missing
dependency, because ``langchain_core`` is already in ``sys.modules`` by
the time the test runs (pytest imports the SDK). Blocking only the
import path would test nothing. The only faithful simulation of "this
package is not installed" is a fresh interpreter that never had the
chance to import it.

So each case below spawns ``python -c`` with a meta-path blocker that
raises ModuleNotFoundError for ``langchain_core``, and asserts the
real outcome of ``import nullrun`` / ``NullRunRuntime(...)``.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

# Blocker + probe, run in a clean interpreter. The probe deliberately
# points at an unroutable URL: pre-fix the run died earlier, at the
# langchain_core import; post-fix it must get PAST the import and fail
# (or succeed) on the network instead. Either way the ModuleNotFoundError
# for langchain_core must not appear.
_BLOCK_AND_PROBE = textwrap.dedent(
    """
    import sys

    class _BlockLangChainCore:
        def find_module(self, name, path=None):
            if name == "langchain_core" or name.startswith("langchain_core."):
                return self
        def load_module(self, name):
            raise ModuleNotFoundError("No module named '%s'" % name)

    sys.meta_path.insert(0, _BlockLangChainCore())
    for _m in [m for m in sys.modules if m.startswith("langchain_core")]:
        del sys.modules[_m]

    import nullrun
    print("IMPORT_OK")

    from nullrun.runtime import NullRunRuntime
    try:
        NullRunRuntime(
            api_key="nr_live_testkey123456",
            api_url="https://nullrun.invalid",
            polling=False,
        )
        print("INIT_OK")
    except ModuleNotFoundError as exc:
        if "langchain_core" in str(exc):
            print("INIT_LANGCHAIN_MISSING:" + str(exc))
        else:
            print("INIT_OTHER_MODULENOTFOUND:" + str(exc))
    except Exception as exc:
        # Network / auth failure against the unroutable host is the
        # EXPECTED outcome: it proves we got past the import.
        print("INIT_REACHED_NETWORK:" + type(exc).__name__)
    """
)


def _run_probe() -> str:
    proc = subprocess.run(
        [sys.executable, "-c", _BLOCK_AND_PROBE],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.stdout + proc.stderr


class TestLangGraphOptional:
    def test_import_nullrun_without_langchain_core(self):
        """`import nullrun` must not require langchain-core."""
        out = _run_probe()
        assert "IMPORT_OK" in out, (
            "`import nullrun` failed with langchain-core absent — the SDK "
            "is unusable for non-LangChain consumers.\n" + out
        )

    def test_init_without_langchain_core_does_not_crash_on_import(self):
        """`init()` must not die on the langchain_core import.

        Pre-fix this printed
        ``INIT_LANGCHAIN_MISSING:No module named 'langchain_core'``.
        Post-fix it must reach the network layer instead, proving the
        import chain is clean.
        """
        out = _run_probe()
        assert "INIT_LANGCHAIN_MISSING" not in out, (
            "NullRunRuntime.__init__ still requires langchain-core. The import "
            "chain runtime -> instrumentation.auto -> instrumentation.langgraph "
            "-> langchain_core.callbacks must be optional.\n" + out
        )
        assert ("INIT_OK" in out) or ("INIT_REACHED_NETWORK" in out), (
            "init() did not reach the network layer with langchain-core "
            "absent — it failed for an unexpected reason.\n" + out
        )

    def test_langchain_present_still_works(self):
        """Guard the fix did not break the langchain-installed path.

        Without langchain-core the callback falls back to an `object`
        base. This asserts the real base class is still used when the
        dependency IS present, so LangChain registration still works.
        """
        from nullrun.instrumentation.langgraph import BaseCallbackHandler

        try:
            from langchain_core.callbacks import (
                BaseCallbackHandler as RealBase,
            )
        except ModuleNotFoundError:
            pytest.skip("langchain-core not installed in this environment")

        assert BaseCallbackHandler is RealBase, (
            "with langchain-core installed, NullRunCallback must subclass the "
            "real BaseCallbackHandler so LangChain recognises the handler"
        )
