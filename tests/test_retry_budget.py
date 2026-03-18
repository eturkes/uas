"""Tests for Section 1: LLM-Based Retry Decision.

Covers:
- LLM-driven retry decisions via should_continue_retrying
- Heuristic fallback when LLM fails
- Hard ceiling enforcement regardless of LLM response
- Stagnation and budget behavior via the heuristic fallback
"""

import json
from unittest.mock import patch, MagicMock

import pytest

from architect.main import (
    should_continue_retrying,
    _should_continue_retrying_heuristic,
    MAX_SPEC_REWRITES,
)


class TestLLMRetryDecision:
    @patch("architect.main.MINIMAL_MODE", False)
    @patch("architect.main.get_event_log")
    @patch("orchestrator.llm_client.get_llm_client")
    def test_llm_continue_false_stops_retries(self, mock_get_client, mock_event_log):
        client = MagicMock()
        client.generate.return_value = json.dumps({
            "continue": False,
            "reason": "repeated dependency error with no new approach",
        })
        mock_get_client.return_value = client
        mock_event_log.return_value = MagicMock()

        step = {"id": 1, "description": "install pandas and process data"}
        reflections = [
            {"attempt": 1, "error_type": "dependency_error",
             "root_cause": "pandas not found", "what_to_try_next": "pip install pandas"},
        ]
        ok, reason = should_continue_retrying(step, 1, "dependency_error", reflections)
        assert ok is False
        assert "repeated dependency error" in reason

    @patch("architect.main.MINIMAL_MODE", False)
    @patch("architect.main.get_event_log")
    @patch("orchestrator.llm_client.get_llm_client")
    def test_llm_continue_true_allows_retries(self, mock_get_client, mock_event_log):
        client = MagicMock()
        client.generate.return_value = json.dumps({
            "continue": True,
            "reason": "new approach suggested using csv module",
        })
        mock_get_client.return_value = client
        mock_event_log.return_value = MagicMock()

        step = {"id": 1, "description": "process data file"}
        reflections = [
            {"attempt": 1, "error_type": "logic_error",
             "root_cause": "key error", "what_to_try_next": "use csv module"},
        ]
        ok, reason = should_continue_retrying(step, 1, "logic_error", reflections)
        assert ok is True
        assert "csv module" in reason

    @patch("architect.main.MINIMAL_MODE", False)
    @patch("architect.main.get_event_log")
    @patch("orchestrator.llm_client.get_llm_client")
    def test_llm_failure_falls_back_to_heuristic(self, mock_get_client, mock_event_log):
        client = MagicMock()
        client.generate.side_effect = RuntimeError("API unavailable")
        mock_get_client.return_value = client
        mock_event_log.return_value = MagicMock()

        step = {"id": 1, "description": "task"}
        ok, reason = should_continue_retrying(step, 0, "logic_error", [])
        assert ok is True
        assert "within retry budget" in reason

    @patch("architect.main.MINIMAL_MODE", False)
    @patch("architect.main.get_event_log")
    @patch("orchestrator.llm_client.get_llm_client")
    def test_hard_ceiling_respected_even_with_llm_continue(self, mock_get_client, mock_event_log):
        client = MagicMock()
        client.generate.return_value = json.dumps({
            "continue": True,
            "reason": "should keep trying",
        })
        mock_get_client.return_value = client

        step = {"id": 1, "description": "task"}
        ok, reason = should_continue_retrying(
            step, MAX_SPEC_REWRITES, "logic_error", []
        )
        assert ok is False
        assert "max spec rewrites" in reason
        client.generate.assert_not_called()

    @patch("architect.main.MINIMAL_MODE", False)
    @patch("architect.main.get_event_log")
    @patch("orchestrator.llm_client.get_llm_client")
    def test_llm_response_with_code_fences(self, mock_get_client, mock_event_log):
        client = MagicMock()
        client.generate.return_value = '```json\n{"continue": false, "reason": "stagnating"}\n```'
        mock_get_client.return_value = client
        mock_event_log.return_value = MagicMock()

        step = {"id": 1, "description": "task"}
        ok, reason = should_continue_retrying(step, 1, "logic_error", [])
        assert ok is False
        assert "stagnating" in reason

    @patch("architect.main.MINIMAL_MODE", False)
    @patch("architect.main.get_event_log")
    @patch("orchestrator.llm_client.get_llm_client")
    def test_llm_unparseable_response_falls_back(self, mock_get_client, mock_event_log):
        client = MagicMock()
        client.generate.return_value = "I think you should keep trying!"
        mock_get_client.return_value = client
        mock_event_log.return_value = MagicMock()

        step = {"id": 1, "description": "task"}
        ok, reason = should_continue_retrying(step, 0, "logic_error", [])
        assert ok is True
        assert "within retry budget" in reason

    @patch("architect.main.MINIMAL_MODE", True)
    def test_minimal_mode_skips_llm(self):
        step = {"id": 1, "description": "task"}
        ok, reason = should_continue_retrying(step, 0, "logic_error", [])
        assert ok is True
        assert "within retry budget" in reason

    @patch("architect.main.MINIMAL_MODE", False)
    @patch("architect.main.get_event_log")
    @patch("orchestrator.llm_client.get_llm_client")
    def test_event_log_emitted(self, mock_get_client, mock_event_log):
        client = MagicMock()
        client.generate.return_value = json.dumps({"continue": True, "reason": "ok"})
        mock_get_client.return_value = client
        event_log = MagicMock()
        mock_event_log.return_value = event_log

        step = {"id": 1, "description": "task"}
        should_continue_retrying(step, 0, "logic_error", [])
        assert event_log.emit.call_count == 2


