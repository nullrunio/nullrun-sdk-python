"""
SDK-6 — a refused ``/track`` must reach the application.

The SDK's own runtime raises the refusal correctly: ``runtime.track``
re-raises a ``NullRunDecision`` when the backend refuses to book the
money (ADR-008's table — the ``/track`` v3 single path is **CLOSED for
enforcement rejections**, so "a typed ``NullRunDecision``
(``CONSUME_OVERBUDGET`` 422, budget block 402) propagates to the
caller").

The auto-instrumentation layer then caught that refusal with a blanket
``except Exception`` and logged it at DEBUG. The application saw a
perfectly ordinary LLM call while the backend had just refused to book
it — a refused ``/track`` looked like a success.

What these tests pin:

1. A ``NullRunDecision`` raised by the runtime escapes EVERY
   auto-instrumented emission path: the httpx sync transport, the httpx
   async transport, the ``requests`` transport, and the OpenAI-Agents
   tracer.
2. The refusal the wire parser produces for a refused consume IS a
   ``NullRunDecision`` — the rule is written in the class the backend
   actually raises.
3. The fail-OPEN posture is UNCHANGED for everything else: a
   transport-level failure (a dead backend) is still swallowed at DEBUG
   and the caller still gets its response.
4. The refusal the caller sees is the SAME object the runtime raised —
   no laundering into a generic error on the way out.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from types import ModuleType, SimpleNamespace

import httpx
import pytest

from nullrun.breaker.exceptions import (
    NullRunBudgetError,
    NullRunConsumeOverbudgetError,
    NullRunDecision,
)
from nullrun.instrumentation.auto import (
    NullRunAsyncTransport,
    NullRunSyncTransport,
    _emit_from_agents_result,
)

# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _RefusingRuntime:
    """A runtime double whose ``track`` raises what the real runtime
    raises when the backend refuses the consume.

    ``raise_exc`` is raised AS IS — the test asserts the caller sees the
    same object, not a copy.
    """

    def __init__(self, raise_exc: BaseException) -> None:
        self.raise_exc = raise_exc
        self.tracked: list[dict] = []

    def check_workflow_budget(self, **kwargs):
        # No reservation: the pre-flight is not what this file is about.
        return None

    def track(self, event, *, reservation_id=None):
        self.tracked.append(event)
        raise self.raise_exc

    def track_event(self, **kwargs):
        # Span emission — not money, and never a refusal.
        return None


class _RecordingTransport(httpx.BaseTransport):
    """An inner transport that answers with a canned OpenAI-shaped body."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.sent: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        return httpx.Response(self.status, content=self.body, request=request)


class _RecordingAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.sent: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        return httpx.Response(self.status, content=self.body, request=request)


def _openai_body() -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-1",
            "model": "gpt-4o-mini",
            "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
            "choices": [{"message": {"role": "assistant", "content": "hi"}}],
        }
    ).encode()


def _request() -> httpx.Request:
    return httpx.Request(
        "POST",
        "https://api.openai.com/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]},
        headers={"Content-Type": "application/json"},
    )


def _budget_refusal() -> NullRunBudgetError:
    """The refusal the backend sends as a 402 budget block."""
    return NullRunBudgetError("wf-1", "period budget exhausted", status_code=402)


# ---------------------------------------------------------------------------
# 1. The httpx sync path
# ---------------------------------------------------------------------------


