"""
Regression test for plan item P0-1: positional args to a sensitive tool
must be masked the same way as kwargs.

Pre-fix, only kwargs were passed through ``_safe_kwargs``. A sensitive
tool called positionally — ``charge("4111-1111-1111-1111", 50)`` —
would forward the PAN as-is into the /execute payload and the audit
log. PCI-DSS Req. 3.4 requires the PAN to be unreadable anywhere it is
stored; sending the raw string to the gateway violates that.

Post-fix, ``_safe_args`` introspects the function signature, binds
positional args to parameter names, and applies the same
``SENSITIVE_ARG_KEYS`` mask that the kwargs path already uses.

We test by capturing the payload that ``runtime.execute`` received
(the SDK's pre-execution policy check is the only thing that sees
the args, so the audit-log PII risk lives at this single hop).
"""

import inspect
from unittest.mock import MagicMock

import pytest

from nullrun.decorators import _safe_args, _safe_kwargs


def test_safe_args_masks_known_sensitive_position():
    """``def charge(credit_card_number, amount)`` with a PAN at position 0
    must come out masked. ``credit_card_number`` is in SENSITIVE_ARG_KEYS."""

    def charge(credit_card_number, amount):
        return None

    masked = _safe_args(charge, ("4111-1111-1111-1111", 50))
    assert masked[0] == "***"
    # Amount is not sensitive — it should round-trip through _safe_repr.
    assert masked[1] == "50"


def test_safe_args_preserves_non_sensitive_position():
    """Non-sensitive positional args must pass through _safe_repr
    unchanged (modulo truncation), so dashboard debugging still has
    the value, not just ``***``."""

    def run(prompt, temperature):
        return None

    masked = _safe_args(run, ("hello world", 0.7))
    assert masked[0] == "'hello world'"
    assert masked[1] == "0.7"


def test_safe_args_masks_password_keyword_position():
    """The mask is case-insensitive (matches _safe_kwargs behaviour)
    and matches the full SENSITIVE_ARG_KEYS set: ``password``,
    ``api_key``, ``token``, etc."""

    def login(user, password):
        return None

    masked = _safe_args(login, ("alice", "s3cret"))
    assert masked[0] == "'alice'"
    assert masked[1] == "***"


def test_safe_args_handles_var_args():
    """When the function has ``*args``, the extra positional args have
    no parameter name to key on. They should still be ``_safe_repr``-ed
    so we don't ship an arbitrary ``repr(obj)`` to the audit log."""

    def variadic(*args):
        return None

    masked = _safe_args(variadic, ("ok", 1, 2, 3))
    assert masked == ["'ok'", "1", "2", "3"]


def test_safe_args_handles_builtin_without_signature():
    """``inspect.signature`` raises ``ValueError`` on builtins /
    C-extensions. We must fall back to safe repr for every arg rather
    than crash the @protect pipeline (FIX-4 / T3-S2 invariant:
    @protect must never silently swallow errors; it must also never
    crash on unrelated introspection failures)."""
    # ``len`` is a builtin — no inspectable signature.
    masked = _safe_args(len, ("sensitive-payload",))
    assert masked[0] == "'sensitive-payload'"  # safe repr, not raw


def test_enforce_sensitive_tool_passes_masked_args_to_runtime_execute():
    """End-to-end: ``_enforce_sensitive_tool`` must hand ``runtime.execute``
    a payload whose ``args[0]`` (the PAN) is ``"***"``, not the raw
    string. This is the audit-log integration point."""
    from nullrun.decorators import _enforce_sensitive_tool

    def charge(credit_card_number, amount):
        return None

    runtime = MagicMock()
    runtime.is_sensitive_tool.return_value = True
    runtime.execute.return_value = {"decision": "allow"}

    _enforce_sensitive_tool(
        runtime,
        charge,
        args=("4111-1111-1111-1111", 50),
        kwargs={},
    )

    # The /execute payload is the second positional arg to runtime.execute.
    payload = runtime.execute.call_args[0][1]
    assert payload["args"][0] == "***", (
        f"positional PAN leaked into /execute payload — got {payload['args'][0]!r}"
    )
    # Amount is non-sensitive — survives _safe_repr.
    assert payload["args"][1] == "50"


def test_safe_args_and_kwargs_consistency():
    """A sensitive param passed positionally OR as a kwarg must end up
    masked with the same ``"***"`` token. This keeps the audit log
    format uniform regardless of call style."""

    def login(user, password):
        return None

    # Positional call:
    pos_masked = _safe_args(login, ("alice", "s3cret"))
    # Kwargs call:
    kw_masked = _safe_kwargs({"user": "alice", "password": "s3cret"})

    assert pos_masked[1] == "***"
    assert kw_masked["password"] == "***"
    # And the non-sensitive slot is preserved (different format — list
    # vs dict — but both should NOT be masked):
    assert pos_masked[0] == "'alice'"
    assert kw_masked["user"] == "'alice'"
