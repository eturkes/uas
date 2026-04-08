"""Git-driven state management for the Reflexion loop.

Provides branch-per-attempt isolation so that every worker attempt gets its
own branch forked from the last successful checkpoint on ``uas-wip``.  Failed
attempts are hard-reset to restore a clean filesystem; successful attempts are
fast-forward merged back into ``uas-wip``.

Branch naming convention::

    uas/step-{step_id}/attempt-{attempt}

The ``uas-wip`` branch (created by :func:`architect.main.ensure_git_repo`)
serves as the authoritative checkpoint reference.
"""

import logging
import os
import subprocess

logger = logging.getLogger(__name__)


def create_attempt_branch(
    workspace: str, step_id: int, attempt: int,
) -> str:
    """Create a fresh attempt branch from the latest ``uas-wip`` checkpoint.

    The branch is named ``uas/step-{step_id}/attempt-{attempt}`` and is
    forked from the current tip of ``uas-wip``.

    Returns the branch name on success, or an empty string if the branch
    could not be created (e.g. no git repo, missing ``uas-wip``).
    """
    branch = f"uas/step-{step_id}/attempt-{attempt}"
    try:
        git_dir = os.path.join(workspace, ".git")
        if not os.path.isdir(git_dir):
            return ""

        # Ensure uas-wip exists
        result = subprocess.run(
            ["git", "branch", "--list", "uas-wip"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True,
        )
        if not result.stdout.strip():
            logger.debug("uas-wip branch not found in %s", workspace)
            return ""

        # Delete the branch if it already exists (re-run of same attempt)
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=workspace,
            capture_output=True,
        )

        # Create the attempt branch from uas-wip and check it out
        subprocess.run(
            ["git", "checkout", "-b", branch, "uas-wip"],
            cwd=workspace,
            capture_output=True,
            check=True,
        )
        logger.debug("Created attempt branch %s from uas-wip", branch)
        return branch
    except Exception:
        logger.warning(
            "Failed to create attempt branch %s in %s",
            branch, workspace, exc_info=True,
        )
        return ""


def commit_attempt(workspace: str, branch: str, message: str) -> None:
    """Stage all changes and commit on the given attempt branch.

    Silently skips if the workspace is not a git repo, the branch does not
    match the current HEAD, or there are no changes to commit.
    """
    try:
        git_dir = os.path.join(workspace, ".git")
        if not os.path.isdir(git_dir):
            return

        # Verify we are on the expected branch
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True,
        )
        current = result.stdout.strip()
        if current != branch:
            logger.debug(
                "Expected branch %s but on %s; skipping commit",
                branch, current,
            )
            return

        subprocess.run(
            ["git", "add", "-A"],
            cwd=workspace,
            capture_output=True,
            check=True,
        )

        # Check if there are staged changes
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=workspace,
            capture_output=True,
        )
        if diff.returncode == 0:
            return

        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=workspace,
            capture_output=True,
            check=True,
        )
        logger.debug("Committed on %s: %s", branch, message)
    except Exception:
        logger.debug(
            "Commit on branch %s failed", branch, exc_info=True,
        )


