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
    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_model_flag_passed(self, _mock_which, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="response", stderr=""
        )
        client = ClaudeCodeClient(model="claude-sonnet-4-6")
        client.generate("hello")
        cmd = mock_run.call_args[0][0]
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "claude-sonnet-4-6"

    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_no_model_flag_when_none(self, _mock_which, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="response", stderr=""
        )
        client = ClaudeCodeClient(model=None)
        client.generate("hello")
        cmd = mock_run.call_args[0][0]
        assert "--model" not in cmd


class TestTimeoutConfig:
    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_custom_timeout_passed_to_subprocess(self, _mock_which, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="ok", stderr=""
        )
        client = ClaudeCodeClient(timeout=45)
        client.generate("test")
        assert mock_run.call_args.kwargs["timeout"] == 45


class TestRetryBehaviour:
    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_retries_on_timeout(self, _mock_which, mock_run, mock_sleep):
        mock_run.side_effect = [
            subprocess.TimeoutExpired(cmd="claude", timeout=120),
            MagicMock(returncode=0, stdout="ok", stderr=""),
        ]
        client = ClaudeCodeClient()
        result = client.generate("test")
        assert result == "ok"
        assert mock_run.call_count == 2
        assert mock_sleep.call_count == 1

    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_retries_on_transient_stderr(self, _mock_which, mock_run, mock_sleep):
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr="Connection refused"),
            MagicMock(returncode=0, stdout="ok", stderr=""),
        ]
        client = ClaudeCodeClient()
        result = client.generate("test")
        assert result == "ok"
        assert mock_run.call_count == 2

    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_raises_after_max_retries(self, _mock_which, mock_run, mock_sleep):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=120)
        client = ClaudeCodeClient()
        with pytest.raises(RuntimeError, match="timed out"):
            client.generate("test")
        assert mock_run.call_count == 1 + MAX_RETRIES

    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_no_retry_on_non_transient_error(self, _mock_which, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="Invalid API key"
        )
        client = ClaudeCodeClient()
        with pytest.raises(RuntimeError, match="Invalid API key"):
            client.generate("test")
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_no_retry_on_file_not_found(self, _mock_which, mock_run):
        mock_run.side_effect = FileNotFoundError("No such file")
        client = ClaudeCodeClient()
        with pytest.raises(RuntimeError, match="not found in PATH"):
            client.generate("test")
        assert mock_run.call_count == 1

    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_exponential_backoff(self, _mock_which, mock_run, mock_sleep):
        mock_run.side_effect = [
            subprocess.TimeoutExpired(cmd="claude", timeout=120),
            subprocess.TimeoutExpired(cmd="claude", timeout=120),
            MagicMock(returncode=0, stdout="ok", stderr=""),
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
