"""Plan state management — persists the DAG to JSON on disk."""

import json
import os
from datetime import datetime, timezone

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "architect_state")
STATE_FILE = os.path.join(STATE_DIR, "plan_state.json")
SPECS_DIR = os.path.join(STATE_DIR, "specs")


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
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE) as f:
        return json.load(f)


def add_steps(state: dict, steps: list[dict]) -> dict:
    for i, step in enumerate(steps, 1):
        state["steps"].append({
            "id": i,
            "title": step["title"],
            "description": step["description"],
            "depends_on": step.get("depends_on", []),
            "status": "pending",
            "spec_file": None,
            "rewrites": 0,
            "output": "",
            "error": "",
        })
    state["status"] = "executing"
    save_state(state)
    return state
