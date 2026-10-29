"""
ADR-101 / NR-059 — the zero-code LLM path declares what the call will
cost, BEFORE the provider request leaves.

The finding this closes: on the zero-code path (httpx / requests
transport, no `@protect`, no `set_call_context`), the SDK only ever
emitted the `llm_call` event AFTER the provider had answered. The
event is the only thing that moves the budget counter, so a call that
the cap should have stopped was billed, not stopped. The control
centre's numbers were the sum of spend that had already happened.

What these tests pin:

1. The pre-flight (`check_workflow_budget` → `/gate`) runs BEFORE the
   provider request reaches the network.
2. A refusal (the cap says no) aborts the provider request — the
   request is never sent.
3. The reservation the gate minted travels with the cost event, so
   the backend settles THIS call's reserve and not another call's.
4. A reservation whose call never produced a usage event is RELEASED,
   so a failed call does not hold budget until the reclaim worker runs.
5. Two concurrent calls keep their own reservations — the contextvar
   carrier is one slot per task and would swap them.
6. The declared numbers are the call's own: the model, the estimated
   input size, and the caller's output cap. The gate cache is bypassed
   — a cached "allow" carries the PREVIOUS call's reservation.
"""

from __future__ import annotations

import json
import threading

import httpx
import pytest

from nullrun.breaker.exceptions import NullRunBudgetError
from nullrun.instrumentation.auto import (
    NullRunAsyncTransport,
    NullRunSyncTransport,
    _read_llm_request_profile,
)

# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _FakeRuntime:
    """A runtime double that records the ORDER of the wire calls.

    `check_workflow_budget` stands for the `/gate` pre-flight; `track`
    stands for `/track`. The order list is what proves the pre-flight
    happened before the provider request.
    """

    def __init__(
        self,
        reservation: str | None = "res-1",
        refuse: bool = False,
    ) -> None:
        self.order: list[str] = []
        self.reservation = reservation
        self.refuse = refuse
        self.gate_kwargs: dict | None = None
        self.tracked: list[tuple[dict, str | None]] = []
        self._counter = 0
        self._lock = threading.Lock()

    def check_workflow_budget(self, **kwargs):
        with self._lock:
            self._counter += 1
            index = self._counter
        self.order.append("gate")
        self.gate_kwargs = kwargs
        if self.refuse:
            raise NullRunBudgetError(
                "wf-1",
                "period budget exhausted",
                status_code=402,
            )
        if self.reservation is None:
            return None
        return f"{self.reservation}-{index}"

    def track(self, event, *, reservation_id=None):
        self.order.append("track")
        self.tracked.append((event, reservation_id))
        return {"ok": True}


class _RecordingTransport(httpx.BaseTransport):
    """An inner transport that records the requests it was asked to
    send. `fail=True` raises instead of answering.

    `barrier` forces the interleaving a concurrency test needs: every
    caller reaches the barrier only AFTER its pre-flight, so two
    threads always overlap between "reservation minted" and "cost
    event emitted" — exactly the window in which a single-slot
    carrier would swap the two reservations.
    """

    def __init__(
        self,
        body: bytes,
        status: int = 200,
        fail: bool = False,
        barrier: threading.Barrier | None = None,
    ) -> None:
        self.body = body
        self.status = status
        self.fail = fail
        self.barrier = barrier
        self.sent: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        if self.fail:
            raise httpx.ConnectError("provider unreachable")
        if self.barrier is not None:
            self.barrier.wait(timeout=10)
        return httpx.Response(self.status, content=self.body, request=request)


class _RecordingAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, body: bytes, status: int = 200, fail: bool = False) -> None:
        self.body = body
        self.status = status
        self.fail = fail
        self.sent: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        if self.fail:
            raise httpx.ConnectError("provider unreachable")
        return httpx.Response(self.status, content=self.body, request=request)


def _openai_body() -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-1",
            "model": "gpt-4o-mini",
            "usage": {
                "prompt_tokens": 8,
                "completion_tokens": 2,
                "total_tokens": 10,
            },
        }
    ).encode()


