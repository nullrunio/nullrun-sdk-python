"""Tests for the toolbox.langgraph.wrapper helper.

The wrapper monkey-patches a compiled LangGraph app's
`invoke` and `stream` methods to attach a `NullRunCallback` so
the runtime sees LLM usage. These tests verify the wiring
without requiring an actual LangChain/LangGraph runtime —
we just need a duck-typed object with `.invoke` and `.stream`.
"""
from typing import Any

import pytest

from nullrun.instrumentation.langgraph import NullRunCallback
from nullrun.toolbox.langgraph import wrapper


class _FakeApp:
    """Minimal compiled-LangGraph duck type: .invoke and .stream."""

    def __init__(self) -> None:
        self.invocations: list[dict] = []
        self.stream_calls: list[dict] = []

    def invoke(self, input, config=None, **kwargs):
        self.invocations.append({"input": input, "config": config, "kwargs": kwargs})
        # Echo the callbacks list so the test can inspect what wrapper added.
        return {"callbacks": (config or {}).get("callbacks", [])}

    def stream(self, input, config=None, **kwargs):
        self.stream_calls.append({"input": input, "config": config, "kwargs": kwargs})
        yield {"callbacks": (config or {}).get("callbacks", [])}


class _StubRuntime:
    """A no-network stand-in for NullRunRuntime that the wrapper
    can hand the callback without going through `get_runtime()`.

    `wrapper()` only needs an object that the `NullRunCallback`
    constructor accepts (it just stashes it as `self.runtime`).
    Real test isolation is in `test_langgraph_callback.py` /
    `test_protect.py`.
    """

    def __init__(self) -> None:
        self.track_calls: list[dict] = []


@pytest.fixture
def stub_runtime() -> _StubRuntime:
    return _StubRuntime()


def test_wrapper_returns_app(stub_runtime: _StubRuntime) -> None:
    """wrapper() must return the same app object (mutated in place)."""
    app = _FakeApp()
    out = wrapper(app, runtime=stub_runtime)
    assert out is app


def test_wrapper_attaches_callback_to_invoke(stub_runtime: _StubRuntime) -> None:
    """invoke() must have a NullRunCallback appended to config['callbacks']."""
    app = _FakeApp()
    wrapper(app, runtime=stub_runtime)
    app.invoke({"x": 1})
    callbacks = app.invocations[0]["config"]["callbacks"]
    assert any(isinstance(c, NullRunCallback) for c in callbacks)


def test_wrapper_attaches_callback_to_stream(stub_runtime: _StubRuntime) -> None:
    """stream() must also get a NullRunCallback in config['callbacks']."""
    app = _FakeApp()
    wrapper(app, runtime=stub_runtime)
    list(app.stream({"x": 1}))
    callbacks = app.stream_calls[0]["config"]["callbacks"]
    assert any(isinstance(c, NullRunCallback) for c in callbacks)


def test_wrapper_preserves_user_callbacks(stub_runtime: _StubRuntime) -> None:
    """If the caller already supplied callbacks, wrapper appends to them."""
    app = _FakeApp()
    wrapper(app, runtime=stub_runtime)
    user_cb: Any = object()
    app.invoke({"x": 1}, config={"callbacks": [user_cb]})
    callbacks = app.invocations[0]["config"]["callbacks"]
    assert user_cb in callbacks
    assert any(isinstance(c, NullRunCallback) for c in callbacks)


def test_wrapper_handles_no_config_arg(stub_runtime: _StubRuntime) -> None:
    """invoke(input) without a config kwarg must still get a callbacks list."""
    app = _FakeApp()
    wrapper(app, runtime=stub_runtime)
    app.invoke({"x": 1})
    config = app.invocations[0]["config"]
    assert config is not None
    assert "callbacks" in config


def test_old_instrument_path_is_removed() -> None:
    """`nullrun.instrumentation.langgraph.instrument` no longer exists."""
    import nullrun.instrumentation.langgraph as mod
    assert not hasattr(mod, "instrument"), (
        "Phase 1 Commit 6: `instrument` should be removed; "
        "use `nullrun.toolbox.langgraph.wrapper` instead."
    )
