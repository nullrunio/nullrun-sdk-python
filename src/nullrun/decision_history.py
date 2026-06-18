"""
Local decision-history recorder for the NullRun SDK.

What this module does:
    - Records events emitted by the SDK during a workflow run (LLM calls,
      tool calls, cost events, retries) into a local in-memory session.
    - Lets you save the session to disk, load it later, and inspect it
      offline (e.g. for cost analysis or debugging).
    - Lets you re-emit recorded events through the local runtime tracker
      so you can reproduce the cost line items locally — useful for
      integration tests that need to simulate a past run's spend pattern.

What this module does NOT do (honest scope):
    - It does NOT replay LLM calls. NULLRUN never stores request/response
      payloads, and the SDK never holds provider credentials, so there is
      nothing to re-send to a model.
    - It does NOT contact the backend. The server-side Decision History
      feature (the one you see in the dashboard) lives on the gateway and
      is queried via the HTTP API. This module is the *client-side*
      counterpart for offline analysis only.

For agentic replay with full request/response capture, use Helicone /
LangSmith / Langfuse. NULLRUN is a policy-enforcement plane, not a session
recorder.
"""

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from nullrun.runtime import NullRunRuntime

logger = logging.getLogger(__name__)


@dataclass
class RecordedEvent:
    """
    One event captured by the local recorder.

    Captures the metadata needed to reconstruct the trace line items
    locally, plus the original raw event payload for re-emission through
    the runtime tracker.

    Note (Commit 3): `cost_cents` is a deprecated field. The SDK no
    longer computes cost — the backend does it from tokens + the org's
    policy. Cost-related rollups in this module will read 0 until
    the backend echoes the recomputed cost back via a future
    /track response. We keep the field so the dataclass shape
    doesn't churn, but no event source populates it anymore.
    """
    timestamp: str  # ISO format
    event_type: str  # "llm_call", "tool_call", etc.
    workflow_id: str
    trace_id: str | None = None
    span_id: str | None = None
    tokens: int = 0
    cost_cents: int = 0  # deprecated — see note above
    tool_name: str | None = None
    is_retry: bool = False
    latency_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    # Original raw data
    raw_event: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecordingSession:
    """
    A local recording session containing events captured by the SDK.

    Can be saved to disk and re-loaded later for offline analysis or for
    re-emitting events through the local runtime tracker.
    """
    session_id: str
    workflow_id: str
    started_at: str  # ISO format
    ended_at: str | None = None
    events: list[RecordedEvent] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def add_event(self, event: RecordedEvent) -> None:
        """Add an event to the session."""
        self.events.append(event)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "session_id": self.session_id,
            "workflow_id": self.workflow_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "events": [asdict(e) for e in self.events],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecordingSession":
        """Create from dictionary."""
        events = [RecordedEvent(**e) for e in data.get("events", [])]
        return cls(
            session_id=data["session_id"],
            workflow_id=data["workflow_id"],
            started_at=data["started_at"],
            ended_at=data.get("ended_at"),
            events=events,
            metadata=data.get("metadata", {}),
        )

    def save(self, path: str) -> None:
        """Save session to JSON file."""
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        logger.info(f"Saved recording session to {path}")

    @classmethod
    def load(cls, path: str) -> "RecordingSession":
        """Load session from JSON file."""
        with open(path) as f:
            data = json.load(f)
        logger.info(f"Loaded recording session from {path}")
        return cls.from_dict(data)


