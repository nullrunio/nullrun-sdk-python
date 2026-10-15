"""Every exception class the SDK can raise must be importable from the
top-level ``nullrun`` namespace, and must appear in ``__all__``.

Why this test exists (QA u3h, 2026-10-15)
-----------------------------------------
24 classes the SDK RAISES were missing from
``nullrun.__init__._LAZY_EXPORTS``. ``from nullrun import
NullRunApprovalDeniedError`` raised ``ImportError`` while
``except NullRunError:`` in the same file still caught the instance
— because ``NullRunApprovalDeniedError`` IS a ``NullRunError``.

That asymmetry is what made the defect survive: enforcement was
never touched (the classes existed, ``_V3_ERROR_CODE_MAP``
constructed them), and the documented catch is the BASE class. A
cookbook written against ``except NullRunError:`` passes in
production, so the missing narrower name is only discovered when
someone goes looking for it. The typed catalog exists precisely so a
caller can branch on WHICH refusal occurred — a name that cannot be
imported cannot be branched on.

Two failure modes, so two tests:

1. ``test_every_raisable_exception_is_importable_from_top_level`` —
   enumerates the exception classes by parsing the source, then
   imports each one. Catches a class being REMOVED from the export
   tables.
2. ``test_a_new_exception_class_cannot_be_added_without_an_export`` —
   fails if a class is ADDED to ``exceptions.py`` / ``categories.py``
   and not exported. This is the direction the original defect would
   have been caught in, had the test existed.

Both read the source rather than hardcode a list, so neither goes
stale.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import nullrun
from nullrun.breaker import categories as cat_mod
from nullrun.breaker import exceptions as exc_mod

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "nullrun"

# Modules that define raisable exceptions. `categories.py` holds
# NullRunUnclassifiedRefusalError, which ADR-062 §2.2 requires the SDK
# to RAISE when a refusal carries no category it trusts — putting it
# in the scan is what stops the "an unclassifiable refusal must raise"
# guarantee from having an unimportable outcome.
EXCEPTION_MODULES = (SRC_ROOT / "breaker" / "exceptions.py", SRC_ROOT / "breaker" / "categories.py")

# Modules that own the corresponding live objects, for the identity
# assertion.
LIVE_MODULES = (exc_mod, cat_mod)


def _defined_exception_names() -> list[str]:
    """Every ``class X(Exception-ish)`` defined in the scanned modules.

    AST rather than regex: a class name inside a docstring or a
    comment must not count, and the existing source-pin tests in this
    repo have already been burned by a pin reading text that a
    comment could satisfy (memory `pin-cannot-forbid-string-its-own-doc-quotes`).
    """
    names: list[str] = []
    for path in EXCEPTION_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            # Only exception classes: the base is a name we can resolve
            # without importing, so this cannot recurse.
            if not node.bases:
                continue
            base_names = [b.id for b in node.bases if isinstance(b, ast.Name)]
            # Resolve transitively against the modules under scan plus
            # the live modules. `Exception` / `BaseException` /
            # `Enum` terminate the walk.
            if _is_exception_base(base_names):
                names.append(node.name)
    return sorted(set(names))


def _is_exception_base(base_names: list[str]) -> bool:
    """True when any base is (or derives from) ``Exception``.

    Note what is NOT here: ``Enum`` and ``str`` are not exception
    bases. ``DecisionCategory(str, Enum)`` and
    ``TransportErrorSource(str, Enum)`` live in the scanned modules and
    are values a caller may want to name, but a caller never
    ``except``s them — so they are outside what this module promises
    and must not be counted into the scan.
    """
    if any(b in {"Exception", "BaseException"} for b in base_names):
        return True
    for module in LIVE_MODULES:
        for b in base_names:
            candidate = getattr(module, b, None)
            if isinstance(candidate, type) and issubclass(candidate, Exception):
                return True
    return False


def _exported_names() -> set[str]:
    """Names registered in ``_LAZY_EXPORTS``."""
    src = (SRC_ROOT / "__init__.py").read_text(encoding="utf-8")
    table = src.split("_LAZY_EXPORTS: dict", 1)[1].split("\n}", 1)[0]
    return set(re.findall(r'"([A-Za-z_]\w*)":\s*\(\s*"', table))


RAISABLE = _defined_exception_names()
EXPORTED = _exported_names()


def test_the_scan_actually_found_something():
    """Guard against a vacuous pass.

    If the AST walk ever stops matching (a refactor to
    ``ExceptionGroup``, a moved module), every test below would pass
    with an empty name list and the guard would report green while
    checking nothing — the exact
    "declared ok, nothing ran" failure class. Pin a floor at the
    known size and assert a few names that must be in the set.
    """
    # 39 = every exception class in exceptions.py + the one in
    # categories.py. Enums are deliberately not counted (see
    # `_is_exception_base`), so this number must not be inflated by
    # them and must not drift upward by accident either.
    assert len(RAISABLE) == 39, (
        f"expected the exception scan to find exactly 39 classes, found "
        f"{len(RAISABLE)}. The scan is the premise of every test in "
        f"this module — if it silently stopped matching, they would "
        f"all pass vacuously."
    )
    for must_exist in (
        "NullRunError",
        "NullRunApprovalDeniedError",
        "NullRunUnclassifiedRefusalError",
        "WorkflowKilledInterrupt",
    ):
        assert must_exist in RAISABLE, (
            f"{must_exist} must be in the scanned set; its absence "
            f"means the AST walk is broken, not that the class is gone."
        )


def test_every_raisable_exception_is_importable_from_top_level():
    """Each class must be reachable as ``from nullrun import X``.

    This is the regression for the reported defect: the class exists,
    ``__getattr__`` has no entry, and the import fails while the
    instance still satisfies ``except NullRunError:``.
    """
    failures = []
    for name in RAISABLE:
        try:
            exec(f"from nullrun import {name}", {})  # noqa: S102
        except ImportError:
            failures.append(name)
    assert not failures, (
        f"{len(failures)} exception class(es) the SDK raises cannot be "
        f"imported from the top-level `nullrun` namespace: "
        f"{sorted(failures)}. A user cannot `except` a name they cannot "
        f"import. Add each to `_LAZY_EXPORTS` in "
        f"src/nullrun/__init__.py. (Note the trap this defect hid "
        f"behind: `except NullRunError:` kept working, so no test that "
        f"only catches the base would ever notice.)"
    )


def test_a_new_exception_class_cannot_be_added_without_an_export():
    """A class added to the modules must be exported in the same change.

    The other direction from the reported defect, and the one worth
    pinning: it turns "remember to also update ``__init__.py``" into a
    red test instead of a silent hole that surfaces months later as a
    user report.

    There are currently ZERO deliberate omissions — every raisable
    exception is exported, including the pre-Layer-1 root alias
    ``BreakerError``. If a future omission is genuinely wanted, add it
    to ALLOWED_UNEXPORTED with a comment saying why; the list is empty
    so any new entry is visible in the diff.
    """
    unexported = sorted(name for name in RAISABLE if name not in EXPORTED)
    # Compared against the documented list rather than `[]`, so the
    # escape hatch in the message below is real: adding a name to
    # ALLOWED_UNEXPORTED must turn this test green, or the message is
    # lying about how to proceed.
    assert unexported == sorted(ALLOWED_UNEXPORTED), (
        f"these exception classes are raisable but not registered in "
        f"_LAZY_EXPORTS: {unexported}. A new class landed in "
        f"exceptions.py / categories.py without a matching entry in "
        f"src/nullrun/__init__.py _LAZY_EXPORTS. If the omission is "
        f"deliberate, extend ALLOWED_UNEXPORTED below with the reason; "
        f"otherwise add the export."
    )


# Documented, deliberate omissions from `_LAZY_EXPORTS`. Empty on
# purpose: `BreakerError` IS exported, because old cookbook code that
# catches it must keep working. If this list is ever filled in, each
# entry needs a comment saying why the class is raisable but should
# not be nameable from the top level.
ALLOWED_UNEXPORTED: set[str] = set()


def test_allowed_unexported_is_not_broader_than_the_scan():
    """Every documented omission must still be a real class.

    Otherwise a rename would leave a stale key in
    ALLOWED_UNEXPORTED that silently permits the NEW name to go
    unexported — the exemption would outlive its justification.
    """
    stale = sorted(set(ALLOWED_UNEXPORTED) - set(RAISABLE))
    assert not stale, (
        f"ALLOWED_UNEXPORTED names classes that no longer exist: "
        f"{stale}. Remove the stale exemption — it would otherwise "
        f"let a future same-named class skip the export silently."
    )


@pytest.mark.parametrize(
    "name",
    [
        # The names the QA matrix reported as top_level_missing, kept
        # explicit so a regression names the offending class instead
        # of dumping the whole scan.
        "NullRunApprovalNotYetApprovedError",
        "NullRunApprovalResponseMissingError",
        "NullRunApprovalDeniedError",
        "NullRunApprovalExpiredError",
        "NullRunApprovalReplayRejectedError",
        "NullRunApprovalDigestMismatchError",
        "NullRunApprovalToolDigestMismatchError",
        "NullRunUnclassifiedRefusalError",
    ],
)
def test_reported_missing_names_are_importable(name: str):
    """Named regression cases from the QA report (NR-A010 … NR-A015)."""
    assert hasattr(nullrun, name), (
        f"nullrun.{name} must be reachable — this is one of the "
        f"top_level_missing names from the QA matrix."
    )