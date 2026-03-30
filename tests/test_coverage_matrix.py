"""Tests for Section 1 — Goal-coverage matrix (extract, verify, fill)."""

import json
from unittest.mock import patch, MagicMock

import pytest

from architect.planner import (
    extract_requirements,
    verify_coverage,
    fill_coverage_gaps,
    ensure_coverage,
)


# ---------------------------------------------------------------------------
# extract_requirements
# ---------------------------------------------------------------------------

class TestExtractRequirements:
    @patch("architect.planner.get_llm_client")
    def test_parses_json_array(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = json.dumps([
            "data simulator",
            "cleaning pipeline",
            "XGBoost model",
        ])
        mock_get_client.return_value = client

        reqs = extract_requirements("Build a data pipeline with ML model")
        assert reqs == ["data simulator", "cleaning pipeline", "XGBoost model"]

    @patch("architect.planner.get_llm_client")
    def test_parses_fenced_json(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = (
            "```json\n"
            '["req A", "req B"]\n'
            "```"
        )
        mock_get_client.return_value = client

        reqs = extract_requirements("Some goal")
        assert reqs == ["req A", "req B"]

    @patch("architect.planner.get_llm_client")
    def test_extracts_from_surrounding_text(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = (
            'Here are the requirements: ["alpha", "beta"] hope that helps.'
        )
        mock_get_client.return_value = client

        reqs = extract_requirements("Goal text")
        assert reqs == ["alpha", "beta"]

    @patch("architect.planner.get_llm_client")
    def test_returns_empty_on_llm_failure(self, mock_get_client):
        client = MagicMock()
        client.generate.side_effect = RuntimeError("LLM down")
        mock_get_client.return_value = client

        reqs = extract_requirements("Some goal")
        assert reqs == []

    @patch("architect.planner.get_llm_client")
    def test_returns_empty_on_unparseable(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "I cannot parse this goal."
        mock_get_client.return_value = client

        reqs = extract_requirements("Goal text")
        assert reqs == []

    @patch("architect.planner.get_llm_client")
    def test_prompt_includes_goal(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = '["req1"]'
        mock_get_client.return_value = client

        extract_requirements("Build a dashboard")
        prompt = client.generate.call_args[0][0]
        assert "Build a dashboard" in prompt


# ---------------------------------------------------------------------------
# verify_coverage
# ---------------------------------------------------------------------------

class TestVerifyCoverage:
    @patch("architect.planner.get_llm_client")
    def test_all_covered(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = json.dumps([
            {"requirement": "data simulator", "covered": True, "covering_steps": [1]},
            {"requirement": "ML model", "covered": True, "covering_steps": [2]},
        ])
        mock_get_client.return_value = client

        steps = [
            {"title": "Build simulator", "description": "Create data simulator"},
            {"title": "Train model", "description": "Train XGBoost model"},
        ]
        matrix = verify_coverage(["data simulator", "ML model"], steps)
        assert len(matrix) == 2
        assert all(entry["covered"] for entry in matrix)

    @patch("architect.planner.get_llm_client")
    def test_detects_uncovered(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = json.dumps([
            {"requirement": "data simulator", "covered": True, "covering_steps": [1]},
            {"requirement": "SHAP explainability", "covered": False, "covering_steps": []},
        ])
        mock_get_client.return_value = client

        steps = [
            {"title": "Build simulator", "description": "Create data simulator"},
        ]
        matrix = verify_coverage(
            ["data simulator", "SHAP explainability"], steps,
        )
        uncovered = [e for e in matrix if not e["covered"]]
        assert len(uncovered) == 1
        assert uncovered[0]["requirement"] == "SHAP explainability"

    @patch("architect.planner.get_llm_client")
    def test_empty_requirements(self, mock_get_client):
        matrix = verify_coverage([], [])
        assert matrix == []
        mock_get_client.assert_not_called()

    @patch("architect.planner.get_llm_client")
    def test_fails_open_on_llm_error(self, mock_get_client):
        client = MagicMock()
        client.generate.side_effect = RuntimeError("LLM down")
        mock_get_client.return_value = client

        matrix = verify_coverage(
            ["req1", "req2"],
            [{"title": "step", "description": "do stuff"}],
        )
        # Fail-open: all marked as covered
        assert len(matrix) == 2
        assert all(e["covered"] for e in matrix)

    @patch("architect.planner.get_llm_client")
    def test_parses_fenced_response(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = (
            "```json\n"
            + json.dumps([
                {"requirement": "r1", "covered": False, "covering_steps": []},
            ])
            + "\n```"
        )
        mock_get_client.return_value = client

        matrix = verify_coverage(
            ["r1"],
            [{"title": "s1", "description": "d1"}],
        )
        assert len(matrix) == 1
        assert not matrix[0]["covered"]


# ---------------------------------------------------------------------------
# fill_coverage_gaps
# ---------------------------------------------------------------------------

class TestFillCoverageGaps:
    @patch("architect.planner.get_llm_client")
    def test_generates_new_steps(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = json.dumps([
            {
                "title": "SHAP analysis",
                "description": "Implement SHAP explainability",
                "depends_on": [2],
                "verify": "SHAP values computed",
                "environment": ["shap"],
            },
        ])
        mock_get_client.return_value = client

        existing = [
            {"title": "Simulator", "description": "Build sim", "depends_on": []},
            {"title": "Model", "description": "Train model", "depends_on": [1]},
        ]
        new_steps = fill_coverage_gaps(
            "Build ML pipeline with SHAP",
            ["SHAP explainability"],
            existing,
        )
        assert len(new_steps) == 1
        assert new_steps[0]["title"] == "SHAP analysis"
        assert new_steps[0]["depends_on"] == [2]

    @patch("architect.planner.get_llm_client")
    def test_empty_uncovered_returns_empty(self, mock_get_client):
        result = fill_coverage_gaps("goal", [], [])
        assert result == []
        mock_get_client.assert_not_called()

    @patch("architect.planner.get_llm_client")
    def test_llm_failure_returns_empty(self, mock_get_client):
        client = MagicMock()
        client.generate.side_effect = RuntimeError("LLM down")
        mock_get_client.return_value = client

        result = fill_coverage_gaps("goal", ["missing req"], [])
        assert result == []

    @patch("architect.planner.get_llm_client")
    def test_prompt_includes_next_step_number(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = '[]'
        mock_get_client.return_value = client

        existing = [
            {"title": "A", "description": "a", "depends_on": []},
            {"title": "B", "description": "b", "depends_on": []},
            {"title": "C", "description": "c", "depends_on": []},
        ]
        fill_coverage_gaps("goal", ["uncovered"], existing)
        prompt = client.generate.call_args[0][0]
        assert "4" in prompt  # next_step_number = len(existing) + 1


# ---------------------------------------------------------------------------
# ensure_coverage (end-to-end chain)
# ---------------------------------------------------------------------------

class TestEnsureCoverage:
    @patch("architect.planner.get_llm_client")
    def test_no_gaps_returns_original_steps(self, mock_get_client):
        client = MagicMock()
        # First call: extract_requirements
        # Second call: verify_coverage (all covered)
        client.generate.side_effect = [
            json.dumps(["req A", "req B"]),
            json.dumps([
                {"requirement": "req A", "covered": True, "covering_steps": [1]},
                {"requirement": "req B", "covered": True, "covering_steps": [2]},
            ]),
        ]
        mock_get_client.return_value = client

        steps = [
            {"title": "Step 1", "description": "Does A", "depends_on": []},
            {"title": "Step 2", "description": "Does B", "depends_on": [1]},
        ]
        result_steps, reqs = ensure_coverage("Do A and B", steps)
        assert len(result_steps) == 2
        assert reqs == ["req A", "req B"]

    @patch("architect.planner.get_llm_client")
    def test_fills_gaps_when_uncovered(self, mock_get_client):
        client = MagicMock()
        client.generate.side_effect = [
            # extract_requirements
            json.dumps(["data sim", "ML model", "SHAP"]),
            # verify_coverage — SHAP uncovered
            json.dumps([
                {"requirement": "data sim", "covered": True, "covering_steps": [1]},
                {"requirement": "ML model", "covered": True, "covering_steps": [2]},
                {"requirement": "SHAP", "covered": False, "covering_steps": []},
            ]),
            # fill_coverage_gaps
            json.dumps([{
                "title": "Add SHAP", "description": "SHAP analysis",
                "depends_on": [2], "verify": "ok", "environment": [],
            }]),
        ]
        mock_get_client.return_value = client

        steps = [
            {"title": "Simulator", "description": "Build sim", "depends_on": []},
            {"title": "Model", "description": "Train model", "depends_on": [1]},
        ]
        result_steps, reqs = ensure_coverage("Full pipeline", steps)
        assert len(result_steps) == 3
        assert result_steps[2]["title"] == "Add SHAP"
        assert reqs == ["data sim", "ML model", "SHAP"]

    @patch("architect.planner.get_llm_client")
    def test_extraction_failure_returns_original(self, mock_get_client):
        client = MagicMock()
        client.generate.side_effect = RuntimeError("LLM down")
        mock_get_client.return_value = client

        steps = [{"title": "S1", "description": "D1", "depends_on": []}]
        result_steps, reqs = ensure_coverage("goal", steps)
        assert result_steps == steps
        assert reqs == []

    @patch("architect.planner.get_llm_client")
    def test_complex_scenario_detects_gaps(self, mock_get_client):
        """Simulate a complex run: 5-step plan missing modeling, SHAP,
        segmentation, and dashboard tabs."""
        client = MagicMock()
        client.generate.side_effect = [
            # extract_requirements — comprehensive list
            json.dumps([
                "data simulator from spec",
                "cleaning pipeline",
                "bilingual translations",
                "XGBoost predictive model",
                "SHAP explainability",
                "subgroup discovery",
                "dashboard tab: cohort overview",
                "dashboard tab: patient simulator",
                "dashboard tab: insight engine",
                "bilingual toggle",
            ]),
            # verify_coverage — most uncovered
            json.dumps([
                {"requirement": "data simulator from spec", "covered": True, "covering_steps": [1]},
                {"requirement": "cleaning pipeline", "covered": True, "covering_steps": [2]},
                {"requirement": "bilingual translations", "covered": True, "covering_steps": [3]},
                {"requirement": "XGBoost predictive model", "covered": False, "covering_steps": []},
                {"requirement": "SHAP explainability", "covered": False, "covering_steps": []},
                {"requirement": "subgroup discovery", "covered": False, "covering_steps": []},
                {"requirement": "dashboard tab: cohort overview", "covered": False, "covering_steps": []},
                {"requirement": "dashboard tab: patient simulator", "covered": False, "covering_steps": []},
                {"requirement": "dashboard tab: insight engine", "covered": True, "covering_steps": [5]},
                {"requirement": "bilingual toggle", "covered": True, "covering_steps": [3]},
            ]),
            # fill_coverage_gaps — 5 new steps
            json.dumps([
                {"title": "XGBoost model", "description": "Train predictive model",
                 "depends_on": [2], "verify": "model saved", "environment": ["xgboost"]},
                {"title": "SHAP analysis", "description": "Compute SHAP values",
                 "depends_on": [6], "verify": "SHAP done", "environment": ["shap"]},
                {"title": "Subgroup discovery", "description": "Find subgroups",
                 "depends_on": [2], "verify": "subgroups found", "environment": []},
                {"title": "Cohort overview tab", "description": "Dashboard tab 1",
                 "depends_on": [5], "verify": "tab renders", "environment": []},
                {"title": "Patient simulator tab", "description": "Dashboard tab 2",
                 "depends_on": [5], "verify": "tab renders", "environment": []},
            ]),
        ]
        mock_get_client.return_value = client

        # Sparse 5-step plan missing key deliverables
        steps = [
            {"title": "Data simulator", "description": "Build simulator", "depends_on": []},
            {"title": "Cleaning", "description": "Clean data", "depends_on": [1]},
            {"title": "Translations", "description": "JA/EN", "depends_on": []},
            {"title": "Temporal analysis", "description": "Temporal", "depends_on": [2]},
            {"title": "Dashboard", "description": "Main dashboard", "depends_on": [4]},
        ]
        result_steps, reqs = ensure_coverage(
            "Build analytics dashboard with modeling, SHAP, segmentation, 3 tabs",
            steps,
        )
        # Original 5 + 5 gap-fill steps
        assert len(result_steps) == 10
        assert len(reqs) == 10
        # Verify the gap-fill steps are present
        titles = [s["title"] for s in result_steps]
        assert "XGBoost model" in titles
        assert "SHAP analysis" in titles
        assert "Subgroup discovery" in titles
