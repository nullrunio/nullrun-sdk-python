"""Source-pin regression tests for the SDK cleanup batch (2026-09-10).

Each test pins a single cleanup fix to prevent future refactors from
silently re-introducing the debt that was removed. Mirrors the
source-pin pattern from ``test_2026_08_11_fixes.py``.

Defects / cleanups being pinned (RUN_ID 2026-09-10 batch):

- CLEANUP-PARSE-CODE-COLLISION — ``_safe_json`` previously raised
  with ``error_code="NR-T001"``, which collides with
  ``NullRunToolBlockedError.error_code`` (breaker/exceptions.py:955).
  Cookbook handlers that branch on ``exc.error_code == "NR-T001"``
  mis-classified a JSON parse failure as a tool block. Pin: the
  literal must be NR-T-PARSE.

- CLEANUP-REDIS-UNAVAILABLE-CODE — ``transport.py`` previously
  mapped the backend's ``REDIS_UNAVAILABLE`` envelope to
  ``NullRunBudgetError``. That's wrong: the backend distinguishes
  BUDGET_REDIS_UNAVAILABLE (budget path, fail-CLOSED 402) and
  RATE_LIMIT_REDIS_UNAVAILABLE (rate-limit path, fail-CLOSED 429).
  Lumping both into ``NullRunBudgetError`` conflated the two and
  hid rate-limit-Redis outages from operators. Pin: the literal
  mapping must be gone (replaced by per-path branches elsewhere).

- CLEANUP-BUDGET-THROTTLE-ZOMBIE — ``NullRunBudgetThrottleError``
  (NR-B007) was a zombie exception class. ``runtime.py:2116``
  raises ``WorkflowPausedException`` on ``decision == "throttle"``,
  so ``NullRunBudgetThrottleError`` never fired on any wire or
  runtime path. Catalog + class removed to keep the exception
  surface in sync with what the SDK actually raises.

- CLEANUP-WORKFLOW-KILLED-DISCOVERABILITY —
  ``NullRunWorkflowKilledError`` was the only kill-related typed
  exception missing from ``__init__._LAZY_EXPORTS`` and
  ``__init__.__all__``. Asymmetric with ``WorkflowKilledInterrupt``
  which was already exported. Pin: must be importable from top
  level ``nullrun``.
"""

from __future__ import annotations

import os
import re

RUNTIME_PATH = os.path.join(
    os.path.dirname(__file__), "..", "src", "nullrun", "runtime.py"
)
TRANSPORT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "src", "nullrun", "transport.py"
)
EXCEPTIONS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "src", "nullrun", "breaker", "exceptions.py"
)
MESSAGES_PATH = os.path.join(
    os.path.dirname(__file__), "..", "src", "nullrun", "messages.py"
)
INIT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "src", "nullrun", "__init__.py"
)


def _read(path: str) -> str:
    return open(path, encoding="utf-8").read()


def _strip_comment_lines(src: str) -> str:
    """Drop ``#`` comment lines so source-pin tests checking for
    forbidden user-facing wording don't trip on rationale comments
    that legitimately mention the same word or code."""
    return "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )


# ─── CLEANUP-PARSE-CODE-COLLISION ──────────────────────────────────────────


def test_safe_json_uses_nr_t_parse_not_nr_t001():
    """``_safe_json`` must raise with ``error_code="NR-T-PARSE"``.

    Pre-cleanup, the literal was ``NR-T001``, which collides with
    ``NullRunToolBlockedError.error_code`` (breaker/exceptions.py).
    Cookbook handlers that branch on ``exc.error_code == "NR-T001"``
    mis-classified a JSON parse failure as a tool block. The
    post-cleanup literal NR-T-PARSE is dedicated transport-class
    code; matches NR-T (transport) vocabulary without colliding
    with NR-T001 / NR-T002 / etc.
    """
    src = _read(TRANSPORT_PATH)
    assert 'error_code="NR-T-PARSE"' in src, (
        "transport.py:_safe_json must raise with "
        "error_code='NR-T-PARSE' (avoids collision with NR-T001 / "
        "NullRunToolBlockedError)"
    )
    # Negative pin: the pre-cleanup literal must NOT appear
    # anywhere in transport.py. Code-only strip isn't necessary
    # here — the literal is always inside a string in production
    # code (assignments to ``error_code=``), never inside a comment
    # that explains the cleanup (we have inline rationale elsewhere
    # that mentions the old code, but as prose not a string literal).
    code_only = _strip_comment_lines(src)
    assert 'error_code="NR-T001"' not in code_only, (
        "transport.py must NOT contain the pre-cleanup literal "
        "error_code='NR-T001' — that code belongs to "
        "NullRunToolBlockedError (breaker/exceptions.py). The cleanup "
        "renamed the transport-side literal to NR-T-PARSE."
    )


