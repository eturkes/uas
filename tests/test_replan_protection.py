"""Tests for Section 2 — Protect requirements during replanning.

Verifies that replan_remaining_steps() preserves goal coverage when
requirements are provided:
- Protected requirements appear in the REPLAN_PROMPT
- Coverage regression triggers LLM retry (up to 2 retries)
- Exhausted retries fall back to fill_coverage_gaps()
- No retries when all requirements remain covered
- No retries when requirements is None (backward compat)
"""

import json
from unittest.mock import patch, MagicMock, call

import pytest

from architect.planner import (
    replan_remaining_steps,
    _build_replan_prompt,
    verify_coverage,
    fill_coverage_gaps,
)


def _make_state(completed=None, pending=None, requirements=None):
    """Build a minimal state dict for testing."""
    steps = []
    for c in (completed or []):
        steps.append({
            "id": c["id"],
            "title": c.get("title", f"Step {c['id']}"),
            "description": c.get("description", "done"),
            "status": "completed",
            "depends_on": c.get("depends_on", []),
            "files_written": c.get("files_written", []),
            "summary": c.get("summary", ""),
            "verify": "",
            "environment": [],
        })
    for p in (pending or []):
        steps.append({
            "id": p["id"],
            "title": p.get("title", f"Step {p['id']}"),
            "description": p.get("description", "todo"),
            "status": "pending",
            "depends_on": p.get("depends_on", []),
            "verify": "",
            "environment": [],
        })
    state = {"goal": "Build a dashboard", "steps": steps}
    if requirements is not None:
        state["requirements"] = requirements
    return state


# ---------------------------------------------------------------------------
# _build_replan_prompt — protected requirements block
# ---------------------------------------------------------------------------

class TestReplanPromptProtection:
    def test_requirements_appear_in_prompt(self):
        state = _make_state(
            completed=[{"id": 1}],
            pending=[{"id": 2, "depends_on": [1]}],
        )
        reqs = ["XGBoost model", "SHAP explainability", "dashboard tabs"]
        prompt = _build_replan_prompt(
            "Build a dashboard", state, state["steps"][0],
            "mismatch", requirements=reqs,
        )
        assert "<protected_requirements>" in prompt
        assert "XGBoost model" in prompt
        assert "SHAP explainability" in prompt
        assert "dashboard tabs" in prompt

    def test_no_block_when_requirements_empty(self):
        state = _make_state(
            completed=[{"id": 1}],
            pending=[{"id": 2, "depends_on": [1]}],
        )
        prompt = _build_replan_prompt(
            "Build a dashboard", state, state["steps"][0],
            "mismatch", requirements=[],
        )
        assert "<protected_requirements>" not in prompt

    def test_no_block_when_requirements_none(self):
        state = _make_state(
            completed=[{"id": 1}],
            pending=[{"id": 2, "depends_on": [1]}],
        )
        prompt = _build_replan_prompt(
            "Build a dashboard", state, state["steps"][0],
            "mismatch", requirements=None,
        )
        assert "<protected_requirements>" not in prompt

    def test_rule_9_in_prompt(self):
        """The prompt contains the rule about not removing protected coverage."""
        state = _make_state(
            completed=[{"id": 1}],
            pending=[{"id": 2, "depends_on": [1]}],
        )
        prompt = _build_replan_prompt(
            "goal", state, state["steps"][0], "detail",
            requirements=["req A"],
        )
        assert "MUST NOT remove coverage" in prompt


# ---------------------------------------------------------------------------
# replan_remaining_steps — coverage enforcement
# ---------------------------------------------------------------------------

