"""Tests for orchestrator.llm_client: timeout, model, retry behaviour."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.llm_client import (
    ClaudeCodeClient,
    DEFAULT_TIMEOUT,
    INITIAL_BACKOFF,
    LLMError,
    MAX_BACKOFF,
    MAX_RETRIES,
    OVERLOADED_BACKOFF,
    PERSISTENT_HEARTBEAT_INTERVAL,
    _sleep_with_heartbeat,
    classify_error,
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
        assert result.text == "ok"
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
        assert result.text == "ok"
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
        with pytest.raises(RuntimeError):
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
        mock_sleep.assert_any_call(INITIAL_BACKOFF)
        mock_sleep.assert_any_call(INITIAL_BACKOFF * 2)

    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_overloaded_uses_longer_backoff(self, _mock_which, mock_stream, mock_sleep):
        """529 overloaded errors should use OVERLOADED_BACKOFF, not INITIAL_BACKOFF."""
        mock_stream.side_effect = [
            ("", 'API Error: 529 {"type":"error","error":{"type":"overloaded_error"}}', 1),
            ("ok", "", 0),
        ]
        client = ClaudeCodeClient()
        result = client.generate("test")
        assert result.text == "ok"
        assert mock_stream.call_count == 2
        # Overloaded backoff: OVERLOADED_BACKOFF * 2^0
        mock_sleep.assert_called_with(OVERLOADED_BACKOFF)

    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_529_retries_until_success(self, _mock_which, mock_stream, mock_sleep):
        """529 errors should be retried across multiple attempts."""
        mock_stream.side_effect = [
            ("", "529 overloaded_error", 1),
            ("", "529 overloaded_error", 1),
            ("", "529 overloaded_error", 1),
            ("ok", "", 0),
        ]
        client = ClaudeCodeClient()
        result = client.generate("test")
        assert result.text == "ok"
        assert mock_stream.call_count == 4


class TestClassifyError:
    """Unit tests for classify_error — a pure function with no I/O."""

    def test_timeout(self):
        err = classify_error(1, "", "request timed out")
        assert err.category == "timeout"
        assert err.retryable is True

    def test_connection_error(self):
        err = classify_error(1, "", "Connection refused by server")
        assert err.category == "connection"
        assert err.retryable is True

    def test_auth_error(self):
        err = classify_error(1, "", "Invalid API key")
        assert err.category == "auth"
        assert err.retryable is False

    def test_empty_string_unknown(self):
        err = classify_error(1, "", "")
        assert err.category == "unknown"
        assert err.retryable is False

    def test_rate_limit(self):
        err = classify_error(1, "", "You've hit your limit · resets 1pm (UTC)")
        assert err.category == "rate_limit"
        assert err.retryable is True

    def test_too_many_requests(self):
        err = classify_error(1, "", "Too many requests, please slow down")
        assert err.category == "rate_limit"
        assert err.retryable is True

    def test_overloaded_is_capacity(self):
        err = classify_error(1, "", "API is overloaded")
        assert err.category == "capacity"
        assert err.retryable is True

    def test_529_is_capacity(self):
        err = classify_error(
            1, "",
            'API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
        )
        assert err.category == "capacity"
        assert err.retryable is True

    def test_overloaded_error_type_is_capacity(self):
        err = classify_error(1, "", "overloaded_error")
        assert err.category == "capacity"
        assert err.retryable is True

    def test_rate_limit_has_longer_backoff(self):
        err = classify_error(1, "", "rate limit exceeded")
        assert err.category == "rate_limit"
        assert err.recommended_backoff == OVERLOADED_BACKOFF

    def test_capacity_has_longer_backoff(self):
        err = classify_error(1, "", "529 overloaded")
        assert err.category == "capacity"
        assert err.recommended_backoff == OVERLOADED_BACKOFF

    def test_429_is_rate_limit(self):
        err = classify_error(1, "", "HTTP 429 Too Many Requests")
        assert err.category == "rate_limit"
        assert err.retryable is True

    def test_connection_has_short_backoff(self):
        err = classify_error(1, "", "Connection refused")
        assert err.recommended_backoff == INITIAL_BACKOFF

    def test_timeout_has_short_backoff(self):
        err = classify_error(1, "", "request timed out")
        assert err.recommended_backoff == INITIAL_BACKOFF

    def test_prompt_too_long(self):
        err = classify_error(1, "", "prompt too long")
        assert err.category == "prompt_too_long"
        assert err.retryable is False

    def test_output_truncated(self):
        err = classify_error(1, "partial output here", "some error")
        assert err.category == "output_truncated"
        assert err.retryable is False

    def test_auth_in_stdout(self):
        err = classify_error(0, "not logged in", "")
        assert err.category == "auth"
        assert err.retryable is False

    def test_raw_output_preserved(self):
        err = classify_error(1, "out", "err")
        assert "err" in err.raw_output
        assert "out" in err.raw_output


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
        assert result.text == "valid response"
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
        assert result.text == "valid response"
        assert mock_stream.call_count == 2

    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_rate_limit_exhausted_raises_not_returns(self, _mock_which, mock_stream, mock_sleep):
        """When all transient retries are exhausted, raise instead of returning
        the rate limit message as valid LLM content."""
        mock_stream.return_value = ("You've hit your limit · resets 6pm (UTC)", "", 1)
        client = ClaudeCodeClient()
        with pytest.raises(RuntimeError):
            client.generate("test")
        # Should have attempted initial + MAX_RETRIES = 5 calls
        assert mock_stream.call_count == 1 + MAX_RETRIES

    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_non_transient_stdout_still_returned(self, _mock_which, mock_stream):
        """Non-transient failures with stdout should still return partial output
        for truncation recovery."""
        mock_stream.return_value = ("partial valid code output", "some non-transient error", 1)
        client = ClaudeCodeClient()
        result = client.generate("test")
        assert result.text == "partial valid code output"


class TestPersistentRetry:
    """Tests for Section 3: UAS_PERSISTENT_RETRY mode."""

    @patch("orchestrator.llm_client.PERSISTENT_RETRY", True)
    @patch("orchestrator.llm_client._sleep_with_heartbeat")
    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_retries_beyond_max_on_429(self, _mock_which, mock_stream, mock_hb_sleep):
        """With PERSISTENT_RETRY, retryable errors retry beyond MAX_RETRIES."""
        errors = [("", "429 Too Many Requests", 1)] * (MAX_RETRIES + 2)
        mock_stream.side_effect = errors + [("ok", "", 0)]
        client = ClaudeCodeClient()
        result = client.generate("test")
        assert result.text == "ok"
        assert mock_stream.call_count == MAX_RETRIES + 3

    @patch("orchestrator.llm_client.PERSISTENT_RETRY", True)
    @patch("orchestrator.llm_client._sleep_with_heartbeat")
    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_backoff_caps_at_max_backoff(self, _mock_which, mock_stream, mock_hb_sleep):
        """Backoff is capped at MAX_BACKOFF (300s) in persistent mode."""
        errors = [("", "overloaded", 1)] * 6
        mock_stream.side_effect = errors + [("ok", "", 0)]
        client = ClaudeCodeClient()
        client.generate("test")
        for call in mock_hb_sleep.call_args_list:
            assert call[0][0] <= MAX_BACKOFF

    @patch("orchestrator.llm_client.PERSISTENT_RETRY", True)
    @patch("orchestrator.llm_client._sleep_with_heartbeat")
    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_non_retryable_raises_in_persistent_mode(self, _mock_which, mock_stream, mock_hb_sleep):
        """Non-retryable errors still raise immediately in persistent mode."""
        mock_stream.return_value = ("", "Invalid API key", 1)
        client = ClaudeCodeClient()
        with pytest.raises(RuntimeError):
            client.generate("test")
        assert mock_stream.call_count == 1
        mock_hb_sleep.assert_not_called()

    @patch("orchestrator.llm_client.PERSISTENT_RETRY", True)
    @patch("orchestrator.llm_client._sleep_with_heartbeat")
    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_capacity_unlimited_in_persistent_mode(self, _mock_which, mock_stream, mock_hb_sleep):
        """In persistent mode, capacity errors retry beyond MAX_CAPACITY_RETRIES (3)."""
        errors = [("", "529 overloaded_error", 1)] * 5
        mock_stream.side_effect = errors + [("ok", "", 0)]
        client = ClaudeCodeClient()
        result = client.generate("test")
        assert result.text == "ok"
        assert mock_stream.call_count == 6

    @patch("orchestrator.llm_client.PERSISTENT_RETRY", True)
    @patch("orchestrator.llm_client._sleep_with_heartbeat")
    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_uses_heartbeat_sleep(self, _mock_which, mock_stream, mock_hb_sleep):
        """Persistent mode uses _sleep_with_heartbeat instead of time.sleep."""
        mock_stream.side_effect = [
            ("", "Connection refused", 1),
            ("ok", "", 0),
        ]
        client = ClaudeCodeClient()
        client.generate("test")
        mock_hb_sleep.assert_called_once()
        label = mock_hb_sleep.call_args[0][1]
        assert "Persistent retry" in label


class TestSleepWithHeartbeat:
    """Tests for the _sleep_with_heartbeat helper."""

    @patch("orchestrator.llm_client.time.sleep")
    def test_heartbeat_logged(self, mock_sleep, caplog):
        import logging
        with caplog.at_level(logging.INFO, logger="orchestrator.llm_client"):
            _sleep_with_heartbeat(65, "Test wait", interval=30)
        assert mock_sleep.call_count == 3  # 30 + 30 + 5
        heartbeats = [r for r in caplog.records if "still waiting" in r.message]
        assert len(heartbeats) == 2

    @patch("orchestrator.llm_client.time.sleep")
    def test_short_sleep_no_heartbeat(self, mock_sleep, caplog):
        import logging
        with caplog.at_level(logging.INFO, logger="orchestrator.llm_client"):
            _sleep_with_heartbeat(10, "Short wait", interval=30)
        assert mock_sleep.call_count == 1
        heartbeats = [r for r in caplog.records if "still waiting" in r.message]
        assert len(heartbeats) == 0
