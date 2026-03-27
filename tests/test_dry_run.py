"""Tests for dry-run mode (Step 3)."""

import sys
from unittest.mock import patch, MagicMock

import pytest

from architect.main import parse_args, print_plan, main
from architect.state import init_state, add_steps


class TestDryRunFlag:
    def test_dry_run_flag_parsed(self):
        with patch("sys.argv", ["prog", "--dry-run", "test goal"]):
            args = parse_args()
            assert args.dry_run is True

    def test_dry_run_flag_default_false(self):
        with patch("sys.argv", ["prog", "test goal"]):
            args = parse_args()
            assert args.dry_run is False

    def test_dry_run_env_var(self, monkeypatch):
        monkeypatch.setenv("UAS_DRY_RUN", "1")
        with patch("sys.argv", ["prog", "test goal"]):
            args = parse_args()
            # Flag itself is False, but main() checks env var too
            assert args.dry_run is False


class TestPrintPlan:
    def test_print_plan_output(self, tmp_workspace, capsys):
        state = init_state("build a website")
        state = add_steps(state, [
            {"title": "Setup project", "description": "Init the project", "depends_on": []},
            {"title": "Add pages", "description": "Create HTML pages", "depends_on": [1]},
            {"title": "Add styles", "description": "Create CSS files", "depends_on": [1]},
            {"title": "Deploy", "description": "Deploy to server", "depends_on": [2, 3]},
        ])

        print_plan(state)

        captured = capsys.readouterr()
        stderr = captured.err

        assert "Goal: build a website" in stderr
        assert "Steps: 4" in stderr
        assert "Execution levels: 3" in stderr
        assert "Level 1" in stderr
        assert "Level 2" in stderr
        assert "Level 3" in stderr
        assert "Step 1: Setup project" in stderr
        assert "Step 2: Add pages" in stderr
        assert "[depends on: [1]]" in stderr
        assert "Step 4: Deploy" in stderr
        assert "[depends on: [2, 3]]" in stderr

    def test_print_plan_single_step(self, tmp_workspace, capsys):
        state = init_state("simple task")
        state = add_steps(state, [
            {"title": "Do it", "description": "Just do the thing", "depends_on": []},
        ])

        print_plan(state)

        captured = capsys.readouterr()
        stderr = captured.err

        assert "Steps: 1" in stderr
        assert "Execution levels: 1" in stderr
        assert "Step 1: Do it" in stderr


class TestDryRunMode:
    @patch("architect.main.insert_integration_checkpoints", side_effect=lambda s: s)
    @patch("architect.main.split_coupled_steps", side_effect=lambda s: s)
    @patch("architect.main.enforce_minimum_steps", side_effect=lambda g, s, c: s)
    @patch("architect.main.ensure_coverage", side_effect=lambda g, s: (s, []))
    @patch("architect.main.generate_project_spec", return_value="")
    @patch("architect.main.critique_and_refine_plan", side_effect=lambda g, s: s)
    @patch("architect.main.merge_steps_with_llm", side_effect=lambda g, s: s)
    @patch("architect.main.research_goal", return_value="")
    @patch("architect.main.estimate_complexity", return_value="simple")
    @patch("architect.main.decompose_goal_with_voting")
    def test_dry_run_skips_executor(self, mock_decompose, mock_complexity,
                                    mock_research, mock_merge, mock_critique,
                                    mock_gen_spec, mock_coverage,
                                    mock_enforce, mock_split, mock_checkpoints,
                                    tmp_workspace, monkeypatch):
        """Dry-run should decompose but not call run_orchestrator."""
        mock_decompose.return_value = [
            {"title": "Step A", "description": "Do A", "depends_on": []},
            {"title": "Step B", "description": "Do B", "depends_on": [1]},
        ]

        monkeypatch.setattr("sys.argv", ["prog", "--dry-run", "test goal"])

        with patch("architect.main.run_orchestrator") as mock_orch, \
             pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 0
        mock_decompose.assert_called_once()
        mock_orch.assert_not_called()

    @patch("architect.main.insert_integration_checkpoints", side_effect=lambda s: s)
    @patch("architect.main.split_coupled_steps", side_effect=lambda s: s)
    @patch("architect.main.enforce_minimum_steps", side_effect=lambda g, s, c: s)
    @patch("architect.main.ensure_coverage", side_effect=lambda g, s: (s, []))
    @patch("architect.main.generate_project_spec", return_value="")
    @patch("architect.main.research_goal", return_value="")
    @patch("architect.main.estimate_complexity", return_value="simple")
    @patch("architect.main.decompose_goal_with_voting")
    def test_dry_run_env_var_skips_executor(self, mock_decompose, mock_complexity,
                                            mock_research, mock_gen_spec,
                                            mock_coverage,
                                            mock_enforce, mock_split,
                                            mock_checkpoints,
                                            tmp_workspace, monkeypatch):
        """UAS_DRY_RUN=1 should also trigger dry-run mode."""
        mock_decompose.return_value = [
            {"title": "Step A", "description": "Do A", "depends_on": []},
        ]

        monkeypatch.setenv("UAS_DRY_RUN", "true")
        monkeypatch.setattr("sys.argv", ["prog", "test goal"])

        with patch("architect.main.run_orchestrator") as mock_orch, \
             pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 0
        mock_orch.assert_not_called()
