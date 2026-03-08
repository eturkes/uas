"""Tests for Section 8: Verification Loop and Success Validation."""

import os
from unittest.mock import patch, MagicMock

import pytest

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


class TestValidateWorkspace:
    def test_empty_workspace(self, tmp_path):
        state = {"goal": "test", "steps": []}
        result = validate_workspace(state, str(tmp_path))
        assert result["workspace_empty"] is True
        assert result["missing_files"] == []
        # validation.md should be written inside .state/
        assert (tmp_path / ".state" / "validation.md").exists()

    def test_workspace_with_files(self, tmp_path):
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

    def test_validation_md_content(self, tmp_path):
        (tmp_path / "result.txt").write_text("data")
        state = {
            "goal": "analyze data",
            "steps": [
                {"status": "completed", "files_written": [str(tmp_path / "result.txt")]},
                {"status": "completed", "files_written": []},
            ],
        }
        validate_workspace(state, str(tmp_path))
        content = (tmp_path / ".state" / "validation.md").read_text()
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
        content = (tmp_path / ".state" / "validation.md").read_text()
        assert "Missing Files" in content
        assert "ghost.txt" in content

    def test_write_failure_does_not_crash(self, tmp_path):
        """validate_workspace should not crash if it can't write validation.md."""
        state = {"goal": "test", "steps": []}
        # Use a non-writable path
        result = validate_workspace(state, "/nonexistent/path")
        assert isinstance(result, dict)