class TestTheHttpxSyncPath:
    def test_a_refused_track_reaches_the_caller(self):
        """The refusal must NOT be swallowed by the transport wrapper.

        Before the fix the wrapper caught it and logged it at DEBUG, so
        ``handle_request`` returned a normal response and the
        application never learned the backend refused to book the call.
        """
        refusal = _budget_refusal()
        runtime = _RefusingRuntime(refusal)
        transport = NullRunSyncTransport(_RecordingTransport(_openai_body()), runtime)

        with pytest.raises(NullRunBudgetError) as excinfo:
            transport.handle_request(_request())

        assert excinfo.value is refusal
        assert runtime.tracked, "the cost event must have been emitted"

    def test_a_successful_track_still_returns_the_response(self):
        """The control: no refusal ⇒ the caller gets its response and the
        body is intact."""
        runtime = _RefusingRuntime(_budget_refusal())
        runtime.track = lambda event, reservation_id=None: {"ok": True}  # type: ignore[method-assign]
        transport = NullRunSyncTransport(_RecordingTransport(_openai_body()), runtime)

        response = transport.handle_request(_request())

        assert response.status_code == 200
        assert json.loads(response.content)["id"] == "chatcmpl-1"

    def test_a_transport_failure_is_still_swallowed(self):
        """The fail-OPEN posture is unchanged: a DEAD backend must not
        freeze the agent, so a non-decision failure stays swallowed."""
        runtime = _RefusingRuntime(RuntimeError("backend is down"))
        transport = NullRunSyncTransport(_RecordingTransport(_openai_body()), runtime)

        response = transport.handle_request(_request())

        assert response.status_code == 200


# ---------------------------------------------------------------------------
# 2. The httpx async path
# ---------------------------------------------------------------------------


class TestTheHttpxAsyncPath:
    def test_a_refused_track_reaches_the_caller(self):
        refusal = _budget_refusal()
        runtime = _RefusingRuntime(refusal)
        transport = NullRunAsyncTransport(_RecordingAsyncTransport(_openai_body()), runtime)

        async def _drive():
            await transport.handle_async_request(_request())

        with pytest.raises(NullRunBudgetError) as excinfo:
            asyncio.run(_drive())

        assert excinfo.value is refusal

    def test_a_transport_failure_is_still_swallowed(self):
        runtime = _RefusingRuntime(RuntimeError("backend is down"))
        transport = NullRunAsyncTransport(_RecordingAsyncTransport(_openai_body()), runtime)

        async def _drive():
            return await transport.handle_async_request(_request())

        response = asyncio.run(_drive())
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# 3. The requests path
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_patch_module():
    if "nullrun.instrumentation.auto_requests" in sys.modules:
        importlib.reload(sys.modules["nullrun.instrumentation.auto_requests"])
    else:
        importlib.import_module("nullrun.instrumentation.auto_requests")
    yield
    if "nullrun.instrumentation.auto_requests" in sys.modules:
        importlib.reload(sys.modules["nullrun.instrumentation.auto_requests"])


def _install_fake_requests(monkeypatch) -> None:
    """Install a fake ``requests`` module whose ``Session.send`` answers
    with an OpenAI-shaped body."""

    class _FakeResponse:
        def __init__(self, body: bytes, status_code: int):
            self.content = body
            self.status_code = status_code
            self.headers = {"Content-Type": "application/json"}

    class _FakeSession:
        send_count = 0
        _nullrun_patched = False

        @staticmethod
        def send(self_or_cls, request, **kwargs):
            _FakeSession.send_count += 1
            return _FakeResponse(_openai_body(), 200)

    fake_mod = ModuleType("requests")
    fake_mod.Session = _FakeSession
    monkeypatch.setitem(sys.modules, "requests", fake_mod)


class TestTheRequestsPath:
    def test_a_refused_track_reaches_the_caller(self, monkeypatch, fresh_patch_module):
        _install_fake_requests(monkeypatch)
        refusal = _budget_refusal()
        runtime = _RefusingRuntime(refusal)

        from requests import Session

        from nullrun.instrumentation.auto_requests import patch_requests

        assert patch_requests(runtime) is True
        req = SimpleNamespace(
            url="https://api.openai.com/v1/chat/completions",
            headers={},
            _nullrun_tracked=False,
        )

        with pytest.raises(NullRunBudgetError) as excinfo:
            Session().send(req)

        assert excinfo.value is refusal

    def test_a_transport_failure_is_still_swallowed(self, monkeypatch, fresh_patch_module):
        """The pre-existing swallow for a non-decision failure stays."""
        _install_fake_requests(monkeypatch)
        runtime = _RefusingRuntime(RuntimeError("down"))

        from requests import Session

        from nullrun.instrumentation.auto_requests import patch_requests

        assert patch_requests(runtime) is True
        req = SimpleNamespace(url="https://api.openai.com/v1/chat/completions", headers={})

        resp = Session().send(req)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 4. The OpenAI-Agents tracer
