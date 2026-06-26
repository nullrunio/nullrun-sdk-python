"""Layer 3 of the "give the user a chance" design — the
``nullrun.status()`` introspection API.

Pre-Layer-3: the only way to know if the SDK was healthy was to
trigger a protected call and see whether it raised. There was no
synchronous snapshot — the user could not "look at the SDK" in a
debugger or in a dashboard without instrumenting every code
path.

Post-Layer-3: ``nullrun.status()`` returns a frozen
``NullRunStatus`` dataclass describing the runtime's current
state — backend reachability, WS connection, policy freshness,
workflow state, and a ring buffer of recent errors. Designed
for the "the agent is stuck, what's wrong?" runbook:

  1. Open the dashboard / dev console.
  2. ``print(nullrun.status())``.
  3. See ``state="degraded"`` and ``fallback_reason="backend 401
     at 15:58:01"`` — root cause in one line.

The status is a synchronous SNAPSHOT, not a live stream. It is
safe to call from any thread (including the agent loop, the
transport flush thread, or a debug console). The dataclass is
frozen (``frozen=True``) so it can be safely shared / cached.

## State-derivation rules

The ``state`` field is the headline answer. It is derived from
the rest of the snapshot — the user can read it as "is the SDK
doing what I think it's doing?" without inspecting the rest:

  * ``"misconfigured"`` — no api_key, or ``init()`` raised a
    config error and the runtime was never bound. The SDK is
    not operating; fix the config.
  * ``"offline"`` — backend is not reachable AND no successful
    ``/gate`` call has ever landed. Every cost-bearing call will
    be rejected by the SDK's fail-CLOSED path. Fix the network /
    backend.
  * ``"degraded"`` — one or more of: WS disconnected, circuit
    breaker open, workflow state != Normal. The SDK is operating
    but with reduced guarantees. Surface the ``workflow_state.reason``
    to the user.
  * ``"ok"`` — everything healthy. This is the steady state.

Note (0.7.0): SDK no longer maintains a local ``Policy`` cache. All
enforcement decisions arrive from the backend via ``/gate`` and
``/execute``. The "cached policy" degradation state from prior
versions is gone — SDK is either talking to the backend or it isn't.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Headline states — string literals, not an Enum, so the
# snapshot is JSON-serialisable without an adapter.
STATE_OK = "ok"
STATE_DEGRADED = "degraded"
STATE_OFFLINE = "offline"
STATE_MISCONFIGURED = "misconfigured"


@dataclass(frozen=True)
class RecentError:
    """One entry in the status's recent-errors ring buffer.

    Captured by the runtime's ``_record_error`` method (called
    from ``_emit_sdk_error``, which is the same path the
    Layer-2 ``on_error`` hook uses). Capacity is bounded so a
    long-lived process does not leak memory even if the SDK
    raises thousands of errors per minute.

    Fields are best-effort — a hook / record may receive
    ``None`` for ``workflow_id`` / ``tool_name`` when the
    error fired before the runtime was bound.
    """

    #: Stable error code (e.g. ``"NR-A003"``).
    error_code: str

    #: Stage identifier from the Layer-2 ``STAGES`` catalogue.
    stage: str

    #: Workflow at the time of the error, or ``None`` for
    #: pre-bind errors.
    workflow_id: str | None

    #: Tool at the time of the error, or ``None`` for
    #: non-tool errors.
    tool_name: str | None

    #: UTC wall-clock timestamp.
    timestamp: datetime

    #: Truncated message (200 chars) — long enough for human
    #: reading, short enough to keep the snapshot small.
    message: str


@dataclass(frozen=True)
class WorkflowState:
    """The kill/pause state for the bound workflow, as last
    pushed by the WS control plane.

    Mirrors the shape of the WS ``state_change`` message so the
    user can read ``status.workflow_state.state`` and know
    whether the body will run on the next call.

    CP1 fix (2026-06-26): the backend WsWorkflowState enum has 5
    variants, not 3 — Flagged and Tripped were previously silently
    treated as Normal. The SDK now handles all 5 explicitly in
    ``runtime.check_control_plane``; this dataclass reflects the
    full set so the operator-facing status mirrors reality.
    """

    workflow_id: str
    state: str  # "Normal" | "Paused" | "Killed" | "Flagged" | "Tripped"
    version: int
    reason: str | None = None


@dataclass(frozen=True)
class NullRunStatus:
    """Synchronous snapshot of the SDK runtime.

    Build with ``NullRunRuntime.status()`` or the top-level
    ``nullrun.status()`` shortcut. The dataclass is frozen so
    snapshots can be cached, shared across threads, and
    compared with ``==`` without defensive copying.
    """

    # Headline. One of STATE_* above. Read this first.
    state: str

    # Auth
    api_key_valid: bool | None  # None = never tested
    api_key_prefix: str | None  # first 10 chars, never the full key
    organization_id: str | None
    workflow_id: str | None
    api_url: str

    # Connectivity
    backend_reachable: bool | None  # None = never tested
    ws_connected: bool | None  # None = not started / unknown

    # Workflow
    workflow_state: WorkflowState | None

    # Recent errors (ring buffer, bounded)
    recent_errors: list[RecentError] = field(default_factory=list)

    def is_healthy(self) -> bool:
        """``True`` iff ``state == "ok"``. Convenience for
        guard clauses:

            if not nullrun.status().is_healthy():
                return render_degraded_banner(status)
        """
        return self.state == STATE_OK

    def summary(self) -> str:
        """One-line human-readable summary. Designed for
        ``print(nullrun.status().summary())`` in a debug
        console.

        Example outputs:
            "NullRunStatus(ok, api_key=nr_live_S, org=…, wf=…)"
            "NullRunStatus(degraded, wf_state=Killed, backend=unreachable)"
            "NullRunStatus(offline, ws=False, errors=2)"
        """
        bits = [f"NullRunStatus({self.state}"]
        if self.api_key_prefix:
            bits.append(f"api_key={self.api_key_prefix}")
        if self.organization_id:
            bits.append(f"org={self.organization_id[:8]}")
        if self.workflow_id:
            bits.append(f"wf={self.workflow_id[:8]}")
        if self.workflow_state and self.workflow_state.state != "Normal":
            bits.append(f"wf_state={self.workflow_state.state}")
        if self.backend_reachable is False:
            bits.append("backend=unreachable")
        if self.ws_connected is False:
            bits.append("ws=False")
        if self.recent_errors:
            bits.append(f"errors={len(self.recent_errors)}")
        bits.append(")")
        return " ".join(bits)


class _RecentErrorRing:
    """Thread-safe ring buffer for ``RecentError`` entries.

    Not exposed — the runtime owns one of these and feeds it
    from ``_record_error``. The status builder reads the
    snapshot list when constructing the dataclass.

    Capacity is fixed (``DEFAULT_CAPACITY = 10``) so a
    long-lived process cannot leak memory even when the SDK
    raises thousands of errors per minute. The deque's
    ``maxlen`` does the eviction; the lock guards the
    iteration.
    """

    DEFAULT_CAPACITY = 10

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        import threading

        self._lock = threading.Lock()
        self._items: deque[RecentError] = deque(maxlen=capacity)

    def push(self, entry: RecentError) -> None:
        with self._lock:
            self._items.append(entry)

    def snapshot(self) -> list[RecentError]:
        with self._lock:
            return list(self._items)
