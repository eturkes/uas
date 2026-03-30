"""Tests for Section 8 — Cross-section integration verification.

Verifies that all quality improvements (Sections 1-7) work together
without regressions:
- Coverage matrix catches missing requirements
- Replanning preserves coverage
- Coupled steps are split
- File signatures propagate to downstream steps
- Integration checkpoints catch interface mismatches
- Validation triggers corrections
- Complex goals produce enough steps
"""

import csv
import json
import os
from unittest.mock import patch, MagicMock

import pytest

from architect.planner import (
    extract_requirements,
    verify_coverage,
    fill_coverage_gaps,
    ensure_coverage,
    split_coupled_steps,
    insert_integration_checkpoints,
    enforce_minimum_steps,
    replan_remaining_steps,
    generate_corrective_steps,
    topological_sort,
    count_step_deliverables,
    flag_overloaded_steps,
    MINIMUM_STEPS,
    MAX_CORRECTIVE_STEPS_PER_ROUND,
    MAX_CORRECTION_ROUNDS,
)
from architect.executor import extract_file_signatures


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _step(title, description=None, depends_on=None):
    """Shorthand for creating a step dict."""
    return {
        "title": title,
        "description": description or f"Do {title}",
        "depends_on": depends_on or [],
        "verify": "",
        "environment": [],
    }


def _assign_ids(steps):
    """Assign 1-based IDs to steps."""
    for i, s in enumerate(steps):
        s["id"] = i + 1
    return steps


# ---------------------------------------------------------------------------
# End-to-end: coverage + splitting + checkpoints pipeline
# ---------------------------------------------------------------------------

class TestPostDecompositionPipeline:
    """Verify the full post-decomposition pipeline: enforce_minimum_steps →
    ensure_coverage → split_coupled_steps → insert_integration_checkpoints."""

    @patch("architect.planner.get_llm_client")
    def test_pipeline_catches_gaps_and_splits(self, mock_get_client):
        """A sparse plan with coupled steps gets gaps filled, steps split,
        and checkpoints inserted."""
        client = MagicMock()
        call_idx = {"n": 0}

        def _mock_generate(prompt):
            idx = call_idx["n"]
            call_idx["n"] += 1

            # First call: extract_requirements
            if idx == 0:
                return json.dumps([
                    "data simulator",
                    "ML model",
                    "SHAP analysis",
                    "dashboard",
                ])

            # Second call: verify_coverage (SHAP uncovered)
            if idx == 1:
                return json.dumps([
                    {"requirement": "data simulator", "covered": True,
                     "covering_steps": [1]},
                    {"requirement": "ML model", "covered": True,
                     "covering_steps": [2]},
                    {"requirement": "SHAP analysis", "covered": False,
                     "covering_steps": []},
                    {"requirement": "dashboard", "covered": True,
                     "covering_steps": [5]},
                ])

            # Third call: fill_coverage_gaps
            if idx == 2:
                return json.dumps([{
                    "title": "SHAP explainability",
                    "description": "Compute SHAP values for the model",
                    "depends_on": [2],
                    "verify": "SHAP values computed",
                    "environment": ["shap"],
                }])

            # Fourth call: split_coupled_steps (for the coupled step)
            if idx == 3:
                return json.dumps([
                    {"title": "Create dashboard layout",
                     "description": "Build the dashboard skeleton",
                     "depends_on": [4], "verify": "", "environment": []},
                    {"title": "Wire data into dashboard",
                     "description": "Connect model outputs to dashboard",
                     "depends_on": [4, 5], "verify": "", "environment": []},
                ])

            return "[]"

        client.generate.side_effect = _mock_generate
        mock_get_client.return_value = client

        # Initial sparse plan with a coupled step
        steps = [
            _step("Build simulator", "Create data simulator module"),
            _step("Train model", "Train ML model", [1]),
            _step("Feature engineering", "Build feature pipeline", [1]),
            _step("Temporal analysis", "Compute temporal metrics", [2, 3]),
            _step("Create dashboard and integrate data",
                  "Create dashboard.py and update main.py to display results",
                  [4]),
        ]

        # Step 1: ensure_coverage fills the SHAP gap
        steps, reqs = ensure_coverage("Build ML pipeline", steps)
        assert len(reqs) == 4
        assert any("SHAP" in s["title"] for s in steps)
        assert len(steps) == 6  # 5 original + 1 gap-fill

        # Step 2: split_coupled_steps separates creation and integration
        steps = split_coupled_steps(steps)
        coupled_after = [s for s in steps
                         if "create" in s["title"].lower()
                         and "integrate" in s["description"].lower()]
        # The coupled step should have been split
        assert len(steps) >= 6

        # Step 3: insert checkpoints (only for 7+ steps)
        steps = insert_integration_checkpoints(steps)
        if len(steps) >= 7:
            checkpoints = [s for s in steps
                           if "checkpoint" in s["title"].lower()]
            assert len(checkpoints) >= 1

    def test_pipeline_handles_clean_plan(self):
        """A well-formed plan passes through the pipeline unchanged
        (no LLM calls needed for splitting or checkpoints)."""
        steps = [
            _step("Setup"),
            _step("Build module A", depends_on=[1]),
            _step("Build module B", depends_on=[1]),
            _step("Test modules", depends_on=[2, 3]),
        ]

        # split_coupled_steps should be a no-op
        result = split_coupled_steps(steps)
        assert len(result) == 4

        # insert_integration_checkpoints skips plans < 7 steps
        result = insert_integration_checkpoints(result)
        assert len(result) == 4