# ---------------------------------------------------------------------------


class TestTheAgentsTracer:
    def test_a_refused_track_reaches_the_caller(self):
        refusal = _budget_refusal()
        runtime = _RefusingRuntime(refusal)

        result = SimpleNamespace(
            _trace_spans=[
                {
                    "type": "llm_call",
                    "id": "span-1",
                    "model": "gpt-4o-mini",
                    "usage": {
                        "input_tokens": 11,
                        "output_tokens": 4,
                        "total_tokens": 15,
                    },
                }
            ]
        )

        with pytest.raises(NullRunBudgetError) as excinfo:
            _emit_from_agents_result(runtime, result)

        assert excinfo.value is refusal


# ---------------------------------------------------------------------------
# 5. The refusal the wire produces IS a decision
# ---------------------------------------------------------------------------


class TestTheWireRefusalIsADecision:
    def test_a_refused_consume_is_a_decision(self):
        """The rule is written in ``NullRunDecision`` — so the class the
        BACKEND produces must be one.

        The 422 CONSUME_OVERBUDGET envelope is parsed by the real
        transport and must land as a ``NullRunDecision``.
        """
        from nullrun.transport import _parse_v3_error_envelope

        response = httpx.Response(
            422,
            json={
                "error_code": "CONSUME_OVERBUDGET",
                "error_message": "actual 12 > reserved 10 + 1",
                "details": {
                    "execution_id": "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0000",
                    "reserved_millicents": 10_000,
                    "max_allowed_millicents": 11_000,
                    "actual_cost_cents": 12,
                    "reservation_recorded": True,
                },
            },
            request=httpx.Request("POST", "https://api.nullrun.io/api/v1/track"),
        )

        exc = _parse_v3_error_envelope(response, "track")

        assert isinstance(exc, NullRunConsumeOverbudgetError)
        assert isinstance(exc, NullRunDecision), (
            "the enforcement refusal must be a NullRunDecision — the "
            "class the instrumentation rule is written in"
        )


# ---------------------------------------------------------------------------
# 6. The framework integrations the auto-instrumentation layer installs
# ---------------------------------------------------------------------------
#
# ``patch_all()`` installs the httpx transport, the requests transport,
# the OpenAI-Agents tracer AND the crewai / autogen / llama_index /
# langchain integrations. A refusal swallowed in ANY of them is the same
# defect the audit named: the backend refused to book the money and the
# application was told the call succeeded.
#
# The doubles below install SYNTHETIC vendor modules, so the real
# wrapper code runs without the (heavy, optional) dependencies.


def _reload(module_name: str) -> None:
    if module_name in sys.modules:
        importlib.reload(sys.modules[module_name])
    else:
        importlib.import_module(module_name)


@pytest.fixture
def fresh_crewai():
    _reload("nullrun.instrumentation.crewai")
    yield
    _reload("nullrun.instrumentation.crewai")


@pytest.fixture
def fresh_autogen():
    _reload("nullrun.instrumentation.autogen")
    yield
    _reload("nullrun.instrumentation.autogen")


@pytest.fixture
def fresh_llama_index():
    _reload("nullrun.instrumentation.llama_index")
    yield
    _reload("nullrun.instrumentation.llama_index")


def _install_fake_crewai(monkeypatch) -> None:
    class _FakeCrew:
        _nullrun_patched = False
        usage_metrics: dict = {}

        @staticmethod
        def kickoff(self, inputs=None, **kwargs):
            return SimpleNamespace(result="ok")

    fake_mod = ModuleType("crewai")
    fake_mod.Crew = _FakeCrew
    monkeypatch.setitem(sys.modules, "crewai", fake_mod)


