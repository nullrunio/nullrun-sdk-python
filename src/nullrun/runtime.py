"""
NullRun Runtime - core runtime safety layer for AI agents.

This is the main entry point for the SDK. It handles:
- Authentication with NullRun cloud
- Policy fetching and caching
- Event buffering and batched flush
- Local policy enforcement (instant, no network latency)
- Pre-execution enforcement via /execute endpoint

## Pre-execution gate fail-OPEN/CLOSED contract (ADR-008)

The SDK enforces workflow safety through a set of *pre-execution gates*
that run before a protected function body executes and may raise to halt
the work. Each gate declares its own fail-OPEN/CLOSED policy -- this is
the authoritative table; deviations require an ADR amendment (Rule 5).

| Gate | Transport-error behavior | Recovery behavior | Opt-out |
|---|---|---|---|
| `check_workflow_budget` | OPEN (skip check, log warning) | silent post-hoc correction in `/track` events via `cost_correction_applied=true` | `NULLRUN_SKIP_BUDGET_CHECK=1` -- **full billing bypass**, not just check bypass (see docstring WARNING) |
| `check_control_plane` | OPEN (treat state as `Normal`) | deferred enforcement -- next WS-push or `/status` poll sees the true state | none |
| `_enforce_sensitive_tool` (default `_fallback_mode=permissive`) | CLOSED -- body MUST NOT run when `decision_source` is any `FALLBACK_*` | n/a (body did not run) | `NULLRUN_SENSITIVE_FAIL_OPEN=1` -- explicitly documented as "OPEN-when-engine-unavailable" |
| `_enforce_sensitive_tool` (`_fallback_mode=strict`) | CLOSED -- transport returns `decision=block, decision_source=FALLBACK_*` | n/a | none |
| `_emit_span_start` / `_emit_span_end` | n/a -- never blocks | n/a | n/a |
| `/track` batch path (legacy) | OPEN-on-network-error (event dropped, no retry) | n/a -- circuit breaker backoff applies | none |

**Drift fix 2026-07-04:** the SDK_README.md claim
"Fail-OPEN на инфраструктурных сбоях. Если backend недоступен, бюджет
не блокирует агента" is **partially wrong** — it conflates SDK-side
transport failure with backend-side budget-enforcement failure. The
honest split is:

* **SDK-side transport failure** (network timeout, 5xx, breaker open)
  → fail-OPEN on the *check* path so a dead backend doesn't freeze
  the user's agent loop (this is what the README describes).
* **Backend-side budget-enforcement failure** (the /gate or /track
  handler actually returned a wire response, just one indicating a
  Redis outage or aggregate rate-limit Redis unavailable) → the
  wire response is what it is, and the SDK raises the corresponding
  exception. ``BUDGET_REDIS_UNAVAILABLE`` → 402 ``NullRunBudgetError``
  (fail-CLOSED, the backend rejected the request because Redis was
  unreachable for the budget counter — this is the authoritative
  enforcement signal, not a transport blip). ``RATE_LIMIT_REDIS_UNAVAILABLE``
  → 503 ``NullRunRateLimitRedisError`` (fail-CLOSED for the same
  reason). The SDK does NOT silently fall-OPEN on a wire 4xx/5xx
  that names an enforcement failure.

The table above is authoritative; if any of these change, the
README claim must be updated in lockstep.

The "Opt-out" column makes it explicit that `NULLRUN_SKIP_BUDGET_CHECK=1`
is a **different category** of action than
`NULLRUN_SENSITIVE_FAIL_OPEN=1` (bypass vs. change semantics), despite
the similar naming. See `docs/adr/008-sdk-preflight-fail-policy.md`
for the full rules, including transport error classification
(`FALLBACK_NETWORK_ERROR` / `FALLBACK_GATEWAY_ERROR` / `FALLBACK_BREAKER_OPEN`).
"""

import asyncio
import logging
import os
import threading
import time
import uuid
import warnings
from collections.abc import Callable
from typing import Any, Optional

import httpx

from nullrun.actions import ActionHandler, ActionType
from nullrun.breaker.exceptions import (
    BreakerError,
    NullRunAuthenticationError,
    NullRunBlockedException,
    WorkflowKilledInterrupt,
    WorkflowPausedException,
)
from nullrun.context import (
    generate_span_id,
    generate_trace_id,
    get_agent_id,
    get_attempt_index,
    get_span_id,
    get_trace_id,
    get_workflow_id,
)
from nullrun.observability import metrics
from nullrun.transport import (
    HEADER_PROTOCOL,
    NULLRUN_PROTOCOL_VERSION,
    DecisionSource,
    FallbackMode,
    FlushConfig,
    Transport,
    TransportErrorSource,
    _emit_for_transport_error,
    _protocol_header_value,
)
from nullrun.uuid7 import uuid7_str  # 2026-07-04 BUG #4

logger = logging.getLogger(__name__)

# Phase 0.3.1: sentinel used when a gate fires outside a
# ``with workflow(...)`` context. The double-underscore prefix
# namespacing avoids collision with a user workflow that happens
# to be named ``<unknown>`` (the previous literal was a
# collision hazard). Wire compat: still a string.
UNKNOWN_WORKFLOW_ID: str = "__nullrun_unknown__"

# 2026-07-04 (BUG #5): in-process gate cache for chain-mode
# invocations. Without this, every @protect inside `with chain(...)`
# issues a /gate HTTP roundtrip + Redis reserve. For a 100-step
# agent loop that's 100 roundtrips. The gate decision is
# deterministic for a given (workflow_id, chain_id, model) over a
# short window (chain status only changes on `chain_end`), so
# caching the LAST decision for 5s is safe.
#
# Scope: ONLY when chain_id is set. Single-shot (Hard) callers
# must NOT cache — the gate legitimately returns "allow" once and
# "block" on the next call (Hard mode binary), and a stale "allow"
# could let through a budget-exhausted call. Chain-mode callers
# share a budget envelope, so caching "allow" is consistent with
# the chain's semantics.
#
# Opt-out: NULLRUN_GATE_CACHE_DISABLE=1
_GATE_CACHE: dict[tuple[str, str | None, str | None], tuple[float, dict[str, Any]]] = {}
_GATE_CACHE_TTL_SECONDS: float = 5.0

# 2026-07-04 (v0.12.0 wiring fix — ):
# the maximum age (seconds) for a captured ``reservation_id``
# to be eligible for forwarding onto a /track payload. Past
# this age the underlying ``reservation:{execution_id}`` Redis
# key has expired (300s TTL per) — forwarding would
# guarantee a 503 ``RESERVATION_NOT_FOUND`` on /track. The
# 5s margin below the 300s TTL absorbs clock-skew between
# the SDK's ``time.monotonic `` and the Redis cluster's own
# TTL decay (sub-second typically, but the safety budget is
# worth the simplicity of a hard-coded threshold).
SERVER_MINTED_RESERVATION_MAX_AGE_SECONDS: float = 295.0

# Phase 4.1: privacy boundary. Fields that MUST NOT leave the SDK on
# the wire. The transport layer (POST /api/v1/track/batch) reads
# whatever is in the event dict, so anything not allowlisted ends up
# in the user's audit log on the backend side. We strip:
#
# * ``cost_cents`` -- the SDK does not estimate cost; the backend
# recomputes it from tokens + the org's pricing policy. Sending
# a wrong number risks double-billing when the backend also
# persists its own computed cost.
# * ``_fingerprint`` -- the dedup key (sha256[:16] over the raw
# response body). Process-local; leaking it to audit logs
# would let an operator with audit-log read access fingerprint
# which prompts went through dedup, defeating the purpose.
# * ``raw_usage`` -- the vendor's full usage dict (OpenAI
# ``prompt_tokens_details``, Anthropic ``cache_*_input_tokens``
# etc.) — Phase 4.1 moved every field we care about out of
# raw_usage onto the event itself, so the original dict is now
# just an opaque blob of provider-specific data. Carrying it on
# the wire is a privacy regression: provider response payloads
# can include user-supplied metadata, organization names, or
# other PII the backend has no business logging.
#
# Anything new added here MUST also be added to the in-process
# callers that consume these fields (the dedup LRU at
# ``_seen_track_fingerprints``, any local loggers).
_WIRE_STRIP_FIELDS: frozenset[str] = frozenset(
    {"cost_cents", "_fingerprint", "raw_usage"}
)


