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
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
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
    DecisionSource,
    FallbackMode,
    FlushConfig,
    Transport,
    TransportErrorSource,
)


class LoopTracker:
    """
    In-memory loop detection using deque with timestamps.

    Tracks calls per tool_name with a 60-second sliding window.
    """

    def __init__(self, window_seconds: int = 60):
        self._calls = defaultdict(deque)
        self._window_seconds = window_seconds

    def record(self, tool_name: str) -> None:
        """Record a call for a tool."""
        now = time.time()
        self._calls[tool_name].append(now)
        self._prune(tool_name, before=now - self._window_seconds)

    def count(self, tool_name: str, window: int = None) -> int:
        """
        Count calls for a tool within the time window.

        Args:
            tool_name: Name of the tool
            window: Time window in seconds (defaults to init window)

        Returns:
            Number of calls in the window
        """
        if window is None:
            window = self._window_seconds
        self._prune(tool_name, before=time.time() - window)
        return len(self._calls[tool_name])

    def _prune(self, tool_name: str, before: float) -> None:
        """Remove calls older than the threshold."""
        while self._calls[tool_name] and self._calls[tool_name][0] < before:
            self._calls[tool_name].popleft()

class RateTracker:
    """
    In-memory rate tracking using deque with timestamps.

    Tracks total calls per minute to enforce rate limits.
    """

    def __init__(self, window_seconds: int = 60):
        self._calls = deque()
        self._window_seconds = window_seconds

    def record(self) -> None:
        """Record a call."""
        now = time.time()
        self._calls.append(now)
        self._prune(before=now - self._window_seconds)

    def count(self, window: int = None) -> int:
        """
        Count calls within the time window.

        Args:
            window: Time window in seconds (defaults to init window)

        Returns:
            Number of calls in the window
        """
        if window is None:
            window = self._window_seconds
        self._prune(before=time.time() - window)
        return len(self._calls)

    def exceeds_limit(self, limit: int, window: int = None) -> bool:
        """
        Check if rate limit is exceeded.

        Args:
            limit: Maximum allowed calls in the window
            window: Time window in seconds (defaults to init window)

        Returns:
            True if limit is exceeded
        """
        return self.count(window) >= limit

    def _prune(self, before: float) -> None:
        """Remove calls older than the threshold."""
        while self._calls and self._calls[0] < before:
            self._calls.popleft()

@dataclass
class LocalDecision:
    """Decision from local check (no network round-trip)."""

    allowed: bool
    reason: str = None
    suggestion: str = None

logger = logging.getLogger(__name__)

# Phase 0.3.1: sentinel used when a gate fires outside a
# ``with workflow(...)`` context. The double-underscore prefix
# namespacing avoids collision with a user workflow that happens
# to be named ``<unknown>`` (the previous literal was a
# collision hazard). Wire compat: still a string.
UNKNOWN_WORKFLOW_ID: str = "__nullrun_unknown__"