def _request(
    body: dict | None = None,
    url: str = "https://api.openai.com/v1/chat/completions",
) -> httpx.Request:
    payload = body if body is not None else {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "hello"}],
    }
    return httpx.Request(
        "POST",
        url,
        json=payload,
        headers={"Content-Type": "application/json"},
    )


# ---------------------------------------------------------------------------
# 1. The pre-flight runs BEFORE the provider request
# ---------------------------------------------------------------------------


def test_preflight_runs_before_the_provider_request():
    runtime = _FakeRuntime()
    inner = _RecordingTransport(_openai_body())
    transport = NullRunSyncTransport(inner, runtime)

    transport.handle_request(_request())

    assert runtime.order == ["gate", "track"], (
        "the gate pre-flight must come FIRST and the cost event after it"
    )
    assert len(inner.sent) == 1


def test_preflight_declares_the_calls_own_numbers():
    runtime = _FakeRuntime()
    inner = _RecordingTransport(_openai_body())
    transport = NullRunSyncTransport(inner, runtime)

    transport.handle_request(
        _request(
            {
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "x" * 300}],
                "max_completion_tokens": 512,
            }
        )
    )

    kwargs = runtime.gate_kwargs
    assert kwargs is not None
    assert kwargs["model"] == "gpt-4o-mini"
    assert kwargs["max_output_tokens"] == 512
    # The historical value was the literal 1 — one token. The whole
    # point of ADR-101 is that the call's own size is declared.
    assert kwargs["estimated_tokens"] >= 100
    # A cached "allow" carries the PREVIOUS call's reservation id.
    assert kwargs["use_gate_cache"] is False


def test_no_output_cap_means_no_output_cap_is_declared():
    """A declared cap is a HARD upper bound on the output. Without one
    there is no upper bound, and pricing the model cap would strand a
    month's budget on one call — so the SDK declares none.

    ``None`` is the "no cap" signal: the runtime drops a null cap from
    the wire body, so the backend reserves the input leg only."""
    runtime = _FakeRuntime()
    inner = _RecordingTransport(_openai_body())
    transport = NullRunSyncTransport(inner, runtime)

    transport.handle_request(_request())

    assert runtime.gate_kwargs.get("max_output_tokens") is None


def test_a_non_llm_host_is_not_preflighted():
    runtime = _FakeRuntime()
    inner = _RecordingTransport(b'{"ok":true}')
    transport = NullRunSyncTransport(inner, runtime)

    transport.handle_request(_request(url="https://example.com/v1/thing"))

    assert runtime.order == []
    assert runtime.tracked == []


def test_a_scope_that_already_holds_a_reservation_is_not_preflighted_again():
    """``@protect`` reserves once for the whole scope, and the LLM call
    inside settles THAT reservation. A second reserve would strand the
    first one — it would never be settled and would hold budget until
    the reclaim worker ran."""
    import time

    from nullrun.context import (
        clear_server_minted_execution_id,
        set_server_minted_execution_id,
        set_server_minted_reservation_at,
    )

    runtime = _FakeRuntime(reservation="res-new")
    inner = _RecordingTransport(_openai_body())
    transport = NullRunSyncTransport(inner, runtime)

    set_server_minted_execution_id("scope-reservation")
    set_server_minted_reservation_at(time.monotonic())
    try:
        transport.handle_request(_request())
    finally:
        clear_server_minted_execution_id()

    assert runtime.order == ["track"], (
        "the scope's own reservation is the call's reservation — the "
        "transport must not mint a second one"
    )
    assert runtime.tracked[0][1] is None, (
        "no transport reservation: the event falls back to the ambient "
        "server-minted execution id"
    )


# ---------------------------------------------------------------------------
# 2. A refusal aborts the provider request
# ---------------------------------------------------------------------------


def test_a_refusal_aborts_the_provider_request():
    runtime = _FakeRuntime(refuse=True)
    inner = _RecordingTransport(_openai_body())
    transport = NullRunSyncTransport(inner, runtime)

    with pytest.raises(NullRunBudgetError):
        transport.handle_request(_request())

    assert inner.sent == [], (
        "the provider request must NOT be sent when the budget says no"
    )
    assert runtime.tracked == []