class NullRunRuntime:
    """
    Central runtime for NullRun SDK.

    This is a singleton that manages:
    - Authentication state (organization_id)
    - Cached policies from backend
    - Event buffering and batched transport
    - Local policy enforcement

    Usage:
        # Automatic (via protect )
        import nullrun
        nullrun.protect 

        # Manual
        rt = NullRunRuntime.get_instance 
        # Note: `cost_cents` is NOT a valid event key — the SDK strips
        # it before sending (see ``track_event`` / wire payload below).
        # The backend computes cost from tokens + the org's pricing
        # policy. Use ``tokens`` (or, for llm_call specifically
        # ``input_tokens`` / ``output_tokens``) to feed cost math.
        rt.track({"type": "llm_call", "tokens": 100})
    """

    _instance: Optional["NullRunRuntime"] = None
    _lock = threading.Lock()

    def __init__(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        api_url: str = "https://api.nullrun.io",
        fallback_mode: str | None = None,
        debug: bool = False,
        _test_mode: bool = False,
        polling: bool = True,
    ):
        """
        Initialize NullRun Runtime.

        Args:
            api_key: API key from NullRun dashboard. If None, reads from
                     NULLRUN_API_KEY env variable. If both None, uses local mode.
            secret_key: Secret key for HMAC request signing. If None, no signing.
            api_url: URL of NullRun proxy server. Defaults to https:/api.nullrun.io.
            debug: Enable debug logging.
            _test_mode: Internal flag to skip network calls (for testing).
            polling: Internal flag for tests/CI to skip the background
                     control-plane listener (WS or HTTP poll). Defaults True
                     in production. Set False when the test environment
                     cannot tolerate a background thread opening sockets.

        Note:
            - `organization_id` is set from `_authenticate ` after init; it is
              NOT a public init parameter and not read from env.
            - `api_key` is required as of 0.3.0 (T3-S2). The previous
              `local_mode` flag was removed because it silently bypassed
              every backend gate.
            - `fallback_mode` is fixed at PERMISSIVE (no public override).
            - `timeout`/`max_retries` are fixed at 30s / 3 (no public override).

        Raises:
            NullRunAuthenticationError: if neither `api_key` nor
                `NULLRUN_API_KEY` is set. The public `init ` surface
                performs the same check first and produces a clearer
                error message; this constructor-level raise is the
                direct fallback for tests and advanced callers that
                build the runtime by hand.
        """
        self.api_key = api_key or os.getenv("NULLRUN_API_KEY")
        self.secret_key = secret_key or os.getenv("NULLRUN_SECRET_KEY")
        self.api_url = api_url or os.getenv("NULLRUN_API_URL", "https://api.nullrun.io")

        # T3-S2 (0.3.0): api_key is now required. The previous `local_mode`
        # flag silently bypassed every backend gate (budget, policy
        # control plane), which was a real safety hole in production.
        # We raise NullRunAuthenticationError here instead so the
        # misconfiguration is caught at startup. The public `init `
        # surface raises first with a clearer message; this is the
        # direct construction path used by tests and advanced callers.
        if not self.api_key:
            raise NullRunAuthenticationError(
                "NullRunRuntime() requires an api_key. Pass api_key='nr_live_...' "
                "or set NULLRUN_API_KEY. (Silent no-op fallback was removed "
                "in 0.3.0 -- see CHANGELOG.)"
            )
        # organization_id is set by _authenticate; stays None until then.
        self.organization_id: str | None = None
        # Phase 139+: workflow_id is set by _authenticate from the API
        # key's binding (organization_api_keys.workflow_id). Used as a
        # fallback for /check, /status, and span events when the user
        # hasn't entered a `with workflow(...)` context. None on legacy
        # keys (pre-139 or never used) -- call sites must NOT invent one.
        self.workflow_id: str | None = None

        self._test_mode = _test_mode
        self.polling = polling

        # The string ``fallback_mode`` parameter is deprecated and
        # accepted only for backward compat — the CACHED variant
        # was removed in 0.7.0 because the SDK no longer maintains
        # a local policy cache (see CHANGELOG D-01).
        fb_upper = str(fallback_mode).upper() if fallback_mode is not None else "PERMISSIVE"
        if fb_upper == "STRICT":
            self._fallback_mode = FallbackMode.STRICT
        else:
            self._fallback_mode = FallbackMode.PERMISSIVE
        self._timeout = 30
        self._max_retries = 3
        self._debug = debug
        self._transport: Transport | None = None

        # Local enforcement state
        # Phase 0.3.1: the BoundedDict-based per-workflow cost /
        # loop / retry counters have been removed alongside
        # ``_check_local_limits``. As of 0.7.0 ALL local
        # enforcement (LoopTracker / RateTracker / _local_check /
        # hardcoded thresholds) has been removed -- the SDK is a
        # thin client, the backend is authoritative.
        self._workflow_start_time: float = time.time()

        # Layer 3: ring buffer for the ``nullrun.status `` recent
        # errors list. Capacity 10 — bounded so a long-lived process
        # does not leak memory even if the SDK raises thousands of
        # errors per minute. Fed by ``_record_error`` (called from
        # ``_emit_sdk_error`` after the Layer-2 ``emit_error``).
        from nullrun.observability.status import _RecentErrorRing

        self._recent_errors = _RecentErrorRing(capacity=10)

        # Layer 3: backend connectivity timestamps for the status
        # snapshot. Set in ``_authenticate`` and updated on every
        # successful / failed backend call thereafter.
        self._last_backend_attempt_at: float | None = None
        self._last_backend_attempt_ok: bool | None = None

        # Phase D: dedup LRU. Multiple observation paths (httpx transport
        # LangChain callback, OpenAI Agents tracer) can fire for the same
        # LLM call. We collapse them to a single track per fingerprint.
        # The fingerprint is computed at the observation point and passed
        # via the `_fingerprint` event field.
        from nullrun.instrumentation.auto import make_dedup_state

        self._seen_track_fingerprints = make_dedup_state()

        # Per ADR-008 the SDK does not track local cost. The two response
        # fields below are kept in the return shape for backwards
        # compatibility with 0.3.x callers but always read 0. The previous
        # implementation read from `self._workflow_costs` (a BoundedDict
        # removed in 0.3.1) which left `track ` raising AttributeError on
        # first call.
        self._local_cost_cents_estimate: int = 0

        # 0.9.0: coverage counters removed. Coverage is now derived
        # server-side from the llm_call span metadata (`tracked` and
        # `streaming_skipped` flags set by the instrumentation layer).
        # The previous per-host dicts and 60s daemon thread are gone.

        # Remote control plane state (per-workflow, pushed from server via WS).
        # Unified model: effective_state = max(local_state, remote_state).
        # All writes and reads go through the `_remote_state_for` /
        # `_set_remote_state` helpers (Phase 5 #5.1) so the WS callback
        # the HTTP poll, and the gate check can run concurrently
        # without a TOCTOU race. RLock because the same thread can
        # re-enter via the gate's get-then-set sequence.
        self._remote_states: dict[str, dict[str, Any]] = {}
        self._states_lock = threading.RLock()

        # Phase B: control plane transport. The SDK connects to the server's
        # WS endpoint and receives state push events (killed/paused) within
        # ~100ms of the operator action -- vs the previous 1s HTTP poll.
        # The HTTP poll path is preserved as a fallback when
        # `NULLRUN_TRANSPORT=http` is set (env var defaults to `ws`).
        self._transport_mode: str = os.getenv("NULLRUN_TRANSPORT", "ws").lower()
        self._ws_thread: threading.Thread | None = None
        self._ws_stop_event = threading.Event()
        self._ws_connection: Any = None  # WebSocketConnection; typed loosely to avoid import cycle
        self._ws_loop: Any = None  # asyncio loop running in the WS thread
        # Legacy HTTP-poll state -- only used when transport mode is `http`.
        self._poll_thread: threading.Thread | None = None
        self._poll_running = False

        # Action handling
        self._action_handler: ActionHandler | None = None

        # Initialize transport FIRST (before auth/policy) so we can reuse its client
        # Transport will be started later after auth/policy succeed
        self._transport = Transport(
            api_url=self.api_url,
            api_key=self.api_key,
            secret_key=self.secret_key,
            config=FlushConfig(
                batch_size=50,
                flush_interval=5.0,
            ),
        )

        # Note: a gRPC transport was prototyped in earlier SDK versions but the
        # gRPC server at the platform is intentionally frozen until the
        # activation checklist (TLS, auth, proto extensions, cost pipeline
        # parity, tests) is complete. The SDK no longer attempts to construct
        # a gRPC client.
        # FIX 2026-06-28: was a silent no-op (logger.info) — customers who
        # set NULLRUN_USE_GRPC expecting gRPC silently fell back to HTTP with
        # no signal. Now we raise loudly so the misconfiguration is visible
        # at startup instead of being diagnosed from a missing proto trace.
        if os.getenv("NULLRUN_USE_GRPC"):
            raise RuntimeError(
                "NULLRUN_USE_GRPC is set but the gRPC transport is not "
                "yet implemented. This option is reserved for a future "
                "release. Unset the env var to use the HTTP transport. "
                "See https://docs.nullrun.io/reference/sdk-api#transport"
            )

        # Initialize
        if self._test_mode:
            # Test mode: skip all network calls
            self._transport.start()
        else:
            try:
                self._authenticate()
            except NullRunAuthenticationError:
                raise  # Re-raise auth errors immediately - don't continue in unprotected mode
            except httpx.RequestError as e:
                raise NullRunAuthenticationError(
                    f"Auth request failed: {e}. Cannot establish secure connection to NullRun. "
                    f"Refusing to operate in unprotected mode."
                ) from e
            self._transport.start()
            # Start remote polling unless disabled (internal `polling=False`
            # for tests/CI). Production always polls.
            if self.polling:
                self._start_remote_polling()

        # Initialize action handler
        self._action_handler = ActionHandler()

        # Phase 1.4: Sensitive tools that require strict mode (pre-execution enforcement)
        # These tools MUST go through /execute endpoint, NOT direct execution
        self._sensitive_tools: set = {
            # Financial operations
            "stripe.charge",
            "stripe.refund",
            "stripe.payout",
            "payment.process",
            # Email / communication
            "send_email",
            "send_sms",
            "send_slack",
            "send_discord",
            # Database operations
            "db.delete",
            "db.drop",
            "db.truncate",
            "db.write",
            # External API calls
            "api.post",
            "api.put",
            "api.delete",
            # File operations
            "file.delete",
            "file.write",
            "s3.delete",
            # Admin operations
            "admin.delete",
            "admin.create_user",
            "admin.disable_user",
        }
        self._strict_mode_tools: set[str] = set()
        # lock that guards every mutation of the
        # sensitive-tools sets. The pre-fix code did
        # ``self._strict_mode_tools.add(tool_name)`` from
        # ``add_sensitive_tool`` without holding any lock; the
        # reader in ``is_sensitive_tool`` (line 1270-ish) did
        # ``tool_name in self._strict_mode_tools`` without a lock.
        # Under CPython's GIL the set mutation is atomic at the
        # bytecode level, but the snapshot you read can still be
        # stale mid-mutation (a single-threaded read can see the
        # new value fine, but a multi-threaded read can race with
        # a concurrent ``add`` if both interleave on a free-threaded
        # build). The lock is uncontended on the read path so the
        # cost is one acquire per call.
        self._tools_lock = threading.Lock()

        logger.info("NullRun Runtime initialized: mode=cloud")

    @classmethod
    def get_instance(cls) -> "NullRunRuntime":
        """Get the singleton runtime instance.

        Thread-safe: the singleton lock is held for the full read-compare-
        rebuild sequence (Phase 5 #5.3). The previous version dropped the
        lock between shutdown and the recursive get_instance, creating a
        window where a concurrent caller could observe a half-shutdown
        runtime.
        """
        with cls._lock:
            # Re-read env vars at every call site so credential rotation
            # is observed on the next get_instance invocation.
            api_key = os.getenv("NULLRUN_API_KEY")
            api_url = os.getenv("NULLRUN_API_URL", "https://api.nullrun.io")

            if cls._instance is None:
                cls._instance = cls(api_key=api_key, api_url=api_url)
                return cls._instance

            existing = cls._instance
            key_changed = api_key != existing.api_key
            url_changed = api_url != existing.api_url

            if key_changed or url_changed:
                logger.info(
                    f"Credentials changed: api_key={'***' if key_changed else 'unchanged'}, "
                    f"api_url={'changed' if url_changed else 'unchanged'} - reinitializing"
                )
                existing.shutdown()
                cls._instance = cls(api_key=api_key, api_url=api_url)
                return cls._instance

            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton. Mainly for testing."""
        with cls._lock:
            if cls._instance is not None:
                cls._instance.shutdown()
            cls._instance = None

    def status(self) -> "Any":
        """Build a Layer-3 ``NullRunStatus`` snapshot.

        Synchronous, thread-safe, side-effect-free — safe to
        call from the agent loop, the transport flush thread
        or a debug console. The returned dataclass is frozen
        so it can be cached, shared, and compared with ``==``.

        State-derivation rules (see
        ``nullrun/observability/status.py`` for the full
        rationale):

        * ``misconfigured`` — no api_key, or runtime never
          bound to an org.
        * ``offline`` — backend not reachable AND no cached
          policy. SDK is running in strict-local fallback.
        * ``degraded`` — using cached policy, OR WS
          disconnected, OR circuit breaker open, OR workflow
          state != Normal. SDK is operating with reduced
          guarantees.
        * ``ok`` — everything healthy.
        """
        from datetime import datetime, timezone

        from nullrun.observability.status import (
            STATE_DEGRADED,
            STATE_MISCONFIGURED,
            STATE_OFFLINE,
            STATE_OK,
            NullRunStatus,
            RecentError,
            WorkflowState,
        )

        # --- Auth state ---
        api_key_valid: bool | None = None
        api_key_prefix: str | None = self.api_key[:10] if self.api_key else None
        if self.organization_id is not None:
            # If we have an org, auth at least started — it
            # may have failed (we'd be in misconfigured), but
            # in the normal flow org binding means a 200 came
            # back from /auth/verify.
            api_key_valid = True

        # --- Connectivity ---
        backend_reachable: bool | None = None
        if self._last_backend_attempt_at is not None:
            # ``_last_backend_attempt_ok`` is set to True on
            # a successful HTTP response, False on a transport
            # error. ``None`` if no attempt since init.
            backend_reachable = self._last_backend_attempt_ok

        ws_connected: bool | None = None
        if self._ws_connection is not None:
            # ``is_open`` is the underlying websockets flag
            # None when the connection has never been
            # successfully established.
            ws_connected = getattr(self._ws_connection, "is_open", None)
        elif self._ws_stop_event.is_set():
            ws_connected = False  # explicit shutdown

        # --- Workflow state from last WS push ---
        workflow_state: WorkflowState | None = None
        if self.workflow_id is not None:
            cached = self._remote_state_for(self.workflow_id)
            if cached:
                state_str = cached.get("state", "Normal")
                workflow_state = WorkflowState(
                    workflow_id=self.workflow_id,
                    state=state_str,
                    version=cached.get("version", 0),
                    reason=cached.get("reason"),
                )

        # --- Recent errors ---
        recent_errors = self._recent_errors.snapshot()

        # --- Headline state derivation ---
        # Order matters: most specific first.
        if self.api_key is None or (
            self.organization_id is None and self._last_backend_attempt_at is not None
        ):
            headline = STATE_MISCONFIGURED
        elif (
            ws_connected is False
            or backend_reachable is False
            or (workflow_state is not None and workflow_state.state != "Normal")
        ):
            headline = STATE_DEGRADED
        else:
            headline = STATE_OK

        return NullRunStatus(
            state=headline,
            api_key_valid=api_key_valid,
            api_key_prefix=api_key_prefix,
            organization_id=self.organization_id,
            workflow_id=self.workflow_id,
            api_url=self.api_url,
            backend_reachable=backend_reachable,
            ws_connected=ws_connected,
            workflow_state=workflow_state,
            recent_errors=recent_errors,
        )

    def _record_error(
        self,
        err: "BaseException",
        stage: str,
        *,
        workflow_id: str | None = None,
        tool_name: str | None = None,
    ) -> None:
        """Layer 3: append a ``RecentError`` to the runtime's
        ring buffer. Called from ``_emit_sdk_error`` AFTER the
        Layer-2 ``emit_error`` so both layers see the same
        error. The ring buffer feeds ``NullRunStatus.recent_errors``
        — the user sees the last N errors via
        ``nullrun.status `` without instrumenting every
        call site.
        """
        from datetime import datetime, timezone

        from nullrun.observability.status import RecentError

        # Resolve workflow_id from the contextvar when the
        # caller did not pass one — same precedence as
        # ``_emit_sdk_error``.
        resolved_workflow_id = workflow_id
        if resolved_workflow_id is None and self.workflow_id is not None:
            resolved_workflow_id = self.workflow_id

        self._recent_errors.push(
            RecentError(
                error_code=getattr(err, "error_code", "NR-0000"),
                stage=stage,
                workflow_id=resolved_workflow_id,
                tool_name=tool_name,
                timestamp=datetime.now(tz=timezone.utc),
                message=str(err)[:200],
            )
        )

    def _emit_sdk_error(
        self,
        err: "BaseException",
        stage: str,
        *,
        workflow_id: str | None = None,
        tool_name: str | None = None,
        correlation_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Layer 2: fire the on_error hook with the runtime's known
        context fields. Called from every raise site immediately
        BEFORE the ``raise`` statement so the hook sees the
        fully-constructed exception while the call stack is still
        live.

        Best-effort: this method NEVER raises. The hook itself is
        wrapped in ``emit_error`` which catches hook exceptions.
        A failure inside the hook cannot break the SDK.

        Layer 3: also appends to the runtime's recent-errors
        ring buffer so ``nullrun.status `` surfaces the error
        without the user having to register a hook. Done AFTER
        the hook dispatch (so the ring buffer does not delay
        the hook) and AFTER the call-stack is built (so the
        ring buffer sees the resolved workflow_id).

        Hot path: the no-hooks case is skipped via ``has_hooks ``
        so the call cost when nobody is listening is one boolean
        check + an attribute access on ``self`` (no allocation
        no lock — the hook registry short-circuits inside
        ``emit_error``). The Layer-3 ring-buffer push is ALWAYS
        done — it is the no-instrumentation path to introspection.
        """
        from nullrun.observability.error_hooks import (
            ErrorContext,
            emit_error,
            has_hooks,
        )

        # Layer 3 (cheap path): always push to the ring buffer
        # BEFORE the hook dispatch so a failing hook cannot
        # prevent the error from appearing in ``nullrun.status ``.
        self._record_error(
            err,
            stage,
            workflow_id=workflow_id,
            tool_name=tool_name,
        )

        if not has_hooks():
            return
        # Lazy-resolve workflow_id: the contextvar (set by
        # ``nullrun.workflow(...)`` blocks) is authoritative for
        # in-loop calls, falling back to the runtime's bound
        # workflow when no contextvar is active.
        resolved_workflow_id = workflow_id
        if resolved_workflow_id is None and self.workflow_id is not None:
            resolved_workflow_id = self.workflow_id
        emit_error(
            err,
            ErrorContext(
                stage=stage,
                workflow_id=resolved_workflow_id,
                tool_name=tool_name,
                api_key_prefix=(self.api_key[:10] if self.api_key else None),
                correlation_id=correlation_id,
                extra=extra or {},
            ),
        )

    def _authenticate(self) -> None:
        """Authenticate with API key and get organization_id.

        Also handles key version updates for HMAC secret key rotation.
        On successful auth, the server may return a new key_version indicating
        a secret key rotation. The SDK stores this and uses it for signing.
        """
        if not self.api_key:
            from nullrun.breaker.exceptions import NullRunConfigError

            err = NullRunConfigError(
                "API key required for cloud mode",
                error_code="NR-C001",
                user_action=(
                    "Set NULLRUN_API_KEY env var or pass api_key='nr_live_...' "
                    "to nullrun.init(). The SDK cannot operate without "
                    "credentials — the no-op local mode was removed in 0.3.0."
                ),
            )
            self._emit_sdk_error(err, stage="auth")
            raise err

        logger.debug(f"Authenticating with API at {self.api_url}/auth/verify")
        try:
            # 2026-06-28 audit P2.3: retry transient 503/504 + network blips
            # during init. Backend emits 503 + Retry-After: 5 on transient
            # DB error (backend/src/proxy/handlers.rs:11346-11351). Pre-fix
            # the first 503 surfaced as NR-A001 to the user as if their API
            # key were bad. Three attempts, exponential backoff (0.5s → 1s
            # → 2s), honor Retry-After when present. Auth-key failures (401)
            # are NOT retried — the key is wrong on attempt 1 means it's
            # wrong on attempt 3.
            response = self._post_auth_with_retry(
                f"{self.api_url}/api/v1/auth/verify",
                json_body={"api_key": self.api_key},
                max_attempts=3,
            )

            if response.status_code == 200:
                data = response.json()
                # STRICT MODE: organization_id is REQUIRED, no fallback
                org_id = data.get("organization_id")
                if not org_id:
                    err = NullRunAuthenticationError(
                        "Auth response missing organization_id - server may be outdated or compromised. "
                        "Refusing to operate with legacy identity.",
                        error_code="NR-A002",
                        user_action=(
                            "The NullRun backend returned a 200 but the response "
                            "is missing organization_id. This usually means the "
                            "backend is on an older version than the SDK expects — "
                            "update the backend, or downgrade the SDK to a "
                            "version compatible with the deployed backend."
                        ),
                    )
                    self._emit_sdk_error(err, stage="auth")
                    raise err
                self.organization_id = org_id

                # Phase 139+: pick up the workflow this key is bound to.
                # `None` on legacy keys (pre-139 or never-used) -- call
                # sites that NEED a workflow (check_workflow_budget
                # check_control_plane, span events) will fall through to
                # the contextvar when self.workflow_id is None, exactly
                # like before. New keys always have this set.
                self.workflow_id = data.get("workflow_id")

                # Phase 0.3.1: pre-Phase-139 API keys do not return
                # workflow_id, so the SDK cannot honour the
                # dashboard's KILL/PAUSE for that workflow. Emit a
                # one-time WARNING so the operator knows to rotate
                # the key. Without this, the kill switch silently
                # no-ops (a real safety hole for legacy users).
                if self.workflow_id is None:
                    masked_key = (
                        (self.api_key[:8] + "***")
                        if self.api_key and len(self.api_key) >= 8
                        else "***"
                    )
                    logger.warning(
                        f"API key {masked_key!s} is a legacy key with no "
                        f"workflow binding; remote kill/pause will not be "
                        f"honoured. Rotate to a Phase 139+ key in the "
                        f"dashboard to enable control plane enforcement."
                    )

                # Handle key rotation: server may return new key_version and secret_key
                # This allows seamless secret key rotation without downtime
                new_key_version = data.get("key_version")
                new_secret_key = data.get("secret_key")

                if new_key_version is not None and new_secret_key is not None:
                    old_version = getattr(self, "_key_version", None)
                    if old_version != new_key_version:
                        logger.info(
                            f"Secret key rotation: version {old_version} -> {new_key_version}"
                        )
                    self._key_version = new_key_version
                    self.secret_key = new_secret_key
                    # Update transport's secret key for subsequent requests
                    self._transport.secret_key = new_secret_key

                logger.info(f"Authenticated: organization_id={self.organization_id}")
            else:
                # Auth failed - raise exception instead of silent fallback
                err = NullRunAuthenticationError(
                    f"Auth failed with status {response.status_code}. "
                    f"API key may be invalid or expired. Not operating in unsafe mode.",
                    error_code=("NR-A003" if response.status_code == 401 else "NR-A001"),
                )
                self._emit_sdk_error(
                    err,
                    stage="auth",
                    correlation_id=response.headers.get("x-correlation-id"),
                    extra={"status_code": response.status_code},
                )
                raise err
        except httpx.RequestError as e:
            # Network error - raise exception, do not fall back silently
            err = NullRunAuthenticationError(
                f"Auth request failed: {e}. Cannot establish secure connection to NullRun. "
                f"Refusing to operate in unprotected mode.",
                error_code="NR-B001",
                user_action=(
                    "Could not reach the NullRun backend at "
                    f"{self.api_url}. Check network connectivity and the "
                    "configured api_url. This is a transport failure (not "
                    "an auth failure) — the API key may be valid, the "
                    "backend is just unreachable."
                ),
                cause=e,
            )
            self._emit_sdk_error(err, stage="auth")
            raise err from e

    def _start_transport(self) -> None:
        """Start the transport layer with background flush.

        Note: Transport is already created in __init__ before auth/policy.
        This method only starts it.
        """
        if self._transport:
            self._transport.start()

    def _start_remote_polling(self) -> None:
        """Start the control-plane background listener.

        Phase B: defaults to WebSocket push for sub-second kill/pause
        propagation. Set `NULLRUN_TRANSPORT=http` to fall back to the
        legacy 1-second HTTP poll (kept for environments where the WS
        endpoint is blocked or for parity with old SDK behavior).
        """
        if self._transport_mode == "http":
            self._start_http_poller()
        else:
            self._start_ws_listener()

    def _start_http_poller(self) -> None:
        """Legacy: poll the server every second for state changes."""
        self._poll_running = True
        self._poll_thread = threading.Thread(
            target=self._poll_commands, daemon=True, name="nullrun-poller"
        )
        self._poll_thread.start()
        logger.info("Started remote state poller (HTTP)")

    def _start_ws_listener(self) -> None:
        """Phase B: connect the WebSocket push channel in a background thread.

        The thread runs its own asyncio loop so the WS receive task can
        drive `_remote_states` from server pushes without contending with
        the user's main loop. Reconnects with exponential backoff on
        disconnect (handled inside `WebSocketConnection`).
        """
        if not self.organization_id:
            logger.warning(
                "Cannot start WS control plane: organization_id is unset. "
                "Falling back to HTTP poll."
            )
            self._start_http_poller()
            return

        self._ws_stop_event.clear()
        self._ws_thread = threading.Thread(
            target=self._ws_run,
            daemon=True,
            name="nullrun-ws",
        )
        self._ws_thread.start()
        logger.info("Started WS control plane listener (org=%s)", self.organization_id)

    def _ws_run(self) -> None:
        """Background thread entry point: run the WS connect/receive loop.

        On any exception (connect refused, network drop, auth failure)
        we wait on the stop event with a small backoff so the next
        `_start_ws_listener` can take over without busy-looping.
        """
        try:
            import asyncio

            self._ws_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._ws_loop)
            try:
                self._ws_loop.run_until_complete(self._ws_connect_and_serve())
            finally:
                self._ws_loop.close()
                self._ws_loop = None
        except Exception as e:  # noqa: BLE001 -- background thread, must never die silently
            logger.warning(f"WS control plane thread exited: {e}")
        finally:
            self._ws_connection = None

    async def _ws_connect_and_serve(self) -> None:
        """Connect the WS once and serve messages until stop is signalled.

        Uses `connect_websocket` from the existing transport, which handles
        HMAC, ACK, and reconnect internally. We just need to install the
        state-change callback that updates `_remote_states`.
        """
        if not self._transport:
            logger.warning("WS control plane: transport not initialized, aborting")
            return

        def on_state_change(state: dict[str, Any]) -> None:
            """Push state into `_remote_states` so `check_control_plane`
            sees it on the next gate call. The push is synchronous (just
            a dict write) so latency from server → gate is bounded only
            by network + event-loop scheduling.
            """
            try:
                workflow_id = state.get("workflow_id")
                if not workflow_id:
                    logger.debug("WS state message missing workflow_id: %s", state)
                    return
                self._set_remote_state(
                    workflow_id,
                    {
                        "state": state.get("state", "Normal"),
                        "version": state.get("version", 0),
                        "reason": state.get("reason"),
                        "updated_at": state.get("updated_at", 0),
                    },
                )
                logger.debug(
                    "WS state push: workflow=%s state=%s reason=%s",
                    workflow_id,
                    self._remote_states[workflow_id]["state"],
                    self._remote_states[workflow_id]["reason"],
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"WS state callback error: {e}")

        try:
            conn = await self._transport.connect_websocket(
                organization_id=self.organization_id,
                on_state_change=on_state_change,
            )
            self._ws_connection = conn
        except Exception as e:
            logger.warning(f"WS control plane connect failed: {e}")
            return

        # Block until the connection closes (e.g. server disconnect).
        try:
            if conn._receive_task is not None:  # type: ignore[attr-defined]
                await conn._receive_task  # type: ignore[attr-defined]
        except Exception as e:
            logger.debug(f"WS receive loop ended: {e}")
        finally:
            try:
                await conn.close()
            except Exception:
                pass
            self._ws_connection = None

    def _poll_commands(self) -> None:
        """
        Poll server for per-workflow control plane state.

        This runs in a background thread and updates _remote_states
        with the latest state from the server.
        """
        while self._poll_running:
            try:
                # Get all workflows we're tracking
                workflow_ids = list(self._remote_states.keys())
                if not workflow_ids:
                    # If no workflows yet, try to get organization workflows
                    pass

                for workflow_id in workflow_ids:
                    self._fetch_remote_state(workflow_id)

            except Exception as e:
                logger.debug(f"Polling error: {e}")

            time.sleep(1.0)  # Poll every second

    def _resolve_workflow_id(self, explicit: str | None = None) -> str | None:
        """
        Resolve the effective workflow_id for /check, /status, and span
        events. Order of precedence:

          1. `explicit` -- passed by the call site (e.g. contextvar in
             track_event or the user-supplied arg in check_control_plane)
          2. `self.workflow_id` -- bound to the API key by the server
             (Phase 139+). Set during _authenticate. None on legacy
             keys.
          3. None -- caller is in cloud mode but has no workflow scope.
             /check falls through to org-level policy; /status is
             skipped; span events are emitted without workflow_id
             (orphan, as before).

        The SDK does NOT auto-generate a workflow_id. The Phase 139
        invariant -- workflow is derived server-side from the key, never
        invented by the SDK -- is preserved.
        """
        if explicit:
            return explicit
        return self.workflow_id

    def _remote_state_for(self, workflow_id: str) -> dict[str, Any]:
        """Return the cached remote state for `workflow_id` (Phase 5 #5.1).

        Thread-safe via `_states_lock`. If no state has been pushed
        yet, returns an empty dict (so callers can do
        ``state.get("state", "Normal")`` without an extra check).
        """
        with self._states_lock:
            st = self._remote_states.get(workflow_id)
            if st is None:
                st = {}
                self._remote_states[workflow_id] = st
            return st

    def _set_remote_state(self, workflow_id: str, state: dict[str, Any]) -> None:
        """Atomically replace the cached remote state for `workflow_id`."""
        with self._states_lock:
            self._remote_states[workflow_id] = dict(state)

    def _fetch_remote_state(self, workflow_id: str) -> None:
        """Fetch remote state for a specific workflow.

        2026-06-27: target endpoint swapped from
        ``GET /api/v1/orgs/{org_id}/workflows/{workflow_id}`` (the
        DASHBOARD route — requires Bearer session cookie, returns 401
        to SDK clients that only send X-API-Key) to
        ``GET /api/v1/status/{workflow_id}`` (the SDK-polling route —
        backend/src/proxy/handlers.rs:9758, accepts X-API-Key OR
        Authorization: Bearer). Pre-swap the HTTP-poll path silently
        401'd on every poll, so the legacy HTTP-poll fallback never
        observed a remote kill/pause. WS push (the default mode since
        Phase 5) does NOT go through this code path, so the WS control
        plane is unaffected.

        Backend ``StatusResponse`` (handlers.rs:9747-9756) returns
        ``workflow_id, state, version, reason?, updated_at
        current_cost, rate_per_minute``. We only consume ``state`` —
        ``version`` and ``reason`` are SDK-local fields and remain at
        their cached values (mirroring the prior behaviour). This is
        sufficient for ``check_control_plane`` which only reads
        ``state``.
        """
        try:
            response = self._transport._client.get(
                f"{self.api_url}/api/v1/status/{workflow_id}",
                headers=self._auth_headers(),
                timeout=5.0,
            )
            if response.status_code == 200:
                data = response.json()
                # Merge with existing cached state so version / reason /
                # updated_at (SDK-local fields not on the wire) survive.
                cached = self._remote_state_for(workflow_id)
                self._set_remote_state(
                    workflow_id,
                    {
                        **cached,
                        "state": data.get("state", cached.get("state", "Normal")),
                    },
                )
                logger.debug(
                    "Remote state for %s: %s",
                    workflow_id,
                    self._remote_state_for(workflow_id),
                )
        except Exception as e:
            logger.debug(f"Failed to fetch remote state for {workflow_id}: {e}")

    def check_control_plane(self, workflow_id: str) -> None:
        """
        Check remote control plane state and raise if workflow is paused/killed.

        This is called in the execution path after local enforcement.
        The unified state model: effective_state = max(local_state, remote_state)

        Raises:
            WorkflowPausedException: If workflow is paused on server
            WorkflowKilledInterrupt: If workflow is killed on server
        """
        # Phase 139+: prefer the explicit arg (contextvar-supplied), fall
        # back to the API key's bound workflow. None on legacy keys --
        # in that case there's no workflow to check, so we no-op
        # (preserves pre-139 behavior for keys that have never been
        # workflow-bound).
        resolved = self._resolve_workflow_id(workflow_id or None)
        if not resolved:
            return
        workflow_id = resolved

        # Ensure we have the latest remote state
        # Phase 5 #5.1: use the lock-protected getter so a concurrent
        # WS push can't drop the state between the membership check
        # and the read.
        remote_state = self._remote_state_for(workflow_id)
        if not remote_state:
            # Fetch synchronously if not in cache yet
            self._fetch_remote_state(workflow_id)
            remote_state = self._remote_state_for(workflow_id)
        state = remote_state.get("state", "Normal")

        # S-4: case-insensitive compare. The backend
        # already emits PascalCase via the `as_pascal_case ` normaliser
        # in `handlers.rs:9258`, but a future regression to UPPERCASE
        # (or any other casing) would silently fail the match and let a
        # killed workflow keep running. Normalise here so the SDK
        # survives any wire-format drift without needing a coordinated
        # backend change.
        state_normalized = state.lower() if isinstance(state, str) else "normal"

        if state_normalized == "paused":
            reason = remote_state.get("reason", "remote pause")
            raise WorkflowPausedException(
                workflow_id=workflow_id,
                reason=reason,
            )
        elif state_normalized == "killed":
            reason = remote_state.get("reason", "remote kill")
            raise WorkflowKilledInterrupt(
                workflow_id=workflow_id,
                reason=reason,
            )

    def check_workflow_budget(self) -> None:
        """
        Pre-flight budget check via /api/v1/gate. Called from @protect
        before the wrapped function runs, so a workflow with no remaining
        budget never gets to spend tokens.

        Sprint 3.1: bumps the ``check_calls`` metric so the dashboard
        can show the rate of pre-flight budget checks.

        Decision → exception mapping:
            "block" → WorkflowKilledInterrupt (hard policy / reservation error)
            "throttle"→ WorkflowPausedException (insufficient budget, can resume)
            "allow" → return

        Fail-OPEN: any transport error (network, timeout, 5xx) is logged
        at warning level and the caller proceeds. This mirrors the
        pattern in `check_control_plane` -- a transient backend outage
        must never freeze the user's agent. The /track fast path also
        does not gate on budget, so the worst case under /gate failure
        is that we revert to the pre-C behaviour: budget enforcement is
        advisory until the gateway recovers.

        Uses `estimated_tokens=1` (the minimum the API accepts). Goal
        is the binary question "is there any budget left?", not cost
        prediction -- the backend recomputes the authoritative cost on
        /track from the real token count.

        Opt-out: set `NULLRUN_SKIP_BUDGET_CHECK=1` to disable the
        pre-flight. Useful in tests where the org's API key has
        exhausted its budget from previous runs and the test only
        wants to exercise a non-budget code path.
        """
        if os.environ.get("NULLRUN_SKIP_BUDGET_CHECK", "").strip() == "1":
            logger.debug("check_workflow_budget: skipped via NULLRUN_SKIP_BUDGET_CHECK=1")
            return

        # Sprint 3.1 (B23): bump the ``check_calls`` counter so the
        # dashboard can show the rate of pre-flight budget checks
        # and the operator can verify the pre-flight is actually
        # running (not silently always-skipped).
        metrics.inc_runtime("check_calls")

        from nullrun.context import (
            get_call_model,
            get_call_tools,
            get_chain_id,
            get_chain_op,
            get_workflow_id,
        )

        # Phase 139+: prefer the user-set contextvar (explicit `with
        # workflow(...)` block), fall back to the API key's bound
        # workflow. Returns None only on legacy keys that have never
        # been workflow-bound -- in that case the check is silently
        # skipped, exactly as before this change.
        workflow_id = self._resolve_workflow_id(get_workflow_id())
        if not workflow_id:
            return

        # T4 (2026-06-27): use the real model name from the call
        # context if the user set it via `set_call_context(model=...)`
        # (or via a future `with workflow(..., model=...)` block).
        # Pre-T4 this always sent the literal string "budget-precheck"
        # — a fake sentinel that:
        # 1. forced backend pricing lookup to fall through to the
        # default 3.0 rate, so projected_cost was always computed
        # against the wrong per-model rate
        # 2. blocked any future per-model budget tier (model-specific
        # caps) from being enforced correctly.
        # Sending `None` is fine — backend `calculate_projected_cost`
        # defaults to claude-sonnet-4 when model is unset, and tool_block
        # enforcement on /gate is best-effort when no tools are sent.
        call_model = get_call_model()
        call_tools = get_call_tools()

        # 2026-07-02 (v0.11.0): forward chain context for soft-mode
        # budget enforcement. When the user
        # has wrapped the call in `with chain(chain_id, op="start")`
        # the backend's Lua RESERVE_SCRIPT uses the chain to decide
        # whether to allow soft-mode overdrafts. Absent chain_id, the
        # gate falls back to single-shot Hard mode (binary budget
        # or no) — the previous behaviour.
        chain_id = get_chain_id()
        chain_op = get_chain_op()

        check_req = {
            "organization_id": self.organization_id or "local",
            # 2026-07-04 (BUG #4): requires server-minted
            # execution_id. Sending `workflow_id` here would re-use the
            # same execution_id for every /check in the workflow, breaking
            # the v3 reservation binding. We send a fresh uuidv7 per call
            # as a placeholder; the server's `gate_reserve_v3` overwrites
            # the field on the response, and `_capture_server_minted_execution_id`
            # (called below) picks up the server-minted `reservation_id`
            # for the downstream /track path.
            "execution_id": uuid7_str(),
            "operation_id": str(uuid.uuid4()),
            "check_type": "llm",
            "model": call_model,  # may be None if user didn't set it
            "estimated_tokens": 1,
            "stream": False,
        }

        # Forward the tool list so backend (T3) can match each tool
        # against the workflow's effective `blocked_tools` aggregate.
        # Only included when the user actually set it — `[]` means
        # "no tools will be called" which is different from "I didn't
        # tell you what tools will be called" (None).
        if call_tools:
            check_req["tools"] = list(call_tools)

        # Chain context — only included when the user has set it.
        # None vs missing chain_id is significant on the backend:
        # missing means "I'm a single-shot Hard call", None
        # explicitly would mean the same. Both safe to omit.
        if chain_id is not None:
            check_req["chain_id"] = chain_id
            check_req["chain_op"] = chain_op if chain_op != "auto" else None

        # 2026-07-02 (v0.11.0): idempotency key.
        # Replays of the same idempotency_key return the original
        # decision instead of re-running the gate. We use the
        # operation_id as the idempotency anchor — operation_id is
        # already a UUID v4 generated per call, so it doubles as
        # an idempotency_key without an extra round-trip.
        check_req["idempotency_key"] = check_req["operation_id"]

        # 2026-07-04 (BUG #5): in-process gate cache for chain-mode.
        # See module-top comment on _GATE_CACHE for full rationale.
        response: dict[str, Any]
        cache_key: tuple[str, str | None, str | None] | None = None
        cache_enabled = (
            chain_id is not None
            and not os.environ.get("NULLRUN_GATE_CACHE_DISABLE", "").strip() == "1"
        )
        if cache_enabled:
            cache_key = (str(workflow_id), chain_id, call_model)
            cached = _GATE_CACHE.get(cache_key)
            if cached is not None and (time.monotonic() - cached[0]) < _GATE_CACHE_TTL_SECONDS:
                # Cache hit within TTL — reuse the response without a
                # network roundtrip. The server's cumulative-spend
                # tracking is the source of truth; this is a debounce.
                response = cached[1]
            else:
                # Cache miss or expired — go to the server, then store.
                try:
                    response = self._transport.check(check_req)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        f"check_workflow_budget: /gate unavailable, failing open: {exc}"
                    )
                    return
                _GATE_CACHE[cache_key] = (time.monotonic(), response)
        else:
            try:
                response = self._transport.check(check_req)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"check_workflow_budget: /gate unavailable, failing open: {exc}"
                )
                return

        # 2026-07-04 (v0.12.0 wiring fix — ):
        # capture the server-minted ``reservation_id`` returned by
        # the backend's v3 ``gate_reserve_v3`` Lua path. Per
        # the server is the source-of-truth for execution_id
        # ownership; the value in ``GateResponse.reservation_id``
        # is a freshly-minted uuidv7 that maps to the
        # ``reservation:{execution_id}`` Redis key (TTL 300s).
        #
        # The /track handler v3 ``consume_budget_v3`` rejects with
        # 503 ``RESERVATION_NOT_FOUND`` when ``execution_id`` in
        # the request body does NOT match a live reservation key
        # — fail-CLOSED. Storing the id on a contextvar
        # means downstream ``track_llm`` / ``track_tool`` /
        # ``track_event`` calls can fill in the field without
        # threading it through the user-facing call sites.
        #
        # On legacy backends (``server_minted_execution_id=False``
        # capability) the field is omitted — ``get_...`` returns
        # ``None`` and the SDK falls back to the previous
        # (un-minted) wire flow. Capture happens regardless of
        # ``decision``: a "throttle" pass still produces a
        # reservation_id; only "block" + transport-failed clear it.
        # We capture BEFORE the decision checks so a future
        # bugfix that reorders them can't desync capture from
        # response.
        _capture_server_minted_execution_id(response)

        decision = response.get("decision", "allow")
        decision_source = response.get("decision_source", DecisionSource.GATEWAY)
        # Round 3 (Phase 0.4.0): only fail-OPEN on EXPLICIT synthetic
        # responses (decision_source starts with "fallback" or is one
        # of the classified TransportErrorSource values). Real
        # backend decisions (decision_source="gateway", or missing
        # for backward compat) are honoured.
        if decision_source.startswith("fallback") or decision_source in {
            TransportErrorSource.NETWORK_ERROR,
            TransportErrorSource.GATEWAY_ERROR,
            TransportErrorSource.BREAKER_OPEN,
            TransportErrorSource.AUTH_ERROR,
        }:
            logger.debug(
                f"check_workflow_budget: synthetic decision_source="
                f"{decision_source!r}, treating as transport error"
            )
            return
        if decision == "block":
            # FIX-2026-06-27: backend /gate sets both `explanation` (a
            # human-readable string, always populated on GateResponse::block)
            # and `explanations` (an optional Vec<String> that the gate
            # engine never populates today — `Some(vec![])` on the success
            # path, `None` on the explicit-block path). Pre-fix the SDK only
            # read `explanations`, so the user saw the useless fallback
            # "block" with `details={}` even when the backend knew exactly
            # why it blocked ("Budget exhausted: need 2 cents, 0 available").
            # Fall back to `explanation` (singular String) when the list is
            # empty so the real reason surfaces in the kill/pause reason.
            reasons = (
                response.get("explanations")
                or ([response["explanation"]] if response.get("explanation") else ["block"])
            )
            # Sprint 3 follow-up (B23): bump ``cost_limit_exceeded``
            # when the pre-flight blocks the workflow. The counter
            # is the operator's primary signal for "the budget
            # cap is biting" — distinct from loop / retry / rate
            # which have their own counters.
            metrics.inc_runtime("cost_limit_exceeded")
            raise WorkflowKilledInterrupt(
                workflow_id=workflow_id,
                reason="; ".join(reasons),
            )
        if decision == "throttle":
            reasons = (
                response.get("explanations")
                or ([response["explanation"]] if response.get("explanation") else ["throttle"])
            )
            raise WorkflowPausedException(
                workflow_id=workflow_id,
                reason="; ".join(reasons),
            )

    # =============================================================================
    # v3 wire-protocol helpers
    # =============================================================================

    def ping_chain(
        self,
        chain_id: str,
        interval: float = 30.0,
    ) -> Callable[[], None]:
        """Schedule time-based heartbeats for an active chain
.

        Returns a ``stop `` callable that cancels the scheduler
        thread. The heartbeat runs on a dedicated daemon thread so
        the agent loop stays unblocked.

        Replaces the previous chunk-based heuristic (every N chunks)
        with a wall-clock scheduler. Chunks do not correlate with
        time — one chunk per minute still leaves the chain idle for
        long stretches between heartbeat emissions, while bursty
        1000-chunk-per-second traffic wastes heartbeat budget on an
        already-fresh chain. ``time.monotonic `` ties the cadence
        to wall-clock time as recommended.

        Args:
            chain_id: Active chain_id (UUID v4). Must match a chain
                registered via ``with chain(chain_id, op="start")``.
            interval: Seconds between heartbeats. Default 30s
                the spec (configurable per policy in the
                10-120s range). ±5s skew is tolerated server-side.

        Returns:
            ``stop `` — call to cancel the scheduler. Idempotent.

        Notes:
            - The heartbeat POST is non-blocking and best-effort.
              A failed heartbeat is logged at DEBUG and the chain
              will simply expire via the server-side idle TTL.
            - The thread is a daemon so an interpreter shutdown
              without explicit ``stop `` does not hang.
            - Cadence is wall-clock (``time.monotonic``), not
              chunk-count. Bursting the agent loop 100x/sec does
              not change the heartbeat rate.
        """
        import threading as _threading

        if interval < 10.0 or interval > 120.0:
            raise ValueError(
                f"ping_chain interval must be in [10, 120] seconds per "
                f"CLAUDE.md §26, got {interval}"
            )

        stop_event = _threading.Event()
        thread_done = _threading.Event()

        def _heartbeat_loop() -> None:
            try:
                while not stop_event.is_set():
                    # Wait in small slices so ``stop `` returns
                    # promptly. ``Event.wait`` returns True if the
                    # event is set during the wait, so we break on
                    # shutdown without a long sleep.
                    if stop_event.wait(timeout=interval):
                        break
                    if stop_event.is_set():
                        break
                    try:
                        self._transport.heartbeat(chain_id)
                    except Exception as exc:  # noqa: BLE001 — best-effort
                        logger.debug(
                            "ping_chain: heartbeat for %s failed: %s",
                            chain_id,
                            exc,
                        )
            finally:
                thread_done.set()

        thread = _threading.Thread(
            target=_heartbeat_loop,
            daemon=True,
            name=f"nullrun-ping-chain-{chain_id[:8]}",
        )
        thread.start()

        def stop() -> None:
            """Cancel the heartbeat scheduler. Idempotent."""
            if stop_event.is_set():
                return
            stop_event.set()
            # Bounded wait so a stuck network call cannot keep the
            # interpreter alive past shutdown. The thread exits via
            # the ``stop_event.wait`` slice on the next iteration.
            thread_done.wait(timeout=interval + 1.0)

        return stop

    def cancel_execution(self, execution_id: str, reason: str | None = None) -> dict[str, Any]:
        """Cancel an in-flight execution via /api/v1/cancel
.

        Idempotent: repeated calls with the same ``execution_id``
        return 200 OK without side effects. A non-existent id
        surfaces as ``NullRunBackendError`` — the user should not
        retry in that case (the execution already terminated).

        Args:
            execution_id: Server-minted id from the matching /check
                response. Client-supplied execution_ids from pre-v3
                SDKs are NOT accepted.
            reason: Optional audit-trail reason.

        Returns:
            Parsed JSON dict.
        """
        return self._transport.cancel(execution_id, reason=reason)

    def chain_end(self, chain_id: str) -> dict[str, Any]:
        """Close a chain explicitly via /api/v1/chain/end
.

        Idempotent on the server — a no-op 200 for unknown
        chain_ids is the documented success path. Prefer using the
        ``with chain(...)`` contextmanager for normal flows; this
        helper is for the case where the chain was opened in a
        prior request and you need to close it from a different
        one.

        Args:
            chain_id: Chain to close.

        Returns:
            Parsed JSON dict.
        """
        return self._transport.chain_end(chain_id)

    def approximate_budget(self) -> dict[str, Any]:
        """UI-only budget estimate via GET /api/v1/budget/approximate
.

        NEVER use this value for enforcement — the response carries
        ``is_approximate: True`` and the estimate lags the
        authoritative budget counter by the outbox flush interval.
        Dashboards should display "Data unavailable" + retry button
        on the 503 path, NEVER "≈ $0 spent".

        Returns:
            Parsed JSON dict with ``current_spend_cents_estimate``
            ``is_approximate: True``, ``source``, ``confidence``
            ``last_updated_at``.

        Raises:
            NullRunBackendError: 503 BUDGET_DATA_UNAVAILABLE when
                all three sources (Redis period counter → Postgres
                cost_events → last-known cache) failed.
        """
        return self._transport.approximate_budget(
            organization_id=self.organization_id,
        )

    def _auth_headers(self) -> dict[str, str]:
        """Get authentication headers.

         the wire-protocol handshake header is
        required on every signed POST. The three direct callers of
        this helper — ``_post_auth_with_retry``, ``_fetch_remote_state``
        and ``get_org_status`` — all go through the backend's protocol
        middleware, so the header has to be present here rather than
        at every call site.
        """
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        headers[HEADER_PROTOCOL] = _protocol_header_value()
        return headers

    def shutdown(self) -> None:
        """Shutdown runtime gracefully."""
        # Stop the HTTP poller (legacy path) if it was started.
        self._poll_running = False
        if self._poll_thread and self._poll_thread.is_alive():
            # Phase 6 #6.3: cap to 0.5s (was 2.0s) so a SIGTERM
            # handler returns quickly. The HTTP-poll is best-effort
            # and the WS push channel is the authoritative source.
            self._poll_thread.join(timeout=0.5)

        # Stop the WS control plane listener (Phase B). Closing the
        # connection causes the receive task to unblock, the loop to
        # exit, and the thread to terminate.
        self._ws_stop_event.set()
        conn = self._ws_connection
        if conn is not None and self._ws_loop is not None:
            try:
                future = asyncio.run_coroutine_threadsafe(conn.close(), self._ws_loop)
                future.result(timeout=2.0)
            except Exception as e:
                logger.debug(f"WS close on shutdown failed (best-effort): {e}")
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=0.5)

        if self._transport:
            self._transport.stop()
        NullRunRuntime._instance = None
        logger.info("NullRun Runtime shutdown")

    def track(
        self,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Track a cost event.

        This is the main API for recording events. It:
        1. Adds workflow_id, trace_id, span_id from context
        2. Runs local check FIRST (no network round-trip)
        3. If local check passes, records and sends to backend
        4. If local check blocks, returns blocked response immediately

        Args:
            event: Event dict with keys like:
                   - type: "llm_call" | "tool_call" | "workflow_start" | "workflow_end"
                   - tokens: int
                   - tool_name: str (optional)
                   - is_retry: bool (optional)
                   - latency_ms: int (optional)
                   - metadata: dict (optional)

        Note:
            `cost_cents` is NOT a valid event key -- the SDK does not
            estimate cost. The backend computes it from tokens + the
            organization's policy.

        Returns:
            Dict with enforcement results:
            - allowed: bool
            - actions: list of actions taken
            - local_cost: current local cost
            - blocked_reason: str (if blocked locally)
            - blocked_suggestion: str (if blocked locally)

        Note:
            Local block reasons (loop detected, retry storm, rate
            limit, cost limit) are reported via the returned dict's
            ``blocked`` / ``blocked_reason`` / ``blocked_suggestion``
            fields rather than by raising an exception. The
            exception-raising variants of these conditions were
            removed in 0.4.0 because they had no in-tree callers
            see ``nullrun.breaker.exceptions`` for the list.
        """
        logger.debug(f"Tracking event: {event.get('event_type', 'unknown')}")

        # Phase D: dedup gate. The httpx transport, LangChain callback, and
        # OpenAI Agents tracer can all fire for the same LLM call. We drop
        # repeats keyed by `_fingerprint` (set by the observation path) so
        # each unique call produces exactly one /api/v1/track POST.
        fp = event.get("_fingerprint")
        if fp:
            from nullrun.instrumentation.auto import _fingerprint_is_seen

            if _fingerprint_is_seen(self._seen_track_fingerprints, fp):
                logger.debug("track() dedup hit for fingerprint=%s", fp)
                return {
                    "allowed": True,
                    "actions": [],
                    "local_cost_cents": self._local_cost_cents_estimate,
                    "deduped": True,
                }

        # 0.7.0 thin-client: NO local check here. All enforcement
        # decisions arrive from the backend via /gate and /execute.
        # The SDK forwards the event to the transport and lets the
        # backend decide.

        # Enrich event with context
        enriched = self._enrich_event(event)
        logger.debug(
            "Event enriched: workflow_id=%s, tokens=%s",
            enriched.get("workflow_id"),
            enriched.get("tokens"),
        )

        # Register workflow for remote state polling. workflow_id
        # may be None on legacy keys -- that's fine, the no-op
        # branch in check_control_plane will skip polling.
        #
        # Audit F-R2-12 (2026-06-22): route through ``_remote_state_for``
        # which takes ``_states_lock`` for the entire setdefault. The
        # pre-fix code did `with self._states_lock: setdefault(...)`
        # in a single lock entry but never held the lock across the
        # subsequent state read — so a concurrent ``_set_remote_state``
        # from a WS push could win the race and leave the entry as a
        # freshly-empty dict again on the next track_event call (a
        # remote PAUSE / KILL would silently lose its state between
        # the WS push and the next event). Using the locked helper
        # here keeps setdefault atomic against WS pushes, and we
        # don't read the returned dict anywhere — we only need the
        # side-effect of registering the workflow_id.
        workflow_id = enriched.get("workflow_id")
        if workflow_id:
            self._remote_state_for(workflow_id)

        # Phase 0.3.1: the local cost / loop / retry-storm check
        # (``_check_local_limits``) has been removed. It read
        # ``event.get("cost_cents", 0)`` and accumulated into a
        # per-workflow counter, but ``track_llm`` /
        # ``track_tool`` / ``track_event`` never set ``cost_cents``
        # (the SDK does not estimate cost -- the backend does). The
        # local check therefore never fired for the public API
        # and silently drifted from the backend's authoritative
        # cost. The local loop / rate checks (``_local_check``)
        # are independent and stay -- they do not depend on cost.
        # Budget enforcement is now exclusively the backend's
        # job: ``check_workflow_budget`` (pre-flight) + the
        # server-side /track cost ledger reconciliation.

        # Check remote control plane (after local enforcement)
        # This catches server-initiated pause/kill. Resolves
        # contextvar → self.workflow_id → no-op (legacy keys).
        self.check_control_plane(workflow_id)

        # Buffer for transport. The wire payload must NOT include
        # any field in ``_WIRE_STRIP_FIELDS`` -- see that constant's
        # docstring for the privacy rationale per field. We also drop
        # ``None`` values: putting ``{"model": null}`` on the wire
        # triggers backend ``unwrap_or("default")`` and a fallback
        # warning. Backend handles missing key as well as null, and
        # dropping None here keeps the diagnostic signal loud (the
        # warning below fires on missing-key, which is what we want
        # to see in operator logs) instead of silent (the JSON null
        # case).
        wire_event = {
            k: v
            for k, v in enriched.items()
            if k not in _WIRE_STRIP_FIELDS and v is not None
        }

        # Audit 2026-06-29 (SDK↔backend wire: silent zero-billing):
        # backend cost pipeline emits ``WARN model_id=default``
        # whenever an llm_call event reaches the wire without a
        # ``model`` field (pipeline.rs:176 ``unwrap_or("default")``).
        # Pre-fix the SDK warned and continued — the backend then
        # silently fell through to ``DEFAULT_RATE`` and every call
        # was recorded as ≈$0, breaking budget enforcement.
        #
        # Post-fix the SDK is fail-LOUD (not fail-closed yet — the
        # event is still sent so the backend can audit/reject):
        #
        # 1. ERROR log instead of WARN — operator sees the breakage
        # immediately, not buried in routine log noise.
        # 2. Bump the ``dropped_llm_call_no_model`` runtime counter
        # so dashboards can surface the regression rate.
        # 3. Tag the wire event with ``__missing_model: True`` so
        # the backend's into_track_request gate (fail-CLOSED
        # layer) can reject with HTTP 422 and a clear error
        # envelope instead of silently recording a zero-cost
        # call. The flag is treated as a wire-private signal —
        # the backend strips it before persisting.
        #
        # Activated only for llm_call so span_start/span_end/
        # tool_call traffic doesn't pollute logs or the wire.
        if wire_event.get("type") == "llm_call" and not wire_event.get("model"):
            logger.error(
                "track(): llm_call event missing 'model' field — "
                "tagging for backend rejection (HTTP 422). event=%s",
                wire_event,
            )
            metrics.inc_runtime("dropped_llm_call_no_model")
            wire_event["__missing_model"] = True

        self._route_track(wire_event)

        # Update metrics (thread-safe)
        metrics.inc_runtime("track_calls")

        return {
            "allowed": True,
            "actions": [],
            "local_cost_cents": self._local_cost_cents_estimate,
        }

    def _trigger_action(
        self,
        action: ActionType,
        workflow_id: str,
        reason: str,
    ) -> None:
        """
        Trigger a protective action.

        This executes the action through the action handler.
        """
        if self._action_handler:
            try:
                self._action_handler.handle(action.value, workflow_id, reason)
            except Exception as e:
                logger.debug(f"Action handler raised: {e}")
                # Let the exception propagate

    # =============================================================================
    # Phase 1.4: Pre-Execution Enforcement (SDK Boundary Fix)
    # =============================================================================

    def is_sensitive_tool(self, tool_name: str) -> bool:
        """
        Check if a tool is sensitive (requires strict mode).

        Sensitive tools MUST go through /execute endpoint for pre-execution
        enforcement. They cannot be executed directly.

        Args:
            tool_name: Name of the tool

        Returns:
            True if tool requires strict mode

        P2-3: match is case-insensitive. The pre-fix code did an exact
        ``tool_name in self._sensitive_tools`` check, so a tool
        registered as ``"stripe.charge"`` would silently fail to
        match a caller passing ``"Stripe.Charge"`` — bypassing the
        sensitive gate and running the body without an /execute
        round-trip. The fix normalises both sides to lowercase
        before the membership test, matching the case-insensitive
        style of ``_safe_kwargs``.

 #39: the read path takes ``_tools_lock`` so it sees a
        consistent snapshot alongside any concurrent
        ``add_sensitive_tool``. The lock is uncontended under
        CPython's GIL, so the cost is negligible.
        """
        needle = tool_name.lower()
        with self._tools_lock:
            return needle in {t.lower() for t in self._sensitive_tools} or needle in {
                t.lower() for t in self._strict_mode_tools
            }

    def get_org_status(self, org_id: str | None = None) -> dict[str, Any]:
        """Public helper for reading ``/api/v1/orgs/{org_id}/status``.

        Phase 8 #8.1: routes through ``self._transport._client`` so
        the shared connection pool, retry policy, and circuit breaker
        apply. Used by ``examples/cost_dashboard.py``.

        Args:
            org_id: Optional organisation ID. Defaults to the runtime's
                ``self.organization_id`` (set during ``_authenticate``).

        Returns:
            Parsed JSON dict of the org-status payload.

        Raises:
            NullRunAuthenticationError: if neither ``org_id`` nor
                ``self.organization_id`` is available.
            httpx.HTTPError: on transport failure.
        """
        resolved = org_id or self.organization_id
        if not resolved:
            err = NullRunAuthenticationError(
                "get_org_status requires org_id (or a runtime bound to one)",
                error_code="NR-C003",
                user_action=(
                    "Call nullrun.init() first, or pass org_id=<uuid> "
                    "explicitly. The runtime is not bound to an organization "
                    "yet — auth() must complete before this method can be used."
                ),
            )
            self._emit_sdk_error(err, stage="org_status")
            raise err
        response = self._transport._client.get(
            f"{self.api_url}/api/v1/orgs/{resolved}/status",
            headers=self._auth_headers(),
            timeout=10.0,
        )
        response.raise_for_status()
        return response.json()  # type: ignore[no-any-return]

    def add_sensitive_tool(self, tool_name: str) -> None:
        """
        Add a tool to the sensitive tools list.

        Sensitive tools require strict mode enforcement and must go through
        the /execute endpoint for pre-execution policy evaluation.

        Args:
            tool_name: Name of the tool to mark as sensitive

        Example:
            runtime = NullRunRuntime.get_instance 
            runtime.add_sensitive_tool("my.custom_tool")

 #39: takes ``_tools_lock`` so the mutation is atomic
        against concurrent ``is_sensitive_tool`` reads and other
        ``add``/``remove`` calls. Without the lock a free-threaded
        build could observe a torn set state during the mutation.
        """
        with self._tools_lock:
            self._strict_mode_tools.add(tool_name)

    def remove_sensitive_tool(self, tool_name: str) -> None:
        """
        Remove a tool from the sensitive tools list.

        Args:
            tool_name: Name of the tool to remove from sensitive list

        Example:
            runtime = NullRunRuntime.get_instance 
            runtime.remove_sensitive_tool("my.custom_tool")

 #39: takes ``_tools_lock`` to mirror ``add_sensitive_tool``.
        """
        with self._tools_lock:
            self._strict_mode_tools.discard(tool_name)

    def register_sensitive_tools(self, tool_names: list[str]) -> None:
        """
        Register multiple tools as sensitive.

        Args:
            tool_names: List of tool names to mark as sensitive

        Example:
            runtime = NullRunRuntime.get_instance 
            runtime.register_sensitive_tools([
                "stripe.charge"
                "payment.process"
                "send_email"
            ])
        """
        for tool_name in tool_names:
            self._strict_mode_tools.add(tool_name)

    def get_sensitive_tools(self) -> set[str]:
        """
        Get all currently registered sensitive tools.

        Returns:
            Set of sensitive tool names (includes both built-in and custom)
        """
        return self._sensitive_tools | self._strict_mode_tools

    def execute(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        mode: str = "auto",
        on_transport_error: Callable[[Exception], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """
        Pre-execution policy evaluation via /execute endpoint.

        This is the PRIMARY enforcement point for sensitive tools.
        Decision is made BEFORE execution.

        Args:
            tool_name: Name of the tool to execute
            input_data: Tool input parameters
            mode: Execution mode ("auto", "inline", "strict")
                - "auto": auto-select based on tool risk
                - "inline": force fast path (non-sensitive tools only)
                - "strict": force gateway roundtrip

        Returns:
            Dict with:
                - decision: "allow" | "block" | "flag" | "pause" | "require_approval"
                - decision_source: "gateway" | "cached" | "fallback"
                - explanation: Human-readable explanation
                - policy_version: Policy version used
                - decision_context: Context used for the decision (for decision-history audit)

        Raises:
            NullRunBlockedException: If decision is "block"
        """
        from nullrun.context import get_trace_id, get_workflow_id

        organization_id = self.organization_id or "local"
        workflow_id = get_workflow_id()
        trace_id = get_trace_id() or str(uuid.uuid4())

        # Auto-select mode: sensitive tools always use strict
        if mode == "auto":
            if self.is_sensitive_tool(tool_name):
                mode = "strict"
            else:
                mode = "inline"

        # For inline mode with non-sensitive tools, skip execute and use local enforcement
        if mode == "inline" and not self.is_sensitive_tool(tool_name):
            return {
                "decision": "allow",
                "decision_source": DecisionSource.LOCAL,
                "explanation": "Inline mode: local enforcement only",
                "policy_version": 0,
                "allow_execution": True,
            }

        # Strict mode or sensitive tool: call /execute endpoint
        # (no local_mode branch -- api_key is now required, see T3-S2)
        result = self._transport.execute(
            organization_id=organization_id,
            execution_id=workflow_id,
            trace_id=trace_id,
            tool=tool_name,
            input_data=input_data,
            mode=mode,
            fallback_mode=self._fallback_mode,
            on_transport_error=on_transport_error,
        )

        # Update metrics (thread-safe)
        metrics.inc_runtime("execute_calls")

        # Check if execution is allowed
        if result.get("decision") == "block":
            metrics.inc_runtime("execute_blocked")
            # Layer 1: best-effort error_code mapping from the
            # backend's ``explanation`` string. The backend does not
            # yet stamp a structured block_reason on /execute
            # responses (planned for the next round), so we match on
            # keywords in the free-text explanation. Anything we
            # cannot classify falls back to ``NR-X001`` (generic
            # block). The mapping is intentionally conservative —
            # false positives give the user the wrong code, false
            # negatives just fall back to the generic code.
            explanation = result.get("explanation", "policy violation")
            explanation_lower = explanation.lower()
            if "budget" in explanation_lower or "exhausted" in explanation_lower:
                block_code, block_action = "NR-B004", "block"
                block_cls = "NullRunBudgetError"
            elif "loop" in explanation_lower or "repetition" in explanation_lower:
                block_code, block_action = "NR-L001", "block"
                block_cls = "NullRunBlockedException"
            elif "rate" in explanation_lower or "too many" in explanation_lower:
                block_code, block_action = "NR-R001", "block"
                block_cls = "NullRunBlockedException"
            elif "tool" in explanation_lower and "block" in explanation_lower:
                block_code, block_action = "NR-T001", "block"
                block_cls = "NullRunToolBlockedError"
            else:
                block_code, block_action = "NR-X001", "block"
                block_cls = "NullRunBlockedException"
            # Note: we still raise the base ``NullRunBlockedException``
            # for non-budget/tool cases to keep the construction
            # shape simple — the catalogue code is what the user
            # reads, and they can branch on it via ``except
            # NullRunBudgetError:`` for the budget case if they need
            # to handle it specifically. We could instantiate the
            # subclass per branch above; keeping one raise here is
            # easier to reason about and matches the way the rest of
            # the codebase handles backend blocks.
            err = NullRunBlockedException(
                workflow_id=workflow_id or UNKNOWN_WORKFLOW_ID,
                reason=explanation,
                action=block_action,
                tool_name=tool_name,
                error_code=block_code,
                details={"mapped_class": block_cls},
            )
            # Layer 2: fire the on_error hook. The hook sees the
            # same exception the caller will catch plus the
            # workflow + tool context. A handler can use this to
            # emit a per-block Sentry event with a stable
            # ``error_code`` tag.
            self._emit_sdk_error(
                err,
                stage="execute",
                workflow_id=workflow_id,
                tool_name=tool_name,
                extra={"decision_source": result.get("decision_source")},
            )
            raise err

        metrics.inc_runtime("execute_allowed")
        return result

    def start_recording(self, workflow_id: str, metadata: dict[str, Any] = None) -> str:
        """
        Start recording events for local decision history.

.. deprecated:: 0.8.0
            Decision history moved to the backend dashboard. This method
            is a no-op stub and will be removed in 0.9.0. Use
            ``nullrun.status `` for a per-runtime snapshot or visit
            https:/docs.nullrun.io/concepts/decision-history for the
            dashboard workflow.

        Args:
            workflow_id: ID of the workflow to record
            metadata: Optional metadata about the session

        Returns:
            session_id for this recording (always ``""`` since 0.4.0)
        """
        # FIX 2026-06-28: was a silent no-op with logger.debug. Now emits
        # DeprecationWarning so customer code that still imports this
        # surfaces a visible migration signal before deletion in 0.9.0.
        warnings.warn(
            "NullRunRuntime.start_recording() is deprecated and will be "
            "removed in nullrun 0.9.0. Decision history is available via "
            "the backend dashboard at /control-center/decision-history.",
            DeprecationWarning,
            stacklevel=2,
        )
        return ""

    def stop_recording(self):
        """
        Stop recording and return the session.

.. deprecated:: 0.8.0
            See:meth:`start_recording`. Will be removed in 0.9.0.

        Returns:
            The recorded session, or None if not recording
        """
        # FIX 2026-06-28: paired deprecation warning for start_recording.
        warnings.warn(
            "NullRunRuntime.stop_recording() is deprecated and will be "
            "removed in nullrun 0.9.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        return None

    def _enrich_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Add context fields to event."""
        enriched = dict(event)  # Don't modify original

        # Phase 139+: workflow_id from context, else from the API
        # key's binding (set in _authenticate). Stays unset on legacy
        # keys -- emitted events then carry no workflow_id (orphan, as
        # before this change).
        if "workflow_id" not in enriched:
            wf_id = self._resolve_workflow_id(get_workflow_id())
            if wf_id:
                enriched["workflow_id"] = wf_id

        # Add trace context
        if "trace_id" not in enriched:
            trace_id = get_trace_id() or generate_trace_id()
            enriched["trace_id"] = trace_id

        if "span_id" not in enriched:
            span_id = get_span_id() or generate_span_id()
            enriched["span_id"] = span_id

        # Add agent_id from context (for per-agent cost attribution)
        if "agent_id" not in enriched:
            agent_id = get_agent_id()
            if agent_id:
                enriched["agent_id"] = agent_id

        # Add attempt_index from context (for retry correlation)
        if "attempt_index" not in enriched:
            attempt_index = get_attempt_index()
            if attempt_index > 0:  # Only add if not default (first attempt)
                enriched["attempt_index"] = attempt_index

        # 2026-07-04 (v0.12.0 wiring fix — ):
        # include the server-minted execution_id on the /track
        # payload when one is in scope (captured by
        # ``check_workflow_budget`` via
        # ``_capture_server_minted_execution_id``).
        #
        # Wire field: ``execution_id`` — matches the backend's
        # ``consume_budget_v3`` consume-request body schema
        # (``backend/src/cost/reservation.rs::consume_budget_v3``).
        #
        # Skip when:
        # * the user / caller already supplied ``execution_id``
        # (explicit takes precedence)
        # * no reservation was captured yet (legacy path or this
        # is the very first event before the first /check)
        # * the captured reservation has aged past
        # ``SERVER_MINTED_RESERVATION_MAX_AGE_SECONDS`` (295s
        # by default — 5s safety margin below the 300s Redis
        # reservation TTL per). Forwards of a stale id
        # would 503 ``RESERVATION_NOT_FOUND`` on /track and
        # we'd rather drop the field than trip the gate.
        if "execution_id" not in enriched:
            import time as _time

            from nullrun.context import (
                get_server_minted_execution_id,
                get_server_minted_reservation_at,
            )

            smid = get_server_minted_execution_id()
            if smid:
                age = _time.monotonic() - get_server_minted_reservation_at()
                if age >= SERVER_MINTED_RESERVATION_MAX_AGE_SECONDS:
                    # Drop the stale capture. The user (or the
                    # next @protect invocation) will mint a fresh
                    # id on the next /check.
                    from nullrun.context import (
                        clear_server_minted_execution_id,
                    )
                    clear_server_minted_execution_id()
                    logger.debug(
                        "_enrich_event: dropping stale server-minted "
                        "execution_id (age=%.1fs >= %ds)",
                        age,
                        SERVER_MINTED_RESERVATION_MAX_AGE_SECONDS,
                    )
                else:
                    enriched["execution_id"] = smid

        # 2026-07-04: propagate the in-scope
        # /check idempotency_key onto the wire_event so the v3
        # /track single-event payload carries the same anchor and
        # the backend's replay branch returns 200 +
        # ``idempotent_replay: true`` on retry (handlers.rs:
        # 4654-4725). Without this, a transport-level retry on the
        # SAME event either re-runs CONSUME_SCRIPT (→ 503
        # RESERVATION_NOT_FOUND, since the reservation key was
        # DEL'ed after the first successful consume per) or
        # double-bills. Read via the same contextvar written at
        # ``_capture_server_minted_execution_id`` time — symmetric
        # lifetime with ``execution_id`` (cleared together on
        # /track emit and on workflow/chain block exit).
        if "idempotency_key" not in enriched:
            from nullrun.context import get_server_minted_idempotency_key

            idem_key = get_server_minted_idempotency_key()
            if idem_key:
                enriched["idempotency_key"] = idem_key

        # Add type if not present
        if "type" not in enriched:
            enriched["type"] = "event"

        # Add required fields with defaults
        if "is_retry" not in enriched:
            enriched["is_retry"] = False

        if "operation_name" not in enriched:
            enriched["operation_name"] = None

        return enriched

    def _route_track(self, wire_event: dict[str, Any]) -> None:
        """Route a tracked event to v3 single-event /track or
        legacy batch /track/batch.

        Why this exists
        ---------------
        Pre-0.12.0 wiring the SDK always called
        ``self._transport.track(wire_event)`` which posts to the
        legacy ``/api/v1/track/batch`` (the ``process_span_event``
        pipeline). That pipeline reads the org's lifetime
        ``monthly_cost`` counter — drift with the dashboard's
        period-bound ``bp:{ts}:cost_cents`` per G1
        and never exercises v3 ``consume_budget_v3`` so the
        consume ≤ reserve + ε invariant is never validated.

        The fix: route events that have a paired ``/check``
        reservation (currently: ``llm_call``) to
        ``track_single`` which posts to ``/api/v1/track``. The
        backend's consume takes the server-minted execution_id
        from the request, looks up
        ``reservation:{execution_id}`` and runs the invariant.
        Span events still ride /track/batch — they have no
        reservation to release.

        Opt-out
        -------
        ``NULLRUN_V3_TRACK_DISABLE=1`` forces every event
        through the legacy batch path. Use it on backends that
        haven't flipped ``NULLRUN_CONSUME_V3_ENABLED=1`` yet.

        Failure mode
        ------------
        ``track_single`` raises on 422 / 503 / 5xx (see
        ``nullrun.breaker.exceptions``). We catch and log at
        WARNING level; the event is dropped (NOT retried via
        the batch path — that would risk double-billing
 idempotency contract).
        """
        from nullrun.context import get_server_minted_execution_id

        event_type = wire_event.get("type")
        v3_disabled = (
            os.environ.get("NULLRUN_V3_TRACK_DISABLE", "").strip() == "1"
        )

        if event_type != "llm_call" or v3_disabled:
            # Span / heartbeat / tool events have no reservation
            # the legacy batch path is the right endpoint.
            self._transport.track(wire_event)
            return

        smid = get_server_minted_execution_id()
        if not smid:
            # Either no /check landed in this scope (legacy v1/v2
            # path) or the capture expired past the 295s safety
            # window. Don't make up an id — fall back to batch
            # which uses the no-reservation v1/v2 consume path.
            self._transport.track(wire_event)
            logger.debug(
                "_route_track: llm_call without server-minted "
                "execution_id in scope — routing via /track/batch"
            )
            return

        single_payload = _build_v3_track_payload(wire_event, smid)
        if single_payload is None:
            # Mapper refused (missing required field). Fall back.
            self._transport.track(wire_event)
            return

        try:
            self._transport.track_single(single_payload)
            metrics.inc_runtime("v3_track_single_ok")
        except Exception as exc:  # noqa: BLE001 — transport-level
            metrics.inc_runtime("v3_track_single_failed")
            _emit_for_transport_error(
                exc,
                stage="track_v3_single",
                correlation_id=smid,
                status_code=getattr(exc, "status_code", None),
            )
            logger.warning(
                "_route_track: track_single failed for "
                "execution_id=%s (%s) — event dropped",
                smid,
                exc,
            )

    def track_llm(
        self,
        input_tokens: int,
        output_tokens: int = 0,
        *,
        model: str | None = None,
        latency_ms: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Track an LLM call. Pulls the active SpanContext from contextvars
        automatically so the backend can attribute the call to the right
        span (e.g. the one created by `@protect`).

        Args:
            input_tokens: Number of input / prompt tokens.
            output_tokens: Number of output / completion tokens. Defaults
                to 0 -- embeddings and reasoning-only calls have no
                completion token count.
            model: Model name, e.g. "gpt-4o-mini".
            latency_ms: Request latency in milliseconds.
            metadata: Arbitrary key-value pairs.

        Returns:
            Track result dict from the runtime.

        Note:
            `cost_cents` is no longer a parameter. The backend computes
            it from `input_tokens` + `output_tokens` + the org's pricing
            policy. Splitting prompt vs completion matters because most
            models price them differently.
        """
        # Lazy import to keep the runtime import graph acyclic --
        # `nullrun.tracing` deliberately has no SDK-side dependencies.
        from nullrun.tracing import get_current_span

        event: dict[str, Any] = {
            "type": "llm_call",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "tokens": input_tokens + output_tokens,
        }
        if model:
            event["model"] = model
        if latency_ms is not None:
            event["latency_ms"] = latency_ms
        if metadata:
            event["metadata"] = metadata

        # Auto-tag the event with the active span so the backend can
        # render this call under the right node in the trace timeline.
        # If no @protect / manual set_span is active, span is None and
        # the field is omitted -- _enrich_event will fall back to the
        # loose contextvars or generate fresh IDs.
        span = get_current_span()
        if span is not None:
            event["trace_id"] = span.trace_id
            event["span_id"] = span.span_id
            event["parent_span_id"] = span.parent_span_id
            event["depth"] = span.depth

        return self.track(event)

    def track_tool(
        self,
        tool_name: str,
        duration_ms: int | None = None,
        *,
        is_retry: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Track a tool call. Pulls the active SpanContext from contextvars
        automatically -- see `track_llm` for the rationale.

        Args:
            tool_name: Name of the tool called.
            duration_ms: Execution duration in milliseconds.
            is_retry: Whether this is a retry attempt.
            metadata: Arbitrary key-value pairs.

        Returns:
            Track result dict from the runtime.

        Note:
            `cost_cents` is no longer a parameter. Tool cost is derived
            from `duration_ms` + the org's policy (or left at 0 if the
            org doesn't bill tools). `duration_ms` is the public field
            name; the wire field is `latency_ms` for backward compat
            with backend consumers.
        """
        from nullrun.tracing import get_current_span

        event: dict[str, Any] = {
            "type": "tool_call",
            "tool_name": tool_name,
            "is_retry": is_retry,
        }
        if duration_ms is not None:
            event["latency_ms"] = duration_ms
        if metadata:
            event["metadata"] = metadata

        span = get_current_span()
        if span is not None:
            event["trace_id"] = span.trace_id
            event["span_id"] = span.span_id
            event["parent_span_id"] = span.parent_span_id
            event["depth"] = span.depth

        return self.track(event)

    def track_event(
        self,
        event_type: str,
        **kwargs,
    ) -> dict[str, Any]:
        """
        Generic event tracking.

        Args:
            event_type: Type of event ("workflow_start", "workflow_end", etc.)
            **kwargs: Additional event fields

        Returns:
            Track result dict
        """
        event = {"type": event_type, **kwargs}
        # Backend's SdkTrackRequest requires `tokens: u64` (non-Optional).
        # Span-lifecycle events (span_start / span_end) don't have a
        # token count -- they're bookkeeping, not consumption. Default
        # to 0 so the deserializer accepts the event; the cost
        # computation in the handler treats 0 tokens as no-op.
        event.setdefault("tokens", 0)
        # Phase 3: emit a stable fingerprint so the dedup LRU at
        # the track sink can collapse repeat emissions of the
        # same event (e.g. when the user calls track_event manually
        # AND the httpx transport hook fires for the same LLM
        # call). Field is stripped before wire send (see
        # ``_strip_wire_only_fields``).
        if "_fingerprint" not in event:
            from nullrun.instrumentation.auto import (
                _fingerprint_for_event_dict,
            )

            event["_fingerprint"] = _fingerprint_for_event_dict(event)
        return self.track(event)

    def _post_auth_with_retry(
        self,
        url: str,
        json_body: dict[str, Any],
        max_attempts: int = 3,
    ) -> httpx.Response:
        """POST ``json_body`` to ``url`` with bounded retry on transient
        failure.

        2026-06-28 audit P2.3: the init path ``POST /api/v1/auth/verify``
        previously did a single bare ``self._transport._client.post(...)``
        call. Backend emits ``503 + Retry-After: 5`` on transient DB
        errors (see ``backend/src/proxy/handlers.rs:11346-11351``), which
        pre-fix surfaced to the user as ``NR-A001`` ("configuration
        issue") even though the SDK was fine and the key was fine —
        just a Postgres blip. This helper retries 5xx and network
        errors up to ``max_attempts`` total tries, honors
        ``Retry-After`` when the backend provides one, and propagates
        ``httpx.RequestError`` unchanged on the LAST attempt so the
        existing ``except`` arm below can turn it into ``NR-B001``.

        Auth failures (401/403/422) are NOT retried — the API key is
        wrong on attempt 1 means it's wrong on attempt 3.
        """
        import time as _time

        last_exc: httpx.RequestError | None = None
        for attempt in range(max_attempts):
            try:
                response = self._transport._client.post(
                    url,
                    json=json_body,
                    headers=self._auth_headers(),
                )
            except httpx.RequestError as e:
                last_exc = e
                if attempt < max_attempts - 1:
                    backoff_s = min(0.5 * (2 ** attempt), 5.0)
                    logger.debug(
                        f"/auth/verify network error "
                        f"(attempt {attempt + 1}/{max_attempts}): "
                        f"{e}; retrying in {backoff_s}s"
                    )
                    _time.sleep(backoff_s)
                    continue
                raise

            # 5xx (transient) → retry. 4xx → return as-is so the
            # caller's status-code branching can do its job.
            if response.status_code >= 500 and attempt < max_attempts - 1:
                retry_after_header = response.headers.get("retry-after")
                if retry_after_header:
                    try:
                        backoff_s = float(retry_after_header)
                    except ValueError:
                        # HTTP-date or unparseable — fall back to exp backoff
                        backoff_s = min(0.5 * (2 ** attempt), 5.0)
                else:
                    backoff_s = min(0.5 * (2 ** attempt), 5.0)
                logger.debug(
                    f"/auth/verify returned {response.status_code} "
                    f"(attempt {attempt + 1}/{max_attempts}); "
                    f"retrying in {backoff_s}s"
                )
                _time.sleep(backoff_s)
                continue

            return response

        # Defensive: should be unreachable (loop either returns or
        # raises). If a future refactor breaks that invariant, surface
        # the last network error rather than silently returning None.
        assert last_exc is not None
        raise last_exc


# Module-level convenience functions
_runtime: NullRunRuntime | None = None


# 2026-07-04 (v0.12.0 wiring fix — ):
# helper used by ``check_workflow_budget`` to capture the server-minted
# execution_id from the /check response into a contextvar. Lives at
# module scope so any /check path (``check_workflow_budget``
# ``check_v3``, future ``preflight_v3``) can call it without taking
# a dependency on the runtime singleton.
#
# Behaviour:
# * On a real ``reservation_id`` field: store it on the
# ``_server_minted_execution_id_var`` contextvar + record
# ``time.monotonic `` on ``_server_minted_reservation_at_var``
# so ``_enrich_event`` can refuse to forward a stale capture
# past the 300s reservation TTL.
# * On missing/None/empty value: clear both contextvars so
# downstream /track ships without ``execution_id`` (the legacy
# / v1-v2 wire shape — backend is tolerant per the
# ``server_minted_execution_id=False`` capability gating).
# * On an invalid UUID string (defence-in-depth — backend is the
# source-of-truth and only mints uuidv7, but a buggy proxy
# could echo a malformed field): drop it with a warning log.
def _capture_server_minted_execution_id(response: dict[str, Any]) -> str | None:
    """Capture ``response["reservation_id"]`` into the server-minted
    execution_id contextvar.

    Returns the captured id (or ``None`` on miss / malformed) so the
    caller can log it on debug paths. The contextvar itself is the
    authoritative side-effect — readers consult
    ``get_server_minted_execution_id`` from ``nullrun.context``.

    Import is lazy (inside the function) to keep
    ``nullrun.runtime`` import order stable: ``context`` itself
    imports nothing from ``runtime``, but ``_enrich_event`` lives
    in this module and depends on the context getters.
    """
    import time as _time

    from nullrun.context import (
        clear_server_minted_execution_id,
        set_server_minted_execution_id,
        set_server_minted_idempotency_key,
        set_server_minted_reservation_at,
    )

    raw = response.get("reservation_id") if isinstance(response, dict) else None
    if not raw:
        # Legacy / v1-v2 backend, or a block response with no
        # reservation. Clear any prior capture so the next /track
        # doesn't ship a stale id from a previous /check.
        clear_server_minted_execution_id()
        return None

    if not isinstance(raw, str):
        clear_server_minted_execution_id()
        logger.warning(
            "_capture_server_minted_execution_id: response.reservation_id "
            "is %s, expected str — dropping",
            type(raw).__name__,
        )
        return None

    # Defence-in-depth UUID parse — backend's mint_execution_id
    # emits RFC-4122 uuidv7 but a buggy proxy could echo garbage.
    # Drop without raising (fail-OPEN on capture; the backend will
    # still reject malformed ids with 400 on /track).
    import uuid as _uuid

    try:
        _uuid.UUID(raw)
    except (ValueError, AttributeError):
        clear_server_minted_execution_id()
        logger.warning(
            "_capture_server_minted_execution_id: response.reservation_id=%r "
            "is not a valid UUID — dropping",
            raw,
        )
        return None

    set_server_minted_execution_id(raw)
    set_server_minted_reservation_at(_time.monotonic())
    # 2026-07-04: capture the /check
    # idempotency_key so the matching /track event can carry the
    # same anchor (handlers.rs:4654-4725 — replay returns 200 +
    # idempotent_replay: true on key hit). We look at the
    # request body via the response's ``operation_id`` field
    # when the server echoes it (the /check request sets
    # ``idempotency_key = operation_id`` at runtime.py:1260)
    # when absent, fall back to None and let the /track wire
    # payload drop the field.
    op_id = response.get("operation_id") if isinstance(response, dict) else None
    if isinstance(op_id, str) and op_id:
        set_server_minted_idempotency_key(op_id)
    logger.debug(
        "_capture_server_minted_execution_id: captured %s",
        raw,
    )
    return raw


# 2026-07-04 (v0.12.0 wiring fix — ): build the
# v3 /track single-event payload from an enriched llm_call event.
# Lives at module scope so ``_route_track`` (a method) can call it
# without taking a runtime dependency beyond the contextvar getters.
#
# Wire shape (``/api/v1/track`` schema
# ``backend/src/proxy/handlers.rs::TrackRequest``):
#
# {
# "reservation_id": "<server-minted uuidv7 from /check>"
# "workflow_id": "<bound workflow uuid>"
# "tokens": <int>, # input + output
# "input_tokens": <int>
# "output_tokens": <int>
# "cost_cents": <int>, # 0 — backend computes from tokens
# "model": "<model name>", # used for rate lookup
# "metadata": {...}, # optional, free-form
# "cost_source": "provisional", # per trust model
# }
#
# The backend's ``gate_consume_v3`` reads ``reservation_id`` and
# runs CONSUME_SCRIPT v3 (server-minted execution_id owner check +
# consume ≤ reserve + epsilon invariant). If a required field is
# missing OR the runtime cannot construct the payload, returns
# ``None`` and the caller falls back to ``/track/batch``.
def _build_v3_track_payload(
    wire_event: dict[str, Any],
    reservation_id: str,
) -> dict[str, Any] | None:
    """Map an enriched llm_call event onto the v3 /track schema.

    Returns ``None`` when the event cannot be mapped (caller
    falls back to legacy batch path). Required ``tokens`` /
    ``workflow_id`` absence is the only failure mode today.
    """
    wf_id = wire_event.get("workflow_id")
    if not wf_id:
        # The backend's consume_budget_v3 needs a workflow_id to
        # attribute the consume to a key+workflow counter; without
        # one the consume becomes unattributable.
        # ownership binding). A missing workflow_id means the
        # SDK never bound the API key to a workflow (legacy
        # legacy-no-binding). Fall back.
        logger.debug(
            "_build_v3_track_payload: missing workflow_id — "
            "cannot shape v3 /track payload"
        )
        return None

    tokens = wire_event.get("tokens")
    if tokens is None:
        # Same as llm_call missing required fields — the backend
        # would 422 anyway. Fall back to batch.
        logger.debug(
            "_build_v3_track_payload: missing tokens — cannot "
            "shape v3 /track payload"
        )
        return None

    payload: dict[str, Any] = {
        "reservation_id": reservation_id,
        "workflow_id": wf_id,
        "tokens": int(tokens),
        "cost_cents": 0,
        "cost_source": "provisional",  # 
    }
    if "input_tokens" in wire_event and wire_event["input_tokens"] is not None:
        payload["input_tokens"] = int(wire_event["input_tokens"])
    if "output_tokens" in wire_event and wire_event["output_tokens"] is not None:
        payload["output_tokens"] = int(wire_event["output_tokens"])
    if "model" in wire_event and wire_event["model"]:
        payload["model"] = wire_event["model"]
    if "latency_ms" in wire_event and wire_event["latency_ms"] is not None:
        payload["latency_ms"] = int(wire_event["latency_ms"])
    if "metadata" in wire_event and wire_event["metadata"]:
        payload["metadata"] = wire_event["metadata"]
    if "trace_id" in wire_event and wire_event["trace_id"]:
        payload["trace_id"] = wire_event["trace_id"]
    if "span_id" in wire_event and wire_event["span_id"]:
        payload["span_id"] = wire_event["span_id"]

    # Optional downstream fields preserved verbatim (workflow-level
    # cost attribution, agent_id, etc.). Backend ignores unknown
    # fields, so unknown keys are safe — we just surface the ones
    # the SDK actually emits.
    for k in (
        "agent_id",
        "environment",
        "agent_type",
        "attempt_index",
        "is_retry",
    ):
        if k in wire_event and wire_event[k] is not None:
            payload[k] = wire_event[k]

    # Wire idempotency_key: the
    # backend's /track handler (``handlers.rs:4654-4725``) accepts
    # ``idempotency_key: Option<String>`` and, on hit of the same
    # key, replays the original response with 200 OK +
    # ``idempotent_replay: true``. Without this, a transport-level
    # retry (5xx, timeout) on the SAME event would re-call the v3
    # CONSUME_SCRIPT and either double-bill or get 503
    # ``RESERVATION_NOT_FOUND`` (because the reservation key was
    # DEL'ed after the first successful consume per).
    #
    # Source of truth: ``check_req.idempotency_key`` (set in
    # ``check_workflow_budget`` to the operation_id UUID v4, see
    # runtime.py:1260) is captured into a contextvar by
    # ``_capture_server_minted_execution_id`` and stamped onto the
    # wire_event by ``_enrich_event``. We accept EITHER source —
    # ``wire_event`` takes precedence (explicit caller override)
    # then the contextvar fallback (covers tests / flows that call
    # ``_build_v3_track_payload`` directly without going through
    # ``_enrich_event``). When both are absent, omit the field and
    # the backend falls back to ``execution_id`` only.
    idem_key = wire_event.get("idempotency_key")
    if not idem_key:
        from nullrun.context import get_server_minted_idempotency_key

        idem_key = get_server_minted_idempotency_key()
    if idem_key:
        payload["idempotency_key"] = str(idem_key)

    return payload


def get_runtime() -> NullRunRuntime:
    """Get or create the global runtime instance."""
    global _runtime
    if _runtime is None:
        _runtime = NullRunRuntime.get_instance()
    return _runtime


def track(event: dict[str, Any]) -> dict[str, Any]:
    """
    Module-level track function.

    Usage:
        from nullrun import track

        # Note: `cost_cents` is NOT a valid event key — the SDK strips
        # it before sending. Use `tokens` (or input_tokens/output_tokens
        # for track_llm).
        track({"type": "llm_call", "tokens": 100})
    """
    return get_runtime().track(event)


# Phase 3.4: explicit alias for `track ` -- same call signature, friendlier
# name for users who reach for `track_event` first. Both names share the
# same callable object, so `nullrun.track is nullrun.track_event` is True.
track_event = track


def track_llm(
    input_tokens: int,
    output_tokens: int = 0,
    **kwargs,
) -> dict[str, Any]:
    """Module-level LLM tracking.

    Forwards to `NullRunRuntime.track_llm`. The active SpanContext (if
    any) is attached to the event automatically so the backend can
    render the call under the right span.

    Args:
        input_tokens: Number of input / prompt tokens.
        output_tokens: Number of output / completion tokens. Defaults
            to 0 -- embeddings and reasoning-only calls have no
            completion token count.
        **kwargs: Forwarded to `NullRunRuntime.track_llm` (model
            latency_ms, metadata).
    """
    return get_runtime().track_llm(input_tokens, output_tokens, **kwargs)


def track_tool(
    tool_name: str,
    duration_ms: int | None = None,
    **kwargs,
) -> dict[str, Any]:
    """Module-level tool tracking.

    Forwards to `NullRunRuntime.track_tool`. The active SpanContext
    (if any) is attached to the event automatically.

    Args:
        tool_name: Name of the tool
        duration_ms: How long the tool call took
        **kwargs: Forwarded to `NullRunRuntime.track_tool` (is_retry
            metadata).
    """
    return get_runtime().track_tool(tool_name, duration_ms=duration_ms, **kwargs)
