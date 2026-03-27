"""Tests for Section 5 — Integration checkpoint steps.

Verifies that insert_integration_checkpoints() detects phase boundaries in
the step DAG and inserts validation checkpoint steps with correct
dependencies.
"""

import pytest

from architect.planner import (
    insert_integration_checkpoints,
    _find_phase_boundaries,
    topological_sort,
    CHECKPOINT_TEMPLATE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _step(title, depends_on=None):
    """Shorthand for creating a step dict."""
    return {
        "title": title,
        "description": f"Do {title}",
        "depends_on": depends_on or [],
        "verify": "",
        "environment": [],
    }


def _make_three_phase_plan():
    """9-step plan with 3 clear phases and parallel work in each.

    Topology:
      Level 0: [1]        - download
      Level 1: [2, 3]     - clean + features (parallel)
      Level 2: [4]        - train model (converges 2, 3)
      Level 3: [5, 6]     - SHAP + subgroups (parallel)
      Level 4: [7]        - build dashboard (converges 5, 6)
      Level 5: [8, 9]     - overview tab + report (parallel)
    """
    return [
        _step("Download dataset"),
        _step("Clean data", [1]),
        _step("Feature engineering", [1]),
        _step("Train model", [2, 3]),
        _step("SHAP analysis", [4]),
        _step("Subgroup discovery", [4]),
        _step("Build dashboard", [5, 6]),
        _step("Overview tab", [7]),
        _step("Final report", [7]),
    ]


def _assign_ids(steps):
    """Assign 1-based IDs to steps (for topological_sort)."""
    for i, s in enumerate(steps):
        s["id"] = i + 1
    return steps


# ---------------------------------------------------------------------------
# _find_phase_boundaries
# ---------------------------------------------------------------------------

class TestFindPhaseBoundaries:
    def test_empty_levels(self):
        assert _find_phase_boundaries([], []) == []

    def test_fewer_than_3_levels(self):
        steps = _assign_ids([_step("A"), _step("B", [1])])
        levels = topological_sort(steps)
        assert len(levels) == 2
        assert _find_phase_boundaries(steps, levels) == []

    def test_parallel_level_detected(self):
        """A level with >=2 steps is detected as a boundary."""
        steps = _assign_ids([
            _step("A"),
            _step("B", [1]),
            _step("C", [1]),       # Level 1: [2, 3] parallel
            _step("D", [2, 3]),    # Level 2: converges
            _step("E", [4]),
            _step("F", [4]),       # Level 3: [5, 6] parallel
            _step("G", [5, 6]),
        ])
        levels = topological_sort(steps)
        # Level 0: [1], Level 1: [2,3], Level 2: [4], Level 3: [5,6], Level 4: [7]
        boundaries = _find_phase_boundaries(steps, levels)
        assert 1 in boundaries  # after the [2,3] parallel level

    def test_convergence_detected(self):
        """A step depending on >=2 prior steps triggers a boundary."""
        steps = _assign_ids([
            _step("A"),
            _step("B"),                    # Level 0: [1, 2]
            _step("C", [1]),
            _step("D", [2]),               # Level 1: [3, 4]
            _step("E", [3, 4]),            # Level 2: converges
            _step("F", [5]),
            _step("G", [5]),               # Level 3: [6, 7]
            _step("H", [6, 7]),            # Level 4: converges
        ])
        levels = topological_sort(steps)
        boundaries = _find_phase_boundaries(steps, levels)
        assert len(boundaries) >= 1

    def test_minimum_spacing_enforced(self):
        """Consecutive candidate levels should be spaced >=2 apart."""
        steps = _assign_ids([
            _step("A"),
            _step("B", [1]),
            _step("C", [1]),       # Level 1: [2,3]
            _step("D", [2]),
            _step("E", [3]),       # Level 2: [4,5]
            _step("F", [4, 5]),    # Level 3: converges
            _step("G", [6]),
            _step("H", [6]),       # Level 4: [7,8]
            _step("I", [7, 8]),
        ])
        levels = topological_sort(steps)
        boundaries = _find_phase_boundaries(steps, levels)
        for i in range(1, len(boundaries)):
            assert boundaries[i] - boundaries[i - 1] >= 2

    def test_linear_plan_no_boundaries(self):
        """A purely linear plan (all levels have 1 step) has no boundaries."""
        steps = _assign_ids([
            _step("A"),
            _step("B", [1]),
            _step("C", [2]),
            _step("D", [3]),
            _step("E", [4]),
        ])
        levels = topological_sort(steps)
        assert all(len(lv) == 1 for lv in levels)
        boundaries = _find_phase_boundaries(steps, levels)
        assert boundaries == []


# ---------------------------------------------------------------------------
# insert_integration_checkpoints — small plans unchanged
# ---------------------------------------------------------------------------

class TestCheckpointsSmallPlans:
    def test_empty_list(self):
        assert insert_integration_checkpoints([]) == []

    def test_single_step(self):
        steps = [_step("Only step")]
        result = insert_integration_checkpoints(steps)
        assert len(result) == 1

    def test_six_steps_unchanged(self):
        """Plans with < 7 steps get no checkpoints."""
        steps = [
            _step("A"),
            _step("B", [1]),
            _step("C", [1]),
            _step("D", [2, 3]),
            _step("E", [4]),
            _step("F", [5]),
        ]
        result = insert_integration_checkpoints(steps)
        assert len(result) == 6


# ---------------------------------------------------------------------------
# insert_integration_checkpoints — checkpoint insertion
# ---------------------------------------------------------------------------

class TestCheckpointInsertion:
    def test_nine_step_three_phases_gets_two_checkpoints(self):
        """A 9-step plan with 3 phases gets 2 checkpoint steps inserted."""
        steps = _make_three_phase_plan()
        result = insert_integration_checkpoints(steps)

        # Original 9 + 2 checkpoints = 11
        assert len(result) == 11

        # Last 2 steps are checkpoints
        cp1 = result[9]
        cp2 = result[10]
        assert "checkpoint" in cp1["title"].lower()
        assert "checkpoint" in cp2["title"].lower()

    def test_checkpoint_depends_on_preceding_phase(self):
        """Each checkpoint depends on all steps in its preceding phase."""
        steps = _make_three_phase_plan()
        result = insert_integration_checkpoints(steps)

        checkpoints = [s for s in result if "checkpoint" in s["title"].lower()]
        assert len(checkpoints) >= 1

        # First checkpoint should depend on steps from the first phase
        cp1 = checkpoints[0]
        # Must depend on at least steps 1, 2, 3 (the first phase)
        assert 1 in cp1["depends_on"]
        assert 2 in cp1["depends_on"]
        assert 3 in cp1["depends_on"]

    def test_following_steps_gain_checkpoint_dependency(self):
        """Steps after a boundary that depend on preceding steps also
        depend on the checkpoint."""
        steps = _make_three_phase_plan()
        result = insert_integration_checkpoints(steps)

        # Step 4 (train model) depends on [2, 3] which are in the first phase.
        # It should also depend on the first checkpoint (step 10).
        step4 = result[3]
        assert 10 in step4["depends_on"]

    def test_checkpoint_description_is_validation_only(self):
        """Checkpoint descriptions state they must not modify files."""
        steps = _make_three_phase_plan()
        result = insert_integration_checkpoints(steps)

        checkpoints = [s for s in result if "checkpoint" in s["title"].lower()]
        for cp in checkpoints:
            assert "must not modify any files" in cp["description"]

    def test_no_id_field_in_output(self):
        """Output steps should not have temporary 'id' fields."""
        steps = _make_three_phase_plan()
        result = insert_integration_checkpoints(steps)
        for step in result:
            assert "id" not in step

    def test_checkpoint_has_required_fields(self):
        """Checkpoint steps have all fields needed by add_steps."""
        steps = _make_three_phase_plan()
        result = insert_integration_checkpoints(steps)

        checkpoints = [s for s in result if "checkpoint" in s["title"].lower()]
        for cp in checkpoints:
            assert "title" in cp
            assert "description" in cp
            assert "depends_on" in cp
            assert "verify" in cp
            assert "environment" in cp

    def test_resulting_dag_is_valid(self):
        """The output DAG should be acyclic (topological sort succeeds)."""
        steps = _make_three_phase_plan()
        result = insert_integration_checkpoints(steps)

        # Assign IDs and verify topological sort works
        for i, s in enumerate(result):
            s["id"] = i + 1
        levels = topological_sort(result)
        assert sum(len(lv) for lv in levels) == len(result)

        # Clean up
        for s in result:
            del s["id"]


# ---------------------------------------------------------------------------
# insert_integration_checkpoints — fallback midpoint
# ---------------------------------------------------------------------------

class TestCheckpointFallback:
    def test_linear_plan_gets_midpoint_checkpoint(self):
        """A linear 8-step plan with no natural boundaries gets a
        midpoint checkpoint as fallback."""
        steps = [
            _step("Step 1"),
            _step("Step 2", [1]),
            _step("Step 3", [2]),
            _step("Step 4", [3]),
            _step("Step 5", [4]),
            _step("Step 6", [5]),
            _step("Step 7", [6]),
            _step("Step 8", [7]),
        ]
        result = insert_integration_checkpoints(steps)

        # Should have at least 1 checkpoint
        checkpoints = [s for s in result if "checkpoint" in s["title"].lower()]
        assert len(checkpoints) >= 1
        assert len(result) > len(steps)


# ---------------------------------------------------------------------------
# insert_integration_checkpoints — complex scenario
# ---------------------------------------------------------------------------

class TestCheckpointComplexPlan:
    def test_twelve_step_plan(self):
        """A 12-step plan gets at least one checkpoint."""
        steps = [
            _step("Setup"),                          # 1
            _step("Download data", [1]),             # 2
            _step("Clean data", [1]),                # 3
            _step("Feature engineering", [2, 3]),    # 4
            _step("Train model A", [4]),             # 5
            _step("Train model B", [4]),             # 6
            _step("Ensemble models", [5, 6]),        # 7
            _step("Build dashboard", [7]),           # 8
            _step("Add overview tab", [8]),          # 9
            _step("Add analysis tab", [8]),          # 10
            _step("Add patient tab", [8]),           # 11
            _step("Final integration", [9, 10, 11]), # 12
        ]
        result = insert_integration_checkpoints(steps)

        checkpoints = [s for s in result if "checkpoint" in s["title"].lower()]
        assert len(checkpoints) >= 1
        assert len(result) > 12

    def test_seven_step_minimum_gets_checkpoint(self):
        """The minimum threshold (7 steps) with parallel work gets a
        checkpoint."""
        steps = [
            _step("A"),
            _step("B", [1]),
            _step("C", [1]),
            _step("D", [2, 3]),
            _step("E", [4]),
            _step("F", [4]),
            _step("G", [5, 6]),
        ]
        result = insert_integration_checkpoints(steps)
        assert len(result) > 7
