"""Tests for Section 4: commit hygiene (git_checkpoint on wip branch, finalize_git squash)."""

import os
import subprocess

import pytest

from architect.main import ensure_git_repo, git_checkpoint, finalize_git


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


def _commit_messages(workspace, branch="HEAD"):
    """Return list of commit messages on *branch*."""
    log = _git(workspace, "log", branch, "--format=%s")
    return [line for line in log.splitlines() if line]


def _current_branch(workspace):
    return _git(workspace, "branch", "--show-current")


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    """Set git identity for commits in temp repos via env vars (not global config)."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.com")


def _init_workspace(tmp_path):
    """Create a workspace with two files so ensure_git_repo will init."""
    (tmp_path / "file1.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "file2.txt").write_text("world", encoding="utf-8")


class TestEnsureGitRepoCreatesWipBranch:
    def test_fresh_init_ends_on_wip_branch(self, tmp_path):
        _init_workspace(tmp_path)
        ensure_git_repo(str(tmp_path))

        assert _current_branch(str(tmp_path)) == "uas-wip"

    def test_main_has_initial_commit(self, tmp_path):
        _init_workspace(tmp_path)
        ensure_git_repo(str(tmp_path))

        msgs = _commit_messages(str(tmp_path), "main")
        assert msgs == ["Initial workspace state"]

    def test_uas_main_tag_created(self, tmp_path):
        _init_workspace(tmp_path)
        ensure_git_repo(str(tmp_path))

        # uas-main tag should point to the initial commit on main
        tag_commit = _git(str(tmp_path), "rev-parse", "uas-main")
        main_commit = _git(str(tmp_path), "rev-parse", "main")
        assert tag_commit == main_commit

    def test_existing_repo_not_modified(self, tmp_path):
        """ensure_git_repo returns early if .git already exists."""
        _init_workspace(tmp_path)
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=str(tmp_path), capture_output=True, check=True,
        )
        ensure_git_repo(str(tmp_path))
        # Should still be on main since ensure_git_repo returned early
        assert _current_branch(str(tmp_path)) == "main"


class TestGitCheckpointOnWipBranch:
    def test_checkpoint_commits_on_wip(self, tmp_path):
        _init_workspace(tmp_path)
        ensure_git_repo(str(tmp_path))

        # Add a file and checkpoint
        (tmp_path / "step1.py").write_text("print(1)", encoding="utf-8")
        git_checkpoint(str(tmp_path), 1, "Create script")

        assert _current_branch(str(tmp_path)) == "uas-wip"
        wip_msgs = _commit_messages(str(tmp_path), "uas-wip")
        assert "Step 1: Create script" in wip_msgs

        # main should still have only the initial commit
        main_msgs = _commit_messages(str(tmp_path), "main")
        assert main_msgs == ["Initial workspace state"]

    def test_checkpoint_creates_wip_if_missing(self, tmp_path):
        """If we're on main with no uas-wip, checkpoint creates it."""
        _init_workspace(tmp_path)
        # Manually init on main without creating uas-wip
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=str(tmp_path), capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(tmp_path), capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Init"],
            cwd=str(tmp_path), capture_output=True, check=True,
        )

        (tmp_path / "new.py").write_text("x = 1", encoding="utf-8")
        git_checkpoint(str(tmp_path), 1, "Add new file")

        assert _current_branch(str(tmp_path)) == "uas-wip"
        wip_msgs = _commit_messages(str(tmp_path), "uas-wip")
        assert "Step 1: Add new file" in wip_msgs

    def test_no_changes_no_commit(self, tmp_path):
        _init_workspace(tmp_path)
        ensure_git_repo(str(tmp_path))

        # Checkpoint with no changes
        before_msgs = _commit_messages(str(tmp_path), "uas-wip")
        git_checkpoint(str(tmp_path), 1, "No-op")
        after_msgs = _commit_messages(str(tmp_path), "uas-wip")
        assert before_msgs == after_msgs


