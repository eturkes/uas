"""Tests for Section 8: Verification Loop and Success Validation."""

import os
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from architect.git_state import changed_py_files_since_uas_wip
from architect.main import (
    validate_uas_result,
    verify_step_output,
    validate_workspace,
)


class TestValidateUasResult:
    def test_no_uas_result(self):
        step = {"id": 1}
        assert validate_uas_result(step, "/workspace") is None

    def test_ok_status_passes(self):
        step = {"uas_result": {"status": "ok", "files_written": [], "summary": "done"}}
        assert validate_uas_result(step, "/workspace") is None

    def test_error_status_fails(self):
        step = {"uas_result": {"status": "error", "error": "file missing"}}
        result = validate_uas_result(step, "/workspace")
        assert result is not None
        assert "file missing" in result

    def test_missing_file_fails(self, tmp_path):
        step = {"uas_result": {
            "status": "ok",
            "files_written": ["nonexistent.txt"],
        }}
        result = validate_uas_result(step, str(tmp_path))
        assert result is not None
        assert "nonexistent.txt" in result

    def test_existing_file_passes(self, tmp_path):
        (tmp_path / "output.txt").write_text("data")
        step = {"uas_result": {
            "status": "ok",
            "files_written": [str(tmp_path / "output.txt")],
        }}
        result = validate_uas_result(step, str(tmp_path))
        assert result is None

    def test_relative_file_resolved_against_workspace(self, tmp_path):
        (tmp_path / "result.csv").write_text("a,b\n1,2")
        step = {"uas_result": {
            "status": "ok",
            "files_written": ["result.csv"],
        }}
        result = validate_uas_result(step, str(tmp_path))
        assert result is None

    def test_relative_file_missing(self, tmp_path):
        step = {"uas_result": {
            "status": "ok",
            "files_written": ["missing.csv"],
        }}
        result = validate_uas_result(step, str(tmp_path))
        assert result is not None
        assert "missing.csv" in result

    def test_empty_files_written(self):
        step = {"uas_result": {"status": "ok", "files_written": []}}
        assert validate_uas_result(step, "/workspace") is None

    def test_error_without_message(self):
        step = {"uas_result": {"status": "error"}}
        result = validate_uas_result(step, "/workspace")
        assert result is not None
        assert "unknown error" in result


class TestVerifyStepOutput:
    def test_no_verify_field(self):
        step = {"id": 1, "verify": ""}
        assert verify_step_output(step, "/workspace") is None

    def test_no_verify_key(self):
        step = {"id": 1}
        assert verify_step_output(step, "/workspace") is None

    @patch("architect.main.run_orchestrator")
    def test_verification_passes(self, mock_orch):
        mock_orch.return_value = {
            "exit_code": 0,
            "stdout": "",
            "stderr": "stdout:\nVERIFICATION PASSED\nExit code: 0",
        }
        step = {
            "id": 1,
            "verify": "output.txt exists and has >0 bytes",
            "files_written": ["/workspace/output.txt"],
            "output": "wrote output.txt",
        }
        result = verify_step_output(step, "/workspace")
        assert result is None
        # Check the orchestrator was called with a verification task
        task_arg = mock_orch.call_args[0][0]
        assert "verification" in task_arg.lower() or "verify" in task_arg.lower()

    @patch("architect.main.run_orchestrator")
    def test_verification_calls_orchestrator(self, mock_orch):
        """Verify that verification invokes the orchestrator."""
        mock_orch.return_value = {
            "exit_code": 0,
            "stdout": "",
            "stderr": "stdout:\nVERIFICATION PASSED\nExit code: 0",
        }
        step = {"id": 1, "verify": "check something"}
        verify_step_output(step, "/workspace")
        mock_orch.assert_called_once()

    @patch("architect.main.run_orchestrator")
    def test_verification_fails(self, mock_orch):
        mock_orch.return_value = {
            "exit_code": 1,
            "stdout": "",
            "stderr": "stdout:\nVERIFICATION FAILED: file is empty\nExit code: 1",
        }
        step = {
            "id": 1,
            "verify": "output.txt has >0 bytes",
            "files_written": [],
            "output": "",
        }
        result = verify_step_output(step, "/workspace")
        assert result is not None

    @patch("architect.main.run_orchestrator")
    def test_verification_orchestrator_crash(self, mock_orch):
        mock_orch.return_value = {
            "exit_code": -1,
            "stdout": "",
            "stderr": "Orchestrator timed out.",
        }
        step = {"id": 1, "verify": "check something"}
        result = verify_step_output(step, "/workspace")
        assert result is not None

    @patch("architect.main.run_orchestrator")
    def test_verification_passed_in_stdout(self, mock_orch):
        mock_orch.return_value = {
            "exit_code": 0,
            "stdout": "VERIFICATION PASSED",
            "stderr": "",
        }
        step = {"id": 1, "verify": "check something"}
        result = verify_step_output(step, "/workspace")
        assert result is None


