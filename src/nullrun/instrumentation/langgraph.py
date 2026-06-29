"""
LangGraph instrumentation primitives for NullRun SDK.

This module ships the LangChain-compatible `NullRunCallback` —
the low-level handler that:

  1. Extracts `input_tokens` / `output_tokens` from LLM responses
     and forwards them to the runtime's `track()` method (so the
     backend can compute cost from the org's pricing policy).
  2. Emits `span_start` / `span_end` events for chain / tool /
     agent runs so the dashboard reconstructs the agent tree
     (not just LLM cost). Nested runs become a parent/child span
     tree via `parent_run_id` → active-span lookup.

The user-facing helper that wires this callback onto a compiled
LangGraph app lives at `nullrun.toolbox.langgraph.wrapper` (the
manual escape hatch). For automatic attachment, see
`nullrun.instrumentation.auto.patch_langgraph_compiled` — that
is what `nullrun.init()` installs when `langgraph` is importable,
so the user does NOT need to call `wrapper()` explicitly.

Callers who want raw access to the callback can still import it
from this module:

    from nullrun.instrumentation.langgraph import NullRunCallback
"""

import logging
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from nullrun.runtime import get_runtime
from nullrun.tracing import (
    SpanContext,
    create_child_span,
    create_root_span,
    get_current_span,
)

logger = logging.getLogger(__name__)

# S-9 (plan §10 P1-3): FIFO cap on NullRunCallback._active_runs.
# Pre-fix this dict grew unbounded when ``on_chain_end`` did not fire
# (errors in the chain body). 4096 mirrors DEDUP_LRU_MAX in auto.py
# and is enough headroom for a typical agent workload without leaking
# in long-running services.
_ACTIVE_RUNS_MAX = 4096


# =============================================================================
# Helpers — read second-tier fields from every plausible source
# =============================================================================
# The token-extraction chain below is `elif` because merging token counts
# from five different shapes would silently double-count. finish_reason
# and tool_names are different: they're best-effort lookups, and a value
# sitting on `response_metadata` must not be shadowed by an earlier
# branch's empty raw_usage. These helpers walk every source independently.


def _safe_get_gen_message(response: Any) -> Any:
    """Return ``response.generations[0][0].message`` for LLMResult callback
    responses, or ``None`` if any layer is missing / malformed.

    The LLMResult shape nests the actual AI message inside a generations
    list, so anything attached to the AIMessage (tool_calls, response
    metadata, finish_reason) is unreachable via ``response.<attr>``.
    """
    try:
        gens = getattr(response, "generations", None)
        if not gens:
            return None
        first = gens[0]
        if not first:
            return None
        msg = first[0]
        return getattr(msg, "message", None)
    except (AttributeError, IndexError, TypeError):
        return None


def _get_finish_reason(response: Any) -> str | None:
    """Read finish_reason from every known location, returning the first
    non-empty value found.

    Different LangChain chat-model wrappers expose the same logical
    field under different names on different objects. We walk the
    candidate sources in priority order and return the first hit;
    priority is "outermost first" so a top-level attribute wins over
    a response_metadata hint, and a generation-message attribute is
    consulted for the LLMResult callback path where the wrapper puts
    metadata on the AIMessage rather than the LLMResult.

    Sources checked, in order:

    1. ``response.finish_reason`` / ``stop_reason`` / ``stopReason`` —
       the chat-model wrapper's top-level attribute.
    2. ``response.response_metadata[<key>]`` — OpenAI-via-LangChain
       nests finish_reason inside the metadata dict.
    3. ``response.generations[0][0].message.<attr>`` — LLMResult path
       where the wrapper put the field on the AIMessage directly.
    4. ``response.generations[0][0].message.response_metadata[<key>]``
       — LLMResult where the metadata dict lives on the AIMessage.
    5. ``response.llm_output.finish_reason`` / ``stop_reason`` — legacy
       LLMResult where finish info sits on the LLMResult itself.
    """
    finish_keys = ("finish_reason", "stop_reason", "stopReason")
    direct_attrs = ("finish_reason", "stop_reason", "stopReason")

    # 1. Direct attributes on the response object.
    for attr in direct_attrs:
        val = getattr(response, attr, None)
        if val:
            return str(val)

    # 2. response_metadata dict on the response.
    resp_meta = getattr(response, "response_metadata", None)
    if isinstance(resp_meta, dict):
        for key in finish_keys:
            val = resp_meta.get(key)
            if val:
                return str(val)

    # 3 + 4. LLMResult callback path — look on the generation's message.
    gen_msg = _safe_get_gen_message(response)
    if gen_msg is not None:
        for attr in direct_attrs:
            val = getattr(gen_msg, attr, None)
            if val:
                return str(val)
        gen_meta = getattr(gen_msg, "response_metadata", None)
        if isinstance(gen_meta, dict):
            for key in finish_keys:
                val = gen_meta.get(key)
                if val:
                    return str(val)

    # 5. llm_output dict (legacy LLMResult).
    llm_out = getattr(response, "llm_output", None)
    if isinstance(llm_out, dict):
        for key in finish_keys:
            val = llm_out.get(key)
            if val:
                return str(val)

    return None