def test_a_refusal_in_the_async_path_aborts_the_provider_request():
    import asyncio

    runtime = _FakeRuntime(refuse=True)
    inner = _RecordingAsyncTransport(_openai_body())
    transport = NullRunAsyncTransport(inner, runtime)

    async def _run():
        with pytest.raises(NullRunBudgetError):
            await transport.handle_async_request(_request())

    asyncio.run(_run())
    assert inner.sent == []


# ---------------------------------------------------------------------------
# 3. The reservation travels with the cost event
# ---------------------------------------------------------------------------


def test_the_reservation_id_reaches_the_cost_event():
    runtime = _FakeRuntime(reservation="res-42")
    inner = _RecordingTransport(_openai_body())
    transport = NullRunSyncTransport(inner, runtime)

    transport.handle_request(_request())

    assert len(runtime.tracked) == 1
    event, reservation_id = runtime.tracked[0]
    assert event["type"] == "llm_call"
    assert event["model"] == "gpt-4o-mini"
    assert reservation_id == "res-42-1", (
        "the cost event must settle the reservation THIS call minted"
    )


def test_two_calls_each_settle_their_own_reservation():
    runtime = _FakeRuntime(reservation="res")
    inner = _RecordingTransport(_openai_body())
    transport = NullRunSyncTransport(inner, runtime)

    transport.handle_request(_request())
    transport.handle_request(_request())

    reservations = [rid for _, rid in runtime.tracked]
    assert reservations == ["res-1", "res-2"], (
        "each call must settle its own reserve — a shared one would "
        "book the second call's spend against the first call's reserve"
    )


def test_concurrent_calls_do_not_swap_reservations():
    """Two calls in two threads. The legacy carrier is ONE contextvar
    slot per task, so a contextvar-based hand-off would swap them.

    The barrier forces both calls to reach the provider request before
    either emits its cost event, so the overlap really happens.
    """
    runtime = _FakeRuntime(reservation="res")
    barrier = threading.Barrier(2)
    inner = _RecordingTransport(_openai_body(), barrier=barrier)
    transport = NullRunSyncTransport(inner, runtime)

    errors: list[BaseException] = []

    def _call():
        try:
            transport.handle_request(_request())
        except BaseException as exc:  # pragma: no cover — surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=_call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    reservations = sorted(rid for _, rid in runtime.tracked)
    assert reservations == ["res-1", "res-2"], (
        "concurrent calls must not swap reservations — a contextvar "
        "hand-off would"
    )
    assert len(set(reservations)) == 2


# ---------------------------------------------------------------------------
# 4. A reservation with no usage event is released
# ---------------------------------------------------------------------------


def _release_events(runtime: _FakeRuntime) -> list[dict]:
    return [
        event
        for event, _ in runtime.tracked
        if event.get("metadata", {}).get("reservation_released")
    ]


def test_a_failed_provider_request_releases_the_reservation():
    runtime = _FakeRuntime(reservation="res-7")
    inner = _RecordingTransport(_openai_body(), fail=True)
    transport = NullRunSyncTransport(inner, runtime)

    with pytest.raises(httpx.ConnectError):
        transport.handle_request(_request())

    releases = _release_events(runtime)
    assert len(releases) == 1, "a failed call must release its reserve"
    assert releases[0]["tokens"] == 0
    assert releases[0]["model"] == "gpt-4o-mini"
    assert runtime.tracked[0][1] == "res-7-1", (
        "the release must settle the reservation it releases"
    )


def test_a_response_without_usage_releases_the_reservation():
    runtime = _FakeRuntime(reservation="res-8")
    inner = _RecordingTransport(b'{"id":"x","model":"gpt-4o-mini"}')
    transport = NullRunSyncTransport(inner, runtime)

    transport.handle_request(_request())

    releases = _release_events(runtime)
    assert len(releases) == 1
    assert releases[0]["metadata"]["release_reason"] == "usage_not_extractable"


def test_an_empty_body_releases_the_reservation():
    runtime = _FakeRuntime(reservation="res-9")
    inner = _RecordingTransport(b"")
    transport = NullRunSyncTransport(inner, runtime)

    transport.handle_request(_request())

    releases = _release_events(runtime)
    assert len(releases) == 1
    assert releases[0]["metadata"]["release_reason"] == "empty_body"