class TestVerifyStepOutputSection6Regression:
    """Section 6 of PLAN.md regression: verify_step_output must not be
    sabotaged by the orchestrator's lint pre-check rolling back a
    verifier-script attempt because of pre-existing unused-import files
    committed to ``uas-wip``.

    The bug surfaced as ``Step N FAILED. Error: VERIFICATION PASSED``:
    the verifier sandbox printed VERIFICATION PASSED and exited 0, but
    the orchestrator subprocess wrapping it then ran ``lint_workspace``
    against every ``*.py`` in the workspace, found pre-existing F401
    errors, and exited 1. The architect's verify_step_output saw
    exit_code != 0 and surfaced the verifier's own stdout as the error.

    The fix scopes the lint pre-check to .py files this attempt actually
    touched (via ``changed_py_files_since_uas_wip``), so a read-only
    verifier script with no .py changes against ``uas-wip`` triggers no
    lint at all.
    """

    @pytest.fixture
    def workspace_with_pre_existing_unused_imports(self, tmp_path, monkeypatch):
        """Create a real git workspace seeded with pre-existing F401
        files committed to uas-wip — the exact situation that triggered
        the bug in the rehab/ project.
        """
        monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.com")
        monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
        monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.com")

        ws = str(tmp_path)
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_config.py").write_text(
            "import os\nimport pytest\n", encoding="utf-8",
        )
        (tmp_path / "config.py").write_text(
            "import sys\n", encoding="utf-8",
        )

        def _git(*args):
            subprocess.run(
                ["git"] + list(args),
                cwd=ws, capture_output=True, text=True, check=True,
            )

        _git("init", "-b", "main")
        _git("add", "-A")
        _git("commit", "-m", "Initial workspace state")
        _git("tag", "-f", "uas-main")
        _git("checkout", "-b", "uas-wip")
        return ws

    def test_changed_py_files_helper_returns_empty_for_clean_verifier(
        self, workspace_with_pre_existing_unused_imports,
    ):
        """Direct check on the helper used by the orchestrator's lint
        pre-check: a workspace with pre-existing F401 files committed
        to uas-wip but no changes since must report zero changed files.
        """
        ws = workspace_with_pre_existing_unused_imports
        result = changed_py_files_since_uas_wip(ws)
        assert result == [], (
            f"Pre-existing F401 files leaked into the changed-files "
            f"set: {result!r}. The orchestrator's lint pre-check would "
            f"re-blame the verifier attempt for them."
        )

    @patch("architect.main.run_orchestrator")
    def test_verify_step_output_succeeds_with_pre_existing_unused_imports(
        self, mock_orch, workspace_with_pre_existing_unused_imports,
    ):
        """End-to-end-ish: verify_step_output must return None when
        run against a real workspace whose tracked .py files have
        unused imports.

        Inside the mock for run_orchestrator we re-check
        ``changed_py_files_since_uas_wip`` against the actual workspace
        — this is the same scoping signal the real orchestrator
        subprocess uses for its lint pre-check, so the assertion
        guarantees that the orchestrator path that depends on the
        Section 6 helper would behave correctly here.
        """
        ws = workspace_with_pre_existing_unused_imports

        def _fake_run_orchestrator(task, *args, **kwargs):
            # The orchestrator subprocess would compute this and
            # find no .py files to lint (because the verifier never
            # touches the workspace).
            scoped = changed_py_files_since_uas_wip(ws)
            assert scoped == [], (
                f"changed_py_files_since_uas_wip leaked pre-existing "
                f"files: {scoped!r}"
            )
            return {
                "exit_code": 0,
                "stdout": "",
                "stderr": (
                    "Sandbox verified.\n"
                    "===STDOUT_START===\n"
                    "VERIFICATION PASSED\n"
                    "===STDOUT_END===\n"
                    "Exit code: 0\n"
                ),
            }

        mock_orch.side_effect = _fake_run_orchestrator

        step = {
            "id": 1,
            "verify": "config.py exists",
            "files_written": [],
            "output": "",
        }
        result = verify_step_output(step, ws)
        assert result is None, (
            f"verify_step_output failed for workspace with pre-existing "
            f"unused-import files: {result!r}. This is the Section 6 "
            f"regression — before the fix this surfaced as "
            f"'Step 1 FAILED. Error: VERIFICATION PASSED'."
        )


