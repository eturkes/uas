"""Tests for architect.explain module."""

import json
import os

from architect.explain import (
    RunExplainer,
    classify_failure,
    compute_critical_path,
    load_run_data,
    _time_breakdown,
    _rewrite_effectiveness,
    _context_influence,
)


def _fixture_state():
    return {
        "goal": "Build a data pipeline",
        "status": "completed",
        "total_elapsed": 60.0,
        "steps": [
            {
                "id": 1,
                "title": "Download data",
                "description": "Download CSV from URL",
                "depends_on": [],
                "status": "completed",
                "elapsed": 15.0,
                "timing": {"llm_time": 8.0, "sandbox_time": 5.0, "total_time": 15.0},
                "output": "Downloaded 100 rows",
                "error": "",
                "rewrites": 0,
                "files_written": ["data.csv"],
            },
            {
                "id": 2,
                "title": "Process data",
                "description": "Clean and transform the data",
                "depends_on": [1],
                "status": "completed",
                "elapsed": 25.0,
                "timing": {"llm_time": 15.0, "sandbox_time": 8.0, "total_time": 25.0},
                "output": "Processed 95 rows",
                "error": "",
                "rewrites": 1,
                "files_written": ["clean.csv"],
            },
            {
                "id": 3,
                "title": "Generate report",
                "description": "Create summary stats",
                "depends_on": [2],
                "status": "failed",
                "elapsed": 20.0,
                "timing": {"llm_time": 12.0, "sandbox_time": 6.0, "total_time": 20.0},
                "output": "",
                "error": "ImportError: No module named pandas",
                "rewrites": 2,
                "files_written": [],
            },
        ],
    }


def _fixture_state_parallel():
    """State with parallel steps to test critical path."""
    return {
        "goal": "Parallel task",
        "status": "completed",
        "total_elapsed": 30.0,
        "steps": [
            {
                "id": 1,
                "title": "Step A",
                "depends_on": [],
                "status": "completed",
                "elapsed": 10.0,
                "timing": {"llm_time": 5.0, "sandbox_time": 3.0},
                "error": "",
                "rewrites": 0,
                "files_written": ["a.txt"],
            },
            {
                "id": 2,
                "title": "Step B",
                "depends_on": [],
                "status": "completed",
                "elapsed": 5.0,
                "timing": {"llm_time": 3.0, "sandbox_time": 1.0},
                "error": "",
                "rewrites": 0,
                "files_written": ["b.txt"],
            },
            {
                "id": 3,
                "title": "Step C",
                "depends_on": [1, 2],
                "status": "completed",
                "elapsed": 20.0,
                "timing": {"llm_time": 12.0, "sandbox_time": 6.0},
                "error": "",
                "rewrites": 0,
                "files_written": ["c.txt"],
            },
        ],
    }


def _fixture_events():
    return [
        {"timestamp": "2024-01-01T00:00:00Z", "event_type": "goal_received", "data": {"goal": "test"}},
        {"timestamp": "2024-01-01T00:00:01Z", "event_type": "step_start", "step_id": 1},
        {"timestamp": "2024-01-01T00:00:10Z", "event_type": "step_complete", "step_id": 1},
    ]


def _fixture_provenance():
    return {
        "nodes": {
            "abc": {"id": "abc", "node_type": "entity", "label": "goal"},
        },
        "edges": [],
    }