def test_a_reserved_call_with_usage_does_not_release():
    runtime = _FakeRuntime(reservation="res-10")
    inner = _RecordingTransport(_openai_body())
    transport = NullRunSyncTransport(inner, runtime)

    transport.handle_request(_request())

    assert _release_events(runtime) == [], (
        "a call that produced a usage event settles its reserve with "
        "that event — a second release would double-settle"
    )


def test_a_call_with_no_model_is_not_reserved_at_all():
    """A body with no ``model`` is not a priced call — there is nothing
    to reserve for it and no ``llm_call`` event to settle it. The
    reclaim worker is the backstop for a reserve that never arrives."""
    runtime = _FakeRuntime(reservation="res-11")
    inner = _RecordingTransport(b'{"id":"x"}')
    transport = NullRunSyncTransport(inner, runtime)

    transport.handle_request(_request({"messages": []}))

    assert runtime.order == [], "no gate call and no cost event"
    assert runtime.tracked == []


# ---------------------------------------------------------------------------
# 5. The profile reader
# ---------------------------------------------------------------------------


def test_the_profile_reads_the_model_the_size_and_the_cap():
    request = _request(
        {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hello world"}],
            "max_tokens": 256,
        }
    )
    profile = _read_llm_request_profile(request)
    assert profile is not None
    assert profile.model == "gpt-4o-mini"
    assert profile.declared_max_output_tokens == 256
    assert profile.estimated_input_tokens > 0


def test_a_body_without_a_model_is_not_a_priced_call():
    request = _request({"messages": []})
    assert _read_llm_request_profile(request) is None


def test_a_base64_blob_is_not_counted_as_prompt_text():
    """A multimodal request carries the image as base64 — megabytes of
    characters the provider bills as tiles, not as text tokens."""
    blob = "A" * 200_000
    request = _request(
        {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "user", "content": blob},
            ],
        }
    )
    profile = _read_llm_request_profile(request)
    assert profile is not None
    assert profile.estimated_input_tokens < 1000, (
        "the base64 blob must not be counted as prompt text"
    )


def test_the_input_estimate_is_capped():
    long_text = "word " * 200_000
    request = _request(
        {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": long_text}],
        }
    )
    profile = _read_llm_request_profile(request)
    assert profile is not None
    assert profile.estimated_input_tokens <= 32_768


# ---------------------------------------------------------------------------
# 6. The `requests` path carries the same pre-flight
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_requests(monkeypatch):
    """Install a synthetic ``requests`` module so the patcher can wrap
    ``Session.send`` without the real dependency."""
    import sys
    from types import ModuleType

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

    module = ModuleType("requests")
    module.Session = _FakeSession
    monkeypatch.setitem(sys.modules, "requests", module)
    yield module


def test_the_requests_path_preflights_and_carries_the_reservation(
    fake_requests, monkeypatch
):
    import importlib
    import sys
    from types import SimpleNamespace

    if "nullrun.instrumentation.auto_requests" in sys.modules:
        importlib.reload(sys.modules["nullrun.instrumentation.auto_requests"])
    from nullrun.instrumentation.auto_requests import patch_requests

    runtime = _FakeRuntime(reservation="req-1")
    assert patch_requests(runtime) is True

    body = json.dumps(
        {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 128,
        }
    ).encode()
    request = SimpleNamespace(
        url="https://api.openai.com/v1/chat/completions",
        headers={},
        body=body,
        _nullrun_tracked=False,
    )

    fake_requests.Session().send(request)

    assert runtime.order == ["gate", "track"], (
        "the requests path must pre-flight before the provider request"
    )
    assert runtime.gate_kwargs["model"] == "gpt-4o-mini"
    assert runtime.gate_kwargs["max_output_tokens"] == 128
    assert runtime.gate_kwargs["use_gate_cache"] is False
    assert len(runtime.tracked) == 1
    assert runtime.tracked[0][1] == "req-1-1"

    if "nullrun.instrumentation.auto_requests" in sys.modules:
        importlib.reload(sys.modules["nullrun.instrumentation.auto_requests"])
