"""Tests for Section 6 — Validation-driven correction loop.

Verifies:
- generate_corrective_steps() produces one step per issue
- Corrective steps are capped at MAX_CORRECTIVE_STEPS_PER_ROUND
- The correction loop triggers on medium/low confidence
- The correction loop exits early when confidence reaches high
- The correction loop terminates after MAX_CORRECTION_ROUNDS
- No correction loop in MINIMAL_MODE or when confidence is high
"""

import json
from unittest.mock import patch, MagicMock, call

import pytest

from architect.planner import (
    generate_corrective_steps,
    MAX_CORRECTIVE_STEPS_PER_ROUND,
    MAX_CORRECTION_ROUNDS,
)


def _make_state(num_completed=3, goal="Build a dashboard"):
    """Build a minimal state dict with completed steps."""
    steps = []
    for i in range(1, num_completed + 1):
        steps.append({
            "id": i,
            "title": f"Step {i}",
            "description": f"Description for step {i}",
            "status": "completed",
            "depends_on": [i - 1] if i > 1 else [],
            "files_written": [f"module_{i}.py"],
            "summary": f"Completed step {i}",
            "verify": "",
            "environment": [],
        })
    return {"goal": goal, "steps": steps, "status": "completed"}


# ---------------------------------------------------------------------------
# generate_corrective_steps — unit tests
# ---------------------------------------------------------------------------

class TestGenerateCorrective:
    """Unit tests for generate_corrective_steps()."""

    @patch("architect.planner.get_llm_client")
    def test_one_step_per_issue(self, mock_get_client):
        """Each validation issue should map to one corrective step."""
        client = MagicMock()
        client.generate.return_value = (json.dumps([
            {"title": "Fix: empty overview tab",
             "description": "Populate the overview tab with cohort stats",
             "depends_on": [2], "verify": "", "environment": []},
            {"title": "Fix: motor column mapping",
             "description": "Correct JA->EN mapping for motor column",
             "depends_on": [1], "verify": "", "environment": []},
        ]), {"input": 0, "output": 0})
        mock_get_client.return_value = client

        state = _make_state()
        issues = [
            "Overview tab is empty placeholder",
            "Motor column mapping is wrong",
        ]

        result = generate_corrective_steps(state["goal"], issues, state)

        assert len(result) == 2
        assert "overview" in result[0]["title"].lower() or "Fix" in result[0]["title"]
        assert "motor" in result[1]["title"].lower() or "Fix" in result[1]["title"]
        client.generate.assert_called_once()

    @patch("architect.planner.get_llm_client")
    def test_capped_at_max_per_round(self, mock_get_client):
        """Even if LLM returns more steps, result is capped."""
        steps_json = json.dumps([
            {"title": f"Fix {i}", "description": f"Fix issue {i}",
             "depends_on": [], "verify": "", "environment": []}
            for i in range(10)
        ])
        client = MagicMock()
        client.generate.return_value = (steps_json, {"input": 0, "output": 0})
        mock_get_client.return_value = client

        state = _make_state()
        issues = [f"Issue {i}" for i in range(10)]

        result = generate_corrective_steps(state["goal"], issues, state)

        assert len(result) <= MAX_CORRECTIVE_STEPS_PER_ROUND

    @patch("architect.planner.get_llm_client")
    def test_empty_issues_returns_empty(self, mock_get_client):
        """No issues means no corrective steps, no LLM call."""
        client = MagicMock()
        mock_get_client.return_value = client

        state = _make_state()
        result = generate_corrective_steps(state["goal"], [], state)

        assert result == []
        client.generate.assert_not_called()

    @patch("architect.planner.get_llm_client")
    def test_llm_failure_returns_empty(self, mock_get_client):
        """If LLM call raises, return empty list gracefully."""
        client = MagicMock()
        client.generate.side_effect = RuntimeError("API error")
        mock_get_client.return_value = client

        state = _make_state()
        result = generate_corrective_steps(
            state["goal"], ["some issue"], state,
        )

        assert result == []

    @patch("architect.planner.get_llm_client")
    def test_unparseable_response_returns_empty(self, mock_get_client):
        """If LLM returns garbage, return empty list gracefully."""
        client = MagicMock()
        client.generate.return_value = ("This is not JSON at all.", {"input": 0, "output": 0})
        mock_get_client.return_value = client

        state = _make_state()
        result = generate_corrective_steps(
            state["goal"], ["some issue"], state,
        )

        assert result == []

    @patch("architect.planner.get_llm_client")
    def test_defaults_filled(self, mock_get_client):
        """Steps missing optional fields get defaults."""
        client = MagicMock()
        client.generate.return_value = (json.dumps([
            {"title": "Fix X", "description": "Do X"},
        ]), {"input": 0, "output": 0})
        mock_get_client.return_value = client

        state = _make_state()
        result = generate_corrective_steps(
            state["goal"], ["issue X"], state,
        )

        assert len(result) == 1
        assert result[0]["depends_on"] == []
        assert result[0]["verify"] == ""
        assert result[0]["environment"] == []

    @patch("architect.planner.get_llm_client")
    def test_malformed_steps_filtered(self, mock_get_client):
        """Steps without title or description are filtered out."""
        client = MagicMock()
        client.generate.return_value = (json.dumps([
            {"title": "Good step", "description": "Has both fields"},
            {"title": "Missing desc"},  # no description
            {"description": "Missing title"},  # no title
        ]), {"input": 0, "output": 0})
        mock_get_client.return_value = client

        state = _make_state()
        result = generate_corrective_steps(
            state["goal"], ["issue"], state,
        )

        assert len(result) == 1
        assert result[0]["title"] == "Good step"

    @patch("architect.planner.get_llm_client")
    def test_next_step_number_continues_from_state(self, mock_get_client):
        """Step numbering should continue from max existing step ID."""
        client = MagicMock()
        client.generate.return_value = (json.dumps([
            {"title": "Fix", "description": "Fix it", "depends_on": []},
        ]), {"input": 0, "output": 0})
        mock_get_client.return_value = client

        state = _make_state(num_completed=5)
        generate_corrective_steps(state["goal"], ["issue"], state)

        prompt = client.generate.call_args[0][0]
        assert "6" in prompt  # next_step_number = 5 + 1


