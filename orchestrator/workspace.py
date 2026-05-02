"""Resume-safe per-task workspace setup for Phase 3 §6.

Sibling to ``integration/eval.py::setup_workspace`` — that variant
unconditionally ``rmtree``s the workspace directory before recreating
it (per ``docs/substrate.md`` §5 Gap, "destructive by default;
resume-from-state needs the inverse default"). For long-horizon
orchestrator tasks the workspace must survive process restarts so a
multi-day run doesn't lose its accumulated state every time the
orchestrator returns from a window-pause.

``setup_task_workspace(task_id) -> str`` ensures the per-task
workspace directory exists at ``<workspaces_dir>/<task_id>/`` and
returns its absolute path. Idempotent under repeated calls; never
destroys existing files. Tests pass ``workspaces_dir`` to redirect
from the canonical ``<repo>/integration/workspace`` to a
``tmp_path``.
"""

import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_WORKSPACES_DIR = os.path.join(REPO_ROOT, "integration", "workspace")


def setup_task_workspace(
    task_id: str,
    *,
    workspaces_dir: str | None = None,
) -> str:
    """Ensure the per-task workspace directory exists; return its path.

    Mirrors the path layout the eval harness uses
    (``<repo>/integration/workspace/<name>``) so the bind-mount
    contract used by §2's ``spawn_worker`` is unchanged. Unlike the
    eval variant, **no existing content is deleted**: callers may
    rely on prior workspace state surviving across orchestrator
    invocations.
    """
    if not isinstance(task_id, str) or not task_id:
        raise ValueError(
            f"task_id must be a non-empty string; got {task_id!r}"
        )
    base = (
        workspaces_dir if workspaces_dir is not None else DEFAULT_WORKSPACES_DIR
    )
    path = os.path.join(base, task_id)
    os.makedirs(path, exist_ok=True)
    return path
