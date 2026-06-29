"""
Pin the wire shape of ``llm_call`` event metadata for coverage derivation.

The backend's coverage query (backend/src/coverage/mod.rs) reads two
boolean flags off `metadata`:

  - `tracked` — True when the SDK's `_match_extractor` identified a
    known provider (extractor returned a non-None usage). False for
    hosts without an extractor, or where the model was the literal
    "unknown" fallback.

  - `streaming_skipped` — True when the response body exceeded
    `MAX_RESPONSE_BYTES` and usage was NOT extractable. The event is
    still emitted (counts toward `llm_call_count` denominator) so
    coverage_pct is honest about streamed calls.

0.9.0: these flags REPLACE the old per-host `_coverage_seen` /
`_coverage_tracked` / `_coverage_streaming_skipped` counter dicts.
The previous counter-bump path is gone — see plan at
`~/.claude/plans/async-swinging-hanrahan.md`.

These tests do NOT exercise the actual HTTP path (that's
`test_streaming_oom_cap.py` and `test_auto_requests.py`). They pin
the wire shape at the SDK boundary so a future refactor that drops
the flags will fail CI immediately.
"""

from unittest.mock import MagicMock

import httpx


# Mirror the response builder from test_streaming_oom_cap.py to keep
# these tests self-contained.
def _make_response(content: bytes, content_length: int | None = None) -> httpx.Response:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    headers = {"content-type": "application/json"}
    if content_length is not None:
        headers["content-length"] = str(content_length)
    return httpx.Response(200, headers=headers, content=content, request=request)


def test_tracked_flag_true_on_normal_call():
    """A normal call (under cap, extractor matched) emits tracked: True
    and NO streaming_skipped flag."""
    from nullrun.instrumentation.auto import (
        MAX_RESPONSE_BYTES,
        NullRunSyncTransport,
    )

    runtime = MagicMock()
    inner = MagicMock()
    body = (
        b'{"id":"chatcmpl-1","choices":[{"message":{"role":"assistant","content":"hi"}}],'
        b'"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}'
    )
    inner.handle_request.return_value = _make_response(body, content_length=len(body))
    transport = NullRunSyncTransport(inner=inner, runtime=runtime)
    transport.handle_request(
        httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    )

    event = runtime.track.call_args[0][0]
    assert event["metadata"]["tracked"] is True
    # `streaming_skipped` may be absent or False; the absence is the
    # honest wire shape.
    assert event["metadata"].get("streaming_skipped", False) is False


def test_streaming_skipped_flag_on_oversized_response():
    """Oversized response → tracked: False, streaming_skipped: True."""
    from nullrun.instrumentation.auto import (
        MAX_RESPONSE_BYTES,
        NullRunSyncTransport,
    )

    runtime = MagicMock()
    inner = MagicMock()
    body = b"x" * (MAX_RESPONSE_BYTES + 1)
    inner.handle_request.return_value = _make_response(body, content_length=len(body))
    transport = NullRunSyncTransport(inner=inner, runtime=runtime)
    transport.handle_request(
        httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    )

    event = runtime.track.call_args[0][0]
    assert event["metadata"]["tracked"] is False
    assert event["metadata"]["streaming_skipped"] is True


def test_track_does_not_strip_metadata_flags():
    """`metadata` is NOT in `_WIRE_STRIP_FIELDS` (runtime.py:106-108).
    Verify the flags survive the wire boundary by mocking the
    transport-level `track` to apply the same stripping rule the
    real runtime uses."""
    from nullrun.instrumentation.auto import (
        MAX_RESPONSE_BYTES,
        NullRunSyncTransport,
    )
    from nullrun.runtime import NullRunRuntime

    runtime = NullRunRuntime(api_key="test", _test_mode=True)
    # Capture wire-event shape post-strip:
    captured = {}

    def _capture(enriched):
        # Mirror runtime.py:1427-1431 strip rule.
        _WIRE_STRIP_FIELDS = frozenset({"cost_cents", "_fingerprint", "raw_usage"})
        wire = {k: v for k, v in enriched.items() if k not in _WIRE_STRIP_FIELDS and v is not None}
        captured["event"] = wire
        return wire

    runtime.track = _capture
    inner = MagicMock()
    body = b"x" * (MAX_RESPONSE_BYTES + 1)
    inner.handle_request.return_value = _make_response(body, content_length=len(body))
    transport = NullRunSyncTransport(inner=inner, runtime=runtime)
    transport.handle_request(
        httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    )

    wire = captured["event"]
    # `metadata` field is preserved on the wire — backend reads it.
    assert "metadata" in wire
    assert wire["metadata"]["tracked"] is False
    assert wire["metadata"]["streaming_skipped"] is True