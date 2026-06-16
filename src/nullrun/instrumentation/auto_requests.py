"""
Auto-instrumentation for the `requests` library — Phase P2 of the audit
fix plan.

Mirrors `auto.py` (the httpx transport hook) for the `requests` HTTP
client. The motivation: 30-50% of real codebases use `requests` directly
(e.g. via `langchain-community` or `llama-index` adapters, or hand-rolled
clients for OpenAI/Anthropic). Without this patch those calls are
invisible to NullRun even though the SDK is installed.

Reuses from `auto.py`:
- PROVIDER_EXTRACTORS and the 5 per-vendor extractor functions
  (`_openai_extractor`, `_anthropic_extractor`, etc.)
- `_match_extractor(host)` — exact + subdomain match
- `_provider_label(host)` — short label for the `provider` event field
- `_fingerprint_for(host, body, status)` — dedup fingerprint
- `_safe_bump_coverage(runtime, target_attr, host)` — bounded counter
  bump that tolerates stub runtimes (MagicMock, custom test doubles)

What this module owns:
- `patch_requests(runtime)` — wraps `requests.Session.send` so every
  call routed through a session is observed. Idempotent.
- Streaming handling: `requests.get(url, stream=True)` and
  `Accept: text/event-stream` are skipped with a `streaming-skipped`
  coverage marker. We do NOT buffer the response — that would break
  user-facing streaming (the caller reads `iter_content`/`iter_lines`
  chunk-by-chunk). The known limit is documented in
  `docs/known-limitations.md`.
- Double-emission guard: `request._nullrun_tracked = True` is set on
  the PreparedRequest after a successful track, so a future
  `urllib3` patch (which `requests` uses under the hood) can skip
  already-tracked requests. See plan section P2 / "requests ↔ urllib3".

`aiohttp` is deliberately out of scope for this phase — see
`docs/known-limitations.md` and the plan's open questions.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from nullrun.instrumentation.auto import (
    _fingerprint_for,
    _match_extractor,
    _provider_label,
    _safe_bump_coverage,
)

logger = logging.getLogger(__name__)


# Streaming detection. `stream=True` is a kwarg on the high-level
# `requests.get/post/etc.` and is also the underlying flag in
# `Session.send(prepared_request, stream=...)`. The `Accept` header
# is a server-side indicator that the user (or the high-level
# helper) declared a streaming response.
_STREAMING_CONTENT_TYPES = ("text/event-stream",)


def _is_streaming_request(request: Any, send_kwargs: dict[str, Any]) -> bool:
    """Return True when this request should be skipped because the caller
    is consuming a streaming response. We skip rather than bufferize —
    buffering `iter_content`/`iter_lines` would break user-facing SSE
    parsing, which is the dominant reason someone passes `stream=True`.
    """
    if send_kwargs.get("stream") is True:
        return True
    accept = ""
    headers = getattr(request, "headers", None)
    if headers is not None:
        try:
            accept = headers.get("Accept", "") or ""
        except Exception:  # pragma: no cover — defensive
            accept = ""
    return any(ct in accept for ct in _STREAMING_CONTENT_TYPES)


def _bump_streaming_skipped(runtime: Any, host: str) -> None:
    """Phase P2: bump a `streaming-skipped` counter so the dashboard
    surfaces *known* untracked hosts (vs. just "seen but unknown
    extractor"). Mirrors the structure of `_safe_bump_coverage` to
    tolerate stub runtimes.
    """
    target = getattr(runtime, "_coverage_streaming_skipped", None)
    if target is None:
        return
    bump = getattr(runtime, "_bump_coverage_counter", None)
    if bump is None:
        return
    try:
        bump(target, host)
    except Exception as e:  # pragma: no cover — defensive
        logger.debug("NullRun streaming-skipped bump failed: %s", e)


def _emit_to_runtime(
    runtime: Any,
    request: Any,
    host: str,
    usage: dict[str, Any],
    body: bytes,
    status: int,
) -> None:
    """Single-source-of-truth for emitting an LLM call event from any
    transport. Kept in this module (rather than re-exported from
    `auto.py`) so the requests path is self-contained and the
    `requests` dep is not pulled into `auto.py`'s import graph.
    """
    _safe_bump_coverage(runtime, "_coverage_tracked", host)
    try:
        runtime.track(
            {
                "type": "llm_call",
                "provider": _provider_label(host),
                "host": host,
                "model": usage.get("model"),
                "tokens": usage.get("total_tokens", 0),
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "has_usage": True,
                "raw_usage": usage,
                "_fingerprint": _fingerprint_for(host, body, status),
            }
        )
    except Exception as e:
        logger.debug("NullRun requests transport: track failed: %s", e)


_requests_patched = False
_requests_lock = threading.Lock()
_orig_session_send: Any = None


def patch_requests(runtime: Any) -> bool:
    """Wrap `requests.Session.send` so every call routed through a
    session is observed. Idempotent: subsequent calls are no-ops.

    Returns True on success, False if `requests` is not installed.
    """
    global _requests_patched, _orig_session_send
    with _requests_lock:
        if _requests_patched:
            return True
        try:
            import requests  # type: ignore[import-not-found]
        except ImportError:
            logger.debug("requests not installed; auto-instrumentation skipped")
            return False

        # Idempotency marker — `Session` is a class-level singleton of
        # sorts for `requests`'s module-level API too, so a class marker
        # is the cheapest way to detect "already patched".
        from requests import Session

        if getattr(Session, "_nullrun_patched", False):
            _requests_patched = True
            return True

        # Stash original on first patch so `reset_for_tests` can
        # restore. Without this, a second `patch_requests` would
        # no-op (class marker still set) AND the closure inside the
        # existing wrap would still reference the first runtime —
        # silently losing track() calls from later test runs.
        _orig_session_send = Session.send

        def _wrapped_send(self: Any, request: Any, **kwargs: Any) -> Any:
            # Cheap dedup: if a previous wrapper (e.g. a future
            # urllib3 patch) already tracked this request, do nothing.
            if getattr(request, "_nullrun_tracked", False):
                return _orig_session_send(self, request, **kwargs)

            url = getattr(request, "url", "") or ""
            # `urllib.parse` is stdlib; cheap to import lazily here
            # rather than at module load (so this module imports
            # quickly even when `requests` is not used).
            import urllib.parse

            host = urllib.parse.urlparse(url).hostname or ""

            # Phase 1.1: bump seen-counter for *every* host, including
            # ones we don't have an extractor for. Same pattern as
            # the httpx transport.
            _safe_bump_coverage(runtime, "_coverage_seen", host)

            # Streaming skip: do NOT read `response.content` here —
            # that would buffer the entire stream and break the
            # caller's chunked consumption. Mark as `streaming-skipped`
            # so the dashboard can show "known but untracked".
            if _is_streaming_request(request, kwargs):
                _bump_streaming_skipped(runtime, host)
                return _orig_session_send(self, request, **kwargs)

            extractor = _match_extractor(host)
            if extractor is None:
                return _orig_session_send(self, request, **kwargs)

            response = _orig_session_send(self, request, **kwargs)
            try:
                # `response.content` is the fully-materialized bytes.
                # For non-streaming responses this is the body; for
                # `stream=True` we already returned above so we never
                # reach this line on a stream.
                body = response.content
            except Exception as e:  # pragma: no cover — defensive
                logger.debug(
                    "NullRun requests transport: failed to read body: %s", e
                )
                return response
            if not body:
                return response

            usage = extractor(body, response.status_code)
            if usage is None:
                return response

            # Mark BEFORE the track call so a track-failure (network,
            # validation) still records the request as tracked from a
            # coverage perspective — the response WAS successfully
            # extracted, even if the server rejected the event.
            try:
                request._nullrun_tracked = True
            except Exception:  # pragma: no cover — defensive
                # Some PreparedRequest subclasses disallow attribute
                # assignment; we just lose the dedup marker in that
                # case (a future urllib3 patch may double-emit, which
                # is deduped by fingerprint at the track() sink).
                pass
            _emit_to_runtime(
                runtime, request, host, usage, body, response.status_code
            )
            return response

        Session.send = _wrapped_send  # type: ignore[method-assign]
        Session._nullrun_patched = True  # type: ignore[attr-defined]
        _requests_patched = True
        logger.info("requests auto-instrumentation installed")
        return True


def reset_for_tests() -> None:
    """Restore `requests.Session.send` to its pre-patch implementation.
    Mirrors `auto.reset_for_tests` — the next `patch_requests` installs
    a fresh wrap bound to the new runtime. Test-only.
    """
    global _requests_patched, _orig_session_send
    _requests_patched = False
    if _orig_session_send is not None:
        try:
            from requests import Session

            Session.send = _orig_session_send  # type: ignore[method-assign]
            Session._nullrun_patched = False  # type: ignore[attr-defined]
        except Exception as e:  # pragma: no cover — defensive
            logger.debug("reset_for_tests: failed to restore Session: %s", e)
    _orig_session_send = None