def _fixture_code_versions():
    return {
        2: [
            {
                "step_id": 2, "spec_attempt": 0, "orch_attempt": 0,
                "code": "import pandas\ndf = pandas.read_csv('data.csv')\n",
                "prompt_hash": "", "exit_code": 1,
                "error_summary": "ImportError: No module named pandas",
                "timestamp": "2024-01-01T00:00:00Z",
            },
            {
                "step_id": 2, "spec_attempt": 0, "orch_attempt": 1,
                "code": "import subprocess\nsubprocess.run(['pip', 'install', 'pandas'])\nimport pandas\ndf = pandas.read_csv('data.csv')\n",
                "prompt_hash": "", "exit_code": 0,
                "error_summary": "",
                "timestamp": "2024-01-01T00:00:05Z",
            },
        ],
        3: [
            {
                "step_id": 3, "spec_attempt": 0, "orch_attempt": 0,
                "code": "import pandas\n",
                "prompt_hash": "", "exit_code": 1,
                "error_summary": "ImportError: No module named pandas",
                "timestamp": "2024-01-01T00:00:10Z",
            },
            {
                "step_id": 3, "spec_attempt": 1, "orch_attempt": 0,
                "code": "import pandas\nprint('hello')\n",
                "prompt_hash": "", "exit_code": 1,
                "error_summary": "ImportError: No module named pandas",
                "timestamp": "2024-01-01T00:00:15Z",
            },
            {
                "step_id": 3, "spec_attempt": 2, "orch_attempt": 0,
                "code": "import pandas\nprint('world')\n",
                "prompt_hash": "", "exit_code": 1,
                "error_summary": "ImportError: No module named pandas",
                "timestamp": "2024-01-01T00:00:20Z",
            },
        ],
    }


class TestClassifyFailure:
    def test_dependency_error(self):
        assert classify_failure("ImportError: No module named foo") == "dependency_error"

    def test_logic_error(self):
        assert classify_failure("TypeError: unsupported operand") == "logic_error"

    def test_network_error(self):
        assert classify_failure("ConnectionError: Connection refused") == "network_error"

    def test_environment_error(self):
        assert classify_failure("PermissionError: /root/file") == "environment_error"

    def test_timeout(self):
        assert classify_failure("Operation timed out after 30s") == "timeout"

    def test_format_error(self):
        assert classify_failure("JSONDecodeError: Expecting value") == "format_error"

    def test_unknown(self):
        assert classify_failure("something totally random happened") == "unknown"

    def test_empty_string(self):
        assert classify_failure("") == "unknown"

    def test_multiple_matches_picks_highest(self):
        # Contains both dependency and logic keywords
        result = classify_failure("ImportError: No module named foo TypeError")
        assert result in ("dependency_error", "logic_error")

    def test_with_step_context_uses_reflection(self):
        step = {
            "reflections": [
                {"error_type": "network_error", "root_cause": "DNS failure"},
            ],
        }
        # Even though the text matches dependency_error, the reflection wins
        result = classify_failure("ImportError: No module named foo", step_context=step)
        assert result == "network_error"

    def test_with_step_context_uses_latest_reflection(self):
        step = {
            "reflections": [
                {"error_type": "dependency_error", "root_cause": "old"},
                {"error_type": "logic_error", "root_cause": "latest"},
            ],
        }
        result = classify_failure("some error", step_context=step)
        assert result == "logic_error"

    def test_with_step_context_invalid_error_type_falls_back(self):
        step = {
            "reflections": [
                {"error_type": "not_a_real_type", "root_cause": "something"},
            ],
        }
        result = classify_failure("ImportError: No module named foo", step_context=step)
        assert result == "dependency_error"  # Falls back to heuristic

    def test_with_step_context_no_reflections_falls_back(self):
        step = {}
        result = classify_failure("ConnectionError: Connection refused", step_context=step)
        assert result == "network_error"  # Falls back to heuristic

    def test_with_step_context_empty_reflections_falls_back(self):
        step = {"reflections": []}
        result = classify_failure("PermissionError: denied", step_context=step)
        assert result == "environment_error"  # Falls back to heuristic



class TestComputeCriticalPath:
    def test_linear_chain(self):
        steps = [
            {"id": 1, "depends_on": [], "elapsed": 10.0},
            {"id": 2, "depends_on": [1], "elapsed": 20.0},
            {"id": 3, "depends_on": [2], "elapsed": 5.0},
        ]
        path = compute_critical_path(steps)
        assert path == [1, 2, 3]

    def test_parallel_steps_picks_longest(self):
        state = _fixture_state_parallel()
        path = compute_critical_path(state["steps"])
        # Critical path should go through Step 1 (10s) -> Step 3 (20s) = 30s
        # Not Step 2 (5s) -> Step 3 (20s) = 25s
        assert path == [1, 3]

    def test_single_step(self):
        steps = [{"id": 1, "depends_on": [], "elapsed": 5.0}]
        path = compute_critical_path(steps)
        assert path == [1]

    def test_empty_steps(self):
        assert compute_critical_path([]) == []

    def test_no_elapsed(self):
        steps = [{"id": 1, "depends_on": []}]
        path = compute_critical_path(steps)
        assert path == [1]


