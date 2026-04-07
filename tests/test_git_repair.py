"""Tests for ensure_git_repo's partial-state repair behavior.

Covers Section 1 of PLAN.md: when ``.git/`` already exists, the function
inspects the repo and finishes whatever an interrupted previous run failed
to do, instead of returning silently and leaving callers to trip over the
half-initialized state.
"""

import os
import subprocess

import pytest

from architect.main import ensure_git_repo


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


def _has_ref(workspace, ref):
    """True if ``git show-ref --verify ref`` exits 0."""
    result = subprocess.run(
        ["git", "show-ref", "--verify", ref],
        cwd=workspace,
        capture_output=True,
    )
    return result.returncode == 0


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    """Set git identity for commits in temp repos via env vars."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.com")


class TestRepairHalfInit:
    """``.git/`` exists from a previous ``git init`` but no commit was made."""

    def test_repair_creates_commit_and_wip_branch(self, tmp_path):
        ws = str(tmp_path)
        (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
        (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")

        # Half-init state: only `git init` ran.
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=ws, capture_output=True, check=True,
        )
        # Sanity check: no commits yet, no uas-wip
        assert subprocess.run(
            ["git", "log", "-1"],
            cwd=ws, capture_output=True,
        ).returncode != 0
        assert not _has_ref(ws, "refs/heads/uas-wip")

        ensure_git_repo(ws)

        # Repair finished the missing init steps.
        assert _has_ref(ws, "refs/heads/uas-wip")
        assert _current_branch(ws) == "uas-wip"

        # At least one commit on uas-wip.
        log = _git(ws, "log", "uas-wip", "--format=%s")
        msgs = [line for line in log.splitlines() if line]
        assert len(msgs) >= 1
        assert "Initial workspace state" in msgs

        # Working tree is clean (the user's files are tracked).
        status = _git(ws, "status", "--porcelain")
        assert status == ""
        tracked = _git(ws, "ls-files")
        assert "main.py" in tracked
        assert "README.md" in tracked


class TestNoOpHealthyRepo:
    """Healthy repo on ``uas-wip`` with one commit -- ``ensure_git_repo`` is
    a no-op (commit count and branch unchanged)."""

    def test_healthy_uas_wip_repo_left_alone(self, tmp_path):
        ws = str(tmp_path)
        (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=ws, capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "add", "-A"],
            cwd=ws, capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial workspace state"],
            cwd=ws, capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "tag", "-f", "uas-main"],
            cwd=ws, capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "checkout", "-b", "uas-wip"],
            cwd=ws, capture_output=True, check=True,
        )

        before_branch = _current_branch(ws)
        before_commits = _git(ws, "log", "--all", "--format=%H")
        before_uas_main_sha = _git(ws, "rev-parse", "uas-main")

        ensure_git_repo(ws)

        assert _current_branch(ws) == before_branch
        assert _git(ws, "log", "--all", "--format=%H") == before_commits
        assert _git(ws, "rev-parse", "uas-main") == before_uas_main_sha


class TestRepairPostFinalize:
    """Repo where ``finalize_git`` already squashed ``uas-wip`` away.

    Only ``main`` exists; ``uas-wip`` and the ``uas-main`` tag are absent.
    ``ensure_git_repo`` should recognize this as the post-finalize state and
    re-create ``uas-wip`` from the current ``HEAD`` without re-tagging
    ``uas-main`` (since we don't know what the original baseline was)."""

    def test_recreates_wip_without_retagging(self, tmp_path):
        ws = str(tmp_path)
        (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "result.py").write_text("y = 2\n", encoding="utf-8")
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=ws, capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "add", "-A"],
            cwd=ws, capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Squashed run output"],
            cwd=ws, capture_output=True, check=True,
        )
        # Confirm post-finalize preconditions
        assert not _has_ref(ws, "refs/heads/uas-wip")
        assert not _has_ref(ws, "refs/tags/uas-main")

        head_before = _git(ws, "rev-parse", "HEAD")
        main_log_before = _git(ws, "log", "main", "--format=%H")

        ensure_git_repo(ws)

        # uas-wip now exists and points at the current HEAD.
        assert _has_ref(ws, "refs/heads/uas-wip")
        assert _git(ws, "rev-parse", "uas-wip") == head_before

        # uas-main tag was NOT created.
        assert not _has_ref(ws, "refs/tags/uas-main")

        # main is unchanged (no new commits, same history).
        assert _git(ws, "log", "main", "--format=%H") == main_log_before
