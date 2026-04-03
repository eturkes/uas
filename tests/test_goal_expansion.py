"""Tests for architect.planner.generate_project_spec."""

from unittest.mock import MagicMock, patch

from architect.planner import generate_project_spec


SAMPLE_SPEC = """\
# Project Specification

## 1. Overview
A data processing pipeline.

## 2. Goals
- Download CSV data
- Clean and validate

## 3. Non-Goals
- Production deployment

## 4. Architecture
Single sequential pipeline.

## 5. Data Model
raw_data.csv with headers.

## 6. Interface Contracts
N/A — single component.

## 7. Acceptance Criteria
- raw_data.csv exists with >0 rows

## 8. Constraints
- Python, pandas
"""


class TestGenerateProjectSpec:
    @patch("architect.planner.get_llm_client")
    def test_returns_spec_for_medium_goal(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = (SAMPLE_SPEC, {"input": 0, "output": 0})
        mock_get_client.return_value = client

        result = generate_project_spec("analyze sales data", complexity="medium")
        assert "## 1. Overview" in result
        assert "## 7. Acceptance Criteria" in result
        assert client.generate.call_count == 1

    @patch("architect.planner.get_llm_client")
    def test_returns_spec_for_complex_goal(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = (SAMPLE_SPEC, {"input": 0, "output": 0})
        mock_get_client.return_value = client

        result = generate_project_spec("build a dashboard", complexity="complex")
        assert "## 1. Overview" in result

    @patch("architect.planner.get_llm_client")
    def test_skips_for_trivial_goal(self, mock_get_client):
        result = generate_project_spec("print hello", complexity="trivial")
        assert result == ""
        mock_get_client.assert_not_called()

    @patch("architect.planner.get_llm_client")
    def test_returns_spec_for_simple_goal(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = (SAMPLE_SPEC, {"input": 0, "output": 0})
        mock_get_client.return_value = client

        result = generate_project_spec("download a file", complexity="simple")
        assert "## 1. Overview" in result

    @patch("architect.planner.get_llm_client")
    def test_llm_failure_returns_empty(self, mock_get_client):
        client = MagicMock()
        client.generate.side_effect = RuntimeError("API timeout")
        mock_get_client.return_value = client

        result = generate_project_spec("my goal", complexity="medium")
        assert result == ""

    @patch("architect.planner.get_llm_client")
    def test_empty_response_returns_empty(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = ("   ", {"input": 0, "output": 0})
        mock_get_client.return_value = client

        result = generate_project_spec("goal", complexity="medium")
        assert result == ""

    @patch("architect.planner.get_llm_client")
    def test_whitespace_stripped(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = ("\n  # Project Specification\n\n## 1. Overview\nTest  \n", {"input": 0, "output": 0})
        mock_get_client.return_value = client

        result = generate_project_spec("goal", complexity="medium")
        assert result.startswith("# Project Specification")
        assert not result.endswith("\n")

    @patch("architect.planner.get_llm_client")
    def test_uses_planner_role(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = (SAMPLE_SPEC, {"input": 0, "output": 0})
        mock_get_client.return_value = client

        generate_project_spec("any goal", complexity="medium")
        mock_get_client.assert_called_once_with(role="planner")

    @patch("architect.planner.get_llm_client")
    def test_prompt_includes_goal(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = (SAMPLE_SPEC, {"input": 0, "output": 0})
        mock_get_client.return_value = client

        generate_project_spec("build a web scraper", complexity="medium")
        prompt = client.generate.call_args[0][0]
        assert "build a web scraper" in prompt

    @patch("architect.planner.get_llm_client")
    def test_research_context_included_in_prompt(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = (SAMPLE_SPEC, {"input": 0, "output": 0})
        mock_get_client.return_value = client

        generate_project_spec(
            "build an API", complexity="medium",
            research_context="Use FastAPI 0.115",
        )
        prompt = client.generate.call_args[0][0]
        assert "Use FastAPI 0.115" in prompt
        assert "<research_findings>" in prompt

    @patch("architect.planner.get_llm_client")
    def test_no_research_no_tags(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = (SAMPLE_SPEC, {"input": 0, "output": 0})
        mock_get_client.return_value = client

        generate_project_spec("build something", complexity="medium")
        prompt = client.generate.call_args[0][0]
        assert "<research_findings>" not in prompt

    @patch("architect.planner.get_llm_client")
    def test_default_complexity_is_medium(self, mock_get_client):
        """Default complexity should not skip spec generation."""
        client = MagicMock()
        client.generate.return_value = (SAMPLE_SPEC, {"input": 0, "output": 0})
        mock_get_client.return_value = client

        result = generate_project_spec("do something")
        assert result != ""
        assert client.generate.call_count == 1