class TestTimeBreakdown:
    def test_computes_breakdown(self):
        state = _fixture_state()
        tb = _time_breakdown(state["steps"])
        assert tb["llm_time"] == 35.0  # 8 + 15 + 12
        assert tb["sandbox_time"] == 19.0  # 5 + 8 + 6
        assert tb["total_elapsed"] == 60.0  # 15 + 25 + 20
        assert tb["overhead"] == 6.0  # 60 - 35 - 19

    def test_empty_steps(self):
        tb = _time_breakdown([])
        assert tb["llm_time"] == 0.0
        assert tb["total_elapsed"] == 0.0


class TestRewriteEffectiveness:
    def test_effective_rewrite(self):
        cv = _fixture_code_versions()
        eff = _rewrite_effectiveness(cv)
        assert 2 in eff
        assert eff[2]["final_success"] is True
        assert eff[2]["verdict"] == "effective"

    def test_ineffective_rewrite(self):
        cv = _fixture_code_versions()
        eff = _rewrite_effectiveness(cv)
        assert 3 in eff
        assert eff[3]["final_success"] is False
        assert eff[3]["verdict"] == "ineffective"

    def test_single_version_skipped(self):
        cv = {1: [{"code": "x", "exit_code": 0}]}
        eff = _rewrite_effectiveness(cv)
        assert 1 not in eff

    def test_empty_versions(self):
        eff = _rewrite_effectiveness({})
        assert eff == {}


class TestContextInfluence:
    def test_detects_reference(self):
        steps = [
            {"id": 1, "depends_on": [], "files_written": ["data.csv"]},
            {"id": 2, "depends_on": [1], "files_written": []},
        ]
        cv = {
            2: [{"code": "df = read_csv('data.csv')"}],
        }
        ci = _context_influence(steps, cv)
        assert 2 in ci
        assert 1 in ci[2]["referenced_deps"]

    def test_no_reference(self):
        steps = [
            {"id": 1, "depends_on": [], "files_written": ["data.csv"]},
            {"id": 2, "depends_on": [1], "files_written": []},
        ]
        cv = {
            2: [{"code": "print('hello')"}],
        }
        ci = _context_influence(steps, cv)
        assert 2 in ci
        assert 1 in ci[2]["unreferenced_deps"]

    def test_no_deps(self):
        steps = [{"id": 1, "depends_on": [], "files_written": []}]
        ci = _context_influence(steps, {})
        assert ci == {}


