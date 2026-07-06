"""Tests for the NullRunDecision / NullRunInfrastructureError split.

These tests pin the categorical contract that lets host code write::

    try:
...
    except NullRunDecision as d: # budget, tool, rate, loop, pause
        return d.user_message 
    except NullRunInfrastructureError as e: # transport, backend, auth, config
        sentry.capture_exception(e)
        return "service unavailable"

Backward compat is also asserted — every existing ``except`` clause
(``except NullRunError:``, ``except NullRunBlockedException:``,...)
must keep matching after the refactor.
"""
from __future__ import annotations

import pytest

from nullrun.breaker import exceptions as exc

# ---------------------------------------------------------------------------
# Category membership — every subclass lands in the right bucket
# ---------------------------------------------------------------------------
DECISION_CLASSES = [
    exc.NullRunBlockedException,
    exc.NullRunBudgetError,
    exc.NullRunToolBlockedError,
    exc.WorkflowPausedException,
]

INFRASTRUCTURE_CLASSES = [
    exc.NullRunTransportError,
    exc.NullRunBackendError,
    exc.RateLimitError,
    exc.NullRunConfigError,
    exc.NullRunAuthenticationError,
    exc.NullRunAuthError,
]


@pytest.mark.parametrize("cls", DECISION_CLASSES)
def test_decision_classes_inherit_from_nullrun_decision(cls):
    assert issubclass(cls, exc.NullRunDecision), (
        f"{cls.__name__} should be a NullRunDecision"
    )
    # And transitively, still NullRunError — back-compat.
    assert issubclass(cls, exc.NullRunError)


@pytest.mark.parametrize("cls", INFRASTRUCTURE_CLASSES)
def test_infrastructure_classes_inherit_from_nullrun_infrastructure(cls):
    assert issubclass(cls, exc.NullRunInfrastructureError), (
        f"{cls.__name__} should be a NullRunInfrastructureError"
    )
    # And transitively, still NullRunError — back-compat.
    assert issubclass(cls, exc.NullRunError)


def test_decision_and_infrastructure_are_disjoint():
    """A class cannot be both Decision and Infrastructure — that would
    mean ``except`` order matters, which is a footgun."""
    for cls in DECISION_CLASSES:
        assert not issubclass(cls, exc.NullRunInfrastructureError), (
            f"{cls.__name__} should NOT also be Infrastructure"
        )
    for cls in INFRASTRUCTURE_CLASSES:
        assert not issubclass(cls, exc.NullRunDecision), (
            f"{cls.__name__} should NOT also be Decision"
        )


def test_workflow_killed_interrupt_is_neither_decision_nor_infrastructure():
    """The kill signal is a BaseException — it deliberately bypasses
    ``except Exception:`` so careless handlers can't swallow operator
    kills. It must NOT inherit from NullRunDecision (which would make
    it catchable by `except Exception:` via the NullRunError branch)."""
    assert not issubclass(exc.WorkflowKilledInterrupt, exc.NullRunError)
    assert not issubclass(exc.WorkflowKilledInterrupt, exc.NullRunDecision)
    assert not issubclass(exc.WorkflowKilledInterrupt, exc.NullRunInfrastructureError)
    # But it IS a BaseException, which is the whole point.
    assert issubclass(exc.WorkflowKilledInterrupt, BaseException)


# ---------------------------------------------------------------------------
# Backward compatibility — existing handlers still match
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cls", DECISION_CLASSES + INFRASTRUCTURE_CLASSES)
def test_every_subclass_still_caught_by_except_nullrun_error(cls):
    """The split is additive — `except NullRunError:` keeps matching
    every public subclass. If this breaks, every existing handler in
    customer code that does ``except NullRunError:`` silently stops
    catching the new instances."""
    # We can't construct every class cleanly without their specific
    # kwargs, but we can verify the issubclass invariant directly.
    assert issubclass(cls, exc.NullRunError)


