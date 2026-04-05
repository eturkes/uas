"""Tests for architect.git_state — branch-per-attempt git state management."""

import os
import subprocess

import pytest

from architect.git_state import (
    commit_attempt,
    create_attempt_branch,
    promote_attempt,
    rollback_to_checkpoint,
)


def _git(workspace, *args):
    """Run a git command in workspace, return stdout."""
    result = subprocess.run(
        ["git"] + list(args),
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _current_branch(workspace):
    return _git(workspace, "branch", "--show-current")


def _branches(workspace):
    """Return set of branch names (stripped of leading markers)."""
    raw = _git(workspace, "branch")
    return {line.lstrip("* ").strip() for line in raw.splitlines() if line.strip()}


def _commit_messages(workspace, branch="HEAD"):
    log = _git(workspace, "log", branch, "--format=%s")
    return [line for line in log.splitlines() if line]


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    """Set git identity for commits in temp repos via env vars."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.com")


@pytest.fixture()
def workspace(tmp_path):
    """Create a workspace with an initial commit, uas-main tag, and uas-wip branch."""
    ws = str(tmp_path)
    (tmp_path / "file.txt").write_text("initial", encoding="utf-8")
    _git(ws, "init", "-b", "main")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-m", "Initial workspace state")
    _git(ws, "tag", "-f", "uas-main")
    _git(ws, "checkout", "-b", "uas-wip")
    return ws


# ---------------------------------------------------------------------------
# create_attempt_branch
# ---------------------------------------------------------------------------


class TestCreateAttemptBranch:
    def test_creates_branch_from_wip(self, workspace):
        branch = create_attempt_branch(workspace, step_id=1, attempt=1)
        assert branch == "uas/step-1/attempt-1"
        assert _current_branch(workspace) == branch

    def test_branch_inherits_wip_content(self, workspace):
        # Add a checkpoint commit to uas-wip
        (workspace + "/extra.txt").replace("", "")
        with open(os.path.join(workspace, "extra.txt"), "w") as f:
            f.write("checkpoint data")
        _git(workspace, "add", "-A")
        _git(workspace, "commit", "-m", "Checkpoint")

        branch = create_attempt_branch(workspace, step_id=2, attempt=1)
        assert branch != ""
        # The attempt branch should contain the checkpoint file
        assert os.path.exists(os.path.join(workspace, "extra.txt"))

    def test_returns_empty_without_git(self, tmp_path):
        branch = create_attempt_branch(str(tmp_path), step_id=1, attempt=1)
        assert branch == ""

    def test_returns_empty_without_wip_branch(self, tmp_path):
        ws = str(tmp_path)
        (tmp_path / "f.txt").write_text("x", encoding="utf-8")
        _git(ws, "init", "-b", "main")
        _git(ws, "add", "-A")
        _git(ws, "commit", "-m", "Init")
        # No uas-wip branch
        branch = create_attempt_branch(ws, step_id=1, attempt=1)
        assert branch == ""

    def test_recreates_existing_branch(self, workspace):
        # Create the branch once
        create_attempt_branch(workspace, step_id=1, attempt=1)
        with open(os.path.join(workspace, "new.txt"), "w") as f:
            f.write("attempt 1 work")
        _git(workspace, "add", "-A")
        _git(workspace, "commit", "-m", "Work")

        # Switch back to uas-wip to simulate retry setup
        _git(workspace, "checkout", "uas-wip")

        # Re-creating the same branch should succeed (delete + recreate)
        branch = create_attempt_branch(workspace, step_id=1, attempt=1)
        assert branch == "uas/step-1/attempt-1"
        # The file from the first attempt should not be present
        assert not os.path.exists(os.path.join(workspace, "new.txt"))

    def test_multiple_steps(self, workspace):
        b1 = create_attempt_branch(workspace, step_id=1, attempt=1)
        _git(workspace, "checkout", "uas-wip")
        b2 = create_attempt_branch(workspace, step_id=2, attempt=1)
        assert b1 == "uas/step-1/attempt-1"
        assert b2 == "uas/step-2/attempt-1"
        assert "uas/step-1/attempt-1" in _branches(workspace)


# ---------------------------------------------------------------------------
# commit_attempt
# ---------------------------------------------------------------------------


class TestCommitAttempt:
    def test_commits_changes(self, workspace):
        branch = create_attempt_branch(workspace, step_id=1, attempt=1)
        with open(os.path.join(workspace, "output.py"), "w") as f:
            f.write("print('hello')")

        commit_attempt(workspace, branch, "Attempt 1 implementation")
        msgs = _commit_messages(workspace)
        assert "Attempt 1 implementation" in msgs

    def test_skips_if_no_changes(self, workspace):
        branch = create_attempt_branch(workspace, step_id=1, attempt=1)
        msgs_before = _commit_messages(workspace)
        commit_attempt(workspace, branch, "Empty commit")
        msgs_after = _commit_messages(workspace)
        assert msgs_before == msgs_after

    def test_skips_if_wrong_branch(self, workspace):
        create_attempt_branch(workspace, step_id=1, attempt=1)
        with open(os.path.join(workspace, "output.py"), "w") as f:
            f.write("print('hello')")

        # Pass a different branch name
        commit_attempt(workspace, "wrong-branch", "Should not commit")
        # File should still be untracked
        status = _git(workspace, "status", "--porcelain")
        assert "output.py" in status

    def test_skips_without_git(self, tmp_path):
        # Should not raise
        commit_attempt(str(tmp_path), "any-branch", "msg")


# ---------------------------------------------------------------------------
# rollback_to_checkpoint
# ---------------------------------------------------------------------------


class TestRollbackToCheckpoint:
    def test_restores_wip_state(self, workspace):
        branch = create_attempt_branch(workspace, step_id=1, attempt=1)
        # Make changes on the attempt branch
        with open(os.path.join(workspace, "bad.py"), "w") as f:
            f.write("broken code")
        commit_attempt(workspace, branch, "Failed attempt")

        rollback_to_checkpoint(workspace, step_id=1)

        assert _current_branch(workspace) == "uas-wip"
        assert not os.path.exists(os.path.join(workspace, "bad.py"))

    def test_cleans_untracked_files(self, workspace):
        create_attempt_branch(workspace, step_id=1, attempt=1)
        # Create untracked file without committing
        with open(os.path.join(workspace, "untracked.txt"), "w") as f:
            f.write("junk")

        rollback_to_checkpoint(workspace, step_id=1)

        assert not os.path.exists(os.path.join(workspace, "untracked.txt"))

    def test_deletes_attempt_branches(self, workspace):
        create_attempt_branch(workspace, step_id=1, attempt=1)
        _git(workspace, "checkout", "uas-wip")
        create_attempt_branch(workspace, step_id=1, attempt=2)
        _git(workspace, "checkout", "uas-wip")

        rollback_to_checkpoint(workspace, step_id=1)

        branches = _branches(workspace)
        assert "uas/step-1/attempt-1" not in branches
        assert "uas/step-1/attempt-2" not in branches

    def test_preserves_other_step_branches(self, workspace):
        create_attempt_branch(workspace, step_id=1, attempt=1)
        _git(workspace, "checkout", "uas-wip")
        create_attempt_branch(workspace, step_id=2, attempt=1)
        _git(workspace, "checkout", "uas-wip")

        rollback_to_checkpoint(workspace, step_id=1)

        branches = _branches(workspace)
        assert "uas/step-2/attempt-1" in branches

    def test_skips_without_git(self, tmp_path):
        # Should not raise
        rollback_to_checkpoint(str(tmp_path), step_id=1)


# ---------------------------------------------------------------------------
# promote_attempt
# ---------------------------------------------------------------------------


class TestPromoteAttempt:
    def test_merges_into_wip(self, workspace):
        branch = create_attempt_branch(workspace, step_id=1, attempt=1)
        with open(os.path.join(workspace, "result.py"), "w") as f:
            f.write("x = 42")
        commit_attempt(workspace, branch, "Step 1 success")

        promote_attempt(workspace, branch)

        assert _current_branch(workspace) == "uas-wip"
        assert os.path.exists(os.path.join(workspace, "result.py"))
        msgs = _commit_messages(workspace)
        assert "Step 1 success" in msgs

    def test_deletes_attempt_branch_after_merge(self, workspace):
        branch = create_attempt_branch(workspace, step_id=1, attempt=1)
        with open(os.path.join(workspace, "result.py"), "w") as f:
            f.write("x = 42")
        commit_attempt(workspace, branch, "Done")

        promote_attempt(workspace, branch)

        assert branch not in _branches(workspace)

    def test_skips_without_git(self, tmp_path):
        # Should not raise
        promote_attempt(str(tmp_path), "uas/step-1/attempt-1")

    def test_sequential_promotions(self, workspace):
        # Step 1
        b1 = create_attempt_branch(workspace, step_id=1, attempt=1)
        with open(os.path.join(workspace, "step1.py"), "w") as f:
            f.write("step1 = True")
        commit_attempt(workspace, b1, "Step 1")
        promote_attempt(workspace, b1)

        # Step 2 should see step 1's work
        b2 = create_attempt_branch(workspace, step_id=2, attempt=1)
        assert os.path.exists(os.path.join(workspace, "step1.py"))
        with open(os.path.join(workspace, "step2.py"), "w") as f:
            f.write("step2 = True")
        commit_attempt(workspace, b2, "Step 2")
        promote_attempt(workspace, b2)

        assert _current_branch(workspace) == "uas-wip"
        assert os.path.exists(os.path.join(workspace, "step1.py"))
        assert os.path.exists(os.path.join(workspace, "step2.py"))
