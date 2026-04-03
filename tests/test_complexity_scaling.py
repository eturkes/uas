"""Tests for Section 7 — Decomposition depth scaling for complex goals.

Verifies that:
- Complex goals produce at least 8 steps
- No step has more than 3 distinct deliverables
- enforce_minimum_steps re-prompts when under the minimum
- count_step_deliverables accurately counts outputs
- flag_overloaded_steps identifies steps with too many deliverables
"""

import json
from unittest.mock import patch, MagicMock

import pytest

from architect.planner import (
    enforce_minimum_steps,
    count_step_deliverables,
    flag_overloaded_steps,
    MINIMUM_STEPS,
    MAX_DELIVERABLES_PER_STEP,
)


# ---------------------------------------------------------------------------
# MINIMUM_STEPS constants
# ---------------------------------------------------------------------------

class TestMinimumStepsConstants:
    def test_trivial_minimum(self):
        assert MINIMUM_STEPS["trivial"] == 1

    def test_simple_minimum(self):
        assert MINIMUM_STEPS["simple"] == 2

    def test_medium_minimum(self):
        assert MINIMUM_STEPS["medium"] == 4

    def test_complex_minimum(self):
        assert MINIMUM_STEPS["complex"] == 8


# ---------------------------------------------------------------------------
# count_step_deliverables
# ---------------------------------------------------------------------------

class TestCountStepDeliverables:
    def test_empty_step(self):
        assert count_step_deliverables({}) == 0
        assert count_step_deliverables({"description": ""}) == 0

    def test_single_deliverable(self):
        step = {
            "description": "Create a data simulator module and save as simulator.py."
        }
        assert count_step_deliverables(step) >= 1

    def test_multiple_file_outputs(self):
        step = {
            "description": (
                "Save as outputs/summary.json, export as outputs/report.csv, "
                "and write to outputs/chart.png."
            )
        }
        count = count_step_deliverables(step)
        assert count >= 3

    def test_multiple_creation_actions(self):
        step = {
            "description": (
                "Create a cleaning module, build a validation module, "
                "implement a transformation pipeline, and generate test fixtures."
            )
        }
        count = count_step_deliverables(step)
        assert count >= 3

    def test_overloaded_step(self):
        step = {
            "description": (
                "Train an XGBoost model, implement SHAP explainability, "
                "create subgroup clustering, build visualization dashboard, "
                "and generate a PDF report."
            )
        }
        count = count_step_deliverables(step)
        assert count > MAX_DELIVERABLES_PER_STEP


# ---------------------------------------------------------------------------
# flag_overloaded_steps
# ---------------------------------------------------------------------------

class TestFlagOverloadedSteps:
    def test_no_overloaded(self):
        steps = [
            {"title": "Step 1", "description": "Create a module."},
            {"title": "Step 2", "description": "Save results to out.json."},
        ]
        assert flag_overloaded_steps(steps) == []

    def test_detects_overloaded(self):
        steps = [
            {"title": "Simple", "description": "Create a module."},
            {
                "title": "Overloaded",
                "description": (
                    "Train a model, implement SHAP analysis, "
                    "create clustering module, build dashboard, "
                    "and generate report."
                ),
            },
        ]
        flagged = flag_overloaded_steps(steps)
        assert 1 in flagged

    def test_empty_list(self):
        assert flag_overloaded_steps([]) == []


# ---------------------------------------------------------------------------
# enforce_minimum_steps
# ---------------------------------------------------------------------------

