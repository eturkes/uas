"""Tests for Section 5: Claude CLI Optimization.

Covers: JSON output mode (5a), workspace scanning (5b), model tiering (5c),
and delimited output parsing (5d).
"""

import json
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.llm_client import (
    ClaudeCodeClient,
    _extract_from_json,
    get_llm_client,
)
from orchestrator.parser import extract_code, extract_code_from_json
from orchestrator.main import (
    scan_workspace,
    STDOUT_START, STDOUT_END, STDERR_START, STDERR_END,
)
from architect.executor import (
    extract_sandbox_stdout,
    extract_sandbox_stderr,
)


# ── Section 5a: JSON output mode ──────────────────────────────────────────


class TestExtractFromJson:
    def test_valid_json_response(self):
        raw = json.dumps({"result": "Hello world", "cost_usd": 0.01})
        assert _extract_from_json(raw) == "Hello world"

    def test_result_with_whitespace(self):
        raw = json.dumps({"result": "  response text  "})
        assert _extract_from_json(raw) == "response text"

    def test_missing_result_key(self):
        raw = json.dumps({"output": "something"})
        assert _extract_from_json(raw) is None

    def test_invalid_json(self):
        assert _extract_from_json("not json") is None

    def test_empty_result(self):
        raw = json.dumps({"result": ""})
        assert _extract_from_json(raw) == ""

    def test_non_dict_json(self):
        raw = json.dumps([1, 2, 3])
        assert _extract_from_json(raw) is None

    def test_result_not_string(self):
        raw = json.dumps({"result": 42})
        assert _extract_from_json(raw) is None


