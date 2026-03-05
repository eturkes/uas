"""Tests for orchestrator.main: build_prompt, get_task, and main loop."""

import argparse
import io
import sys
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.main import build_prompt, get_task, main, MAX_RETRIES


class TestBuildPrompt:
    def test_first_attempt_no_error(self):
        prompt = build_prompt("Write hello world", attempt=1)
        assert "Write hello world" in prompt
        assert "Previous Error" not in prompt

    def test_includes_environment_section(self):
        prompt = build_prompt("any task", attempt=1)
        assert "Environment" in prompt
        assert "WORKSPACE" in prompt

    def test_includes_constraints(self):
        prompt = build_prompt("any task", attempt=1)
        assert "Exit with code 0" in prompt
        assert "stdout" in prompt

    def test_includes_sandbox_constraints(self):
        prompt = build_prompt("any task", attempt=1)
        assert "full network access" in prompt
        assert "install packages freely" in prompt

    def test_with_previous_error(self):
        prompt = build_prompt("fix it", attempt=2, previous_error="NameError: x")
        assert "Previous Error (attempt 1)" in prompt
        assert "NameError: x" in prompt
        assert "Fix the error" in prompt

    def test_no_error_section_on_attempt1_even_with_error(self):
        prompt = build_prompt("task", attempt=1, previous_error="some error")
        assert "Previous Error" not in prompt

    def test_no_error_section_when_error_is_none(self):
        prompt = build_prompt("task", attempt=2, previous_error=None)
        assert "Previous Error" not in prompt


class TestGetTask:
    def test_from_cli_args(self):
        args = argparse.Namespace(task=["hello", "world"])
        assert get_task(args) == "hello world"

    def test_from_env_var(self, monkeypatch):
        monkeypatch.setenv("UAS_TASK", "env task")
        args = argparse.Namespace(task=[])
        assert get_task(args) == "env task"

    def test_from_stdin(self, monkeypatch):
        monkeypatch.setenv("UAS_TASK", "")
        monkeypatch.delenv("UAS_TASK", raising=False)
        args = argparse.Namespace(task=[])
        fake_stdin = io.StringIO("stdin task\n")
        fake_stdin.isatty = lambda: False
        with patch.object(sys, "stdin", fake_stdin):
            assert get_task(args) == "stdin task"

    def test_cli_args_take_precedence(self, monkeypatch):
        monkeypatch.setenv("UAS_TASK", "env task")
        args = argparse.Namespace(task=["cli", "task"])
        assert get_task(args) == "cli task"


class TestMainLoop:
    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_success_on_first_attempt(self, mock_client_factory, mock_sandbox, mock_args):
        mock_args.return_value = argparse.Namespace(task=["test task"], verbose=False)
        mock_client = MagicMock()
        mock_client.generate.return_value = '```python\nprint("hello")\n```'
        mock_client_factory.return_value = mock_client
        mock_sandbox.return_value = {"exit_code": 0, "stdout": "hello", "stderr": ""}

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        assert mock_client.generate.call_count == 1
        # Two sandbox calls: verify + execute
        assert mock_sandbox.call_count == 2

    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_retry_on_sandbox_failure(self, mock_client_factory, mock_sandbox, mock_args):
        mock_args.return_value = argparse.Namespace(task=["test task"], verbose=False)
        mock_client = MagicMock()
        mock_client.generate.return_value = '```python\nprint("hello")\n```'
        mock_client_factory.return_value = mock_client
        # Verify succeeds, first exec fails, second exec succeeds
        mock_sandbox.side_effect = [
            {"exit_code": 0, "stdout": "sandbox OK", "stderr": ""},  # verify
            {"exit_code": 1, "stdout": "", "stderr": "error msg"},   # attempt 1
            {"exit_code": 0, "stdout": "done", "stderr": ""},        # attempt 2
        ]

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        assert mock_client.generate.call_count == 2

    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_failure_after_all_retries(self, mock_client_factory, mock_sandbox, mock_args):
        mock_args.return_value = argparse.Namespace(task=["test task"], verbose=False)
        mock_client = MagicMock()
        mock_client.generate.return_value = '```python\nprint("hello")\n```'
        mock_client_factory.return_value = mock_client
        # Verify succeeds, all attempts fail
        mock_sandbox.side_effect = [
            {"exit_code": 0, "stdout": "sandbox OK", "stderr": ""},
        ] + [
            {"exit_code": 1, "stdout": "", "stderr": "error"} for _ in range(MAX_RETRIES)
        ]

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        assert mock_client.generate.call_count == MAX_RETRIES

    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_empty_code_extraction(self, mock_client_factory, mock_sandbox, mock_args):
        mock_args.return_value = argparse.Namespace(task=["test task"], verbose=False)
        mock_client = MagicMock()
        # LLM returns text with no code block
        mock_client.generate.return_value = "I cannot do that."
        mock_client_factory.return_value = mock_client
        mock_sandbox.return_value = {"exit_code": 0, "stdout": "sandbox OK", "stderr": ""}

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        # Sandbox only called once (verify), never for execution
        assert mock_sandbox.call_count == 1

    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_no_task_exits_1(self, mock_client_factory, mock_sandbox, mock_args, monkeypatch):
        monkeypatch.delenv("UAS_TASK", raising=False)
        mock_args.return_value = argparse.Namespace(task=[], verbose=False)
        fake_stdin = io.StringIO("")
        fake_stdin.isatty = lambda: False
        with patch.object(sys, "stdin", fake_stdin):
            with pytest.raises(SystemExit) as exc_info:
                main()
        assert exc_info.value.code == 1
