"""Tests for Section 7: Leftover script artifact cleanup."""

import os
import tempfile

from architect.main import cleanup_workspace_artifacts


class TestCleanupWorkspaceArtifacts:
    """Tests for cleanup_workspace_artifacts() script artifact removal."""

    def test_removes_uas_script_artifact(self, tmp_path):
        """New .py file containing UAS_RESULT should be removed."""
        artifact = tmp_path / "fix_git_structure.py"
        artifact.write_text(
            'print("done")\n'
            'print(\'UAS_RESULT: {"status": "ok", "files_written": [], "summary": "done"}\')\n',
            encoding="utf-8",
        )
        pre_step_files: set[str] = set()  # file didn't exist before

        removed = cleanup_workspace_artifacts(str(tmp_path), pre_step_files=pre_step_files)

        assert not artifact.exists()
        assert "fix_git_structure.py" in removed

    def test_preserves_legitimate_project_py(self, tmp_path):
        """A .py file that was present before the step should not be removed."""
        legit = tmp_path / "analysis.py"
        legit.write_text(
            'print(\'UAS_RESULT: {"status": "ok"}\')\n',
            encoding="utf-8",
        )
        pre_step_files = {"analysis.py"}

        removed = cleanup_workspace_artifacts(str(tmp_path), pre_step_files=pre_step_files)

        assert legit.exists()
        assert removed == []

    def test_preserves_new_py_without_uas_result(self, tmp_path):
        """A new .py file that does NOT contain UAS_RESULT is a real project file."""
        real = tmp_path / "app.py"
        real.write_text("def main():\n    pass\n", encoding="utf-8")
        pre_step_files: set[str] = set()

        removed = cleanup_workspace_artifacts(str(tmp_path), pre_step_files=pre_step_files)

        assert real.exists()
        assert removed == []

    def test_no_pre_step_files_skips_artifact_cleanup(self, tmp_path):
        """When pre_step_files is None (not provided), skip artifact cleanup."""
        artifact = tmp_path / "setup_project.py"
        artifact.write_text('print(\'UAS_RESULT: {"status": "ok"}\')\n', encoding="utf-8")

        removed = cleanup_workspace_artifacts(str(tmp_path), pre_step_files=None)

        assert artifact.exists()
        assert removed == []

    def test_still_cleans_pycache(self, tmp_path):
        """Existing __pycache__ cleanup still works alongside artifact removal."""
        pycache = tmp_path / "__pycache__"
        pycache.mkdir()
        (pycache / "mod.cpython-312.pyc").write_bytes(b"\x00")

        removed = cleanup_workspace_artifacts(str(tmp_path), pre_step_files=set())

        assert not pycache.exists()
        assert removed == []  # pycache isn't reported in the returned list

    def test_multiple_artifacts_removed(self, tmp_path):
        """Multiple UAS script artifacts in one cleanup."""
        for name in ("fix_git.py", "setup_env.py"):
            (tmp_path / name).write_text(
                f'print(\'UAS_RESULT: {{"status": "ok", "summary": "{name}"}}\')\n',
                encoding="utf-8",
            )
        # Also a legit file
        (tmp_path / "main.py").write_text("import sys\n", encoding="utf-8")
        pre_step_files: set[str] = set()

        removed = cleanup_workspace_artifacts(str(tmp_path), pre_step_files=pre_step_files)

        assert not (tmp_path / "fix_git.py").exists()
        assert not (tmp_path / "setup_env.py").exists()
        assert (tmp_path / "main.py").exists()
        assert sorted(removed) == ["fix_git.py", "setup_env.py"]