# =============================================================================
# Usage Normalization (SDK extracts, backend computes)
# =============================================================================

def extract_usage_from_response(response: Any, provider: str, model: str) -> dict[str, Any]:
    """
    Extract usage data from LLM response.

    Returns raw usage dict - backend will normalize and compute cost.
    SDK does NOT compute cost - this is intentional (backend is source of truth).

    Phase 4.1: also extracts cache_read_tokens, cache_write_tokens,
    reasoning_tokens, finish_reason, and tool_names so the backend's
    gate/budget/loop detection can see them as first-class columns.
    Fields are best-effort — different LangChain providers expose
    different sub-objects, so any field can be missing.

    Returns:
        Dict with keys:
            - input_tokens: int
            - output_tokens: int
            - total_tokens: int
            - has_usage: bool
            - raw_usage: original dict from provider
            - cache_read_tokens: int
            - cache_write_tokens: int
            - reasoning_tokens: int
            - finish_reason: str | None
            - tool_names: list[str]
    """
    usage: dict[str, Any] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "has_usage": False,
        "raw_usage": {},
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "finish_reason": None,
        "tool_names": [],
    }

    # Try LangChain's usage_metadata first (most common for OpenAI via LangChain)
    # NOTE: For callback-based invocation, response is LLMResult, not AIMessage
    # LLMResult stores usage in generations[0][0].message.usage_metadata
    if hasattr(response, 'usage_metadata'):
        usage_meta = response.usage_metadata
        if isinstance(usage_meta, dict):
            usage["input_tokens"] = usage_meta.get('input_tokens', 0) or 0
            usage["output_tokens"] = usage_meta.get('output_tokens', 0) or 0
            usage["total_tokens"] = usage_meta.get('total_tokens', 0) or 0
            usage["raw_usage"] = dict(usage_meta)
        elif hasattr(usage_meta, 'input_tokens'):
            # Object with attributes
            usage["input_tokens"] = getattr(usage_meta, 'input_tokens', 0) or 0
            usage["output_tokens"] = getattr(usage_meta, 'output_tokens', 0) or 0
            usage["total_tokens"] = getattr(usage_meta, 'total_tokens', 0) or 0
            usage["raw_usage"] = {
                'input_tokens': usage["input_tokens"],
                'output_tokens': usage["output_tokens"],
                'total_tokens': usage["total_tokens"],
            }

    # For callback-based LLMResult, check generations[0][0].message.usage_metadata
    elif hasattr(response, 'generations') and response.generations:
        first_gen = response.generations[0][0] if response.generations else None
        if first_gen and hasattr(first_gen, 'message'):
            msg = first_gen.message
            if hasattr(msg, 'usage_metadata'):
                usage_meta = msg.usage_metadata
                if isinstance(usage_meta, dict):
                    usage["input_tokens"] = usage_meta.get('input_tokens', 0) or 0
                    usage["output_tokens"] = usage_meta.get('output_tokens', 0) or 0
                    usage["total_tokens"] = usage_meta.get('total_tokens', 0) or 0
                    usage["raw_usage"] = dict(usage_meta)
                elif hasattr(usage_meta, 'input_tokens'):
                    usage["input_tokens"] = getattr(usage_meta, 'input_tokens', 0) or 0
                    usage["output_tokens"] = getattr(usage_meta, 'output_tokens', 0) or 0
                    usage["total_tokens"] = getattr(usage_meta, 'total_tokens', 0) or 0
                    usage["raw_usage"] = {
                        'input_tokens': usage["input_tokens"],
                        'output_tokens': usage["output_tokens"],
                        'total_tokens': usage["total_tokens"],
                    }

    # Try response.usage (Anthropic, standard OpenAI format)
    elif hasattr(response, 'usage') and response.usage:
        usage_raw = response.usage
        if isinstance(usage_raw, dict):
            usage["input_tokens"] = usage_raw.get('input_tokens', 0) or 0
            usage["output_tokens"] = usage_raw.get('output_tokens', 0) or 0
            usage["total_tokens"] = usage_raw.get('total_tokens', 0) or 0
            usage["raw_usage"] = dict(usage_raw)
        elif hasattr(usage_raw, 'input_tokens') or hasattr(usage_raw, 'total_tokens'):
            # Object with attributes
            usage["input_tokens"] = getattr(usage_raw, 'input_tokens', 0) or 0
            usage["output_tokens"] = getattr(usage_raw, 'output_tokens', 0) or 0
            usage["total_tokens"] = getattr(usage_raw, 'total_tokens', 0) or 0
            usage["raw_usage"] = {
                'input_tokens': usage["input_tokens"],
                'output_tokens': usage["output_tokens"],
                'total_tokens': usage["total_tokens"],
            }

    # Try response_metadata (some providers) - also check llm_output for LLMResult
    elif hasattr(response, 'response_metadata'):
        resp_meta = response.response_metadata
        if isinstance(resp_meta, dict):
            # Some providers put token info here
            token_usage = resp_meta.get('token_usage', {})
            if isinstance(token_usage, dict):
                usage["input_tokens"] = (
                    token_usage.get('prompt_tokens', 0) or
                    token_usage.get('input_tokens', 0) or 0
                )
                usage["output_tokens"] = (
                    token_usage.get('completion_tokens', 0) or
                    token_usage.get('output_tokens', 0) or 0
                )
                usage["total_tokens"] = token_usage.get('total_tokens', 0) or 0
                usage["raw_usage"] = dict(token_usage)
    # Check llm_output for LLMResult (callback case)
    elif hasattr(response, 'llm_output') and response.llm_output:
        token_usage = response.llm_output.get('token_usage', {})
        if isinstance(token_usage, dict):
            usage["input_tokens"] = (
                token_usage.get('prompt_tokens', 0) or
                token_usage.get('input_tokens', 0) or 0
            )
            usage["output_tokens"] = (
                token_usage.get('completion_tokens', 0) or
                token_usage.get('output_tokens', 0) or 0
            )
            usage["total_tokens"] = token_usage.get('total_tokens', 0) or 0
            usage["raw_usage"] = dict(token_usage)

    # Check for streaming chunks that accumulated usage
    # (streaming responses may not have usage until final chunk)
    if not usage["has_usage"] and hasattr(response, '__iter__'):
        # For streaming, we can't get accurate usage in middle of stream
        # Final response should have usage_metadata
        pass

    # Phase 4.1: extract the second-tier fields the backend gate/budget
    # loop detection now needs. We pull from the same response object
    # LangChain already loaded — no extra HTTP, no schema surprise.
    # All five fields are best-effort: any provider that doesn't expose
    # them simply leaves the default value (0 / None / []).

    # Cache tokens — Anthropic exposes these on the usage block.
    # OpenAI exposes cached_tokens on a nested prompt_tokens_details.
    raw = usage.get("raw_usage") or {}
    if isinstance(raw, dict):
        cache_read = raw.get("cache_read_input_tokens") or raw.get(
            "cacheReadInputTokenCount"
        )
        if cache_read:
            usage["cache_read_tokens"] = int(cache_read) or 0
        cache_write = raw.get("cache_creation_input_tokens") or raw.get(
            "cacheWriteInputTokenCount"
        )
        if cache_write:
            usage["cache_write_tokens"] = int(cache_write) or 0
        prompt_details = raw.get("prompt_tokens_details") or {}
        if isinstance(prompt_details, dict) and prompt_details.get("cached_tokens"):
            # OpenAI's prefix-cached prompt hits — best-effort merge.
            usage["cache_read_tokens"] = int(
                prompt_details.get("cached_tokens") or 0
            )
        completion_details = raw.get("completion_tokens_details") or {}
        if isinstance(completion_details, dict) and completion_details.get(
            "reasoning_tokens"
        ):
            usage["reasoning_tokens"] = int(
                completion_details.get("reasoning_tokens") or 0
            )

    # Finish reason — read from every known source independently of the
    # token branch. The `elif`-chain above means only one branch fills
    # raw_usage, so finish_reason must NOT depend on which branch won;
    # otherwise a finish_reason sitting on response_metadata gets lost
    # whenever the tokens happened to live in usage_metadata.
    usage["finish_reason"] = _get_finish_reason(response)

    # Tool names — most LangChain chat models put tool calls on
    # response.tool_calls or response.additional_kwargs.tool_calls.
    # We only want the function name, not the arguments.
    def _extract_tool_names(obj: Any) -> list[str]:
        names: list[str] = []
        if obj is None:
            return names
        # Dict-style tool_calls (OpenAI ChatCompletion)
        if isinstance(obj, dict):
            tcs = obj.get("tool_calls") or []
            for tc in tcs:
                if not isinstance(tc, dict):
                    continue
                func = tc.get("function") or {}
                name = func.get("name") if isinstance(func, dict) else None
                if not name:
                    name = tc.get("name")
                if name:
                    names.append(str(name))
            return names
        # Object-style tool_calls (langchain_core.messages)
        tcs = getattr(obj, "tool_calls", None) or []
        for tc in tcs:
            name = None
            if isinstance(tc, dict):
                func = tc.get("function") or {}
                name = func.get("name") if isinstance(func, dict) else None
                if not name:
                    name = tc.get("name")
            else:
                func = getattr(tc, "function", None)
                if func is not None:
                    name = getattr(func, "name", None)
                if not name:
                    name = getattr(tc, "name", None)
            if name:
                names.append(str(name))
        return names

    collected: list[str] = []
    for src in (
        response,
        getattr(response, "additional_kwargs", None),
        getattr(response, "response_metadata", None),
        # LLMResult callback path — tool_calls live on the generation's
        # AIMessage, not on the response object itself. Without this,
        # a callback-driven LLMResult emits an empty tool_names list
        # even when the model produced several function calls.
        _safe_get_gen_message(response),
    ):
        collected.extend(_extract_tool_names(src))
    # De-duplicate while preserving first-seen order so a tool called
    # multiple times in one response appears once in the wire shape.
    seen: set[str] = set()
    usage["tool_names"] = [n for n in collected if not (n in seen or seen.add(n))]

    # Determine if we got real usage data
    usage["has_usage"] = (
        usage["total_tokens"] > 0 or
        usage["input_tokens"] > 0 or
        usage["output_tokens"] > 0
    )

    return usage


