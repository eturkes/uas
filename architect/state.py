"""Plan state management — persists the DAG to JSON on disk.

Each run's artifacts are stored under ``.uas_state/runs/{run_id}/`` so that
multiple runs can coexist without overwriting each other.  A shared
scratchpad at ``.uas_state/scratchpad.md`` provides cross-run learning with
per-run filtering via ``[run:{run_id}]`` tags.
"""

import json
import logging
import os
import shutil
import uuid
from datetime import datetime, timezone

import uas_config as config

logger = logging.getLogger(__name__)

WORKSPACE = config.get("workspace")
STATE_DIR = os.path.join(WORKSPACE, ".uas_state")
SCRATCHPAD_FILE = os.path.join(STATE_DIR, "scratchpad.md")

# Legacy flat paths — kept only for migration / fallback
_LEGACY_STATE_FILE = os.path.join(STATE_DIR, "state.json")


# ---------------------------------------------------------------------------
# Run directory helpers
# ---------------------------------------------------------------------------

def get_run_dir(run_id: str) -> str:
    """Return the directory for a specific run: .uas_state/runs/{run_id}."""
    return os.path.join(STATE_DIR, "runs", run_id)


def get_specs_dir(run_id: str) -> str:
    """Return the specs directory for a specific run.

    Falls back to the legacy ``.uas_state/specs`` path when *run_id* is empty.
    """
    if not run_id:
        return os.path.join(STATE_DIR, "specs")
    return os.path.join(get_run_dir(run_id), "specs")


def _write_latest_run(run_id: str):
    """Record the latest run_id so resume can find it."""
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, "latest_run")
    with open(path, "w", encoding="utf-8") as f:
        f.write(run_id)


def get_latest_run_id() -> str | None:
    """Read the latest run_id from disk."""
    path = os.path.join(STATE_DIR, "latest_run")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def list_runs() -> list[str]:
    """Return a list of all run IDs, sorted by directory mtime (oldest first)."""
    runs_dir = os.path.join(STATE_DIR, "runs")
    if not os.path.isdir(runs_dir):
        return []
    entries = []
    for name in os.listdir(runs_dir):
        run_dir = os.path.join(runs_dir, name)
        if os.path.isdir(run_dir):
            try:
                mtime = os.path.getmtime(run_dir)
            except OSError:
                mtime = 0.0
            entries.append((mtime, name))
    entries.sort()
    return [name for _, name in entries]


# ---------------------------------------------------------------------------
# State init / save / load
# ---------------------------------------------------------------------------

def init_state(goal: str, run_id: str | None = None) -> dict:
    if run_id is None:
        run_id = uuid.uuid4().hex[:12]
    run_dir = get_run_dir(run_id)
    os.makedirs(os.path.join(run_dir, "specs"), exist_ok=True)
    state = {
        "goal": goal,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "status": "planning",
        "steps": [],
        "total_tokens": {"input": 0, "output": 0},
        "total_cost_usd": 0.0,
    }
    save_state(state)
    _write_latest_run(run_id)
    return state


def save_state(state: dict):
    run_id = state.get("run_id", "")
    if run_id:
        run_dir = get_run_dir(run_id)
        os.makedirs(run_dir, exist_ok=True)
        path = os.path.join(run_dir, "state.json")
    else:
        os.makedirs(STATE_DIR, exist_ok=True)
        path = _LEGACY_STATE_FILE
    # Strip runtime-only keys (prefixed with '_') that are not JSON-serialisable.
    runtime_keys = [k for k in state if k.startswith("_")]
    stashed = {k: state.pop(k) for k in runtime_keys}
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    finally:
        state.update(stashed)


def load_state(run_id: str | None = None) -> dict | None:
    """Load state from disk.

    If *run_id* is given, load from that run's directory.
    Otherwise try the latest run, then fall back to the legacy flat path.
    Returns None if missing or corrupted.
    """
    if run_id is None:
        run_id = get_latest_run_id()

    if run_id:
        path = os.path.join(get_run_dir(run_id), "state.json")
    else:
        path = _LEGACY_STATE_FILE

    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "goal" not in data or "steps" not in data:
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Scratchpad (shared across runs, filtered by run_id tags)
# ---------------------------------------------------------------------------

