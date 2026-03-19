"""Structured event system for tracking significant actions during a UAS run."""

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class EventType(Enum):
    GOAL_RECEIVED = "goal_received"
    DECOMPOSITION_START = "decomposition_start"
    DECOMPOSITION_COMPLETE = "decomposition_complete"
    PLAN_CRITIQUE = "plan_critique"
    STEP_MERGE = "step_merge"
    STEP_START = "step_start"
    LLM_CALL_START = "llm_call_start"
    LLM_CALL_COMPLETE = "llm_call_complete"
    CODE_EXTRACTED = "code_extracted"
    SANDBOX_START = "sandbox_start"
    SANDBOX_COMPLETE = "sandbox_complete"
    STEP_COMPLETE = "step_complete"
    STEP_FAILED = "step_failed"
    REWRITE_START = "rewrite_start"
    REWRITE_COMPLETE = "rewrite_complete"
    VERIFICATION_START = "verification_start"
    VERIFICATION_COMPLETE = "verification_complete"
    CONTEXT_BUILT = "context_built"
    COMPLEXITY_ESTIMATE = "complexity_estimate"
    VOTING_COMPLETE = "voting_complete"
    REFLECTION_GENERATED = "reflection_generated"
    ROOT_CAUSE_TRACED = "root_cause_traced"
    BACKTRACK_START = "backtrack_start"
    BACKTRACK_COMPLETE = "backtrack_complete"
    REPLAN_CHECK = "replan_check"
    REPLAN_TRIGGERED = "replan_triggered"
    REPLAN_COMPLETE = "replan_complete"
    STEP_ENRICHED = "step_enriched"
    RESEARCH_START = "research_start"
    RESEARCH_COMPLETE = "research_complete"
    RUN_COMPLETE = "run_complete"


@dataclass
class Event:
    timestamp: str
    event_type: str
    step_id: Optional[int] = None
    attempt: Optional[int] = None
    duration: Optional[float] = None
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        # Remove None optional fields for cleaner JSON
        return {k: v for k, v in d.items() if v is not None}


class EventLog:
    """Append-only event log that writes to memory and optionally to disk."""

    def __init__(self, events_path: Optional[str] = None):
        self._events: list[Event] = []
        self._lock = threading.Lock()
        self._events_path = events_path
        if events_path:
            os.makedirs(os.path.dirname(events_path), exist_ok=True)

    @property
    def events_path(self) -> Optional[str]:
        return self._events_path

    def emit(self, event_type: EventType, **kwargs) -> Event:
        """Create and append an event. Returns the event for chaining."""
        event = Event(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_type=event_type.value,
            **kwargs,
        )
        with self._lock:
            self._events.append(event)
            if self._events_path:
                with open(self._events_path, "a") as f:
                    f.write(json.dumps(event.to_dict()) + "\n")
        return event

    def query(
        self,
        event_type: Optional[EventType] = None,
        step_id: Optional[int] = None,
    ) -> list[Event]:
        """Retrieve events matching optional filters."""
        with self._lock:
            results = list(self._events)
        if event_type is not None:
            results = [e for e in results if e.event_type == event_type.value]
        if step_id is not None:
            results = [e for e in results if e.step_id == step_id]
        return results

    @property
    def events(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


_event_log: Optional[EventLog] = None
_event_log_lock = threading.Lock()


def get_event_log(events_path: Optional[str] = None) -> EventLog:
    """Module-level singleton accessor for the EventLog.

    On first call, creates the instance. If events_path is provided on first
    call, events are persisted to that file. Subsequent calls return the
    existing instance regardless of arguments.
    """
    global _event_log
    with _event_log_lock:
        if _event_log is None:
            _event_log = EventLog(events_path=events_path)
        return _event_log


def reset_event_log():
    """Reset the singleton (for testing)."""
    global _event_log
    with _event_log_lock:
        _event_log = None
