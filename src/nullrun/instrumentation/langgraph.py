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
# Usage Normalization (SDK extracts, backend computes)
# =============================================================================

def extract_usage_from_response(response: Any, provider: str, model: str) -> dict[str, Any]:
    """
    Extract usage data from LLM response.

    Returns raw usage dict - backend will normalize and compute cost.
    SDK does NOT compute cost - this is intentional (backend is source of truth).

    Returns:
        Dict with keys:
            - input_tokens: int
            - output_tokens: int
            - total_tokens: int
            - has_usage: bool
            - raw_usage: original dict from provider
    """
    usage: dict[str, Any] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "has_usage": False,
        "raw_usage": {},
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
        """
        try:
            # Extract provider/model from invocation params
            invocation_params = kwargs.get('invocation_params', {})
            model = invocation_params.get('model_name', 'unknown')
            provider = invocation_params.get('model_provider', 'openai')

            # Extract usage (normalized format)
            usage = extract_usage_from_response(response, provider, model)

            logger.info(f"NullRun callback: model={model}, provider={provider}, "
                      f"usage={usage}, has_usage={usage['has_usage']}")

            # Build event with RAW usage data (no cost computation in SDK!)
            event = {
                "type": "llm_call",
                "model": model,
                "provider": provider,
                "tokens": usage["total_tokens"],
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                # Flag to backend: this is raw usage, compute cost yourself
                "has_usage": usage["has_usage"],
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