def test_except_nullrun_blocked_still_catches_budget_and_tool():
    """Existing cookbook pattern: ``except NullRunBlockedException``
    catches both budget and tool blocks. Must keep working."""
    budget = exc.NullRunBudgetError("wf", "x")
    tool = exc.NullRunToolBlockedError("wf", "x", tool_name="send_email")
    assert isinstance(budget, exc.NullRunBlockedException)
    assert isinstance(tool, exc.NullRunBlockedException)


def test_except_nullrun_transport_still_catches_backend_and_rate():
    """Existing cookbook pattern: ``except NullRunTransportError``
    catches both backend 5xx and rate limit."""
    backend = exc.NullRunBackendError("boom", endpoint="check")
    rate = exc.RateLimitError(
        "rate limited",
        source=exc.TransportErrorSource.GATEWAY_ERROR,
        endpoint="check",
    )
    assert isinstance(backend, exc.NullRunTransportError)
    assert isinstance(rate, exc.NullRunTransportError)


def test_except_nullrun_authentication_still_catches_auth_error():
    """Existing cookbook pattern: ``except NullRunAuthenticationError``
    catches the 401-specific subclass."""
    auth = exc.NullRunAuthError("rejected")
    assert isinstance(auth, exc.NullRunAuthenticationError)


# ---------------------------------------------------------------------------
# Construction still works for every category
# ---------------------------------------------------------------------------
def test_can_construct_each_decision_subclass():
    """Constructability check — if the refactor broke a constructor
    signature, this fires immediately rather than at customer runtime."""
    exc.NullRunBlockedException("wf", "reason")
    exc.NullRunBudgetError("wf", "reason")
    exc.NullRunToolBlockedError("wf", "reason", tool_name="send_email")
    exc.WorkflowPausedException("wf", "reason")


def test_can_construct_each_infrastructure_subclass():
    exc.NullRunTransportError(
        "boom",
        source=exc.TransportErrorSource.NETWORK_ERROR,
        endpoint="execute",
    )
    exc.NullRunBackendError("boom", endpoint="check")
    exc.RateLimitError(
        "rate limited",
        source=exc.TransportErrorSource.GATEWAY_ERROR,
        endpoint="check",
    )
    exc.NullRunConfigError("misconfigured")
    exc.NullRunAuthenticationError("unauthenticated")
    exc.NullRunAuthError("rejected")


def test_workflow_killed_interrupt_constructs_and_carries_metadata():
    """The kill class still works after the refactor and exposes
    ``workflow_id`` / ``reason`` so the FastAPI middleware can render
    a clean response without parsing ``str(exc)``."""
    killed = exc.WorkflowKilledInterrupt(workflow_id="wf-1", reason="killed via API")
    assert killed.workflow_id == "wf-1"
    assert killed.reason == "killed via API"
    # error_code comes from the deprecated parent class attribute.
    assert killed.error_code == "NR-W002"


# ---------------------------------------------------------------------------
# Catalog compatibility — Decision/Infrastructure members keep their
# existing error_code so format_user_message keeps working
# ---------------------------------------------------------------------------
def test_decision_subclasses_have_distinct_codes():
    """Each decision subclass must have its own error_code (not just
    the generic NR-X001 fallback). Otherwise every block would
    resolve to the same user-facing message and the user couldn't
    tell budget-exceeded from tool-blocked from loop-detected."""
    codes = {
        cls.error_code
        for cls in DECISION_CLASSES
        if cls is not exc.NullRunBlockedException  # generic — excluded
    }
    assert len(codes) >= 3, (
        f"Decision subclasses share too few codes: {codes}. "
        "Each block reason (budget, tool, pause, ...) needs its own code."
    )


def test_infrastructure_subclasses_have_distinct_codes():
    codes = {
        cls.error_code
        for cls in INFRASTRUCTURE_CLASSES
        if cls is not exc.NullRunTransportError  # generic — excluded
    }
    assert len(codes) >= 3, (
        f"Infrastructure subclasses share too few codes: {codes}. "
        "Network / 5xx / auth / config / rate-limit each need a code."
    )