class TestJsonOutputFlag:
    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_json_flag_added_for_non_streaming(self, _mock_which, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"result": "response text"}),
            stderr="",
        )
        client = ClaudeCodeClient()
        result = client.generate("hello", stream=False)
        cmd = mock_run.call_args[0][0]
        assert "--output-format" in cmd
        idx = cmd.index("--output-format")
        assert cmd[idx + 1] == "json"
        assert result == "response text"

    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_json_fallback_to_text(self, _mock_which, mock_run):
        """When JSON parsing fails, fall back to raw text."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="plain text response",
            stderr="",
        )
        client = ClaudeCodeClient()
        result = client.generate("hello")
        assert result == "plain text response"

    @patch("orchestrator.llm_client.ClaudeCodeClient._run_streaming")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_no_json_flag_for_streaming(self, _mock_which, mock_stream):
        mock_stream.return_value = ("response text", "", 0)
        client = ClaudeCodeClient()
        client.generate("hello", stream=True)
        cmd = mock_stream.call_args[0][0]
        assert "--output-format" not in cmd


class TestExtractCodeFromJson:
    def test_json_with_python_code(self):
        response = json.dumps({
            "result": '```python\nprint("hello")\n```',
        })
        assert extract_code_from_json(response) == 'print("hello")'

    def test_non_json_returns_none(self):
        assert extract_code_from_json("not json") is None

    def test_json_without_code(self):
        response = json.dumps({"result": "no code here"})
        assert extract_code_from_json(response) is None


class TestExtractCodeJsonIntegration:
    def test_json_wrapped_response(self):
        """extract_code should handle JSON-wrapped responses transparently."""
        response = json.dumps({
            "result": '```python\nimport os\nprint(os.getcwd())\n```',
        })
        code = extract_code(response)
        assert code is not None
        assert "import os" in code

    def test_plain_text_still_works(self):
        response = '```python\nprint("hello")\n```'
        assert extract_code(response) == 'print("hello")'


# ── Section 5b: Workspace scanning ────────────────────────────────────────


class TestScanWorkspace:
    def test_scans_files(self, tmp_path):
        (tmp_path / "output.txt").write_text("hello")
        (tmp_path / "data.json").write_text("{}")
        result = scan_workspace(str(tmp_path))
        assert "output.txt" in result
        assert "data.json" in result
        assert "text" in result

    def test_skips_hidden_dirs(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("x")
        (tmp_path / "visible.txt").write_text("y")
        result = scan_workspace(str(tmp_path))
        assert "visible.txt" in result
        assert ".git" not in result

    def test_skips_state_dir(self, tmp_path):
        state_dir = tmp_path / ".state"
        state_dir.mkdir()
        (state_dir / "state.json").write_text("{}")
        result = scan_workspace(str(tmp_path))
        assert ".state" not in result

    def test_nonexistent_path(self):
        assert scan_workspace("/nonexistent/path") == ""

    def test_empty_workspace(self, tmp_path):
        assert scan_workspace(str(tmp_path)) == ""

    def test_includes_directories(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "main.py").write_text("print(1)")
        result = scan_workspace(str(tmp_path))
        assert "src/" in result
        assert "main.py" in result


# ── Section 5c: Model tiering ─────────────────────────────────────────────


class TestModelTiering:
    def test_planner_model_from_env(self, monkeypatch):
        monkeypatch.setenv("UAS_MODEL_PLANNER", "claude-haiku-4-5")
        client = get_llm_client(role="planner")
        assert client.model == "claude-haiku-4-5"

    def test_coder_model_from_env(self, monkeypatch):
        monkeypatch.setenv("UAS_MODEL_CODER", "claude-sonnet-4-6")
        client = get_llm_client(role="coder")
        assert client.model == "claude-sonnet-4-6"

    def test_falls_back_to_uas_model(self, monkeypatch):
        monkeypatch.setenv("UAS_MODEL", "claude-opus-4-6")
        monkeypatch.delenv("UAS_MODEL_PLANNER", raising=False)
        client = get_llm_client(role="planner")
        assert client.model == "claude-opus-4-6"

    def test_role_specific_overrides_uas_model(self, monkeypatch):
        monkeypatch.setenv("UAS_MODEL", "claude-opus-4-6")
        monkeypatch.setenv("UAS_MODEL_CODER", "claude-sonnet-4-6")
        client = get_llm_client(role="coder")
        assert client.model == "claude-sonnet-4-6"

    def test_no_role_uses_uas_model(self, monkeypatch):
        monkeypatch.setenv("UAS_MODEL", "claude-opus-4-6")
        client = get_llm_client()
        assert client.model == "claude-opus-4-6"

    def test_no_role_no_env_gives_none(self, monkeypatch):
        monkeypatch.delenv("UAS_MODEL", raising=False)
        monkeypatch.delenv("UAS_MODEL_PLANNER", raising=False)
        monkeypatch.delenv("UAS_MODEL_CODER", raising=False)
        client = get_llm_client()
        assert client.model is None

    def test_unknown_role_uses_uas_model(self, monkeypatch):
        monkeypatch.setenv("UAS_MODEL", "claude-opus-4-6")
        client = get_llm_client(role="unknown")
        assert client.model == "claude-opus-4-6"


# ── Section 5d: Delimited output parsing ──────────────────────────────────


class TestDelimitedStdoutExtraction:
    def test_basic_delimited_stdout(self):
        log = (
            f"Exit code: 0\n"
            f"{STDOUT_START}\n"
            f"Hello, World!\n"
            f"{STDOUT_END}\n"
            f"SUCCESS on attempt 1."
        )
        assert extract_sandbox_stdout(log) == "Hello, World!"

    def test_multiline_delimited_stdout(self):
        log = (
            f"{STDOUT_START}\n"
            f"line1\nline2\nline3\n"
            f"{STDOUT_END}\n"
        )
        assert extract_sandbox_stdout(log) == "line1\nline2\nline3"

    def test_delimited_with_retries_uses_last(self):
        log = (
            f"{STDOUT_START}\nfirst attempt\n{STDOUT_END}\n"
            f"FAILED on attempt 1.\n"
            f"{STDOUT_START}\nsecond attempt\n{STDOUT_END}\n"
            f"SUCCESS on attempt 2."
        )
        assert extract_sandbox_stdout(log) == "second attempt"

    def test_falls_back_to_regex_when_no_delimiters(self):
        log = "stdout:\nhello world\nExit code: 0"
        assert extract_sandbox_stdout(log) == "hello world"


class TestDelimitedStderrExtraction:
    def test_basic_delimited_stderr(self):
        log = (
            f"{STDERR_START}\n"
            f"warning: deprecated\n"
            f"{STDERR_END}\n"
        )
        assert extract_sandbox_stderr(log) == "warning: deprecated"

    def test_delimited_with_retries_uses_last(self):
        log = (
            f"{STDERR_START}\nfirst err\n{STDERR_END}\n"
            f"{STDERR_START}\nsecond err\n{STDERR_END}\n"
        )
        assert extract_sandbox_stderr(log) == "second err"

    def test_falls_back_to_regex_when_no_delimiters(self):
        log = "stderr:\nsome error\nExit code: 1"
        assert extract_sandbox_stderr(log) == "some error"


class TestDelimitedBothStreams:
    def test_realistic_orchestrator_output(self):
        uas_line = 'UAS_RESULT: {"status": "ok"}'
        log = (
            "Task: do something\n"
            "Verifying sandbox...\n"
            "Sandbox verified.\n"
            "\n--- Attempt 1/3 ---\n"
            "Querying LLM...\n"
            "Executing in sandbox...\n"
            "Exit code: 0\n"
            f"{STDOUT_START}\n"
            f"Hello, World!\n"
            f"{uas_line}\n"
            f"{STDOUT_END}\n"
            f"{STDERR_START}\n"
            "DeprecationWarning: use new API\n"
            f"{STDERR_END}\n"
            "\nSUCCESS on attempt 1."
        )
        assert extract_sandbox_stdout(log) == f"Hello, World!\n{uas_line}"
        assert extract_sandbox_stderr(log) == "DeprecationWarning: use new API"
