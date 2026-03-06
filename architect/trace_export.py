"""Export execution data in Chrome Trace Event JSON format for Perfetto.

Converts UAS event logs into trace spans viewable at ui.perfetto.dev or
chrome://tracing. Uses three processes to represent the Architect,
Orchestrator, and Sandbox layers, with threads per step for parallel
visualization.
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional


# Chrome Trace Event phase constants
PH_DURATION_BEGIN = "B"
PH_DURATION_END = "E"
PH_COMPLETE = "X"
PH_COUNTER = "C"
PH_METADATA = "M"

# Process IDs
PID_ARCHITECT = 1
PID_ORCHESTRATOR = 2
PID_SANDBOX = 3

# Base thread IDs (step_id is added to get per-step threads)
TID_MAIN = 0


def _iso_to_us(iso_str: str) -> float:
    """Convert ISO 8601 timestamp string to microseconds since epoch."""
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1_000_000


class TraceExporter:
    """Converts UAS event logs to Chrome Trace Event format."""

    def __init__(self, events: list[dict]):
        self._events = events
        self._trace_events: list[dict] = []
        self._run_start_us: Optional[float] = None
        self._counters = {
            "llm_calls": 0,
            "sandbox_runs": 0,
            "cumulative_llm_time_ms": 0.0,
        }

    def export(self) -> list[dict]:
        """Convert events to Chrome Trace Event format and return the list."""
        self._trace_events = []
        self._counters = {
            "llm_calls": 0,
            "sandbox_runs": 0,
            "cumulative_llm_time_ms": 0.0,
        }

        if not self._events:
            return self._trace_events

        # Determine run start time
        self._run_start_us = _iso_to_us(self._events[0]["timestamp"])

        # Add process/thread metadata
        self._add_metadata()

        # Build paired events from the log
        self._process_events()

        return self._trace_events

    def export_json(self, output_path: str) -> str:
        """Export trace to a JSON file. Returns the output path."""
        trace = self.export()
        parent = os.path.dirname(output_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(trace, f, indent=None)
        return output_path

    def _add_metadata(self):
        """Add process and thread name metadata events."""
        for pid, name in [
            (PID_ARCHITECT, "Architect"),
            (PID_ORCHESTRATOR, "Orchestrator"),
            (PID_SANDBOX, "Sandbox"),
        ]:
            self._trace_events.append({
                "ph": PH_METADATA,
                "name": "process_name",
                "pid": pid,
                "tid": 0,
                "args": {"name": name},
            })

    def _ts(self, event: dict) -> float:
        """Get timestamp in microseconds relative to run start."""
        abs_us = _iso_to_us(event["timestamp"])
        return abs_us - self._run_start_us

    def _tid_for_step(self, step_id: Optional[int]) -> int:
        """Get thread ID for a step. Main thread if no step_id."""
        if step_id is None:
            return TID_MAIN
        return step_id

    def _process_events(self):
        """Walk through events and generate trace spans."""
        # Track open spans for pairing begin/end events
        open_spans: dict[str, dict] = {}

        for event in self._events:
            etype = event["event_type"]
            step_id = event.get("step_id")
            data = event.get("data", {})
            attempt = event.get("attempt")
            duration = event.get("duration")
            ts = self._ts(event)
            tid = self._tid_for_step(step_id)

            if etype == "decomposition_start":
                open_spans["decomposition"] = event
                self._trace_events.append({
                    "ph": PH_DURATION_BEGIN,
                    "name": "Decompose Goal",
                    "cat": "planning",
                    "pid": PID_ARCHITECT,
                    "tid": TID_MAIN,
                    "ts": ts,
                })

            elif etype == "decomposition_complete":
                self._trace_events.append({
                    "ph": PH_DURATION_END,
                    "name": "Decompose Goal",
                    "cat": "planning",
                    "pid": PID_ARCHITECT,
                    "tid": TID_MAIN,
                    "ts": ts,
                    "args": {
                        "num_steps": data.get("num_steps"),
                        "duration_s": duration,
                    },
                })
                open_spans.pop("decomposition", None)

            elif etype == "step_start":
                span_key = f"step_{step_id}"
                open_spans[span_key] = event
                args = {"title": data.get("title", "")}
                self._trace_events.append({
                    "ph": PH_DURATION_BEGIN,
                    "name": f"Step {step_id}: {data.get('title', '')}",
                    "cat": "step",
                    "pid": PID_ARCHITECT,
                    "tid": tid,
                    "ts": ts,
                    "args": args,
                })

            elif etype == "step_complete":
                self._trace_events.append({
                    "ph": PH_DURATION_END,
                    "name": f"Step {step_id}",
                    "cat": "step",
                    "pid": PID_ARCHITECT,
                    "tid": tid,
                    "ts": ts,
                    "args": {
                        "duration_s": duration,
                        "files_written": data.get("files_written", []),
                    },
                })
                open_spans.pop(f"step_{step_id}", None)

            elif etype == "step_failed":
                self._trace_events.append({
                    "ph": PH_DURATION_END,
                    "name": f"Step {step_id} (FAILED)",
                    "cat": "step",
                    "pid": PID_ARCHITECT,
                    "tid": tid,
                    "ts": ts,
                    "args": {
                        "error": data.get("error", "")[:200],
                    },
                })
                open_spans.pop(f"step_{step_id}", None)

            elif etype == "llm_call_start":
                span_key = f"llm_{step_id}_{attempt}"
                open_spans[span_key] = event
                self._trace_events.append({
                    "ph": PH_DURATION_BEGIN,
                    "name": f"LLM Call (step {step_id}, attempt {attempt})",
                    "cat": "llm",
                    "pid": PID_ORCHESTRATOR,
                    "tid": tid,
                    "ts": ts,
                    "args": {"attempt": attempt},
                })

            elif etype == "llm_call_complete":
                span_key = f"llm_{step_id}_{attempt}"
                self._counters["llm_calls"] += 1
                if duration:
                    self._counters["cumulative_llm_time_ms"] += duration * 1000
                self._trace_events.append({
                    "ph": PH_DURATION_END,
                    "name": f"LLM Call (step {step_id})",
                    "cat": "llm",
                    "pid": PID_ORCHESTRATOR,
                    "tid": tid,
                    "ts": ts,
                    "args": {
                        "exit_code": data.get("exit_code"),
                        "duration_s": duration,
                        "attempt": attempt,
                    },
                })
                open_spans.pop(span_key, None)

                # Emit counter event
                self._trace_events.append({
                    "ph": PH_COUNTER,
                    "name": "LLM Metrics",
                    "pid": PID_ORCHESTRATOR,
                    "tid": 0,
                    "ts": ts,
                    "args": {
                        "total_calls": self._counters["llm_calls"],
                        "cumulative_time_ms": self._counters["cumulative_llm_time_ms"],
                    },
                })

            elif etype == "sandbox_start":
                span_key = f"sandbox_{step_id}_{attempt}"
                open_spans[span_key] = event
                self._trace_events.append({
                    "ph": PH_DURATION_BEGIN,
                    "name": f"Sandbox (step {step_id})",
                    "cat": "sandbox",
                    "pid": PID_SANDBOX,
                    "tid": tid,
                    "ts": ts,
                    "args": {"attempt": attempt},
                })

            elif etype == "sandbox_complete":
                span_key = f"sandbox_{step_id}_{attempt}"
                self._counters["sandbox_runs"] += 1
                self._trace_events.append({
                    "ph": PH_DURATION_END,
                    "name": f"Sandbox (step {step_id})",
                    "cat": "sandbox",
                    "pid": PID_SANDBOX,
                    "tid": tid,
                    "ts": ts,
                    "args": {
                        "exit_code": data.get("exit_code"),
                        "duration_s": duration,
                    },
                })
                open_spans.pop(span_key, None)

                # Emit counter event
                self._trace_events.append({
                    "ph": PH_COUNTER,
                    "name": "Sandbox Metrics",
                    "pid": PID_SANDBOX,
                    "tid": 0,
                    "ts": ts,
                    "args": {
                        "total_runs": self._counters["sandbox_runs"],
                    },
                })

            elif etype == "rewrite_start":
                span_key = f"rewrite_{step_id}_{attempt}"
                open_spans[span_key] = event
                self._trace_events.append({
                    "ph": PH_DURATION_BEGIN,
                    "name": f"Rewrite (step {step_id}, attempt {attempt})",
                    "cat": "rewrite",
                    "pid": PID_ARCHITECT,
                    "tid": tid,
                    "ts": ts,
                    "args": {"attempt": attempt},
                })

            elif etype == "rewrite_complete":
                span_key = f"rewrite_{step_id}_{attempt}"
                self._trace_events.append({
                    "ph": PH_DURATION_END,
                    "name": f"Rewrite (step {step_id})",
                    "cat": "rewrite",
                    "pid": PID_ARCHITECT,
                    "tid": tid,
                    "ts": ts,
                    "args": {"attempt": attempt},
                })
                open_spans.pop(span_key, None)

            elif etype == "verification_start":
                span_key = f"verify_{step_id}"
                open_spans[span_key] = event
                self._trace_events.append({
                    "ph": PH_DURATION_BEGIN,
                    "name": f"Verify (step {step_id})",
                    "cat": "verification",
                    "pid": PID_ARCHITECT,
                    "tid": tid,
                    "ts": ts,
                })

            elif etype == "verification_complete":
                span_key = f"verify_{step_id}"
                self._trace_events.append({
                    "ph": PH_DURATION_END,
                    "name": f"Verify (step {step_id})",
                    "cat": "verification",
                    "pid": PID_ARCHITECT,
                    "tid": tid,
                    "ts": ts,
                    "args": {"passed": data.get("passed")},
                })
                open_spans.pop(span_key, None)