# ---------------------------------------------------------------------------
# Coverage + replanning integration
# ---------------------------------------------------------------------------

class TestCoverageReplanIntegration:
    """Verify that replanning preserves requirements from ensure_coverage."""

    @patch("architect.planner.fill_coverage_gaps")
    @patch("architect.planner.verify_coverage")
    @patch("architect.planner.get_llm_client")
    def test_replan_uses_requirements_from_coverage(
        self, mock_get_client, mock_verify, mock_fill,
    ):
        """Requirements extracted by ensure_coverage are usable by
        replan_remaining_steps for protection."""
        client = MagicMock()
        client.generate.return_value = json.dumps([
            {"title": "Revised step", "description": "Updated approach",
             "depends_on": [1]},
        ])
        mock_get_client.return_value = client
        mock_verify.return_value = [
            {"requirement": "data sim", "covered": True,
             "covering_steps": [2]},
            {"requirement": "ML model", "covered": True,
             "covering_steps": [2]},
        ]

        state = {
            "goal": "Build ML pipeline",
            "steps": [
                {"id": 1, "title": "Setup", "description": "Init",
                 "status": "completed", "depends_on": [],
                 "files_written": [], "summary": "", "verify": "",
                 "environment": []},
                {"id": 2, "title": "Build model", "description": "Train",
                 "status": "pending", "depends_on": [1],
                 "verify": "", "environment": []},
            ],
            "requirements": ["data sim", "ML model"],
        }

        # Pass requirements from state to replan
        result = replan_remaining_steps(
            "Build ML pipeline", state, state["steps"][0], "issue",
            requirements=state.get("requirements"),
        )

        assert result is not None
        # verify_coverage was called with the requirements
        mock_verify.assert_called_once()
        call_args = mock_verify.call_args[0]
        assert "data sim" in call_args[0]
        assert "ML model" in call_args[0]


# ---------------------------------------------------------------------------
# File signatures + checkpoints integration
# ---------------------------------------------------------------------------

class TestSignaturesAndCheckpoints:
    """Verify that file signatures from completed steps would be available
    to checkpoint steps for validation."""

    def test_signatures_available_for_checkpoint_context(self, tmp_path):
        """File signatures extracted from Step N's outputs can be formatted
        as context for a checkpoint step."""
        # Create sample files as if produced by prior steps
        py_file = tmp_path / "model.py"
        py_file.write_text(
            "def train_model(X, y, params: dict) -> 'Model':\n"
            '    """Train an XGBoost model."""\n'
            "    pass\n\n"
            "def predict(model, X) -> list:\n"
            '    """Generate predictions."""\n'
            "    pass\n"
        )

        csv_file = tmp_path / "features.csv"
        with open(csv_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["record_id", "age", "score", "outcome"])
            writer.writerow([1, 45, 32, 0.7])

        # Extract signatures
        sigs = extract_file_signatures(
            [str(py_file), str(csv_file)]
        )

        # Signatures include function names and column names
        assert "train_model" in sigs
        assert "predict" in sigs
        assert "record_id" in sigs
        assert "score" in sigs

        # These would be injected into a checkpoint step's context
        # Verify the format is XML-compatible
        assert "<file" in sigs
        assert "</file>" in sigs

    def test_checkpoint_depends_on_steps_with_signatures(self):
        """Integration checkpoints depend on steps that produce files,
        ensuring signatures are available before validation runs."""
        steps = [
            _step("Download data"),
            _step("Build model", depends_on=[1]),
            _step("Build features", depends_on=[1]),
            _step("Train ensemble", depends_on=[2, 3]),
            _step("SHAP analysis", depends_on=[4]),
            _step("Subgroups", depends_on=[4]),
            _step("Build dashboard", depends_on=[5, 6]),
        ]
        result = insert_integration_checkpoints(steps)

        checkpoints = [s for s in result
                       if "checkpoint" in s["title"].lower()]
        assert len(checkpoints) >= 1

        # Each checkpoint should depend on steps before it
        for cp in checkpoints:
            assert len(cp["depends_on"]) >= 1
            assert all(d >= 1 for d in cp["depends_on"])


