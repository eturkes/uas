"""Tests for architect.planner.expand_goal."""

from unittest.mock import MagicMock, patch

from architect.planner import expand_goal, _goal_is_specific


class TestExpandGoal:
    @patch("architect.planner.get_llm_client")
    def test_clear_goal_passed_through(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "Write a Python script that downloads CSV data from URL and saves it as output.csv"
        mock_get_client.return_value = client

        result = expand_goal("Write a Python script that downloads CSV data from URL and saves it as output.csv")
        assert "CSV" in result
        assert client.generate.call_count == 1

    @patch("architect.planner.get_llm_client")
    def test_vague_goal_expanded(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = (
            "Analyze the sales data in data.csv: compute monthly totals, "
            "identify trends, and save a summary report as report.md"
        )
        mock_get_client.return_value = client

        result = expand_goal("analyze data")
        assert "sales data" in result or "summary" in result
        assert client.generate.call_count == 1

    @patch("architect.planner.get_llm_client")
    def test_llm_failure_returns_original(self, mock_get_client):
        client = MagicMock()
        client.generate.side_effect = RuntimeError("API timeout")
        mock_get_client.return_value = client

        result = expand_goal("my vague goal")
        assert result == "my vague goal"

    @patch("architect.planner.get_llm_client")
    def test_empty_response_returns_original(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "   "
        mock_get_client.return_value = client

        result = expand_goal("original goal")
        assert result == "original goal"

    @patch("architect.planner.get_llm_client")
    def test_whitespace_stripped(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "\n  expanded goal text  \n"
        mock_get_client.return_value = client

        result = expand_goal("goal")
        assert result == "expanded goal text"

    @patch("architect.planner.get_llm_client")
    def test_uses_planner_role(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "expanded"
        mock_get_client.return_value = client

        expand_goal("any goal")
        mock_get_client.assert_called_once_with(role="planner")

    @patch("architect.planner.get_llm_client")
    def test_prompt_includes_goal(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "expanded"
        mock_get_client.return_value = client

        expand_goal("build a web scraper")
        prompt = client.generate.call_args[0][0]
        assert "build a web scraper" in prompt


class TestGoalIsSpecific:
    def test_short_vague_goal_not_specific(self):
        assert not _goal_is_specific("analyze data")

    def test_long_goal_is_specific(self):
        goal = "A" * 501
        assert _goal_is_specific(goal)

    def test_exactly_500_chars_not_specific(self):
        goal = "A" * 500
        assert not _goal_is_specific(goal)

    def test_numbered_list_is_specific(self):
        goal = "Build an app:\n1. Create the UI\n2. Add backend\n3. Deploy"
        assert _goal_is_specific(goal)

    def test_numbered_paren_list_is_specific(self):
        goal = "Build an app:\n1) Create the UI\n2) Add backend"
        assert _goal_is_specific(goal)

    def test_markdown_header_is_specific(self):
        goal = "## Requirements\nMust handle 1000 users"
        assert _goal_is_specific(goal)

    def test_code_block_is_specific(self):
        goal = "Run this:\n```python\nprint('hello')\n```"
        assert _goal_is_specific(goal)

    def test_plain_short_goal_not_specific(self):
        assert not _goal_is_specific("build a web scraper for news sites")


class TestExpandGoalSkipsSpecific:
    @patch("architect.planner.get_llm_client")
    def test_long_detailed_goal_skips_llm(self, mock_get_client):
        """A detailed multi-paragraph goal should not be sent to the LLM."""
        goal = (
            "Build a comprehensive SCI rehabilitation analytics dashboard.\n"
            "## Phase 1: Data Ingestion\n"
            "1. Parse ISNCSCI motor and sensory scores from CSV\n"
            "2. Validate AIS grade classification\n"
            "3. Compute MCID thresholds for FIM scores\n"
            "## Phase 2: Analysis\n"
            "4. Longitudinal trend analysis with confidence intervals\n"
            "5. Neurological level progression tracking\n"
            "## Phase 3: Visualization\n"
            "6. Interactive Plotly dashboard with drill-down\n"
            "7. Export to PDF report format\n"
        )
        result = expand_goal(goal)
        assert result == goal
        mock_get_client.assert_not_called()

    @patch("architect.planner.get_llm_client")
    def test_short_goal_still_calls_llm(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "expanded version"
        mock_get_client.return_value = client

        result = expand_goal("analyze data")
        assert result == "expanded version"
        assert client.generate.call_count == 1
