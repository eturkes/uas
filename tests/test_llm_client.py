"""Tests for orchestrator.llm_client: timeout, model, retry behaviour."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

def _cp(stdout="", stderr="", returncode=0):
    """Create a CompletedProcess for mocking subprocess.run."""
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


from orchestrator.llm_client import (
    ClaudeCodeClient,
    DEFAULT_TIMEOUT,
    INITIAL_BACKOFF,
    MAX_BACKOFF,
    MAX_RETRIES,
    OVERLOADED_BACKOFF,
    PERSISTENT_HEARTBEAT_INTERVAL,
    _sleep_with_heartbeat,
    classify_error,
    classify_llm_error,
    get_llm_client,
)
from uas.fuzzy_models import ErrorClassification


def _mock_classify(returncode, stdout, stderr):
    """Deterministic classification for tests — mimics old regex behaviour."""
    combined = f"{stderr} {stdout}".lower()
    if any(p in combined for p in [
        "not logged in", "invalid api key", "unauthorized",
        "authentication required",
    ]):
        return ErrorClassification(
            category="auth", retryable=False,
            recommended_backoff=0, message="Auth error")
    if any(p in combined for p in ["prompt too long", "context length exceeded"]):
        return ErrorClassification(
            category="prompt_too_long", retryable=False,
            recommended_backoff=0, message="Prompt too long")
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
    if any(p in combined for p in [
        "connection error", "connection refused", "connection reset",
        "network is unreachable",
    ]):
        return ErrorClassification(
            category="connection", retryable=True,
            recommended_backoff=INITIAL_BACKOFF, message="Connection error")
    if any(p in combined for p in ["timed out", "timeout"]):
        return ErrorClassification(
            category="timeout", retryable=True,
            recommended_backoff=INITIAL_BACKOFF, message="Request timed out.")
    if returncode != 0 and stdout.strip():
        return ErrorClassification(
            category="output_truncated", retryable=False,
            recommended_backoff=0,
            message=f"CLI exited with code {returncode} with partial output")
    return ErrorClassification(
        category="unknown", retryable=False,
        recommended_backoff=0, message=f"CLI exited with code {returncode}")


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


@patch("orchestrator.llm_client.classify_llm_error", side_effect=_mock_classify)
class TestModelFlag:
    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_model_flag_passed(self, _mock_which, mock_run, _mock_cls):
        mock_run.return_value = _cp("response")
        client = ClaudeCodeClient(model="claude-sonnet-4-6")
        client.generate("hello")
        cmd = mock_run.call_args[0][0]
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "claude-sonnet-4-6"

    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_omits_model_flag_when_none(self, _mock_which, mock_run, _mock_cls):
        # UAS framework policy: when no model is explicitly set, omit
        # --model entirely so the CLI uses Claude's current default.
        mock_run.return_value = _cp("response")
        client = ClaudeCodeClient(model=None)
        client.generate("hello")
        cmd = mock_run.call_args[0][0]
        assert "--model" not in cmd


@patch("orchestrator.llm_client.classify_llm_error", side_effect=_mock_classify)
@patch("orchestrator.llm_client.PERSISTENT_RETRY", False)
class TestRetryBehaviour:
    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_retries_on_timeout(self, _mock_which, mock_run, mock_sleep, _mock_cls):
        mock_run.side_effect = [
            subprocess.TimeoutExpired(cmd="claude", timeout=120),
            _cp("ok"),
        ]
        client = ClaudeCodeClient()
        result = client.generate("test")
        assert result.text == "ok"
        assert mock_run.call_count == 2
        assert mock_sleep.call_count == 1

    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_retries_on_transient_stderr(self, _mock_which, mock_run, mock_sleep, _mock_cls):
        mock_run.side_effect = [
            _cp("", "Connection refused", 1),
            _cp("ok"),
        ]
        client = ClaudeCodeClient()
        result = client.generate("test")
        assert result.text == "ok"
        assert mock_run.call_count == 2

    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_raises_after_max_retries(self, _mock_which, mock_run, mock_sleep, _mock_cls):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=120)
        client = ClaudeCodeClient()
        with pytest.raises(RuntimeError, match="timed out"):
            client.generate("test")
        assert mock_run.call_count == 1 + MAX_RETRIES

    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_no_retry_on_non_transient_error(self, _mock_which, mock_run, mock_sleep, _mock_cls):
        mock_run.return_value = _cp("", "Invalid API key", 1)
        client = ClaudeCodeClient()
        with pytest.raises(RuntimeError):
            client.generate("test")
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_no_retry_on_file_not_found(self, _mock_which, mock_run, _mock_cls):
        mock_run.side_effect = FileNotFoundError("No such file")
        client = ClaudeCodeClient()
        with pytest.raises(RuntimeError, match="not found in PATH"):
            client.generate("test")
        assert mock_run.call_count == 1

    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_exponential_backoff(self, _mock_which, mock_run, mock_sleep, _mock_cls):
        mock_run.side_effect = [
            subprocess.TimeoutExpired(cmd="claude", timeout=120),
            subprocess.TimeoutExpired(cmd="claude", timeout=120),
            _cp("ok"),
        ]
        client = ClaudeCodeClient()
        client.generate("test")
        assert mock_sleep.call_count == 2
        # First backoff: 2 * 2^0 = 2s, second: 2 * 2^1 = 4s
        mock_sleep.assert_any_call(INITIAL_BACKOFF)
        mock_sleep.assert_any_call(INITIAL_BACKOFF * 2)

    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_overloaded_uses_longer_backoff(self, _mock_which, mock_run, mock_sleep, _mock_cls):
        """529 overloaded errors should use OVERLOADED_BACKOFF, not INITIAL_BACKOFF."""
        mock_run.side_effect = [
            _cp("", 'API Error: 529 {"type":"error","error":{"type":"overloaded_error"}}', 1),
            _cp("ok"),
        ]
        client = ClaudeCodeClient()
        result = client.generate("test")
        assert result.text == "ok"
        assert mock_run.call_count == 2
        # Overloaded backoff: OVERLOADED_BACKOFF * 2^0
        mock_sleep.assert_called_with(OVERLOADED_BACKOFF)

    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_529_retries_until_success(self, _mock_which, mock_run, mock_sleep, _mock_cls):
        """529 errors should be retried across multiple attempts."""
        mock_run.side_effect = [
            _cp("", "529 overloaded_error", 1),
            _cp("", "529 overloaded_error", 1),
            _cp("", "529 overloaded_error", 1),
            _cp("ok"),
        ]
        client = ClaudeCodeClient()
        result = client.generate("test")
        assert result.text == "ok"
        assert mock_run.call_count == 4


@patch("orchestrator.llm_client.classify_llm_error", side_effect=_mock_classify)
class TestClassifyError:
    """Tests for classify_error wrapper around classify_llm_error fuzzy fn."""

    def test_passthrough(self, _mock_cls):
        """classify_error passes through results from classify_llm_error."""
        err = classify_error(1, "", "request timed out")
        assert err.category == "timeout"
        assert err.retryable is True

    def test_returns_error_classification(self, _mock_cls):
        """Return type is ErrorClassification."""
        err = classify_error(1, "", "Connection refused")
        assert isinstance(err, ErrorClassification)

    def test_fallback_on_fuzzy_failure(self, _mock_cls):
        """When classify_llm_error raises, classify_error returns unknown."""
        with patch("orchestrator.llm_client.classify_llm_error",
                   side_effect=RuntimeError("API down")):
            err = classify_error(1, "", "Connection refused")
        assert err.category == "unknown"
        assert err.retryable is False

    def test_rate_limit(self, _mock_cls):
        err = classify_error(1, "", "rate limit exceeded")
        assert err.category == "rate_limit"
        assert err.retryable is True

    def test_capacity(self, _mock_cls):
        err = classify_error(1, "", "529 overloaded")
        assert err.category == "capacity"
        assert err.retryable is True

    def test_auth(self, _mock_cls):
        err = classify_error(1, "", "Invalid API key")
        assert err.category == "auth"
        assert err.retryable is False

    def test_unknown_on_empty(self, _mock_cls):
        err = classify_error(1, "", "")
        assert err.category == "unknown"
        assert err.retryable is False

    def test_output_truncated(self, _mock_cls):
        err = classify_error(1, "partial output here", "some error")
        assert err.category == "output_truncated"
        assert err.retryable is False


@patch("orchestrator.llm_client.classify_llm_error", side_effect=_mock_classify)
@patch("orchestrator.llm_client.PERSISTENT_RETRY", False)
class TestRateLimitInStdout:
    """Regression tests: rate limit text in stdout must not be returned as
    valid LLM content — it should be retried as a transient error."""

    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_rate_limit_in_stdout_retried(self, _mock_which, mock_run, mock_sleep, _mock_cls):
        mock_run.side_effect = [
            _cp("You've hit your limit · resets 1pm (UTC)", "", 1),
            _cp("valid response"),
        ]
        client = ClaudeCodeClient()
        result = client.generate("test")
        assert result.text == "valid response"
        assert mock_run.call_count == 2

    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_rate_limit_in_stderr_with_stdout_retried(self, _mock_which, mock_run, mock_sleep, _mock_cls):
        """Even if stdout has content, a rate-limit in stderr should trigger retry."""
        mock_run.side_effect = [
            _cp("partial output", "rate limit exceeded", 1),
            _cp("valid response"),
        ]
        client = ClaudeCodeClient()
        result = client.generate("test")
        assert result.text == "valid response"
        assert mock_run.call_count == 2

    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_rate_limit_exhausted_raises_not_returns(self, _mock_which, mock_run, mock_sleep, _mock_cls):
        """When all transient retries are exhausted, raise instead of returning
        the rate limit message as valid LLM content."""
        mock_run.return_value = _cp("You've hit your limit · resets 6pm (UTC)", "", 1)
        client = ClaudeCodeClient()
        with pytest.raises(RuntimeError):
            client.generate("test")
        # Should have attempted initial + MAX_RETRIES = 5 calls
        assert mock_run.call_count == 1 + MAX_RETRIES

    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_non_transient_stdout_still_returned(self, _mock_which, mock_run, _mock_cls):
        """Non-transient failures with stdout should still return partial output
        for truncation recovery."""
        mock_run.return_value = _cp("partial valid code output", "some non-transient error", 1)
        client = ClaudeCodeClient()
        result = client.generate("test")
        assert result.text == "partial valid code output"


@patch("orchestrator.llm_client.classify_llm_error", side_effect=_mock_classify)
class TestPersistentRetry:
    """Tests for Section 3: UAS_PERSISTENT_RETRY mode."""

    @patch("orchestrator.llm_client.PERSISTENT_RETRY", True)
    @patch("orchestrator.llm_client._sleep_with_heartbeat")
    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_retries_beyond_max_on_429(self, _mock_which, mock_run, mock_hb_sleep, _mock_cls):
        """With PERSISTENT_RETRY, retryable errors retry beyond MAX_RETRIES."""
        errors = [_cp("", "429 Too Many Requests", 1)] * (MAX_RETRIES + 2)
        mock_run.side_effect = errors + [_cp("ok")]
        client = ClaudeCodeClient()
        result = client.generate("test")
        assert result.text == "ok"
        assert mock_run.call_count == MAX_RETRIES + 3

    @patch("orchestrator.llm_client.PERSISTENT_RETRY", True)
    @patch("orchestrator.llm_client._sleep_with_heartbeat")
    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_backoff_caps_at_max_backoff(self, _mock_which, mock_run, mock_hb_sleep, _mock_cls):
        """Backoff is capped at MAX_BACKOFF (300s) in persistent mode."""
        errors = [_cp("", "overloaded", 1)] * 6
        mock_run.side_effect = errors + [_cp("ok")]
        client = ClaudeCodeClient()
        client.generate("test")
        for call in mock_hb_sleep.call_args_list:
            assert call[0][0] <= MAX_BACKOFF

    @patch("orchestrator.llm_client.PERSISTENT_RETRY", True)
    @patch("orchestrator.llm_client._sleep_with_heartbeat")
    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_non_retryable_raises_in_persistent_mode(self, _mock_which, mock_run, mock_hb_sleep, _mock_cls):
        """Non-retryable errors still raise immediately in persistent mode."""
        mock_run.return_value = _cp("", "Invalid API key", 1)
        client = ClaudeCodeClient()
        with pytest.raises(RuntimeError):
            client.generate("test")
        assert mock_run.call_count == 1
        mock_hb_sleep.assert_not_called()

    @patch("orchestrator.llm_client.PERSISTENT_RETRY", True)
    @patch("orchestrator.llm_client._sleep_with_heartbeat")
    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_capacity_unlimited_in_persistent_mode(self, _mock_which, mock_run, mock_hb_sleep, _mock_cls):
        """In persistent mode, capacity errors retry beyond MAX_CAPACITY_RETRIES (3)."""
        errors = [_cp("", "529 overloaded_error", 1)] * 5
        mock_run.side_effect = errors + [_cp("ok")]
        client = ClaudeCodeClient()
        result = client.generate("test")
        assert result.text == "ok"
        assert mock_run.call_count == 6

    @patch("orchestrator.llm_client.PERSISTENT_RETRY", True)
    @patch("orchestrator.llm_client._sleep_with_heartbeat")
    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_uses_heartbeat_sleep(self, _mock_which, mock_run, mock_hb_sleep, _mock_cls):
        """Persistent mode uses _sleep_with_heartbeat instead of time.sleep."""
        mock_run.side_effect = [
            _cp("", "Connection refused", 1),
            _cp("ok"),
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
