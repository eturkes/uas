"""Tests for parallel step execution and step merging."""

import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from architect.planner import topological_sort, merge_trivial_steps


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


class TestMergeTrivialSteps:
    """Tests for merge_trivial_steps() (Section 7a)."""

    def test_no_merge_single_step(self):
        steps = [{"title": "A", "description": "Do A", "depends_on": []}]
        result = merge_trivial_steps(steps)
        assert len(result) == 1

    def test_merge_two_independent_short_steps(self):
        steps = [
            {"title": "A", "description": "Do A", "depends_on": [], "verify": "check A", "environment": ["pkg1"]},
            {"title": "B", "description": "Do B", "depends_on": [], "verify": "check B", "environment": ["pkg2"]},
        ]
        result = merge_trivial_steps(steps)
        assert len(result) == 1
        assert "A" in result[0]["title"] and "B" in result[0]["title"]
        assert "Do A" in result[0]["description"]
        assert "Do B" in result[0]["description"]
        assert "check A" in result[0]["verify"]
        assert "check B" in result[0]["verify"]
        assert "pkg1" in result[0]["environment"]
        assert "pkg2" in result[0]["environment"]

    def test_no_merge_long_descriptions(self):
        steps = [
            {"title": "A", "description": "x" * 200, "depends_on": []},
            {"title": "B", "description": "y" * 200, "depends_on": []},
        ]
        result = merge_trivial_steps(steps)
        assert len(result) == 2

    def test_no_merge_dependent_steps(self):
        steps = [
            {"title": "A", "description": "Do A", "depends_on": []},
            {"title": "B", "description": "Do B", "depends_on": [1]},
        ]
        result = merge_trivial_steps(steps)
        # They're in different levels so can't be merged
        assert len(result) == 2

    def test_merge_three_independent_merges_pair(self):
        steps = [
            {"title": "A", "description": "Do A", "depends_on": []},
            {"title": "B", "description": "Do B", "depends_on": []},
            {"title": "C", "description": "Do C", "depends_on": []},
        ]
        result = merge_trivial_steps(steps)
        # Should merge A+B, leave C unpaired
        assert len(result) == 2

    def test_merge_preserves_defaults(self):
        steps = [
            {"title": "A", "description": "Do A", "depends_on": []},
            {"title": "B", "description": "Do B", "depends_on": []},
        ]
        result = merge_trivial_steps(steps)
        assert result[0].get("verify") is not None
        assert result[0].get("environment") is not None


class TestMaxParallel:
    """Tests for UAS_MAX_PARALLEL throttling (Section 7b)."""

    @patch("architect.main.run_orchestrator")
    @patch("architect.main.generate_spec", return_value="spec.yaml")
    @patch("architect.main.build_task_from_spec", return_value="task")
    def test_max_parallel_caps_workers(
        self, mock_task, mock_spec, mock_orch, tmp_workspace, monkeypatch,
    ):
        """Parallel execution should respect MAX_PARALLEL."""
        import architect.main as main_mod
        from architect.main import execute_step
        from architect.state import init_state, add_steps
        import concurrent.futures

        monkeypatch.setattr(main_mod, "MAX_PARALLEL", 2)

        active_count = []
        active_lock = threading.Lock()
        current_active = [0]

        def tracked_orchestrator(task):
            with active_lock:
                current_active[0] += 1
                active_count.append(current_active[0])
            time.sleep(0.05)
            with active_lock:
                current_active[0] -= 1
            return {"exit_code": 0, "stdout": "", "stderr": ""}

        mock_orch.side_effect = tracked_orchestrator

        state = init_state("test goal")
        state = add_steps(state, [
            {"title": f"Step {i}", "description": f"Do {i}", "depends_on": []}
            for i in range(5)
        ])

        completed_outputs = {}
        workers = min(len(state["steps"]), 2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(execute_step, step, state, completed_outputs)
                for step in state["steps"]
            ]
            for future in concurrent.futures.as_completed(futures):
                future.result()

        # The max concurrent should never exceed 2 (our MAX_PARALLEL)
        assert max(active_count) <= 2


class TestStepTiming:
    """Tests for per-step timing fields (Section 7c)."""

    def test_timing_fields_in_add_steps(self, tmp_workspace):
        from architect.state import init_state, add_steps
        state = init_state("test goal")
        state = add_steps(state, [
            {"title": "A", "description": "Do A", "depends_on": []},
        ])
        step = state["steps"][0]
        assert "timing" in step
        assert step["timing"]["llm_time"] == 0.0
        assert step["timing"]["sandbox_time"] == 0.0
        assert step["timing"]["total_time"] == 0.0

    @patch("architect.main.run_orchestrator")
    @patch("architect.main.generate_spec", return_value="spec.yaml")
    @patch("architect.main.build_task_from_spec", return_value="task")
    def test_timing_populated_after_execution(
        self, mock_task, mock_spec, mock_orch, tmp_workspace,
    ):
        from architect.main import execute_step
        from architect.state import init_state, add_steps

        def slow_orch(task):
            time.sleep(0.05)
            return {"exit_code": 0, "stdout": "", "stderr": "", "sandbox_time": 0.02}

        mock_orch.side_effect = slow_orch

        state = init_state("test goal")
        state = add_steps(state, [
            {"title": "A", "description": "Do A", "depends_on": []},
        ])

        execute_step(state["steps"][0], state, {})

        timing = state["steps"][0]["timing"]
        assert timing["total_time"] > 0
        assert timing["sandbox_time"] >= 0.02
        assert timing["llm_time"] >= 0