# ─── CLEANUP-REDIS-UNAVAILABLE-CODE ───────────────────────────────────────


def test_transport_no_longer_maps_redis_unavailable_to_budget_error():
    """``transport.py`` must NOT have a blanket mapping from
    ``REDIS_UNAVAILABLE`` to ``NullRunBudgetError``.

    Pre-cleanup, the mapping was ``"REDIS_UNAVAILABLE": NullRunBudgetError``.
    That's wrong because the backend distinguishes:
      - BUDGET_REDIS_UNAVAILABLE (budget path, fail-CLOSED 402)
      - RATE_LIMIT_REDIS_UNAVAILABLE (rate-limit path, fail-CLOSED 429)
    Lumping both into ``NullRunBudgetError`` conflated the two and
    hid rate-limit-Redis outages from operators.

    Post-cleanup the literal mapping is removed; per-path branches
    elsewhere in transport.py route the correct code to the right
    typed exception (e.g. ``NullRunBudgetRedisError`` / ``NullRunRateLimitRedisError``).
    """
    src = _read(TRANSPORT_PATH)
    code_only = _strip_comment_lines(src)
    assert '"REDIS_UNAVAILABLE": NullRunBudgetError' not in code_only, (
        "transport.py must not contain the pre-cleanup literal "
        "'REDIS_UNAVAILABLE': NullRunBudgetError. The blanket mapping "
        "conflated BUDGET_REDIS_UNAVAILABLE (budget path) with "
        "RATE_LIMIT_REDIS_UNAVAILABLE (rate-limit path); per-path "
        "branches must route each to its own typed exception."
    )
    # Negative pin: also confirm the dict entry style (with single
    # quotes variant) isn't sneaking in via formatter churn.
    assert "'REDIS_UNAVAILABLE': NullRunBudgetError" not in code_only, (
        "transport.py must not contain the single-quote variant of "
        "the pre-cleanup REDIS_UNAVAILABLE mapping."
    )


# ─── CLEANUP-BUDGET-THROTTLE-ZOMBIE ───────────────────────────────────────


def test_null_run_budget_throttle_error_class_is_removed():
    """``NullRunBudgetThrottleError`` (NR-B007) must be removed from
    ``breaker/exceptions.py``.

    Pre-cleanup this class existed with ``error_code = "NR-B007"`` and
    ``retryable = True``. But ``runtime.py:2116`` raises
    ``WorkflowPausedException`` on ``decision == "throttle"``, so
    the class never fired on any wire or runtime path — a zombie
    exception that the catalog had to maintain anyway.

    Post-cleanup: class removed, NR-B007 catalog entry removed,
    test_format_user_message_handles_budget_throttle removed.
    """
    src = _read(EXCEPTIONS_PATH)
    code_only = _strip_comment_lines(src)
    assert "class NullRunBudgetThrottleError" not in code_only, (
        "breaker/exceptions.py must NOT define NullRunBudgetThrottleError "
        "— it was a zombie class never raised on a wire or runtime path. "
        "See CLEANUP-BUDGET-THROTTLE-ZOMBIE."
    )
    # Negative pin: the class's error_code literal must also be gone.
    # (Other classes may still reference the string "NR-B007" in
    # comments or string formatting, but no `error_code = "NR-B007"`
    # class attribute on a NullRun*Error subclass should remain.)
    assert re.search(
        r"error_code\s*=\s*[\"']NR-B007[\"']",
        code_only,
    ) is None, (
        "No exception class in breaker/exceptions.py may declare "
        "error_code='NR-B007' — NullRunBudgetThrottleError was removed "
        "and the code is reserved-but-unused."
    )


def test_messages_catalog_no_longer_has_nr_b007_entry():
    """``messages.DEFAULT_MESSAGES`` must NOT contain a NR-B007 entry.

    Companion to ``test_null_run_budget_throttle_error_class_is_removed``:
    the catalog entry for NR-B007 must be removed in lockstep so the
    formatter doesn't return a stale message for a code that's no
    longer raised.
    """
    src = _read(MESSAGES_PATH)
    code_only = _strip_comment_lines(src)
    # The catalog uses dict-literal style: "NR-B007": "...", ...
    # A bare re.search for the string key catches any formatting
    # variant (single quote, trailing comma, etc.).
    assert re.search(r"[\"']NR-B007[\"']\s*:", code_only) is None, (
        "messages.py DEFAULT_MESSAGES must NOT contain a key for "
        "NR-B007 — the corresponding exception class was removed "
        "and a stale catalog entry would mislead operators."
    )


