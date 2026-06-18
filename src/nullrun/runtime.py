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
the work. Each gate declares its own fail-OPEN/CLOSED policy — this is
the authoritative table; deviations require an ADR amendment (Rule 5).

| Gate | Transport-error behavior | Recovery behavior | Opt-out |
|---|---|---|---|
| `check_workflow_budget` | OPEN (skip check, log warning) | silent post-hoc correction in `/track` events via `cost_correction_applied=true` | `NULLRUN_SKIP_BUDGET_CHECK=1` — **full billing bypass**, not just check bypass (see docstring WARNING) |
| `check_control_plane` | OPEN (treat state as `Normal`) | deferred enforcement — next WS-push or `/status` poll sees the true state | none |
| `_enforce_sensitive_tool` (default `_fallback_mode=permissive`) | CLOSED — body MUST NOT run when `decision_source` is any `FALLBACK_*` | n/a (body did not run) | `NULLRUN_SENSITIVE_FAIL_OPEN=1` — explicitly documented as "OPEN-when-engine-unavailable" |
| `_enforce_sensitive_tool` (`_fallback_mode=strict`) | CLOSED — transport returns `decision=block, decision_source=FALLBACK_*` | n/a | none |
| `_emit_span_start` / `_emit_span_end` | n/a — never blocks | n/a | n/a |