class TestReplanCoverageRetry:
    """Verify that coverage regression triggers retries."""

    @patch("architect.planner.fill_coverage_gaps")
    @patch("architect.planner.verify_coverage")
    @patch("architect.planner.get_llm_client")
    def test_no_retry_when_coverage_preserved(
        self, mock_get_client, mock_verify, mock_fill,
    ):
        """If the first replan covers all requirements, no retry needed."""
        client = MagicMock()
        client.generate.return_value = json.dumps([
            {"title": "New step", "description": "Do X", "depends_on": [1]},
        ])
        mock_get_client.return_value = client
        mock_verify.return_value = [
            {"requirement": "model", "covered": True, "covering_steps": [2]},
            {"requirement": "SHAP", "covered": True, "covering_steps": [2]},
        ]

        state = _make_state(
            completed=[{"id": 1}],
            pending=[{"id": 2, "depends_on": [1]}],
        )
        reqs = ["model", "SHAP"]

        result = replan_remaining_steps(
            "goal", state, state["steps"][0], "detail",
            requirements=reqs,
        )

        assert result is not None
        assert len(result) == 1
        # LLM called only once — no retry
        assert client.generate.call_count == 1
        mock_fill.assert_not_called()

    @patch("architect.planner.fill_coverage_gaps")
    @patch("architect.planner.verify_coverage")
    @patch("architect.planner.get_llm_client")
    def test_retry_on_coverage_regression(
        self, mock_get_client, mock_verify, mock_fill,
    ):
        """If first attempt drops a requirement, retry with dropped info."""
        client = MagicMock()
        # Both attempts return valid steps
        client.generate.return_value = json.dumps([
            {"title": "Step A", "description": "Do A", "depends_on": [1]},
        ])
        mock_get_client.return_value = client

        # First call: SHAP uncovered; second call: all covered
        mock_verify.side_effect = [
            [
                {"requirement": "model", "covered": True, "covering_steps": [2]},
                {"requirement": "SHAP", "covered": False, "covering_steps": []},
            ],
            [
                {"requirement": "model", "covered": True, "covering_steps": [2]},
                {"requirement": "SHAP", "covered": True, "covering_steps": [2]},
            ],
        ]

        state = _make_state(
            completed=[{"id": 1}],
            pending=[{"id": 2, "depends_on": [1]}],
        )

        result = replan_remaining_steps(
            "goal", state, state["steps"][0], "detail",
            requirements=["model", "SHAP"],
        )

        assert result is not None
        # LLM called twice (initial + 1 retry)
        assert client.generate.call_count == 2
        # Second prompt should mention the dropped requirement
        second_prompt = client.generate.call_args_list[1][0][0]
        assert "SHAP" in second_prompt
        mock_fill.assert_not_called()

    @patch("architect.planner.fill_coverage_gaps")
    @patch("architect.planner.verify_coverage")
    @patch("architect.planner.get_llm_client")
    def test_fill_gaps_after_retries_exhausted(
        self, mock_get_client, mock_verify, mock_fill,
    ):
        """After 2 retries, fall back to fill_coverage_gaps."""
        client = MagicMock()
        client.generate.return_value = json.dumps([
            {"title": "Partial", "description": "Does not cover SHAP",
             "depends_on": [1]},
        ])
        mock_get_client.return_value = client

        # All 3 attempts (1 initial + 2 retries) leave SHAP uncovered
        mock_verify.return_value = [
            {"requirement": "model", "covered": True, "covering_steps": [2]},
            {"requirement": "SHAP", "covered": False, "covering_steps": []},
        ]

        mock_fill.return_value = [
            {"title": "SHAP analysis", "description": "Add SHAP",
             "depends_on": [1], "verify": "", "environment": []},
        ]

        state = _make_state(
            completed=[{"id": 1}],
            pending=[{"id": 2, "depends_on": [1]}],
        )

        result = replan_remaining_steps(
            "goal", state, state["steps"][0], "detail",
            requirements=["model", "SHAP"],
        )

        assert result is not None
        # 1 initial + 2 retries = 3 LLM calls
        assert client.generate.call_count == 3
        # fill_coverage_gaps called with the dropped requirements
        mock_fill.assert_called_once()
        args = mock_fill.call_args
        assert "SHAP" in args[0][1]  # uncovered list
        # Result includes both the replan step and the gap-fill step
        assert len(result) == 2

    @patch("architect.planner.fill_coverage_gaps")
    @patch("architect.planner.verify_coverage")
    @patch("architect.planner.get_llm_client")
    def test_no_requirements_skips_coverage_check(
        self, mock_get_client, mock_verify, mock_fill,
    ):
        """Without requirements, replan works as before (no coverage check)."""
        client = MagicMock()
        client.generate.return_value = json.dumps([
            {"title": "New step", "description": "Do X", "depends_on": [1]},
        ])
        mock_get_client.return_value = client

        state = _make_state(
            completed=[{"id": 1}],
            pending=[{"id": 2, "depends_on": [1]}],
        )

        result = replan_remaining_steps(
            "goal", state, state["steps"][0], "detail",
            requirements=None,
        )

        assert result is not None
        assert client.generate.call_count == 1
        mock_verify.assert_not_called()
        mock_fill.assert_not_called()

    @patch("architect.planner.fill_coverage_gaps")
    @patch("architect.planner.verify_coverage")
    @patch("architect.planner.get_llm_client")
    def test_retry_succeeds_on_second_attempt(
        self, mock_get_client, mock_verify, mock_fill,
    ):
        """Coverage fixed on the first retry — no gap fill needed."""
        client = MagicMock()
        # First attempt: partial plan; second attempt: complete plan
        client.generate.side_effect = [
            json.dumps([
                {"title": "Partial", "description": "No SHAP",
                 "depends_on": [1]},
            ]),
            json.dumps([
                {"title": "Full", "description": "With SHAP",
                 "depends_on": [1]},
                {"title": "SHAP step", "description": "SHAP analysis",
                 "depends_on": [1]},
            ]),
        ]
        mock_get_client.return_value = client

        # First: SHAP uncovered; second: all covered
        mock_verify.side_effect = [
            [
                {"requirement": "model", "covered": True, "covering_steps": [2]},
                {"requirement": "SHAP", "covered": False, "covering_steps": []},
            ],
            [
                {"requirement": "model", "covered": True, "covering_steps": [2]},
                {"requirement": "SHAP", "covered": True, "covering_steps": [3]},
            ],
        ]

        state = _make_state(
            completed=[{"id": 1}],
            pending=[{"id": 2, "depends_on": [1]}],
        )

        result = replan_remaining_steps(
            "goal", state, state["steps"][0], "detail",
            requirements=["model", "SHAP"],
        )

        assert result is not None
        assert len(result) == 2
        assert client.generate.call_count == 2
        mock_fill.assert_not_called()


# ---------------------------------------------------------------------------
# State integration — requirements field
# ---------------------------------------------------------------------------

class TestStateRequirementsField:
    """Verify requirements are passed from state to replan."""

    @patch("architect.planner.verify_coverage")
    @patch("architect.planner.get_llm_client")
    def test_requirements_from_state_used_in_prompt(
        self, mock_get_client, mock_verify,
    ):
        """When state has requirements, they appear as protected in prompt."""
        client = MagicMock()
        client.generate.return_value = json.dumps([
            {"title": "Step", "description": "Do it", "depends_on": [1]},
        ])
        mock_get_client.return_value = client
        mock_verify.return_value = [
            {"requirement": "subgroup discovery", "covered": True,
             "covering_steps": [2]},
        ]

        state = _make_state(
            completed=[{"id": 1}],
            pending=[{"id": 2, "depends_on": [1]}],
            requirements=["subgroup discovery"],
        )

        replan_remaining_steps(
            "goal", state, state["steps"][0], "detail",
            requirements=state.get("requirements"),
        )

        prompt = client.generate.call_args[0][0]
        assert "subgroup discovery" in prompt
        assert "<protected_requirements>" in prompt