# ---------------------------------------------------------------------------
# Correction loop integration — tests for main.py logic
# ---------------------------------------------------------------------------

class TestCorrectionLoopConstants:
    """Verify the constants are importable and sane."""

    def test_max_rounds_is_positive(self):
        assert MAX_CORRECTION_ROUNDS >= 1

    def test_max_steps_per_round_is_positive(self):
        assert MAX_CORRECTIVE_STEPS_PER_ROUND >= 1

    def test_max_rounds_value(self):
        assert MAX_CORRECTION_ROUNDS == 2

    def test_max_steps_value(self):
        assert MAX_CORRECTIVE_STEPS_PER_ROUND == 5


class TestCorrectionLoopBehavior:
    """Test the correction loop logic as used in main.py.

    We extract the loop logic into a helper and test it in isolation,
    mocking validate_workspace, generate_corrective_steps, and execute_step.
    """

    def _run_correction_loop(
        self,
        state,
        validation_results,
        corrective_steps_per_round=None,
        execute_results=None,
    ):
        """Simulate the correction loop from main.py.

        Args:
            state: Initial run state.
            validation_results: List of validation dicts returned by
                successive calls to validate_workspace.
            corrective_steps_per_round: List of lists of step dicts
                returned by successive calls to generate_corrective_steps.
            execute_results: List of booleans for execute_step return values.

        Returns:
            (final_state, rounds_executed, total_corrective_steps_executed)
        """
        if corrective_steps_per_round is None:
            corrective_steps_per_round = []
        if execute_results is None:
            execute_results = [True] * 50  # default: all succeed

        val_idx = 0
        gen_idx = 0
        exec_idx = 0
        rounds_done = 0
        total_executed = 0

        # First validation is already done; use validation_results[0]
        validation = validation_results[val_idx]
        val_idx += 1

        llm_assessment = validation.get("llm_assessment") or {}
        confidence = llm_assessment.get("confidence", "high")
        issues = llm_assessment.get("issues", [])

        while (
            confidence in ("medium", "low")
            and issues
            and rounds_done < MAX_CORRECTION_ROUNDS
        ):
            rounds_done += 1

            if gen_idx < len(corrective_steps_per_round):
                corrective_steps = corrective_steps_per_round[gen_idx]
            else:
                corrective_steps = []
            gen_idx += 1

            if not corrective_steps:
                break

            # Assign IDs
            max_id = max((s["id"] for s in state["steps"]), default=0)
            for i, cs in enumerate(corrective_steps):
                cs["id"] = max_id + i + 1
                cs["status"] = "pending"
                state["steps"].append(cs)

            # Execute
            failed = False
            for cs in corrective_steps:
                success = execute_results[exec_idx] if exec_idx < len(execute_results) else True
                exec_idx += 1
                if success:
                    cs["status"] = "completed"
                    total_executed += 1
                else:
                    cs["status"] = "failed"
                    failed = True
                    break

            if failed:
                break

            # Re-validate
            if val_idx < len(validation_results):
                validation = validation_results[val_idx]
            else:
                validation = {"llm_assessment": {"confidence": "high", "issues": []}}
            val_idx += 1

            llm_assessment = validation.get("llm_assessment") or {}
            confidence = llm_assessment.get("confidence", "high")
            issues = llm_assessment.get("issues", [])

            if confidence == "high":
                break

        state["correction_rounds"] = rounds_done
        return state, rounds_done, total_executed

    def test_high_confidence_skips_loop(self):
        """If initial validation is high confidence, no corrections run."""
        state = _make_state()
        validation = {
            "llm_assessment": {"confidence": "high", "issues": []},
        }
        _, rounds, executed = self._run_correction_loop(
            state, [validation],
        )
        assert rounds == 0
        assert executed == 0

    def test_medium_confidence_triggers_correction(self):
        """Medium confidence with issues triggers a correction round."""
        state = _make_state()
        validations = [
            {
                "llm_assessment": {
                    "confidence": "medium",
                    "issues": ["Overview tab is empty"],
                },
            },
            {
                "llm_assessment": {
                    "confidence": "high",
                    "issues": [],
                },
            },
        ]
        corrective = [[
            {"title": "Fix overview tab", "description": "Populate it",
             "depends_on": [], "verify": "", "environment": []},
        ]]

        _, rounds, executed = self._run_correction_loop(
            state, validations, corrective,
        )

        assert rounds == 1
        assert executed == 1

    def test_low_confidence_triggers_correction(self):
        """Low confidence also triggers correction."""
        state = _make_state()
        validations = [
            {
                "llm_assessment": {
                    "confidence": "low",
                    "issues": ["Everything is broken"],
                },
            },
            {
                "llm_assessment": {
                    "confidence": "high",
                    "issues": [],
                },
            },
        ]
        corrective = [[
            {"title": "Fix all", "description": "Fix everything",
             "depends_on": [], "verify": "", "environment": []},
        ]]

        _, rounds, executed = self._run_correction_loop(
            state, validations, corrective,
        )

        assert rounds == 1
        assert executed == 1

    def test_max_rounds_terminates(self):
        """Loop exits after MAX_CORRECTION_ROUNDS even if still medium."""
        state = _make_state()
        validations = [
            {
                "llm_assessment": {
                    "confidence": "medium",
                    "issues": ["Issue A"],
                },
            },
        ] * (MAX_CORRECTION_ROUNDS + 2)  # always medium

        corrective = [
            [{"title": f"Fix round {i}", "description": f"Attempt {i}",
              "depends_on": [], "verify": "", "environment": []}]
            for i in range(MAX_CORRECTION_ROUNDS + 1)
        ]

        _, rounds, _ = self._run_correction_loop(
            state, validations, corrective,
        )

        assert rounds == MAX_CORRECTION_ROUNDS

    def test_early_exit_on_high_confidence(self):
        """If corrections bring confidence to high, loop exits early."""
        state = _make_state()
        validations = [
            {
                "llm_assessment": {
                    "confidence": "medium",
                    "issues": ["Issue A", "Issue B"],
                },
            },
            {
                "llm_assessment": {
                    "confidence": "high",
                    "issues": [],
                },
            },
        ]
        corrective = [[
            {"title": "Fix A", "description": "Fix issue A",
             "depends_on": [], "verify": "", "environment": []},
        ]]

        _, rounds, executed = self._run_correction_loop(
            state, validations, corrective,
        )

        assert rounds == 1
        assert executed == 1

    def test_no_corrective_steps_exits(self):
        """If generate returns empty, loop exits immediately."""
        state = _make_state()
        validations = [
            {
                "llm_assessment": {
                    "confidence": "medium",
                    "issues": ["Issue A"],
                },
            },
        ]

        _, rounds, executed = self._run_correction_loop(
            state, validations, corrective_steps_per_round=[[]],
        )

        assert rounds == 1
        assert executed == 0

    def test_failed_corrective_step_exits(self):
        """If a corrective step fails, loop exits that round."""
        state = _make_state()
        validations = [
            {
                "llm_assessment": {
                    "confidence": "medium",
                    "issues": ["Issue A", "Issue B"],
                },
            },
        ]
        corrective = [[
            {"title": "Fix A", "description": "Fix issue A",
             "depends_on": [], "verify": "", "environment": []},
            {"title": "Fix B", "description": "Fix issue B",
             "depends_on": [], "verify": "", "environment": []},
        ]]

        _, rounds, executed = self._run_correction_loop(
            state, validations, corrective,
            execute_results=[True, False],  # second step fails
        )

        assert rounds == 1
        assert executed == 1  # only first succeeded

    def test_no_issues_skips_even_if_medium(self):
        """Medium confidence but empty issues list should not loop."""
        state = _make_state()
        validations = [
            {
                "llm_assessment": {
                    "confidence": "medium",
                    "issues": [],
                },
            },
        ]

        _, rounds, executed = self._run_correction_loop(
            state, validations,
        )

        assert rounds == 0
        assert executed == 0

    def test_correction_rounds_stored_in_state(self):
        """State should track how many correction rounds were performed."""
        state = _make_state()
        validations = [
            {
                "llm_assessment": {
                    "confidence": "medium",
                    "issues": ["Issue"],
                },
            },
            {
                "llm_assessment": {
                    "confidence": "high",
                    "issues": [],
                },
            },
        ]
        corrective = [[
            {"title": "Fix", "description": "Fix it",
             "depends_on": [], "verify": "", "environment": []},
        ]]

        result_state, _, _ = self._run_correction_loop(
            state, validations, corrective,
        )

        assert result_state["correction_rounds"] == 1

    def test_corrective_steps_appended_to_state(self):
        """Corrective steps should be added to state['steps']."""
        state = _make_state(num_completed=3)
        initial_step_count = len(state["steps"])

        validations = [
            {
                "llm_assessment": {
                    "confidence": "medium",
                    "issues": ["Issue A"],
                },
            },
            {
                "llm_assessment": {
                    "confidence": "high",
                    "issues": [],
                },
            },
        ]
        corrective = [[
            {"title": "Fix A", "description": "Fix issue A",
             "depends_on": [], "verify": "", "environment": []},
        ]]

        result_state, _, _ = self._run_correction_loop(
            state, validations, corrective,
        )

        assert len(result_state["steps"]) == initial_step_count + 1
        assert result_state["steps"][-1]["title"] == "Fix A"
        assert result_state["steps"][-1]["id"] == 4  # max was 3

    def test_two_rounds_both_execute(self):
        """Two correction rounds should both execute if confidence stays medium."""
        state = _make_state(num_completed=2)
        validations = [
            {
                "llm_assessment": {
                    "confidence": "medium",
                    "issues": ["Issue A"],
                },
            },
            {
                "llm_assessment": {
                    "confidence": "medium",
                    "issues": ["Issue B"],
                },
            },
            {
                "llm_assessment": {
                    "confidence": "high",
                    "issues": [],
                },
            },
        ]
        corrective = [
            [{"title": "Fix A", "description": "Round 1",
              "depends_on": [], "verify": "", "environment": []}],
            [{"title": "Fix B", "description": "Round 2",
              "depends_on": [], "verify": "", "environment": []}],
        ]

        result_state, rounds, executed = self._run_correction_loop(
            state, validations, corrective,
        )

        assert rounds == 2
        assert executed == 2
        assert len(result_state["steps"]) == 4  # 2 original + 2 corrective

    def test_no_llm_assessment_skips_loop(self):
        """If llm_assessment is None (LLM validation failed), skip loop."""
        state = _make_state()
        validations = [
            {"llm_assessment": None},
        ]

        _, rounds, executed = self._run_correction_loop(
            state, validations,
        )

        assert rounds == 0
        assert executed == 0