class TestEnforceMinimumSteps:
    def _make_steps(self, n):
        """Create n minimal valid steps with sequential dependencies."""
        steps = []
        for i in range(n):
            steps.append({
                "title": f"Step {i + 1}",
                "description": f"Do thing {i + 1}.",
                "depends_on": [i] if i > 0 else [],
                "verify": "check output",
                "environment": [],
            })
        return steps

    def test_sufficient_steps_returned_unchanged(self):
        """If steps >= minimum, no re-decomposition happens."""
        steps = self._make_steps(10)
        result = enforce_minimum_steps("big goal", steps, "complex")
        assert result is steps  # Same object, not re-decomposed

    def test_trivial_single_step_ok(self):
        steps = self._make_steps(1)
        result = enforce_minimum_steps("print hello", steps, "trivial")
        assert result is steps

    @patch("architect.planner.get_llm_client")
    def test_complex_with_too_few_steps_reprompts(self, mock_get_client):
        """A complex goal with 3 steps triggers re-decomposition."""
        original_steps = self._make_steps(3)

        # Mock LLM returning a larger plan
        expanded_steps = self._make_steps(10)
        client = MagicMock()
        client.generate.return_value = (json.dumps(expanded_steps), {"input": 0, "output": 0})
        mock_get_client.return_value = client

        result = enforce_minimum_steps("complex goal", original_steps, "complex")
        assert len(result) == 10
        # Verify the LLM was called
        client.generate.assert_called_once()

    @patch("architect.planner.get_llm_client")
    def test_returns_original_on_llm_failure(self, mock_get_client):
        """If re-decomposition fails, original steps are preserved."""
        original_steps = self._make_steps(3)
        client = MagicMock()
        client.generate.side_effect = RuntimeError("LLM down")
        mock_get_client.return_value = client

        result = enforce_minimum_steps("complex goal", original_steps, "complex")
        assert result is original_steps

    @patch("architect.planner.get_llm_client")
    def test_returns_original_on_parse_failure(self, mock_get_client):
        """If the LLM returns unparseable JSON, original steps are preserved."""
        original_steps = self._make_steps(3)
        client = MagicMock()
        client.generate.return_value = ("not valid json at all", {"input": 0, "output": 0})
        mock_get_client.return_value = client

        result = enforce_minimum_steps("complex goal", original_steps, "complex")
        assert result is original_steps

    @patch("architect.planner.get_llm_client")
    def test_returns_original_if_redecomp_is_smaller(self, mock_get_client):
        """If re-decomposition returns fewer steps than original, keep original."""
        original_steps = self._make_steps(5)
        client = MagicMock()
        client.generate.return_value = (json.dumps(self._make_steps(2)), {"input": 0, "output": 0})
        mock_get_client.return_value = client

        result = enforce_minimum_steps("complex goal", original_steps, "complex")
        assert result is original_steps

    @patch("architect.planner.get_llm_client")
    def test_medium_below_minimum_reprompts(self, mock_get_client):
        """A medium goal with 2 steps triggers re-decomposition (min 4)."""
        original_steps = self._make_steps(2)
        expanded = self._make_steps(5)
        client = MagicMock()
        client.generate.return_value = (json.dumps(expanded), {"input": 0, "output": 0})
        mock_get_client.return_value = client

        result = enforce_minimum_steps("medium goal", original_steps, "medium")
        assert len(result) == 5

    @patch("architect.planner.get_llm_client")
    def test_overloaded_steps_trigger_reprompt(self, mock_get_client):
        """Steps with >3 deliverables trigger re-decomposition even if count is sufficient."""
        steps = [
            {
                "title": "Overloaded step",
                "description": (
                    "Train a model, implement SHAP, create clustering, "
                    "build dashboard, and generate report."
                ),
                "depends_on": [],
                "verify": "",
                "environment": [],
            },
        ] * 8  # 8 steps but all overloaded

        expanded = self._make_steps(12)
        client = MagicMock()
        client.generate.return_value = (json.dumps(expanded), {"input": 0, "output": 0})
        mock_get_client.return_value = client

        result = enforce_minimum_steps("complex goal", steps, "complex")
        assert len(result) == 12

    def test_unknown_complexity_uses_default_minimum(self):
        """Unknown complexity category uses fallback minimum of 4."""
        steps = self._make_steps(5)
        result = enforce_minimum_steps("goal", steps, "unknown_category")
        assert result is steps  # 5 >= 4 (default)

    @patch("architect.planner.get_llm_client")
    def test_normalizes_zero_indexed_deps(self, mock_get_client):
        """Re-decomposed plan with 0-indexed deps gets normalized to 1-based."""
        original_steps = self._make_steps(2)
        # Return steps with 0-based depends_on
        new_steps = [
            {"title": "A", "description": "Do A", "depends_on": []},
            {"title": "B", "description": "Do B", "depends_on": [0]},
            {"title": "C", "description": "Do C", "depends_on": [0]},
            {"title": "D", "description": "Do D", "depends_on": [1, 2]},
        ]
        client = MagicMock()
        client.generate.return_value = (json.dumps(new_steps), {"input": 0, "output": 0})
        mock_get_client.return_value = client

        result = enforce_minimum_steps("medium goal", original_steps, "medium")
        assert len(result) == 4
        assert result[1]["depends_on"] == [1]  # Normalized from [0]
        assert result[3]["depends_on"] == [2, 3]  # Normalized from [1, 2]


# ---------------------------------------------------------------------------
# Integration: Example 4 in DECOMPOSITION_PROMPT
# ---------------------------------------------------------------------------

class TestDecompositionPromptExample:
    def test_complex_example_exists_in_prompt(self):
        """The DECOMPOSITION_PROMPT includes the complex multi-phase example."""
        from architect.planner import DECOMPOSITION_PROMPT
        assert "Example 4" in DECOMPOSITION_PROMPT
        assert "complex" in DECOMPOSITION_PROMPT
        assert "Phase 1 integration checkpoint" in DECOMPOSITION_PROMPT
        assert "Phase 2 integration checkpoint" in DECOMPOSITION_PROMPT
        assert "12 steps" in DECOMPOSITION_PROMPT
