"""Tests for architect.planner: parse_steps_json, prompt content, and critique."""

import json
from unittest.mock import patch, MagicMock

import pytest

from architect.planner import (
    parse_steps_json,
    DECOMPOSITION_PROMPT,
    critique_and_refine_plan,
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
