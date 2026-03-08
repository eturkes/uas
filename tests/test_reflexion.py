"""Tests for Section 3: Reflexion-Based Error Recovery.

Covers:
- 3a: Structured reflection memory (generate_reflection, reflection history in rewrites)
- 3b: Error-type-adaptive retry budgets
- 3c: Counterfactual root cause tracing (trace_root_cause)
- 3d: Backtracking support
"""

import json
from unittest.mock import patch, MagicMock

import pytest

from architect.planner import (
    generate_reflection,
    trace_root_cause,
    reflect_and_rewrite,
)
from architect.explain import classify_failure
from architect.state import add_steps, init_state


# ---------------------------------------------------------------------------
# 3a: Structured reflection memory
# ---------------------------------------------------------------------------

class TestGenerateReflection:
    @patch("architect.planner.get_llm_client")
    def test_returns_structured_dict(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = json.dumps({
            "error_type": "dependency_error",
            "root_cause": "pandas not installed",
            "strategy_tried": "import pandas directly",
            "lesson": "always pip install first",
            "what_to_try_next": "add pip install pandas before import",
        })
        mock_get_client.return_value = client

        step = {"description": "process data with pandas"}
        result = generate_reflection(step, "stdout", "ImportError: pandas", attempt=1)

        assert result["attempt"] == 1
        assert result["error_type"] == "dependency_error"
        assert "pandas" in result["root_cause"]
        assert result["lesson"] == "always pip install first"

    @patch("architect.planner.get_llm_client")
    def test_handles_code_fenced_json(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = '```json\n{"error_type": "logic_error", "root_cause": "off by one", "strategy_tried": "loop", "lesson": "check bounds", "what_to_try_next": "fix index"}\n```'
        mock_get_client.return_value = client

        step = {"description": "iterate"}
        result = generate_reflection(step, "", "IndexError", attempt=2)
        assert result["error_type"] == "logic_error"
        assert result["attempt"] == 2

    @patch("architect.planner.get_llm_client")
    def test_fallback_on_unparseable_response(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "This is not JSON at all."
        mock_get_client.return_value = client

        step = {"description": "task"}
        result = generate_reflection(step, "", "some error", attempt=1)
        assert result["error_type"] == "unknown"
        assert result["attempt"] == 1
        assert "This is not JSON" in result["lesson"]

    @patch("architect.planner.get_llm_client")
    def test_fallback_on_llm_exception(self, mock_get_client):
        client = MagicMock()
        client.generate.side_effect = RuntimeError("API down")
        mock_get_client.return_value = client

        step = {"description": "task"}
        result = generate_reflection(step, "", "err", attempt=3)
        assert result["error_type"] == "unknown"
        assert result["attempt"] == 3

    @patch("architect.planner.get_llm_client")
    def test_truncates_long_output(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = json.dumps({
            "error_type": "logic_error",
            "root_cause": "bug",
            "strategy_tried": "x",
            "lesson": "y",
            "what_to_try_next": "z",
        })
        mock_get_client.return_value = client

        step = {"description": "task"}
        long_stdout = "x" * 10000
        generate_reflection(step, long_stdout, "err", attempt=1)
        prompt = client.generate.call_args[0][0]
        # Should be truncated to last 2000 chars
        assert len(prompt) < 10000 + 2000


class TestReflectionHistoryInRewrite:
    @patch("architect.planner.get_llm_client")
    def test_reflections_included_in_prompt(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "improved task"
        mock_get_client.return_value = client

        step = {"description": "do something"}
        reflections = [
            {
                "attempt": 1,
                "error_type": "dependency_error",
                "root_cause": "missing pandas",
                "strategy_tried": "direct import",
                "lesson": "install deps first",
                "what_to_try_next": "pip install",
            },
            {
                "attempt": 2,
                "error_type": "logic_error",
                "root_cause": "wrong column",
                "strategy_tried": "fixed import",
                "lesson": "verify schema",
                "what_to_try_next": "check columns",
            },
        ]
        result = reflect_and_rewrite(
            step, "out", "err", reflections=reflections,
        )
        prompt = client.generate.call_args[0][0]
        assert "<reflection_history>" in prompt
        assert "missing pandas" in prompt
        assert "wrong column" in prompt
        assert "install deps first" in prompt
        assert result == "improved task"

    @patch("architect.planner.get_llm_client")
    def test_no_reflections_no_section(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "rewritten"
        mock_get_client.return_value = client

        step = {"description": "task"}
        reflect_and_rewrite(step, "out", "err", reflections=None)
        prompt = client.generate.call_args[0][0]
        assert "<reflection_history>" not in prompt

    @patch("architect.planner.get_llm_client")
    def test_empty_reflections_no_section(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "rewritten"
        mock_get_client.return_value = client

        step = {"description": "task"}
        reflect_and_rewrite(step, "out", "err", reflections=[])
        prompt = client.generate.call_args[0][0]
        assert "<reflection_history>" not in prompt


class TestReflectionsInState:
    def test_add_steps_includes_reflections_field(self, tmp_workspace):
        state = init_state("goal")
        steps = [{"title": "S", "description": "D"}]
        state = add_steps(state, steps)
        assert "reflections" in state["steps"][0]
        assert state["steps"][0]["reflections"] == []


# ---------------------------------------------------------------------------
# 3b: Error-type-adaptive retry budgets
# ---------------------------------------------------------------------------

class TestErrorRetryBudgets:
    def test_dependency_error_budget(self):
        from architect.main import _ERROR_RETRY_BUDGETS
        assert _ERROR_RETRY_BUDGETS["dependency_error"] == 1

    def test_logic_error_full_budget(self):
        from architect.main import _ERROR_RETRY_BUDGETS, MAX_SPEC_REWRITES
        assert _ERROR_RETRY_BUDGETS["logic_error"] == MAX_SPEC_REWRITES

    def test_timeout_zero_budget(self):
        from architect.main import _ERROR_RETRY_BUDGETS
        assert _ERROR_RETRY_BUDGETS["timeout"] == 0

    def test_network_error_budget(self):
        from architect.main import _ERROR_RETRY_BUDGETS
        assert _ERROR_RETRY_BUDGETS["network_error"] == 2

    def test_environment_error_budget(self):
        from architect.main import _ERROR_RETRY_BUDGETS
        assert _ERROR_RETRY_BUDGETS["environment_error"] == 1

    def test_format_error_budget(self):
        from architect.main import _ERROR_RETRY_BUDGETS
        assert _ERROR_RETRY_BUDGETS["format_error"] == 2

    def test_unknown_full_budget(self):
        from architect.main import _ERROR_RETRY_BUDGETS, MAX_SPEC_REWRITES
        assert _ERROR_RETRY_BUDGETS["unknown"] == MAX_SPEC_REWRITES

    def test_classify_failure_used_for_budget(self):
        """classify_failure is importable from explain and returns valid types."""
        assert classify_failure("ImportError: No module named foo") == "dependency_error"
        assert classify_failure("TimeoutError: timed out") in ("timeout", "network_error")
        assert classify_failure("TypeError: x") == "logic_error"


# ---------------------------------------------------------------------------
# 3c: Counterfactual root cause tracing
# ---------------------------------------------------------------------------

class TestTraceRootCause:
    @patch("architect.planner.get_llm_client")
    def test_returns_self_for_no_deps(self, mock_get_client):
        step = {"description": "task", "depends_on": []}
        target, dep = trace_root_cause(step, "error", {}, {"steps": []})
        assert target == "self"
        assert dep is None
        mock_get_client.assert_not_called()

    @patch("architect.planner.get_llm_client")
    def test_identifies_self(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "SELF"
        mock_get_client.return_value = client

        step = {"description": "process data", "depends_on": [1]}
        state = {"steps": [
            {"id": 1, "title": "Download", "depends_on": []},
            {"id": 2, "title": "Process", "depends_on": [1]},
        ]}
        outputs = {1: {"stdout": "downloaded ok", "files": ["data.csv"]}}

        target, dep = trace_root_cause(step, "KeyError: 'name'", outputs, state)
        assert target == "self"
        assert dep is None

    @patch("architect.planner.get_llm_client")
    def test_identifies_dependency(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "STEP_1"
        mock_get_client.return_value = client

        step = {"description": "process data", "depends_on": [1]}
        state = {"steps": [
            {"id": 1, "title": "Download", "depends_on": []},
            {"id": 2, "title": "Process", "depends_on": [1]},
        ]}
        outputs = {1: {"stdout": "downloaded", "files": ["data.csv"]}}

        target, dep = trace_root_cause(step, "file is empty", outputs, state)
        assert target == "dependency"
        assert dep == 1

    @patch("architect.planner.get_llm_client")
    def test_invalid_dep_id_returns_self(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "STEP_99"
        mock_get_client.return_value = client

        step = {"description": "task", "depends_on": [1]}
        state = {"steps": [
            {"id": 1, "title": "A", "depends_on": []},
        ]}
        outputs = {1: "ok"}

        target, dep = trace_root_cause(step, "error", outputs, state)
        assert target == "self"
        assert dep is None

    @patch("architect.planner.get_llm_client")
    def test_llm_failure_returns_self(self, mock_get_client):
        client = MagicMock()
        client.generate.side_effect = RuntimeError("API down")
        mock_get_client.return_value = client

        step = {"description": "task", "depends_on": [1]}
        state = {"steps": [{"id": 1, "title": "A", "depends_on": []}]}
        outputs = {1: "ok"}

        target, dep = trace_root_cause(step, "error", outputs, state)
        assert target == "self"
        assert dep is None

    @patch("architect.planner.get_llm_client")
    def test_prompt_contains_dependency_info(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "SELF"
        mock_get_client.return_value = client

        step = {"description": "analyze results", "depends_on": [1, 2]}
        state = {"steps": [
            {"id": 1, "title": "Download A", "depends_on": []},
            {"id": 2, "title": "Download B", "depends_on": []},
            {"id": 3, "title": "Analyze", "depends_on": [1, 2]},
        ]}
        outputs = {
            1: {"stdout": "got A", "files": ["a.csv"]},
            2: {"stdout": "got B", "files": ["b.csv"]},
        }

        trace_root_cause(step, "merge failed", outputs, state)
        prompt = client.generate.call_args[0][0]
        assert "Download A" in prompt
        assert "Download B" in prompt
        assert "a.csv" in prompt
        assert "b.csv" in prompt

    @patch("architect.planner.get_llm_client")
    def test_handles_string_output(self, mock_get_client):
        """completed_outputs may contain plain strings instead of dicts."""
        client = MagicMock()
        client.generate.return_value = "SELF"
        mock_get_client.return_value = client

        step = {"description": "task", "depends_on": [1]}
        state = {"steps": [{"id": 1, "title": "A", "depends_on": []}]}
        outputs = {1: "plain string output"}

        target, dep = trace_root_cause(step, "error", outputs, state)
        assert target == "self"


# ---------------------------------------------------------------------------
# 3d: Backtracking support
# ---------------------------------------------------------------------------

class TestBacktrackingSupport:
    def test_backtracked_steps_set_default(self):
        """execute_step should accept backtracked_steps parameter."""
        from architect.main import execute_step
        import inspect
        sig = inspect.signature(execute_step)
        assert "backtracked_steps" in sig.parameters
        assert sig.parameters["backtracked_steps"].default is None
