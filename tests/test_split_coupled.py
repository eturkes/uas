"""Tests for Section 3 — Enforce creation/integration separation.

Verifies that split_coupled_steps() detects and splits steps that couple
creation of a new module with integration into an existing one, and that
dependencies are correctly remapped.
"""

import json
from unittest.mock import patch, MagicMock

import pytest

from architect.planner import (
    split_coupled_steps,
    _step_is_coupled,
    _parse_split_response,
)


# ---------------------------------------------------------------------------
# _step_is_coupled heuristic
# ---------------------------------------------------------------------------

class TestStepIsCoupled:
    def test_creation_only_is_not_coupled(self):
        step = {"title": "Build simulator", "description": "Create a data simulator module."}
        assert not _step_is_coupled(step)

    def test_integration_only_is_not_coupled(self):
        step = {"title": "Wire dashboard", "description": "Update the dashboard to show new charts."}
        assert not _step_is_coupled(step)

    def test_creation_and_integration_is_coupled(self):
        step = {
            "title": "Create temporal.py and update pipeline",
            "description": "Create temporal.py and update pipeline.py to use it.",
        }
        assert _step_is_coupled(step)

    def test_build_and_integrate_is_coupled(self):
        step = {
            "title": "Build SHAP module",
            "description": "Build SHAP explainability module and integrate into the dashboard.",
        }
        assert _step_is_coupled(step)

    def test_implement_and_modify_is_coupled(self):
        step = {
            "title": "Add subgroup analysis",
            "description": "Implement subgroup discovery and modify the report generator to include results.",
        }
        assert _step_is_coupled(step)

    def test_empty_step_is_not_coupled(self):
        assert not _step_is_coupled({})
        assert not _step_is_coupled({"title": "", "description": ""})

    def test_case_insensitive(self):
        step = {
            "title": "CREATE model AND UPDATE dashboard",
            "description": "",
        }
        assert _step_is_coupled(step)


# ---------------------------------------------------------------------------
# _parse_split_response
# ---------------------------------------------------------------------------

class TestParseSplitResponse:
    def test_parses_json_array(self):
        response = json.dumps([
            {"title": "Create X", "description": "Make X"},
            {"title": "Integrate X", "description": "Wire X into Y"},
        ])
        result = _parse_split_response(response)
        assert len(result) == 2
        assert result[0]["title"] == "Create X"

    def test_parses_fenced_json(self):
        response = "```json\n" + json.dumps([
            {"title": "A", "description": "a"},
            {"title": "B", "description": "b"},
        ]) + "\n```"
        result = _parse_split_response(response)
        assert len(result) == 2

    def test_extracts_from_surrounding_text(self):
        inner = json.dumps([
            {"title": "A", "description": "a"},
            {"title": "B", "description": "b"},
        ])
        response = f"Here is the split: {inner} done."
        result = _parse_split_response(response)
        assert len(result) == 2

    def test_rejects_wrong_length(self):
        response = json.dumps([{"title": "Only one", "description": "one"}])
        assert _parse_split_response(response) is None

    def test_returns_none_on_garbage(self):
        assert _parse_split_response("not json at all") is None


# ---------------------------------------------------------------------------
# split_coupled_steps — no coupled steps
# ---------------------------------------------------------------------------

class TestSplitCoupledNoop:
    def test_no_coupled_steps_returns_unchanged(self):
        steps = [
            {"title": "Build simulator", "description": "Create simulator", "depends_on": []},
            {"title": "Train model", "description": "Train XGBoost", "depends_on": [1]},
        ]
        result = split_coupled_steps(steps)
        assert len(result) == 2
        assert result[0]["title"] == "Build simulator"
        assert result[1]["depends_on"] == [1]

    def test_empty_list(self):
        assert split_coupled_steps([]) == []


# ---------------------------------------------------------------------------
# split_coupled_steps — with coupled steps
# ---------------------------------------------------------------------------

