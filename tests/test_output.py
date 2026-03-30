"""Tests for structured JSON output (Step 7)."""

import json
import os
from unittest.mock import patch

import pytest

from architect.main import parse_args, write_json_output, main
from architect.state import init_state, add_steps


class TestOutputFlag:
    def test_output_flag_parsed(self):
        with patch("sys.argv", ["prog", "-o", "results.json", "test goal"]):
            args = parse_args()
            assert args.output == "results.json"

    def test_output_long_flag_parsed(self):
        with patch("sys.argv", ["prog", "--output", "out.json", "test goal"]):
            args = parse_args()
            assert args.output == "out.json"

    def test_output_flag_auto_mode(self):
        with patch("sys.argv", ["prog", "-o", "--", "test goal"]):
            args = parse_args()
            assert args.output == "auto"

    def test_output_flag_default_none(self):
        with patch("sys.argv", ["prog", "test goal"]):
            args = parse_args()
            assert args.output is None


class TestWriteJsonOutput:
    def test_creates_json_file(self, tmp_workspace):
        state = init_state("build something")
        state = add_steps(state, [
            {"title": "Step A", "description": "Do A", "depends_on": []},
            {"title": "Step B", "description": "Do B", "depends_on": [1]},
        ])
        state["steps"][0]["status"] = "completed"
        state["steps"][0]["elapsed"] = 5.0
        state["steps"][1]["status"] = "completed"
        state["steps"][1]["elapsed"] = 3.0
        state["status"] = "completed"
        state["total_elapsed"] = 8.0

        output_path = os.path.join(str(tmp_workspace), "results.json")
        write_json_output(state, output_path)

        assert os.path.exists(output_path)
        with open(output_path) as f:
            data = json.load(f)

        assert data["goal"] == "build something"
        assert data["status"] == "completed"
        assert data["total_elapsed"] == 8.0
        assert len(data["steps"]) == 2
        assert data["steps"][0]["id"] == 1
        assert data["steps"][0]["title"] == "Step A"
        assert data["steps"][0]["status"] == "completed"
        assert data["steps"][0]["elapsed"] == 5.0
        assert data["steps"][1]["id"] == 2

    def test_output_on_failure(self, tmp_workspace):
        state = init_state("failing goal")
        state = add_steps(state, [
            {"title": "Step A", "description": "Do A", "depends_on": []},
        ])
        state["steps"][0]["status"] = "failed"
        state["steps"][0]["elapsed"] = 2.5
        state["status"] = "blocked"
        state["total_elapsed"] = 2.5

        output_path = os.path.join(str(tmp_workspace), "results.json")
        write_json_output(state, output_path)

        with open(output_path) as f:
            data = json.load(f)

        assert data["status"] == "blocked"
        assert data["steps"][0]["status"] == "failed"

    def test_output_creates_parent_dirs(self, tmp_workspace):
        state = init_state("test")
        state["status"] = "completed"
        state["total_elapsed"] = 0.0

        output_path = os.path.join(str(tmp_workspace), "sub", "dir", "out.json")
        write_json_output(state, output_path)

        assert os.path.exists(output_path)

    def test_output_missing_elapsed_defaults(self, tmp_workspace):
        state = init_state("test")
        state = add_steps(state, [
            {"title": "Step A", "description": "Do A", "depends_on": []},
        ])
        state["status"] = "completed"

        output_path = os.path.join(str(tmp_workspace), "results.json")
        write_json_output(state, output_path)

        with open(output_path) as f:
            data = json.load(f)

        assert data["steps"][0]["elapsed"] == 0.0
        assert data["total_elapsed"] == 0.0