class NullRunCallback(BaseCallbackHandler):
    """
    LangChain-compatible callback handler for automatic tracking.

    IMPORTANT: This callback extracts USAGE DATA only.
    Cost computation happens in backend (source of truth).

    Span emission: chain / tool / agent runs are wrapped in
    `span_start` / `span_end` events so the dashboard reconstructs
    the agent tree (not just LLM cost). Nested runs become a
    parent/child span tree via `parent_run_id` -> active-span
    lookup; if no parent is known, we fall back to the active
    contextvar span (set by `@protect`) so a callback-driven chain
    inside an `@protect`-wrapped function is properly nested.
    """

    def __init__(self, runtime: Any | None = None) -> None:
        self.runtime = runtime or get_runtime()
        # run_id -> SpanContext for in-flight chain / tool / agent
        # runs. We use the LangChain run_id as the key because
        # on_chain_end gives us the same run_id and we need to look
        # up the corresponding span to emit span_end.
        #
        # S-9 (plan §10 P1-3): bounded to ``_ACTIVE_RUNS_MAX`` entries
        # with FIFO eviction. Pre-fix this dict grew without limit if
        # ``on_chain_start`` ran without a matching ``on_chain_end``
        # (error-heavy workloads: an exception in the chain body short-
        # circuits ``on_chain_end`` for some LangChain versions, leaving
        # the SpanContext stranded forever). Long-running services saw
        # a slow memory leak.
        #
        # Eviction policy is FIFO (insertion order) rather than LRU:
        # the most recent entries are the ones most likely to be
        # looked up by an upcoming ``on_*_end``, so we drop the
        # oldest-inserted. This matches the DEDUP_LRU_MAX pattern in
        # auto.py but uses an OrderedDict for deterministic order.
        from collections import OrderedDict

        self._active_runs: OrderedDict[str, SpanContext] = OrderedDict()
        self._active_runs_max: int = _ACTIVE_RUNS_MAX

    def _register_active_run(self, run_id: str, ctx: SpanContext) -> None:
        """Insert ``run_id -> ctx`` into ``_active_runs`` with FIFO cap.

        If the dict is at capacity, evict the oldest-inserted entry
        and log a warning so operators can detect chain-end drops.
        """
        if len(self._active_runs) >= self._active_runs_max:
            evicted_id, _ = self._active_runs.popitem(last=False)
            logger.warning(
                f"NullRunCallback._active_runs cap reached "
                f"({self._active_runs_max}); evicted oldest run_id "
                f"{evicted_id!r} — on_*_end for that run will be a no-op"
            )
        self._active_runs[run_id] = ctx

    # ------------------------------------------------------------------
    # LLM hooks (existing — token extraction only, no span bookkeeping)
    # ------------------------------------------------------------------

    def on_llm_start(self, serialized: Any, prompts: Any, **kwargs: Any) -> None:
        """Called when LLM call starts."""
        logger.debug(f"LLM start: {kwargs.get('invocation_params', {})}")

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        """
        Called when LLM call ends.

        Extracts usage data and sends to backend for cost computation.
        Does NOT compute cost - backend is source of truth.

        Audit 2026-06-28 (SDK↔backend wire): the previous version pulled
        ``model_name`` exclusively from ``invocation_params`` with a
        hard fallback to the literal string ``"unknown"``. When langchain
        1.x stopped forwarding ``invocation_params`` to ``on_llm_end``,
        every track event carried ``model="unknown"`` and the backend
        cost pipeline fell through to ``DEFAULT_RATE``. Now we try
        ``invocation_params.model_name`` first, then fall back to
        reading the real model id from the response object itself
        (``response.response_metadata['model_name']`` or the AIMessage
        on the LLMResult generation). ``"unknown"`` is now a true last
        resort, not the common case.
        """
        try:
            # Extract provider/model from invocation params first, then
            # fall back to the response object. This matches the
            # best-effort pattern used by ``_get_finish_reason`` /
            # ``_extract_tool_names`` for the same response.
            invocation_params = kwargs.get('invocation_params') or {}
            model = (
                invocation_params.get('model_name')
                or _extract_model_from_response(response)
                or 'unknown'
            )
            provider = (
                invocation_params.get('model_provider')
                or _extract_provider_from_response(response)
                or 'openai'
            )

            # Extract usage (normalized format)
            usage = extract_usage_from_response(response, provider, model)

            logger.info(f"NullRun callback: model={model}, provider={provider}, "
                      f"usage={usage}, has_usage={usage['has_usage']}")

            # Build event with RAW usage data (no cost computation in SDK!)
            # Phase 4.1: lift cache / reasoning / finish / tool names out
            # of raw_usage onto the event itself, mirroring the httpx
            # transport shape so the dedup key space stays unified.
            event = {
                "type": "llm_call",
                "model": model,
                "provider": provider,
                "tokens": usage["total_tokens"],
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "cache_read_tokens": int(usage.get("cache_read_tokens", 0) or 0),
                "cache_write_tokens": int(usage.get("cache_write_tokens", 0) or 0),
                "reasoning_tokens": int(usage.get("reasoning_tokens", 0) or 0),
                "finish_reason": usage.get("finish_reason"),
                "tool_names": usage.get("tool_names") or [],
                # Flag to backend: this is raw usage, compute cost yourself
                "has_usage": usage["has_usage"],
                # Stripped at the wire boundary by _WIRE_STRIP_FIELDS —
                # kept here for in-process dedup + test introspection.
                "raw_usage": usage["raw_usage"],
            }

            logger.info(f"NullRun track event: {event}")
            self.runtime.track(event)

            if usage["has_usage"]:
                logger.debug(
                    f"LLM tracked: model={model}, "
                    f"tokens={usage['total_tokens']} "
                    f"(in={usage['input_tokens']}, out={usage['output_tokens']})"
                )
            else:
                logger.debug(f"LLM tracked: model={model}, NO usage data available")

        except Exception as e:
            logger.warning(f"Failed to track LLM event: {e}")

    # ------------------------------------------------------------------
    # Chain / tool / agent hooks — emit span events
    # ------------------------------------------------------------------

    def on_chain_start(
        self,
        serialized: Any,
        inputs: Any,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """Open a chain span. Nested chains become child spans."""
        if run_id is None:
            # Defensive: some LangChain versions may omit run_id. We
            # cannot emit a span we can later close without a key.
            logger.debug("on_chain_start without run_id — skipping span emission")
            return
        name = _extract_node_name(serialized, "chain")
        self._begin_run(str(run_id), str(parent_run_id) if parent_run_id else None,
                        name, kind="chain")

    def on_chain_end(self, outputs: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        self._end_run(run_id)

    def on_chain_error(self, error: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        self._end_run(run_id, error=str(error))

    def on_tool_start(
        self,
        serialized: Any,
        input_str: Any,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """Open a tool span — function calls inside an agent."""
        if run_id is None:
            logger.debug("on_tool_start without run_id — skipping span emission")
            return
        name = _extract_node_name(serialized, "tool")
        self._begin_run(str(run_id), str(parent_run_id) if parent_run_id else None,
                        name, kind="tool")

    def on_tool_end(self, output: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        self._end_run(run_id)

    def on_tool_error(self, error: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        self._end_run(run_id, error=str(error))

    def on_agent_action(
        self,
        action: Any,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """Agent reasoning step (ReAct / OpenAI Functions agent)."""
        if run_id is None:
            return
        tool = getattr(action, "tool", None) or "agent"
        self._begin_run(str(run_id), str(parent_run_id) if parent_run_id else None,
                        f"agent_action:{tool}", kind="agent")

    def on_agent_finish(self, finish: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        self._end_run(run_id)

    # ------------------------------------------------------------------
    # Span bookkeeping
    # ------------------------------------------------------------------

    def _begin_run(
        self,
        run_id: str,
        parent_run_id: str | None,
        name: str,
        kind: str,
    ) -> None:
        """
        Open a span for `run_id`, attached to the parent either via
        the active-runs map (callback-internal nesting) or via the
        SDK's `tracing` contextvar (set by `@protect`).

        Span emission is best-effort — a failure here must never
        break the user's chain. Mirrors the contract in
        `nullrun.decorators._emit_span_start`.
        """
        parent_ctx: SpanContext | None = None
        if parent_run_id:
            parent_ctx = self._active_runs.get(parent_run_id)
        if parent_ctx is None:
            # Fall back to contextvar (e.g. we're inside an
            # @protect-wrapped function or a manual `set_span`).
            parent_ctx = get_current_span()
        if parent_ctx is not None:
            ctx = create_child_span(parent_ctx)
        else:
            ctx = create_root_span()
        self._register_active_run(run_id, ctx)
        try:
            self.runtime.track_event(
                event_type="span_start",
                trace_id=ctx.trace_id,
                span_id=ctx.span_id,
                parent_span_id=ctx.parent_span_id,
                depth=ctx.depth,
                fn_name=name,
                span_kind=kind,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"span_start emission failed: {exc}")

    def _end_run(self, run_id: Any, error: str | None = None) -> None:
        if run_id is None:
            return
        ctx = self._active_runs.pop(str(run_id), None)
        if ctx is None:
            return
        try:
            self.runtime.track_event(
                event_type="span_end",
                trace_id=ctx.trace_id,
                span_id=ctx.span_id,
                parent_span_id=ctx.parent_span_id,
                depth=ctx.depth,
                fn_name=None,
                error=error,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"span_end emission failed: {exc}")


def _extract_node_name(serialized: Any, default: str) -> str:
    """
    Best-effort extraction of a friendly node name from a LangChain
    `serialized` dict. Falls back to `default` if nothing readable.
    """
    if not isinstance(serialized, dict):
        return default
    ident = serialized.get("id")
    if isinstance(ident, (list, tuple)) and ident:
        return str(ident[-1])
    if isinstance(ident, str):
        return ident
    name = serialized.get("name")
    if isinstance(name, str):
        return name
    return default


# ---------------------------------------------------------------------------
# Audit 2026-06-28 (SDK↔backend wire): model_name on the callback path
# ---------------------------------------------------------------------------
# Pre-fix: ``on_llm_end`` pulled ``model_name`` exclusively from
# ``kwargs['invocation_params']`` with a hard fallback to the literal
# string ``"unknown"``. When langchain 1.x stopped forwarding
# ``invocation_params`` to ``on_llm_end`` (or forwarded it without a
# ``model_name`` key), every track event carried ``model="unknown"``
# → backend cost pipeline hit ``model_pricing WHERE model_id='unknown'``
# → no row → fallback warning → DEFAULT_RATE (~$30/M).
#
# Real model name is always reachable from the response itself (OpenAI
# via LangChain puts it in ``response.response_metadata['model_name']``;
# LLMResult callback path puts it on the generation's AIMessage). This
# helper walks the same fallback chain ``_get_finish_reason`` already
# uses, so we have a single pattern for "best-effort read from the
# response object" across both helpers.

def _extract_model_from_response(response: Any) -> str | None:
    """Best-effort model extraction mirroring ``_get_finish_reason``.

    Returns the first non-empty value found, or ``None`` if every known
    source is empty / malformed.

    Audit 2026-06-29 (SDK↔backend wire: silent zero-billing): the chain
    was checked top-to-bottom and silently returned ``None`` whenever
    none of the four known locations carried the model. The backend
    then ``unwrap_or("default")``'d to ``DEFAULT_RATE`` and every call
    was recorded as ≈$0. We now:

      - promote ``response.llm_output['model_name']`` (the location
        langchain-openai 1.x uses for the date-suffixed model id
        ``gpt-4.1-mini-2025-04-14``) to step 1, ahead of the
        ``response_metadata`` step that langchain 0.x used;
      - add ``response.llm_output['model']`` and a generic
        "any key containing 'model'" sweep so non-OpenAI wrappers
        (proxies, custom chat models) still get attributed;
      - log a DEBUG line on the None path so an operator who sees
        the wire warning in the backend can correlate it to the
        observation site that produced the event.

    Sources checked, in order:

    1. ``response.llm_output['model_name']`` / ``['model']`` /
       any key containing "model" — langchain-openai 1.x puts the
       date-suffixed id (e.g. ``"gpt-4.1-mini-2025-04-14"``) on
       ``LLMResult.llm_output``. The backend's ``MODEL_RATES``
       substring-match handles the date suffix.
    2. ``response.response_metadata['model_name']`` — direct AIMessage
       case (langchain 0.x chat-model wrappers expose metadata at
       this level).
    3. ``response.generations[0][0].message.response_metadata['model_name']``
       — LLMResult callback path where the metadata lives on the
       AIMessage rather than the LLMResult itself.
    4. Direct ``response.model`` / ``response.model_name`` attributes
       (rare, seen on some custom wrappers).
    """
    # 1. llm_output dict (langchain-openai 1.x primary location).
    #    Promote ahead of the response_metadata step: for OpenAI via
    #    LangChain 1.x, the LLMResult carries the model on
    #    ``llm_output['model_name']`` (date-suffixed) while the
    #    AIMessage inside ``generations[0][0].message`` does NOT
    #    carry ``response_metadata`` populated — step 3 would return
    #    None. Without promoting step 1, every OpenAI call was
    #    silently zero-billed.
    llm_out = getattr(response, "llm_output", None)
    if isinstance(llm_out, dict) and llm_out:
        # Preferred: explicit "model_name" then "model" key.
        for key in ("model_name", "model"):
            val = llm_out.get(key)
            if isinstance(val, str) and val:
                return val
        # Fallback: scan every key in llm_output for one that
        # contains "model" and holds a non-empty string. Some
        # custom chat-model wrappers / proxies put the model under
        # less canonical keys (``"model_id"``, ``"modelName"``,
        # ``"resolved_model"``).
        for key, val in llm_out.items():
            if (
                isinstance(key, str)
                and "model" in key.lower()
                and isinstance(val, str)
                and val
            ):
                return val

    # 2. response_metadata on the response (langchain 0.x AIMessage
    #    case, and any wrapper that hoists the metadata up).
    resp_meta = getattr(response, "response_metadata", None)
    if isinstance(resp_meta, dict):
        val = resp_meta.get("model_name") or resp_meta.get("model")
        if val:
            return str(val)

    # 3. LLMResult callback path — look on the generation's AIMessage.
    gen_msg = _safe_get_gen_message(response)
    if gen_msg is not None:
        gm = getattr(gen_msg, "response_metadata", None)
        if isinstance(gm, dict):
            val = gm.get("model_name") or gm.get("model")
            if val:
                return str(val)
        # Some wrappers put the model name directly on the AIMessage.
        for attr in ("model_name", "model"):
            v = getattr(gen_msg, attr, None)
            if v:
                return str(v)

    # 4. Direct attribute on response.
    for attr in ("model_name", "model"):
        v = getattr(response, attr, None)
        if v:
            return str(v)

    # Diagnostic: every code path above returned None. The runtime
    # layer will warn at ERROR when this happens for an llm_call
    # event; this DEBUG line is for the per-call site so the
    # operator can correlate the wire warning back to a specific
    # response shape.
    try:
        response_type = type(response).__name__
    except Exception:
        response_type = "<unknown>"
    logger.debug(
        "_extract_model_from_response returned None for response of type %s",
        response_type,
    )
    return None


def _extract_provider_from_response(response: Any) -> str | None:
    """Best-effort provider extraction mirroring ``_extract_model_from_response``.

    Same fallback chain — ``model_provider`` is what langchain passes
    in ``invocation_params`` and what we want to read from response
    metadata when invocation_params is absent. Returns ``None`` if
    nothing is found so the caller keeps the default ('openai').
    """
    resp_meta = getattr(response, "response_metadata", None)
    if isinstance(resp_meta, dict):
        val = resp_meta.get("model_provider") or resp_meta.get("provider")
        if val:
            return str(val)

    gen_msg = _safe_get_gen_message(response)
    if gen_msg is not None:
        gm = getattr(gen_msg, "response_metadata", None)
        if isinstance(gm, dict):
            val = gm.get("model_provider") or gm.get("provider")
            if val:
                return str(val)

    return None

