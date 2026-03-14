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


class TestHeuristicFallbackBehavior:
    def test_stagnation_stops_retrying(self):
        step = {"id": 1}
        reflections = [
            {"error_type": "logic_error", "root_cause": "variable x is undefined",
             "what_to_try_next": "define x"},
            {"error_type": "logic_error", "root_cause": "variable x is undefined",
             "what_to_try_next": "define x before use"},
        ]
        ok, reason = _should_continue_retrying_heuristic(
            step, 1, "logic_error", reflections
        )
        assert ok is False
        assert "stagnation" in reason

    def test_within_budget_continues(self):
        step = {"id": 1}
        ok, reason = _should_continue_retrying_heuristic(step, 0, "logic_error", [])
        assert ok is True
        assert "within retry budget" in reason

    def test_over_budget_with_novel_approach_extends(self):
        step = {"id": 1}
        reflections = [
            {"error_type": "dependency_error",
             "root_cause": "pandas not installed",
             "what_to_try_next": "pip install pandas"},
            {"error_type": "dependency_error",
             "root_cause": "pandas version incompatible",
             "what_to_try_next": "use csv module instead of pandas"},
        ]
        ok, reason = _should_continue_retrying_heuristic(
            step, 1, "dependency_error", reflections
        )
        assert ok is True
        assert "novel approach" in reason

    def test_timeout_zero_budget(self):
        step = {"id": 1}
        reflections = [
            {"error_type": "timeout", "root_cause": "timed out",
             "what_to_try_next": "optimize"},
        ]
        ok, reason = _should_continue_retrying_heuristic(
            step, 0, "timeout", reflections
        )
        assert ok is False
        assert "exceeded retry budget" in reason

    def test_unknown_error_uses_max_budget(self):
        step = {"id": 1}
        ok, reason = _should_continue_retrying_heuristic(
            step, 0, "brand_new_error", []
        )
        assert ok is True
        assert f"/{MAX_SPEC_REWRITES}" in reason

    def test_different_root_cause_continues(self):
        step = {"id": 1}
        reflections = [
            {"error_type": "logic_error", "root_cause": "variable x is undefined",
             "what_to_try_next": "define x"},
            {"error_type": "logic_error",
             "root_cause": "wrong return type from function parse_data",
             "what_to_try_next": "cast return value to int"},
        ]
        ok, reason = _should_continue_retrying_heuristic(
            step, 1, "logic_error", reflections
        )
        assert ok is True
        assert "within retry budget" in reason

    def test_empty_root_cause_no_stagnation(self):
        step = {"id": 1}
        reflections = [
            {"error_type": "logic_error", "root_cause": "",
             "what_to_try_next": "try A"},
            {"error_type": "logic_error", "root_cause": "",
             "what_to_try_next": "try B"},
        ]
        ok, reason = _should_continue_retrying_heuristic(
            step, 1, "logic_error", reflections
        )
        assert ok is True