class TestOutputIntegration:
    @patch("architect.main.insert_integration_checkpoints", side_effect=lambda s: s)
    @patch("architect.main.split_coupled_steps", side_effect=lambda s: s)
    @patch("architect.main.enforce_minimum_steps", side_effect=lambda g, s, c: s)
    @patch("architect.main.ensure_coverage", side_effect=lambda g, s: (s, []))
    @patch("architect.main.generate_project_spec", return_value="")
    @patch("architect.main.post_run_meta_learning", return_value=None)
    @patch("architect.main.research_goal", return_value="")
    @patch("architect.main.estimate_complexity", return_value="simple")
    @patch("architect.main.decompose_goal_with_voting")
    @patch("architect.main.run_orchestrator")
    @patch("architect.main.generate_spec")
    @patch("architect.main.build_task_from_spec")
    def test_output_written_on_success(
        self, mock_build_task, mock_gen_spec, mock_run_orch, mock_decompose,
        mock_complexity, mock_research, mock_meta, mock_proj_spec, mock_coverage,
        mock_enforce, mock_split, mock_checkpoints,
        tmp_workspace, monkeypatch,
    ):
        mock_decompose.return_value = [
            {"title": "Step A", "description": "Do A", "depends_on": []},
        ]
        mock_gen_spec.return_value = "/tmp/spec.md"
        mock_build_task.return_value = "task text"
        mock_run_orch.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}

        output_path = os.path.join(str(tmp_workspace), "out.json")
        monkeypatch.setattr("sys.argv", ["prog", "-o", output_path, "test goal"])

        main()

        assert os.path.exists(output_path)
        with open(output_path) as f:
            data = json.load(f)
        assert data["status"] == "completed"
        assert len(data["steps"]) >= 1
        assert data["steps"][0]["status"] == "completed"

    @patch("architect.main.insert_integration_checkpoints", side_effect=lambda s: s)
    @patch("architect.main.split_coupled_steps", side_effect=lambda s: s)
    @patch("architect.main.enforce_minimum_steps", side_effect=lambda g, s, c: s)
    @patch("architect.main.ensure_coverage", side_effect=lambda g, s: (s, []))
    @patch("architect.main.generate_project_spec", return_value="")
    @patch("architect.main.research_goal", return_value="")
    @patch("architect.main.estimate_complexity", return_value="simple")
    @patch("architect.main.decompose_goal_with_voting")
    @patch("architect.main.run_orchestrator")
    @patch("architect.main.generate_spec")
    @patch("architect.main.build_task_from_spec")
    @patch("architect.main.decompose_failing_step")
    @patch("architect.main.reflect_and_rewrite")
    @patch("architect.main.generate_reflection")
    def test_output_written_on_blocked(
        self, mock_gen_refl, mock_reflect, mock_decompose_step,
        mock_build_task, mock_gen_spec,
        mock_run_orch, mock_decompose, mock_complexity, mock_research,
        mock_proj_spec, mock_coverage, mock_enforce, mock_split, mock_checkpoints,
        tmp_workspace, monkeypatch,
    ):
        mock_decompose.return_value = [
            {"title": "Step A", "description": "Do A", "depends_on": []},
        ]
        mock_gen_spec.return_value = "/tmp/spec.md"
        mock_build_task.return_value = "task text"
        mock_run_orch.return_value = {
            "exit_code": 1, "stdout": "err", "stderr": "fail",
        }
        mock_reflect.return_value = "rewritten task"
        mock_decompose_step.return_value = "decomposed task"
        mock_gen_refl.return_value = {
            "attempt": 1, "error_type": "logic_error",
            "root_cause": "test failure", "strategy_tried": "attempt 1",
            "lesson": "", "what_to_try_next": "",
        }

        import architect.main as main_mod
        monkeypatch.setattr(main_mod, "WORKSPACE", str(tmp_workspace))

        output_path = os.path.join(str(tmp_workspace), "out.json")
        monkeypatch.setattr("sys.argv", ["prog", "-o", output_path, "test goal"])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
        assert os.path.exists(output_path)
        with open(output_path) as f:
            data = json.load(f)
        assert data["status"] == "blocked"
        assert data["steps"][0]["status"] == "failed"

    @patch("architect.main.insert_integration_checkpoints", side_effect=lambda s: s)
    @patch("architect.main.split_coupled_steps", side_effect=lambda s: s)
    @patch("architect.main.enforce_minimum_steps", side_effect=lambda g, s, c: s)
    @patch("architect.main.ensure_coverage", side_effect=lambda g, s: (s, []))
    @patch("architect.main.generate_project_spec", return_value="")
    @patch("architect.main.post_run_meta_learning", return_value=None)
    @patch("architect.main.research_goal", return_value="")
    @patch("architect.main.estimate_complexity", return_value="simple")
    @patch("architect.main.decompose_goal_with_voting")
    @patch("architect.main.run_orchestrator")
    @patch("architect.main.generate_spec")
    @patch("architect.main.build_task_from_spec")
    def test_output_env_var(
        self, mock_build_task, mock_gen_spec, mock_run_orch, mock_decompose,
        mock_complexity, mock_research, mock_meta, mock_proj_spec, mock_coverage,
        mock_enforce, mock_split, mock_checkpoints,
        tmp_workspace, monkeypatch,
    ):
        mock_decompose.return_value = [
            {"title": "Step A", "description": "Do A", "depends_on": []},
        ]
        mock_gen_spec.return_value = "/tmp/spec.md"
        mock_build_task.return_value = "task text"
        mock_run_orch.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}

        output_path = os.path.join(str(tmp_workspace), "env_out.json")
        monkeypatch.setenv("UAS_OUTPUT", output_path)
        monkeypatch.setattr("sys.argv", ["prog", "test goal"])

        main()

        assert os.path.exists(output_path)
        with open(output_path) as f:
            data = json.load(f)
        assert data["status"] == "completed"

    @patch("architect.main.insert_integration_checkpoints", side_effect=lambda s: s)
    @patch("architect.main.split_coupled_steps", side_effect=lambda s: s)
    @patch("architect.main.enforce_minimum_steps", side_effect=lambda g, s, c: s)
    @patch("architect.main.ensure_coverage", side_effect=lambda g, s: (s, []))
    @patch("architect.main.generate_project_spec", return_value="")
    @patch("architect.main.post_run_meta_learning", return_value=None)
    @patch("architect.main.research_goal", return_value="")
    @patch("architect.main.estimate_complexity", return_value="simple")
    @patch("architect.main.decompose_goal_with_voting")
    @patch("architect.main.run_orchestrator")
    @patch("architect.main.generate_spec")
    @patch("architect.main.build_task_from_spec")
    def test_no_output_when_not_requested(
        self, mock_build_task, mock_gen_spec, mock_run_orch, mock_decompose,
        mock_complexity, mock_research, mock_meta, mock_proj_spec, mock_coverage,
        mock_enforce, mock_split, mock_checkpoints,
        tmp_workspace, monkeypatch,
    ):
        mock_decompose.return_value = [
            {"title": "Step A", "description": "Do A", "depends_on": []},
        ]
        mock_gen_spec.return_value = "/tmp/spec.md"
        mock_build_task.return_value = "task text"
        mock_run_orch.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}

        monkeypatch.setattr("sys.argv", ["prog", "test goal"])

        main()

        # No JSON output file should exist in workspace
        json_files = [f for f in os.listdir(str(tmp_workspace))
                      if f.endswith(".json") and f != "state.json"]
        # Only .uas_state dir should exist, no stray JSON files
        assert not any(f.endswith(".json") for f in json_files)
