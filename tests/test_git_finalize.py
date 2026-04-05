"""Tests for Section 5: Hardened git finalization."""

import os
import subprocess

import pytest

from architect.main import (
    ensure_git_repo,
    finalize_git,
    git_checkpoint,
    _ensure_gitignore_data_patterns,
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


class TestEnsureGitRepoSingleFile:
    """ensure_git_repo with only one file should now init (lowered threshold)."""

    def test_single_file_triggers_init(self, tmp_path):
        (tmp_path / "only_file.txt").write_text("data", encoding="utf-8")
        ensure_git_repo(str(tmp_path))

        assert os.path.isdir(tmp_path / ".git")
        assert _current_branch(str(tmp_path)) == "uas-wip"

    def test_empty_workspace_no_init(self, tmp_path):
        """Empty workspace (no non-dot entries, no .py in subdirs) should not init."""
        ensure_git_repo(str(tmp_path))
        assert not os.path.isdir(tmp_path / ".git")


class TestEnsureGitRepoSubdirPy:
    """ensure_git_repo with subdirectory containing .py files should init."""

    def test_subdir_with_py_triggers_init(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "app.py").write_text("print('hi')", encoding="utf-8")
        ensure_git_repo(str(tmp_path))

        assert os.path.isdir(tmp_path / ".git")
        assert _current_branch(str(tmp_path)) == "uas-wip"

    def test_nested_subdir_with_py(self, tmp_path):
        """Project in nested subdirectory should still trigger init."""
        nested = tmp_path / "project" / "src"
        nested.mkdir(parents=True)
        (nested / "main.py").write_text("x = 1", encoding="utf-8")
        ensure_git_repo(str(tmp_path))

        assert os.path.isdir(tmp_path / ".git")


class TestFinalizeGitNoWipWithChanges:
    """finalize_git when uas-wip doesn't exist but there are uncommitted changes."""

    def test_uncommitted_changes_committed(self, tmp_path):
        (tmp_path / "file1.txt").write_text("hello", encoding="utf-8")
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

        # Add uncommitted changes
        (tmp_path / "new_module.py").write_text("x = 1", encoding="utf-8")

        finalize_git(str(tmp_path), "Test goal")

        assert _current_branch(str(tmp_path)) == "main"
        msgs = _commit_messages(str(tmp_path), "main")
        assert any(m != "Init" and m != "Initial workspace state" for m in msgs)
        # Verify new_module.py is committed
        tracked = _git(str(tmp_path), "ls-files")
        assert "new_module.py" in tracked

    def test_no_changes_with_gitignore_is_noop(self, tmp_path):
        """No wip branch, no uncommitted changes, gitignore present -> no new commit."""
        (tmp_path / "file1.txt").write_text("hello", encoding="utf-8")
        (tmp_path / ".gitignore").write_text(
            "*.csv\n*.pkl\n*.parquet\n*.joblib\n*.npz\n"
            "*.h5\n*.hdf5\n*.feather\n*.arrow\n"
            "*.sqlite\n*.db\nmodels/\n",
            encoding="utf-8",
        )
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

        finalize_git(str(tmp_path), "Test goal")

        assert _current_branch(str(tmp_path)) == "main"
        msgs = _commit_messages(str(tmp_path), "main")
        assert len(msgs) == 1
        assert msgs[0] == "Init"


class TestFinalizeGitSquashFallback:
    """finalize_git falls back to regular commit when squash merge fails."""

    def test_squash_failure_falls_back(self, tmp_path):
        (tmp_path / "file1.txt").write_text("hello", encoding="utf-8")
        (tmp_path / "file2.txt").write_text("world", encoding="utf-8")
        ensure_git_repo(str(tmp_path))

        # Make changes on uas-wip
        (tmp_path / "conflicting.txt").write_text("wip version", encoding="utf-8")
        git_checkpoint(str(tmp_path), 1, "Step one")

        # Make conflicting change on main
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=str(tmp_path), capture_output=True, check=True,
        )
        (tmp_path / "conflicting.txt").write_text("main version", encoding="utf-8")
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(tmp_path), capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Conflict on main"],
            cwd=str(tmp_path), capture_output=True, check=True,
        )

        # Go back to uas-wip for finalize_git
        subprocess.run(
            ["git", "checkout", "uas-wip"],
            cwd=str(tmp_path), capture_output=True, check=True,
        )

        finalize_git(str(tmp_path), "Test with conflict")

        # Should end up on main with the changes committed
        assert _current_branch(str(tmp_path)) == "main"
        msgs = _commit_messages(str(tmp_path), "main")
        assert any(m != "Init" and m != "Initial workspace state" for m in msgs)

        # uas-wip should be cleaned up
        branches = _git(str(tmp_path), "branch")
        assert "uas-wip" not in branches

        # Fallback should preserve uas-wip's version of files
        content = (tmp_path / "conflicting.txt").read_text(encoding="utf-8")
        assert content == "wip version"