@dataclass
class Policy:
    """
    Policy fetched from NullRun backend.

    Defines the safety limits for an agent workflow.
    """

    budget_cents: int
    rate_limit: int  # cents per minute
    loop_threshold: int = 6  # same tool calls in window
    retry_threshold: int = 5  # retries in window
    anomaly_detection_enabled: bool = True
    loop_detection_enabled: bool = True
    retry_detection_enabled: bool = True

    @classmethod
    def default_local(cls) -> "Policy":
        """Default policy for local mode (free tier)."""
        return cls(
            budget_cents=1000,  # $10
            rate_limit=100,
            loop_threshold=6,
            retry_threshold=5,
        )

    @classmethod
    def strict_local(cls) -> "Policy":
        """Tight fail-CLOSED fallback used when policy fetch fails
        AND there is no last-known-good cached policy.

        Per audit F-R2-02 (2026-06-22): the previous ``default_local``
        fallback silently widened every limit (no rate limit, $10
        budget, 6-loop threshold). On any backend blip, the SDK ran
        with zero enforcement until the next successful fetch — a
        classic fail-OPEN regression on an enforcement path.

        ``strict_local`` is tight on every axis: 0 budget cap forces
        every cost-bearing operation through the backend's
        reservation service (fail-CLOSED there too), 1-call rate
        limit caps sustained throughput, and loop/retry thresholds
        of 1 fire on the first suspicious repetition. Callers that
        genuinely need the legacy permissive fallback can opt in
        via ``NULLRUN_POLICY_FAIL_OPEN=1`` — that env var is the
        only place the SDK keeps the old behaviour.
        """
        return cls(
            budget_cents=0,  # zero cap → backend reservation rejects
            rate_limit=1,  # 1 call/min ceiling
            loop_threshold=1,  # first repetition trips loop detector
            retry_threshold=1,  # first retry trips retry detector
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Policy":
        """Create Policy from a backend ``PolicyResponse`` dict.

        Backend fields (see backend/src/proxy/http/policies.rs::
        ``PolicyResponse``) and the SDK's local ``Policy`` class
        describe overlapping but non-identical facets of the same
        domain. We map the intersection and fall back to defaults
        where the backend doesn't surface the field — in particular
        ``budget_cents`` and ``retry_detection_enabled`` are SDK-local
        concepts with no counterpart on the wire today.
        """
        return cls(
            budget_cents=data.get("budget_cents", 1000),
            # Backend field is rate_limit_per_minute; SDK keeps the
            # legacy "rate_limit" attribute name (cents per minute).
            rate_limit=data.get("rate_limit_per_minute", data.get("rate_limit", 100)),
            loop_threshold=data.get("loop_threshold", 6),
            retry_threshold=data.get("retry_threshold", 5),
            anomaly_detection_enabled=data.get("anomaly_detection_enabled", True),
            loop_detection_enabled=data.get("loop_detection_enabled", True),
            # No backend flag for this today — default keeps existing
            # behaviour intact when the field is absent.
            retry_detection_enabled=data.get("retry_detection_enabled", True),
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
        # Automatic (via protect())
        import nullrun
        nullrun.protect()

        # Manual
        rt = NullRunRuntime.get_instance()
        # Note: `cost_cents` is NOT a valid event key — the SDK strips
        # it before sending (see ``track_event`` / wire payload below).
        # The backend computes cost from tokens + the org's pricing
        # policy. Use ``tokens`` (or, for llm_call specifically,
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
        policy: Policy | None = None,
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
            api_url: URL of NullRun proxy server. Defaults to https://api.nullrun.io.
            policy: Optional policy to use. If None, fetches from backend
                   (cloud mode) or uses default (local mode).
            debug: Enable debug logging.
            _test_mode: Internal flag to skip network calls (for testing).
            polling: Internal flag for tests/CI to skip the background
                     control-plane listener (WS or HTTP poll). Defaults True
                     in production. Set False when the test environment
                     cannot tolerate a background thread opening sockets.

        Note:
            - `organization_id` is set from `_authenticate()` after init; it is
              NOT a public init parameter and not read from env.
            - `api_key` is required as of 0.3.0 (T3-S2). The previous
              `local_mode` flag was removed because it silently bypassed
              every backend gate.
            - `fallback_mode` is fixed at PERMISSIVE (no public override).
            - `timeout`/`max_retries` are fixed at 30s / 3 (no public override).

        Raises:
            NullRunAuthenticationError: if neither `api_key` nor
                `NULLRUN_API_KEY` is set. The public `init()` surface
                performs the same check first and produces a clearer
                error message; this constructor-level raise is the
                direct fallback for tests and advanced callers that
                build the runtime by hand.
        """
        self.api_key = api_key or os.getenv("NULLRUN_API_KEY")
        self.secret_key = secret_key or os.getenv("NULLRUN_SECRET_KEY")
        self.api_url = api_url or os.getenv("NULLRUN_API_URL", "https://api.nullrun.io")

        # T3-S2 (0.3.0): api_key is now required. The previous `local_mode`
        # flag silently bypassed every backend gate (budget, policy,
        # control plane), which was a real safety hole in production.
        # We raise NullRunAuthenticationError here instead so the
        # misconfiguration is caught at startup. The public `init()`
        # surface raises first with a clearer message; this is the
        # direct construction path used by tests and advanced callers.
        if not self.api_key:
            raise NullRunAuthenticationError(
                "NullRunRuntime() requires an api_key. Pass api_key='nr_live_...' "
                "or set NULLRUN_API_KEY. (Silent no-op fallback was removed "
                "in 0.3.0 -- see CHANGELOG.)"
            )
        # organization_id is set by _authenticate(); stays None until then.
        self.organization_id: str | None = None
        # Phase 139+: workflow_id is set by _authenticate() from the API
        # key's binding (organization_api_keys.workflow_id). Used as a
        # fallback for /check, /status, and span events when the user
        # hasn't entered a `with workflow(...)` context. None on legacy
        # keys (pre-139 or never used) -- call sites must NOT invent one.
        self.workflow_id: str | None = None

        self._test_mode = _test_mode
        self.polling = polling

        self._policy: Policy | None = policy
        # Audit F-R2-02 (2026-06-22): cache the last good policy so a
        # transient backend outage doesn't silently widen enforcement.
        # _fetch_policy() writes here on every successful 200; the
        # failure path reads from it before falling through to
        # Policy.strict_local().
        self._last_good_policy: Policy | None = policy
        # Sprint 3.2: prefer the typed ``on_transport_error`` parameter
        # over the legacy string ``fallback_mode`` parameter. The
        # legacy string (and its NULLRUN_FALLBACK_MODE env var) is
        # still honoured for one minor version, with a one-time
        # ``DeprecationWarning`` so operators see the migration path.
        fb_raw = fallback_mode
        if fb_raw is None and os.environ.get("NULLRUN_FALLBACK_MODE"):
            # Legacy env var: emit a one-time deprecation warning
            # at construction. After Sprint 3.2 the env var
            # continues to work (so existing deployments don't
            # break) but the user is told to migrate to
            # ``on_transport_error`` on ``Transport.execute()``.
            import warnings as _w

            _w.warn(
                "NULLRUN_FALLBACK_MODE is deprecated. Pass "
                "``on_transport_error=`` to ``Transport.execute()`` "
                "instead (one of 'raise' | 'open' | 'closed'). "
                "The env var will be removed in 0.5.0.",
                DeprecationWarning,
                stacklevel=2,
            )
            fb_raw = os.environ.get("NULLRUN_FALLBACK_MODE", "permissive")
        fb_upper = str(fb_raw).upper() if fb_raw is not None else "PERMISSIVE"
        if fb_upper == "STRICT":
            self._fallback_mode = FallbackMode.STRICT
        elif fb_upper == "CACHED":
            self._fallback_mode = FallbackMode.CACHED
        else:
            self._fallback_mode = FallbackMode.PERMISSIVE
        self._timeout = 30
        self._max_retries = 3
        self._debug = debug
        self._transport: Transport | None = None

        # Local enforcement state
        # Phase 0.3.1: the BoundedDict-based per-workflow cost /
        # loop / retry counters have been removed alongside
        # ``_check_local_limits``. The local loop / rate checks
        # (``_loop_tracker`` / ``_rate_tracker`` below) are
        # independent and stay -- they do not depend on cost.
        self._workflow_start_time: float = time.time()

        # Local loop and rate tracking (for _local_check in track())
        self._loop_tracker = LoopTracker(window_seconds=60)
        self._rate_tracker = RateTracker(window_seconds=60)

        # Phase D: dedup LRU. Multiple observation paths (httpx transport,
        # LangChain callback, OpenAI Agents tracer) can fire for the same
        # LLM call. We collapse them to a single track() per fingerprint.
        # The fingerprint is computed at the observation point and passed
        # via the `_fingerprint` event field.
        from nullrun.instrumentation.auto import make_dedup_state

        self._seen_track_fingerprints = make_dedup_state()

        # Per ADR-008 the SDK does not track local cost. The two response
        # fields below are kept in the return shape for backwards
        # compatibility with 0.3.x callers but always read 0. The previous
        # implementation read from `self._workflow_costs` (a BoundedDict
        # removed in 0.3.1) which left `track()` raising AttributeError on
        # first call.
        self._local_cost_cents_estimate: int = 0

        # Default thresholds for local check (Phase 1 - hardcoded, not from backend)
        self._local_loop_threshold = 6
        self._local_rate_limit = 1000  # calls per minute

        # Coverage counters (Phase 3 of the production-readiness plan).
        # The instrumentation layer in `nullrun.instrumentation.auto`
        # calls ``_safe_bump_coverage(runtime, "_coverage_seen" /
        # "_coverage_tracked" / "_coverage_streaming_skipped", host)``
        # so the dashboard can show "which LLM hosts the SDK is
        # seeing vs. successfully tracking". Previous versions
        # relied on ``_safe_bump_coverage`` to no-op when these
        # attributes were missing -- the dashboard's coverage tab
        # was always empty.
        self._coverage_seen: dict[str, int] = {}
        self._coverage_tracked: dict[str, int] = {}
        self._coverage_streaming_skipped: dict[str, int] = {}

        # Remote control plane state (per-workflow, pushed from server via WS).
        # Unified model: effective_state = max(local_state, remote_state).
        # All writes and reads go through the `_remote_state_for` /
        # `_set_remote_state` helpers (Phase 5 #5.1) so the WS callback,
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
        # a gRPC client. NULLRUN_USE_GRPC is a silent no-op.
        if os.getenv("NULLRUN_USE_GRPC"):
            logger.info(
                "NULLRUN_USE_GRPC is set but the gRPC transport is not "
                "implemented in this SDK version; falling back to HTTP."
            )

        # Initialize
        if self._test_mode:
            # Test mode: skip all network calls, use local policy
            self._policy = self._policy or Policy.default_local()
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
            self._fetch_policy()
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
        # §7.2 #39: lock that guards every mutation of the
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
        #
        # We also reuse this lock to guard the coverage-counter
        # dicts (§7.2 #33) because the bump + prune sequence must
        # be atomic — otherwise two threads could both observe the
        # dict at length 4095, both bump their counter, and both
        # evict a different entry, growing the dict to 4097
        # before either prune lands. One lock, one source of
        # truth, cheaper than two fine-grained ones.
        self._tools_lock = threading.Lock()
        # §7.2 #33: cap the per-host coverage counters. Without
        # this, a long-running process that sees thousands of
        # custom LLM endpoints over its lifetime would grow these
        # dicts without bound — same hazard as
        # ``NullRunCallback._active_runs`` (now capped at 4096).
        self._COVERAGE_CAP: int = 4096

        logger.info(f"NullRun Runtime initialized: mode=cloud, policy={self._policy}")

    @classmethod
    def get_instance(cls) -> "NullRunRuntime":
        """Get the singleton runtime instance.

        Thread-safe: the singleton lock is held for the full read-compare-
        rebuild sequence (Phase 5 #5.3). The previous version dropped the
        lock between shutdown and the recursive get_instance(), creating a
        window where a concurrent caller could observe a half-shutdown
        runtime.
        """
        with cls._lock:
            # Re-read env vars at every call site so credential rotation
            # is observed on the next get_instance() invocation.
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

    def _authenticate(self) -> None:
        """Authenticate with API key and get organization_id.

        Also handles key version updates for HMAC secret key rotation.
        On successful auth, the server may return a new key_version indicating
        a secret key rotation. The SDK stores this and uses it for signing.
        """
        if not self.api_key:
            raise BreakerError("API key required for cloud mode")

        logger.debug(f"Authenticating with API at {self.api_url}/auth/verify")
        try:
            # Use Transport's client for connection pooling, retry, and circuit breaker
            response = self._transport._client.post(
                f"{self.api_url}/api/v1/auth/verify",
                json={"api_key": self.api_key},
            )

            if response.status_code == 200:
                data = response.json()
                # STRICT MODE: organization_id is REQUIRED, no fallback
                org_id = data.get("organization_id")
                if not org_id:
                    raise NullRunAuthenticationError(
                        "Auth response missing organization_id - server may be outdated or compromised. "
                        "Refusing to operate with legacy identity."
                    )
                self.organization_id = org_id

                # Phase 139+: pick up the workflow this key is bound to.
                # `None` on legacy keys (pre-139 or never-used) -- call
                # sites that NEED a workflow (check_workflow_budget,
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
                raise NullRunAuthenticationError(
                    f"Auth failed with status {response.status_code}. "
                    f"API key may be invalid or expired. Not operating in unsafe mode."
                )
        except httpx.RequestError as e:
            # Network error - raise exception, do not fall back silently
            raise NullRunAuthenticationError(
                f"Auth request failed: {e}. Cannot establish secure connection to NullRun. "
                f"Refusing to operate in unprotected mode."
            ) from e

    def _fetch_policy(self) -> None:
        """Fetch policy from backend and cache locally.

        Backend route: GET /api/v1/orgs/{org_id}/policies (see
        backend/src/proxy/http/routes.rs). Pre-FIX-F1 the SDK POSTed
        to /api/v1/policies with organization_id in the body — the
        backend route is GET + org-scoped URL, so the call 404'd and
        fell through to ``Policy.default_local()`` (silent fail-open
        on every policy fetch).

        Response shape: ``{"data": [...], "meta": {...}}`` where each
        entry is a ``PolicyResponse`` (backend/src/proxy/http/policies.rs).
        The SDK ``Policy`` class and backend ``PolicyResponse`` describe
        different facets of the same domain — we map the overlap
        (rate_limit_per_minute, loop_threshold, retry_threshold, and the
        detection-enabled flags) and fall back to defaults for fields
        the backend doesn't surface.

        ## Fail-CLOSED contract (audit F-R2-02, 2026-06-22)

        Pre-fix: any HTTP exception, non-200 status, or empty
        ``{"data": []}`` response silently fell through to
        ``Policy.default_local()`` — which has ``budget_cents=1000``,
        ``rate_limit=100``, ``loop_threshold=6``, no tool block — i.e.
        effectively unenforced. A 503 from the backend would keep the
        customer's SDK running with zero enforcement for the rest of
        the session.

        Post-fix: the SDK enforces fail-CLOSED on this gate, mirroring
        the broader CLAUDE.md fail-CLOSED policy. On any failure path
        the SDK uses, in priority order:

        1. The last known-good cached policy (``self._last_good_policy``).
           The customer's existing limits are preserved across a
           transient outage — they pay the cost of any policy
           tightening baked into the last fetch, but do not lose
           enforcement.
        2. ``Policy.strict_local()`` — tight cap (zero budget,
           1-call rate limit, first-repetition loop detection) that
           forces every cost-bearing call through the backend's
           reservation service, which is itself fail-CLOSED.

        Opt-out: ``NULLRUN_POLICY_FAIL_OPEN=1`` restores the
        pre-fix permissive fallback. Mirrors the shape of
        ``NULLRUN_SKIP_BUDGET_CHECK=1`` and
        ``NULLRUN_SENSITIVE_FAIL_OPEN=1`` — a single env var to
        re-enable the legacy behaviour for tests or staging.
        """
        fail_open = os.environ.get("NULLRUN_POLICY_FAIL_OPEN", "").strip() == "1"

        if not self.organization_id:
            self._policy = (
                Policy.default_local() if fail_open else Policy.strict_local()
            )
            logger.warning(
                "No organization_id; policy fetch skipped. fail-OPEN=%s "
                "(NULLRUN_POLICY_FAIL_OPEN=1 to restore permissive fallback).",
                fail_open,
            )
            return

        try:
            # Use Transport's client for connection pooling, retry, and circuit breaker
            response = self._transport._client.get(
                f"{self.api_url}/api/v1/orgs/{self.organization_id}/policies",
                headers=self._auth_headers(),
                timeout=5.0,
            )

            if response.status_code == 200:
                payload = response.json()
                # Backend wraps the list in {"data": [...], "meta": ...}.
                # The pre-FIX-F1 code assumed a bare list and would
                # crash on len(payload[...]) of a dict.
                entries = payload.get("data", []) if isinstance(payload, dict) else payload
                # Find the most relevant active policy: prefer the
                # first is_active entry; if all are inactive, skip the
                # whole list (inactive policies should not tighten
                # enforcement).
                active = next(
                    (p for p in entries if isinstance(p, dict) and p.get("is_active", True)),
                    None,
                )
                if active is not None:
                    fetched = Policy.from_dict(active)
                    self._policy = fetched
                    # Audit F-R2-02: cache the last good policy so
                    # transient outages don't silently widen limits.
                    self._last_good_policy = fetched
                    logger.info(f"Policy fetched: {self._policy}")
                    return
                # 200 OK but no active policy — same shape as the
                # pre-fix behaviour, but post-fix we drop to the
                # cached or strict fallback rather than the permissive
                # default. Without an active policy the backend is
                # not asserting any limits, so the SDK cannot safely
                # assume the legacy $10/100-rpm defaults reflect
                # current intent.
                logger.warning(
                    "Policy fetch returned no active policies for org=%s",
                    self.organization_id,
                )
            else:
                logger.warning(
                    "Policy fetch returned status=%s for org=%s",
                    response.status_code,
                    self.organization_id,
                )
        except Exception as e:
            logger.warning(
                "Failed to fetch policy for org=%s: %s", self.organization_id, e
            )

        # Audit F-R2-02: fail-CLOSED. Order of precedence:
        #   1. last known-good cached policy (if any)
        #   2. strict_local() (zero budget, 1-call rate limit)
        #   3. opt-out env var NULLRUN_POLICY_FAIL_OPEN=1 → default_local()
        if getattr(self, "_last_good_policy", None) is not None:
            self._policy = self._last_good_policy
            logger.warning(
                "Policy fetch failed; using last known-good policy (fail-CLOSED). "
                "Set NULLRUN_POLICY_FAIL_OPEN=1 to fall back to permissive defaults."
            )
            return

        if fail_open:
            self._policy = Policy.default_local()
            logger.warning(
                "No cached policy and NULLRUN_POLICY_FAIL_OPEN=1; "
                "using permissive default policy (audit F-R2-02 fail-OPEN opt-in)."
            )
            return

        self._policy = Policy.strict_local()
        logger.warning(
            "No cached policy available; activating Policy.strict_local() "
            "(zero budget, 1-call rate limit). Backend unreachable — "
            "every cost-bearing call will be rejected by the reservation "
            "service until the next successful policy fetch. "
            "Set NULLRUN_POLICY_FAIL_OPEN=1 to restore the legacy "
            "permissive fallback for tests / staging."
        )

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
             (Phase 139+). Set during _authenticate(). None on legacy
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
        """Fetch remote state for a specific workflow via the org-scoped
        workflow endpoint.

        Pre-FIX-F2 the SDK hit ``/api/v1/status/{workflow_id}``, which
        is not a registered route on the backend (the backend exposes
        per-workflow state via
        ``GET /api/v1/orgs/{org_id}/workflows/{workflow_id}``). The
        pre-fix code therefore 404'd every poll and silently fell back
        to local state — meaning the legacy HTTP-poll path could never
        observe a remote kill/pause. WS push (the default mode since
        Phase 5) does NOT go through this code path, so the WS control
        plane is unaffected.

        Backend ``WorkflowResponse`` (see
        backend/src/proxy/http/workflows.rs:43) does not surface a
        numeric ``version`` or ``reason`` for a workflow — those
        fields are SDK-local only and remain at their cached values
        when the remote response arrives. ``state`` is the only field
        the kill/pause check (``check_control_plane``) actually reads,
        so this is sufficient for correctness.
        """
        if not self.organization_id:
            # Legacy HTTP-poll was always org-bound; without org_id we
            # cannot resolve the right route. Bail silently — the WS
            # push path remains the authoritative source.
            return
        try:
            response = self._transport._client.get(
                f"{self.api_url}/api/v1/orgs/{self.organization_id}/workflows/{workflow_id}",
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

        # S-4: case-insensitive compare per analyze.md §11.6. The backend
        # already emits PascalCase via the `as_pascal_case()` normaliser
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
            "block"   → WorkflowKilledInterrupt   (hard policy / reservation error)
            "throttle"→ WorkflowPausedException   (insufficient budget, can resume)
            "allow"   → return

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

        from nullrun.context import get_workflow_id

        # Phase 139+: prefer the user-set contextvar (explicit `with
        # workflow(...)` block), fall back to the API key's bound
        # workflow. Returns None only on legacy keys that have never
        # been workflow-bound -- in that case the check is silently
        # skipped, exactly as before this change.
        workflow_id = self._resolve_workflow_id(get_workflow_id())
        if not workflow_id:
            return

        check_req = {
            "organization_id": self.organization_id or "local",
            "execution_id": workflow_id,
            "operation_id": str(uuid.uuid4()),
            "check_type": "llm",
            "model": "budget-precheck",
            "estimated_tokens": 1,
        }

        try:
            response = self._transport.check(check_req)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"check_workflow_budget: /gate unavailable, failing open: {exc}")
            return

        decision = response.get("decision", "allow")
        decision_source = response.get("decision_source", DecisionSource.GATEWAY)
        # Round 3 (Phase 0.4.0): only fail-OPEN on EXPLICIT synthetic
        # responses (decision_source starts with "fallback" or is one
        # of the classified TransportErrorSource values). Real
        # backend decisions (decision_source="gateway", or missing,
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
            reasons = response.get("explanations") or ["block"]
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
            reasons = response.get("explanations") or ["throttle"]
            raise WorkflowPausedException(
                workflow_id=workflow_id,
                reason="; ".join(reasons),
            )

    def _auth_headers(self) -> dict[str, str]:
        """Get authentication headers."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
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

    @property
    def policy(self) -> Policy:
        """Get current policy."""
        return self._policy or Policy.default_local()

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
            removed in 0.4.0 because they had no in-tree callers;
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

        # Phase 1: LOCAL CHECK FIRST (before any network call)
        # This provides instant blocking without round-trip latency
        local_decision = self._local_check(event)
        if not local_decision.allowed:
            # Blocked locally - return immediately without backend call
            logger.debug(f"Local check blocked: {local_decision.reason}")
            return {
                "allowed": False,
                "actions": ["block"],
                "blocked_reason": local_decision.reason,
                "blocked_suggestion": local_decision.suggestion,
                "local_cost_cents": 0,
            }

        # Local check passed - record the call BEFORE sending to backend
        tool_name = event.get("tool_name", "unknown")
        self._loop_tracker.record(tool_name)
        self._rate_tracker.record()

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
        # cost_cents -- the SDK does not estimate cost; the backend
        # recomputes it from tokens + the org's policy. The
        # sink-only ``_fingerprint`` field is also stripped before
        # the wire send so the dedup key shape is not leaked to
        # anyone with audit-log access.
        wire_event = {k: v for k, v in enriched.items() if k not in ("cost_cents", "_fingerprint")}
        self._transport.track(wire_event)

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

        §7.2 #39: the read path takes ``_tools_lock`` so it sees a
        consistent snapshot alongside any concurrent
        ``add_sensitive_tool``. The lock is uncontended under
        CPython's GIL, so the cost is negligible.
        """
        needle = tool_name.lower()
        with self._tools_lock:
            return needle in {t.lower() for t in self._sensitive_tools} or needle in {
                t.lower() for t in self._strict_mode_tools
            }

    def coverage_report(self) -> dict[str, dict[str, int]]:
        """
        Snapshot of the LLM-host coverage counters that the auto-
        instrumentation layer maintains. The SDK tracks three
        counters per host:

          - ``seen`` -- every LLM host the SDK observed a request to.
          - ``tracked`` -- hosts whose response was successfully
            extracted and emitted as an ``llm_call`` event.
          - ``streaming_skipped`` -- hosts whose response was a
            streaming SSE / ``stream=True`` and was deliberately
            NOT buffered (so the user keeps their chunked read).

        The same payload is sent over the WebSocket heartbeat every
        60s and via the HTTP-fallback path when the WS connection
        is down. The dashboard's coverage tab uses these counters
        to surface "we know about this host but cannot track it" --
        the leading indicator that an SDK upgrade is needed.

        Returns:
            ``{"seen": {...}, "tracked": {...},
            "streaming_skipped": {...}}``. Each value is a fresh
            ``dict`` so callers can mutate the result without
            affecting the runtime's internal state.
        """
        return {
            "seen": dict(self._coverage_seen),
            "tracked": dict(self._coverage_tracked),
            "streaming_skipped": dict(self._coverage_streaming_skipped),
        }

    def track_coverage(self) -> dict[str, Any] | None:
        """Emit a `coverage_report` event with the current per-host counters.

        Returned from ``track_event`` so the caller can observe the
        transport-side outcome (queued, deduped, breaker open, etc.).
        Returns ``None`` when there are no counters to report yet
        (cold start, no LLM traffic) — the backend doesn't need an
        empty row per minute per process.

        Background emission is driven by ``start_coverage_reporter``;
        most callers don't invoke this method directly.
        """
        stats = self.coverage_report()
        seen_total = sum(stats["seen"].values())
        if seen_total == 0:
            # Nothing to report — avoid empty rows.
            return None
        return self.track_event(
            "coverage_report",
            **{
                "seen": stats["seen"],
                "tracked": stats["tracked"],
                "streaming_skipped": stats["streaming_skipped"],
            },
        )

    _COVERAGE_REPORT_INTERVAL_SECONDS = 60.0

    def start_coverage_reporter(self) -> None:
        """Start a background thread that emits ``coverage_report`` events
        every ``_COVERAGE_REPORT_INTERVAL_SECONDS``.

        Idempotent — second call is a no-op. Caller is responsible
        for calling :meth:`stop_coverage_reporter` on shutdown, but
        the thread is a daemon so a missed stop does not block exit.
        """
        if getattr(self, "_coverage_reporter_thread", None) is not None:
            return
        thread = threading.Thread(
            target=self._coverage_reporter_loop,
            name="nullrun-coverage-reporter",
            daemon=True,
        )
        self._coverage_reporter_thread = thread
        thread.start()

    def stop_coverage_reporter(self, timeout: float = 2.0) -> None:
        """Signal the coverage reporter to exit and join its thread."""
        self._coverage_reporter_stop = True
        thread = getattr(self, "_coverage_reporter_thread", None)
        if thread is not None:
            thread.join(timeout=timeout)

    def _coverage_reporter_loop(self) -> None:
        """Loop body for the coverage reporter thread.

        Emits a coverage report on entry (so the dashboard has data
        within ~1s of process start), then every interval until
        ``stop_coverage_reporter`` is called.
        """
        self._coverage_reporter_stop = False
        # Emit once on entry — gives the backend a row even if the
        # process is short-lived (CI, batch jobs).
        try:
            self.track_coverage()
        except Exception as e:  # noqa: BLE001 — background loop
            logger.debug(f"coverage_reporter: initial emit failed: {e}")
        while not getattr(self, "_coverage_reporter_stop", False):
            # Sleep in short slices so shutdown is responsive.
            slept = 0.0
            while slept < self._COVERAGE_REPORT_INTERVAL_SECONDS and not getattr(
                self, "_coverage_reporter_stop", False
            ):
                time.sleep(min(0.5, self._COVERAGE_REPORT_INTERVAL_SECONDS - slept))
                slept += 0.5
            if getattr(self, "_coverage_reporter_stop", False):
                break
            try:
                self.track_coverage()
            except Exception as e:  # noqa: BLE001 — background loop
                logger.debug(f"coverage_reporter: emit failed: {e}")

    def bump_coverage_counter(self, target_attr: str, host: str) -> None:
        """Bump a per-host coverage counter with FIFO eviction at the cap.

        §7.2 #33: replaces the previous direct-dict-mutation path
        used by ``nullrun.instrumentation.auto._safe_bump_coverage``.
        The pre-fix code just did ``target[host] = target.get(host,
        0) + 1``, which let a process with many custom LLM
        endpoints grow the dict without bound. We now:

          1. Take ``_tools_lock`` so concurrent bumps from
             multiple threads (sync httpx + async httpx + the
             requests transport) can't both pass the cap check
             and evict different entries.
          2. If the dict already has the key, increment (LRU
             bump via dict insertion order).
          3. If the key is new and we're at the cap, evict the
             oldest entry before inserting.

        Tolerates a missing attribute (stub runtimes in tests):
        no-op when ``getattr(self, target_attr, None)`` returns
        ``None``. Tolerates a non-dict target (also a test-only
        scenario): logs DEBUG and moves on.
        """
        with self._tools_lock:
            target = getattr(self, target_attr, None)
            if target is None:
                return
            if not isinstance(target, dict):
                logger.debug(
                    "bump_coverage_counter: %s is not a dict (%s); skipping",
                    target_attr,
                    type(target).__name__,
                )
                return
            if host in target:
                # Insertion-order LRU bump: re-insert so this
                # host moves to the end of the dict.
                target[host] = int(target.get(host, 0)) + 1
                # Re-set to refresh insertion order (Python dicts
                # don't auto-promote on value update).
                value = target.pop(host)
                target[host] = value
            else:
                if len(target) >= self._COVERAGE_CAP:
                    evicted_host, _ = next(iter(target.items()))
                    del target[evicted_host]
                    logger.warning(
                        "coverage counter %s hit cap %d; evicting oldest host=%s",
                        target_attr,
                        self._COVERAGE_CAP,
                        evicted_host,
                    )
                target[host] = 1

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
            raise NullRunAuthenticationError(
                "get_org_status requires org_id (or a runtime bound to one)"
            )
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
            runtime = NullRunRuntime.get_instance()
            runtime.add_sensitive_tool("my.custom_tool")

        §7.2 #39: takes ``_tools_lock`` so the mutation is atomic
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
            runtime = NullRunRuntime.get_instance()
            runtime.remove_sensitive_tool("my.custom_tool")

        §7.2 #39: takes ``_tools_lock`` to mirror ``add_sensitive_tool``.
        """
        with self._tools_lock:
            self._strict_mode_tools.discard(tool_name)

    def register_sensitive_tools(self, tool_names: list[str]) -> None:
        """
        Register multiple tools as sensitive.

        Args:
            tool_names: List of tool names to mark as sensitive

        Example:
            runtime = NullRunRuntime.get_instance()
            runtime.register_sensitive_tools([
                "stripe.charge",
                "payment.process",
                "send_email",
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
            raise NullRunBlockedException(
                workflow_id=workflow_id or UNKNOWN_WORKFLOW_ID,
                reason=result.get("explanation", "policy violation"),
                tool_name=tool_name,
            )

        metrics.inc_runtime("execute_allowed")
        return result

    def start_recording(self, workflow_id: str, metadata: dict[str, Any] = None) -> str:
        """
        Start recording events for local decision history.

        Args:
            workflow_id: ID of the workflow to record
            metadata: Optional metadata about the session

        Returns:
            session_id for this recording
        """
        # Sprint 2.1: local decision-history recorder was removed.
        # This method is kept as a no-op stub for one minor
        # version to avoid breaking callers that imported it. It
        # will be deleted in the next release.
        logger.debug(
            "runtime.start_recording() is a no-op; decision history moved to the backend dashboard."
        )
        return ""

    def stop_recording(self):
        """
        Stop recording and return the session.

        Returns:
            The recorded session, or None if not recording
        """
        # Sprint 2.1: paired no-op stub for start_recording().
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

        # Add type if not present
        if "type" not in enriched:
            enriched["type"] = "event"

        # Add required fields with defaults
        if "is_retry" not in enriched:
            enriched["is_retry"] = False

        if "operation_name" not in enriched:
            enriched["operation_name"] = None

        return enriched

    def _local_check(self, event: dict[str, Any]) -> LocalDecision:
        """
        Local check BEFORE sending to backend.

        This runs before the event is sent to the backend and provides
        instant blocking without network round-trip.

        Args:
            event: Event dict with tool_name

        Returns:
            LocalDecision with allowed/blocked status
        """
        tool_name = event.get("tool_name", "unknown")

        # Check loop count (6 same tool calls in 60s window)
        loop_count = self._loop_tracker.count(tool_name, window=60)
        if loop_count >= self._local_loop_threshold:
            # Sprint 3.1 (B23): bump the ``loop_detections`` counter
            # so an SRE can alert on a sudden spike (often a sign
            # of an agent stuck in a retry loop).
            metrics.inc_runtime("loop_detections")
            return LocalDecision(
                allowed=False, reason="loop_detected", suggestion="retry after 60s"
            )

        # Check rate limit (max 1000/min default)
        if self._rate_tracker.exceeds_limit(self._local_rate_limit):
            return LocalDecision(allowed=False, reason="rate_limit", suggestion="slow down")

        return LocalDecision(allowed=True)

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
            input_tokens:  Number of input / prompt tokens.
            output_tokens: Number of output / completion tokens. Defaults
                to 0 -- embeddings and reasoning-only calls have no
                completion token count.
            model:         Model name, e.g. "gpt-4o-mini".
            latency_ms:    Request latency in milliseconds.
            metadata:      Arbitrary key-value pairs.

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
            tool_name:   Name of the tool called.
            duration_ms: Execution duration in milliseconds.
            is_retry:    Whether this is a retry attempt.
            metadata:    Arbitrary key-value pairs.

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
        # the track() sink can collapse repeat emissions of the
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

# Module-level convenience functions
_runtime: NullRunRuntime | None = None

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

# Phase 3.4: explicit alias for `track()` -- same call signature, friendlier
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
        input_tokens:  Number of input / prompt tokens.
        output_tokens: Number of output / completion tokens. Defaults
            to 0 -- embeddings and reasoning-only calls have no
            completion token count.
        **kwargs: Forwarded to `NullRunRuntime.track_llm` (model,
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
        **kwargs: Forwarded to `NullRunRuntime.track_tool` (is_retry,
            metadata).
    """
    return get_runtime().track_tool(tool_name, duration_ms=duration_ms, **kwargs)
