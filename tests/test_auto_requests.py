"""
Regression tests for the ``requests`` auto-instrumentation patch.

Installs a synthetic ``requests.Session`` into ``sys.modules`` so the
patcher can wrap ``Session.send`` end-to-end without requiring the
real ``requests`` package in CI.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _install_fake_requests(monkeypatch, *, streaming: bool = False, status: int = 200) -> dict:
    """Install a fake ``requests`` module exposing a real ``Session``
    class. The ``Session.send`` we wrap returns a fake response whose
    body bytes the test controls.

    Returns a recorder dict.
    """
    recorder = {"track": [], "track_event": []}

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
            return _FakeResponse(
                b'{"usage":{"prompt_tokens":7,"completion_tokens":11,"total_tokens":18},"model":"gpt-4o"}',
                status,
            )

        # Track which attrs were set on the class for restore-in-place
        # assertions.

    fake_mod = ModuleType("requests")
    fake_mod.Session = _FakeSession
    monkeypatch.setitem(sys.modules, "requests", fake_mod)
    return recorder


def _fake_runtime(recorder: dict) -> MagicMock:
    rt = MagicMock()
    rt.track.side_effect = lambda ev: recorder["track"].append(ev)
    rt.track_event.side_effect = lambda **kw: recorder["track_event"].append(kw)
    return rt


@pytest.fixture
def fresh_patch_module():
    if "nullrun.instrumentation.auto_requests" in sys.modules:
        importlib.reload(sys.modules["nullrun.instrumentation.auto_requests"])
    else:
        importlib.import_module("nullrun.instrumentation.auto_requests")
    yield
    if "nullrun.instrumentation.auto_requests" in sys.modules:
        importlib.reload(sys.modules["nullrun.instrumentation.auto_requests"])


# ─── ImportError / module-missing branches ───────────────────────────


def test_patch_requests_returns_false_when_missing(monkeypatch, fresh_patch_module):
    """``requests`` not importable → patch returns False."""
    monkeypatch.setitem(sys.modules, "requests", None)
    from nullrun.instrumentation.auto_requests import patch_requests

    assert patch_requests(MagicMock()) is False


def test_patch_requests_idempotent(monkeypatch, fresh_patch_module):
    """Calling patch_requests twice does not double-wrap Session.send."""
    _install_fake_requests(monkeypatch)
    from requests import Session

    from nullrun.instrumentation.auto_requests import patch_requests

    assert patch_requests(MagicMock()) is True
    wrapped = Session.send
    assert patch_requests(MagicMock()) is True
    assert Session.send is wrapped


def test_patch_requests_skips_when_class_marker_present(monkeypatch, fresh_patch_module):
    _install_fake_requests(monkeypatch)
    from requests import Session

    from nullrun.instrumentation.auto_requests import patch_requests

    Session._nullrun_patched = True
    try:
        assert patch_requests(MagicMock()) is True
    finally:
        Session._nullrun_patched = False


# ─── Happy path ──────────────────────────────────────────────────────


def test_session_send_emits_llm_call_for_openai(monkeypatch, fresh_patch_module):
    """When Session.send returns an OpenAI-shaped body, the wrapper
    emits a single llm_call event with split prompt/completion/total.
    """
    _install_fake_requests(monkeypatch)
    recorder = {"track": [], "track_event": []}
    rt = _fake_runtime(recorder)

    from requests import Session

    from nullrun.instrumentation.auto_requests import patch_requests

    assert patch_requests(rt) is True

    # Build a fake PreparedRequest-like object.
    req = SimpleNamespace(
        url="https://api.openai.com/v1/chat/completions", headers={}, _nullrun_tracked=False
    )
    Session().send(req)

    assert len(recorder["track"]) == 1
    ev = recorder["track"][0]
    assert ev["type"] == "llm_call"
    assert ev["provider"] == "openai"
    assert ev["host"] == "api.openai.com"
    assert ev["input_tokens"] == 7
    assert ev["output_tokens"] == 11
    assert ev["tokens"] == 18


def test_session_send_marks_request_as_tracked(monkeypatch, fresh_patch_module):
    """After a successful extract, the PreparedRequest is marked
    ``_nullrun_tracked=True`` for downstream dedup.
    """
    _install_fake_requests(monkeypatch)
    rt = _fake_runtime({})

    from requests import Session

    from nullrun.instrumentation.auto_requests import patch_requests

    assert patch_requests(rt) is True
    req = SimpleNamespace(url="https://api.openai.com/v1/chat/completions", headers={})
    Session().send(req)
    assert getattr(req, "_nullrun_tracked", False) is True


def test_session_send_unknown_host_no_track(monkeypatch, fresh_patch_module):
    """Host is not a known LLM endpoint — wrapper skips emit."""
    _install_fake_requests(monkeypatch)
    recorder = {"track": [], "track_event": []}
    rt = _fake_runtime(recorder)

    from requests import Session

    from nullrun.instrumentation.auto_requests import patch_requests

    assert patch_requests(rt) is True
    req = SimpleNamespace(url="https://example.com/api", headers={})
    Session().send(req)
    assert recorder["track"] == []


def test_session_send_already_tracked_returns_unchanged(monkeypatch, fresh_patch_module):
    """When ``_nullrun_tracked`` is already set, wrapper delegates
    to the original Session.send without re-emitting.
    """
    _install_fake_requests(monkeypatch)
    recorder = {"track": [], "track_event": []}
    rt = _fake_runtime(recorder)

    from requests import Session

    from nullrun.instrumentation.auto_requests import patch_requests

    assert patch_requests(rt) is True
    req = SimpleNamespace(
        url="https://api.openai.com/v1/chat/completions", headers={}, _nullrun_tracked=True
    )
    Session().send(req)
    assert recorder["track"] == []


def test_session_send_streaming_skips_track(monkeypatch, fresh_patch_module):
    """0.9.0: ``stream=True`` triggers the streaming branch which
    emits an llm_call event tagged `metadata.streaming_skipped: True`
    and `metadata.tracked: False`. The call still counts toward
    coverage `llm_call_count` (backend's denominator) but not toward
    `tracked_call_count`.
    """
    _install_fake_requests(monkeypatch, streaming=True)
    recorder = {"track": [], "track_event": []}
    rt = MagicMock()
    rt.track.side_effect = lambda ev: recorder["track"].append(ev)
    rt.track_event.side_effect = lambda **kw: recorder["track_event"].append(kw)

    from requests import Session

    from nullrun.instrumentation.auto_requests import patch_requests

    assert patch_requests(rt) is True
    req = SimpleNamespace(url="https://api.openai.com/v1/chat/completions", headers={})
    Session().send(req, stream=True)
    # Track WAS called (with the streaming-skipped flag) — the new
    # behavior replaces the old counter-bump.
    assert len(recorder["track"]) == 1
    ev = recorder["track"][0]
    assert ev["type"] == "llm_call"
    assert ev["host"] == "api.openai.com"
    assert ev["has_usage"] is False
    assert ev["metadata"]["streaming_skipped"] is True
    assert ev["metadata"]["tracked"] is False


def test_session_send_accept_event_stream_header_skips_track(monkeypatch, fresh_patch_module):
    """0.9.0: ``Accept: text/event-stream`` header triggers the same
    streaming branch — emit llm_call tagged
    `metadata.streaming_skipped: True`.
    """
    _install_fake_requests(monkeypatch)
    recorder = {"track": [], "track_event": []}
    rt = _fake_runtime(recorder)

    from requests import Session

    from nullrun.instrumentation.auto_requests import patch_requests

    assert patch_requests(rt) is True
    req = SimpleNamespace(
        url="https://api.openai.com/v1/chat/completions", headers={"Accept": "text/event-stream"}
    )
    Session().send(req)
    assert len(recorder["track"]) == 1
    assert recorder["track"][0]["metadata"]["streaming_skipped"] is True


def test_session_send_no_extractor_for_host_returns_response(monkeypatch, fresh_patch_module):
    """Unknown extractor → no emit, original response returned to caller."""
    _install_fake_requests(monkeypatch)
    recorder = {"track": [], "track_event": []}
    rt = _fake_runtime(recorder)

    from requests import Session

    from nullrun.instrumentation.auto_requests import patch_requests

    assert patch_requests(rt) is True
    req = SimpleNamespace(url="https://unknown.host.example/api", headers={})
    resp = Session().send(req)
    # Response object passed through.
    assert resp.status_code == 200
    assert recorder["track"] == []


def test_session_send_status_400_no_track(monkeypatch, fresh_patch_module):
    """Even a known host with 4xx body returns no extraction."""
    _install_fake_requests(monkeypatch, status=400)
    recorder = {"track": [], "track_event": []}
    rt = _fake_runtime(recorder)

    from requests import Session

    from nullrun.instrumentation.auto_requests import patch_requests

    assert patch_requests(rt) is True
    req = SimpleNamespace(url="https://api.openai.com/v1/chat/completions", headers={})
    Session().send(req)
    assert recorder["track"] == []


def test_session_send_empty_body_no_track(monkeypatch, fresh_patch_module):
    """Empty body → no extraction (return early)."""
    monkeypatch.setitem(sys.modules, "requests", None)  # placeholder

    # Build a session whose send returns an empty body.
    class _FakeResponse:
        status_code = 200
        content = b""
        headers = {}

    class _FakeSession:
        _nullrun_patched = False
        send_count = 0

        @staticmethod
        def send(self_or_cls, request, **kwargs):
            _FakeSession.send_count += 1
            return _FakeResponse()

    fake_mod = ModuleType("requests")
    fake_mod.Session = _FakeSession
    monkeypatch.setitem(sys.modules, "requests", fake_mod)

    recorder = {"track": [], "track_event": []}
    rt = _fake_runtime(recorder)

    from requests import Session

    from nullrun.instrumentation.auto_requests import patch_requests

    assert patch_requests(rt) is True
    req = SimpleNamespace(url="https://api.openai.com/v1/chat/completions", headers={})
    Session().send(req)
    assert recorder["track"] == []


def test_session_send_track_failure_is_swallowed(monkeypatch, fresh_patch_module):
    """If runtime.track raises, the wrapper returns the original response."""
    _install_fake_requests(monkeypatch)
    rt = MagicMock()
    rt.track.side_effect = RuntimeError("down")
    rt.track_event.side_effect = lambda **kw: None

    from requests import Session

    from nullrun.instrumentation.auto_requests import patch_requests

    assert patch_requests(rt) is True
    req = SimpleNamespace(url="https://api.openai.com/v1/chat/completions", headers={})
    resp = Session().send(req)
    assert resp.status_code == 200


# 0.9.0: removed `test_session_send_seen_counter_bumped`. The
# `_coverage_seen` per-host counter dict is gone — coverage is
# derived from llm_call span metadata.host. See plan at
# `~/.claude/plans/async-swinging-hanrahan.md`.

# ─── reset_for_tests ─────────────────────────────────────────────────


def test_reset_for_tests_restores_session(monkeypatch, fresh_patch_module):
    _install_fake_requests(monkeypatch)
    from requests import Session

    from nullrun.instrumentation.auto_requests import patch_requests, reset_for_tests

    original_send = Session.send
    assert patch_requests(MagicMock()) is True
    assert Session.send is not original_send

    reset_for_tests()
    assert Session.send is original_send
    assert Session._nullrun_patched is False


def test_reset_for_tests_when_session_unavailable_is_silent(monkeypatch, fresh_patch_module):
    """If ``requests`` was uninstalled between patch and reset, the
    reset path must not raise.
    """
    _install_fake_requests(monkeypatch)
    from nullrun.instrumentation.auto_requests import patch_requests, reset_for_tests

    assert patch_requests(MagicMock()) is True
    monkeypatch.delitem(sys.modules, "requests", raising=False)
    reset_for_tests()  # must not raise


# ─── Internal helpers ────────────────────────────────────────────────


def test_is_streaming_request_with_stream_true():
    """``stream=True`` kwarg → True."""
    from nullrun.instrumentation.auto_requests import _is_streaming_request

    req = SimpleNamespace(headers={})
    assert _is_streaming_request(req, {"stream": True}) is True


def test_is_streaming_request_with_event_stream_header():
    """``Accept: text/event-stream`` → True."""
    from nullrun.instrumentation.auto_requests import _is_streaming_request

    req = SimpleNamespace(headers={"Accept": "text/event-stream"})
    assert _is_streaming_request(req, {}) is True


def test_is_streaming_request_without_any_indicator():
    """Plain request → False."""
    from nullrun.instrumentation.auto_requests import _is_streaming_request

    req = SimpleNamespace(headers={"Accept": "application/json"})
    assert _is_streaming_request(req, {}) is False


def test_is_streaming_request_no_headers():
    """No headers at all → False."""
    from nullrun.instrumentation.auto_requests import _is_streaming_request

    req = SimpleNamespace(headers=None)
    assert _is_streaming_request(req, {}) is False


def test_is_streaming_request_headers_get_raises():
    """Header lookup that raises → False (defensive)."""
    from nullrun.instrumentation.auto_requests import _is_streaming_request

    class _BadHeaders:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("bad")

    req = SimpleNamespace(headers=_BadHeaders())
    assert _is_streaming_request(req, {}) is False


# 0.9.0: removed three `_bump_streaming_skipped` helper tests.
# The helper is gone — streaming-skipped calls now emit an
# llm_call event tagged `metadata.streaming_skipped: True`. See
# `test_session_send_streaming_skips_track` and
# `test_session_send_accept_event_stream_header_skips_track` above
# for the new behavior assertions.
