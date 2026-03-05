"""Tests for parallel step execution (Step 6)."""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from architect.planner import topological_sort


class TestTopologicalSort:
    def test_empty_steps(self):
        assert topological_sort([]) == []

    def test_single_step(self):
        steps = [{"id": 1, "depends_on": []}]
        assert topological_sort(steps) == [[1]]

    def test_all_independent(self):
        steps = [
            {"id": 1, "depends_on": []},
            {"id": 2, "depends_on": []},
            {"id": 3, "depends_on": []},
        ]
        assert topological_sort(steps) == [[1, 2, 3]]

    def test_linear_chain(self):
        steps = [
            {"id": 1, "depends_on": []},
            {"id": 2, "depends_on": [1]},
            {"id": 3, "depends_on": [2]},
        ]
        assert topological_sort(steps) == [[1], [2], [3]]

    def test_diamond_dependency(self):
        steps = [
            {"id": 1, "depends_on": []},
            {"id": 2, "depends_on": [1]},
            {"id": 3, "depends_on": [1]},
            {"id": 4, "depends_on": [2, 3]},
        ]
        assert topological_sort(steps) == [[1], [2, 3], [4]]

    def test_two_independent_chains(self):
        steps = [
            {"id": 1, "depends_on": []},
            {"id": 2, "depends_on": [1]},
            {"id": 3, "depends_on": []},
            {"id": 4, "depends_on": [3]},
        ]
        assert topological_sort(steps) == [[1, 3], [2, 4]]

    def test_complex_dag(self):
        # 1 -> 3, 2 -> 3, 2 -> 4, 3 -> 5, 4 -> 5
        steps = [
            {"id": 1, "depends_on": []},
            {"id": 2, "depends_on": []},
            {"id": 3, "depends_on": [1, 2]},
            {"id": 4, "depends_on": [2]},
            {"id": 5, "depends_on": [3, 4]},
        ]
        levels = topological_sort(steps)
        assert levels == [[1, 2], [3, 4], [5]]

    def test_cycle_detection(self):
        steps = [
            {"id": 1, "depends_on": [2]},
            {"id": 2, "depends_on": [1]},
        ]
        with pytest.raises(ValueError, match="Cycle detected"):
            topological_sort(steps)

    def test_missing_depends_on_defaults_empty(self):
        steps = [{"id": 1}, {"id": 2}]
        assert topological_sort(steps) == [[1, 2]]

    def test_levels_are_sorted(self):
        """Step IDs within each level should be sorted."""
        steps = [
            {"id": 3, "depends_on": []},
            {"id": 1, "depends_on": []},
            {"id": 2, "depends_on": []},
        ]
        levels = topological_sort(steps)
        assert levels == [[1, 2, 3]]


class TestParallelExecution:
    """Test that independent steps execute concurrently."""

    @patch("architect.main.run_orchestrator")
    @patch("architect.main.generate_spec", return_value="spec.yaml")
    @patch("architect.main.build_task_from_spec", return_value="task")
    def test_parallel_steps_run_concurrently(
        self, mock_task, mock_spec, mock_orch, tmp_workspace,
    ):
        """Two independent steps should overlap in execution time."""
        from architect.main import execute_step
        from architect.state import init_state, add_steps, save_state
        import concurrent.futures

        execution_log = []
        log_lock = threading.Lock()

        def slow_orchestrator(task):
            tid = threading.current_thread().ident
            with log_lock:
                execution_log.append(("start", tid, time.monotonic()))
            time.sleep(0.1)
            with log_lock:
                execution_log.append(("end", tid, time.monotonic()))
            return {"exit_code": 0, "stdout": "", "stderr": ""}

        mock_orch.side_effect = slow_orchestrator

        state = init_state("test goal")
        state = add_steps(state, [
            {"title": "A", "description": "Do A", "depends_on": []},
            {"title": "B", "description": "Do B", "depends_on": []},
        ])

        completed_outputs = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                executor.submit(
                    execute_step, step, state, completed_outputs,
                ): step
                for step in state["steps"]
            }
            for future in concurrent.futures.as_completed(futures):
                future.result()

        # Both steps should have started before either finished
        starts = [t for label, _, t in execution_log if label == "start"]
        ends = [t for label, _, t in execution_log if label == "end"]
        assert len(starts) == 2
        assert len(ends) == 2
        # The second start should happen before the first end
        assert sorted(starts)[1] < sorted(ends)[0]

    @patch("architect.main.run_orchestrator")
    @patch("architect.main.generate_spec", return_value="spec.yaml")
    @patch("architect.main.build_task_from_spec", return_value="task")
    def test_dependent_steps_not_parallel(
        self, mock_task, mock_spec, mock_orch, tmp_workspace,
    ):
        """Steps with dependencies should be in different levels."""
        from architect.state import init_state, add_steps

        state = init_state("test goal")
        state = add_steps(state, [
            {"title": "A", "description": "Do A", "depends_on": []},
            {"title": "B", "description": "Do B", "depends_on": [1]},
        ])

        levels = topological_sort(state["steps"])
        assert levels == [[1], [2]]

    @patch("architect.main.run_orchestrator")
    @patch("architect.main.generate_spec", return_value="spec.yaml")
    @patch("architect.main.build_task_from_spec", return_value="task")
    def test_state_save_threadsafe(
        self, mock_task, mock_spec, mock_orch, tmp_workspace,
    ):
        """Concurrent execute_step calls should not corrupt state file."""
        from architect.main import execute_step
        from architect.state import init_state, add_steps, load_state
        import concurrent.futures

        mock_orch.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}

        state = init_state("test goal")
        state = add_steps(state, [
            {"title": "A", "description": "Do A", "depends_on": []},
            {"title": "B", "description": "Do B", "depends_on": []},
            {"title": "C", "description": "Do C", "depends_on": []},
        ])

        completed_outputs = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(execute_step, step, state, completed_outputs)
                for step in state["steps"]
            ]
            for future in concurrent.futures.as_completed(futures):
                future.result()

        # State file should be valid JSON and reflect all completions
        saved = load_state()
        assert saved is not None
        for step in saved["steps"]:
            assert step["status"] == "completed"
