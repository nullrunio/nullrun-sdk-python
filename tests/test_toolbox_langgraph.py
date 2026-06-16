"""Tests for the toolbox.langgraph.wrapper helper.

The wrapper monkey-patches a compiled LangGraph app's
`invoke` and `stream` methods to attach a `NullRunCallback` so
the runtime sees LLM usage. These tests verify the wiring
without requiring an actual LangChain/LangGraph runtime —
we just need a duck-typed object with `.invoke` and `.stream`.
"""
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


def test_wrapper_returns_app():
    """wrapper() must return the same app object (mutated in place)."""
    app = _FakeApp()
    out = wrapper(app)
    assert out is app


def test_wrapper_attaches_callback_to_invoke():
    """invoke() must have a NullRunCallback appended to config['callbacks']."""
    app = _FakeApp()
    wrapper(app)
    app.invoke({"x": 1})
    callbacks = app.invocations[0]["config"]["callbacks"]
    assert any(isinstance(c, NullRunCallback) for c in callbacks)


def test_wrapper_attaches_callback_to_stream():
    """stream() must also get a NullRunCallback in config['callbacks']."""
    app = _FakeApp()
    wrapper(app)
    list(app.stream({"x": 1}))
    callbacks = app.stream_calls[0]["config"]["callbacks"]
    assert any(isinstance(c, NullRunCallback) for c in callbacks)


def test_wrapper_preserves_user_callbacks():
    """If the caller already supplied callbacks, wrapper appends to them."""
    app = _FakeApp()
    wrapper(app)
    user_cb = object()
    app.invoke({"x": 1}, config={"callbacks": [user_cb]})
    callbacks = app.invocations[0]["config"]["callbacks"]
    assert user_cb in callbacks
    assert any(isinstance(c, NullRunCallback) for c in callbacks)


def test_wrapper_handles_no_config_arg():
    """invoke(input) without a config kwarg must still get a callbacks list."""
    app = _FakeApp()
    wrapper(app)
    app.invoke({"x": 1})
    config = app.invocations[0]["config"]
    assert config is not None
    assert "callbacks" in config


def test_old_instrument_path_is_removed():
    """`nullrun.instrumentation.langgraph.instrument` no longer exists."""
    import nullrun.instrumentation.langgraph as mod
    assert not hasattr(mod, "instrument"), (
        "Phase 1 Commit 6: `instrument` should be removed; "
        "use `nullrun.toolbox.langgraph.wrapper` instead."
    )
