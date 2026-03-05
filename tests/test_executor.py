"""Tests for architect.executor: run_orchestrator, extract_sandbox_stdout, find_engine."""

import subprocess
from unittest.mock import patch, MagicMock

import pytest

from architect.executor import (
    run_orchestrator,
    extract_sandbox_stdout,
    find_engine,
    RUN_TIMEOUT,
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
            "Verifying nested Podman...\n"
            "Nested Podman verified successfully.\n"
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