class TestSplitCoupledSteps:
    @patch("architect.planner.get_llm_client")
    def test_splits_coupled_step(self, mock_get_client):
        """A step that creates and integrates should be split into two."""
        client = MagicMock()
        client.generate.return_value = (json.dumps([
            {
                "title": "Create temporal analysis module",
                "description": "Create temporal.py with analysis functions.",
                "depends_on": [1],
                "verify": "temporal.py exists",
                "environment": [],
            },
            {
                "title": "Integrate temporal into pipeline",
                "description": "Update pipeline.py to import and use temporal.py.",
                "depends_on": [1, 2],
                "verify": "pipeline uses temporal",
                "environment": [],
            },
        ]), {"input": 0, "output": 0})
        mock_get_client.return_value = client

        steps = [
            {"title": "Build data pipeline", "description": "Create data pipeline", "depends_on": []},
            {
                "title": "Create temporal.py and update pipeline",
                "description": "Create temporal.py and update pipeline.py to use it.",
                "depends_on": [1],
            },
            {"title": "Build dashboard", "description": "Create dashboard", "depends_on": [2]},
        ]

        result = split_coupled_steps(steps)

        # Original 3 steps → 4 (step 2 split into 2)
        assert len(result) == 4

        # Step 1 unchanged
        assert result[0]["title"] == "Build data pipeline"
        assert result[0]["depends_on"] == []

        # Step 2 → creation step
        assert "Create" in result[1]["title"] or "create" in result[1]["title"] or "temporal" in result[1]["title"].lower()
        assert result[1]["depends_on"] == [1]  # same as original

        # Step 3 → integration step
        assert result[2]["depends_on"] == [1, 2]  # depends on creation step

        # Step 4 (was step 3) → depends on integration step (3), not old step 2
        assert 3 in result[3]["depends_on"]

    @patch("architect.planner.get_llm_client")
    def test_llm_failure_keeps_step(self, mock_get_client):
        """If LLM fails to split, the original step is kept."""
        client = MagicMock()
        client.generate.side_effect = RuntimeError("LLM down")
        mock_get_client.return_value = client

        steps = [
            {"title": "Create and integrate module", "description": "Create X and modify Y to use it.", "depends_on": []},
        ]
        result = split_coupled_steps(steps)
        assert len(result) == 1
        assert result[0]["title"] == "Create and integrate module"

    @patch("architect.planner.get_llm_client")
    def test_bad_parse_keeps_step(self, mock_get_client):
        """If LLM returns unparseable response, the original step is kept."""
        client = MagicMock()
        client.generate.return_value = ("I cannot split this step.", {"input": 0, "output": 0})
        mock_get_client.return_value = client

        steps = [
            {"title": "Create and integrate module", "description": "Create X and modify Y to use it.", "depends_on": []},
        ]
        result = split_coupled_steps(steps)
        assert len(result) == 1

    @patch("architect.planner.get_llm_client")
    def test_dependency_remapping_multiple_refs(self, mock_get_client):
        """Steps referencing the split step should be remapped to the integration step."""
        client = MagicMock()
        client.generate.return_value = (json.dumps([
            {"title": "Create module", "description": "Create new module", "depends_on": [], "verify": "", "environment": []},
            {"title": "Integrate module", "description": "Wire into existing", "depends_on": [1], "verify": "", "environment": []},
        ]), {"input": 0, "output": 0})
        mock_get_client.return_value = client

        steps = [
            {"title": "Build and wire module", "description": "Create new module and update existing app to import it.", "depends_on": []},
            {"title": "Step A", "description": "Uses module", "depends_on": [1]},
            {"title": "Step B", "description": "Also uses module", "depends_on": [1]},
        ]

        result = split_coupled_steps(steps)
        assert len(result) == 4

        # Steps A and B should now depend on integration step (2), not creation step (1)
        assert 2 in result[2]["depends_on"]
        assert 2 in result[3]["depends_on"]

    @patch("architect.planner.get_llm_client")
    def test_no_step_creates_file_and_modifies_existing(self, mock_get_client):
        """After splitting, no step should both create and integrate."""
        client = MagicMock()
        client.generate.return_value = (json.dumps([
            {"title": "Create analysis module", "description": "Write analysis.py", "depends_on": [], "verify": "", "environment": []},
            {"title": "Wire analysis into dashboard", "description": "Update dashboard to use analysis", "depends_on": [2], "verify": "", "environment": []},
        ]), {"input": 0, "output": 0})
        mock_get_client.return_value = client

        steps = [
            {"title": "Setup", "description": "Setup project", "depends_on": []},
            {"title": "Create analysis and update dashboard",
             "description": "Write analysis.py and modify dashboard.py to import it.",
             "depends_on": [1]},
        ]

        result = split_coupled_steps(steps)
        assert len(result) == 3
        # Creation step should not be coupled
        assert not _step_is_coupled(result[1])
