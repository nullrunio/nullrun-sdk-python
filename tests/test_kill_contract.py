"""Smoke test for the kill contract exception classes.

Run from sdk-python/ root: python tests/test_kill_contract.py
"""
import sys
import warnings

# Make the sdk-python src importable
sys.path.insert(0, "src")

from nullrun.breaker.exceptions import (  # noqa: E402
    WorkflowKilledException,
    WorkflowKilledInterrupt,
    WorkflowPausedException,
)


def test_interrupt_is_base_exception():
    assert issubclass(WorkflowKilledInterrupt, BaseException)


def test_old_class_no_longer_exception():
    # The whole point: kill must not be catchable by `except Exception`.
    assert not issubclass(WorkflowKilledException, Exception)


def test_old_class_is_interrupt_for_back_compat():
    # User code with `except WorkflowKilledException` must still catch
    # a new `WorkflowKilledInterrupt` raise. Python's `except X` matches
    # subclasses of X, so the new class must be a subclass of the old.
    assert issubclass(WorkflowKilledInterrupt, WorkflowKilledException)


def test_old_class_emits_deprecation_warning():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        WorkflowKilledException(workflow_id="wf-1", reason="test")
    assert len(caught) == 1
    assert issubclass(caught[0].category, DeprecationWarning)


def test_new_class_emits_no_warning():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        WorkflowKilledInterrupt(workflow_id="wf-2", reason="test")
    assert len(caught) == 0


def test_interrupt_not_caught_by_except_exception():
    # Static check: the contract is "not subclass of Exception", which is
    # exactly the same property `except Exception:` uses. We use
    # introspection rather than a real raise/except so the test runner's
    # own `except BaseException` doesn't have to special-case the
    # propagation semantics.
    assert not issubclass(WorkflowKilledInterrupt, Exception), (
        "kill must not be catchable by except Exception"
    )


def test_interrupt_caught_by_except_interrupt():
    caught = None
    try:
        raise WorkflowKilledInterrupt(workflow_id="wf-4", reason="kill")
    except WorkflowKilledInterrupt as e:
        caught = e
    assert caught is not None
    assert caught.workflow_id == "wf-4"
    assert caught.reason == "kill"


def test_interrupt_caught_by_except_old_class():
    """Back-compat: old `except WorkflowKilledException` still works
    because the class now inherits from WorkflowKilledInterrupt.
    Verified statically — same property `except` uses at runtime."""
    assert issubclass(WorkflowKilledInterrupt, WorkflowKilledException)


def test_pause_still_caught_by_except_exception():
    """Paused is intentionally still Exception-derived: it's recoverable."""
    caught = None
    try:
        raise WorkflowPausedException(workflow_id="wf-6", reason="pause")
    except Exception as e:
        caught = e
    assert caught is not None


def test_public_export():
    """The new class must be importable from the top-level package."""
    import nullrun
    assert hasattr(nullrun, "WorkflowKilledInterrupt")
    # And the old one still works
    assert hasattr(nullrun, "WorkflowKilledException")


if __name__ == "__main__":
    tests = [
        test_interrupt_is_base_exception,
        test_old_class_no_longer_exception,
        test_old_class_is_interrupt_for_back_compat,
        test_old_class_emits_deprecation_warning,
        test_new_class_emits_no_warning,
        test_interrupt_not_caught_by_except_exception,
        test_interrupt_caught_by_except_interrupt,
        test_interrupt_caught_by_except_old_class,
        test_pause_still_caught_by_except_exception,
        test_public_export,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
        except BaseException as e:  # noqa: BLE001
            # Must catch BaseException here because
            # `test_interrupt_not_caught_by_except_exception` deliberately
            # raises WorkflowKilledInterrupt (a BaseException) and the
            # whole point is that it propagates through `except Exception`.
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print()
    if failed:
        print(f"{failed} test(s) failed.")
        sys.exit(1)
    print(f"All {len(tests)} tests passed.")
