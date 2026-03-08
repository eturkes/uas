"""Plan state management — persists the DAG to JSON on disk."""

import json
import os
from datetime import datetime, timezone

WORKSPACE = os.environ.get("UAS_WORKSPACE", "/workspace")
STATE_DIR = os.path.join(WORKSPACE, ".state")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
SPECS_DIR = os.path.join(STATE_DIR, "specs")
SCRATCHPAD_FILE = os.path.join(STATE_DIR, "scratchpad.md")
PROGRESS_FILE = os.path.join(STATE_DIR, "progress.md")


def init_state(goal: str) -> dict:
    os.makedirs(SPECS_DIR, exist_ok=True)
    state = {
        "goal": goal,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "planning",
        "steps": [],
    }
    save_state(state)
    return state


def save_state(state: dict):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_state() -> dict | None:
    """Load state from disk. Returns None if missing or corrupted."""
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        # Validate minimum required structure
        if not isinstance(data, dict) or "goal" not in data or "steps" not in data:
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def append_scratchpad(entry: str):
    """Append a timestamped entry to the scratchpad file."""
    os.makedirs(STATE_DIR, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(SCRATCHPAD_FILE, "a") as f:
        f.write(f"\n## [{timestamp}]\n{entry}\n")


def read_scratchpad(max_chars: int = 2000) -> str:
    """Read the most recent scratchpad entries up to max_chars.

    Uses tail-based reading to prioritize the most recent entries.
    """
    if not os.path.exists(SCRATCHPAD_FILE):
        return ""
    try:
        with open(SCRATCHPAD_FILE) as f:
            content = f.read()
    except OSError:
        return ""
    if not content:
        return ""
    if len(content) <= max_chars:
        return content
    # Return the tail (most recent entries)
    return "...[earlier entries omitted]\n" + content[-max_chars:]


def update_progress_file(state: dict, event: str | None = None):
    """Write a structured progress file summarizing execution state.

    Replaces the flat scratchpad for context building (Section 4a).
    The progress file has sections: Current State, Key Decisions,
    Completed Steps, and Lessons Learned.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    steps = state.get("steps", [])
    completed = [s for s in steps if s["status"] == "completed"]
    failed = [s for s in steps if s["status"] == "failed"]
    pending = [s for s in steps if s["status"] == "pending"]
    executing = [s for s in steps if s["status"] == "executing"]

    lines = []

    # Current State section
    lines.append("## Current State")
    lines.append(f"- Steps completed: {len(completed)}/{len(steps)}")
    if executing:
        titles = ", ".join(f'"{s["title"]}"' for s in executing)
        lines.append(f"- Currently executing: {titles}")
    if pending:
        lines.append(f"- Steps remaining: {len(pending)}")
    if failed:
        blockers = "; ".join(
            f'step {s["id"]} "{s["title"]}": {s.get("error", "")[:100]}'
            for s in failed
        )
        lines.append(f"- Known blockers: {blockers}")
    lines.append("")

    # Key Decisions section (from reflections)
    decisions = []
    for s in steps:
        for r in s.get("reflections", []):
            lesson = r.get("lesson", "")
            if lesson:
                decisions.append(
                    f"- [{timestamp}] Step {s['id']} attempt {r.get('attempt', '?')}: {lesson[:200]}"
                )
    if decisions:
        lines.append("## Key Decisions")
        lines.extend(decisions[-10:])  # Keep last 10 decisions
        lines.append("")

    # Completed Steps section
    if completed:
        lines.append("## Completed Steps")
        for s in completed:
            summary = s.get("summary", "")
            if not summary and s.get("output"):
                summary = s["output"][:100]
            files = s.get("files_written", [])
            files_str = f", files: [{', '.join(files[:5])}]" if files else ""
            elapsed = s.get("elapsed", 0.0)
            lines.append(
                f"- Step {s['id']} ({s['title']}): {summary[:150]}{files_str}, time: {elapsed:.1f}s"
            )
        lines.append("")

    # Lessons Learned section (from reflections across all steps)
    lessons = []
    for s in steps:
        for r in s.get("reflections", []):
            lesson = r.get("lesson", "")
            what_next = r.get("what_to_try_next", "")
            if lesson:
                lessons.append(f"- Step {s['id']}: {lesson[:200]}")
            if what_next:
                lessons.append(f"- Step {s['id']} next: {what_next[:200]}")
    if lessons:
        lines.append("## Lessons Learned")
        lines.extend(lessons[-10:])  # Keep last 10 lessons
        lines.append("")

    # Append event if provided
    if event:
        lines.append(f"## Latest Event [{timestamp}]")
        lines.append(event)
        lines.append("")

    content = "\n".join(lines)
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        f.write(content)


def read_progress_file() -> str:
    """Read the structured progress file.

    Returns empty string if the file doesn't exist.
    """
    if not os.path.exists(PROGRESS_FILE):
        return ""
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def add_steps(state: dict, steps: list[dict]) -> dict:
    for i, step in enumerate(steps, 1):
        state["steps"].append({
            "id": i,
            "title": step["title"],
            "description": step["description"],
            "depends_on": step.get("depends_on", []),
            "verify": step.get("verify", ""),
            "environment": step.get("environment", []),
            "status": "pending",
            "spec_file": None,
            "rewrites": 0,
            "reflections": [],
            "output": "",
            "error": "",
            "timing": {
                "llm_time": 0.0,
                "sandbox_time": 0.0,
                "total_time": 0.0,
            },
        })
    state["status"] = "executing"
    save_state(state)
    return state
