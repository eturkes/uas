"""Lightweight hook system for UAS lifecycle extensibility.

Users register shell scripts in ``.uas/hooks.toml`` (or the ``hooks`` section
of ``uas.toml``).  Scripts execute at key lifecycle points, receiving event
data as JSON on stdin and optionally returning control directives on stdout.

Hook stdout can return ``{"abort": true, "reason": "..."}`` to halt the
current operation (for ``PRE_*`` events).
"""

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Callable

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hook event types
# ---------------------------------------------------------------------------

class HookEvent(Enum):
    PRE_STEP = "PRE_STEP"
    POST_STEP = "POST_STEP"
    PRE_REWRITE = "PRE_REWRITE"
    POST_REWRITE = "POST_REWRITE"
    PRE_PLAN = "PRE_PLAN"
    POST_PLAN = "POST_PLAN"
    RUN_START = "RUN_START"
    RUN_COMPLETE = "RUN_COMPLETE"
    STEP_FAILED = "STEP_FAILED"


# ---------------------------------------------------------------------------
# Hook configuration
# ---------------------------------------------------------------------------

@dataclass
class HookConfig:
    event: HookEvent
    command: str
    timeout: int = 30


# ---------------------------------------------------------------------------
# Loading hooks
# ---------------------------------------------------------------------------

def load_hooks(config_path: str | None = None) -> list[HookConfig]:
    """Load hook definitions from a TOML file.

    Checks (in order):
    1. *config_path* if provided explicitly.
    2. ``{workspace}/.uas/hooks.toml``
    3. ``hooks`` section inside ``{workspace}/.uas/config.toml``

    Returns an empty list when no hooks are configured.
    """
    if tomllib is None:
        return []

    workspace = os.environ.get("UAS_WORKSPACE", "/workspace")

    paths_to_try: list[tuple[str, Callable[[dict], list[dict]]]] = []

    if config_path:
        paths_to_try.append((config_path, _extract_hooks_toml))

    # .uas/hooks.toml  (dedicated hooks file)
    hooks_toml = os.path.join(workspace, ".uas", "hooks.toml")
    paths_to_try.append((hooks_toml, _extract_hooks_toml))

    # .uas/config.toml  (hooks section inside project config)
    config_toml = os.path.join(workspace, ".uas", "config.toml")
    paths_to_try.append((config_toml, _extract_hooks_from_config))

    for path, extractor in paths_to_try:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
            raw = extractor(data)
            if raw:
                return _parse_hook_configs(raw)
        except Exception as exc:
            logger.warning("Failed to load hooks from %s: %s", path, exc)

    return []


def _extract_hooks_toml(data: dict) -> list[dict]:
    """Extract hooks from a dedicated hooks.toml (top-level ``[[hooks]]``)."""
    return data.get("hooks", [])


def _extract_hooks_from_config(data: dict) -> list[dict]:
    """Extract hooks from the ``hooks`` section of config.toml."""
    return data.get("hooks", [])


def _parse_hook_configs(raw: list[dict]) -> list[HookConfig]:
    """Convert raw dicts to validated HookConfig instances."""
    configs: list[HookConfig] = []
    for entry in raw:
        event_str = entry.get("event", "")
        command = entry.get("command", "")
        if not event_str or not command:
            logger.warning("Skipping hook with missing event or command: %s", entry)
            continue
        try:
            event = HookEvent(event_str)
        except ValueError:
            logger.warning("Unknown hook event %r, skipping.", event_str)
            continue
        timeout = int(entry.get("timeout", 30))
        configs.append(HookConfig(event=event, command=command, timeout=timeout))
    return configs


# ---------------------------------------------------------------------------
# Running hooks
# ---------------------------------------------------------------------------

def run_hook(
    event: HookEvent,
    data: dict,
    hooks: list[HookConfig],
) -> dict | None:
    """Execute all hooks matching *event*.

    For each matching hook:
    - Pipe *data* as JSON to the script's stdin.
    - Capture stdout; if non-empty, parse as JSON and merge into the result.
    - Stderr is forwarded to the logger.
    - Timeout is enforced per-hook.

    Returns the merged output dict from all matching hooks, or ``None`` if no
    hooks matched or none produced output.  A hook can return
    ``{"abort": true, "reason": "..."}`` to signal the caller to halt.
    """
    matching = [h for h in hooks if h.event == event]
    if not matching:
        return None

    merged: dict = {}
    for hook in matching:
        result = _execute_hook(hook, event, data)
        if result is not None:
            merged.update(result)
            # Short-circuit on abort
            if result.get("abort"):
                break

    return merged if merged else None


def _execute_hook(hook: HookConfig, event: HookEvent, data: dict) -> dict | None:
    """Run a single hook subprocess."""
    payload = json.dumps({"event": event.value, **data})
    try:
        proc = subprocess.run(
            hook.command,
            shell=True,
            input=payload,
            capture_output=True,
            text=True,
            timeout=hook.timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "Hook %r for %s timed out after %ds",
            hook.command, event.value, hook.timeout,
        )
        return None
    except OSError as exc:
        logger.warning(
            "Hook %r for %s failed to execute: %s",
            hook.command, event.value, exc,
        )
        return None

    # Forward stderr to logger
    if proc.stderr and proc.stderr.strip():
        for line in proc.stderr.strip().splitlines():
            logger.info("  [hook %s] %s", event.value, line)

    if proc.returncode != 0:
        logger.warning(
            "Hook %r for %s exited with code %d",
            hook.command, event.value, proc.returncode,
        )
        return None

    # Parse stdout as JSON if non-empty
    stdout = (proc.stdout or "").strip()
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        logger.warning(
            "Hook %r for %s returned non-JSON stdout, ignoring.",
            hook.command, event.value,
        )
        return None
