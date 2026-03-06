"""Code evolution tracking across retries and rewrites."""

import difflib
import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class CodeVersion:
    step_id: int
    spec_attempt: int  # 0-based
    orch_attempt: int  # 0-based
    code: str
    prompt_hash: str
    exit_code: int
    error_summary: str  # first 200 chars of error
    timestamp: str

    def to_dict(self) -> dict:
        return asdict(self)


class CodeTracker:
    """Track code versions across retries and rewrites for each step."""

    def __init__(self, output_dir: Optional[str] = None):
        self._versions: dict[int, list[CodeVersion]] = {}
        self._lock = threading.Lock()
        self._output_dir = output_dir
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

    @property
    def output_dir(self) -> Optional[str]:
        return self._output_dir

    def record(
        self,
        step_id: int,
        spec_attempt: int,
        orch_attempt: int,
        code: str,
        prompt_hash: str = "",
        exit_code: int = -1,
        error_summary: str = "",
    ) -> CodeVersion:
        """Record a new code version."""
        version = CodeVersion(
            step_id=step_id,
            spec_attempt=spec_attempt,
            orch_attempt=orch_attempt,
            code=code,
            prompt_hash=prompt_hash,
            exit_code=exit_code,
            error_summary=error_summary[:200],
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            if step_id not in self._versions:
                self._versions[step_id] = []
            self._versions[step_id].append(version)
            if self._output_dir:
                self._save_step(step_id)
        return version

    def get_versions(self, step_id: int) -> list[CodeVersion]:
        """Get all code versions for a step, ordered by recording time."""
        with self._lock:
            return list(self._versions.get(step_id, []))

    def get_diff(self, step_id: int, from_idx: int, to_idx: int) -> str:
        """Get unified diff between two versions of a step's code."""
        with self._lock:
            versions = self._versions.get(step_id, [])
        if (from_idx < 0 or to_idx < 0
                or from_idx >= len(versions) or to_idx >= len(versions)):
            return ""
        from_v = versions[from_idx]
        to_v = versions[to_idx]
        diff = difflib.unified_diff(
            from_v.code.splitlines(keepends=True),
            to_v.code.splitlines(keepends=True),
            fromfile=f"attempt-{from_idx}",
            tofile=f"attempt-{to_idx}",
        )
        return "".join(diff)

    def get_all_versions(self) -> dict[int, list[CodeVersion]]:
        """Get all tracked versions."""
        with self._lock:
            return {k: list(v) for k, v in self._versions.items()}

    def load_step(self, step_id: int, path: str):
        """Load code versions for a step from a JSON file."""
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return
        with self._lock:
            self._versions[step_id] = [CodeVersion(**v) for v in data]

    def load_from_dir(self, directory: str):
        """Load code versions from a directory of JSON files."""
        if not os.path.isdir(directory):
            return
        for fname in os.listdir(directory):
            if not fname.endswith(".json"):
                continue
            try:
                step_id = int(fname.replace(".json", ""))
            except ValueError:
                continue
            path = os.path.join(directory, fname)
            try:
                with open(path) as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            with self._lock:
                self._versions[step_id] = [CodeVersion(**v) for v in data]

    def _save_step(self, step_id: int):
        """Save versions for a step to disk (caller must hold lock)."""
        if not self._output_dir:
            return
        path = os.path.join(self._output_dir, f"{step_id}.json")
        data = [v.to_dict() for v in self._versions[step_id]]
        with open(path, "w") as f:
            json.dump(data, f, indent=2)


_code_tracker: Optional[CodeTracker] = None
_tracker_lock = threading.Lock()


def get_code_tracker(output_dir: Optional[str] = None) -> CodeTracker:
    """Module-level singleton accessor for the CodeTracker."""
    global _code_tracker
    with _tracker_lock:
        if _code_tracker is None:
            _code_tracker = CodeTracker(output_dir=output_dir)
        return _code_tracker


def reset_code_tracker():
    """Reset the singleton (for testing)."""
    global _code_tracker
    with _tracker_lock:
        _code_tracker = None
