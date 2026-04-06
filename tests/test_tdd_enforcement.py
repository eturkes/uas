"""Tests for TDD enforcement loop and re-prompting (Task 4.8).

Verifies that decompositions lacking test steps are detected by
``validate_tdd_coverage`` and corrected via ``fix_tdd_violations``,
including the 2-attempt retry loop in the architect's post-decomposition
validation pass.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from architect.planner import (
    fix_tdd_violations,
    validate_tdd_coverage,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_mock_client(response_text: str) -> MagicMock:
    """Return a mock LLM client whose generate() returns *response_text*."""
    client = MagicMock()
    client.generate.return_value = (response_text, {"input_tokens": 10, "output_tokens": 20})
    return client


def _plan_without_tests() -> list[dict]:
    """A plan with two implementation steps and no test steps."""
    return [
        {
            "title": "Implement CSV parser",
            "description": "Build a CSV parser module.",
            "depends_on": [],
            "outputs": ["csv_parser.py"],
        },
        {
            "title": "Implement formatter",
            "description": "Build the output formatter.",
            "depends_on": [1],
            "outputs": ["formatter.py"],
        },
    ]


def _valid_tdd_plan() -> list[dict]:
    """A plan where every implementation step has a preceding test step."""
    return [
        {
            "title": "test: Write tests for CSV parser",
            "description": "Write pytest tests for the CSV parser module.",
            "depends_on": [],
            "outputs": ["test_csv_parser.py"],
        },
        {
            "title": "Implement CSV parser",
            "description": "Build a CSV parser module.",
            "depends_on": [1],
            "outputs": ["csv_parser.py"],
        },
        {
            "title": "test: Write tests for formatter",
            "description": "Write pytest tests for the output formatter.",
            "depends_on": [],
            "outputs": ["test_formatter.py"],
        },
        {
            "title": "Implement formatter",
            "description": "Build the output formatter.",
            "depends_on": [2, 3],
            "outputs": ["formatter.py"],
        },
    ]


# ---------------------------------------------------------------------------
# Tests: validate_tdd_coverage detects missing test steps
# ---------------------------------------------------------------------------

class TestMissingTestStepsDetected:
    """Plans without test steps must produce violations."""

    def test_single_impl_step_no_test(self):
        steps = [
            {
                "title": "Build parser",
                "description": "Implement the parser.",
                "depends_on": [],
                "outputs": ["parser.py"],
            },
        ]
        violations = validate_tdd_coverage(steps)
        assert len(violations) == 1
        assert "no preceding test step" in violations[0]

    def test_multiple_impl_steps_no_tests(self):
        violations = validate_tdd_coverage(_plan_without_tests())
        # Step 1 has no test dep, step 2 depends on step 1 (not a test step)
        assert len(violations) == 2
        assert all("no preceding test step" in v for v in violations)

    def test_impl_depending_on_wrong_step(self):
        """Implementation depends on another impl step, not a test step."""
        steps = [
            {
                "title": "test: Write tests for parser",
                "description": "Write pytest tests for the parser.",
                "depends_on": [],
                "outputs": ["test_parser.py"],
            },
            {
                "title": "Implement parser",
                "description": "Build parser.",
                "depends_on": [1],
                "outputs": ["parser.py"],
            },
            {
                "title": "Implement formatter",
                "description": "Build formatter.",
                "depends_on": [2],  # depends on impl, not a test step
                "outputs": ["formatter.py"],
            },
        ]
        violations = validate_tdd_coverage(steps)
        assert len(violations) == 1
        assert "Step 3" in violations[0]

    def test_exempt_steps_not_flagged(self):
        steps = [
            {
                "title": "Setup environment",
                "description": "Configure project dependencies.",
                "depends_on": [],
                "outputs": ["requirements.txt"],
            },
            {
                "title": "Download dataset",
                "description": "Fetch training data.",
                "depends_on": [],
                "outputs": ["data.csv"],
            },
        ]
        assert validate_tdd_coverage(steps) == []

    def test_valid_plan_no_violations(self):
        assert validate_tdd_coverage(_valid_tdd_plan()) == []


# ---------------------------------------------------------------------------
# Tests: fix_tdd_violations re-prompts the planner
# ---------------------------------------------------------------------------

class TestFixTddViolations:
    """fix_tdd_violations calls the LLM and returns corrected steps."""

    @patch("architect.planner.get_event_log")
    @patch("architect.planner.get_llm_client")
    def test_successful_fix(self, mock_get_client, mock_get_event_log):
        """LLM returns a valid fixed plan -> returned as-is."""
        fixed = _valid_tdd_plan()
        mock_get_client.return_value = _make_mock_client(json.dumps(fixed))
        mock_get_event_log.return_value = MagicMock()

        original = _plan_without_tests()
        violations = validate_tdd_coverage(original)
        result = fix_tdd_violations("Build a CSV tool", original, violations)

        assert len(result) == 4
        assert result[0]["title"].lower().startswith("test:")
        mock_get_client.return_value.generate.assert_called_once()

    @patch("architect.planner.get_event_log")
    @patch("architect.planner.get_llm_client")
    def test_fix_prompt_contains_violations(self, mock_get_client, mock_get_event_log):
        """The prompt sent to the LLM includes the violation messages."""
        fixed = _valid_tdd_plan()
        client = _make_mock_client(json.dumps(fixed))
        mock_get_client.return_value = client
        mock_get_event_log.return_value = MagicMock()

        original = _plan_without_tests()
        violations = validate_tdd_coverage(original)
        fix_tdd_violations("Build a CSV tool", original, violations)

        prompt = client.generate.call_args[0][0]
        for v in violations:
            assert v in prompt

    @patch("architect.planner.get_event_log")
    @patch("architect.planner.get_llm_client")
    def test_fix_prompt_contains_current_plan(self, mock_get_client, mock_get_event_log):
        """The prompt includes the original plan as JSON."""
        fixed = _valid_tdd_plan()
        mock_get_client.return_value = _make_mock_client(json.dumps(fixed))
        mock_get_event_log.return_value = MagicMock()

        original = _plan_without_tests()
        violations = validate_tdd_coverage(original)
        fix_tdd_violations("Build a CSV tool", original, violations)

        prompt = mock_get_client.return_value.generate.call_args[0][0]
        assert "Implement CSV parser" in prompt
        assert "Implement formatter" in prompt

    @patch("architect.planner.get_event_log")
    @patch("architect.planner.get_llm_client")
    def test_empty_response_keeps_original(self, mock_get_client, mock_get_event_log):
        """If the LLM returns an empty step list, the original plan is kept."""
        mock_get_client.return_value = _make_mock_client("[]")
        mock_get_event_log.return_value = MagicMock()

        original = _plan_without_tests()
        violations = validate_tdd_coverage(original)
        result = fix_tdd_violations("Build a CSV tool", original, violations)

        assert result is original

    @patch("architect.planner.get_event_log")
    @patch("architect.planner.get_llm_client")
    def test_unparseable_response_raises(self, mock_get_client, mock_get_event_log):
        """If the LLM returns garbage, parse_steps_json raises ValueError."""
        mock_get_client.return_value = _make_mock_client("not valid json at all")
        mock_get_event_log.return_value = MagicMock()

        original = _plan_without_tests()
        violations = validate_tdd_coverage(original)
        with pytest.raises(ValueError, match="Could not parse steps"):
            fix_tdd_violations("Build a CSV tool", original, violations)

    @patch("architect.planner.get_event_log")
    @patch("architect.planner.get_llm_client")
    def test_missing_fields_keeps_original(self, mock_get_client, mock_get_event_log):
        """Steps missing title/description -> original kept."""
        bad_steps = [{"outputs": ["foo.py"]}]
        mock_get_client.return_value = _make_mock_client(json.dumps(bad_steps))
        mock_get_event_log.return_value = MagicMock()

        original = _plan_without_tests()
        violations = validate_tdd_coverage(original)
        result = fix_tdd_violations("Build a CSV tool", original, violations)

        assert result is original

    @patch("architect.planner.get_event_log")
    @patch("architect.planner.get_llm_client")
    def test_zero_indexed_deps_normalized(self, mock_get_client, mock_get_event_log):
        """0-based depends_on refs are incremented to 1-based."""
        fixed = [
            {
                "title": "test: Write tests for parser",
                "description": "Write pytest tests for the parser.",
                "depends_on": [],
                "outputs": ["test_parser.py"],
            },
            {
                "title": "Implement parser",
                "description": "Build parser.",
                "depends_on": [0],  # 0-indexed reference
                "outputs": ["parser.py"],
            },
        ]
        mock_get_client.return_value = _make_mock_client(json.dumps(fixed))
        mock_get_event_log.return_value = MagicMock()

        original = _plan_without_tests()
        violations = validate_tdd_coverage(original)
        result = fix_tdd_violations("Build a tool", original, violations)

        # Should be normalized to 1-based
        assert result[1]["depends_on"] == [1]

    @patch("architect.planner.get_event_log")
    @patch("architect.planner.get_llm_client")
    def test_spec_included_in_prompt(self, mock_get_client, mock_get_event_log):
        """When a spec is provided, it appears in the prompt."""
        fixed = _valid_tdd_plan()
        client = _make_mock_client(json.dumps(fixed))
        mock_get_client.return_value = client
        mock_get_event_log.return_value = MagicMock()

        original = _plan_without_tests()
        violations = validate_tdd_coverage(original)
        fix_tdd_violations(
            "Build a tool", original, violations,
            spec="Must support CSV and JSON formats.",
        )

        prompt = client.generate.call_args[0][0]
        assert "Must support CSV and JSON formats." in prompt


# ---------------------------------------------------------------------------
# Tests: validate + fix loop (simulates architect/main.py lines 5858-5878)
# ---------------------------------------------------------------------------

class TestTddEnforcementLoop:
    """Simulate the post-decomposition TDD validation loop."""

    def _run_enforcement_loop(
        self, steps: list[dict], fix_fn, max_attempts: int = 2,
    ) -> tuple[list[dict], list[str]]:
        """Replicate the enforcement logic from architect/main.py:5858-5878."""
        tdd_violations = validate_tdd_coverage(steps)
        for _ in range(max_attempts):
            if not tdd_violations:
                break
            steps = fix_fn("test goal", steps, tdd_violations)
            tdd_violations = validate_tdd_coverage(steps)
        return steps, tdd_violations

    @patch("architect.planner.get_event_log")
    @patch("architect.planner.get_llm_client")
    def test_no_violations_no_reprompt(self, mock_get_client, mock_get_event_log):
        """A valid plan triggers no re-prompting."""
        steps = _valid_tdd_plan()
        result, violations = self._run_enforcement_loop(steps, fix_tdd_violations)

        assert violations == []
        mock_get_client.assert_not_called()

    @patch("architect.planner.get_event_log")
    @patch("architect.planner.get_llm_client")
    def test_fixed_on_first_attempt(self, mock_get_client, mock_get_event_log):
        """Violations found -> LLM fixes on first try -> loop exits."""
        fixed = _valid_tdd_plan()
        mock_get_client.return_value = _make_mock_client(json.dumps(fixed))
        mock_get_event_log.return_value = MagicMock()

        result, violations = self._run_enforcement_loop(
            _plan_without_tests(), fix_tdd_violations,
        )

        assert violations == []
        assert len(result) == 4
        mock_get_client.return_value.generate.assert_called_once()

    @patch("architect.planner.get_event_log")
    @patch("architect.planner.get_llm_client")
    def test_fixed_on_second_attempt(self, mock_get_client, mock_get_event_log):
        """First fix still has violations, second fix succeeds."""
        # First attempt: still broken (no test outputs)
        still_broken = [
            {
                "title": "test: Tests for parser",
                "description": "Some tests.",
                "depends_on": [],
                "outputs": ["parser.py"],  # wrong file pattern
            },
            {
                "title": "Implement parser",
                "description": "Build parser.",
                "depends_on": [1],
                "outputs": ["parser.py"],
            },
        ]
        fixed = _valid_tdd_plan()

        client = MagicMock()
        client.generate.side_effect = [
            (json.dumps(still_broken), {}),
            (json.dumps(fixed), {}),
        ]
        mock_get_client.return_value = client
        mock_get_event_log.return_value = MagicMock()

        result, violations = self._run_enforcement_loop(
            _plan_without_tests(), fix_tdd_violations,
        )

        assert violations == []
        assert client.generate.call_count == 2

    @patch("architect.planner.get_event_log")
    @patch("architect.planner.get_llm_client")
    def test_persistent_violations_proceed_anyway(
        self, mock_get_client, mock_get_event_log,
    ):
        """After max attempts, violations remain but execution proceeds."""
        # Both attempts return the same broken plan
        broken = _plan_without_tests()
        client = MagicMock()
        client.generate.return_value = (json.dumps(broken), {})
        mock_get_client.return_value = client
        mock_get_event_log.return_value = MagicMock()

        result, violations = self._run_enforcement_loop(
            _plan_without_tests(), fix_tdd_violations,
        )

        assert len(violations) > 0
        assert client.generate.call_count == 2

    @patch("architect.planner.get_event_log")
    @patch("architect.planner.get_llm_client")
    def test_llm_returns_empty_both_attempts(
        self, mock_get_client, mock_get_event_log,
    ):
        """LLM returns empty list both times -> original kept, violations remain."""
        client = MagicMock()
        client.generate.return_value = ("[]", {})
        mock_get_client.return_value = client
        mock_get_event_log.return_value = MagicMock()

        original = _plan_without_tests()
        result, violations = self._run_enforcement_loop(
            original, fix_tdd_violations,
        )

        assert len(violations) > 0
        # Original plan returned since fix returned empty
        assert result[0]["title"] == "Implement CSV parser"

    @patch("architect.planner.get_event_log")
    @patch("architect.planner.get_llm_client")
    def test_reprompt_contains_violation_details(
        self, mock_get_client, mock_get_event_log,
    ):
        """Each re-prompt includes the specific violations from the prior check."""
        fixed = _valid_tdd_plan()
        client = _make_mock_client(json.dumps(fixed))
        mock_get_client.return_value = client
        mock_get_event_log.return_value = MagicMock()

        original = _plan_without_tests()
        self._run_enforcement_loop(original, fix_tdd_violations)

        prompt = client.generate.call_args[0][0]
        # The prompt must mention the violating steps
        assert "Implement CSV parser" in prompt
        assert "no preceding test step" in prompt