class TestFinalizeGit:
    def test_squashes_wip_into_single_main_commit(self, tmp_path):
        """Multi-step run produces one commit on main."""
        _init_workspace(tmp_path)
        ensure_git_repo(str(tmp_path))

        # Simulate 3 steps with checkpoints
        (tmp_path / "step1.py").write_text("print(1)", encoding="utf-8")
        git_checkpoint(str(tmp_path), 1, "Parse data")

        (tmp_path / "step2.py").write_text("print(2)", encoding="utf-8")
        git_checkpoint(str(tmp_path), 2, "Analyze results")

        (tmp_path / "step3.py").write_text("print(3)", encoding="utf-8")
        git_checkpoint(str(tmp_path), 3, "Generate report")

        # Verify 3 checkpoint commits exist on wip
        wip_msgs = _commit_messages(str(tmp_path), "uas-wip")
        assert len(wip_msgs) == 4  # 3 steps + initial commit

        # Finalize
        finalize_git(str(tmp_path), "Build analytics dashboard")

        # main should have exactly 2 commits: initial + squashed
        main_msgs = _commit_messages(str(tmp_path), "main")
        assert len(main_msgs) == 2
        # Commit message should be derived from goal (no "UAS:" prefix)
        assert "Initial workspace state" not in main_msgs[0]
        assert main_msgs[1] == "Initial workspace state"

        # Should be on main after finalize
        assert _current_branch(str(tmp_path)) == "main"

    def test_wip_branch_deleted_after_finalize(self, tmp_path):
        _init_workspace(tmp_path)
        ensure_git_repo(str(tmp_path))

        (tmp_path / "output.txt").write_text("done", encoding="utf-8")
        git_checkpoint(str(tmp_path), 1, "Step one")

        finalize_git(str(tmp_path), "Test goal")

        branches = _git(str(tmp_path), "branch")
        assert "uas-wip" not in branches

    def test_long_goal_truncated_in_commit_message(self, tmp_path):
        _init_workspace(tmp_path)
        ensure_git_repo(str(tmp_path))

        (tmp_path / "out.txt").write_text("x", encoding="utf-8")
        git_checkpoint(str(tmp_path), 1, "Work")

        long_goal = "A" * 200
        finalize_git(str(tmp_path), long_goal)

        main_msgs = _commit_messages(str(tmp_path), "main")
        # Subject line must be ≤50 chars (best practice)
        subject = main_msgs[0].split("\n", 1)[0]
        assert len(subject) <= 50

    def test_no_wip_branch_is_noop(self, tmp_path):
        """finalize_git does nothing if there's no uas-wip branch."""
        _init_workspace(tmp_path)
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=str(tmp_path), capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(tmp_path), capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Init"],
            cwd=str(tmp_path), capture_output=True, check=True,
        )

        # Should not raise
        finalize_git(str(tmp_path), "some goal")
        assert _current_branch(str(tmp_path)) == "main"

    def test_no_git_repo_is_noop(self, tmp_path):
        """finalize_git does nothing if workspace isn't a git repo."""
        finalize_git(str(tmp_path), "some goal")

    def test_failed_run_preserves_wip(self, tmp_path):
        """When finalize_git is NOT called (run failed), wip branch remains."""
        _init_workspace(tmp_path)
        ensure_git_repo(str(tmp_path))

        (tmp_path / "partial.py").write_text("import sys", encoding="utf-8")
        git_checkpoint(str(tmp_path), 1, "Partial work")

        # Don't call finalize_git (simulating failed run)
        # Verify wip branch still has the checkpoint
        wip_msgs = _commit_messages(str(tmp_path), "uas-wip")
        assert "Step 1: Partial work" in wip_msgs

        # main is untouched
        main_msgs = _commit_messages(str(tmp_path), "main")
        assert main_msgs == ["Initial workspace state"]