def _install_fake_autogen(monkeypatch) -> None:
    class _Usage:
        prompt_tokens = 12
        completion_tokens = 34
        total_tokens = 46

    class _Result:
        usage = _Usage()

    class _FakeOpenAIChatCompletionClient:
        _nullrun_patched = False

        @staticmethod
        def create(self, *args, **kwargs):
            return _Result()

    class _FakeBaseChatAgent:
        _nullrun_patched = False

        def on_messages(self, messages, cancellation_token=None):
            return SimpleNamespace(content="ok")

    fake_agents_mod = ModuleType("autogen_agentchat.agents")
    fake_agents_mod.BaseChatAgent = _FakeBaseChatAgent
    monkeypatch.setitem(sys.modules, "autogen_agentchat", ModuleType("autogen_agentchat"))
    monkeypatch.setitem(sys.modules, "autogen_agentchat.agents", fake_agents_mod)

    fake_ext_mod = ModuleType("autogen_ext.models.openai")
    fake_ext_mod.OpenAIChatCompletionClient = _FakeOpenAIChatCompletionClient
    monkeypatch.setitem(sys.modules, "autogen_ext", ModuleType("autogen_ext"))
    monkeypatch.setitem(sys.modules, "autogen_ext.models", ModuleType("autogen_ext.models"))
    monkeypatch.setitem(sys.modules, "autogen_ext.models.openai", fake_ext_mod)


def _install_fake_llama_index(monkeypatch) -> list:
    captured: list = []

    class _FakeDispatcher:
        def add_event_handler(self, event_cls, handler):
            captured.append((event_cls, handler))

        def remove_event_handler(self, event_cls, handler):
            for i, (cls, h) in enumerate(captured):
                if cls is event_cls and h is handler:
                    del captured[i]
                    return

    events_mod = ModuleType("llama_index.core.instrumentation.events")
    events_mod_llm = ModuleType("llama_index.core.instrumentation.events.llm")
    events_mod_llm.LLMChatEndEvent = type("LLMChatEndEvent", (), {})
    events_mod_tool = ModuleType("llama_index.core.instrumentation.events.tool")
    events_mod_tool.FunctionCallEvent = type("FunctionCallEvent", (), {})
    events_mod.llm = events_mod_llm
    events_mod.tool = events_mod_tool

    inst_mod = ModuleType("llama_index.core.instrumentation")
    inst_mod.get_dispatcher = lambda *a, **k: _FakeDispatcher()
    monkeypatch.setitem(sys.modules, "llama_index", ModuleType("llama_index"))
    monkeypatch.setitem(sys.modules, "llama_index.core", ModuleType("llama_index.core"))
    monkeypatch.setitem(sys.modules, "llama_index.core.instrumentation", inst_mod)
    monkeypatch.setitem(sys.modules, "llama_index.core.instrumentation.events", events_mod)
    monkeypatch.setitem(sys.modules, "llama_index.core.instrumentation.events.llm", events_mod_llm)
    monkeypatch.setitem(
        sys.modules, "llama_index.core.instrumentation.events.tool", events_mod_tool
    )
    return captured


class TestTheCrewAiPath:
    def test_a_refused_track_reaches_the_caller(self, monkeypatch, fresh_crewai):
        _install_fake_crewai(monkeypatch)
        refusal = _budget_refusal()
        runtime = _RefusingRuntime(refusal)

        from crewai import Crew

        from nullrun.instrumentation.crewai import patch_crewai

        assert patch_crewai(runtime) is True

        crew = Crew()
        crew.usage_metrics = {
            "gpt-4o": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        }

        with pytest.raises(NullRunBudgetError) as excinfo:
            Crew.kickoff(crew, inputs={"q": "hi"})

        assert excinfo.value is refusal

    def test_a_transport_failure_is_still_swallowed(self, monkeypatch, fresh_crewai):
        _install_fake_crewai(monkeypatch)
        runtime = _RefusingRuntime(RuntimeError("backend is down"))

        from crewai import Crew

        from nullrun.instrumentation.crewai import patch_crewai

        assert patch_crewai(runtime) is True
        crew = Crew()
        crew.usage_metrics = {
            "gpt-4o": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        }

        result = Crew.kickoff(crew, inputs={"q": "hi"})
        assert result.result == "ok"


