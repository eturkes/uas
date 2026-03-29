"""Tests for LLM generation isolation — Section 9.

Verifies that the Claude Code CLI subprocess runs in a temporary directory
so its tool side effects (file writes, .uas_auth/, step scripts) don't
pollute the workspace.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.llm_client import ClaudeCodeClient


class TestGenerateIsolation:
    """Verify that generate() isolates the CLI subprocess."""

    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_passes_temp_cwd_to_run_streaming(self, _mock_which, mock_stream):
        """generate() should pass a temp directory as cwd to _run_streaming."""
        captured = {}

        def capture(*args, **kwargs):
            captured["cwd"] = kwargs.get("cwd")
            captured["existed"] = (
                os.path.isdir(captured["cwd"]) if captured["cwd"] else False
            )
            return ("response", "", 0)

        mock_stream.side_effect = capture
        ClaudeCodeClient().generate("hello")

        assert captured["cwd"] is not None
        assert captured["existed"] is True

    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_workspace_vars_removed_from_env(
        self, _mock_which, mock_stream, monkeypatch
    ):
        """WORKSPACE and UAS_WORKSPACE must not leak to the CLI."""
        monkeypatch.setenv("WORKSPACE", "/ws")
        monkeypatch.setenv("UAS_WORKSPACE", "/uas_ws")
        mock_stream.return_value = ("response", "", 0)
        ClaudeCodeClient().generate("hello")

        env = mock_stream.call_args[0][1]
        assert "WORKSPACE" not in env
        assert "UAS_WORKSPACE" not in env

    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_claude_config_dir_inside_isolation(self, _mock_which, mock_stream):
        """CLAUDE_CONFIG_DIR should be a subdirectory of the isolation dir."""
        captured = {}

        def capture(*args, **kwargs):
            captured["cwd"] = kwargs.get("cwd")
            captured["env"] = args[1]
            return ("response", "", 0)

        mock_stream.side_effect = capture
        ClaudeCodeClient().generate("hello")

        assert captured["env"]["CLAUDE_CONFIG_DIR"].startswith(captured["cwd"])

    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_credentials_copied_to_isolation_dir(
        self, _mock_which, mock_stream, tmp_path, monkeypatch
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
            captured["env"] = args[1]
            iso_cred = os.path.join(
                captured["env"]["CLAUDE_CONFIG_DIR"], ".credentials.json"
            )
            captured["cred_exists"] = os.path.isfile(iso_cred)
            return ("response", "", 0)

        mock_stream.side_effect = capture
        ClaudeCodeClient().generate("hello")

        assert captured["cred_exists"] is True

    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_isolation_dir_cleaned_up_on_success(self, _mock_which, mock_stream):
        """Temp dir must be removed after a successful generation."""
        captured = {}

        def capture(*args, **kwargs):
            captured["cwd"] = kwargs.get("cwd")
            return ("response", "", 0)

        mock_stream.side_effect = capture
        ClaudeCodeClient().generate("hello")

        assert not os.path.exists(captured["cwd"])

    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_isolation_dir_cleaned_up_on_error(self, _mock_which, mock_stream):
        """Temp dir must be removed even when the CLI fails."""
        captured = {}

        def capture(*args, **kwargs):
            captured["cwd"] = kwargs.get("cwd")
            return ("", "Invalid API key", 1)

        mock_stream.side_effect = capture
        with pytest.raises(RuntimeError):
            ClaudeCodeClient().generate("hello")

        assert not os.path.exists(captured["cwd"])

    @patch("orchestrator.llm_client.time.sleep")
    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_same_dir_reused_across_retries(self, _mock_which, mock_stream, _sleep):
        """All retry attempts must share the same isolation directory."""
        cwds = []

        def capture(*args, **kwargs):
            cwds.append(kwargs.get("cwd"))
            if len(cwds) < 2:
                return ("", "Connection refused", 1)
            return ("ok", "", 0)

        mock_stream.side_effect = capture
        ClaudeCodeClient().generate("hello")

        assert len(cwds) == 2
        assert cwds[0] == cwds[1]


class TestRunStreamingCwd:
    """Verify that _run_streaming forwards cwd to subprocess.Popen."""

    @patch("orchestrator.llm_client.subprocess.Popen")
    def test_cwd_forwarded_to_popen(self, mock_popen):
        proc = MagicMock()
        proc.stdout = iter([])
        proc.stderr.read.return_value = ""
        proc.returncode = 0
        mock_popen.return_value = proc

        ClaudeCodeClient()._run_streaming(
            ["claude", "-p"], {}, cwd="/tmp/test_dir",
        )

        _, kwargs = mock_popen.call_args
        assert kwargs["cwd"] == "/tmp/test_dir"

    @patch("orchestrator.llm_client.subprocess.Popen")
    def test_cwd_defaults_to_none(self, mock_popen):
        proc = MagicMock()
        proc.stdout = iter([])
        proc.stderr.read.return_value = ""
        proc.returncode = 0
        mock_popen.return_value = proc

        ClaudeCodeClient()._run_streaming(["claude", "-p"], {})

        _, kwargs = mock_popen.call_args
        assert kwargs.get("cwd") is None


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