class TestValidateWorkspace:
    def test_empty_workspace(self, tmp_path):
        state = {"goal": "test", "steps": []}
        result = validate_workspace(state, str(tmp_path))
        assert result["workspace_empty"] is True
        assert result["missing_files"] == []
        # validation.md should be written inside .uas_state/
        assert (tmp_path / ".uas_state" / "validation.md").exists()

    @patch("architect.main.validate_workspace_llm", return_value=None)
    @patch("architect.main.check_project_guardrails_llm", return_value=[])
    def test_workspace_with_files(self, _mock_guardrails, _mock_llm, tmp_path):
        (tmp_path / "output.txt").write_text("hello")
        (tmp_path / "data.json").write_text("{}")
        state = {
            "goal": "test",
            "steps": [
                {"status": "completed", "files_written": [str(tmp_path / "output.txt")]},
            ],
        }
        result = validate_workspace(state, str(tmp_path))
        assert result["workspace_empty"] is False
        assert result["missing_files"] == []

    def test_missing_files_detected(self, tmp_path):
        state = {
            "goal": "test",
            "steps": [
                {"status": "completed", "files_written": ["/nonexistent/file.txt"]},
            ],
        }
        result = validate_workspace(state, str(tmp_path))
        assert "/nonexistent/file.txt" in result["missing_files"]

    @patch("architect.main.validate_workspace_llm", return_value=None)
    @patch("architect.main.check_project_guardrails_llm", return_value=[])
    def test_validation_md_content(self, _mock_guardrails, _mock_llm, tmp_path):
        (tmp_path / "result.txt").write_text("data")
        state = {
            "goal": "analyze data",
            "steps": [
                {"status": "completed", "files_written": [str(tmp_path / "result.txt")]},
                {"status": "completed", "files_written": []},
            ],
        }
        validate_workspace(state, str(tmp_path))
        content = (tmp_path / ".uas_state" / "validation.md").read_text()
        assert "analyze data" in content
        assert "2/2" in content
        assert "result.txt" in content

    def test_hidden_files_excluded(self, tmp_path):
        (tmp_path / ".hidden").write_text("secret")
        state = {"goal": "test", "steps": []}
        result = validate_workspace(state, str(tmp_path))
        assert result["workspace_empty"] is True

    def test_validation_md_lists_missing(self, tmp_path):
        state = {
            "goal": "test",
            "steps": [
                {"status": "completed", "files_written": ["ghost.txt"]},
            ],
        }
        validate_workspace(state, str(tmp_path))
        content = (tmp_path / ".uas_state" / "validation.md").read_text()
        assert "Missing Files" in content
        assert "ghost.txt" in content

    def test_write_failure_does_not_crash(self, tmp_path):
        """validate_workspace should not crash if it can't write validation.md."""
        state = {"goal": "test", "steps": []}
        # Use a non-writable path
        result = validate_workspace(state, "/nonexistent/path")
        assert isinstance(result, dict)
