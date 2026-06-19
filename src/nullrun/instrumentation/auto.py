"""
Vendor-independent auto-instrumentation for NullRun SDK.

Phase D of the hardening plan: a single `nullrun.init(api_key=...)` call should
track every LLM call regardless of vendor. The user does not need to remember
to call `patch_openai()` or wire callbacks.

Three observation paths feed a single sink (`runtime.track`):

1. **httpx transport hook** — covers ~95% of LLM traffic. Every major vendor
   SDK (openai, anthropic, mistral, google-genai, cohere) uses httpx under
   the hood. The transport intercepts the response, picks an extractor by
   URL host, and emits a `llm_call` event with raw usage.

2. **LangChain callback** — covers in-memory mock providers and callback-only
   flows that do not hit the network.

3. **OpenAI Agents SDK tracer** — covers the `agents` package which has its
   own tracing model.

Dedup happens at the `runtime.track` sink via a small LRU keyed by
`(host, body_hash)` — see `NullRunRuntime._seen_track_fingerprints`. Multiple
observation paths for the same LLM call collapse to a single
`/api/v1/track` POST.

Streaming handling: OpenAI v1.0+ (and friends) send `usage` only in the
final SSE chunk. The async transport accumulates chunks and runs the
extractor on the full buffer before forwarding. This is a deliberate UX
trade-off: streaming users get a buffered body so we can see the final
chunk, but the response content is identical.

For non-streaming responses (the common case) we read the body in-place and
return a reconstructed Response — no buffering, no UX change.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

import httpx

from nullrun.instrumentation.langgraph import NullRunCallback

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# D1: URL-keyed extractor table
# ---------------------------------------------------------------------------
# Each extractor receives the response body bytes + status code. It returns
# None when the body has no usage information (streaming mid-flight, non-LLM
# endpoint sharing the host, error response, etc.). The transport only emits
# a track event when the extractor returns a non-None dict.

ExtractedUsage = dict[str, Any]


def _openai_extractor(body: bytes, status: int) -> ExtractedUsage | None:
    """OpenAI / Azure OpenAI / Mistral / Ollama (OpenAI-compat) response shape.

    Mistral and Ollama (when serving OpenAI-compat) follow the same schema:
    response.usage.{prompt_tokens, completion_tokens, total_tokens}.
    """
    if status >= 400 or not body:
        return None
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    completion = int(usage.get("completion_tokens", 0) or 0)
    total = int(usage.get("total_tokens", 0) or 0)
    if total == 0 and (prompt or completion):
        total = prompt + completion
    if prompt == 0 and completion == 0 and total == 0:
        return None
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "model": payload.get("model"),
    }


def _anthropic_extractor(body: bytes, status: int) -> ExtractedUsage | None:
    """Anthropic Messages API response shape.

    response.usage.{input_tokens, output_tokens}.
    """
    if status >= 400 or not body:
        return None
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None
    inp = int(usage.get("input_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or 0)
    if inp == 0 and out == 0:
        return None
    return {
        "prompt_tokens": inp,
        "completion_tokens": out,
        "total_tokens": inp + out,
        "model": payload.get("model"),
    }


def _gemini_extractor(body: bytes, status: int) -> ExtractedUsage | None:
    """Google Gemini (Generative Language API) response shape.

    response.usageMetadata.{promptTokenCount, candidatesTokenCount, totalTokenCount}.
    """
    if status >= 400 or not body:
        return None
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    usage = payload.get("usageMetadata") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None
    prompt = int(usage.get("promptTokenCount", 0) or 0)
    completion = int(usage.get("candidatesTokenCount", 0) or 0)
    total = int(usage.get("totalTokenCount", 0) or 0)
    if prompt == 0 and completion == 0 and total == 0:
        return None
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total or (prompt + completion),
        "model": payload.get("modelVersion"),
    }


def _cohere_extractor(body: bytes, status: int) -> ExtractedUsage | None:
    """Cohere v2 response shape.

    response.usage.{tokens, input_tokens, output_tokens}.
    Note: Cohere streaming has no usage in stream — only non-streaming
    responses carry it. Documented in the plan.
    """
    if status >= 400 or not body:
        return None
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None
    # v2 uses input_tokens/output_tokens; v1 used prompt_tokens/completion_tokens.
    inp = int(
        usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0) or 0
    )
    out = int(
        usage.get("output_tokens", 0) or usage.get("completion_tokens", 0) or 0
    )
    total = int(usage.get("tokens", 0) or 0) or (inp + out)
    if total == 0 and inp == 0 and out == 0:
        return None
    return {
        "prompt_tokens": inp,
        "completion_tokens": out,
        "total_tokens": total,
        "model": payload.get("model"),
    }


def _bedrock_extractor(body: bytes, status: int) -> ExtractedUsage | None:
    """AWS Bedrock InvokeModel response shape.

    Bedrock returns JSON whose usage is either top-level (`inputTokens` /
    `outputTokens` on Anthropic-on-Bedrock) or nested under `usage`. We
    handle both, since model adapter shapes vary.
    """
    if status >= 400 or not body:
        return None
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    # Top-level (Anthropic-on-Bedrock, Mistral-on-Bedrock)
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
    if usage is None:
        # Some adapters put inputTokens/outputTokens at the top level
        if "inputTokens" in payload or "outputTokens" in payload:
            usage = payload
    if not isinstance(usage, dict):
        return None
    inp = int(
        usage.get("inputTokens", 0)
        or usage.get("input_tokens", 0)
        or 0
    )
    out = int(
        usage.get("outputTokens", 0)
        or usage.get("output_tokens", 0)
        or 0
    )
    total = int(usage.get("totalTokens", 0) or 0) or (inp + out)
    if inp == 0 and out == 0 and total == 0:
        return None
    return {
        "prompt_tokens": inp,
        "completion_tokens": out,
        "total_tokens": total,
        "model": payload.get("modelId") or payload.get("model"),
    }


# Order matters for suffix matching: more specific suffixes first.
PROVIDER_EXTRACTORS: dict[str, Callable[[bytes, int], ExtractedUsage | None]] = {
    "api.openai.com": _openai_extractor,
    "openai.azure.com": _openai_extractor,           # Azure OpenAI
    "api.mistral.ai": _openai_extractor,             # Mistral uses OpenAI-compat
    "api.anthropic.com": _anthropic_extractor,
    "generativelanguage.googleapis.com": _gemini_extractor,
    "api.cohere.ai": _cohere_extractor,
    "bedrock-runtime.amazonaws.com": _bedrock_extractor,
}


def _match_extractor(host: str) -> Callable[[bytes, int], ExtractedUsage | None] | None:
    """Return the extractor for `host`, or None if the host is not a known
    LLM endpoint. We match exact host first, then any subdomain (e.g.
    `eu.api.openai.com` still hits the OpenAI extractor).
    """
    if not host:
        return None
    fn = PROVIDER_EXTRACTORS.get(host)
    if fn is not None:
        return fn
    # Subdomain match: a.b.openai.com still goes to the OpenAI extractor.
    for suffix, fn in PROVIDER_EXTRACTORS.items():
        if host.endswith("." + suffix):
            return fn
    return None


def _check_kill_before_send(runtime: Any, request: httpx.Request) -> None:
    """
    L2 of the kill contract (see docs/kill-contract.md §2).

    Pre-request gate: inspects the cached remote state for the workflow
    bound to the current context / API key. If the workflow has been
    killed (or paused) by the control plane, raise BEFORE the request
    reaches the network — so a kill that lands between two LLM calls in
    a long-running agent loop is honored on the *next* iteration, not
    silently deferred until the next @protect entry or /track.

    No-ops when:
      - runtime is missing
      - the request host is not a known LLM provider (out of scope)
      - no workflow can be resolved (no active context, no API key binding)
      - the cached state is anything other than Killed / Paused

    Note: prior to T3-S2 (0.3.0) this also short-circuited in
    `local_mode` (no api_key). The local_mode branch is gone because
    api_key is now required at runtime construction — every runtime
    has a remote control plane to consult.

    Raises:
        WorkflowKilledInterrupt: state == "Killed"
        WorkflowPausedException: state == "Paused"
    """
    if runtime is None:
        return
    # Defensive: test doubles (and any duck-typed runtime) may not
    # implement `_resolve_workflow_id`. Skip the kill check silently
    # rather than crashing the user's transport hook.
    if not hasattr(runtime, "_resolve_workflow_id"):
        return
    # Phase 5 #5.8: the kill check is independent of which LLM host
    # the user is talking to. Previously the check was gated on the
    # extractor table, so a custom LLM endpoint silently bypassed the
    # dashboard KILL switch. The kill state lives in `_remote_states`,
    # which is keyed by workflow, not by host.
    workflow_id = runtime._resolve_workflow_id(None)
    if not workflow_id:
        return
    state = runtime._remote_state_for(workflow_id) if hasattr(runtime, "_remote_state_for") else getattr(runtime, "_remote_states", {}).get(workflow_id, {})
    state_name = state.get("state", "Normal")
    if state_name == "Killed":
        from nullrun.breaker.exceptions import WorkflowKilledInterrupt
        raise WorkflowKilledInterrupt(
            workflow_id=workflow_id,
            reason=state.get("reason", "remote kill"),
        )
    if state_name == "Paused":
        from nullrun.breaker.exceptions import WorkflowPausedException
        raise WorkflowPausedException(
            workflow_id=workflow_id,
            reason=state.get("reason", "remote pause"),
            resume_after=None,
        )


# ---------------------------------------------------------------------------
# D2: httpx transport hook
# ---------------------------------------------------------------------------
# The transport wraps the user's underlying transport (e.g. the default
# httpx transport). For every request, it consults the extractor table by
# host. If the host is a known LLM provider, the response body is consumed
# once, the extractor runs, and a fresh Response is returned with the same
# body bytes — callers see no behavioural change.

# NOTE (Sprint 2.3): the ``_STREAMING_CONTENT_TYPES`` constant was
# defined here but only consumed in ``auto_requests.py`` (same
# constant is re-defined there). The streaming branch in the
# httpx transport wrapper does not actually consult this table;
# it just reads the body and lets the extractors return ``None``
# for non-usage bodies. The constant is deleted to avoid the
# false impression that this module has streaming-specific
# behaviour. See auto.py module docstring §"Streaming".

class NullRunSyncTransport(httpx.BaseTransport):
    """Synchronous httpx transport that emits a `llm_call` event for known
    LLM provider responses.
    """

    def __init__(
        self,
        inner: httpx.BaseTransport,
        runtime: Any,
    ) -> None:
        self._inner = inner
        self._runtime = runtime

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        _check_kill_before_send(self._runtime, request)
        host = request.url.host
        extractor = _match_extractor(host)
        if extractor is None:
            return self._inner.handle_request(request)
        response = self._inner.handle_request(request)
        try:
            # P0-3: bounded read — never buffer more than
            # MAX_RESPONSE_BYTES for tracking purposes. Above the cap,
            # we skip tracking (the user still gets the full body via
            # the rebuilt response below). The body still needs to
            # be reconstructed for downstream consumers, so when the
            # cap is hit we fall through to ``read()`` for the
            # rebuild path only.
            body = _read_body_with_cap(response, MAX_RESPONSE_BYTES)
            if body is None:
                # Body exceeded the cap. Drain it (so callers don't
                # see a half-consumed response) but don't track.
                _safe_bump_coverage(self._runtime, "_coverage_streaming_skipped", host)
                logger.debug(
                    "NullRun transport: response from %s exceeded %d bytes; "
                    "skipping usage tracking",
                    host, MAX_RESPONSE_BYTES,
                )
                try:
                    return self._rebuild(response, response.read(), request)
                except Exception:
                    return response
        except Exception as e:  # pragma: no cover — defensive
            logger.debug("NullRun transport: failed to read body: %s", e)
            return response
        if not body:
            return response
        usage = extractor(body, response.status_code)
        if usage is None:
            # Reconstruct the response so callers can still consume the body.
            return self._rebuild(response, body, request)
        self._emit(request, host, usage, body, response.status_code)
        return self._rebuild(response, body, request)

    @staticmethod
    def _rebuild(
        response: httpx.Response,
        body: bytes,
        request: httpx.Request,
    ) -> httpx.Response:
        # `response.read()` above consumed the streamed body — and httpx
        # transparently decompresses gzip/br/zstd during that read. We
        # MUST strip the encoding header on the rebuilt response, otherwise
        # the downstream caller (e.g. openai/httpx) sees `content-encoding:
        # gzip` and tries to decompress an already-decompressed body,
        # raising `zlib.error: Error -3 while decompressing data:
        # incorrect header check`. content-length also has to be recomputed
        # against the post-decompression byte count.
        req = getattr(response, "_request", None) or request
        headers = response.headers.copy()
        # Phase 6 #6.2: also strip Transfer-Encoding so downstream
        # HTTP clients (and httpx itself) don't try to chunk-decode
        # an already-buffered body.
        for enc in (
            "content-encoding", "Content-Encoding",
            "transfer-encoding", "Transfer-Encoding",
        ):
            if enc in headers:
                del headers[enc]
        if "content-length" in headers:
            try:
                headers["content-length"] = str(len(body))
            except Exception:  # pragma: no cover
                pass
        elif "Content-Length" in headers:
            try:
                headers["Content-Length"] = str(len(body))
            except Exception:  # pragma: no cover
                pass
        return httpx.Response(
            status_code=response.status_code,
            headers=headers,
            content=body,
            request=req,
            extensions=response.extensions,
        )

    def _emit(
        self,
        request: httpx.Request,
        host: str,
        usage: ExtractedUsage,
        body: bytes,
        status: int,
    ) -> None:
        # P2-1 (plan §10): bump the coverage counter so the dashboard
        # can see which LLM hosts the agent is talking to. Pre-fix
        # this counter was only incremented in the ``requests`` path
        # (auto_requests.py:185). The httpx path is the dominant
        # one (every OpenAI / Anthropic / Gemini / Mistral / Cohere
        # call goes through httpx), so without this bump the
        # ``coverage_seen`` view in the dashboard would be empty for
        # the majority of customers.
        _safe_bump_coverage(self._runtime, "_coverage_seen", host)
        try:
            self._runtime.track(
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
                    # Fingerprint for dedup at the track() sink.
                    "_fingerprint": _fingerprint_for(host, body, status),
                }
            )
        except Exception as e:
            logger.debug("NullRun transport: track failed: %s", e)

    def close(self) -> None:
        try:
            self._inner.close()
        except Exception as e:  # pragma: no cover — defensive
            logger.debug("NullRun transport: inner close failed: %s", e)


class NullRunAsyncTransport(httpx.AsyncBaseTransport):
    """Asynchronous httpx transport. Mirrors `NullRunSyncTransport` for
    async httpx clients. The body is consumed in a single pass via
    `response.aread()`; for streamed responses, awaiting the body
    accumulates chunks so the final usage object (last SSE chunk) is
    visible to the extractor.
    """

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        runtime: Any,
    ) -> None:
        self._inner = inner
        self._runtime = runtime

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        _check_kill_before_send(self._runtime, request)
        host = request.url.host
        extractor = _match_extractor(host)
        if extractor is None:
            return await self._inner.handle_async_request(request)
        response = await self._inner.handle_async_request(request)
        try:
            # P0-3: bounded read (see sync path for full rationale).
            body = await _aread_body_with_cap(response, MAX_RESPONSE_BYTES)
            if body is None:
                _safe_bump_coverage(self._runtime, "_coverage_streaming_skipped", host)
                logger.debug(
                    "NullRun transport: async response from %s exceeded %d bytes; "
                    "skipping usage tracking",
                    host, MAX_RESPONSE_BYTES,
                )
                try:
                    return self._rebuild(response, await response.aread(), request)
                except Exception:
                    return response
        except Exception as e:  # pragma: no cover — defensive
            logger.debug("NullRun transport: failed to read async body: %s", e)
            return response
        if not body:
            return response
        usage = extractor(body, response.status_code)
        if usage is None:
            return self._rebuild(response, body, request)
        self._emit(request, host, usage, body, response.status_code)
        return self._rebuild(response, body, request)

    @staticmethod
    def _rebuild(
        response: httpx.Response,
        body: bytes,
        request: httpx.Request,
    ) -> httpx.Response:
        # See `NullRunSyncTransport._rebuild` for the gzip-strip rationale.
        # Without stripping content-encoding, the async openai/anthropic
        # clients re-decompress the already-decompressed body and raise
        # zlib.error.
        req = getattr(response, "_request", None) or request
        headers = response.headers.copy()
        # Phase 6 #6.2: also strip Transfer-Encoding so downstream
        # HTTP clients (and httpx itself) don't try to chunk-decode
        # an already-buffered body.
        for enc in (
            "content-encoding", "Content-Encoding",
            "transfer-encoding", "Transfer-Encoding",
        ):
            if enc in headers:
                del headers[enc]
        if "content-length" in headers:
            try:
                headers["content-length"] = str(len(body))
            except Exception:  # pragma: no cover
                pass
        elif "Content-Length" in headers:
            try:
                headers["Content-Length"] = str(len(body))
            except Exception:  # pragma: no cover
                pass
        return httpx.Response(
            status_code=response.status_code,
            headers=headers,
            content=body,
            request=req,
            extensions=response.extensions,
        )

    def _emit(
        self,
        request: httpx.Request,
        host: str,
        usage: ExtractedUsage,
        body: bytes,
        status: int,
    ) -> None:
        # P2-1 (plan §10): mirror the sync path — bump the coverage
        # counter so the dashboard's ``coverage_seen`` view shows
        # httpx-path traffic (the dominant path).
        _safe_bump_coverage(self._runtime, "_coverage_seen", host)
        try:
            self._runtime.track(
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
            logger.debug("NullRun transport: async track failed: %s", e)

    async def aclose(self) -> None:
        try:
            await self._inner.aclose()
        except Exception as e:  # pragma: no cover — defensive
            logger.debug("NullRun transport: inner aclose failed: %s", e)


def _provider_label(host: str) -> str:
    """Map a host to a short provider label for the `provider` event field."""
    if "openai" in host:
        return "openai"
    if "anthropic" in host:
        return "anthropic"
    if "mistral" in host:
        return "mistral"
    if "googleapis" in host:
        return "gemini"
    if "cohere" in host:
        return "cohere"
    if "bedrock" in host or "amazonaws" in host:
        return "bedrock"
    return host or "unknown"


def _fingerprint_for(host: str, body: bytes, status: int) -> str:
    """Stable fingerprint for dedup. `sha256(host|status|body)[:16]` is
    collision-resistant enough at the dedup-LRU scale (≤ a few hundred
    entries) and short enough to keep memory bounded.
    """
    h = hashlib.sha256()
    h.update(host.encode("utf-8"))
    h.update(b"|")
    h.update(str(status).encode("ascii"))
    h.update(b"|")
    h.update(body)
    return h.hexdigest()[:16]


def _fingerprint_for_event_dict(event: dict[str, Any]) -> str:
    """Stable fingerprint for a generic event dict.

    Phase 3 of the production-readiness plan: ``runtime.track_event``
    was the only emit path that did NOT set ``_fingerprint``, so two
    observers firing for the same LLM call (the user's manual
    ``track_event`` plus the httpx transport hook) produced two
    ``/track`` POSTs. This helper gives the dedup LRU a stable key
    derived from the event's content.
    """
    try:
        payload = json.dumps(event, sort_keys=True, default=str).encode("utf-8")
    except (TypeError, ValueError):
        payload = repr(event).encode("utf-8")
    h = hashlib.sha256()
    h.update(b"event|")
    h.update(payload)
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# D3: patch_httpx — idempotent __init__ wrap
# ---------------------------------------------------------------------------
# We wrap httpx.Client.__init__ / httpx.AsyncClient.__init__ so that ANY
# subsequent client construction automatically gets the NullRun transport
# applied to the user's chosen transport. This means the user does not need
# to do anything special — `openai.OpenAI(http_client=httpx.Client())` will
# be auto-instrumented.

_httpx_patched = False
_httpx_lock = threading.Lock()
# §7.2 #47: separate locks for the langchain / langgraph
# patch functions. The pre-fix code did ``if _x_patched:
# return True`` and ``getattr(SomeClass, "_nullrun_patched",
# False)`` without a lock — two threads racing through
# ``auto_instrument`` simultaneously could both pass the early
# check, both fall through to ``_orig_init = SomeClass.__init__``,
# and double-wrap the class. With CPython's GIL the race is
# narrow but real; on free-threaded builds (PEP 703) it's wide
# open. One lock per framework, held for the entire patch
# sequence so the read and the write are atomic from any other
# thread's view.
_langchain_lock = threading.Lock()
_langgraph_lock = threading.Lock()
# Originals are stashed on first patch so `reset_for_tests` can fully
# restore httpx.Client / AsyncClient to the un-patched state. Without
# this, a second `patch_httpx` would no-op (class marker still set)
# AND the closure inside the existing wrap would still reference the
# first runtime — silently losing track() calls from later test runs.
_orig_sync_init: Callable[..., Any] | None = None
_orig_async_init: Callable[..., Any] | None = None


def patch_httpx(runtime: Any) -> bool:
    """Wrap httpx.Client and httpx.AsyncClient so all new instances route
    responses through NullRun. Returns True if patching succeeded, False
    on import failure. Idempotent: subsequent calls are no-ops.
    """
    global _httpx_patched, _orig_sync_init, _orig_async_init
    with _httpx_lock:
        if _httpx_patched:
            return True
        try:
            import httpx as _httpx  # noqa: F401 — already imported above; this is the safety net
        except ImportError:  # pragma: no cover
            logger.warning("httpx not available; auto-instrumentation skipped")
            return False

        # Idempotency marker on the class itself.
        if getattr(httpx.Client, "_nullrun_patched", False):
            # Already patched by an earlier import. The class-level marker
            # is the source of truth; mirror it into the module-level flag
            # so callers can introspect with is_auto_instrumented().
            _httpx_patched = True
            return True

        # Stash originals on first patch so `reset_for_tests` can restore.
        _orig_sync_init = httpx.Client.__init__
        _orig_async_init = httpx.AsyncClient.__init__

        def _wrap_sync_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
            _orig_sync_init(self, *args, **kwargs)
            current = self._transport
            if not isinstance(current, NullRunSyncTransport):
                self._transport = NullRunSyncTransport(current, runtime)

        def _wrap_async_init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
            _orig_async_init(self, *args, **kwargs)
            current = self._transport
            if not isinstance(current, NullRunAsyncTransport):
                self._transport = NullRunAsyncTransport(current, runtime)

        httpx.Client.__init__ = _wrap_sync_init  # type: ignore[method-assign]
        httpx.AsyncClient.__init__ = _wrap_async_init  # type: ignore[method-assign]
        httpx.Client._nullrun_patched = True  # type: ignore[attr-defined]
        httpx.AsyncClient._nullrun_patched = True  # type: ignore[attr-defined]
        _httpx_patched = True
        logger.info("httpx auto-instrumentation installed (sync + async)")
        return True


# ---------------------------------------------------------------------------
# D4: patch_langchain_callback — in-memory mocks + callback-only flows
# ---------------------------------------------------------------------------
# The httpx hook covers langchain-openai (uses httpx) but NOT in-memory
# mock providers. Reusing NullRunCallback from langgraph.py is the right
# answer: it already extracts usage from LLMResult and emits via
# runtime.track.

_langchain_patched = False


def patch_langchain_callback(runtime: Any) -> bool:
    """Install NullRunCallback into the LangChain callback manager so all
    LLM calls (including mock providers) flow through it. Idempotent.

    §7.2 #47: the pre-fix code did ``if _langchain_patched: return``
    and ``getattr(BaseCallbackManager, "_nullrun_patched", False)``
    without a lock; two threads racing through ``auto_instrument``
    simultaneously could both pass the early check, then both
    fall through to ``_orig_init = BaseCallbackManager.__init__``,
    capturing the same original and double-wrapping the class.
    We hold ``_langchain_lock`` for the entire patch sequence so
    the read and the write happen atomically from any other
    thread's view.
    """
    global _langchain_patched
    with _langchain_lock:
        if _langchain_patched:
            return True
        try:
            from langchain_core.callbacks import BaseCallbackManager
        except ImportError:
            logger.debug("langchain-core not installed; LangChain callback path skipped")
            return False

        if getattr(BaseCallbackManager, "_nullrun_patched", False):
            _langchain_patched = True
            return True

        _orig_init = BaseCallbackManager.__init__

        def _wrap_init(self: Any, *args: Any, **kwargs: Any) -> None:
            _orig_init(self, *args, **kwargs)
            try:
                handlers = getattr(self, "handlers", None) or []
                if any(isinstance(h, NullRunCallback) for h in handlers):
                    return
                # Add a NullRun callback for this manager. We use the
                # add_handler API when available; otherwise we set handlers
                # directly (older LangChain).
                if hasattr(self, "add_handler"):
                    self.add_handler(NullRunCallback(runtime=runtime))
                else:
                    handlers.append(NullRunCallback(runtime=runtime))
                    self.handlers = handlers
            except Exception as e:  # pragma: no cover — defensive
                logger.debug("NullRun: failed to add callback to manager: %s", e)

        BaseCallbackManager.__init__ = _wrap_init  # type: ignore[method-assign]
        BaseCallbackManager._nullrun_patched = True  # type: ignore[attr-defined]
        _langchain_patched = True
        logger.info("LangChain callback auto-instrumentation installed")
        return True


# ---------------------------------------------------------------------------
# D5: patch_openai_agents — OpenAI Agents SDK tracer
# ---------------------------------------------------------------------------
# The `agents` package exposes a `Runner` whose `run` / `run_sync` returns
# an object that carries a `_trace_spans` list (private but stable across
# 0.1.x). We pull usage out of any `llm_call` span and emit a track event.

_agents_patched = False


def patch_openai_agents(runtime: Any) -> bool:
    """Wrap Runner.run and Runner.run_sync to read llm_call spans. Idempotent.
    Returns True on success, False if the `agents` package is not installed.
    """
    global _agents_patched
    if _agents_patched:
        return True
    try:
        from agents import Runner  # type: ignore[import-not-found]
    except ImportError:
        logger.debug("openai-agents not installed; Agents SDK path skipped")
        return False

    if getattr(Runner, "_nullrun_patched", False):
        _agents_patched = True
        return True

    _orig_run = Runner.run
    _orig_run_sync = getattr(Runner, "run_sync", None)

    def _wrap_run(*args: Any, **kwargs: Any) -> Any:
        result = _orig_run(*args, **kwargs)
        _emit_from_agents_result(runtime, result)
        return result

    def _wrap_run_sync(*args: Any, **kwargs: Any) -> Any:
        if _orig_run_sync is None:
            return _wrap_run(*args, **kwargs)
        result = _orig_run_sync(*args, **kwargs)
        _emit_from_agents_result(runtime, result)
        return result

    Runner.run = _wrap_run
    if _orig_run_sync is not None:
        Runner.run_sync = _wrap_run_sync
    Runner._nullrun_patched = True
    _agents_patched = True
    logger.info("openai-agents auto-instrumentation installed")
    return True


def _emit_from_agents_result(runtime: Any, result: Any) -> None:
    """Pull usage off a Runner.run result. The `agents` package stores
    spans on the result's `_trace_spans` attribute (private; falls back
    to `trace_spans` if exposed publicly in newer versions).
    """
    spans = (
        getattr(result, "_trace_spans", None)
        or getattr(result, "trace_spans", None)
        or []
    )
    for span in spans:
        if not isinstance(span, dict):
            continue
        if span.get("type") != "llm_call":
            continue
        usage = span.get("usage")
        if not isinstance(usage, dict):
            continue
        prompt = int(usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0) or 0)
        completion = int(usage.get("output_tokens", 0) or usage.get("completion_tokens", 0) or 0)
        total = int(usage.get("total_tokens", 0) or 0) or (prompt + completion)
        if prompt == 0 and completion == 0 and total == 0:
            continue
        try:
            runtime.track(
                {
                    "type": "llm_call",
                    "provider": "openai_agents",
                    "model": span.get("model"),
                    "tokens": total,
                    "input_tokens": prompt,
                    "output_tokens": completion,
                    "has_usage": True,
                    "raw_usage": usage,
                    "_fingerprint": f"agents-{span.get('id', id(span))}",
                }
            )
        except Exception as e:  # pragma: no cover — defensive
            logger.debug("NullRun: agents track failed: %s", e)


# ---------------------------------------------------------------------------
# D5b: patch_langgraph_compiled — auto-attach callback to compiled LangGraph
# ---------------------------------------------------------------------------
# A compiled LangGraph `StateGraph.compile()` returns a `Pregel` instance.
# To capture every invoke/stream/ainvoke/astream call site we monkey-patch
# the *class* methods so a NullRunCallback is added to
# `config["callbacks"]` automatically — the user does not have to call
# `nullrun.toolbox.langgraph.wrapper` explicitly. The patch is global
# (process-wide) but idempotent and a no-op if `langgraph` is not
# importable. Users who want per-app control (e.g. multiple runtimes in
# the same process) should use `wrapper()` instead.

_langgraph_compiled_patched = False
# Originals stashed on first patch so reset_for_tests can restore
# the un-patched class methods. The wrapped closures capture
# `runtime` in scope — without restoring, a second test pass would
# silently drop events from later runtimes.
_orig_pregel_invoke: Callable[..., Any] | None = None
_orig_pregel_stream: Callable[..., Any] | None = None
_orig_pregel_ainvoke: Callable[..., Any] | None = None
_orig_pregel_astream: Callable[..., Any] | None = None


def patch_langgraph_compiled(runtime: Any) -> bool:
    """
    Wrap `Pregel.invoke`, `Pregel.stream`, `Pregel.ainvoke`, and
    `Pregel.astream` so a `NullRunCallback` is added to the
    `config["callbacks"]` list on every call, unless the user already
    supplied one. Idempotent. Returns False if `langgraph` is not
    importable.

    §7.2 #47: same fix as ``patch_langchain_callback`` — the
    pre-fix code read the patched flag and the class-level marker
    without a lock, so two threads racing through
    ``auto_instrument`` could both fall through to
    ``Pregel.invoke = _wrap_invoke`` and double-wrap the class.
    With ``_langgraph_lock`` held, the read and the write happen
    atomically from any other thread's view.
    """
    global _langgraph_compiled_patched
    with _langgraph_lock:
        if _langgraph_compiled_patched:
            return True
        try:
            from langgraph.pregel import Pregel
        except ImportError:
            logger.debug("langgraph not installed; compiled-graph auto-patch skipped")
            return False

        if getattr(Pregel, "_nullrun_patched", False):
            _langgraph_compiled_patched = True
            return True

        def _make_callback() -> Any:
            return NullRunCallback(runtime=runtime)

        def _ensure_callback(config: Any) -> dict[str, Any]:
            """
            Inject a NullRunCallback into `config["callbacks"]` if the
            user did not already supply one. We never *replace* the
            list — user-supplied callbacks (other observability
            tools, custom handlers) are preserved.
            """
            if config is None:
                config = {}
            if not isinstance(config, dict):
                return config
            callbacks = config.get("callbacks")
            if callbacks is None:
                callbacks = []
            else:
                try:
                    if any(isinstance(cb, NullRunCallback) for cb in callbacks):
                        return config
                except TypeError:
                    return config
            callbacks = list(callbacks) + [_make_callback()]
            config = dict(config)
            config["callbacks"] = callbacks
            return config

        _orig_invoke = Pregel.invoke
        _orig_stream = Pregel.stream
        _orig_ainvoke = Pregel.ainvoke
        _orig_astream = Pregel.astream

        # Stash originals so reset_for_tests can restore the un-patched
        # class methods. The wrapped closures capture `runtime` in
        # scope — without restoring, a second test pass would silently
        # drop events from later runtimes (same hazard as httpx patch).
        global _orig_pregel_invoke, _orig_pregel_stream
        global _orig_pregel_ainvoke, _orig_pregel_astream
        _orig_pregel_invoke = _orig_invoke
        _orig_pregel_stream = _orig_stream
        _orig_pregel_ainvoke = _orig_ainvoke
        _orig_pregel_astream = _orig_astream

        def _wrap_invoke(self: Any, input: Any, config: Any = None, **kwargs: Any) -> Any:
            return _orig_invoke(self, input, _ensure_callback(config), **kwargs)

        def _wrap_stream(self: Any, input: Any, config: Any = None, **kwargs: Any) -> Any:
            return _orig_stream(self, input, _ensure_callback(config), **kwargs)

        async def _wrap_ainvoke(self: Any, input: Any, config: Any = None, **kwargs: Any) -> Any:
            return await _orig_ainvoke(self, input, _ensure_callback(config), **kwargs)

        async def _wrap_astream(self: Any, input: Any, config: Any = None, **kwargs: Any) -> Any:
            async for chunk in _orig_astream(self, input, _ensure_callback(config), **kwargs):
                yield chunk

        Pregel.invoke = _wrap_invoke  # type: ignore[method-assign]
        Pregel.stream = _wrap_stream  # type: ignore[method-assign]
        Pregel.ainvoke = _wrap_ainvoke  # type: ignore[method-assign]
        Pregel.astream = _wrap_astream  # type: ignore[method-assign]
        Pregel._nullrun_patched = True  # type: ignore[attr-defined]
        _langgraph_compiled_patched = True
        logger.info("LangGraph compiled-graph auto-instrumentation installed (Pregel.invoke/stream/ainvoke/astream)")
        return True


# ---------------------------------------------------------------------------
# D6: orchestrator
# ---------------------------------------------------------------------------
# `auto_instrument(runtime)` installs all three observation paths. Each
# patch is best-effort and silently no-ops if the underlying package is
# not installed. The user's `init()` call invokes this once.

_auto_installed = False
_auto_lock = threading.Lock()


def auto_instrument(runtime: Any) -> bool:
    """Install all auto-instrumentation paths. Idempotent. Returns True if
    at least one path was installed (so the caller can log a useful
    'instrumented N paths' message).

    Sprint 2.9 (B47): every patch call is wrapped in ``safe_patch``
    which logs at WARNING if the patch raised a non-ImportError
    exception. Pre-fix the 25+ scattered ``try/except Exception:
    pass  # pragma: no cover`` blocks meant a vendor SDK breaking
    change (e.g. a renamed method) would silently disable cost
    tracking with no log line. The operator would only find out
    when the bill arrived.
    """
    global _auto_installed
    with _auto_lock:
        if _auto_installed:
            return True
        # Lazy imports — auto_requests needs `_safe_bump_coverage` (now
        # defined in this module) at module import time. The framework
        # patches below are silent no-ops when their respective
        # packages aren't installed.
        from nullrun.instrumentation._safe_patch import safe_patch
        from nullrun.instrumentation.auto_requests import patch_requests
        from nullrun.instrumentation.autogen import patch_autogen
        from nullrun.instrumentation.crewai import patch_crewai
        from nullrun.instrumentation.llama_index import patch_llama_index

        paths = [
            safe_patch("httpx", lambda: patch_httpx(runtime)),
            safe_patch("langchain_callback", lambda: patch_langchain_callback(runtime)),
            safe_patch("openai_agents", lambda: patch_openai_agents(runtime)),
            safe_patch("langgraph_compiled", lambda: patch_langgraph_compiled(runtime)),
            safe_patch("requests", lambda: patch_requests(runtime)),
            safe_patch("llama_index", lambda: patch_llama_index(runtime)),
            safe_patch("crewai", lambda: patch_crewai(runtime)),
            safe_patch("autogen", lambda: patch_autogen(runtime)),
        ]
        # We deliberately mark this as installed even if zero paths
        # succeeded — calling auto_instrument twice must not redo work
        # (e.g. if the user calls init() twice, we don't want to double-patch).
        _auto_installed = True
        installed = sum(1 for ok in paths if ok)
        if installed:
            logger.info("NullRun auto-instrumentation: %d path(s) installed", installed)
        else:
            logger.info(
                "NullRun auto-instrumentation: no LLM frameworks detected "
                "(install one of: openai, anthropic, langchain-core, openai-agents)"
            )
        return installed > 0


def is_auto_instrumented() -> bool:
    """Return True if `auto_instrument` has been called successfully."""
    return _auto_installed


def reset_for_tests() -> None:
    """Reset the auto-instrumentation state. Test-only — never call from
    production code. Re-running auto_instrument after this point will
    re-patch httpx / langchain / agents, which can cause double-wrapping
    in long-lived test processes.

    Also restores `httpx.Client.__init__` and `AsyncClient.__init__` to
    their pre-patch implementations so the next `patch_httpx` installs a
    fresh wrap bound to the new runtime (the old wrap's closure still
    references the original runtime, which would silently drop events
    on a second test pass).
    """
    global _auto_installed, _httpx_patched, _langchain_patched, _agents_patched
    global _langgraph_compiled_patched
    global _orig_sync_init, _orig_async_init
    global _orig_pregel_invoke, _orig_pregel_stream
    global _orig_pregel_ainvoke, _orig_pregel_astream
    _auto_installed = False
    _httpx_patched = False
    _langchain_patched = False
    _agents_patched = False
    _langgraph_compiled_patched = False
    if _orig_sync_init is not None:
        try:
            httpx.Client.__init__ = _orig_sync_init  # type: ignore[method-assign]
            httpx.Client._nullrun_patched = False  # type: ignore[attr-defined]
        except Exception as e:  # pragma: no cover — defensive
            logger.debug("reset_for_tests: failed to restore httpx.Client: %s", e)
    if _orig_async_init is not None:
        try:
            httpx.AsyncClient.__init__ = _orig_async_init  # type: ignore[method-assign]
            httpx.AsyncClient._nullrun_patched = False  # type: ignore[attr-defined]
        except Exception as e:  # pragma: no cover — defensive
            logger.debug("reset_for_tests: failed to restore httpx.AsyncClient: %s", e)
    _orig_sync_init = None
    _orig_async_init = None
    if _orig_pregel_invoke is not None:
        try:
            from langgraph.pregel import Pregel
            Pregel.invoke = _orig_pregel_invoke  # type: ignore[method-assign]
            Pregel.stream = _orig_pregel_stream  # type: ignore[method-assign]
            Pregel.ainvoke = _orig_pregel_ainvoke  # type: ignore[method-assign]
            Pregel.astream = _orig_pregel_astream  # type: ignore[method-assign]
            Pregel._nullrun_patched = False  # type: ignore[attr-defined]
        except Exception as e:  # pragma: no cover — defensive
            logger.debug("reset_for_tests: failed to restore Pregel: %s", e)
    _orig_pregel_invoke = None
    _orig_pregel_stream = None
    _orig_pregel_ainvoke = None
    _orig_pregel_astream = None


# ---------------------------------------------------------------------------
# Dedup helper
# ---------------------------------------------------------------------------
# `runtime.track` consults `_seen_track_fingerprints` to drop duplicate
# events. This is exposed here so tests can introspect / clear the LRU
# without poking into the runtime module.

DEDUP_LRU_MAX = 4096  # Phase 6 #6.7: 4096 entries give a 410ms dedup window at 10K events/sec

# P0-3 (plan §10): streaming-OOM cap. Pre-fix, the sync transport
# called ``response.read()`` and the async transport called
# ``await response.aread()`` — both buffer the ENTIRE response body
# in memory. For an OpenAI streaming completion with max_tokens=8192,
# that's 16+ MB held per request. Under load (10+ concurrent streams)
# this is a real OOM risk.
#
# Cap at 16 MB. Above that, we skip tracking and increment
# ``_coverage_streaming_skipped`` so the dashboard can see which
# hosts are producing oversized responses.
#
# Env-var override: NULLRUN_MAX_RESPONSE_BYTES. None disables the cap
# (escape hatch for users who really need full-body inspection and
# can tolerate the memory cost).
import os as _os
_DEFAULT_MAX_RESPONSE_BYTES = 16 * 1024 * 1024  # 16 MiB
MAX_RESPONSE_BYTES = int(
    _os.environ.get("NULLRUN_MAX_RESPONSE_BYTES", _DEFAULT_MAX_RESPONSE_BYTES)
) or _DEFAULT_MAX_RESPONSE_BYTES


def _read_body_with_cap(response: httpx.Response, max_bytes: int) -> bytes | None:
    """Read the response body, aborting at ``max_bytes``.

    Returns the body bytes if it fits within the cap, or ``None`` if
    the body exceeded the cap (the caller should skip tracking and
    increment ``_coverage_streaming_skipped``).

    Strategy:
      1. If Content-Length is known and > cap, return None
         immediately (no read — no allocation).
      2. Otherwise stream-read in 64 KB chunks, aborting the moment
         we cross the cap. This protects against both content-length-
         known and content-length-unknown (chunked) responses.
      3. We also abort cleanly if the response is already closed /
         streaming has been consumed elsewhere.

    The sync mirror for async is ``_aread_body_with_cap``.
    """
    cl = response.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > max_bytes:
                return None
        except ValueError:
            pass  # malformed Content-Length — fall through to chunked read
    out = bytearray()
    try:
        for chunk in response.iter_bytes(chunk_size=64 * 1024):
            if len(out) + len(chunk) > max_bytes:
                return None
            out.extend(chunk)
    except Exception:
        # Stream already consumed / connection closed — fall back to
        # ``read()`` so the caller still gets the body for the user.
        try:
            return response.read()
        except Exception:
            return None
    return bytes(out)


async def _aread_body_with_cap(response: httpx.Response, max_bytes: int) -> bytes | None:
    """Async mirror of ``_read_body_with_cap``."""
    cl = response.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > max_bytes:
                return None
        except ValueError:
            pass
    out = bytearray()
    try:
        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
            if len(out) + len(chunk) > max_bytes:
                return None
            out.extend(chunk)
    except Exception:
        try:
            return await response.aread()
        except Exception:
            return None
    return bytes(out)


def make_dedup_state() -> OrderedDict[str, None]:
    """Return a fresh dedup LRU. Stored on the runtime instance."""
    return OrderedDict()


def _fingerprint_is_seen(state: OrderedDict[str, None], fp: str) -> bool:
    if not fp:
        return False
    if fp in state:
        state.move_to_end(fp)
        return True
    state[fp] = None
    if len(state) > DEDUP_LRU_MAX:
        state.popitem(last=False)
    return False


def _safe_bump_coverage(runtime: Any, target_attr: str, host: str) -> None:
    """Bump a per-host counter on the runtime, tolerating stub runtimes
    (MagicMock, custom test doubles) that don't carry the attribute.

    ``target_attr`` is one of ``_coverage_seen``,
    ``_coverage_streaming_skipped``. Mirrors the structure of
    ``_fingerprint_is_seen`` — never raises.

    Background: ``nullrun.instrumentation.auto_requests`` imports this
    helper but the original 0.3.0 release never defined it, so the
    entire ``requests`` auto-instrumentation path was unimportable.
    Adding the helper here unblocks the module and the dashboard's
    coverage tab.
    """
    target = getattr(runtime, target_attr, None)
    if target is None:
        return
    if isinstance(target, dict):
        target[host] = int(target.get(host, 0)) + 1
    else:
        try:
            target[host] = int(target[host]) + 1
        except Exception as e:  # pragma: no cover — defensive
            logger.debug("_safe_bump_coverage: %s bump failed: %s", target_attr, e)