class DecisionHistoryRecorder:
    """
    Local event recorder for the SDK.

    Captures events emitted by the SDK during a workflow run and lets you
    save, load, and re-emit them locally. See the module docstring for the
    honest scope of this feature (it is not agentic replay).

    Usage:
        # Recording
        recorder = DecisionHistoryRecorder()
        recorder.start_recording("my-workflow")
        # ... run agent ...
        session = recorder.stop_recording()
        session.save("recording.json")

        # Offline inspection
        session = RecordingSession.load("recording.json")
        summary = recorder.estimate_cost(session)
    """

    def __init__(self, runtime: Optional["NullRunRuntime"] = None):
        from nullrun.runtime import NullRunRuntime
        self._runtime_ref = runtime
        self._runtime: NullRunRuntime | None = None  # Lazy loaded
        self._current_session: RecordingSession | None = None
        self._is_recording = False
        self._event_callback: Callable | None = None

    @property
    def runtime(self) -> "NullRunRuntime":
        """Lazy load the runtime."""
        if self._runtime is None:
            from nullrun.runtime import NullRunRuntime
            self._runtime = self._runtime_ref or NullRunRuntime.get_instance()
        return self._runtime

    def start_recording(
        self,
        workflow_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """
        Start recording events for a workflow.

        Args:
            workflow_id: ID of the workflow to record
            metadata: Optional metadata about the session

        Returns:
            session_id for this recording
        """
        if self._is_recording:
            logger.warning("Already recording, stopping previous session")
            self.stop_recording()

        session_id = f"recording-{uuid.uuid4().hex[:8]}"
        self._current_session = RecordingSession(
            session_id=session_id,
            workflow_id=workflow_id,
            started_at=datetime.utcnow().isoformat(),
            metadata=metadata or {},
        )
        self._is_recording = True

        logger.info(f"Started recording: session_id={session_id}, workflow_id={workflow_id}")
        return session_id

    def record_event(self, event: dict[str, Any]) -> None:
        """
        Record an event.

        Called internally when recording is active.
        Can also be called manually to add external events.
        """
        if not self._is_recording or not self._current_session:
            return

        recorded = RecordedEvent(
            timestamp=datetime.utcnow().isoformat(),
            event_type=event.get("type", "event"),
            workflow_id=event.get("workflow_id", ""),
            trace_id=event.get("trace_id"),
            span_id=event.get("span_id"),
            tokens=event.get("tokens", 0),
            cost_cents=event.get("cost_cents", 0),
            tool_name=event.get("tool_name"),
            is_retry=event.get("is_retry", False),
            latency_ms=event.get("latency_ms", 0),
            metadata=event.get("metadata", {}),
            raw_event=dict(event),
        )

        self._current_session.add_event(recorded)

    def stop_recording(self) -> RecordingSession | None:
        """
        Stop recording and return the session.

        Returns:
            The recorded RecordingSession, or None if not recording
        """
        if not self._is_recording or not self._current_session:
            logger.warning("Not currently recording")
            return None

        self._current_session.ended_at = datetime.utcnow().isoformat()
        session = self._current_session

        logger.info(
            f"Stopped recording: session_id={session.session_id}, "
            f"events={len(session.events)}"
        )

        self._is_recording = False
        self._current_session = None

        return session

    def estimate_cost(self, session: RecordingSession) -> dict[str, Any]:
        """
        Estimate total cost from a recorded session.

        Args:
            session: The session to analyze

        Returns:
            Dict with cost breakdown
        """
        total_cost = 0
        total_tokens = 0
        llm_cost = 0
        tool_cost = 0
        event_counts = {}

        for event in session.events:
            total_cost += event.cost_cents
            total_tokens += event.tokens

            if event.event_type == "llm_call":
                llm_cost += event.cost_cents
            elif event.event_type == "tool_call":
                tool_cost += event.cost_cents

            event_counts[event.event_type] = event_counts.get(event.event_type, 0) + 1

        return {
            "total_cost_cents": total_cost,
            "total_cost_dollars": total_cost / 100.0,
            "total_tokens": total_tokens,
            "llm_cost_cents": llm_cost,
            "tool_cost_cents": tool_cost,
            "event_counts": event_counts,
            "duration_seconds": (
                datetime.fromisoformat(session.ended_at) -
                datetime.fromisoformat(session.started_at)
            ).total_seconds() if session.ended_at else None,
        }


class EventRecorder:
    """
    Context manager for easy event recording.

    Usage:
        from nullrun.decision_history import EventRecorder

        with EventRecorder("my-workflow") as recorder:
            # ... run agent code ...
            pass  # or use recorder.record_event()

        session = recorder.session
        session.save("recording.json")
    """

    def __init__(
        self,
        workflow_id: str,
        metadata: dict[str, Any] | None = None,
    ):
        from nullrun.runtime import NullRunRuntime

        self.workflow_id = workflow_id
        self.metadata = metadata or {}
        # Get the runtime's own DecisionHistoryRecorder to share state
        self._runtime = NullRunRuntime.get_instance()
        self._manager = self._runtime._recorder  # Share the same manager!
        self._session_id: str | None = None

    def __enter__(self) -> "EventRecorder":
        # Start recording via the shared manager AND the runtime
        self._session_id = self._manager.start_recording(
            self.workflow_id,
            self.metadata,
        )
        # Also start recording on runtime (to set _is_recording flag)
        self._runtime.start_recording(self.workflow_id, self.metadata)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.session = self._manager.stop_recording()
        return False

    def record_event(self, event: dict[str, Any]) -> None:
        """Record an event manually."""
        self._manager.record_event(event)