def append_scratchpad(entry: str, run_id: str = ""):
    """Append a timestamped entry to the scratchpad file.

    When *run_id* is provided the entry is tagged so that
    ``read_scratchpad`` can filter to a single run.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    header = f"## [{timestamp}]"
    if run_id:
        header += f" [run:{run_id}]"
    with open(SCRATCHPAD_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n{header}\n{entry}\n")


def read_scratchpad(max_chars: int = 2000, run_id: str = "") -> str:
    """Read scratchpad entries up to *max_chars*.

    When *run_id* is given, only entries tagged with that run are
    returned.  Untagged (legacy) entries are always excluded when
    filtering by run.  Uses tail-based reading to prioritise the
    most recent entries.
    """
    if not os.path.exists(SCRATCHPAD_FILE):
        return ""
    try:
        with open(SCRATCHPAD_FILE, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return ""
    if not content:
        return ""

    if run_id:
        content = _filter_scratchpad_by_run(content, run_id)
        if not content:
            return ""

    if len(content) <= max_chars:
        return content
    # Return the tail (most recent entries)
    return "...[earlier entries omitted]\n" + content[-max_chars:]


def _filter_scratchpad_by_run(content: str, run_id: str) -> str:
    """Return only scratchpad sections belonging to *run_id*."""
    marker = f"[run:{run_id}]"
    blocks: list[str] = []
    current: list[str] = []
    keep = False

    for line in content.split("\n"):
        if line.startswith("## ["):
            # Flush previous block
            if keep and current:
                blocks.append("\n".join(current))
            current = [line]
            keep = marker in line
        else:
            current.append(line)

    if keep and current:
        blocks.append("\n".join(current))

    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# Progress file (per-run)
# ---------------------------------------------------------------------------

def update_progress_file(state: dict, event: str | None = None):
    """Write a structured progress file summarizing execution state.

    Replaces the flat scratchpad for context building (Section 4a).
    The progress file has sections: Current State, Key Decisions,
    Completed Steps, and Lessons Learned.
    """
    run_id = state.get("run_id", "")
    if run_id:
        run_dir = get_run_dir(run_id)
        os.makedirs(run_dir, exist_ok=True)
        progress_path = os.path.join(run_dir, "progress.md")
    else:
        os.makedirs(STATE_DIR, exist_ok=True)
        progress_path = os.path.join(STATE_DIR, "progress.md")

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
    with open(progress_path, "w", encoding="utf-8") as f:
        f.write(content)


def read_progress_file(run_id: str = "") -> str:
    """Read the structured progress file.

    Returns empty string if the file doesn't exist.
    """
    if run_id:
        path = os.path.join(get_run_dir(run_id), "progress.md")
    else:
        path = os.path.join(STATE_DIR, "progress.md")
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Step management
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Knowledge base (cross-run persistence)
# ---------------------------------------------------------------------------

def get_knowledge_base_path() -> str:
    workspace = config.get("workspace")
    return os.path.join(workspace, ".uas_state", "knowledge.json")


def read_knowledge_base() -> dict:
    """Load knowledge base or return empty structure."""
    path = get_knowledge_base_path()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"package_versions": {}, "lessons": []}


def append_knowledge(entry_type: str, data: dict):
    """Append an entry to the knowledge base."""
    kb = read_knowledge_base()
    if entry_type == "package_version":
        kb["package_versions"].update(data)
    elif entry_type == "lesson":
        kb["lessons"].append(data)
        # Cap at 50 entries
        if len(kb["lessons"]) > 50:
            kb["lessons"] = kb["lessons"][-50:]
    path = get_knowledge_base_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(kb, f, indent=2)


def add_steps(state: dict, steps: list[dict]) -> dict:
    for i, step in enumerate(steps, 1):
        state["steps"].append({
            "id": i,
            "title": step["title"],
            "description": step["description"],
            "depends_on": step.get("depends_on", []),
            "outputs": step.get("outputs", []),
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
            "token_usage": {"input": 0, "output": 0},
            "cost_usd": 0.0,
        })
    state["status"] = "executing"
    save_state(state)
    return state


# ---------------------------------------------------------------------------
# Run artifact lifecycle management
# ---------------------------------------------------------------------------

def get_run_disk_usage(run_id: str) -> int:
    """Return total size in bytes of all files in a run directory."""
    run_dir = get_run_dir(run_id)
    if not os.path.isdir(run_dir):
        return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(run_dir):
        for fname in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fname))
            except OSError:
                pass
    return total


def list_runs_with_metadata() -> list[dict]:
    """Return run metadata dicts sorted by creation time (oldest first).

    Each dict contains: run_id, created_at, status, disk_usage_bytes.
    """
    runs_dir = os.path.join(STATE_DIR, "runs")
    if not os.path.isdir(runs_dir):
        return []
    result = []
    for name in os.listdir(runs_dir):
        run_dir = os.path.join(runs_dir, name)
        if not os.path.isdir(run_dir):
            continue
        state_path = os.path.join(run_dir, "state.json")
        created_at = None
        status = "unknown"
        if os.path.isfile(state_path):
            try:
                with open(state_path, encoding="utf-8") as f:
                    data = json.load(f)
                created_at = data.get("created_at")
                status = data.get("status", "unknown")
            except (json.JSONDecodeError, OSError):
                pass
        if created_at is None:
            try:
                mtime = os.path.getmtime(run_dir)
                created_at = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            except OSError:
                created_at = ""
        result.append({
            "run_id": name,
            "created_at": created_at,
            "status": status,
            "disk_usage_bytes": get_run_disk_usage(name),
        })
    result.sort(key=lambda r: r["created_at"])
    return result


def prune_old_runs(keep_last: int = 10, max_age_days: int = 30) -> None:
    """Delete old run directories based on retention policy.

    Removes runs older than *max_age_days* OR beyond *keep_last* count,
    whichever is more aggressive. The latest run is never pruned.
    """
    runs = list_runs_with_metadata()
    if not runs:
        return

    latest_run_id = get_latest_run_id()
    now = datetime.now(timezone.utc)
    total_freed = 0
    deleted_count = 0

    # Determine which runs to keep by count (keep the N most recent).
    # runs is sorted oldest-first, so the tail is the newest.
    runs_to_keep_by_count = set()
    if keep_last > 0:
        for r in runs[-keep_last:]:
            runs_to_keep_by_count.add(r["run_id"])

    for run in runs:
        rid = run["run_id"]

        # Never prune the latest run.
        if rid == latest_run_id:
            continue

        prune = False

        # Age-based pruning
        if max_age_days > 0 and run["created_at"]:
            try:
                created = datetime.fromisoformat(run["created_at"])
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age_days = (now - created).total_seconds() / 86400
                if age_days > max_age_days:
                    prune = True
            except (ValueError, TypeError):
                pass

        # Count-based pruning
        if rid not in runs_to_keep_by_count:
            prune = True

        if prune:
            run_dir = get_run_dir(rid)
            size = run["disk_usage_bytes"]
            try:
                shutil.rmtree(run_dir)
                total_freed += size
                deleted_count += 1
                logger.info(
                    "Pruned run %s (%.1f KB freed)", rid, size / 1024
                )
            except OSError as exc:
                logger.warning("Failed to prune run %s: %s", rid, exc)

    if deleted_count:
        logger.info(
            "Pruning complete: %d run(s) deleted, %.1f KB freed",
            deleted_count,
            total_freed / 1024,
        )


# ---------------------------------------------------------------------------
# CLI entry point for `python -m architect.state prune`
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse as _argparse

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = _argparse.ArgumentParser(description="UAS state management utilities")
    sub = parser.add_subparsers(dest="command")

    prune_p = sub.add_parser("prune", help="Prune old run artifacts")
    prune_p.add_argument(
        "--keep", type=int,
        default=int(config.get("keep_last_runs", 10)),
        help="Number of recent runs to keep (default: 10)",
    )
    prune_p.add_argument(
        "--max-age", type=int,
        default=int(config.get("max_run_age_days", 30)),
        help="Max age in days before pruning (default: 30)",
    )

    args = parser.parse_args()
    if args.command == "prune":
        prune_old_runs(keep_last=args.keep, max_age_days=args.max_age)
    else:
        parser.print_help()
