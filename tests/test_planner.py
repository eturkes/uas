"""Tests for architect.planner: parse_steps_json, prompt content, and critique."""

import json
from unittest.mock import patch, MagicMock, call

import pytest

from architect.planner import (
    parse_steps_json,
    critique_and_refine_plan,
    reflect_and_rewrite,
    decompose_failing_step,
    research_goal,
    decompose_goal,
    decompose_goal_with_voting,
    _is_confused_output,
)


class TestParseStepsJson:
    def test_direct_json_array(self):
        raw = '[{"title": "step1", "description": "do X", "depends_on": []}]'
        result = parse_steps_json(raw)
        assert len(result) == 1
        assert result[0]["title"] == "step1"

    def test_json_in_code_fence(self):
        raw = 'Here are the steps:\n```json\n[{"title": "a", "description": "b"}]\n```'
        result = parse_steps_json(raw)
        assert len(result) == 1
        assert result[0]["title"] == "a"

    def test_json_in_bare_fence(self):
        raw = '```\n[{"title": "a", "description": "b"}]\n```'
        result = parse_steps_json(raw)
        assert len(result) == 1

    def test_bracket_extraction(self):
        raw = 'The steps are: [{"title": "x", "description": "y"}] end.'
        result = parse_steps_json(raw)
        assert len(result) == 1
        assert result[0]["title"] == "x"

    def test_multiple_steps(self):
        raw = '[{"title": "a", "description": "b"}, {"title": "c", "description": "d"}]'
        result = parse_steps_json(raw)
        assert len(result) == 2

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="Could not parse"):
            parse_steps_json("this is not json at all")

    def test_json_object_not_array_raises(self):
        with pytest.raises(ValueError, match="Could not parse"):
            parse_steps_json('{"title": "not an array"}')

    def test_whitespace_padded(self):
        raw = '  \n  [{"title": "a", "description": "b"}]  \n  '
        result = parse_steps_json(raw)
        assert len(result) == 1

    def test_analysis_tags_stripped(self):
        raw = (
            '<analysis>This is a simple task.</analysis>\n'
            '[{"title": "a", "description": "b"}]'
        )
        result = parse_steps_json(raw)
        assert len(result) == 1
        assert result[0]["title"] == "a"

    def test_analysis_tags_multiline_stripped(self):
        raw = (
            '<analysis>\nLine 1\nLine 2\n</analysis>\n'
            '[{"title": "x", "description": "y"}]'
        )
        result = parse_steps_json(raw)
        assert len(result) == 1



