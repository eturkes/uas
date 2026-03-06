"""Tests for architect.executor: run_orchestrator, extract_sandbox_stdout, find_engine."""

import subprocess
from unittest.mock import patch, MagicMock

import pytest

from architect.executor import (
    run_orchestrator,
    extract_sandbox_stdout,
    extract_sandbox_stderr,
    extract_workspace_files,
    parse_uas_result,
    truncate_output,
    find_engine,
    RUN_TIMEOUT,
    MAX_CONTEXT_LENGTH,
)


class TestRunOrchestratorLocal:
    @patch("architect.executor.EXECUTION_MODE", "local")
    @patch("architect.executor.subprocess.run")
    def test_local_mode_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="output", stderr="logs"
        )
        result = run_orchestrator("do something")
        assert result["exit_code"] == 0
        assert result["stdout"] == "output"
        assert result["stderr"] == "logs"

    @patch("architect.executor.EXECUTION_MODE", "local")
    @patch("architect.executor.subprocess.run")
    def test_local_mode_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=RUN_TIMEOUT)
        result = run_orchestrator("slow task")
        assert result["exit_code"] == -1
        assert "timed out" in result["stderr"]

    @patch("architect.executor.EXECUTION_MODE", "local")
    @patch("architect.executor.subprocess.run")
    def test_local_mode_passes_env(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        run_orchestrator("task")
        call_kwargs = mock_run.call_args
        env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
        assert env["UAS_TASK"] == "task"
        assert env["IS_SANDBOX"] == "1"


class TestExtractSandboxStdout:
    def test_basic_stdout(self):
        log = "stdout:\nhello world\nExit code: 0"
        assert extract_sandbox_stdout(log) == "hello world"

    def test_inline_stdout(self):
        log = "stdout: hello\nExit code: 0"
        assert extract_sandbox_stdout(log) == "hello"

    def test_multiline_stdout(self):
        log = "stdout:\nline1\nline2\nline3\nExit code: 0"
        assert extract_sandbox_stdout(log) == "line1\nline2\nline3"

    def test_stdout_terminated_by_stderr(self):
        log = "stdout:\nresult\nstderr:\nwarning"
        assert extract_sandbox_stdout(log) == "result"

    def test_stdout_terminated_by_success(self):
        log = "stdout:\nresult\nSUCCESS on attempt 1."
        assert extract_sandbox_stdout(log) == "result"

    def test_stdout_terminated_by_failed(self):
        log = "stdout:\nresult\nFAILED on attempt 1."
        assert extract_sandbox_stdout(log) == "result"

    def test_stdout_terminated_by_attempt(self):
        log = "stdout:\nresult\n--- Attempt 2/3 ---"
        assert extract_sandbox_stdout(log) == "result"

    def test_no_stdout_returns_empty(self):
        log = "stderr:\nsome error\nExit code: 1"
        assert extract_sandbox_stdout(log) == ""

    def test_empty_string(self):
        assert extract_sandbox_stdout("") == ""

    def test_realistic_orchestrator_output(self):
        log = (
            "Task: do something\n"
            "Verifying sandbox...\n"
            "Sandbox verified.\n"
            "\n--- Attempt 1/3 ---\n"
            "Querying LLM...\n"
            "Executing in sandbox...\n"
            "Exit code: 0\n"
            "stdout:\nHello, World!\n"
            "\nSUCCESS on attempt 1."
        )
        assert extract_sandbox_stdout(log) == "Hello, World!"


class TestFindEngine:
    @patch("architect.executor.shutil.which")
    def test_finds_podman(self, mock_which):
        mock_which.side_effect = lambda cmd: "/usr/bin/podman" if cmd == "podman" else None
        assert find_engine() == "podman"

    @patch("architect.executor.shutil.which")
    def test_finds_docker_when_no_podman(self, mock_which):
        mock_which.side_effect = lambda cmd: "/usr/bin/docker" if cmd == "docker" else None
        assert find_engine() == "docker"

    @patch("architect.executor.shutil.which")
    def test_prefers_podman(self, mock_which):
        mock_which.return_value = "/usr/bin/exists"
        assert find_engine() == "podman"

    @patch("architect.executor.shutil.which")
    def test_returns_none_when_neither(self, mock_which):
        mock_which.return_value = None
        assert find_engine() is None


class TestExtractSandboxStderr:
    def test_basic_stderr(self):
        log = "stderr:\nsome warning\nExit code: 0"
        assert extract_sandbox_stderr(log) == "some warning"

    def test_inline_stderr(self):
        log = "stderr: warning msg\nExit code: 0"
        assert extract_sandbox_stderr(log) == "warning msg"

    def test_multiline_stderr(self):
        log = "stderr:\nwarn1\nwarn2\nExit code: 0"
        assert extract_sandbox_stderr(log) == "warn1\nwarn2"

    def test_stderr_terminated_by_stdout(self):
        log = "stderr:\nwarn\nstdout:\nresult"
        assert extract_sandbox_stderr(log) == "warn"

    def test_no_stderr_returns_empty(self):
        log = "stdout:\nresult\nExit code: 0"
        assert extract_sandbox_stderr(log) == ""

    def test_empty_string(self):
        assert extract_sandbox_stderr("") == ""

    def test_realistic_output_with_both(self):
        log = (
            "--- Attempt 1/3 ---\n"
            "Querying LLM...\n"
            "Executing in sandbox...\n"
            "Exit code: 0\n"
            "stdout:\nHello, World!\n"
            "stderr:\nDeprecationWarning: use new API\n"
            "\nSUCCESS on attempt 1."
        )
        assert extract_sandbox_stderr(log) == "DeprecationWarning: use new API"

    def test_last_stderr_block_on_retry(self):
        log = (
            "--- Attempt 1/3 ---\n"
            "stderr:\nfirst error\n"
            "FAILED on attempt 1.\n"
            "--- Attempt 2/3 ---\n"
            "stderr:\nsecond error\n"
            "SUCCESS on attempt 2."
        )
        assert extract_sandbox_stderr(log) == "second error"


class TestTruncateOutput:
    def test_below_limit(self):
        assert truncate_output("short text") == "short text"

    def test_at_limit(self):
        text = "x" * MAX_CONTEXT_LENGTH
        assert truncate_output(text) == text

    def test_above_limit(self):
        text = "x" * (MAX_CONTEXT_LENGTH + 100)
        result = truncate_output(text)
        assert len(result) < len(text)
        assert result.startswith("x" * MAX_CONTEXT_LENGTH)
        assert "truncated" in result
        assert str(len(text)) in result

    def test_custom_limit(self):
        result = truncate_output("hello world", max_length=5)
        assert result.startswith("hello")
        assert "truncated" in result
        assert "11" in result

    def test_empty_string(self):
        assert truncate_output("") == ""


class TestExtractWorkspaceFiles:
    def test_single_file(self):
        log = "Written to /workspace/output.txt"
        assert extract_workspace_files(log) == ["/workspace/output.txt"]

    def test_multiple_files(self):
        log = (
            "Saved /workspace/data.json\n"
            "Created /workspace/results/report.csv\n"
        )
        files = extract_workspace_files(log)
        assert "/workspace/data.json" in files
        assert "/workspace/results/report.csv" in files

    def test_deduplicates(self):
        log = (
            "Reading /workspace/input.txt\n"
            "Processing /workspace/input.txt\n"
        )
        files = extract_workspace_files(log)
        assert files == ["/workspace/input.txt"]

    def test_strips_trailing_punctuation(self):
        log = "File saved to /workspace/out.txt."
        assert extract_workspace_files(log) == ["/workspace/out.txt"]

    def test_no_files(self):
        log = "No file operations performed"
        assert extract_workspace_files(log) == []

    def test_empty_string(self):
        assert extract_workspace_files("") == []

    def test_realistic_orchestrator_output(self):
        log = (
            "--- Attempt 1/3 ---\n"
            "Querying LLM...\n"
            "Executing in sandbox...\n"
            "Exit code: 0\n"
            "stdout:\n"
            "Wrote results to /workspace/analysis.json\n"
            "Summary saved to /workspace/summary.txt\n"
            "stderr:\n"
            "Processing complete\n"
            "SUCCESS on attempt 1."
        )
        files = extract_workspace_files(log)
        assert "/workspace/analysis.json" in files
        assert "/workspace/summary.txt" in files


class TestParseUasResult:
    def test_valid_result_in_orchestrator_output(self):
        output = (
            "stdout:\nsome output\n"
            'UAS_RESULT: {"status": "ok", "files_written": ["a.txt"], "summary": "done"}\n'
            "Exit code: 0"
        )
        result = parse_uas_result(output)
        assert result is not None
        assert result["status"] == "ok"
        assert result["files_written"] == ["a.txt"]

    def test_no_result_line(self):
        assert parse_uas_result("stdout:\njust regular output\nExit code: 0") is None

    def test_invalid_json(self):
        assert parse_uas_result("UAS_RESULT: {bad json}\n") is None

    def test_empty_string(self):
        assert parse_uas_result("") is None

    def test_error_result(self):
        output = 'UAS_RESULT: {"status": "error", "error": "file missing"}\n'
        result = parse_uas_result(output)
        assert result is not None
        assert result["status"] == "error"

    def test_result_among_other_output(self):
        output = (
            "--- Attempt 1/3 ---\n"
            "Querying LLM...\n"
            "Executing in sandbox...\n"
            "Exit code: 0\n"
            "stdout:\nProcessing data...\n"
            'UAS_RESULT: {"status": "ok", "files_written": [], "summary": "processed"}\n'
            "\nSUCCESS on attempt 1."
        )
        result = parse_uas_result(output)
        assert result is not None
        assert result["status"] == "ok"


class TestStdoutTruncation:
    def test_long_stdout_is_truncated(self):
        content = "x" * 10000
        log = f"stdout:\n{content}\nExit code: 0"
        result = extract_sandbox_stdout(log)
        assert "truncated" in result
        assert len(result) < len(content)

    def test_long_stderr_is_truncated(self):
        content = "y" * 10000
        log = f"stderr:\n{content}\nExit code: 0"
        result = extract_sandbox_stderr(log)
        assert "truncated" in result
        assert len(result) < len(content)
