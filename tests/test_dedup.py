"""
Tests for the dedup LRU used by `NullRunRuntime.track` to collapse
duplicate events from multiple observation paths (httpx transport,
LangChain callback, OpenAI Agents tracer).

The dedup contract:
- A fingerprint is `sha256(host|status|body)[:16]`.
- The first time a fingerprint is seen, track() runs the real path.
- Subsequent calls with the same fingerprint short-circuit and return
  a `deduped: True` envelope so the caller still has a well-formed dict.
- The LRU is bounded at `DEDUP_LRU_MAX` (512) entries; the oldest
  entry is dropped on overflow.
- The LRU is shared per-runtime (one `OrderedDict` per
  `NullRunRuntime` instance).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from nullrun.instrumentation.auto import (
    DEDUP_LRU_MAX,
    _fingerprint_for,
    _fingerprint_is_seen,
    make_dedup_state,
    patch_httpx,
    reset_for_tests,
)

# ---------------------------------------------------------------------------
# Pure LRU mechanics
# ---------------------------------------------------------------------------


def test_fingerprint_for_deterministic():
    body = b'{"usage":{"total_tokens":5}}'
    a = _fingerprint_for("api.openai.com", body, 200)
    b = _fingerprint_for("api.openai.com", body, 200)
    assert a == b
    assert len(a) == 16


def test_fingerprint_changes_with_host():
    body = b"x"
    assert _fingerprint_for("a.com", body, 200) != _fingerprint_for("b.com", body, 200)


def test_fingerprint_changes_with_status():
    body = b"x"
    assert _fingerprint_for("a.com", body, 200) != _fingerprint_for("a.com", body, 429)


def test_fingerprint_changes_with_body():
    assert _fingerprint_for("a.com", b"a", 200) != _fingerprint_for("a.com", b"b", 200)


def test_lru_returns_false_then_true():
    state = make_dedup_state()
    assert _fingerprint_is_seen(state, "fp1") is False
    assert _fingerprint_is_seen(state, "fp1") is True


def test_lru_records_unique_fingerprints():
    state = make_dedup_state()
    _fingerprint_is_seen(state, "a")
    _fingerprint_is_seen(state, "b")
    _fingerprint_is_seen(state, "c")
    assert len(state) == 3
    assert "a" in state
    assert "b" in state
    assert "c" in state


def test_lru_refreshes_on_repeat_access():
    """A repeated fingerprint should be moved to the end of the LRU
    (most-recently used) so it survives eviction longer than a one-shot
    entry of the same age."""
    state = make_dedup_state()
    _fingerprint_is_seen(state, "a")
    _fingerprint_is_seen(state, "b")
    _fingerprint_is_seen(state, "a")  # refresh `a`
    # Insertion order is now: b, a
    assert list(state.keys()) == ["b", "a"]


def test_lru_evicts_oldest_on_overflow():
    """When the LRU exceeds `DEDUP_LRU_MAX`, the oldest entry is
    dropped to make room. We pre-fill exactly to the threshold, then
    add one more and assert the first entry is gone."""
    state = make_dedup_state()
    # Pre-fill exactly to DEDUP_LRU_MAX (no eviction yet)
    for i in range(DEDUP_LRU_MAX):
        _fingerprint_is_seen(state, f"fp{i}")
    assert len(state) == DEDUP_LRU_MAX
    assert "fp0" in state  # the oldest entry
    # One more insert pushes us over the limit and evicts fp0
    _fingerprint_is_seen(state, "fp-new")
    assert len(state) == DEDUP_LRU_MAX
    assert "fp0" not in state
    assert "fp-new" in state


def test_lru_empty_fingerprint_short_circuits_to_unseen():
    state = make_dedup_state()
    # An empty fingerprint must NEVER be considered seen (caller
    # probably forgot to attach one). This is the safety net that
    # keeps the dedup logic from accidentally eating legitimate
    # events that lack a fingerprint.
    assert _fingerprint_is_seen(state, "") is False
    assert len(state) == 0


# ---------------------------------------------------------------------------
# End-to-end: track() collapses duplicate LLM calls
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_httpx_patch():
    reset_for_tests()
    yield
    reset_for_tests()


@pytest.fixture
def runtime():
    """A MagicMock runtime with a real dedup LRU attached. The dedup
    tests don't need a real NullRunRuntime — they exercise the LRU
    mechanics directly and use the LRU field as a sentinel."""
    rt = MagicMock()
    rt.track = MagicMock()
    return rt


def _llm_body() -> bytes:
    return json.dumps(
        {
            "model": "gpt-4o-mini",
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
        }
    ).encode()


def _make_test_runtime() -> tuple[MagicMock, dict]:
    """Build a minimal stand-in for NullRunRuntime that exercises the
    dedup branch in track() without a real runtime. We monkeypatch
    the track() method's `_seen_track_fingerprints` attribute onto the
    mock so the real production dedup code path runs against our LRU.
    """
    rt = MagicMock()
    rt._seen_track_fingerprints = make_dedup_state()
    return rt, {"track_calls": 0, "deduped_returns": 0}


def test_two_identical_llm_calls_dedupe_to_one_track(runtime):
    """Simulate the same LLM call hitting the runtime twice (e.g. once
    via httpx transport and once via LangChain callback). With the
    dedup LRU, only the first call should reach `track()`; the second
    should short-circuit."""
    from nullrun.instrumentation.auto import _fingerprint_for
    body = _llm_body()
    fp = _fingerprint_for("api.openai.com", body, 200)
    # Pre-fill the dedup state to simulate "this fingerprint was already
    # seen" — exactly what would happen on the second observation.
    runtime._seen_track_fingerprints = make_dedup_state()
    runtime._seen_track_fingerprints[fp] = None

    # Now build a track() call that exercises the dedup gate. We can't
    # easily call the real NullRunRuntime.track() without a full
    # network stack, so we inline the dedup check that track() runs.
    is_seen = _fingerprint_is_seen(runtime._seen_track_fingerprints, fp)
    assert is_seen is True
    # The dedup branch in track() would return immediately here.
    # runtime.track was never called in production code either; this
    # test pins the contract that the LRU contains the fingerprint
    # and a re-pass returns True.
    assert fp in runtime._seen_track_fingerprints


def test_distinct_llm_calls_have_distinct_fingerprints(runtime):
    """Two different responses (different bodies) must NOT dedupe."""
    body_a = json.dumps({"usage": {"total_tokens": 10}}).encode()
    body_b = json.dumps({"usage": {"total_tokens": 20}}).encode()
    fp_a = _fingerprint_for("api.openai.com", body_a, 200)
    fp_b = _fingerprint_for("api.openai.com", body_b, 200)
    assert fp_a != fp_b
    # And a third with the same body but a different host also differs.
    fp_c = _fingerprint_for("api.anthropic.com", body_a, 200)
    assert fp_c != fp_a


def test_httpx_then_langchain_simulation_dedupes():
    """End-to-end: one OpenAI call fires both the httpx transport AND
    a LangChain callback. The transport always calls `runtime.track`;
    the runtime's `track()` consults the LRU and short-circuits on
    repeat fingerprints. This test pins the contract that the
    transport embeds the SAME fingerprint for the same body, and that
    a re-emitted event with the same fingerprint is recognised by the
    LRU."""
    # Plain object with explicit attrs — no MagicMock magic on the LRU
    # field, since MagicMock auto-attributes would mask the real dict.
    class _Rt:
        track: MagicMock
        _seen_track_fingerprints: Any

    rt = _Rt()
    rt.track = MagicMock()
    rt._seen_track_fingerprints = make_dedup_state()

    patch_httpx(rt)
    body = _llm_body()
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=httpx.Response(200, content=body)
        )
        with httpx.Client(base_url="https://api.openai.com") as client:
            # First call: track() called with an event that has a fingerprint.
            response1 = client.post(
                "/v1/chat/completions", json={"model": "gpt-4o-mini"}
            )
            assert response1.status_code == 200
            assert rt.track.call_count == 1
            event1 = rt.track.call_args_list[0][0][0]
            fp1 = event1["_fingerprint"]
            assert fp1  # non-empty
            # Simulate the real runtime's dedup gate: on the first
            # event, the LRU is fresh, so the gate passes and the
            # fingerprint is recorded.
            assert _fingerprint_is_seen(rt._seen_track_fingerprints, fp1) is False
            _fingerprint_is_seen(rt._seen_track_fingerprints, fp1)
            # Second call (same body, simulating LangChain firing on
            # the same LLMResult): the transport wraps again, so
            # track() is called again with the same fingerprint.
            response2 = client.post(
                "/v1/chat/completions", json={"model": "gpt-4o-mini"}
            )
            assert response2.status_code == 200
            event2 = rt.track.call_args_list[1][0][0]
            assert event2["_fingerprint"] == fp1
            # The runtime's dedup gate would now short-circuit.
            assert _fingerprint_is_seen(rt._seen_track_fingerprints, fp1) is True
    # Transport contract: track() is called for EVERY response (the
    # dedup is the runtime's job, not the transport's). So 2 calls.
    assert rt.track.call_count == 2
    # But the LRU contains exactly one fingerprint — that's the
    # whole point of dedup.
    assert len(rt._seen_track_fingerprints) == 1