def rollback_to_checkpoint(workspace: str, step_id: int) -> None:
    """Reset the workspace to the ``uas-wip`` checkpoint and clean up failed branches.

    Checks out ``uas-wip`` and hard-resets the working tree to match,
    ensuring a pristine filesystem state.  Then deletes all attempt branches
    for the given *step_id*.
    """
    try:
        git_dir = os.path.join(workspace, ".git")
        if not os.path.isdir(git_dir):
            return

        # Switch to uas-wip and hard-reset
        subprocess.run(
            ["git", "checkout", "uas-wip"],
            cwd=workspace,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "reset", "--hard"],
            cwd=workspace,
            capture_output=True,
            check=True,
        )
        # Remove untracked files that might have been left behind
        subprocess.run(
            ["git", "clean", "-fd"],
            cwd=workspace,
            capture_output=True,
            check=True,
        )

        # Delete all attempt branches for this step
        result = subprocess.run(
            ["git", "branch", "--list", f"uas/step-{step_id}/attempt-*"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True,
        )
        for line in result.stdout.splitlines():
            branch_name = line.strip()
            if branch_name:
                subprocess.run(
                    ["git", "branch", "-D", branch_name],
                    cwd=workspace,
                    capture_output=True,
                )
        logger.debug(
            "Rolled back to uas-wip checkpoint for step %s", step_id,
        )
    except Exception:
        logger.warning(
            "Rollback to checkpoint failed for step %s in %s",
            step_id, workspace, exc_info=True,
        )


def changed_py_files_since_uas_wip(workspace: str) -> list[str] | None:
    """Return ``.py`` files in *workspace* that differ from the ``uas-wip`` tip.

    Used by the orchestrator's lint pre-check (Section 6 of PLAN.md) to scope
    lint to files this attempt actually touched, instead of every ``*.py``
    in the workspace. Pre-existing files committed to ``uas-wip`` from a
    prior failed run must NOT be linted by the current attempt — they
    weren't this attempt's fault and re-poisoning every rollback creates
    an infinite failure loop.

    Includes both staged/unstaged changes against ``uas-wip`` and untracked
    ``.py`` files. Returns:

    - a list of workspace-relative ``.py`` paths (possibly empty) when the
      diff was computed successfully, or
    - ``None`` when the diff could not be computed (no git repo, no
      ``uas-wip`` ref, or git command failure). Callers should treat
      ``None`` as "scoping unavailable; do whatever you do for non-git
      workspaces" rather than "no files changed".
    """
    git_dir = os.path.join(workspace, ".git")
    if not os.path.isdir(git_dir):
        return None

    try:
        # Verify uas-wip exists; if not, we cannot scope by diff.
        check = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "refs/heads/uas-wip"],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if check.returncode != 0:
            return None

        files: set[str] = set()

        # Tracked files that differ from uas-wip (staged + unstaged + HEAD).
        diff = subprocess.run(
            ["git", "diff", "--name-only", "uas-wip", "--", "*.py"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True,
        )
        for line in diff.stdout.splitlines():
            line = line.strip()
            if line:
                files.add(line)

        # Untracked .py files (never committed yet).
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "--", "*.py"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True,
        )
        for line in untracked.stdout.splitlines():
            line = line.strip()
            if line:
                files.add(line)

        return sorted(files)
    except Exception:
        logger.debug(
            "changed_py_files_since_uas_wip failed for %s", workspace,
            exc_info=True,
        )
        return None


def _is_test_filename(basename: str) -> bool:
    """Return True if *basename* matches pytest's naming convention.

    Mirrors :func:`architect.planner._is_test_file` to keep ``git_state``
    free of cross-module imports. Accepted patterns: ``test_*.py`` or
    ``*_test.py``. ``conftest.py`` is intentionally excluded.
    """
    return (basename.startswith("test_") and basename.endswith(".py")) or (
        basename.endswith("_test.py")
    )


def changed_test_files_since_uas_wip(workspace: str) -> list[str] | None:
    """Return test files in *workspace* that differ from the ``uas-wip`` tip.

    Used by the architect's full-pytest gate (Section 7 of PLAN.md) to scope
    pytest to test files this attempt actually touched, instead of every
    test file discovered in the workspace. Pre-existing test files
    committed to ``uas-wip`` from a prior failed run that reference modules
    built by not-yet-run steps must NOT be the architect's authoritative
    gate — they were not this attempt's fault and re-running them every
    attempt creates an infinite failure loop. This mirrors the analogous
    Section 6 fix for the orchestrator's lint pre-check.

    Test files are identified by basename: ``test_*.py`` or ``*_test.py``
    (matching pytest's discovery rules and
    :func:`architect.planner._is_test_file`).

    Returns:

    - a list of workspace-relative test file paths (possibly empty) when
      the diff was computed successfully, or
    - ``None`` when the diff could not be computed (no git repo, no
      ``uas-wip`` ref, or git command failure). Callers should treat
      ``None`` as "scoping unavailable; fall back to legacy discovery".
    """
    py_files = changed_py_files_since_uas_wip(workspace)
    if py_files is None:
        return None
    return [p for p in py_files if _is_test_filename(os.path.basename(p))]


def promote_attempt(workspace: str, branch: str) -> None:
    """Fast-forward merge a successful attempt branch into ``uas-wip``.

    After merging, the attempt branch is deleted to keep the ref namespace
    clean.  Silently skips if the workspace is not a git repo or the merge
    fails.
    """
    try:
        git_dir = os.path.join(workspace, ".git")
        if not os.path.isdir(git_dir):
            return

        subprocess.run(
            ["git", "checkout", "uas-wip"],
            cwd=workspace,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "merge", "--ff-only", branch],
            cwd=workspace,
            capture_output=True,
            check=True,
        )
        # Clean up the merged attempt branch
        subprocess.run(
            ["git", "branch", "-d", branch],
            cwd=workspace,
            capture_output=True,
        )
        logger.debug("Promoted %s into uas-wip", branch)
    except Exception:
        logger.warning(
            "Failed to promote %s into uas-wip in %s",
            branch, workspace, exc_info=True,
        )
