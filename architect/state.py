"""Plan state management — persists the DAG to JSON on disk."""

import json
import os
from datetime import datetime, timezone

WORKSPACE = os.environ.get("UAS_WORKSPACE", "/workspace")
STATE_DIR = os.path.join(WORKSPACE, ".state")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
SPECS_DIR = os.path.join(STATE_DIR, "specs")
SCRATCHPAD_FILE = os.path.join(STATE_DIR, "scratchpad.md")


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
