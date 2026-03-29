"""Tests for Section 10: Workspace snapshot and recursive diff cleanup."""

import os

from architect.main import (
    snapshot_workspace,
    cleanup_step_artifacts,
    _remove_empty_dirs,
    _SNAPSHOT_SKIP_DIRS,
)


class TestSnapshotWorkspace:
    """Tests for snapshot_workspace()."""

    def test_captures_root_files(self, tmp_path):
        """Root-level files are included in the snapshot."""
        (tmp_path / "main.py").write_text("print('hi')\n")
        (tmp_path / "README.md").write_text("# hi\n")

        snap = snapshot_workspace(str(tmp_path))

        assert snap == {"main.py", "README.md"}

    def test_captures_nested_files(self, tmp_path):
        """Files in subdirectories are captured with relative paths."""
        src = tmp_path / "src" / "app"
        src.mkdir(parents=True)
        (src / "core.py").write_text("x = 1\n")
        (tmp_path / "setup.py").write_text("")

        snap = snapshot_workspace(str(tmp_path))

        assert snap == {"setup.py", os.path.join("src", "app", "core.py")}

    def test_skips_excluded_directories(self, tmp_path):
        """Directories in _SNAPSHOT_SKIP_DIRS are excluded."""
        for skip_dir in _SNAPSHOT_SKIP_DIRS:
            d = tmp_path / skip_dir
            d.mkdir(exist_ok=True)
            (d / "data.bin").write_bytes(b"\x00")
        (tmp_path / "app.py").write_text("pass\n")

        snap = snapshot_workspace(str(tmp_path))

        assert snap == {"app.py"}

    def test_empty_workspace(self, tmp_path):
        """An empty workspace returns an empty set."""
        snap = snapshot_workspace(str(tmp_path))
        assert snap == set()


class TestRemoveEmptyDirs:
    """Tests for _remove_empty_dirs()."""

    def test_removes_empty_leaf_dir(self, tmp_path):
        """An empty leaf directory is removed."""
        (tmp_path / "empty_dir").mkdir()
        _remove_empty_dirs(str(tmp_path))
        assert not (tmp_path / "empty_dir").exists()

    def test_removes_nested_empty_dirs(self, tmp_path):
        """Nested empty directories are removed bottom-up."""
        (tmp_path / "a" / "b" / "c").mkdir(parents=True)
        _remove_empty_dirs(str(tmp_path))
        assert not (tmp_path / "a").exists()

    def test_preserves_non_empty_dir(self, tmp_path):
        """Directories containing files are kept."""
        d = tmp_path / "keep"
        d.mkdir()
        (d / "file.txt").write_text("data\n")
        _remove_empty_dirs(str(tmp_path))
        assert d.exists()

    def test_does_not_remove_skip_dirs(self, tmp_path):
        """Directories in _SNAPSHOT_SKIP_DIRS are not removed even if empty."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        _remove_empty_dirs(str(tmp_path))
        assert git_dir.exists()


class TestCleanupStepArtifacts:
    """Tests for cleanup_step_artifacts()."""

    def test_removes_unclaimed_new_files(self, tmp_path):
        """New files that the step did not claim are removed."""
        (tmp_path / "existing.py").write_text("pass\n")
        pre = snapshot_workspace(str(tmp_path))

        # Simulate step creating files
        (tmp_path / "existing.py").write_text("pass\n")  # unchanged
        (tmp_path / "artifact.py").write_text('print("UAS_RESULT")\n')
        (tmp_path / "claimed.py").write_text("real output\n")

        removed = cleanup_step_artifacts(
            str(tmp_path),
            pre_snapshot=pre,
            step_output_files={"claimed.py"},
        )

        assert "artifact.py" in removed
        assert not (tmp_path / "artifact.py").exists()
        assert (tmp_path / "claimed.py").exists()
        assert (tmp_path / "existing.py").exists()

    def test_preserves_preexisting_files(self, tmp_path):
        """Files that existed before the step are never removed."""
        (tmp_path / "old.py").write_text("pass\n")
        pre = snapshot_workspace(str(tmp_path))

        removed = cleanup_step_artifacts(
            str(tmp_path),
            pre_snapshot=pre,
            step_output_files=set(),
        )

        assert removed == []
        assert (tmp_path / "old.py").exists()

    def test_removes_nested_artifacts(self, tmp_path):
        """Artifacts in subdirectories are also removed."""
        pre = snapshot_workspace(str(tmp_path))

        sub = tmp_path / "scripts"
        sub.mkdir()
        (sub / "step01_config.py").write_text("pass\n")
        (sub / "validate_phase2.py").write_text("pass\n")

        removed = cleanup_step_artifacts(
            str(tmp_path),
            pre_snapshot=pre,
            step_output_files=set(),
        )

        assert os.path.join("scripts", "step01_config.py") in removed
        assert os.path.join("scripts", "validate_phase2.py") in removed
        assert not (sub / "step01_config.py").exists()
        # Empty dir should also be cleaned up
        assert not sub.exists()

    def test_removes_data_file_debris(self, tmp_path):
        """CSV, JSON, and other data files from intermediate processing are removed."""
        pre = snapshot_workspace(str(tmp_path))

        (tmp_path / "cleaned_data.csv").write_text("a,b\n1,2\n")
        (tmp_path / "raw_data.json").write_text('{"key": 1}\n')
        (tmp_path / "spec.json").write_text("{}\n")

        removed = cleanup_step_artifacts(
            str(tmp_path),
            pre_snapshot=pre,
            step_output_files=set(),
        )

        assert len(removed) == 3
        assert not (tmp_path / "cleaned_data.csv").exists()
        assert not (tmp_path / "raw_data.json").exists()

    def test_claimed_files_with_normpath(self, tmp_path):
        """Claimed files with different path separators are still protected."""
        pre = snapshot_workspace(str(tmp_path))

        sub = tmp_path / "src" / "app"
        sub.mkdir(parents=True)
        (sub / "main.py").write_text("pass\n")

        removed = cleanup_step_artifacts(
            str(tmp_path),
            pre_snapshot=pre,
            step_output_files={"src/app/main.py"},
        )

        assert removed == []
        assert (sub / "main.py").exists()

    def test_returns_sorted_list(self, tmp_path):
        """Removed artifacts are returned in sorted order."""
        pre = snapshot_workspace(str(tmp_path))

        (tmp_path / "z_artifact.py").write_text("pass\n")
        (tmp_path / "a_artifact.py").write_text("pass\n")
        (tmp_path / "m_artifact.py").write_text("pass\n")

        removed = cleanup_step_artifacts(
            str(tmp_path),
            pre_snapshot=pre,
            step_output_files=set(),
        )

        assert removed == ["a_artifact.py", "m_artifact.py", "z_artifact.py"]

    def test_step_output_never_removed(self, tmp_path):
        """The step's declared output files are never removed even if new."""
        (tmp_path / "old_file.py").write_text("pass\n")
        pre = snapshot_workspace(str(tmp_path))

        # Step produces both claimed and unclaimed files
        (tmp_path / "result.csv").write_text("a,b\n")
        sub = tmp_path / "src"
        sub.mkdir()
        (sub / "module.py").write_text("pass\n")
        (tmp_path / "leftover.tmp").write_text("junk\n")

        removed = cleanup_step_artifacts(
            str(tmp_path),
            pre_snapshot=pre,
            step_output_files={"result.csv", os.path.join("src", "module.py")},
        )

        assert "leftover.tmp" in removed
        assert (tmp_path / "result.csv").exists()
        assert (sub / "module.py").exists()