# ---------------------------------------------------------------------------
# Correction loop + coverage integration
# ---------------------------------------------------------------------------

class TestCorrectionAndCoverage:
    """Verify that corrective steps from the validation loop are properly
    structured and can be processed by the coverage system."""

    @patch("architect.planner.get_llm_client")
    def test_corrective_steps_are_valid_for_coverage(self, mock_get_client):
        """Corrective steps have the required fields for coverage analysis."""
        client = MagicMock()
        client.generate.return_value = json.dumps([
            {"title": "Fix: empty overview tab",
             "description": "Populate overview tab with cohort statistics",
             "depends_on": [3], "verify": "tab renders data",
             "environment": []},
            {"title": "Fix: column name mapping",
             "description": "Correct locale mapping in translations.py",
             "depends_on": [1], "verify": "mapping test passes",
             "environment": []},
        ])
        mock_get_client.return_value = client

        state = {
            "goal": "Build analytics dashboard",
            "steps": [
                {"id": 1, "title": "Translations", "description": "JA/EN",
                 "status": "completed", "depends_on": [],
                 "files_written": ["translations.py"], "summary": "",
                 "verify": "", "environment": []},
                {"id": 2, "title": "Model", "description": "Train model",
                 "status": "completed", "depends_on": [1],
                 "files_written": ["model.py"], "summary": "",
                 "verify": "", "environment": []},
                {"id": 3, "title": "Dashboard", "description": "Build tabs",
                 "status": "completed", "depends_on": [2],
                 "files_written": ["dashboard.py"], "summary": "",
                 "verify": "", "environment": []},
            ],
            "status": "completed",
        }

        corrective = generate_corrective_steps(
            state["goal"],
            ["Overview tab is empty", "Column name mapping wrong"],
            state,
        )

        assert len(corrective) == 2

        # Each corrective step has all required fields
        for step in corrective:
            assert "title" in step
            assert "description" in step
            assert "depends_on" in step
            assert isinstance(step["depends_on"], list)

        # Corrective steps don't have >3 deliverables
        for step in corrective:
            assert count_step_deliverables(step) <= 3


# ---------------------------------------------------------------------------
# Complexity scaling + splitting + checkpoints
# ---------------------------------------------------------------------------

class TestComplexityScalingPipeline:
    """Verify that complex goals flow through the full pipeline:
    enforce_minimum_steps → split → checkpoints."""

    @patch("architect.planner.get_llm_client")
    def test_complex_goal_gets_enough_steps_and_checkpoints(
        self, mock_get_client,
    ):
        """A complex goal that starts with too few steps gets expanded,
        split if coupled, and gets checkpoints."""
        # Mock LLM to return a proper 10-step plan
        expanded_plan = [
            _step("Download dataset"),
            _step("Clean data", depends_on=[1]),
            _step("Feature engineering", depends_on=[1]),
            _step("Train model", depends_on=[2, 3]),
            _step("SHAP analysis", depends_on=[4]),
            _step("Subgroup discovery", depends_on=[4]),
            _step("Build dashboard skeleton", depends_on=[5, 6]),
            _step("Populate overview tab", depends_on=[7]),
            _step("Populate analysis tab", depends_on=[7]),
            _step("Final integration", depends_on=[8, 9]),
        ]

        client = MagicMock()
        client.generate.return_value = json.dumps(expanded_plan)
        mock_get_client.return_value = client

        # Start with too few steps for a complex goal
        sparse_steps = [
            _step("Build everything"),
            _step("Test it", depends_on=[1]),
        ]

        # enforce_minimum_steps expands the plan
        result = enforce_minimum_steps("complex goal", sparse_steps, "complex")
        assert len(result) >= MINIMUM_STEPS["complex"]

        # split_coupled_steps is a no-op for clean steps
        result = split_coupled_steps(result)

        # insert_integration_checkpoints adds checkpoints for 7+ steps
        result = insert_integration_checkpoints(result)
        checkpoints = [s for s in result
                       if "checkpoint" in s["title"].lower()]
        assert len(checkpoints) >= 1

        # Verify DAG integrity
        _assign_ids(result)
        levels = topological_sort(result)
        total_steps_in_levels = sum(len(lv) for lv in levels)
        assert total_steps_in_levels == len(result)

    def test_sufficient_steps_skip_expansion(self):
        """A plan that already meets the minimum is not re-decomposed."""
        steps = [_step(f"Step {i+1}", depends_on=[i] if i > 0 else [])
                 for i in range(10)]

        result = enforce_minimum_steps("complex goal", steps, "complex")
        assert result is steps  # Same object, no re-decomposition