class TestTheAutogenPath:
    def test_a_refused_track_reaches_the_caller(self, monkeypatch, fresh_autogen):
        _install_fake_autogen(monkeypatch)
        refusal = _budget_refusal()
        runtime = _RefusingRuntime(refusal)

        from autogen_ext.models.openai import OpenAIChatCompletionClient

        from nullrun.instrumentation.autogen import patch_autogen

        assert patch_autogen(runtime) is True

        class _Inst:
            model = "gpt-4o-mini"

        with pytest.raises(NullRunBudgetError) as excinfo:
            OpenAIChatCompletionClient.create(_Inst())

        assert excinfo.value is refusal

    def test_a_transport_failure_is_still_swallowed(self, monkeypatch, fresh_autogen):
        _install_fake_autogen(monkeypatch)
        runtime = _RefusingRuntime(RuntimeError("backend is down"))

        from autogen_ext.models.openai import OpenAIChatCompletionClient

        from nullrun.instrumentation.autogen import patch_autogen

        assert patch_autogen(runtime) is True

        class _Inst:
            model = "gpt-4o-mini"

        result = OpenAIChatCompletionClient.create(_Inst())
        assert result.usage.prompt_tokens == 12


class TestTheLlamaIndexPath:
    def test_a_refused_track_reaches_the_caller(self, monkeypatch, fresh_llama_index):
        captured = _install_fake_llama_index(monkeypatch)
        refusal = _budget_refusal()
        runtime = _RefusingRuntime(refusal)

        from nullrun.instrumentation.llama_index import patch_llama_index

        assert patch_llama_index(runtime) is True

        import llama_index.core.instrumentation.events.llm as _llm_events

        response = SimpleNamespace(
            raw={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            model="gpt-4o",
        )
        event = SimpleNamespace(response=response)

        fired = False
        for cls, handler in captured:
            if cls is _llm_events.LLMChatEndEvent:
                with pytest.raises(NullRunBudgetError) as excinfo:
                    handler(event)
                assert excinfo.value is refusal
                fired = True
                break
        assert fired, "the LLMChatEndEvent handler was not registered"

    def test_a_transport_failure_is_still_swallowed(self, monkeypatch, fresh_llama_index):
        captured = _install_fake_llama_index(monkeypatch)
        runtime = _RefusingRuntime(RuntimeError("backend is down"))

        from nullrun.instrumentation.llama_index import patch_llama_index

        assert patch_llama_index(runtime) is True

        import llama_index.core.instrumentation.events.llm as _llm_events

        response = SimpleNamespace(
            raw={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            model="gpt-4o",
        )
        event = SimpleNamespace(response=response)

        for cls, handler in captured:
            if cls is _llm_events.LLMChatEndEvent:
                handler(event)  # must not raise
                break


class TestTheLangChainPath:
    """The LangChain callback's /track emission.

    ``on_llm_end`` used to swallow the refusal in a blanket
    ``except Exception`` INSIDE the method — so the refusal never
    reached ``@_only_a_gate_refusal_escapes`` and the SDK-4 machinery
    could not do its job.
    """

    def test_a_refused_track_reaches_the_caller(self):
        from nullrun.instrumentation.langgraph import NullRunCallback

        refusal = _budget_refusal()
        runtime = _RefusingRuntime(refusal)
        cb = NullRunCallback(runtime=runtime)

        with pytest.raises(NullRunBudgetError) as excinfo:
            cb.on_llm_end(
                SimpleNamespace(
                    usage_metadata={
                        "input_tokens": 5,
                        "output_tokens": 10,
                        "total_tokens": 15,
                    }
                ),
                invocation_params={"model_name": "gpt-4o", "model_provider": "openai"},
            )

        assert excinfo.value is refusal

    def test_the_refusal_is_also_recorded_for_the_protect_boundary(self):
        """The secondary channel: a host framework that ignores
        ``raise_error`` still gets the decision at the ``@protect``
        boundary."""
        from nullrun.instrumentation.langgraph import (
            NullRunCallback,
            drain_deferred_enforcement,
        )

        drain_deferred_enforcement()
        runtime = _RefusingRuntime(_budget_refusal())
        cb = NullRunCallback(runtime=runtime)

        with pytest.raises(NullRunBudgetError):
            cb.on_llm_end(
                SimpleNamespace(usage_metadata={"total_tokens": 15}),
                invocation_params={"model_name": "gpt-4o"},
            )

        deferred = drain_deferred_enforcement()
        assert isinstance(deferred, NullRunBudgetError)

    def test_a_transport_failure_is_still_swallowed(self):
        from nullrun.instrumentation.langgraph import NullRunCallback

        runtime = _RefusingRuntime(RuntimeError("backend is down"))
        cb = NullRunCallback(runtime=runtime)

        cb.on_llm_end(
            SimpleNamespace(usage_metadata={"total_tokens": 15}),
            invocation_params={"model_name": "gpt-4o"},
        )  # must not raise


# ---------------------------------------------------------------------------
# 7. End to end: the REAL runtime behind the REAL transport
# ---------------------------------------------------------------------------
#
# The two halves are proven separately — the runtime re-raises the
# refusal (``tests/test_route_track_enforcement_propagates.py`` drives
# the real runtime against a mocked wire) and the transport does not
# swallow it (the classes above, with a double runtime).
#
# This joins them, because the seam IS the defect: a real
# ``NullRunRuntime`` behind a real ``NullRunSyncTransport``, with the
# backend mocked at the wire, must surface the refusal to the
# application. A source-pin cannot prove that — the whole defect is
# "what the transport does with what the runtime raised".

CONSUMED_TOO_MUCH = {
    "error_code": "CONSUME_OVERBUDGET",
    "error_message": "actual > reserved + epsilon",
    "details": {
        "execution_id": "0190c5b5-7c9a-7def-8a1b-0123456789ab",
        "reserved_millicents": 1000,
        "max_allowed_millicents": 1010,
        "actual_cost_cents": 150,
        "soft_pass": False,
        "reservation_recorded": True,
    },
}


class TestTheEndToEndPath:
    def test_a_refused_track_reaches_the_caller(self, make_runtime):
        import respx
        from httpx import Response

        from nullrun.instrumentation.auto import NullRunSyncTransport
        from nullrun.runtime import _capture_server_minted_execution_id

        rt = make_runtime()
        # Bind a server-minted id so ``_route_track`` reaches
        # ``track_single`` instead of dropping on "no reservation".
        _capture_server_minted_execution_id({"reservation_id": "0190c5b5-7c9a-7def-8a1b-0123456789ab"})

        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=Response(200, json=json.loads(_openai_body()))
        )
        respx.post("https://api.test.nullrun.io/api/v1/track").mock(
            return_value=Response(422, json=CONSUMED_TOO_MUCH)
        )

        transport = NullRunSyncTransport(_RecordingTransport(_openai_body()), rt)

        with pytest.raises(NullRunConsumeOverbudgetError) as excinfo:
            transport.handle_request(_request())

        assert excinfo.value.status_code == 422
        assert excinfo.value.actual_cost_cents == 150

    def test_a_transport_failure_is_still_swallowed(self, make_runtime):
        """The fail-OPEN half, end to end: a dead backend must not
        freeze the agent."""
        import respx
        from httpx import Response

        from nullrun.instrumentation.auto import NullRunSyncTransport
        from nullrun.runtime import _capture_server_minted_execution_id

        rt = make_runtime()
        _capture_server_minted_execution_id({"reservation_id": "0190c5b5-7c9a-7def-8a1b-0123456789ab"})

        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=Response(200, json=json.loads(_openai_body()))
        )
        respx.post("https://api.test.nullrun.io/api/v1/track").mock(
            return_value=Response(500, json={"error_code": "INTERNAL", "error_message": "boom"})
        )

        transport = NullRunSyncTransport(_RecordingTransport(_openai_body()), rt)

        response = transport.handle_request(_request())

        assert response.status_code == 200
