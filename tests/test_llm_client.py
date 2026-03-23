"""Tests for orchestrator.llm_client: timeout, model, retry behaviour."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.llm_client import (
    ClaudeCodeClient,
    DEFAULT_TIMEOUT,
    MAX_RETRIES,
    _is_transient,
    get_llm_client,
)


class TestGetLlmClient:
    def test_default_timeout(self):
        client = get_llm_client()
        assert client.timeout == DEFAULT_TIMEOUT

    def test_timeout_from_env(self, monkeypatch):
        monkeypatch.setenv("UAS_LLM_TIMEOUT", "30")
        client = get_llm_client()
        assert client.timeout == 30

    def test_model_from_env(self, monkeypatch):
        monkeypatch.setenv("UAS_MODEL", "claude-sonnet-4-6")
        client = get_llm_client()
        assert client.model == "claude-sonnet-4-6"

    def test_no_model_by_default(self):
        client = get_llm_client()
        assert client.model is None


class TestModelFlag:
    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_model_flag_passed(self, _mock_which, mock_stream):
        mock_stream.return_value = ("response", "", 0)
        client = ClaudeCodeClient(model="claude-sonnet-4-6")
        client.generate("hello")
        cmd = mock_stream.call_args[0][0]
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "claude-sonnet-4-6"

    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_fallback_model_when_none(self, _mock_which, mock_stream):
        mock_stream.return_value = ("response", "", 0)
        client = ClaudeCodeClient(model=None)
        client.generate("hello")
        cmd = mock_stream.call_args[0][0]
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "claude-opus-4-6"


class TestRetryBehaviour:
    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_retries_on_timeout(self, _mock_which, mock_stream, mock_sleep):
        mock_stream.side_effect = [
            subprocess.TimeoutExpired(cmd="claude", timeout=120),
            ("ok", "", 0),
        ]
        client = ClaudeCodeClient()
        result = client.generate("test")
        assert result == "ok"
        assert mock_stream.call_count == 2
        assert mock_sleep.call_count == 1

    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_retries_on_transient_stderr(self, _mock_which, mock_stream, mock_sleep):
        mock_stream.side_effect = [
            ("", "Connection refused", 1),
            ("ok", "", 0),
        ]
        client = ClaudeCodeClient()
        result = client.generate("test")
        assert result == "ok"
        assert mock_stream.call_count == 2

    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_raises_after_max_retries(self, _mock_which, mock_stream, mock_sleep):
        mock_stream.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=120)
        client = ClaudeCodeClient()
        with pytest.raises(RuntimeError, match="timed out"):
            client.generate("test")
        assert mock_stream.call_count == 1 + MAX_RETRIES

    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_no_retry_on_non_transient_error(self, _mock_which, mock_stream, mock_sleep):
        mock_stream.return_value = ("", "Invalid API key", 1)
        client = ClaudeCodeClient()
        with pytest.raises(RuntimeError, match="Invalid API key"):
            client.generate("test")
        assert mock_stream.call_count == 1
        mock_sleep.assert_not_called()

    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_no_retry_on_file_not_found(self, _mock_which, mock_stream):
        mock_stream.side_effect = FileNotFoundError("No such file")
        client = ClaudeCodeClient()
        with pytest.raises(RuntimeError, match="not found in PATH"):
            client.generate("test")
        assert mock_stream.call_count == 1

    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_exponential_backoff(self, _mock_which, mock_stream, mock_sleep):
        mock_stream.side_effect = [
            subprocess.TimeoutExpired(cmd="claude", timeout=120),
            subprocess.TimeoutExpired(cmd="claude", timeout=120),
            ("ok", "", 0),
        ]
        client = ClaudeCodeClient()
        client.generate("test")
        assert mock_sleep.call_count == 2
        # First backoff: 2 * 2^0 = 2s, second: 2 * 2^1 = 4s
        mock_sleep.assert_any_call(2)
        mock_sleep.assert_any_call(4)


class TestIsTransient:
    def test_timeout_is_transient(self):
        assert _is_transient("request timed out") is True

    def test_connection_error_is_transient(self):
        assert _is_transient("Connection refused by server") is True

    def test_invalid_key_not_transient(self):
        assert _is_transient("Invalid API key") is False

    def test_empty_string_not_transient(self):
        assert _is_transient("") is False

    def test_rate_limit_is_transient(self):
        assert _is_transient("You've hit your limit · resets 1pm (UTC)") is True

    def test_too_many_requests_is_transient(self):
        assert _is_transient("Too many requests, please slow down") is True

    def test_overloaded_is_transient(self):
        assert _is_transient("API is overloaded") is True


class TestRateLimitInStdout:
    """Regression tests: rate limit text in stdout must not be returned as
    valid LLM content — it should be retried as a transient error."""

    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_rate_limit_in_stdout_retried(self, _mock_which, mock_stream, mock_sleep):
        mock_stream.side_effect = [
            ("You've hit your limit · resets 1pm (UTC)", "", 1),
            ("valid response", "", 0),
        ]
        client = ClaudeCodeClient()
        result = client.generate("test")
        assert result == "valid response"
        assert mock_stream.call_count == 2

    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_rate_limit_in_stderr_with_stdout_retried(self, _mock_which, mock_stream, mock_sleep):
        """Even if stdout has content, a rate-limit in stderr should trigger retry."""
        mock_stream.side_effect = [
            ("partial output", "rate limit exceeded", 1),
            ("valid response", "", 0),
        ]
        client = ClaudeCodeClient()
        result = client.generate("test")
        assert result == "valid response"
        assert mock_stream.call_count == 2

    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_non_transient_stdout_still_returned(self, _mock_which, mock_stream):
        """Non-transient failures with stdout should still return partial output
        for truncation recovery."""
        mock_stream.return_value = ("partial valid code output", "some non-transient error", 1)
        client = ClaudeCodeClient()
        result = client.generate("test")
        assert result == "partial valid code output"