class TestEnsureGitignoreDataPatterns:
    """_ensure_gitignore_data_patterns adds missing data artifact patterns."""

    EXPECTED_PATTERNS = [
        "*.csv", "*.pkl", "*.parquet", "*.joblib", "*.npz",
        "*.h5", "*.hdf5", "*.feather", "*.arrow",
        "*.sqlite", "*.db",
        "models/",
    ]

    def test_adds_patterns_to_existing(self, tmp_path):
        (tmp_path / ".gitignore").write_text("# empty\n", encoding="utf-8")
        _ensure_gitignore_data_patterns(str(tmp_path))

        content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        for pattern in self.EXPECTED_PATTERNS:
            assert pattern in content, f"Missing pattern: {pattern}"

    def test_creates_gitignore_if_missing(self, tmp_path):
        _ensure_gitignore_data_patterns(str(tmp_path))

        assert (tmp_path / ".gitignore").exists()
        content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        for pattern in self.EXPECTED_PATTERNS:
            assert pattern in content, f"Missing pattern: {pattern}"

    def test_no_duplicates_when_present(self, tmp_path):
        (tmp_path / ".gitignore").write_text(
            "*.joblib\n*.npz\n*.csv\n", encoding="utf-8",
        )
        _ensure_gitignore_data_patterns(str(tmp_path))

        content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert content.count("*.joblib") == 1
        assert content.count("*.csv") == 1


class TestFinalizeGitCleansAttemptBranches:
    """finalize_git removes leftover uas/step-*/attempt-* branches."""

    def test_attempt_branches_deleted_after_finalize(self, tmp_path):
        ws = str(tmp_path)
        (tmp_path / "app.py").write_text("print('hi')", encoding="utf-8")
        ensure_git_repo(ws)

        # Make a change on uas-wip so squash merge has something to commit
        (tmp_path / "result.py").write_text("x = 1", encoding="utf-8")
        git_checkpoint(ws, 1, "Step one")

        # Create leftover attempt branches (simulating incomplete cleanup)
        _git(ws, "branch", "uas/step-1/attempt-1", "uas-wip")
        _git(ws, "branch", "uas/step-1/attempt-2", "uas-wip")
        _git(ws, "branch", "uas/step-2/attempt-1", "uas-wip")

        # Verify they exist
        branches_before = _git(ws, "branch")
        assert "uas/step-1/attempt-1" in branches_before
        assert "uas/step-2/attempt-1" in branches_before

        finalize_git(ws, "Clean up test")

        branches_after = _git(ws, "branch")
        assert "uas/step-1/attempt-1" not in branches_after
        assert "uas/step-1/attempt-2" not in branches_after
        assert "uas/step-2/attempt-1" not in branches_after
        assert "uas-wip" not in branches_after
        assert "main" in branches_after

    def test_no_attempt_branches_is_fine(self, tmp_path):
        """finalize_git works when there are no attempt branches to clean up."""
        ws = str(tmp_path)
        (tmp_path / "app.py").write_text("print('hi')", encoding="utf-8")
        ensure_git_repo(ws)

        (tmp_path / "result.py").write_text("x = 1", encoding="utf-8")
        git_checkpoint(ws, 1, "Step one")

        finalize_git(ws, "No attempt branches")

        assert _current_branch(ws) == "main"
        branches = _git(ws, "branch")
        assert "uas-wip" not in branches


class TestFinalizeGitCleanRepo:
    """finalize_git leaves a clean repo when data files are covered by gitignore."""

    def test_data_files_not_untracked_after_finalize(self, tmp_path):
        """Data files matching gitignore patterns are not left untracked."""
        # Set up a git repo with an initial commit
        (tmp_path / "app.py").write_text("print('hi')", encoding="utf-8")
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

        # Create data files that should be ignored
        (tmp_path / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        (tmp_path / "model.pkl").write_bytes(b"\x80\x04\x95")
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        (models_dir / "best.h5").write_bytes(b"\x00")

        finalize_git(str(tmp_path), "Test clean repo")

        # Repo should be clean — all data files covered by gitignore
        porcelain = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            check=True,
        )
        assert porcelain.stdout.strip() == "", (
            f"Repo not clean after finalize:\n{porcelain.stdout}"
        )
