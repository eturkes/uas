"""Tests for architect.planner.expand_goal."""

from unittest.mock import MagicMock, patch

from architect.planner import expand_goal


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