class TestCritiqueAndRefinePlan:
    def _make_steps(self, n=2):
        return [
            {"title": f"step{i}", "description": f"do {i}", "depends_on":
             [i - 1] if i > 1 else [], "verify": "", "environment": []}
            for i in range(1, n + 1)
        ]

    @patch("architect.planner.get_llm_client")
    def test_plan_ok_returns_original(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "PLAN_OK"
        mock_get_client.return_value = client

        steps = self._make_steps()
        result = critique_and_refine_plan("test goal", steps)
        assert result is steps

    @patch("architect.planner.get_llm_client")
    def test_refined_steps_returned(self, mock_get_client):
        refined = [
            {"title": "better1", "description": "improved", "depends_on": [],
             "verify": "check it", "environment": []},
        ]
        client = MagicMock()
        client.generate.return_value = json.dumps(refined)
        mock_get_client.return_value = client

        steps = self._make_steps()
        result = critique_and_refine_plan("test goal", steps)
        assert len(result) == 1
        assert result[0]["title"] == "better1"

    @patch("architect.planner.get_llm_client")
    def test_unparseable_response_returns_original(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "I have some thoughts but no JSON"
        mock_get_client.return_value = client

        steps = self._make_steps()
        result = critique_and_refine_plan("test goal", steps)
        assert result is steps

    @patch("architect.planner.get_llm_client")
    def test_llm_exception_returns_original(self, mock_get_client):
        client = MagicMock()
        client.generate.side_effect = RuntimeError("API down")
        mock_get_client.return_value = client

        steps = self._make_steps()
        result = critique_and_refine_plan("test goal", steps)
        assert result is steps


class TestReflectAndRewrite:
    @patch("architect.planner.get_llm_client")
    def test_basic_reflection(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = (
            "<diagnosis>Logic error in parsing</diagnosis>\n"
            "<strategies>1. Use regex. 2. Use json. Pick json.</strategies>\n"
            "Improved task: parse the data using json.loads"
        )
        mock_get_client.return_value = client

        step = {"description": "parse some data"}
        result = reflect_and_rewrite(step, "stdout", "stderr")
        assert "parse the data using json.loads" in result
        assert "<diagnosis>" not in result
        assert "<strategies>" not in result

    @patch("architect.planner.MINIMAL_MODE", True)
    @patch("architect.planner.get_llm_client")
    def test_llm_driven_strategy_menu_in_prompt(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "Use a completely different approach"
        mock_get_client.return_value = client

        step = {"description": "do something"}
        attempts = [
            {"attempt": 1, "error": "import error", "strategy": "attempt 1"},
        ]
        result = reflect_and_rewrite(
            step, "stdout", "stderr", previous_attempts=attempts
        )
        prompt = client.generate.call_args[0][0]
        assert "failed 1 time(s)" in prompt
        assert "fixable bug" in prompt
        assert "completely new approach" in prompt
        assert "defensive fallbacks" in prompt
        assert result == "Use a completely different approach"

    @patch("architect.planner.MINIMAL_MODE", True)
    @patch("architect.planner.get_llm_client")
    def test_multiple_attempts_count(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "Final defensive approach"
        mock_get_client.return_value = client

        step = {"description": "do something"}
        attempts = [
            {"attempt": 1, "error": "err1", "strategy": "attempt 1"},
            {"attempt": 2, "error": "err2", "strategy": "attempt 2"},
            {"attempt": 3, "error": "err3", "strategy": "attempt 3"},
        ]
        result = reflect_and_rewrite(
            step, "stdout", "stderr", previous_attempts=attempts
        )
        prompt = client.generate.call_args[0][0]
        assert "failed 3 time(s)" in prompt
        assert result == "Final defensive approach"

    @patch("architect.planner.MINIMAL_MODE", True)
    @patch("architect.planner.get_llm_client")
    def test_red_flag_excessive_length_resamples(self, mock_get_client):
        client = MagicMock()
        long_response = "x" * 10000
        client.generate.side_effect = [long_response, "fixed task description"]
        mock_get_client.return_value = client

        step = {"description": "short task"}
        result = reflect_and_rewrite(step, "", "error")
        assert client.generate.call_count == 2
        assert result == "fixed task description"

    @patch("architect.planner.MINIMAL_MODE", True)
    @patch("architect.planner.get_llm_client")
    def test_red_flag_error_verbatim_resamples(self, mock_get_client):
        client = MagicMock()
        error_text = "A" * 300
        # First response contains the error verbatim
        client.generate.side_effect = [
            f"some prefix {error_text} some suffix",
            "clean rewrite",
        ]
        mock_get_client.return_value = client

        step = {"description": "a task"}
        result = reflect_and_rewrite(step, "", error_text)
        assert client.generate.call_count == 2
        assert result == "clean rewrite"

    @patch("architect.planner.get_llm_client")
    def test_empty_result_returns_original(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = (
            "<diagnosis>x</diagnosis><strategies>y</strategies>"
        )
        mock_get_client.return_value = client

        step = {"description": "original task"}
        result = reflect_and_rewrite(step, "", "")
        assert result == "original task"

    @patch("architect.planner.MINIMAL_MODE", True)
    @patch("architect.planner.get_llm_client")
    def test_stdout_stderr_trimmed_in_prompt(self, mock_get_client):
        """Long stdout and stderr are trimmed to avoid flooding the prompt."""
        client = MagicMock()
        client.generate.return_value = "rewritten"
        mock_get_client.return_value = client

        step = {"description": "task"}
        long_stdout = "x" * 5000
        long_stderr = "y" * 5000
        reflect_and_rewrite(step, long_stdout, long_stderr)
        prompt = client.generate.call_args[0][0]
        # Outputs are trimmed to the default limit (tail preserved)
        assert "x" * 3000 in prompt
        assert "y" * 3000 in prompt
        assert "x" * 5000 not in prompt
        assert "y" * 5000 not in prompt

    @patch("architect.planner.MINIMAL_MODE", True)
    @patch("architect.planner.get_llm_client")
    def test_previous_attempts_included(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "improved task"
        mock_get_client.return_value = client

        step = {"description": "do something"}
        attempts = [
            {"attempt": 1, "error": "ModuleNotFoundError: pandas", "strategy": "initial attempt"},
            {"attempt": 2, "error": "FileNotFoundError: data.csv", "strategy": "alternative strategy"},
        ]
        result = reflect_and_rewrite(step, "out", "err", previous_attempts=attempts)
        prompt = client.generate.call_args[0][0]
        assert "<previous_attempts>" in prompt
        assert "ModuleNotFoundError" in prompt
        assert "initial attempt" in prompt
        assert "alternative strategy" in prompt
        assert result == "improved task"

    @patch("architect.planner.MINIMAL_MODE", True)
    @patch("architect.planner.get_llm_client")
    def test_no_previous_attempts_section_when_none(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "rewritten"
        mock_get_client.return_value = client

        step = {"description": "task"}
        reflect_and_rewrite(step, "out", "err")
        prompt = client.generate.call_args[0][0]
        assert "<previous_attempts>" not in prompt

    @patch("architect.planner.get_llm_client")
    def test_counterfactual_tags_stripped(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = (
            "<diagnosis>Logic error</diagnosis>\n"
            "<counterfactual>Root cause is in this step, not a dependency.</counterfactual>\n"
            "<strategies>1. Fix parsing. Pick 1.</strategies>\n"
            "Improved: fix the parsing logic"
        )
        mock_get_client.return_value = client

        step = {"description": "parse data"}
        result = reflect_and_rewrite(step, "out", "err")
        assert "fix the parsing logic" in result
        assert "<counterfactual>" not in result
        assert "<diagnosis>" not in result
        assert "<strategies>" not in result


class TestIsConfusedOutput:
    def test_excessive_length(self):
        assert _is_confused_output("x" * 10000, "short", "") is True

    def test_reasonable_length(self):
        assert _is_confused_output("reasonable output", "short task", "") is False

    def test_error_verbatim(self):
        error = "A" * 300
        assert _is_confused_output(f"prefix {error} suffix", "task", error) is True

    def test_short_error_not_flagged(self):
        assert _is_confused_output("has error text", "task", "error") is False


class TestDecomposeFailingStep:
    @patch("architect.planner.get_llm_client")
    def test_returns_decomposed_description(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = (
            "Phase 1: download the file. Phase 2: parse it. Phase 3: save results."
        )
        mock_get_client.return_value = client

        step = {"description": "download and process data"}
        result = decompose_failing_step(step, "stdout", "stderr")
        assert "Phase 1" in result

    @patch("architect.planner.get_llm_client")
    def test_empty_returns_original(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "  "
        mock_get_client.return_value = client

        step = {"description": "original task"}
        result = decompose_failing_step(step, "", "")
        assert result == "original task"

    @patch("architect.planner.get_llm_client")
    def test_prompt_includes_failure_context(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "decomposed"
        mock_get_client.return_value = client

        step = {"description": "my task"}
        decompose_failing_step(step, "out_data", "err_data")
        prompt = client.generate.call_args[0][0]
        assert "my task" in prompt
        assert "out_data" in prompt
        assert "err_data" in prompt


class TestResearchGoal:
    @patch("architect.planner.get_llm_client")
    def test_returns_research_summary(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = (
            "1. Key findings: Use ISNCSCI standards.\n"
            "2. Recommended: pandas 2.1.0\n"
            "3. Pitfalls: Avoid manual scoring.\n"
        )
        mock_get_client.return_value = client

        result = research_goal("Build a SCI rehab analytics tool")
        assert "ISNCSCI" in result
        assert "pandas" in result
        client.generate.assert_called_once()
        prompt = client.generate.call_args[0][0]
        assert "SCI rehab analytics" in prompt

    @patch("architect.planner.get_llm_client")
    def test_returns_empty_on_exception(self, mock_get_client):
        client = MagicMock()
        client.generate.side_effect = RuntimeError("API down")
        mock_get_client.return_value = client

        result = research_goal("some goal")
        assert result == ""

    @patch("architect.planner.get_llm_client")
    def test_returns_empty_on_blank_response(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "   "
        mock_get_client.return_value = client

        result = research_goal("some goal")
        assert result == ""

    @patch("architect.planner.get_llm_client")
    def test_prompt_contains_research_instructions(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "No additional research needed"
        mock_get_client.return_value = client

        research_goal("Print hello world")
        prompt = client.generate.call_args[0][0]
        assert "best practices" in prompt.lower()
        assert "citations" in prompt.lower()


class TestResearchInDecomposition:
    @patch("architect.planner.get_llm_client")
    def test_research_context_appears_in_decompose_prompt(self, mock_get_client):
        """Research context is injected into decompose_goal's prompt."""
        steps_json = json.dumps([
            {"title": "step1", "description": "do X", "depends_on": []}
        ])
        client = MagicMock()
        client.generate.return_value = steps_json
        mock_get_client.return_value = client

        research = "Use ISNCSCI scoring standards v2023."
        decompose_goal("Build rehab tool", research_context=research)
        prompt = client.generate.call_args[0][0]
        assert "<research_findings>" in prompt
        assert "ISNCSCI scoring standards v2023" in prompt
        assert "</research_findings>" in prompt

    @patch("architect.planner.get_llm_client")
    def test_no_research_context_no_tags(self, mock_get_client):
        """When research_context is empty, no research_findings tags appear."""
        steps_json = json.dumps([
            {"title": "step1", "description": "do X", "depends_on": []}
        ])
        client = MagicMock()
        client.generate.return_value = steps_json
        mock_get_client.return_value = client

        decompose_goal("Print hello", research_context="")
        prompt = client.generate.call_args[0][0]
        assert "<research_findings>" not in prompt

    @patch("architect.planner.get_llm_client")
    def test_voting_passes_research_to_all_plans(self, mock_get_client):
        """decompose_goal_with_voting passes research_context to plan generation."""
        steps_json = json.dumps([
            {"title": "s1", "description": "d1", "depends_on": []},
            {"title": "s2", "description": "d2", "depends_on": [1]},
        ])
        client = MagicMock()
        # Return valid steps for all generate calls (plan generation + selection)
        client.generate.return_value = steps_json
        mock_get_client.return_value = client

        research = "Use library X v3.0 for best results."
        decompose_goal_with_voting(
            "Complex analytics project",
            research_context=research,
            complexity="complex",
        )
        # At least one generate call should contain the research context
        prompts = [c.args[0] for c in client.generate.call_args_list]
        assert any("library X v3.0" in p for p in prompts)
        assert any("<research_findings>" in p for p in prompts)

    @patch("architect.planner.get_llm_client")
    def test_trivial_goal_passes_research_to_single_decompose(self, mock_get_client):
        """Even trivial goals pass through research_context if provided."""
        steps_json = json.dumps([
            {"title": "s1", "description": "d1", "depends_on": []}
        ])
        client = MagicMock()
        client.generate.return_value = steps_json
        mock_get_client.return_value = client

        research = "Relevant finding here."
        decompose_goal_with_voting(
            "Simple task",
            research_context=research,
            complexity="trivial",
        )
        # The decompose call should include research context
        prompts = [c.args[0] for c in client.generate.call_args_list]
        assert any("Relevant finding here" in p for p in prompts)

    @patch("architect.planner.get_llm_client")
    def test_precomputed_complexity_skips_estimation(self, mock_get_client):
        """When complexity is pre-computed, estimate_complexity is not called."""
        steps_json = json.dumps([
            {"title": "s1", "description": "d1", "depends_on": []}
        ])
        client = MagicMock()
        client.generate.return_value = steps_json
        mock_get_client.return_value = client

        decompose_goal_with_voting(
            "A task", complexity="simple",
        )
        # Only one generate call (decompose), not two (complexity + decompose)
        prompts = [c.args[0] for c in client.generate.call_args_list]
        assert not any("Rate the complexity" in p for p in prompts)
