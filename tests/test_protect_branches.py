"""
Additional tests for ``nullrun.decorators`` — branch coverage for the
``_safe_args`` / ``_strip_details_balanced`` / ``_enforce_sensitive_tool``
helpers, the fail-CLOSED / fail-OPEN contract, the KILL→BlockedException
unification (Round 3), and the ``@protect()`` paren-form.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nullrun.breaker.exceptions import (
    NullRunBlockedException,
    NullRunTransportError,
    TransportErrorSource,
    WorkflowKilledInterrupt,
    WorkflowPausedException,
)
from nullrun.decorators import (
    SENSITIVE_ARG_KEYS,
    _enforce_sensitive_tool,
    _safe_args,
    _safe_error_str,
    _safe_kwargs,
    _safe_repr,
    _strip_details_balanced,
    protect,
    sensitive,
)
from nullrun.runtime import NullRunRuntime


@pytest.fixture
def test_runtime(monkeypatch):
    """Provide a runtime in test mode so get_runtime() returns without
    authenticating against a real server.
    """
    monkeypatch.setenv("NULLRUN_API_KEY", "test-key-12345678")
    NullRunRuntime.reset_instance()
    rt = NullRunRuntime(api_key="test-key-12345678", _test_mode=True)
    rt.organization_id = "org-1"
    # Stub the transport so the network is never touched in tests.
    # - ``_do_flush`` overrides the public flush.
    # - ``_do_flush_locked`` is what ``track()`` calls when the buffer
    #   fills — must also be stubbed to be safe.
    # - ``_client`` is the httpx client — magicmock so even a stray
    #   ``post`` raises a clean AttributeError instead of hitting the API.
    rt._transport._do_flush = lambda: None
    rt._transport._do_flush_locked = lambda: None
    rt._transport._client = MagicMock()
    NullRunRuntime._instance = rt
    yield rt
    NullRunRuntime.reset_instance()


# ─── _safe_repr ───────────────────────────────────────────────────────


def test_safe_repr_short_value_passes_through(test_runtime):
    """Under the 50-char cap, value flows through unmodified."""
    s = _safe_repr("hi")
    assert s == "'hi'"


def test_safe_repr_long_value_truncated(test_runtime):
    """Over 50 chars, suffix ``...<truncated>`` appended."""
    s = _safe_repr("x" * 200, max_len=50)
    assert s.endswith("...<truncated>")
    assert len(s) > 50


def test_safe_repr_redacts_details_before_truncating(test_runtime):
    """``details={PAN: '4111-...'}`` must be redacted BEFORE truncation."""
    # String kept under the 50-char cap so the redact survives the
    # truncate step (otherwise we'd only verify truncation).
    secret = "4111-1111-1111-1111"
    payload = f"x details={{'card': '{secret}'}}"
    out = _safe_repr(payload, max_len=50)
    assert secret not in out
    assert "<redacted>" in out


# ─── _safe_kwargs ────────────────────────────────────────────────────


def test_safe_kwargs_masks_sensitive_keys(test_runtime):
    out = _safe_kwargs({"password": "p", "token": "t", "user": "alice"})
    assert out["password"] == "***"
    assert out["token"] == "***"
    # Non-sensitive values go through _safe_repr → ``repr()``.
    assert out["user"] == "'alice'"


def test_safe_kwargs_is_case_insensitive(test_runtime):
    out = _safe_kwargs({"PASSWORD": "p", "Token": "t"})
    assert out["PASSWORD"] == "***"
    assert out["Token"] == "***"


# ─── _safe_args ──────────────────────────────────────────────────────


def test_safe_args_masks_positional_sensitive_param(test_runtime):
    """Positional sensitive param (e.g. ``credit_card_number``) is masked."""
    def charge(credit_card_number, amount):
        return amount

    masked = _safe_args(charge, ("4111-1111-1111-1111", 50))
    assert masked[0] == "***"
    # ``repr(50)`` is ``"50"``.
    assert masked[1] == "50"


def test_safe_args_trailing_extra_args_uses_safe_repr():
    """``*args``-style callable: extra positional args use safe_repr."""
    def variadic(*args, **kwargs):
        return args

    masked = _safe_args(variadic, ("x", "ok"))
    # ``*args`` has no name → safe_repr for both (no masking).
    assert masked[0] == "'x'"
    assert masked[1] == "'ok'"


def test_safe_args_no_signature_falls_back_to_safe_repr():
    """C-extension / built-in without signature → safe_repr on all."""

    class _NoSig:
        # Builtin-ish class; ``inspect.signature`` raises ValueError.
        pass

    masked = _safe_args(_NoSig, ("4111", 50))
    assert masked[0] == "'4111'"
    assert masked[1] == "50"


def test_safe_args_signature_raises_typeerror_falls_back():
    """``inspect.signature`` raises ``TypeError`` for some callables."""

    class _Bad:
        # Trigger ValueError path.
        __signature__ = None  # type: ignore[assignment]

    masked = _safe_args(_Bad, ("x",))
    assert masked == ["'x'"]


# ─── _strip_details_balanced ─────────────────────────────────────────


def test_strip_details_balanced_no_details_unchanged():
    s = "no details here"
    assert _strip_details_balanced(s) == s


def test_strip_details_balanced_details_without_brace_unchanged():
    s = "details=plain text without braces"
    # No '{' after 'details=' → left as-is.
    assert _strip_details_balanced(s) == s


def test_strip_details_balanced_simple_payload(test_runtime):
    s = "context=ok details={'a': 1, 'b': 2}"
    out = _strip_details_balanced(s)
    assert "<redacted>" in out
    assert "'a': 1" not in out


def test_strip_details_balanced_nested_dicts(test_runtime):
    """Nested dicts in the details payload → still redacted as a unit."""
    s = "msg details={'a': {'b': {'c': 'secret'}}}"
    out = _strip_details_balanced(s)
    assert "secret" not in out
    assert "<redacted>" in out


def test_strip_details_balanced_string_with_braces_inside(test_runtime):
    """A string value containing ``{`` / ``}`` does NOT break the brace walker."""
    s = 'msg details={"key": "value with { and } inside"}'
    out = _strip_details_balanced(s)
    assert "value with { and } inside" not in out
    assert "<redacted>" in out


def test_strip_details_balanced_multiple_details(test_runtime):
    """Two ``details={...}`` substrings in the same string → both redacted."""
    s = "first details={'a': 1} middle details={'b': 2}"
    out = _strip_details_balanced(s)
    assert out.count("<redacted>") == 2


def test_strip_details_balanced_escaped_quote_in_string(test_runtime):
    r"""A string with an escaped quote (\") is handled by the walker."""
    s = r'msg details={"key": "val\"ue"}'
    out = _strip_details_balanced(s)
    assert "<redacted>" in out


# ─── _safe_error_str ─────────────────────────────────────────────────


def test_safe_error_str_none_returns_none(test_runtime):
    assert _safe_error_str(None) is None


def test_safe_error_str_simple_message_passes_through(test_runtime):
    e = RuntimeError("plain")
    assert _safe_error_str(e) == "plain"


def test_safe_error_str_details_redacted(test_runtime):
    e = RuntimeError("oops details={'secret': 'value'}")
    out = _safe_error_str(e)
    assert "secret" not in out
    assert "<redacted>" in out


# ─── _enforce_sensitive_tool ────────────────────────────────────────


def test_enforce_sensitive_tool_non_sensitive_returns(test_runtime):
    """Non-sensitive tool → no-op, no runtime call."""
    rt = MagicMock()
    rt.is_sensitive_tool.return_value = False
    rt.execute = MagicMock()
    _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
    rt.execute.assert_not_called()


def test_enforce_sensitive_tool_real_block_propagates(test_runtime):
    """``decision=block`` from gateway → raises NullRunBlockedException."""
    rt = MagicMock()
    rt.is_sensitive_tool.return_value = True
    rt.execute.side_effect = NullRunBlockedException(
        workflow_id="wf-1", reason="denied"
    )
    with pytest.raises(NullRunBlockedException):
        _enforce_sensitive_tool(rt, lambda x: x, (1,), {})


def test_enforce_sensitive_tool_transport_error_fail_closed(test_runtime):
    """``NullRunTransportError`` + no fail-open → raises NullRunBlockedException."""
    rt = MagicMock()
    rt.is_sensitive_tool.return_value = True
    rt.execute.side_effect = NullRunTransportError(
        "down",
        source=TransportErrorSource.NETWORK_ERROR,
        endpoint="/execute",
    )
    with pytest.raises(NullRunBlockedException) as excinfo:
        _enforce_sensitive_tool(rt, lambda x: x, (1,), {})
    assert "NETWORK_ERROR" in excinfo.value.reason


def test_enforce_sensitive_tool_transport_error_fail_open(test_runtime, monkeypatch):
    """``NULLRUN_SENSITIVE_FAIL_OPEN=1`` + transport error → body runs."""
    monkeypatch.setenv("NULLRUN_SENSITIVE_FAIL_OPEN", "1")
    rt = MagicMock()
    rt.is_sensitive_tool.return_value = True
    rt.execute.side_effect = NullRunTransportError(
        "down",
        source=TransportErrorSource.NETWORK_ERROR,
        endpoint="/execute",
    )
    # Must NOT raise.
    _enforce_sensitive_tool(rt, lambda x: x, (1,), {})


def test_enforce_sensitive_tool_generic_exception_fail_closed(test_runtime):
    """Non-transport exception → NullRunBlockedException."""
    rt = MagicMock()
    rt.is_sensitive_tool.return_value = True
    rt.execute.side_effect = ValueError("oops")
    with pytest.raises(NullRunBlockedException):
        _enforce_sensitive_tool(rt, lambda x: x, (1,), {})


def test_enforce_sensitive_tool_generic_exception_fail_open(test_runtime, monkeypatch):
    """Generic exception + fail-open → no raise."""
    monkeypatch.setenv("NULLRUN_SENSITIVE_FAIL_OPEN", "1")
    rt = MagicMock()
    rt.is_sensitive_tool.return_value = True
    rt.execute.side_effect = ValueError("oops")
    _enforce_sensitive_tool(rt, lambda x: x, (1,), {})  # no raise


def test_enforce_sensitive_tool_dict_with_fallback_decision_source(test_runtime):
    """``decision_source`` starts with FALLBACK_ → raises."""
    rt = MagicMock()
    rt.is_sensitive_tool.return_value = True
    rt.execute.return_value = {
        "decision": "allow",
        "decision_source": "FALLBACK_NETWORK_ERROR",
    }
    with pytest.raises(NullRunBlockedException):
        _enforce_sensitive_tool(rt, lambda x: x, (1,), {})


def test_enforce_sensitive_tool_dict_with_typed_error_source(test_runtime):
    """``decision_source`` ∈ TransportErrorSource values → raises."""
    rt = MagicMock()
    rt.is_sensitive_tool.return_value = True
    rt.execute.return_value = {
        "decision": "allow",
        "decision_source": TransportErrorSource.GATEWAY_ERROR,
    }
    with pytest.raises(NullRunBlockedException):
        _enforce_sensitive_tool(rt, lambda x: x, (1,), {})


def test_enforce_sensitive_tool_dict_with_fallback_fail_open(test_runtime, monkeypatch):
    """``decision_source`` FALLBACK_* + fail-open → no raise."""
    monkeypatch.setenv("NULLRUN_SENSITIVE_FAIL_OPEN", "1")
    rt = MagicMock()
    rt.is_sensitive_tool.return_value = True
    rt.execute.return_value = {
        "decision": "allow",
        "decision_source": "FALLBACK_NETWORK_ERROR",
    }
    _enforce_sensitive_tool(rt, lambda x: x, (1,), {})  # no raise


def test_enforce_sensitive_tool_dict_with_gateway_decision_falls_through(test_runtime):
    """``decision_source=gateway`` + ``decision=allow`` → no raise."""
    rt = MagicMock()
    rt.is_sensitive_tool.return_value = True
    rt.execute.return_value = {
        "decision": "allow",
        "decision_source": "gateway",
    }
    _enforce_sensitive_tool(rt, lambda x: x, (1,), {})  # no raise


def test_enforce_sensitive_tool_sensitive_kwargs_masked_in_call(test_runtime):
    """``password`` kwarg on a sensitive tool is masked before /execute."""
    rt = MagicMock()
    rt.is_sensitive_tool.return_value = True
    rt.execute.return_value = {"decision": "allow", "decision_source": "gateway"}
    _enforce_sensitive_tool(rt, lambda x: x, (), {"password": "p", "user": "alice"})
    # ``runtime.execute`` is called positionally: ``(tool_name, input_data, ...)``.
    forwarded = rt.execute.call_args.args[1]
    assert forwarded["kwargs"]["password"] == "***"
    # Non-sensitive → safe_repr → ``"'alice'"``.
    assert forwarded["kwargs"]["user"] == "'alice'"


def test_enforce_sensitive_tool_sensitive_positional_arg_masked(test_runtime):
    """``credit_card_number`` positional on a sensitive tool is masked."""
    rt = MagicMock()
    rt.is_sensitive_tool.return_value = True
    rt.execute.return_value = {"decision": "allow", "decision_source": "gateway"}

    def charge(credit_card_number, amount):
        return amount

    _enforce_sensitive_tool(rt, charge, ("4111-1111-1111-1111", 50), {})
    forwarded = rt.execute.call_args.args[1]
    assert forwarded["args"][0] == "***"


# ─── @protect paren-form ─────────────────────────────────────────────


def test_protect_with_parens_returns_decorator(test_runtime):
    """``@protect()`` with empty parens works just like ``@protect``."""
    # Stub track_event so the finally-block span emission does not
    # re-enter check_control_plane with our mocked side effect.
    test_runtime.track_event = MagicMock()

    @protect()
    def f(x):
        return x * 2

    assert f(3) == 6


def test_protect_without_parens_wraps_directly(test_runtime):
    """``@protect`` without parens wraps the function directly."""
    # Stub track_event so the finally-block span emission does not
    # re-enter check_control_plane with our mocked side effect.
    test_runtime.track_event = MagicMock()

    @protect
    def f(x):
        return x * 2

    assert f(3) == 6


# ─── KILL→BlockedException unification (Round 3) ──────────────────────


def test_protect_sync_kill_raises_NullRunBlockedException(test_runtime):
    """``WorkflowKilledInterrupt`` from gate → unified as NullRunBlockedException."""
    from nullrun import decorators as dec_mod

    rt = NullRunRuntime(api_key="test-key-12345678", _test_mode=True)
    rt.track_event = MagicMock()
    rt.check_control_plane = MagicMock(
        side_effect=WorkflowKilledInterrupt(workflow_id="wf-1", reason="admin kill")
    )
    rt.check_workflow_budget = MagicMock()
    dec_mod._runtime = rt

    @protect
    def f():
        return "should not run"

    with pytest.raises(NullRunBlockedException) as excinfo:
        f()
    assert excinfo.value.reason == "admin kill"


def test_protect_sync_pause_raises_NullRunBlockedException(test_runtime):
    """``WorkflowPausedException`` from gate → unified as NullRunBlockedException."""
    from nullrun import decorators as dec_mod

    rt = NullRunRuntime(api_key="test-key-12345678", _test_mode=True)
    rt.track_event = MagicMock()
    rt.check_control_plane = MagicMock(
        side_effect=WorkflowPausedException(workflow_id="wf-1", reason="budget pause")
    )
    rt.check_workflow_budget = MagicMock()
    dec_mod._runtime = rt

    @protect
    def f():
        return "should not run"

    with pytest.raises(NullRunBlockedException) as excinfo:
        f()
    assert excinfo.value.reason == "budget pause"


@pytest.mark.asyncio
async def test_protect_async_kill_re_raises_WorkflowKilledInterrupt():
    """Async wrapper does NOT unify — kill signal propagates as-is so
    async frameworks can interrupt the event loop cleanly.
    """
    from nullrun import decorators as dec_mod

    rt = NullRunRuntime(api_key="test-key-12345678", _test_mode=True)
    rt.track_event = MagicMock()
    rt.check_control_plane = MagicMock(
        side_effect=WorkflowKilledInterrupt(workflow_id="wf-1", reason="x")
    )
    rt.check_workflow_budget = MagicMock()
    dec_mod._runtime = rt

    @protect
    async def f():
        return "ok"

    with pytest.raises(WorkflowKilledInterrupt):
        await f()


# ─── @sensitive decorator ────────────────────────────────────────────


def test_sensitive_registers_tool_with_runtime(test_runtime):
    """``@sensitive`` calls ``add_sensitive_tool`` on the runtime."""

    @sensitive
    def my_charge(amount):
        return amount

    rt = NullRunRuntime.get_instance()
    assert "my_charge" in rt.get_sensitive_tools()


def test_sensitive_runtime_init_failure_is_silent(test_runtime, monkeypatch):
    """If runtime construction fails inside @sensitive, import must not crash."""
    from nullrun import decorators

    monkeypatch.setattr(decorators, "_get_or_create_runtime", MagicMock(side_effect=RuntimeError("x")))
    # Decorator must NOT raise even though registration failed.
    @sensitive
    def f():
        return 1

    assert f() == 1


# ─── reset() ──────────────────────────────────────────────────────────


def test_reset_clears_runtime_slot(test_runtime, monkeypatch):
    """``reset()`` shuts down the runtime and clears the module-level slot."""
    from nullrun import decorators

    rt = NullRunRuntime.get_instance()
    decorators._runtime = rt
    decorators.reset()
    assert decorators._runtime is None


def test_reset_when_no_runtime_is_silent(test_runtime):
    from nullrun import decorators

    decorators._runtime = None
    decorators.reset()  # must not raise


def test_reset_shutdown_failure_is_silent(test_runtime, monkeypatch):
    """``reset()`` swallows runtime shutdown exceptions."""
    from nullrun import decorators

    rt = MagicMock()
    rt.shutdown.side_effect = RuntimeError("oops")
    decorators._runtime = rt
    decorators.reset()  # must not raise
    assert decorators._runtime is None


# ─── get_protected_runtime ──────────────────────────────────────────


def test_get_protected_runtime_returns_runtime(test_runtime):
    from nullrun import decorators

    rt = NullRunRuntime.get_instance()
    decorators._runtime = rt
    assert decorators.get_protected_runtime() is rt


def test_get_protected_runtime_falls_back_to_get_runtime(test_runtime, monkeypatch):
    """When the decorator slot is empty, fall back to the global singleton."""
    from nullrun import decorators

    decorators._runtime = None
    NullRunRuntime._instance = NullRunRuntime(api_key="test-key-12345678", _test_mode=True)
    try:
        out = decorators.get_protected_runtime()
        assert out is NullRunRuntime._instance
    finally:
        NullRunRuntime.reset_instance()