"""Tests for LLM generation isolation — Section 9.

Verifies that the Claude Code CLI subprocess runs in a temporary directory
so its tool side effects (file writes, .uas_auth/, step scripts) don't
pollute the workspace.
"""

import os
import subprocess
from unittest.mock import patch

import pytest

from orchestrator.llm_client import ClaudeCodeClient, INITIAL_BACKOFF, OVERLOADED_BACKOFF
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
        "connection error", "connection refused", "connection reset",
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
            recommended_backoff=0, message="Output truncated")
    return ErrorClassification(
        category="unknown", retryable=False,
        recommended_backoff=0, message=f"CLI exited with code {returncode}")


@patch("orchestrator.llm_client.classify_llm_error", side_effect=_mock_classify)
class TestGenerateIsolation:
    """Verify that generate() isolates the CLI subprocess."""

    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_passes_temp_cwd_to_subprocess(self, _mock_which, mock_run, _mock_cls):
        """generate() should pass a temp directory as cwd to subprocess.run."""
        captured = {}

        def capture(*args, **kwargs):
            captured["cwd"] = kwargs.get("cwd")
            captured["existed"] = (
                os.path.isdir(captured["cwd"]) if captured["cwd"] else False
            )
            return subprocess.CompletedProcess(
                args=args[0], returncode=0, stdout="response", stderr="")

        mock_run.side_effect = capture
        ClaudeCodeClient().generate("hello")

        assert captured["cwd"] is not None
        assert captured["existed"] is True

    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_workspace_vars_removed_from_env(
        self, _mock_which, mock_run, _mock_cls, monkeypatch
    ):
        """WORKSPACE and UAS_WORKSPACE must not leak to the CLI."""
        monkeypatch.setenv("WORKSPACE", "/ws")
        monkeypatch.setenv("UAS_WORKSPACE", "/uas_ws")
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="response", stderr="")
        ClaudeCodeClient().generate("hello")

        env = mock_run.call_args.kwargs["env"]
        assert "WORKSPACE" not in env
        assert "UAS_WORKSPACE" not in env

    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_claude_config_dir_inside_isolation(self, _mock_which, mock_run, _mock_cls):
        """CLAUDE_CONFIG_DIR should be a subdirectory of the isolation dir."""
        captured = {}

        def capture(*args, **kwargs):
            captured["cwd"] = kwargs.get("cwd")
            captured["env"] = kwargs.get("env", {})
            return subprocess.CompletedProcess(
                args=args[0], returncode=0, stdout="response", stderr="")

        mock_run.side_effect = capture
        ClaudeCodeClient().generate("hello")

        assert captured["env"]["CLAUDE_CONFIG_DIR"].startswith(captured["cwd"])

    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_credentials_copied_to_isolation_dir(
        self, _mock_which, mock_run, _mock_cls, tmp_path, monkeypatch
    ):
        """Credential files from the original config dir should be copied."""
        # Create a fake config dir with credentials
        fake_config = tmp_path / "fake_claude"
        fake_config.mkdir()
        cred_file = fake_config / ".credentials.json"
        cred_file.write_text('{"token": "test"}')
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(fake_config))

        captured = {}

        def capture(*args, **kwargs):
            captured["env"] = kwargs.get("env", {})
            iso_cred = os.path.join(
                captured["env"]["CLAUDE_CONFIG_DIR"], ".credentials.json"
            )
            captured["cred_exists"] = os.path.isfile(iso_cred)
            return subprocess.CompletedProcess(
                args=args[0], returncode=0, stdout="response", stderr="")

        mock_run.side_effect = capture
        ClaudeCodeClient().generate("hello")

        assert captured["cred_exists"] is True

    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_isolation_dir_cleaned_up_on_success(self, _mock_which, mock_run, _mock_cls):
        """Temp dir must be removed after a successful generation."""
        captured = {}

        def capture(*args, **kwargs):
            captured["cwd"] = kwargs.get("cwd")
            return subprocess.CompletedProcess(
                args=args[0], returncode=0, stdout="response", stderr="")

        mock_run.side_effect = capture
        ClaudeCodeClient().generate("hello")

        assert not os.path.exists(captured["cwd"])

    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_isolation_dir_cleaned_up_on_error(self, _mock_which, mock_run, _mock_cls):
        """Temp dir must be removed even when the CLI fails."""
        captured = {}

        def capture(*args, **kwargs):
            captured["cwd"] = kwargs.get("cwd")
            return subprocess.CompletedProcess(
                args=args[0], returncode=1, stdout="", stderr="Invalid API key")

        mock_run.side_effect = capture
        with pytest.raises(RuntimeError):
            ClaudeCodeClient().generate("hello")

        assert not os.path.exists(captured["cwd"])

    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_same_dir_reused_across_retries(self, _mock_which, mock_run, _sleep, _mock_cls):
        """All retry attempts must share the same isolation directory."""
        cwds = []

        def capture(*args, **kwargs):
            cwds.append(kwargs.get("cwd"))
            if len(cwds) < 2:
                return subprocess.CompletedProcess(
                    args=args[0], returncode=1, stdout="", stderr="Connection refused")
            return subprocess.CompletedProcess(
                args=args[0], returncode=0, stdout="ok", stderr="")

        mock_run.side_effect = capture
        ClaudeCodeClient().generate("hello")

        assert len(cwds) == 2
        assert cwds[0] == cwds[1]


class TestClaudeMdIsolationGuidance:
    """Verify CLAUDE.md tells the CLI to produce text only."""

    def test_text_output_in_role(self):
        from orchestrator.claude_config import CLAUDE_MD_TEMPLATE

        assert "TEXT output" in CLAUDE_MD_TEMPLATE

    def test_output_mode_section_exists(self):
        from orchestrator.claude_config import CLAUDE_MD_TEMPLATE

        assert "## Output Mode" in CLAUDE_MD_TEMPLATE

    def test_no_file_creation_instruction(self):
        from orchestrator.claude_config import CLAUDE_MD_TEMPLATE

        assert "Do NOT create any files or directories" in CLAUDE_MD_TEMPLATE

    def test_no_write_tool_instruction(self):
        from orchestrator.claude_config import CLAUDE_MD_TEMPLATE

        assert (
            "Do NOT use Write, Edit, or Bash tools to create files"
            in CLAUDE_MD_TEMPLATE
        )
