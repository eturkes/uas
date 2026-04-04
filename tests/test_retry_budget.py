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
from orchestrator.llm_client import classify_error, classify_llm_error, INITIAL_BACKOFF, OVERLOADED_BACKOFF
from uas.fuzzy_models import ErrorClassification


def _mock_classify(returncode, stdout, stderr):
    """Deterministic classification for tests — mimics old regex behaviour."""
    combined = f"{stderr} {stdout}".lower()
    if any(p in combined for p in [
        "not logged in", "invalid api key", "unauthorized",
    ]):
        return ErrorClassification(
            category="auth", retryable=False,
            recommended_backoff=0, message="Auth error")
    if any(p in combined for p in [
        "rate limit", "rate_limit", "hit your limit", "too many requests",
        "out of usage", "out of extra usage", "429",
    ]):
        return ErrorClassification(
            category="rate_limit", retryable=True,
            recommended_backoff=OVERLOADED_BACKOFF, message="Rate limit hit")
    if any(p in combined for p in [
        "529", "overloaded", "overloaded_error", "capacity",
    ]):
        return ErrorClassification(
            category="capacity", retryable=True,
            recommended_backoff=OVERLOADED_BACKOFF, message="API at capacity")
    if returncode != 0 and stdout.strip():
        return ErrorClassification(
            category="output_truncated", retryable=False,
            recommended_backoff=0, message="Output truncated")
    return ErrorClassification(
        category="unknown", retryable=False,
        recommended_backoff=0, message=f"CLI exited with code {returncode}")


class TestLLMRetryDecision:
    @patch("architect.main.MINIMAL_MODE", False)
    @patch("architect.main.get_event_log")
    @patch("orchestrator.llm_client.get_llm_client")
    def test_llm_continue_false_stops_retries(self, mock_get_client, mock_event_log):
        client = MagicMock()
        client.generate.return_value = (json.dumps({
            "continue": False,
            "reason": "repeated dependency error with no new approach",
        }), {"input": 0, "output": 0})
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
        client.generate.return_value = (json.dumps({
            "continue": True,
            "reason": "new approach suggested using csv module",
        }), {"input": 0, "output": 0})
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
        client.generate.return_value = (json.dumps({
            "continue": True,
            "reason": "should keep trying",
        }), {"input": 0, "output": 0})
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
        client.generate.return_value = ('```json\n{"continue": false, "reason": "stagnating"}\n```', {"input": 0, "output": 0})
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
        client.generate.return_value = ("I think you should keep trying!", {"input": 0, "output": 0})
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
        client.generate.return_value = (json.dumps({"continue": True, "reason": "ok"}), {"input": 0, "output": 0})
        mock_get_client.return_value = client
        event_log = MagicMock()
        mock_event_log.return_value = event_log

        step = {"id": 1, "description": "task"}
        should_continue_retrying(step, 0, "logic_error", [])
        assert event_log.emit.call_count == 2


@patch("orchestrator.llm_client.classify_llm_error", side_effect=_mock_classify)
class TestRateLimitClassification:
    """Tests that rate-limit/capacity errors are classified as retryable."""

    def test_hit_your_limit_detected(self, _mock_cls):
        err = classify_error(1, "", "You've hit your limit · resets 6pm (UTC)")
        assert err.retryable is True
        assert err.category == "rate_limit"

    def test_rate_limit_detected(self, _mock_cls):
        err = classify_error(1, "", "Error: rate limit exceeded")
        assert err.retryable is True
        assert err.category == "rate_limit"

    def test_429_detected(self, _mock_cls):
        err = classify_error(1, "", "HTTP 429 Too Many Requests")
        assert err.retryable is True
        assert err.category == "rate_limit"

    def test_overloaded_detected(self, _mock_cls):
        err = classify_error(1, "", "API is overloaded, try again later")
        assert err.retryable is True
        assert err.category == "capacity"

    def test_normal_error_not_retryable(self, _mock_cls):
        err = classify_error(1, "", "ModuleNotFoundError: No module named 'foo'")
        assert err.retryable is False

    def test_empty_not_retryable(self, _mock_cls):
        err = classify_error(1, "", "")
        assert err.retryable is False