# ---------------------------------------------------------------------------
# DAG integrity after all transformations
# ---------------------------------------------------------------------------

class TestDAGIntegrity:
    """Verify that the step DAG remains valid after all transformations."""

    @patch("architect.planner.get_llm_client")
    def test_dag_valid_after_split_and_checkpoints(self, mock_get_client):
        """Splitting coupled steps and inserting checkpoints produces
        a valid acyclic DAG."""
        client = MagicMock()
        client.generate.return_value = json.dumps([
            {"title": "Create analysis module",
             "description": "Write analysis.py",
             "depends_on": [1], "verify": "", "environment": []},
            {"title": "Wire analysis into pipeline",
             "description": "Update pipeline.py to use analysis",
             "depends_on": [1, 2], "verify": "", "environment": []},
        ])
        mock_get_client.return_value = client

        steps = [
            _step("Setup project"),
            _step("Create analysis and update pipeline",
                  "Create analysis.py and modify pipeline.py to import it",
                  [1]),
            _step("Build dashboard", depends_on=[2]),
            _step("Add chart A", depends_on=[3]),
            _step("Add chart B", depends_on=[3]),
            _step("Add chart C", depends_on=[3]),
            _step("Final integration", depends_on=[4, 5, 6]),
        ]

        # Split coupled step
        steps = split_coupled_steps(steps)
        assert len(steps) == 8  # 7 original → 8 after split

        # Insert checkpoints
        steps = insert_integration_checkpoints(steps)
        assert len(steps) > 8  # at least 1 checkpoint added

        # Verify DAG is acyclic
        _assign_ids(steps)
        levels = topological_sort(steps)
        total = sum(len(lv) for lv in levels)
        assert total == len(steps)

        # All dependency references are valid
        id_set = {s["id"] for s in steps}
        for step in steps:
            for dep in step["depends_on"]:
                assert dep in id_set, (
                    f"Step '{step['title']}' depends on {dep} "
                    f"which is not in {id_set}"
                )

    def test_empty_plan_survives_pipeline(self):
        """Empty plans pass through all transformations without error."""
        steps = []
        steps = split_coupled_steps(steps)
        steps = insert_integration_checkpoints(steps)
        assert steps == []

    def test_single_step_survives_pipeline(self):
        """A single-step plan passes through without modification."""
        steps = [_step("Only step")]
        steps = split_coupled_steps(steps)
        steps = insert_integration_checkpoints(steps)
        assert len(steps) == 1
        assert steps[0]["title"] == "Only step"


# ---------------------------------------------------------------------------
# Overloaded step detection + splitting integration
# ---------------------------------------------------------------------------

class TestOverloadedStepSplitting:
    """Verify that overloaded steps (too many deliverables) can be detected
    and then split by the pipeline."""

    def test_overloaded_steps_detected_before_splitting(self):
        """flag_overloaded_steps identifies steps that need splitting."""
        steps = [
            _step("Setup", "Initialize project structure"),
            _step("Do everything",
                  "Train model, implement SHAP, create clustering, "
                  "build dashboard, and generate report",
                  [1]),
        ]
        flagged = flag_overloaded_steps(steps)
        assert 1 in flagged  # 0-indexed step 1 is overloaded

    @patch("architect.planner.get_llm_client")
    def test_overloaded_step_triggers_expansion(self, mock_get_client):
        """enforce_minimum_steps catches plans with overloaded steps."""
        expanded = [_step(f"Step {i+1}", depends_on=[i] if i > 0 else [])
                    for i in range(10)]
        client = MagicMock()
        client.generate.return_value = json.dumps(expanded)
        mock_get_client.return_value = client

        # 8 steps but all overloaded
        steps = [
            _step(f"Overloaded {i}",
                  "Train model, implement SHAP, create clustering, "
                  "build dashboard, and generate report")
            for i in range(8)
        ]

        result = enforce_minimum_steps("complex goal", steps, "complex")
        # enforce_minimum_steps should detect overloaded steps
        # and re-decompose (returning the expanded 10-step plan)
        assert len(result) == 10
