"""Integration tests for git state management — 3-attempt failure rollback.

Task 3.9: Simulate a 3-attempt failure sequence and verify the workspace
filesystem is byte-identical to the pre-step state after rollback.
"""

import hashlib
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
    raw = _git(workspace, "branch")
    return {line.lstrip("* ").strip() for line in raw.splitlines() if line.strip()}


def _snapshot_files(workspace):
    """Return {relative_path: sha256} for all non-.git files in workspace."""
    snapshot = {}
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d != ".git"]
        for fname in files:
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, workspace)
            data = open(full, "rb").read()
            snapshot[rel] = hashlib.sha256(data).hexdigest()
    return snapshot


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.com")


@pytest.fixture()
def workspace(tmp_path):
    """Workspace with initial commit, uas-main tag, uas-wip branch, and
    several baseline files for realistic content comparison."""
    ws = str(tmp_path)

    # Create a non-trivial baseline: multiple files, a subdirectory
    (tmp_path / "main.py").write_text("def main(): pass\n", encoding="utf-8")
    (tmp_path / "config.json").write_text('{"key": "value"}\n', encoding="utf-8")
    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "util.py").write_text("CONST = 42\n", encoding="utf-8")

    _git(ws, "init", "-b", "main")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-m", "Initial workspace state")
    _git(ws, "tag", "-f", "uas-main")
    _git(ws, "checkout", "-b", "uas-wip")
    return ws


