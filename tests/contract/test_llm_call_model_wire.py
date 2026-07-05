"""
Regression test for the silent zero-billing bug (2026-06-29).

Pre-fix: when an ``llm_call`` event reached the runtime's ``track ``
with ``model=None`` (or absent), the wire-format builder at
``runtime.py:1427-1431`` dropped the None value entirely, the
backend's cost pipeline ``unwrap_or("default")``'d, and every call
was recorded as approximately zero. Budget enforcement, billing
and plan-limit accounting silently broke for every model on every
provider.

Post-fix: three layers of defense

  1. ``_extract_model_from_response`` (langgraph.py) now finds the
     model on every known response shape — including
     ``LLMResult.llm_output['model_name']``, the location
     langchain-openai 1.x uses for the date-suffixed id.
  2. ``runtime.track `` promotes the missing-model warning to
     ERROR, bumps ``dropped_llm_call_no_model``, and tags the
     wire event with ``__missing_model: True`` so the backend
     can reject with HTTP 422.
  3. ``patch_httpx`` eagerly wraps any pre-existing httpx.Client
     instances when ``nullrun.init `` is called — closing the
     init-ordering hazard where ``ChatOpenAI(...)`` is created
     before ``init ``.

This file pins all three invariants at the unit level so a future
refactor can't silently re-break the wire.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nullrun.instrumentation.langgraph import _extract_model_from_response


# ─── _extract_model_from_response: the actual fix ─────────────────────
#
# The chain was promoted so the langchain-openai 1.x primary
# location (``LLMResult.llm_output['model_name']``) is checked
# FIRST. Pre-fix this location was step 3, after the AIMessage
# ``response_metadata`` step that langchain 1.x does not populate
# — so every OpenAI call returned None from extraction and was
# silently zero-billed.


def _make_llmresult(
    *,
    response_metadata=None,
    generations=None,
    llm_output=None,
    direct_model=None,
):
    """Build a minimal LLMResult-like object for the helper to walk."""
    return SimpleNamespace(
        response_metadata=response_metadata,
        generations=generations or [],
        llm_output=llm_output,
        model=direct_model,
        model_name=direct_model,
    )


def test_extracts_from_llm_output_model_name_langchain_openai_1x():
    """langchain-openai 1.x primary location. The date-suffixed id
    ``gpt-4.1-mini-2025-04-14`` lives here. The backend's
    ``MODEL_RATES`` substring-match resolves it to the
    ``gpt-4.1-mini`` rate."""
    response = _make_llmresult(
        llm_output={"model_name": "gpt-4.1-mini-2025-04-14", "token_usage": {}}
    )
    assert _extract_model_from_response(response) == "gpt-4.1-mini-2025-04-14"


def test_extracts_from_llm_output_model_key():
    """Some OpenAI-compatible proxies put the model on
    ``llm_output['model']`` (no ``_name`` suffix)."""
    response = _make_llmresult(llm_output={"model": "gpt-4.1-mini"})
    assert _extract_model_from_response(response) == "gpt-4.1-mini"


def test_extracts_from_llm_output_key_containing_model():
    """Custom wrappers (e.g. ``model_id``, ``modelName``
    ``resolved_model``) fall through the generic any-key
    sweep."""
    response = _make_llmresult(llm_output={"model_id": "claude-haiku-4-5-20251001"})
    assert _extract_model_from_response(response) == "claude-haiku-4-5-20251001"


def test_llm_output_checked_before_response_metadata():
    """Audit invariant: when BOTH ``llm_output['model_name']`` and
    ``response_metadata['model_name']`` are set, the llm_output
    value wins. Pre-fix the order was response_metadata first
    which meant a populated response_metadata shadowed the
    real (date-suffixed) llm_output value."""
    response = _make_llmresult(
        response_metadata={"model_name": "stale-alias"},
        llm_output={"model_name": "gpt-4.1-mini-2025-04-14"},
    )
    assert _extract_model_from_response(response) == "gpt-4.1-mini-2025-04-14"


def test_falls_through_to_response_metadata_when_llm_output_empty():
    """langchain 0.x and direct AIMessage paths still work."""
    response = _make_llmresult(
        response_metadata={"model_name": "gpt-4o-mini"},
    )
    assert _extract_model_from_response(response) == "gpt-4o-mini"


def test_falls_through_to_generations_message_metadata():
    """LLMResult where metadata lives on the AIMessage inside
    ``generations[0][0].message`` rather than the LLMResult itself."""
    msg = SimpleNamespace(
        response_metadata={"model_name": "claude-3-5-sonnet-20240620"},
    )
    response = _make_llmresult(generations=[[SimpleNamespace(message=msg)]])
    assert _extract_model_from_response(response) == "claude-3-5-sonnet-20240620"


def test_returns_none_when_all_sources_empty(caplog):
    """When every known source is empty/missing, extraction returns
    None — but now logs a DEBUG line so the operator can correlate
    the wire warning back to the observation site."""
    response = _make_llmresult()
    with caplog.at_level(logging.DEBUG, logger="nullrun.instrumentation.langgraph"):
        result = _extract_model_from_response(response)
    assert result is None
    # The DEBUG line is for forensics; the runtime layer is the
    # one that bumps the error log + counter.
    assert any(
        "_extract_model_from_response returned None" in record.message
        for record in caplog.records
    )


def test_empty_string_in_llm_output_falls_through():
    """``llm_output['model_name'] = ''`` is treated as empty and
    the helper moves on to the next source rather than returning
    the empty string. Pre-fix this would have shipped ``model=''``
    on the wire, which the backend would still fall through on
    but the SDK would log a misleading warning."""
    response = _make_llmresult(
        llm_output={"model_name": ""},
        response_metadata={"model_name": "gpt-4.1-mini"},
    )
    assert _extract_model_from_response(response) == "gpt-4.1-mini"


# ─── track fail-loud behavior ──────────────────────────────────────
#
# The runtime layer is the front door for the wire. Pre-fix it
# warned at WARN and continued; the backend then silently
# zero-billed. Post-fix it logs at ERROR, bumps a counter, and
# tags the event with ``__missing_model: True`` so the backend
# can reject with HTTP 422.


def test_track_promotes_missing_model_to_error_and_tags_event(make_runtime, caplog):
    """Regression: an ``llm_call`` event with ``model=None`` reaches
    ``track `` and (a) is logged at ERROR, (b) gets the
    ``__missing_model: True`` flag, (c) is still sent on the wire
    so the backend can reject with HTTP 422 (not silently free)."""
    rt = make_runtime()
    captured = []

    # Capture what the transport would send on the wire.
    def _capture_track(event):
        captured.append(event)

    rt._transport.track = _capture_track

    with caplog.at_level(logging.ERROR, logger="nullrun.runtime"):
        rt.track({"type": "llm_call", "tokens": 100, "model": None})

    # The wire event IS sent (so the backend can audit/reject).
    assert len(captured) == 1
    wire = captured[0]
    assert wire["type"] == "llm_call"
    # __missing_model: True is the signal to the backend gate.
    assert wire.get("__missing_model") is True
    # model is absent from the wire (None values are still dropped
    # at runtime.py:1427-1431 — that filter is correct; the flag
    # is the substitute for the field).
    assert "model" not in wire
    # An ERROR log line was emitted.
    assert any(
        "llm_call event missing 'model' field" in record.message
        for record in caplog.records
        if record.levelno == logging.ERROR
    )


def test_track_does_not_tag_when_model_is_set(make_runtime):
    """The happy path: ``llm_call`` event with a model passes
    through unchanged (no ERROR, no __missing_model flag)."""
    rt = make_runtime()
    captured = []
    rt._transport.track = lambda e: captured.append(e)

    rt.track({"type": "llm_call", "tokens": 100, "model": "gpt-4.1-mini"})

    assert len(captured) == 1
    wire = captured[0]
    assert wire.get("model") == "gpt-4.1-mini"
    assert "__missing_model" not in wire


def test_track_does_not_tag_non_llm_call_events_with_missing_model(make_runtime):
    """``span_start`` / ``span_end`` / ``tool_call`` events do not
    carry a model field by design. The fail-loud path must not
    fire for them or every span emission would log an error."""
    rt = make_runtime()
    captured = []
    rt._transport.track = lambda e: captured.append(e)

    rt.track({"type": "span_start", "fn_name": "foo"})
    rt.track({"type": "span_end", "fn_name": "foo"})

    # No __missing_model flag on either event.
    assert all("__missing_model" not in e for e in captured)
    assert len(captured) == 2


# ─── patch_httpx: eager wrap of pre-existing clients ────────────────
#
# Pre-fix the class-level patch on ``httpx.Client.__init__`` only
# wrapped clients created AFTER ``nullrun.init `` ran. The user's
# script (and many real codebases) does
#
# llm = ChatOpenAI(model=...) # before init
# nullrun.init(api_key=...) # patch installed too late
#
# which left ``llm``'s internal httpx.Client unpatched. Post-fix
# the patch sweep finds and wraps pre-existing clients.


def test_patch_httpx_wraps_pre_existing_clients():
    """When ``patch_httpx`` runs and there are pre-existing
    ``httpx.Client`` instances in the process, the new sweep
    finds them and wraps their transports in
    ``NullRunSyncTransport``.

    The test builds a real ``httpx.Client`` (the user's
    ``ChatOpenAI`` shape), runs the patch, and asserts the
    transport was rewritten.
    """
    import httpx

    from nullrun.instrumentation import auto
    from nullrun.instrumentation.auto import NullRunSyncTransport

    # The patch is process-global; reset so we start clean.
    auto.reset_for_tests()
    auto._httpx_patched = False

    # Build a real client BEFORE the patch — the user's order.
    pre_existing = httpx.Client()
    # Sanity: default transport is not yet wrapped.
    assert not isinstance(pre_existing._transport, NullRunSyncTransport)

    runtime = MagicMock()
    try:
        ok = auto.patch_httpx(runtime)
        assert ok is True

        # The sweep should have found the pre-existing client and
        # wrapped it.
        assert isinstance(pre_existing._transport, NullRunSyncTransport)

        # New clients after the patch are also wrapped (class-level
        # patch on ``__init__``).
        post = httpx.Client()
        try:
            assert isinstance(post._transport, NullRunSyncTransport)
        finally:
            post.close()
    finally:
        # Don't leave the patched state for the next test.
        auto.reset_for_tests()
        pre_existing.close()


def test_patch_httpx_eager_wrap_is_idempotent():
    """Running the eager sweep twice must not double-wrap the same
    client. The check is on the existing transport type, not on
    a separate marker, so a no-op re-run leaves the client
    untouched."""
    import httpx

    from nullrun.instrumentation import auto
    from nullrun.instrumentation.auto import NullRunSyncTransport

    auto.reset_for_tests()
    auto._httpx_patched = False

    pre_existing = httpx.Client()
    runtime = MagicMock()
    try:
        auto.patch_httpx(runtime)
        wrapped_transport = pre_existing._transport
        assert isinstance(wrapped_transport, NullRunSyncTransport)

        # Reset the patch flag and call again — simulates a
        # double-init (e.g. test fixtures). The sweep must NOT
        # wrap the already-wrapped transport a second time.
        auto._httpx_patched = False
        # The class-level patch marker (``_nullrun_patched``) is
        # still set on httpx.Client so the second ``patch_httpx``
        # short-circuits — this is the expected path. Verify the
        # transport object is the SAME instance (no double-wrap).
        auto.patch_httpx(runtime)
        assert pre_existing._transport is wrapped_transport
    finally:
        auto.reset_for_tests()
        pre_existing.close()


# ─── patch_chat_model_invoke: init-ordering regression ───────────────
#
# Audit 2026-06-29 (silent zero-billing): the LangGraph case the
# production trace exposed is
#
# llm = ChatOpenAI(model=...) # before init
# nullrun.init(api_key=...) # patch installed too late
# graph.invoke(input) # llm.invoke inside the node
#
# `patch_httpx` covers the eager-sweep path (pre-existing
# ``httpx.Client`` instances are wrapped). For LangChain chat models
# the `BaseCallbackManager.__init__` patch is the original defence.
# ``patch_chat_model_invoke`` is the new belt-and-suspenders layer
# that wraps ``BaseChatModel.invoke`` / ``ainvoke`` directly so a
# ``NullRunCallback`` is present in the per-call config even if the
# callback manager constructor is somehow bypassed.
#
# This regression test pins the wrap so a future refactor can't
# silently drop it.


def test_patch_chat_model_invoke_injects_callback_when_llm_pre_exists():
    """Create a fake BaseChatModel BEFORE ``nullrun.init``, then call
    ``patch_chat_model_invoke``, then invoke. The wrapped invoke must
    inject a ``NullRunCallback`` into the per-call config.
    """
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    from nullrun.instrumentation import auto
    from nullrun.instrumentation.langgraph import NullRunCallback

    auto.reset_for_tests()
    auto._chat_model_invoke_patched = False

    class FakeChatModel(BaseChatModel):
        """Minimal BaseChatModel that records the callbacks it saw."""

        seen_callbacks: list = []

        @property
        def _llm_type(self) -> str:
            return "fake"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            # Record the callbacks the framework attached (or didn't)
            # so the test can assert our wrapper injected one.
            seen = getattr(run_manager, "handlers", None) or []
            type(self).seen_callbacks.append(list(seen))
            # Return a properly-shaped ChatResult with an AIMessage so
            # the downstream on_llm_end extraction has a generations[0]
            # to walk. The model_name is the same string we'd see from
            # a real langchain-openai 1.x response.
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="ok",
                            response_metadata={"model_name": "fake-model"},
                        )
                    )
                ],
                llm_output={"model_name": "fake-model"},
            )

    runtime = MagicMock()
    try:
        # The user's order: create the LLM before init.
        llm = FakeChatModel()
        type(llm).seen_callbacks = []

        ok = auto.patch_chat_model_invoke(runtime)
        assert ok is True

        # Invoke the LLM through the wrapped method. The wrap must
        # inject a NullRunCallback into config["callbacks"] so the
        # internal _generate sees it.
        llm.invoke("hello")

        # Assert: at least one NullRunCallback was seen during _generate.
        saw_nullrun = any(
            any(isinstance(h, NullRunCallback) for h in seen)
            for seen in type(llm).seen_callbacks
        )
        assert saw_nullrun, (
            "patch_chat_model_invoke did not inject NullRunCallback into "
            "the per-call config — the audit fix is broken or missing."
        )
    finally:
        auto.reset_for_tests()


def test_patch_chat_model_invoke_preserves_user_callbacks():
    """If the user already supplied a callback in the config, the
    wrap must NOT replace it — only add the NullRunCallback if absent.
    """
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.callbacks import BaseCallbackHandler

    from nullrun.instrumentation import auto
    from nullrun.instrumentation.langgraph import NullRunCallback

    auto.reset_for_tests()
    auto._chat_model_invoke_patched = False

    class UserCallback(BaseCallbackHandler):
        """Real BaseCallbackHandler so LangChain's manager doesn't
        trip on missing attributes (``ignore_chat_model``
        ``raise_error``, etc.) when it tries to fire the callback."""
        seen_callbacks: list = []

        def on_chat_model_start(self, *args, **kwargs):
            type(self).seen_callbacks.append(("start",))

        def on_llm_end(self, *args, **kwargs):
            type(self).seen_callbacks.append(("end",))

    class FakeChatModel(BaseChatModel):
        seen_callbacks: list = []

        @property
        def _llm_type(self) -> str:
            return "fake"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            seen = getattr(run_manager, "handlers", None) or []
            type(self).seen_callbacks.append(list(seen))
            return ChatResult(
                generations=[
                    ChatGeneration(message=AIMessage(content="ok"))
                ],
                llm_output={"model_name": "fake-model"},
            )

    runtime = MagicMock()
    try:
        llm = FakeChatModel()
        type(llm).seen_callbacks = []
        ok = auto.patch_chat_model_invoke(runtime)
        assert ok is True

        # User already has their own callback in config.
        user_cb = UserCallback()
        llm.invoke("hello", config={"callbacks": [user_cb]})

        # The user's callback must still be there, alongside ours.
        seen_lists = type(llm).seen_callbacks
        assert any(user_cb in seen for seen in seen_lists), (
            "user-supplied callback was lost"
        )
        assert any(
            any(isinstance(h, NullRunCallback) for h in seen) for seen in seen_lists
        ), "NullRunCallback was not injected alongside user callback"
    finally:
        auto.reset_for_tests()
