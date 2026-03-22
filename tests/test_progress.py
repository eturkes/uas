"""Tests for progress reporting (Step 4)."""

import sys
import time
from unittest.mock import patch, MagicMock

import pytest

from architect.main import report_progress, print_summary, execute_step
from architect.state import init_state, add_steps


class TestReportProgress:
    def test_output_format(self, capsys):
        step = {"id": 3, "title": "Parse data", "status": "pending"}
        report_progress(step, total=7, completed=2, failed=0, attempt=1)

        captured = capsys.readouterr()
        assert '[3/7] Step 3: "Parse data" (attempt 1, 2 completed, 0 failed)' in captured.err

    def test_output_with_failures(self, capsys):
        step = {"id": 5, "title": "Deploy", "status": "pending"}
        report_progress(step, total=10, completed=3, failed=1, attempt=2)

        captured = capsys.readouterr()
        assert '[5/10] Step 5: "Deploy" (attempt 2, 3 completed, 1 failed)' in captured.err

    def test_first_step(self, capsys):
        step = {"id": 1, "title": "Init", "status": "pending"}
        report_progress(step, total=3, completed=0, failed=0)

        captured = capsys.readouterr()
        assert '[1/3] Step 1: "Init" (attempt 1, 0 completed, 0 failed)' in captured.err


class TestPrintSummary:
    def test_summary_table(self, tmp_workspace, capsys):
        state = init_state("test goal")
        state = add_steps(state, [
            {"title": "Step A", "description": "Do A", "depends_on": []},
            {"title": "Step B", "description": "Do B", "depends_on": [1]},
        ])
        state["steps"][0]["status"] = "completed"
        state["steps"][0]["elapsed"] = 12.3
        state["steps"][1]["status"] = "completed"
        state["steps"][1]["elapsed"] = 5.7
        state["total_elapsed"] = 18.0

        print_summary(state)

        captured = capsys.readouterr()
        assert "Step A" in captured.err
        assert "Step B" in captured.err
        assert "completed" in captured.err
        assert "12.3s" in captured.err
        assert "5.7s" in captured.err
        assert "18.0s" in captured.err
        assert "TOTAL" in captured.err

    def test_summary_with_failed_step(self, tmp_workspace, capsys):
        state = init_state("test goal")
        state = add_steps(state, [
            {"title": "Step A", "description": "Do A", "depends_on": []},
            {"title": "Step B", "description": "Do B", "depends_on": []},
        ])
        state["steps"][0]["status"] = "completed"
        state["steps"][0]["elapsed"] = 10.0
        state["steps"][1]["status"] = "failed"
        state["steps"][1]["elapsed"] = 3.5
        state["total_elapsed"] = 13.5

        print_summary(state)

        captured = capsys.readouterr()
        assert "completed" in captured.err
        assert "failed" in captured.err
        assert "13.5s" in captured.err

    def test_summary_no_elapsed(self, tmp_workspace, capsys):
        """Steps without elapsed time should default to 0."""
        state = init_state("test goal")
        state = add_steps(state, [
            {"title": "Step A", "description": "Do A", "depends_on": []},
        ])

        print_summary(state)

        captured = capsys.readouterr()
        assert "0.0s" in captured.err


class TestElapsedTimeTracking:
    @patch("architect.main.run_orchestrator")
    @patch("architect.main.generate_spec")
    @patch("architect.main.build_task_from_spec")
    def test_elapsed_time_recorded_on_success(
        self, mock_build_task, mock_gen_spec, mock_run_orch, tmp_workspace
    ):
        mock_gen_spec.return_value = "/tmp/spec.md"
        mock_build_task.return_value = "task text"
        mock_run_orch.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}

        state = init_state("test")
        state = add_steps(state, [
            {"title": "Fast step", "description": "Quick", "depends_on": []},
        ])
        step = state["steps"][0]

        success = execute_step(step, state, {})

        assert success is True
        assert "elapsed" in step
        assert step["elapsed"] >= 0.0

    @patch("architect.main.should_continue_retrying", return_value=(False, "test stop"))
    @patch("architect.main.decompose_failing_step")
    @patch("architect.main.reflect_and_rewrite")
    @patch("architect.main.run_orchestrator")
    @patch("architect.main.generate_spec")
    @patch("architect.main.build_task_from_spec")
    def test_elapsed_time_recorded_on_failure(
        self, mock_build_task, mock_gen_spec, mock_run_orch,
        mock_reflect, mock_decompose, mock_should_retry, tmp_workspace,
    ):
        mock_gen_spec.return_value = "/tmp/spec.md"
        mock_build_task.return_value = "task text"
        mock_run_orch.return_value = {
            "exit_code": 1, "stdout": "err", "stderr": "fail",
        }
        mock_reflect.return_value = "rewritten task"
        mock_decompose.return_value = "decomposed task"

        state = init_state("test")
        state = add_steps(state, [
            {"title": "Bad step", "description": "Fails", "depends_on": []},
        ])
        step = state["steps"][0]

        success = execute_step(step, state, {})

        assert success is False
        assert "elapsed" in step
        assert step["elapsed"] >= 0.0

    @patch("architect.main.run_orchestrator")
    @patch("architect.main.generate_spec")
    @patch("architect.main.build_task_from_spec")
    def test_progress_counts_passed(
        self, mock_build_task, mock_gen_spec, mock_run_orch, tmp_workspace, capsys,
    ):
        mock_gen_spec.return_value = "/tmp/spec.md"
        mock_build_task.return_value = "task text"
        mock_run_orch.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}

        state = init_state("test")
        state = add_steps(state, [
            {"title": "My step", "description": "Do it", "depends_on": []},
        ])
        step = state["steps"][0]
        counts = {"completed": 4, "failed": 1}

        execute_step(step, state, {}, progress_counts=counts)

        captured = capsys.readouterr()
        assert "4 completed" in captured.err
        assert "1 failed" in captured.err