class TestThreeStrikeRollback:
    """Simulate 3 failed attempts on a step, then verify byte-identical rollback."""

    def test_filesystem_identical_after_3_failures(self, workspace):
        """Core test: snapshot before, run 3 failed attempts, rollback,
        compare snapshot — must be byte-identical."""
        pre_snapshot = _snapshot_files(workspace)
        pre_wip_sha = _git(workspace, "rev-parse", "uas-wip")

        # --- Attempt 1: add a new file, modify existing, commit ---
        b1 = create_attempt_branch(workspace, step_id=1, attempt=1)
        assert b1 != ""
        with open(os.path.join(workspace, "new_module.py"), "w") as f:
            f.write("class Broken: pass\n")
        with open(os.path.join(workspace, "main.py"), "w") as f:
            f.write("def main(): raise RuntimeError('oops')\n")
        commit_attempt(workspace, b1, "Attempt 1 — fails")

        # --- Attempt 2: different changes, create subdirectory ---
        _git(workspace, "checkout", "uas-wip")
        b2 = create_attempt_branch(workspace, step_id=1, attempt=2)
        os.makedirs(os.path.join(workspace, "extra"), exist_ok=True)
        with open(os.path.join(workspace, "extra", "debug.py"), "w") as f:
            f.write("import pdb; pdb.set_trace()\n")
        with open(os.path.join(workspace, "config.json"), "w") as f:
            f.write('{"key": "CORRUPTED"}\n')
        commit_attempt(workspace, b2, "Attempt 2 — fails")

        # --- Attempt 3: leave untracked files (no commit) ---
        _git(workspace, "checkout", "uas-wip")
        b3 = create_attempt_branch(workspace, step_id=1, attempt=3)
        with open(os.path.join(workspace, "untracked_junk.log"), "w") as f:
            f.write("garbage\n")
        with open(os.path.join(workspace, "src", "temp.dat"), "wb") as f:
            f.write(b"\x00\x01\x02")
        # Deliberately do NOT commit — simulates crash mid-attempt

        # --- Rollback (the 3-strike trigger) ---
        rollback_to_checkpoint(workspace, step_id=1)

        # --- Verify byte-identical filesystem ---
        post_snapshot = _snapshot_files(workspace)
        assert post_snapshot == pre_snapshot, (
            f"Filesystem mismatch after rollback.\n"
            f"  Extra files: {set(post_snapshot) - set(pre_snapshot)}\n"
            f"  Missing files: {set(pre_snapshot) - set(post_snapshot)}\n"
            f"  Changed files: {[k for k in pre_snapshot if k in post_snapshot and pre_snapshot[k] != post_snapshot[k]]}"
        )

        # --- Verify git state ---
        assert _current_branch(workspace) == "uas-wip"
        post_wip_sha = _git(workspace, "rev-parse", "uas-wip")
        assert post_wip_sha == pre_wip_sha

        # Working tree must be clean
        status = _git(workspace, "status", "--porcelain")
        assert status == ""

    def test_attempt_branches_cleaned_up(self, workspace):
        """All 3 attempt branches for the step must be deleted after rollback."""
        for attempt in range(1, 4):
            branch = create_attempt_branch(workspace, step_id=5, attempt=attempt)
            with open(os.path.join(workspace, f"file_{attempt}.py"), "w") as f:
                f.write(f"x = {attempt}\n")
            commit_attempt(workspace, branch, f"Attempt {attempt}")
            _git(workspace, "checkout", "uas-wip")

        rollback_to_checkpoint(workspace, step_id=5)

        branches = _branches(workspace)
        for attempt in range(1, 4):
            assert f"uas/step-5/attempt-{attempt}" not in branches

    def test_other_step_state_preserved(self, workspace):
        """A successful earlier step's changes must survive the failed step's
        rollback."""
        # Step 1 succeeds: add a file, promote to uas-wip
        b1 = create_attempt_branch(workspace, step_id=1, attempt=1)
        with open(os.path.join(workspace, "step1_output.py"), "w") as f:
            f.write("result = 'success'\n")
        commit_attempt(workspace, b1, "Step 1 success")
        promote_attempt(workspace, b1)

        # Snapshot after step 1 (this is the checkpoint for step 2)
        pre_step2_snapshot = _snapshot_files(workspace)

        # Step 2 fails 3 times
        for attempt in range(1, 4):
            branch = create_attempt_branch(workspace, step_id=2, attempt=attempt)
            with open(os.path.join(workspace, "step2_broken.py"), "w") as f:
                f.write(f"# attempt {attempt}\nraise Exception\n")
            commit_attempt(workspace, branch, f"Step 2 attempt {attempt}")
            _git(workspace, "checkout", "uas-wip")

        rollback_to_checkpoint(workspace, step_id=2)

        # Step 1's output must still exist and be unchanged
        assert os.path.exists(os.path.join(workspace, "step1_output.py"))
        post_snapshot = _snapshot_files(workspace)
        assert post_snapshot == pre_step2_snapshot

    def test_binary_files_restored(self, workspace):
        """Binary content must also be byte-identical after rollback."""
        # Add a binary file to the baseline via uas-wip
        binary_data = bytes(range(256))
        with open(os.path.join(workspace, "data.bin"), "wb") as f:
            f.write(binary_data)
        _git(workspace, "add", "-A")
        _git(workspace, "commit", "-m", "Add binary baseline")

        pre_snapshot = _snapshot_files(workspace)

        # 3 attempts that corrupt the binary
        for attempt in range(1, 4):
            branch = create_attempt_branch(workspace, step_id=3, attempt=attempt)
            with open(os.path.join(workspace, "data.bin"), "wb") as f:
                f.write(b"CORRUPTED" * attempt)
            commit_attempt(workspace, branch, f"Corrupt binary attempt {attempt}")
            _git(workspace, "checkout", "uas-wip")

        rollback_to_checkpoint(workspace, step_id=3)

        post_snapshot = _snapshot_files(workspace)
        assert post_snapshot == pre_snapshot

        # Also verify raw bytes
        with open(os.path.join(workspace, "data.bin"), "rb") as f:
            assert f.read() == binary_data

    def test_deleted_files_restored(self, workspace):
        """Files deleted during attempts must reappear after rollback."""
        pre_snapshot = _snapshot_files(workspace)

        for attempt in range(1, 4):
            branch = create_attempt_branch(workspace, step_id=4, attempt=attempt)
            # Delete a baseline file
            os.remove(os.path.join(workspace, "src", "util.py"))
            commit_attempt(workspace, branch, f"Delete util attempt {attempt}")
            _git(workspace, "checkout", "uas-wip")

        rollback_to_checkpoint(workspace, step_id=4)

        assert os.path.exists(os.path.join(workspace, "src", "util.py"))
        with open(os.path.join(workspace, "src", "util.py"), encoding="utf-8") as f:
            assert f.read() == "CONST = 42\n"
        assert _snapshot_files(workspace) == pre_snapshot

    def test_nested_directories_cleaned(self, workspace):
        """Deeply nested directories created during attempts must be fully
        removed after rollback."""
        pre_snapshot = _snapshot_files(workspace)

        branch = create_attempt_branch(workspace, step_id=6, attempt=1)
        deep = os.path.join(workspace, "a", "b", "c", "d")
        os.makedirs(deep)
        with open(os.path.join(deep, "deep.txt"), "w") as f:
            f.write("deep\n")
        commit_attempt(workspace, branch, "Deep nesting attempt 1")
        _git(workspace, "checkout", "uas-wip")

        # Repeat for attempts 2-3 with untracked nested dirs
        for attempt in (2, 3):
            branch = create_attempt_branch(workspace, step_id=6, attempt=attempt)
            nested = os.path.join(workspace, "x", "y")
            os.makedirs(nested, exist_ok=True)
            with open(os.path.join(nested, "tmp.py"), "w") as f:
                f.write("pass\n")
            # Leave untracked
            _git(workspace, "checkout", "uas-wip")

        rollback_to_checkpoint(workspace, step_id=6)

        post_snapshot = _snapshot_files(workspace)
        assert post_snapshot == pre_snapshot
        assert not os.path.exists(os.path.join(workspace, "a"))
        assert not os.path.exists(os.path.join(workspace, "x"))