def test_test_messages_no_longer_has_budget_throttle_test():
    """``tests/test_messages.py`` must NOT have a
    ``test_format_user_message_handles_budget_throttle`` test.

    Companion cleanup: the orphan test for the zombie class was
    removed along with the class. Pin the removal so a copy-paste
    doesn't restore the test for a class that no longer exists.
    """
    test_path = os.path.join(
        os.path.dirname(__file__), "test_messages.py"
    )
    src = _read(test_path)
    code_only = _strip_comment_lines(src)
    assert "test_format_user_message_handles_budget_throttle" not in code_only, (
        "tests/test_messages.py must not contain "
        "test_format_user_message_handles_budget_throttle — the "
        "orphan test for the zombie NullRunBudgetThrottleError was "
        "removed when the class was deleted."
    )


# ─── CLEANUP-WORKFLOW-KILLED-DISCOVERABILITY ──────────────────────────────


def test_null_run_workflow_killed_error_importable_from_top_level():
    """``NullRunWorkflowKilledError`` must be importable from the
    top-level ``nullrun`` namespace.

    Pre-cleanup, this class was the only kill-related typed
    exception missing from ``__init__._LAZY_EXPORTS`` and
    ``__init__.__all__``. Asymmetric with ``WorkflowKilledInterrupt``
    (already exported). Pin the top-level discoverability so host
    code can ``from nullrun import NullRunWorkflowKilledError``
    alongside the other typed kill exceptions.
    """
    src = _read(INIT_PATH)
    # Pin: the class must appear in the _LAZY_EXPORTS dict
    # (lazy export via __getattr__). The lazy-export form is the
    # post-0.15.0 pattern; the older eager `from nullrun.breaker.X
    # import ...` form has been migrated to lazy across the SDK.
    assert re.search(
        r"[\"']NullRunWorkflowKilledError[\"']\s*:\s*\(",
        src,
    ), (
        "nullrun/__init__.py _LAZY_EXPORTS must register "
        "NullRunWorkflowKilledError for top-level import. Pre-cleanup "
        "the class was discoverable only via "
        "nullrun.breaker.exceptions, asymmetric with "
        "WorkflowKilledInterrupt."
    )
    # Pin: the class must also appear in __all__ so tab-completion
    # surfaces it via dir(nullrun).
    assert re.search(
        r"__all__\s*=\s*\[[\s\S]*?[\"']NullRunWorkflowKilledError[\"']",
        src,
    ), (
        "nullrun/__init__.py __all__ must include "
        "'NullRunWorkflowKilledError' for dir(nullrun) tab-completion "
        "to surface the class."
    )


# ─── Behavioural smoke tests ──────────────────────────────────────────────


def test_null_run_budget_throttle_error_no_longer_importable():
    """Runtime check: importing the zombie class must fail.

    Companion to the source-pin test above — verifies the class is
    actually gone from the live module, not just that the literal
    text was deleted from the source file (someone could delete
    the text but leave an aliased re-export, for example).
    """
    import nullrun
    import nullrun.breaker.exceptions as exc_mod

    assert not hasattr(exc_mod, "NullRunBudgetThrottleError"), (
        "nullrun.breaker.exceptions.NullRunBudgetThrottleError must "
        "be removed at runtime; hasattr returning True means the "
        "class survived the cleanup."
    )
    # Also: must not be re-exported from top-level nullrun
    assert not hasattr(nullrun, "NullRunBudgetThrottleError"), (
        "nullrun.NullRunBudgetThrottleError must NOT exist (top-level "
        "re-export of the zombie class would defeat the cleanup)."
    )


def test_null_run_workflow_killed_error_importable_at_runtime():
    """Runtime check: ``from nullrun import NullRunWorkflowKilledError``
    must succeed.
    """
    import nullrun
    from nullrun.breaker import exceptions as exc_mod

    # Top-level import path (the new discoverability surface)
    top_level_cls = getattr(nullrun, "NullRunWorkflowKilledError", None)
    assert top_level_cls is not None, (
        "nullrun.NullRunWorkflowKilledError must be importable "
        "from the top-level namespace after the CLEANUP-WORKFLOW-"
        "KILLED-DISCOVERABILITY fix."
    )
    # Must be the same class object as the one in breaker.exceptions
    # (not a separate wrapper or proxy class).
    assert top_level_cls is exc_mod.NullRunWorkflowKilledError, (
        "nullrun.NullRunWorkflowKilledError must be the same class "
        "object as nullrun.breaker.exceptions.NullRunWorkflowKilledError; "
        "a separate proxy class would defeat isinstance() checks "
        "across the codebase."
    )