class TestRunExplainer:
    def _make_explainer(self, state=None, code_versions=None):
        return RunExplainer(
            state=state or _fixture_state(),
            events=_fixture_events(),
            provenance=_fixture_provenance(),
            code_versions=code_versions or _fixture_code_versions(),
        )

    def test_explain_run_contains_goal(self):
        ex = self._make_explainer()
        text = ex.explain_run()
        assert "Build a data pipeline" in text

    def test_explain_run_contains_status(self):
        ex = self._make_explainer()
        text = ex.explain_run()
        assert "completed" in text

    def test_explain_run_contains_time_breakdown(self):
        ex = self._make_explainer()
        text = ex.explain_run()
        assert "Time Breakdown" in text
        assert "LLM" in text

    def test_explain_run_contains_critical_path(self):
        ex = self._make_explainer()
        text = ex.explain_run()
        assert "Critical Path" in text

    def test_explain_run_contains_failures(self):
        ex = self._make_explainer()
        text = ex.explain_run()
        assert "Failures" in text
        assert "dependency_error" in text

    def test_explain_run_contains_rewrites(self):
        ex = self._make_explainer()
        text = ex.explain_run()
        assert "Rewrites" in text

    def test_explain_step_found(self):
        ex = self._make_explainer()
        text = ex.explain_step(1)
        assert "Download data" in text
        assert "completed" in text

    def test_explain_step_not_found(self):
        ex = self._make_explainer()
        text = ex.explain_step(99)
        assert "not found" in text

    def test_explain_step_with_deps(self):
        ex = self._make_explainer()
        text = ex.explain_step(2)
        assert "Dependencies" in text
        assert "Step 1" in text

    def test_explain_step_on_critical_path(self):
        ex = self._make_explainer()
        text = ex.explain_step(1)
        assert "critical path" in text

    def test_explain_step_failed(self):
        ex = self._make_explainer()
        text = ex.explain_step(3)
        assert "Failure" in text
        assert "ImportError" in text

    def test_explain_failure_not_found(self):
        ex = self._make_explainer()
        text = ex.explain_failure(99)
        assert "not found" in text

    def test_explain_failure_not_failed(self):
        ex = self._make_explainer()
        text = ex.explain_failure(1)
        assert "did not fail" in text

    def test_explain_failure_analysis(self):
        ex = self._make_explainer()
        text = ex.explain_failure(3)
        assert "dependency_error" in text
        assert "Root Cause" in text
        assert "ImportError" in text

    def test_explain_failure_with_rewrite_history(self):
        ex = self._make_explainer()
        text = ex.explain_failure(3)
        assert "Rewrite History" in text
        assert "v0" in text

    def test_explain_critical_path(self):
        ex = self._make_explainer()
        text = ex.explain_critical_path()
        assert "Critical Path" in text
        assert "Step 1" in text

    def test_explain_critical_path_empty(self):
        state = {"goal": "test", "status": "planning", "steps": [], "total_elapsed": 0.0}
        ex = RunExplainer(state, [], {"nodes": {}, "edges": []})
        text = ex.explain_critical_path()
        assert "No critical path" in text

    def test_explain_critical_path_parallel(self):
        state = _fixture_state_parallel()
        ex = RunExplainer(state, [], {"nodes": {}, "edges": []})
        text = ex.explain_critical_path()
        assert "Step 1" in text
        assert "Step 3" in text
        # Step 2 runs in parallel, should be mentioned as non-critical
        assert "parallel" in text.lower()

    def test_explain_cost(self):
        ex = self._make_explainer()
        text = ex.explain_cost()
        assert "Cost Analysis" in text
        assert "Most Expensive" in text
        assert "LLM" in text

    def test_explain_cost_rewrites(self):
        ex = self._make_explainer()
        text = ex.explain_cost()
        assert "Rewrite Cost" in text

    def test_properties(self):
        ex = self._make_explainer()
        assert isinstance(ex.critical_path, list)
        assert isinstance(ex.failure_taxonomy, dict)
        assert isinstance(ex.rewrite_effectiveness, dict)


class TestLoadRunData:
    def test_loads_from_workspace(self, tmp_path):
        state_dir = tmp_path / ".state"
        state_dir.mkdir()

        state = _fixture_state()
        with open(state_dir / "state.json", "w") as f:
            json.dump(state, f)

        events = _fixture_events()
        with open(state_dir / "events.jsonl", "w") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")

        prov = _fixture_provenance()
        with open(state_dir / "provenance.json", "w") as f:
            json.dump(prov, f)

        cv_dir = state_dir / "code_versions"
        cv_dir.mkdir()
        cv = _fixture_code_versions()
        for step_id, versions in cv.items():
            with open(cv_dir / f"{step_id}.json", "w") as f:
                json.dump(versions, f)

        s, e, p, c = load_run_data(str(tmp_path))
        assert s["goal"] == "Build a data pipeline"
        assert len(e) == 3
        assert "abc" in p["nodes"]
        assert 2 in c
        assert 3 in c

    def test_loads_without_optional_files(self, tmp_path):
        state_dir = tmp_path / ".state"
        state_dir.mkdir()

        state = _fixture_state()
        with open(state_dir / "state.json", "w") as f:
            json.dump(state, f)

        s, e, p, c = load_run_data(str(tmp_path))
        assert s["goal"] == "Build a data pipeline"
        assert e == []
        assert p == {"nodes": {}, "edges": []}
        assert c == {}

    def test_raises_on_missing_state(self, tmp_path):
        import pytest
        with pytest.raises(FileNotFoundError):
            load_run_data(str(tmp_path))
