"""Tests for LLM-based rewrite quality assessment."""

import json
from unittest.mock import patch, MagicMock

import pytest

from architect.planner import (
    _check_rewrite_quality,
    _is_confused_output,
    reflect_and_rewrite,
)


class TestCheckRewriteQualityLLM:
    @patch("architect.planner.MINIMAL_MODE", False)
    @patch("architect.planner.get_llm_client")
    def test_poor_quality_returns_true(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = json.dumps(
            {"quality": "poor", "reason": "repeats the error"}
        )
        mock_get_client.return_value = client

        result = _check_rewrite_quality("bad rewrite", "original task", "some error")
        assert result is True

    @patch("architect.planner.MINIMAL_MODE", False)
    @patch("architect.planner.get_llm_client")
    def test_good_quality_returns_false(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = json.dumps(
            {"quality": "good", "reason": "addresses root cause"}
        )
        mock_get_client.return_value = client

        result = _check_rewrite_quality("good rewrite", "original task", "some error")
        assert result is False

    @patch("architect.planner.MINIMAL_MODE", False)
    @patch("architect.planner.get_llm_client")
    def test_llm_failure_falls_back_to_heuristic(self, mock_get_client):
        client = MagicMock()
        client.generate.side_effect = RuntimeError("API down")
        mock_get_client.return_value = client

        # Short output with action verb, no error verbatim → heuristic returns False
        result = _check_rewrite_quality("Create the output file", "task", "err")
        assert result is False

        # Excessive length → heuristic returns True
        result = _check_rewrite_quality("x" * 10000, "short", "")
        assert result is True

    @patch("architect.planner.MINIMAL_MODE", True)
    def test_minimal_mode_uses_heuristic(self):
        # Short output with action verb → heuristic returns False
        result = _check_rewrite_quality("Build the module", "task", "")
        assert result is False

        # Excessive length → heuristic returns True
        result = _check_rewrite_quality("x" * 10000, "short", "")
        assert result is True

    @patch("architect.planner.MINIMAL_MODE", False)
    @patch("architect.planner.get_llm_client")
    def test_json_in_code_fence_parsed(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = (
            '```json\n{"quality": "poor", "reason": "bad"}\n```'
        )
        mock_get_client.return_value = client

        result = _check_rewrite_quality("bad rewrite", "task", "error")
        assert result is True


class TestRewriteQualityInReflectAndRewrite:
    @patch("architect.planner.MINIMAL_MODE", False)
    @patch("architect.planner.get_llm_client")
    def test_poor_quality_triggers_resampling(self, mock_get_client):
        client = MagicMock()
        client.generate.side_effect = [
            "first rewrite attempt",
            json.dumps({"quality": "poor", "reason": "not actionable"}),
            "second rewrite attempt",
        ]
        mock_get_client.return_value = client

        step = {"description": "do something"}
        result = reflect_and_rewrite(step, "stdout", "stderr")
        assert client.generate.call_count == 3
        assert result == "second rewrite attempt"

    @patch("architect.planner.MINIMAL_MODE", False)
    @patch("architect.planner.get_llm_client")
    def test_good_quality_no_resampling(self, mock_get_client):
        client = MagicMock()
        client.generate.side_effect = [
            "good rewrite",
            json.dumps({"quality": "good", "reason": "addresses root cause"}),
        ]
        mock_get_client.return_value = client

        step = {"description": "do something"}
        result = reflect_and_rewrite(step, "stdout", "stderr")
        assert client.generate.call_count == 2
        assert result == "good rewrite"

    @patch("architect.planner.MINIMAL_MODE", False)
    @patch("architect.planner.get_llm_client")
    def test_low_confidence_still_triggers_resampling(self, mock_get_client):
        client = MagicMock()
        client.generate.side_effect = [
            "first attempt",
            json.dumps({"quality": "good", "reason": "looks fine"}),
            "resampled attempt",
        ]
        mock_get_client.return_value = client

        step = {"description": "task"}
        reflections = [{
            "attempt": 1,
            "confidence": "low",
            "error_type": "logic_error",
            "root_cause": "wrong logic",
            "lesson": "check logic",
            "what_to_try_next": "rewrite",
        }]
        result = reflect_and_rewrite(
            step, "out", "err", reflections=reflections,
        )
        assert client.generate.call_count == 3
        assert result == "resampled attempt"

    @patch("architect.planner.MINIMAL_MODE", False)
    @patch("architect.planner.get_llm_client")
    def test_quality_check_failure_falls_back_during_rewrite(self, mock_get_client):
        client = MagicMock()
        # First response: short and clean with action verb → heuristic fallback says not confused
        client.generate.side_effect = [
            "Create the output file and validate results",
            RuntimeError("quality check API down"),
        ]
        mock_get_client.return_value = client

        step = {"description": "task"}
        result = reflect_and_rewrite(step, "stdout", "stderr")
        # Quality check fails, heuristic says OK (short output with action verb)
        assert result == "Create the output file and validate results"
