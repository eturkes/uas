"""Tests for architect.planner: parse_steps_json, prompt content, and critique."""

import json
from unittest.mock import patch, MagicMock

import pytest

from architect.planner import (
    parse_steps_json,
    DECOMPOSITION_PROMPT,
    REFLECT_PROMPT,
    DECOMPOSE_STEP_PROMPT,
    critique_and_refine_plan,
    reflect_and_rewrite,
    decompose_failing_step,
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


class TestDecompositionPromptConstraints:
    def test_full_network_access(self):
        assert "unrestricted network access" in DECOMPOSITION_PROMPT

    def test_can_install_packages(self):
        assert "Install any packages" in DECOMPOSITION_PROMPT

    def test_observable_stdout(self):
        assert "produce observable output to stdout" in DECOMPOSITION_PROMPT

    def test_no_user_interaction(self):
        assert "Do NOT create steps that require user interaction" in DECOMPOSITION_PROMPT

    def test_xml_tags_present(self):
        assert "<instructions>" in DECOMPOSITION_PROMPT
        assert "<rules>" in DECOMPOSITION_PROMPT
        assert "<output_format>" in DECOMPOSITION_PROMPT
        assert "<examples>" in DECOMPOSITION_PROMPT

    def test_analysis_tags_requested(self):
        assert "<analysis>" in DECOMPOSITION_PROMPT

    def test_verify_field_in_format(self):
        assert '"verify"' in DECOMPOSITION_PROMPT

    def test_environment_field_in_format(self):
        assert '"environment"' in DECOMPOSITION_PROMPT

    def test_complexity_guidance(self):
        assert "trivial" in DECOMPOSITION_PROMPT.lower()
        assert "complex" in DECOMPOSITION_PROMPT.lower()

    def test_parallelism_instruction(self):
        assert "parallelism" in DECOMPOSITION_PROMPT.lower()

    def test_goal_at_top(self):
        """Goal data should appear at the top of the prompt."""
        goal_pos = DECOMPOSITION_PROMPT.index("<goal>")
        instructions_pos = DECOMPOSITION_PROMPT.index("<instructions>")
        rules_pos = DECOMPOSITION_PROMPT.index("<rules>")
        assert goal_pos < instructions_pos
        assert goal_pos < rules_pos

    def test_complexity_assessment_tag(self):
        assert "<complexity_assessment>" in DECOMPOSITION_PROMPT

    def test_anti_patterns_section(self):
        assert "<anti_patterns>" in DECOMPOSITION_PROMPT
        assert "Over-splitting" in DECOMPOSITION_PROMPT
        assert "Under-splitting" in DECOMPOSITION_PROMPT
        assert "Missing dependencies" in DECOMPOSITION_PROMPT

    def test_analysis_strengthened(self):
        assert "failure modes" in DECOMPOSITION_PROMPT.lower()
        assert "risk areas" in DECOMPOSITION_PROMPT.lower()
        assert "parallelization" in DECOMPOSITION_PROMPT.lower()


class TestComplexityAssessmentStripped:
    def test_complexity_assessment_tags_stripped(self):
        raw = (
            '<analysis>This is medium.</analysis>\n'
            '<complexity_assessment>medium — 3 steps</complexity_assessment>\n'
            '[{"title": "a", "description": "b"}]'
        )
        result = parse_steps_json(raw)
        assert len(result) == 1
        assert result[0]["title"] == "a"

    def test_both_analysis_and_complexity_stripped(self):
        raw = (
            '<analysis>\nMulti-line\nanalysis\n</analysis>\n'
            '<complexity_assessment>complex — 8+ steps needed</complexity_assessment>\n'
            '[{"title": "x", "description": "y"}, {"title": "z", "description": "w"}]'
        )
        result = parse_steps_json(raw)
        assert len(result) == 2


class TestPromptStructureOrdering:
    def test_reflect_prompt_data_before_instructions(self):
        """Failure output should appear before instructions in REFLECT_PROMPT."""
        failure_pos = REFLECT_PROMPT.index("<failure_output>")
        instructions_pos = REFLECT_PROMPT.index("<instructions>")
        assert failure_pos < instructions_pos

    def test_reflect_prompt_has_counterfactual(self):
        assert "<counterfactual>" in REFLECT_PROMPT
        assert "root cause" in REFLECT_PROMPT.lower()

    def test_reflect_prompt_has_previous_attempts_placeholder(self):
        assert "{previous_attempts_section}" in REFLECT_PROMPT

    def test_decompose_step_prompt_data_before_instructions(self):
        """Failed task data should appear before instructions."""
        task_pos = DECOMPOSE_STEP_PROMPT.index("<failed_task>")
        instructions_pos = DECOMPOSE_STEP_PROMPT.index("<instructions>")
        assert task_pos < instructions_pos


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

    @patch("architect.planner.get_llm_client")
    def test_escalation_alternative_approach(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "Use a completely different approach"
        mock_get_client.return_value = client

        step = {"description": "do something"}
        result = reflect_and_rewrite(step, "stdout", "stderr", escalation_level=1)
        prompt = client.generate.call_args[0][0]
        assert "fundamentally different strategy" in prompt
        assert result == "Use a completely different approach"

    @patch("architect.planner.get_llm_client")
    def test_escalation_final_attempt(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "Final defensive approach"
        mock_get_client.return_value = client

        step = {"description": "do something"}
        result = reflect_and_rewrite(step, "stdout", "stderr", escalation_level=3)
        prompt = client.generate.call_args[0][0]
        assert "FINAL attempt" in prompt
        assert result == "Final defensive approach"

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
