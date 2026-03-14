"""Tests for Section 7: LLM Semantic Workspace Validation."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from architect.main import validate_workspace, validate_workspace_llm


class TestValidateWorkspaceLLM:

    def _make_state(self, goal="Build a data pipeline", steps=None):
        if steps is None:
            steps = [
                {"title": "Create pipeline", "status": "completed",
                 "files_written": []},
            ]
        return {"goal": goal, "steps": steps}

    def test_llm_identifies_goal_not_satisfied(self, tmp_path):
        (tmp_path / "output.txt").write_text("placeholder content")
        state = self._make_state(
            goal="Build a CSV parser that reads data.csv and outputs stats",
            steps=[
                {"title": "Create CSV parser", "status": "completed",
                 "files_written": ["output.txt"]},
            ],
        )
        llm_response = json.dumps({
            "goal_satisfied": False,
            "confidence": "high",
            "issues": [
                "output.txt contains placeholder content, not actual CSV parsing results",
                "No data.csv file found in workspace",
            ],
            "summary": "The workspace has a file but it does not contain CSV parsing output.",
        })
        mock_client = MagicMock()
        mock_client.generate.return_value = llm_response

        with patch("orchestrator.llm_client.get_llm_client",
                    return_value=mock_client):
            result = validate_workspace_llm(state, str(tmp_path))

        mock_client.generate.assert_called_once()
        assert result is not None
        assert result["goal_satisfied"] is False
        assert result["confidence"] == "high"
        assert len(result["issues"]) == 2

    def test_llm_confirms_goal_satisfied(self, tmp_path):
        (tmp_path / "stats.json").write_text('{"mean": 42, "count": 100}')
        (tmp_path / "parser.py").write_text("import csv\n# parser code")
        state = self._make_state(
            goal="Build a CSV parser that outputs statistics",
            steps=[
                {"title": "Write parser", "status": "completed",
                 "files_written": ["parser.py", "stats.json"]},
            ],
        )
        llm_response = json.dumps({
            "goal_satisfied": True,
            "confidence": "high",
            "issues": [],
            "summary": "Workspace contains a parser and statistics output as expected.",
        })
        mock_client = MagicMock()
        mock_client.generate.return_value = llm_response

        with patch("orchestrator.llm_client.get_llm_client",
                    return_value=mock_client):
            result = validate_workspace_llm(state, str(tmp_path))

        assert result is not None
        assert result["goal_satisfied"] is True
        assert result["issues"] == []

    def test_llm_failure_returns_none(self, tmp_path):
        (tmp_path / "output.txt").write_text("data")
        state = self._make_state()
        mock_client = MagicMock()
        mock_client.generate.side_effect = RuntimeError("LLM unavailable")

        with patch("orchestrator.llm_client.get_llm_client",
                    return_value=mock_client):
            result = validate_workspace_llm(state, str(tmp_path))

        assert result is None

    def test_llm_failure_doesnt_break_validate_workspace(self, tmp_path):
        (tmp_path / "output.txt").write_text("data")
        state = self._make_state(
            steps=[{"title": "Step 1", "status": "completed",
                    "files_written": []}],
        )
        mock_client = MagicMock()
        mock_client.generate.side_effect = RuntimeError("LLM down")

        with patch("architect.main.MINIMAL_MODE", False), \
             patch("orchestrator.llm_client.get_llm_client",
                   return_value=mock_client):
            result = validate_workspace(state, str(tmp_path))

        assert isinstance(result, dict)
        assert "llm_assessment" not in result

    def test_validation_report_includes_llm_assessment(self, tmp_path):
        (tmp_path / "app.py").write_text("print('hello')")
        state = self._make_state(
            goal="Build a hello world app",
            steps=[{"title": "Write app", "status": "completed",
                    "files_written": []}],
        )
        llm_response = json.dumps({
            "goal_satisfied": True,
            "confidence": "high",
            "issues": [],
            "summary": "App correctly prints hello world.",
        })
        mock_client = MagicMock()
        mock_client.generate.return_value = llm_response

        with patch("architect.main.MINIMAL_MODE", False), \
             patch("orchestrator.llm_client.get_llm_client",
                   return_value=mock_client):
            result = validate_workspace(state, str(tmp_path))

        assert "llm_assessment" in result
        assert result["llm_assessment"]["goal_satisfied"] is True

        content = (tmp_path / ".state" / "validation.md").read_text()
        assert "Goal Assessment (LLM)" in content
        assert "Goal satisfied:** Yes" in content
        assert "hello world" in content

    def test_validation_report_shows_issues(self, tmp_path):
        (tmp_path / "empty.txt").write_text("")
        state = self._make_state(
            goal="Generate a report",
            steps=[{"title": "Generate", "status": "completed",
                    "files_written": []}],
        )
        llm_response = json.dumps({
            "goal_satisfied": False,
            "confidence": "medium",
            "issues": ["Output file is empty"],
            "summary": "Report file exists but has no content.",
        })
        mock_client = MagicMock()
        mock_client.generate.return_value = llm_response

        with patch("architect.main.MINIMAL_MODE", False), \
             patch("orchestrator.llm_client.get_llm_client",
                   return_value=mock_client):
            result = validate_workspace(state, str(tmp_path))

        content = (tmp_path / ".state" / "validation.md").read_text()
        assert "Goal satisfied:** No" in content
        assert "Output file is empty" in content

    def test_minimal_mode_skips_llm_validation(self, tmp_path):
        (tmp_path / "output.txt").write_text("data")
        state = self._make_state(
            steps=[{"title": "Step 1", "status": "completed",
                    "files_written": []}],
        )

        with patch("architect.main.MINIMAL_MODE", True), \
             patch("orchestrator.llm_client.get_llm_client") as mock_factory:
            result = validate_workspace(state, str(tmp_path))
            mock_factory.assert_not_called()

        assert "llm_assessment" not in result

    def test_no_goal_returns_none(self, tmp_path):
        state = {"goal": "", "steps": []}
        result = validate_workspace_llm(state, str(tmp_path))
        assert result is None

    def test_llm_response_with_markdown_fences(self, tmp_path):
        (tmp_path / "file.py").write_text("print('hi')")
        state = self._make_state()
        llm_response = '```json\n{"goal_satisfied": true, "confidence": "high", "issues": [], "summary": "OK"}\n```'
        mock_client = MagicMock()
        mock_client.generate.return_value = llm_response

        with patch("orchestrator.llm_client.get_llm_client",
                    return_value=mock_client):
            result = validate_workspace_llm(state, str(tmp_path))

        assert result is not None
        assert result["goal_satisfied"] is True
