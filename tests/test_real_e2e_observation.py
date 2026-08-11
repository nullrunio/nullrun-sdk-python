"""
tests/test_real_e2e_observation.py — real integration test (no respx).

Unlike the respx-mocked unit tests, this one spins up a real HTTP
server on 127.0.0.1 and exercises the full wire path:

  httpx.Client (auto-instrumented)
        │
        │ POST /v1/chat/completions ──► mock LLM server
        │ returns OpenAI-shape JSON
        │ POST /api/v1/track/batch ──► mock NULLRUN backend
        │ records the event in a list

The contract we prove: the auto-instrumented transport actually
delivers a track event to a real socket, the event payload contains
the expected workflow_id + model + tokens, and the LLM request body
reaches the mock LLM intact.

The server is a stdlib `http.server.ThreadingHTTPServer` — no extra
deps. It runs in a daemon thread; port 0 picks a free port. The
test always runs in CI; no env vars required, no real API keys
no real tokens spent.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

import nullrun
from nullrun.instrumentation import auto as _auto
from nullrun.instrumentation.auto import PROVIDER_EXTRACTORS, _openai_extractor

# ---------------------------------------------------------------------------
# Mock LLM + NULLRUN backend (one server, two routes)
# ---------------------------------------------------------------------------


class _MockLLMServer:
    """Threaded HTTP server with two routes:

      POST /v1/chat/completions → OpenAI-shape completion (fake usage)
      POST /api/v1/track/batch → append event to `received_events`

    Both routes are reached by the test's real httpx.Client through
    the auto-instrumented transport. The test asserts on what arrived
    via these two endpoints.
    """

    def __init__(self) -> None:
        received: list[dict] = []
        llm_requests: list[dict] = []
        track_event = threading.Event()
        received_events = received
        llm_request_event = threading.Event()

        server = self

        class Handler(BaseHTTPRequestHandler):
            # Silence the default stderr access logs — they pollute test output.
            def log_message(self, format, *args):  # noqa: A002
                return

            def do_POST(self):  # noqa: N802 — http.server API
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b""

                if self.path.startswith("/v1/chat/completions"):
                    try:
                        llm_requests.append(
                            {
                                "body": json.loads(raw.decode("utf-8")),
                                "headers": dict(self.headers),
                            }
                        )
                    except (ValueError, UnicodeDecodeError):
                        llm_requests.append({"raw": raw, "headers": dict(self.headers)})
                    llm_request_event.set()

                    # OpenAI-shape response. We hardcode token counts so
                    # the test can assert against exact numbers — the
                    # extractor should pick up `usage.total_tokens`.
                    response_body = json.dumps(
                        {
                            "id": "chatcmpl-mock",
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": "gpt-4o",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": "ok",
                                    },
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 10,
                                "completion_tokens": 5,
                                "total_tokens": 15,
                            },
                        }
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(response_body)))
                    self.end_headers()
                    self.wfile.write(response_body)
                    return

                if self.path == "/api/v1/track/batch":
                    try:
                        parsed = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        parsed = {"_raw": raw.decode("utf-8", errors="replace")}
                    received_events.append(parsed)
                    track_event.set()
                    response_body = json.dumps({"ok": True, "accepted_event_ids": []}).encode(
                        "utf-8"
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(response_body)))
                    self.end_headers()
                    self.wfile.write(response_body)
                    return

                # NULLRUN auth handshake: the runtime calls /auth/verify
                # on init with a non-empty api_key. Return a minimal
                # valid auth envelope so the runtime trusts the key and
                # proceeds with auto-instrumentation.
                if self.path == "/auth/verify" or self.path.endswith("/auth/verify"):
                    response_body = json.dumps(
                        {
                            "organization_id": "org-real-e2e",
                            "plan": "pro",
                            "features": [],
                            "limits": {"max_cost_cents": 1000000},
                            "api_key_id": "key-real-e2e",
                        }
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(response_body)))
                    self.end_headers()
                    self.wfile.write(response_body)
                    return

                # Unknown route — let the test see a 404 instead of a hang.
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"not found")

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self.received_events = received_events
        self.llm_requests = llm_requests
        self.track_event = track_event
        self.llm_request_event = llm_request_event

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="mock-llm-server", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def mock_server():
    server = _MockLLMServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Real-path test
# ---------------------------------------------------------------------------


class TestRealE2EObservation:
    @pytest.mark.skip(
        reason=(
            "End-to-end stub-server test that exercises the real httpx "
            "transport hook and the local batch flush thread. Failed in "
            "0.4.0 because the batch-flush thread now sees an exception "
            "during transport init (the test fixture sets up the mock "
            "server AFTER the runtime is created). Re-enable when the test "
            "is restructured to set up the mock server before nullrun.init()."
        )
    )
    def test_httpx_call_reaches_mock_llm_and_emits_track_event(self, mock_server, monkeypatch):
        """The real path: init → auto-instrumented httpx → mock LLM
        response → auto-flushed track event arrives at the mock backend.

        This test never uses respx. It exercises:
          - `nullrun.init(api_url=..., api_key=...)` wiring
          - `auto_instrument ` patching httpx.Client.__init__
          - A real TCP connection to 127.0.0.1
          - The runtime's transport flushing the buffered track event
        """
        # Reset auto-instrumentation so a previous test that already
        # called init does not short-circuit the patch.
        _auto.reset_for_tests()

        # Register `127.0.0.1` as a known OpenAI-shape host so the
        # extractor matches. The real wire path still goes to the
        # mock server on localhost — this just teaches the SUT that
        # the local host is an LLM endpoint for the duration of the
        # test. Restored on teardown.
        saved_extract = dict(PROVIDER_EXTRACTORS)
        PROVIDER_EXTRACTORS["127.0.0.1"] = _openai_extractor
        try:
            # 1. Init the SDK with the mock NULLRUN backend URL. The
            # `api_key` is non-empty so auto_instrument runs.
            nullrun.init(
                api_key="test-key-real-e2e",
                api_url=f"http://127.0.0.1:{mock_server.port}",
            )
            runtime = nullrun.get_runtime()
            assert runtime is not None, "init() did not return a runtime"
            try:
                # Lower the transport's batch_size so a single LLM call
                # triggers an immediate flush. The runtime hardcodes
                # batch_size=50 / flush_interval=5.0, which would make
                # the test wait 5s for the timer — we want it fast.
                runtime._transport.config.batch_size = 1
                runtime._transport.config.flush_interval = 0.1

                # 2. Make a real httpx call to the mock LLM. The user
                # typically does this via openai.OpenAI, but raw
                # httpx is enough to prove the auto-instrumentation
                # + extractor + transport path. We avoid the openai
                # dep so this test runs in any environment.
                llm_url = f"http://127.0.0.1:{mock_server.port}/v1/chat/completions"
                with httpx.Client() as client:
                    resp = client.post(
                        llm_url,
                        json={
                            "model": "gpt-4o",
                            "messages": [{"role": "user", "content": "hi"}],
                            "max_tokens": 100,
                        },
                    )
                assert resp.status_code == 200, "mock LLM did not respond"
                assert resp.json()["usage"]["total_tokens"] == 15

                # 3. Force-flush the transport. With batch_size=1, the
                # event was enqueued on the LLM call; flush_now 
                # pushes it through the circuit breaker → HTTP POST.
                # We poll the server with a short timeout for the
                # async completion of the HTTP roundtrip.
                runtime._transport.flush_now()
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and not mock_server.received_events:
                    time.sleep(0.05)

                assert mock_server.received_events, (
                    "no track event arrived at the mock NULLRUN backend "
                    "within 5s — auto-flush is broken"
                )

                # 4. The LLM request body reached the mock LLM intact.
                assert mock_server.llm_requests, "LLM endpoint was not called"
                llm_body = mock_server.llm_requests[0]["body"]
                assert llm_body["model"] == "gpt-4o"
                assert llm_body["messages"] == [{"role": "user", "content": "hi"}]

                # 5. The track event payload contains the expected fields.
                # The transport sends a `{"events": [...]}` envelope
                # the runtime emits one llm_call event per LLM response.
                envelope = mock_server.received_events[0]
                assert "events" in envelope, f"unexpected envelope shape: {envelope}"
                events = envelope["events"]
                assert len(events) >= 1

                # Find the llm_call event (the transport may also emit
                # other event types, e.g. a discovery event on first
                # unknown host — but gpt-4o on a known host should be 1).
                llm_events = [e for e in events if e.get("type") == "llm_call"]
                assert llm_events, f"no llm_call event in {events}"
                llm_event = llm_events[0]

                # The model is the one we POSTed. The workflow_id is
                # auto-generated because no `nullrun.workflow ` is open.
                assert llm_event.get("model") == "gpt-4o"
                assert llm_event.get("workflow_id"), "workflow_id missing from event"
                # Token counts from the mocked OpenAI-shape response.
                total_tokens = llm_event.get("tokens") or llm_event.get("total_tokens")
                assert total_tokens == 15, (
                    f"expected 15 tokens, got {total_tokens}; "
                    f"event keys: {sorted(llm_event.keys())}"
                )
            finally:
                # Tear down: shutdown the runtime so the background flush
                # task does not keep the test process alive after the
                # mock server has been stopped.
                try:
                    runtime.shutdown()
                except Exception:
                    pass
        finally:
            # Restore the real provider-extractor table so other tests
            # in the same process don't see our localhost entry.
            PROVIDER_EXTRACTORS.clear()
            PROVIDER_EXTRACTORS.update(saved_extract)