The "Opt-out" column makes it explicit that `NULLRUN_SKIP_BUDGET_CHECK=1`
is a **different category** of action than
`NULLRUN_SENSITIVE_FAIL_OPEN=1` (bypass vs. change semantics), despite
the similar naming. The full ADR (including transport error
classification into `NETWORK_ERROR` / `GATEWAY_ERROR` /
`BREAKER_OPEN` via `TransportErrorSource`) lives in the gateway
repository; see the link in the README.
"""

import asyncio
import functools
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, field
from typing import Any, Optional, TypeVar

import httpx

from nullrun.actions import ActionHandler, ActionType
from nullrun.breaker.exceptions import (
    BreakerError,
    CostLimitExceeded,
    LoopDetectedException,
    NullRunAuthenticationError,
    NullRunBlockedException,
    RetryStormException,
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
from nullrun.decision_history import DecisionHistoryRecorder
from nullrun.observability import metrics
from nullrun.transport import DecisionSource, FallbackMode, FlushConfig, Transport

KT = TypeVar("KT")
VT = TypeVar("VT")


class BoundedDict(OrderedDict, MutableMapping[KT, VT]):
    """
    Thread-safe dict with size limit. Evicts oldest entry on overflow (FIFO).

    Used for _workflow_costs, _loop_counts, _retry_counts to prevent unbounded
    memory growth during long-running SDK sessions.
    """

    def __init__(self, maxsize: int = 10_000) -> None:
        self._maxsize = maxsize
        super().__init__()

    def __setitem__(self, key: KT, value: VT) -> None:  # type: ignore[override]
        if key not in self and len(self) >= self._maxsize:
            self.popitem(last=False)
        super().__setitem__(key, value)

    def __repr__(self) -> str:
        return f"BoundedDict(maxsize={self._maxsize}, len={len(self)})"


@dataclass
class LocalDecision:
    """Decision from local check (no network round-trip)."""
    allowed: bool
    reason: str = None
    suggestion: str = None


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
class CheckDecision:
    """
    Decision returned from check_before_llm/check_before_tool.

    This is the non-exception-based API for pre-execution checks.
    """
    decision: str  # "allow", "block", "throttle"
    reservation_id: str | None
    remaining_budget_cents: int
    projected_cost_cents: int
    explanations: list[str]
    suggestions: list[str]

    def is_allowed(self) -> bool:
        return self.decision == "allow"

    def is_blocked(self) -> bool:
        return self.decision == "block"

    def is_throttled(self) -> bool:
        return self.decision == "throttle"


@dataclass(frozen=True)
class TrackResult:
    """Result of a track() call."""
    allowed: bool
    actions: list[str] = field(default_factory=list)
    local_cost_cents: int = 0
    blocked_reason: str | None = None
    policy_id: str | None = None

    def __bool__(self) -> bool:
        return self.allowed


logger = logging.getLogger(__name__)


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
    def from_dict(cls, data: dict[str, Any]) -> "Policy":
        """Create Policy from API response dict."""
        return cls(
            budget_cents=data.get("budget_cents", 1000),
            rate_limit=data.get("rate_limit", 100),
            loop_threshold=data.get("loop_threshold", 6),
            retry_threshold=data.get("retry_threshold", 5),
            anomaly_detection_enabled=data.get("anomaly_detection_enabled", True),
            loop_detection_enabled=data.get("loop_detection_enabled", True),
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
        rt.track({"type": "llm_call", "tokens": 100, "cost_cents": 5})
    """

    _instance: Optional["NullRunRuntime"] = None
    _lock = threading.Lock()

    def __init__(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        api_url: str = "https://api.nullrun.io",
        policy: Policy | None = None,
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
                "in 0.3.0 — see CHANGELOG.)"
            )
        # organization_id is set by _authenticate(); stays None until then.
        self.organization_id: str | None = None
        # Phase 139+: workflow_id is set by _authenticate() from the API
        # key's binding (organization_api_keys.workflow_id). Used as a
        # fallback for /check, /status, and span events when the user
        # hasn't entered a `with workflow(...)` context. None on legacy
        # keys (pre-139 or never used) — call sites must NOT invent one.
        self.workflow_id: str | None = None

        self._test_mode = _test_mode
        self.polling = polling

        self._policy: Policy | None = policy
        self._fallback_mode = "PERMISSIVE"
        self._timeout = 30
        self._max_retries = 3
        self._debug = debug
        self._transport: Transport | None = None

        # Local enforcement state
        # PER-WORKFLOW cost tracking - was a global counter before (BUG)
        self._workflow_costs: BoundedDict = BoundedDict(maxsize=10_000)
        self._loop_counts: BoundedDict = BoundedDict(maxsize=10_000)
        self._retry_counts: BoundedDict = BoundedDict(maxsize=10_000)
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

        # Default thresholds for local check (Phase 1 - hardcoded, not from backend)
        self._local_loop_threshold = 6
        self._local_rate_limit = 1000  # calls per minute

        # Coverage counters (Phase 3 of the production-readiness plan).
        # The instrumentation layer in `nullrun.instrumentation.auto`
        # calls `_safe_bump_coverage(runtime, "_coverage_seen" /
        # "_coverage_tracked" / "_coverage_streaming_skipped", host)`
        # so the dashboard can show "which LLM hosts the SDK is
        # seeing vs. successfully tracking". Previous versions
        # relied on `_safe_bump_coverage` to no-op when these
        # attributes were missing — the dashboard's coverage tab
        # was always empty.
        self._coverage_seen: dict[str, int] = {}
        self._coverage_tracked: dict[str, int] = {}
        self._coverage_streaming_skipped: dict[str, int] = {}

        # Remote control plane state (per-workflow, pushed from server via WS).
        # Unified model: effective_state = max(local_state, remote_state)
        # P1-1.1: All reads/writes go through the `_remote_state_for` /
        # `_set_remote_state` helpers under `_states_lock` to avoid the
        # TOCTOU race that was previously possible between the
        # "if workflow_id not in self._remote_states" check and the
        # subsequent dict write. `dict` itself is GIL-atomic for
        # individual ops, but the "check then insert" pattern in
        # `track()` is not. Re-entrant lock is used because the WS
        # callback and the synchronous `check_control_plane` can
        # both be on the same call path in nested cases.
        self._remote_states: dict[str, dict[str, Any]] = {}
        self._states_lock = threading.RLock()

        # Phase B: control plane transport (WS push vs HTTP poll).
        self._transport_mode: str = os.getenv("NULLRUN_TRANSPORT", "ws").lower()
        self._ws_thread: threading.Thread | None = None
        self._ws_stop_event = threading.Event()
        self._ws_connection: Any = None
        self._ws_loop: Any = None
        # Legacy HTTP-poll state — only used when transport mode is `http`.
        self._poll_thread: threading.Thread | None = None
        self._poll_running = False

        # Action handling + decision-history recorder.
        self._action_handler: ActionHandler | None = None
        self._recorder: DecisionHistoryRecorder | None = None
        self._is_recording = False

    def _remote_state_for(self, workflow_id: str) -> dict[str, Any]:
        """Get-or-create the per-workflow state dict under lock.

        Used by every read of `self._remote_states` to avoid the
        TOCTOU race that was previously possible."""
        with self._states_lock:
            state = self._remote_states.get(workflow_id)
            if state is None:
                state = {}
                self._remote_states[workflow_id] = state
            return state

    def _set_remote_state(
        self, workflow_id: str, state: dict[str, Any]
    ) -> None:
        """Atomically set the per-workflow state under lock."""
        with self._states_lock:
            self._remote_states[workflow_id] = state

        # Phase B: control plane transport. The SDK connects to the server's
        # WS endpoint and receives state push events (killed/paused) within
        # ~100ms of the operator action — vs the previous 1s HTTP poll.
        # The HTTP poll path is preserved as a fallback when
        # `NULLRUN_TRANSPORT=http` is set (env var defaults to `ws`).
        self._transport_mode: str = os.getenv("NULLRUN_TRANSPORT", "ws").lower()
        self._ws_thread: threading.Thread | None = None
        self._ws_stop_event = threading.Event()
        self._ws_connection: Any = None  # WebSocketConnection; typed loosely to avoid import cycle
        self._ws_loop: Any = None  # asyncio loop running in the WS thread
        # Legacy HTTP-poll state — only used when transport mode is `http`.
        self._poll_thread: threading.Thread | None = None
        self._poll_running = False

        # Action handling
        self._action_handler: ActionHandler | None = None

        # Local decision-history recorder
        self._recorder: DecisionHistoryRecorder | None = None
        self._is_recording = False

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

        # P2 (removed in 0.4.0): gRPC transport was deleted. The backend
        # proto is frozen and missing trace/span fields; HTTP is the
        # only supported transport. The NULLRUN_USE_GRPC env var is
        # now a no-op (logged once at WARNING if set).

        # Action handler + decision-history recorder are initialised
        # in BOTH the test-mode and the cloud-mode branches below,
        # so the runtime always has the attribute available when
        # ``track()`` consults ``_is_recording`` (Phase 5 cleanup
        # had a regression where ``_test_mode=True`` skipped the
        # recorder init and ``rt.track(...)`` raised AttributeError).

        # Initialize
        if os.getenv("NULLRUN_USE_GRPC"):
            logger.warning(
                "NULLRUN_USE_GRPC is set but the gRPC transport has been "
                "removed in SDK 0.4.0 — falling back to HTTP. The env var "
                "is now a no-op. See CHANGELOG.md for the migration timeline."
            )

        if self._test_mode:
            # Test mode: skip all network calls, use local policy
            self._policy = self._policy or Policy.default_local()
            self._transport.start()
            # Initialise the action handler and the local
            # decision-history recorder so ``track()`` and
            # ``start_recording()`` work in test mode. The previous
            # code only initialised them in the cloud branch
            # (below) and skipped them for ``_test_mode=True``,
            # which broke ``test_track_increments_counter`` and
            # any test that called ``rt.track(...)`` without going
            # through ``auth/verify``.
            self._action_handler = ActionHandler()
            self._recorder = DecisionHistoryRecorder(runtime=self)
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

        # Initialize local decision-history recorder
        self._recorder = DecisionHistoryRecorder(runtime=self)

        # Phase 1.4: Sensitive tools that require strict mode (pre-execution enforcement)
        # These tools MUST go through /execute endpoint, NOT direct execution
        self._sensitive_tools: set[str] = {
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

        # Convert fallback_mode string to FallbackMode enum
        fallback_mode_upper = self._fallback_mode.upper()
        if fallback_mode_upper == "STRICT":
            self._fallback_mode = FallbackMode.STRICT
        elif fallback_mode_upper == "CACHED":
            self._fallback_mode = FallbackMode.CACHED
        else:
            self._fallback_mode = FallbackMode.PERMISSIVE

        logger.info(
            f"NullRun Runtime initialized: "
            f"mode=cloud, "
            f"policy={self._policy}"
        )

    @classmethod
    def get_instance(cls) -> "NullRunRuntime":
        """Get the singleton runtime instance."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    # Re-read env vars at creation time to ensure we have latest values
                    api_key = os.getenv("NULLRUN_API_KEY")
                    api_url = os.getenv("NULLRUN_API_URL", "https://api.nullrun.io")
                    cls._instance = cls(
                        api_key=api_key,
                        api_url=api_url,
                    )
        else:
            # P6: Check if credentials have changed since last initialization
            # If so, reset and re-authenticate to prevent stale session issues
            current_api_key = os.getenv("NULLRUN_API_KEY")
            current_api_url = os.getenv("NULLRUN_API_URL", "https://api.nullrun.io")
            existing = cls._instance

            # Check if key or URL changed
            key_changed = current_api_key != existing.api_key
            url_changed = current_api_url != existing.api_url

            if key_changed or url_changed:
                logger.info(
                    f"Credentials changed: api_key={'***' if key_changed else 'unchanged'}, "
                    f"api_url={'changed' if url_changed else 'unchanged'} - reinitializing"
                )
                existing.shutdown()
                cls._instance = None
                # Recurse to create fresh instance with new credentials
                return cls.get_instance()

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
            # Route through _signed_post so HMAC + W3C trace context
            # are applied automatically. Phase 1: HMAC always-on.
            # /auth/verify accepts a signed body for symmetry with
            # the rest of the API surface.
            response = self._transport._signed_post(
                "/api/v1/auth/verify",
                {"api_key": self.api_key},
                timeout=10.0,
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
                # `None` on legacy keys (pre-139 or never-used) — call
                # sites that NEED a workflow (check_workflow_budget,
                # check_control_plane, span events) will fall through to
                # the contextvar when self.workflow_id is None, exactly
                # like before. New keys always have this set.
                self.workflow_id = data.get("workflow_id")

                # Handle key rotation: server may return new key_version and secret_key
                # This allows seamless secret key rotation without downtime
                new_key_version = data.get("key_version")
                new_secret_key = data.get("secret_key")

                if new_key_version is not None and new_secret_key is not None:
                    old_version = getattr(self, '_key_version', None)
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
        """Fetch policy from backend and cache locally."""
        if not self.organization_id:
            self._policy = Policy.default_local()
            return

        try:
            # Route through _signed_post (Phase 1).
            response = self._transport._signed_post(
                "/api/v1/policies",
                {"organization_id": self.organization_id},
            )

            if response.status_code == 200:
                data = response.json()
                if data and len(data) > 0:
                    self._policy = Policy.from_dict(data[0])
                    logger.info(f"Policy fetched: {self._policy}")
                    return
        except Exception as e:
            logger.warning(f"Failed to fetch policy: {e}")

        # Fallback to default
        self._policy = Policy.default_local()

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
            target=self._poll_commands,
            daemon=True,
            name="nullrun-poller"
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
        logger.info(
            "Started WS control plane listener (org=%s)", self.organization_id
        )

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
        except Exception as e:  # noqa: BLE001 — background thread, must never die silently
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
                self._set_remote_state(workflow_id, {
                    "state": state.get("state", "Normal"),
                    "version": state.get("version", 0),
                    "reason": state.get("reason"),
                    "updated_at": state.get("updated_at", 0),
                })
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
                # Get all workflows we're tracking. Snapshot the keys
                # under lock to avoid `RuntimeError: dictionary
                # changed size during iteration` if a concurrent
                # `_set_remote_state` adds a workflow mid-poll.
                with self._states_lock:
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

          1. `explicit` — passed by the call site (e.g. contextvar in
             track_event or the user-supplied arg in check_control_plane)
          2. `self.workflow_id` — bound to the API key by the server
             (Phase 139+). Set during _authenticate(). None on legacy
             keys.
          3. None — caller is in cloud mode but has no workflow scope.
             /check falls through to org-level policy; /status is
             skipped; span events are emitted without workflow_id
             (orphan, as before).

        The SDK does NOT auto-generate a workflow_id. The Phase 139
        invariant — workflow is derived server-side from the key, never
        invented by the SDK — is preserved.
        """
        if explicit:
            return explicit
        return self.workflow_id

    def _fetch_remote_state(self, workflow_id: str) -> None:
        """Fetch remote state for a specific workflow from /status endpoint.

        Phase 1: routed through ``_transport._signed_request`` so the
        canonical header set (X-API-Key, X-API-Version, optional HMAC)
        is applied in one place. A GET has no body, so no signature
        is computed — the server authenticates via the X-API-Key
        header.
        """
        try:
            response = self._transport._signed_request(
                "GET",
                f"/api/v1/status/{workflow_id}",
                timeout=5.0,
            )
            if response.status_code == 200:
                data = response.json()
                self._set_remote_state(workflow_id, {
                    "state": data.get("state", "Normal"),
                    "version": data.get("version", 0),
                    "reason": data.get("reason"),
                    "updated_at": data.get("updated_at", 0),
                })
                logger.debug(f"Remote state for {workflow_id}: {data}")
        except Exception as e:
            logger.debug(f"Failed to fetch remote state for {workflow_id}: {e}")

    def check_control_plane(self, workflow_id: str | None) -> None:
        """
        Check remote control plane state and raise if workflow is paused/killed.

        This is called in the execution path after local enforcement.
        The unified state model: effective_state = max(local_state, remote_state)

        Args:
            workflow_id: Optional workflow id. Resolved through
                `_resolve_workflow_id` (contextvar → API-key-bound
                workflow → no-op). `None` is the canonical "no
                workflow scoped" value — the gate then no-ops.

        Raises:
            WorkflowPausedException: If workflow is paused on server
            WorkflowKilledInterrupt: If workflow is killed on server
        """
        # Phase 139+: prefer the explicit arg (contextvar-supplied), fall
        # back to the API key's bound workflow. None on legacy keys —
        # in that case there's no workflow to check, so we no-op
        # (preserves pre-139 behavior for keys that have never been
        # workflow-bound).
        resolved = self._resolve_workflow_id(workflow_id)
        if not resolved:
            return
        workflow_id = resolved

        # Ensure we have the latest remote state. The "is in cache"
        # check is done under the lock to avoid the TOCTOU race
        # where a concurrent `_set_remote_state` could change the
        # answer between the check and the read.
        with self._states_lock:
            in_cache = workflow_id in self._remote_states
        if not in_cache:
            # Fetch synchronously if not in cache yet
            self._fetch_remote_state(workflow_id)

        with self._states_lock:
            remote_state = self._remote_states.get(workflow_id, {})
        state = remote_state.get("state", "Normal")

        if state == "Paused":
            reason = remote_state.get("reason", "remote pause")
            raise WorkflowPausedException(
                workflow_id=workflow_id,
                reason=reason,
            )
        elif state == "Killed":
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

        Decision → exception mapping:
            "block"   → WorkflowKilledInterrupt   (hard policy / reservation error)
            "throttle"→ WorkflowPausedException   (insufficient budget, can resume)
            "allow"   → return

        Fail-OPEN: any transport error (network, timeout, 5xx) is logged
        at warning level and the caller proceeds. This mirrors the
        pattern in `check_control_plane` — a transient backend outage
        must never freeze the user's agent. The /track fast path also
        does not gate on budget, so the worst case under /gate failure
        is that we revert to the pre-C behaviour: budget enforcement is
        advisory until the gateway recovers.

        Uses `estimated_tokens=1` (the minimum the API accepts). Goal
        is the binary question "is there any budget left?", not cost
        prediction — the backend recomputes the authoritative cost on
        /track from the real token count.

        Opt-out: set `NULLRUN_SKIP_BUDGET_CHECK=1` to disable the
        pre-flight. Useful in tests where the org's API key has
        exhausted its budget from previous runs and the test only
        wants to exercise a non-budget code path.
        """
        if os.environ.get("NULLRUN_SKIP_BUDGET_CHECK", "").strip() == "1":
            logger.debug("check_workflow_budget: skipped via NULLRUN_SKIP_BUDGET_CHECK=1")
            return

        from nullrun.context import get_workflow_id

        # Phase 139+: prefer the user-set contextvar (explicit `with
        # workflow(...)` block), fall back to the API key's bound
        # workflow. Returns None only on legacy keys that have never
        # been workflow-bound — in that case the check is silently
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
            logger.warning(
                f"check_workflow_budget: /gate unavailable, failing open: {exc}"
            )
            return

        decision = response.get("decision", "allow")
        if decision == "block":
            reasons = response.get("explanations") or ["block"]
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
        """Shutdown runtime gracefully.

        Defensive against missing attributes: the test-mode
        constructor does not initialize `_poll_thread`, `_ws_thread`,
        `_ws_stop_event`, etc. — `getattr` is used everywhere a
        missing attribute is possible. Without this, a test-mode
        runtime that calls `shutdown()` raises `AttributeError`."""
        # Stop the HTTP poller (legacy path) if it was started.
        self._poll_running = False
        poll_thread = getattr(self, "_poll_thread", None)
        if poll_thread is not None and poll_thread.is_alive():
            poll_thread.join(timeout=2.0)

        # Stop the WS control plane listener (Phase B). Closing the
        # connection causes the receive task to unblock, the loop to
        # exit, and the thread to terminate.
        ws_stop_event = getattr(self, "_ws_stop_event", None)
        if ws_stop_event is not None:
            ws_stop_event.set()
        conn = getattr(self, "_ws_connection", None)
        ws_loop = getattr(self, "_ws_loop", None)
        if conn is not None and ws_loop is not None:
            try:
                future = asyncio.run_coroutine_threadsafe(conn.close(), ws_loop)
                future.result(timeout=2.0)
            except Exception as e:
                logger.debug(f"WS close on shutdown failed (best-effort): {e}")
        ws_thread = getattr(self, "_ws_thread", None)
        if ws_thread is not None and ws_thread.is_alive():
            ws_thread.join(timeout=2.0)

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
            `cost_cents` is NOT a valid event key — the SDK does not
            estimate cost. The backend computes it from tokens + the
            organization's policy.

        Returns:
            Dict with enforcement results:
            - allowed: bool
            - actions: list of actions taken
            - local_cost: current local cost
            - blocked_reason: str (if blocked locally)
            - blocked_suggestion: str (if blocked locally)

        Raises:
            CostLimitExceeded: If local policy limit exceeded
            LoopDetectedException: If loop detected
            RetryStormException: If retry storm detected
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
                    "local_cost_cents": self._workflow_costs.get(
                        event.get("workflow_id") or "", 0
                    ),
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
        tool_name = event.get('tool_name', 'unknown')
        self._loop_tracker.record(tool_name)
        self._rate_tracker.record()

        # Enrich event with context
        enriched = self._enrich_event(event)
        logger.debug(
            "Event enriched: workflow_id=%s, tokens=%s",
            enriched.get("workflow_id"),
            enriched.get("tokens"),
        )

        # Record to local session if active
        if self._is_recording and self._recorder:
            self._recorder.record_event(enriched)

        # Register workflow for remote state polling. workflow_id
        # may be None on legacy keys — that's fine, the no-op
        # branch in check_control_plane will skip polling.
        workflow_id = enriched.get("workflow_id")
        if workflow_id:
            # Use the helper to avoid the TOCTOU race between the
            # "not in dict" check and the write.
            self._remote_state_for(workflow_id)

        # Local policy enforcement (BEFORE sending)
        if self._policy:
            self._check_local_limits(enriched)

        # Check remote control plane (after local enforcement)
        # This catches server-initiated pause/kill. Resolves
        # contextvar → self.workflow_id → no-op (legacy keys).
        self.check_control_plane(workflow_id)

        # Buffer for transport. (gRPC path was removed in 0.4.0 —
        # the backend proto is frozen and missing trace/span fields.)
        # The wire payload must NOT include `cost_cents` — the SDK
        # does not estimate cost; the backend recomputes it from
        # `tokens` + the org's pricing policy.
        if self._transport is not None:
            self._transport.track(self._strip_wire_only_fields(enriched))

        # Update metrics (thread-safe)
        metrics.inc_runtime("track_calls")

        return {
            "allowed": True,
            "actions": [],
            "local_cost_cents": self._workflow_costs.get(workflow_id, 0),
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
        """
        return tool_name in self._sensitive_tools or tool_name in self._strict_mode_tools

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
        """
        self._strict_mode_tools.add(tool_name)

    def remove_sensitive_tool(self, tool_name: str) -> None:
        """
        Remove a tool from the sensitive tools list.

        Args:
            tool_name: Name of the tool to remove from sensitive list

        Example:
            runtime = NullRunRuntime.get_instance()
            runtime.remove_sensitive_tool("my.custom_tool")
        """
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
        # (no local_mode branch — api_key is now required, see T3-S2)
        result = self._transport.execute(
            organization_id=organization_id,
            execution_id=workflow_id,
            trace_id=trace_id,
            tool=tool_name,
            input_data=input_data,
            mode=mode,
            fallback_mode=self._fallback_mode,
        )

        # Update metrics (thread-safe)
        metrics.inc_runtime("execute_calls")

        # Check if execution is allowed
        if result.get("decision") == "block":
            metrics.inc_runtime("execute_blocked")
            raise NullRunBlockedException(
                workflow_id=workflow_id or "<unknown>",
                reason=result.get("explanation", "policy violation"),
                tool_name=tool_name,
            )

        metrics.inc_runtime("execute_allowed")
        return result

    def wrap_tool(self, tool_name: str, tool_fn: Callable[..., Any]) -> Callable[..., Any]:
        """
        Wrap a tool function with pre-execution enforcement.

        The wrapped function will:
        1. Call /execute before the tool runs
        2. Raise NullRunBlockedException if blocked
        3. Track the event after execution

        Args:
            tool_name: Name of the tool (for policy lookup)
            tool_fn: The original tool function

        Returns:
            Wrapped function
        """
        @functools.wraps(tool_fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Pre-execution check (raises if blocked)
            input_data = {"args": args, "kwargs": kwargs}
            self.execute(tool_name, input_data)

            # Execute if allowed
            output = tool_fn(*args, **kwargs)

            # Post-execution tracking
            self.track_tool(tool_name=tool_name)

            return output
        return wrapper

    def wrap(self, tool_fn: Callable[..., Any]) -> Callable[..., Any]:
        """
        Wrap a tool function with NullRun protection.

        Unlike wrap_tool, this uses the function name as the tool name.
        Useful for wrapping any function without explicitly naming it.

        Example:
            db_query = runtime.wrap(original_db_query)
            result = db_query("SELECT * FROM users")  # Auto-protected

        Args:
            tool_fn: The original tool function

        Returns:
            Wrapped function that auto-calls execute() before running
        """
        from nullrun.context import get_workflow_id

        tool_name = tool_fn.__name__

        @functools.wraps(tool_fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Pre-execution check
            input_data = {"args": args, "kwargs": kwargs}
            result = self.execute(tool_name, input_data)

            # Raise if blocked. Resolve workflow_id from the active
            # contextvar — `execute()` already raises NullRunBlockedException
            # for a real gateway block, so this branch only fires if a
            # future caller returns a dict with decision=block without
            # raising. Use the same fallback the rest of the runtime
            # uses ("<unknown>") when no contextvar is set.
            if result.get("decision") == "block":
                resolved_wf = get_workflow_id() or "<unknown>"
                raise NullRunBlockedException(
                    workflow_id=resolved_wf,
                    reason=result.get("explanation", "policy violation"),
                    tool_name=tool_name,
                )

            # Execute if allowed
            output = tool_fn(*args, **kwargs)

            # Post-execution tracking
            self.track_tool(tool_name=tool_name)

            return output
        return wrapper

    def check_before_llm(
        self,
        model: str,
        estimated_tokens: int | None = None,
        operation_name: str | None = None,
    ) -> CheckDecision:
        """
        Pre-execution check for LLM calls.
        Returns decision object - does NOT raise exception.

        Args:
            model: Model name (e.g., "gpt-4", "claude-3-opus")
            estimated_tokens: Estimated token count (optional)
            operation_name: Optional name for this operation

        Returns:
            CheckDecision with allow/block/throttle decision
        """
        event = {
            "type": "llm_call",
            "model": model,
            "tokens": estimated_tokens or 0,
            "check_type": "llm",
        }
        return self._check(event, operation_name)

    def check_before_tool(
        self,
        tool_name: str,
        operation_name: str | None = None,
    ) -> CheckDecision:
        """
        Pre-execution check for tool calls.
        Returns decision object - does NOT raise exception.

        Args:
            tool_name: Name of the tool to check
            operation_name: Optional name for this operation

        Returns:
            CheckDecision with allow/block/throttle decision
        """
        event = {
            "type": "tool_call",
            "tool_name": tool_name,
            "check_type": "tool",
        }
        return self._check(event, operation_name)

    def enforce_check_before_llm(
        self,
        model: str,
        estimated_tokens: int | None = None,
        operation_name: str | None = None,
    ) -> CheckDecision:
        """
        Strict mode: raises NullRunBlockedException if blocked.

        Args:
            model: Model name
            estimated_tokens: Estimated token count (optional)
            operation_name: Optional name for this operation

        Returns:
            CheckDecision if allowed

        Raises:
            NullRunBlockedException: If decision is "block"
        """
        decision = self.check_before_llm(model, estimated_tokens, operation_name)
        if decision.is_blocked():
            raise NullRunBlockedException(
                workflow_id=get_workflow_id() or "<unknown>",
                reason="; ".join(decision.explanations) or "policy violation",
                tool_name=model,
                reservation_id=decision.reservation_id,
                suggestions=decision.suggestions,
            )
        return decision

    def _check(self, event: dict[str, Any], operation_name: str | None) -> CheckDecision:
        """
        Internal check implementation for pre-execution checks.

        Args:
            event: Event dict with check_type, model, tool_name, tokens
            operation_name: Optional operation name

        Returns:
            CheckDecision from the backend
        """
        from nullrun.context import get_workflow_id

        organization_id = self.organization_id or "local"
        execution_id = get_workflow_id()
        operation_id = operation_name or str(uuid.uuid4())

        # Build check request
        check_req = {
            "organization_id": organization_id,
            "execution_id": execution_id,
            "operation_id": operation_id,
            "check_type": event.get("check_type", "llm"),
            "model": event.get("model"),
            "tool_name": event.get("tool_name"),
            "estimated_tokens": event.get("tokens"),
        }

        # Call /api/v1/check endpoint via transport
        response = self._transport.check(check_req)

        return CheckDecision(
            decision=response.get("decision", "block"),
            reservation_id=response.get("reservation_id"),
            remaining_budget_cents=response.get("remaining_budget_cents", 0),
            projected_cost_cents=response.get("projected_cost_cents", 0),
            explanations=response.get("explanations", []),
            suggestions=response.get("suggestions", []),
        )

    def evaluate(
        self,
        tool_name: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Evaluate policies without executing a tool.

        Useful for checking "what if" scenarios before running
        an agent or to pre-validate tool permissions.

        Args:
            tool_name: Name of the tool to evaluate
            context: Optional context dict with tool-specific parameters

        Returns:
            Dict with:
                - decision: "allow" | "block" | "flag" | "pause" | "require_approval"
                - decision_source: "gateway" | "cached" | "fallback" | "local"
                - explanation: Human-readable explanation
                - policy_version: Policy version used
                - matched_rules: List of matching policy rules
                - scores: Dict of rule_id -> score
        """
        from nullrun.context import get_trace_id, get_workflow_id

        organization_id = self.organization_id or "local"
        workflow_id = get_workflow_id()
        trace_id = get_trace_id() or str(uuid.uuid4())

        # Route through `transport.evaluate()` (the public API) so the
        # call benefits from the same connection pool, HMAC signing,
        # circuit breaker, and retry policy as `execute()`. The
        # previous implementation reached into `transport._client`
        # directly, which silently bypassed the circuit breaker — a
        # production hazard on a long-lived runtime.
        #
        # `transport.evaluate()` is fail-CLOSED on transport error by
        # default (raises NullRunTransportError) per ADR-008. We
        # swallow that here because the public `runtime.evaluate()`
        # contract is "always return a dict" (used for pre-validation
        # / dry-run), not "halt the agent on a backend outage".
        from nullrun.breaker.exceptions import NullRunTransportError

        try:
            return self._transport.evaluate(
                organization_id=organization_id,
                execution_id=workflow_id,
                trace_id=trace_id,
                tool=tool_name,
                context=context or {},
                on_transport_error="closed",
            )
        except NullRunTransportError as exc:
            # Transport unavailable — return a local-fallback decision
            # so pre-validation never halts the user's agent.
            is_sensitive = self.is_sensitive_tool(tool_name)
            return {
                "decision": "allow" if not is_sensitive else "block",
                "decision_source": DecisionSource.FALLBACK,
                "explanation": (
                    f"Evaluation endpoint unavailable ({exc.source.value}): {exc}"
                ),
                "policy_version": 0,
                "matched_rules": [],
                "scores": {},
                "allow_execution": not is_sensitive,
            }

    def start_recording(self, workflow_id: str, metadata: dict[str, Any] = None) -> str:
        """
        Start recording events for local decision history.

        Args:
            workflow_id: ID of the workflow to record
            metadata: Optional metadata about the session

        Returns:
            session_id for this recording
        """
        self._is_recording = True
        if self._recorder:
            return self._recorder.start_recording(workflow_id, metadata)
        return ""

    def stop_recording(self):
        """
        Stop recording and return the session.

        Returns:
            The recorded session, or None if not recording
        """
        self._is_recording = False
        if self._recorder:
            return self._recorder.stop_recording()
        return None

    def _enrich_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Add context fields to event."""
        enriched = dict(event)  # Don't modify original

        # Phase 139+: workflow_id from context, else from the API
        # key's binding (set in _authenticate). Stays unset on legacy
        # keys — emitted events then carry no workflow_id (orphan, as
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

    @staticmethod
    def _strip_wire_only_fields(event: dict[str, Any]) -> dict[str, Any]:
        """Remove fields the SDK adds for local enforcement but that
        do not belong on the wire (the backend recomputes them from
        `tokens` + the org's pricing policy).

        Two fields are stripped:

          - ``cost_cents``: backend recomputes from tokens + pricing.
          - ``_fingerprint``: sink-only dedup key for the
            ``_seen_track_fingerprints`` LRU; never reaches the
            gateway.

        Centralized so the wire-format contract is in one place; if
        a future SDK revision adds more local-only fields they land
        here too.
        """
        return {
            k: v
            for k, v in event.items()
            if k not in ("cost_cents", "_fingerprint")
        }

    def _check_local_limits(self, event: dict[str, Any]) -> None:
        """
        Check local policy limits without network call.

        This provides INSTANT enforcement with zero latency.
        Raises specific exceptions and triggers actions.
        """
        cost_cents = event.get("cost_cents", 0)
        tool_name = event.get("tool_name")
        is_retry = event.get("is_retry", False)
        workflow_id = event.get("workflow_id", "unknown")

        # Update local cost (PER-WORKFLOW, not global)
        current_cost = self._workflow_costs.get(workflow_id, 0)
        new_cost = current_cost + cost_cents
        self._workflow_costs[workflow_id] = new_cost

        # Budget exceeded (per-workflow)
        if new_cost > self.policy.budget_cents:
            exc = CostLimitExceeded(
                workflow_id=workflow_id,
                cost=new_cost / 100.0,
                limit=self.policy.budget_cents / 100.0,
            )
            self._trigger_action(ActionType.KILL, workflow_id, str(exc))
            raise exc

        # Loop detection (per-workflow, per-tool)
        if self.policy.loop_detection_enabled and tool_name:
            key = f"{workflow_id}:{tool_name}"
            count = self._loop_counts.get(key, 0) + 1
            self._loop_counts[key] = count
            if count >= self.policy.loop_threshold:
                exc = LoopDetectedException(
                    workflow_id=workflow_id,
                    tool_name=tool_name,
                    count=count,
                )
                self._trigger_action(ActionType.KILL, workflow_id, str(exc))
                raise exc

        # Retry detection (per-workflow)
        if self.policy.retry_detection_enabled and is_retry:
            key = f"{workflow_id}:retries"
            count = self._retry_counts.get(key, 0) + 1
            self._retry_counts[key] = count
            if count >= self.policy.retry_threshold:
                exc = RetryStormException(
                    workflow_id=workflow_id,
                    count=count,
                )
                self._trigger_action(ActionType.KILL, workflow_id, str(exc))
                raise exc

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
        tool_name = event.get('tool_name', 'unknown')

        # Check loop count (6 same tool calls in 60s window)
        loop_count = self._loop_tracker.count(tool_name, window=60)
        if loop_count >= self._local_loop_threshold:
            return LocalDecision(
                allowed=False,
                reason="loop_detected",
                suggestion="retry after 60s"
            )

        # Check rate limit (max 1000/min default)
        if self._rate_tracker.exceeds_limit(self._local_rate_limit):
            return LocalDecision(
                allowed=False,
                reason="rate_limit",
                suggestion="slow down"
            )

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
                to 0 — embeddings and reasoning-only calls have no
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
        # Lazy import to keep the runtime import graph acyclic —
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
        # the field is omitted — _enrich_event will fall back to the
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
        automatically — see `track_llm` for the rationale.

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
        # token count — they're bookkeeping, not consumption. Default
        # to 0 so the deserializer accepts the event; the cost
        # computation in the handler treats 0 tokens as no-op.
        event.setdefault("tokens", 0)
        # Phase 3: emit a stable fingerprint so the dedup LRU at
        # the track() sink can collapse repeat emissions of the
        # same event (e.g. when the user calls track_event manually
        # AND the httpx transport hook fires for the same LLM
        # call). Field is stripped before wire send (see
        # `_strip_wire_only_fields`).
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

        track({"type": "llm_call", "tokens": 100, "cost_cents": 5})
    """
    return get_runtime().track(event)


# Phase 3.4: explicit alias for `track()` — same call signature, friendlier
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
            to 0 — embeddings and reasoning-only calls have no
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
