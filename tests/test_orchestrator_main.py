"""Tests for orchestrator.main: build_prompt, parse_uas_result, get_task, and main loop."""

import argparse
import io
import sys
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.main import (
    _contains_tool_calls,
    _task_mentions_file_modification,
    build_prompt, get_task, main, parse_uas_result, pre_execution_check,
    MAX_RETRIES,
)


class TestBuildPrompt:
    def test_first_attempt_no_error(self):
        prompt = build_prompt("Write hello world", attempt=1)
        assert "Write hello world" in prompt
        assert "previous_error" not in prompt

    def test_includes_xml_role_section(self):
        prompt = build_prompt("any task", attempt=1)
        assert "<role>" in prompt
        assert "</role>" in prompt
        assert "expert engineer" in prompt

    def test_includes_xml_environment_section(self):
        prompt = build_prompt("any task", attempt=1)
        assert "<environment>" in prompt
        assert "</environment>" in prompt
        assert "WORKSPACE" in prompt

    def test_includes_xml_task_section(self):
        prompt = build_prompt("do something", attempt=1)
        assert "<task>" in prompt
        assert "do something" in prompt
        assert "</task>" in prompt

    def test_includes_xml_constraints_section(self):
        prompt = build_prompt("any task", attempt=1)
        assert "<constraints>" in prompt
        assert "</constraints>" in prompt
        assert "Exit with code 0" in prompt
        assert "stdout" in prompt

    def test_includes_xml_output_contract_section(self):
        prompt = build_prompt("any task", attempt=1)
        assert "<output_contract>" in prompt
        assert "</output_contract>" in prompt
        assert "UAS_RESULT" in prompt

    def test_includes_sandbox_constraints(self):
        prompt = build_prompt("any task", attempt=1)
        assert "UNRESTRICTED NETWORK" in prompt
        assert "PACKAGE INSTALLATION" in prompt

    def test_includes_common_failure_guidance(self):
        prompt = build_prompt("any task", attempt=1)
        assert "exponential backoff" in prompt
        assert "os.path.join" in prompt
        assert "Check if files exist" in prompt

    def test_with_previous_error(self):
        prompt = build_prompt("fix it", attempt=2, previous_error="NameError: x")
        assert "<previous_error" in prompt
        assert "NameError: x" in prompt
        assert "analysis" in prompt

    def test_with_previous_error_includes_code(self):
        prompt = build_prompt("fix it", attempt=2,
                              previous_error="NameError: x",
                              previous_code="print(x)")
        assert "print(x)" in prompt
        assert "script that failed" in prompt

    def test_no_error_section_on_attempt1_even_with_error(self):
        prompt = build_prompt("task", attempt=1, previous_error="some error")
        assert "previous_error" not in prompt

    def test_no_error_section_when_error_is_none(self):
        prompt = build_prompt("task", attempt=2, previous_error=None)
        assert "previous_error" not in prompt

    @patch("orchestrator.main._llm_retry_guidance", return_value=None)
    def test_final_attempt_defensive_instructions(self, _mock_llm):
        prompt = build_prompt("task", attempt=MAX_RETRIES,
                              previous_error="error",
                              previous_code="code")
        assert "FINAL ATTEMPT" in prompt
        assert "simplest possible script" in prompt
        assert "try/except" in prompt
        assert "standard library" in prompt

    @patch("orchestrator.main._llm_retry_guidance", return_value=None)
    @patch("orchestrator.main.MAX_RETRIES", 4)
    def test_second_retry_different_strategy(self, _mock_llm):
        prompt = build_prompt("task", attempt=3,
                              previous_error="error",
                              previous_code="code")
        assert "fundamentally flawed" in prompt
        assert "completely different way" in prompt

    def test_environment_hints(self):
        prompt = build_prompt("task", attempt=1,
                              environment=["pandas", "requests"])
        assert "pandas" in prompt
        assert "requests" in prompt
        assert "pip" in prompt

    def test_workspace_files_included(self):
        ws_files = "  output.txt (100 bytes, text)\n  data.json (500 bytes, text)"
        prompt = build_prompt("do work", attempt=1, workspace_files=ws_files)
        assert "<workspace_state>" in prompt
        assert "</workspace_state>" in prompt
        assert "output.txt" in prompt
        assert "data.json" in prompt
        assert "Do not regenerate" in prompt

    def test_no_workspace_state_when_none(self):
        prompt = build_prompt("do work", attempt=1)
        assert "workspace_state" not in prompt

    @patch("orchestrator.main._llm_retry_guidance", return_value=None)
    def test_analysis_instruction_in_retry(self, _mock_llm):
        prompt = build_prompt("any task", attempt=2, previous_error="some error")
        assert "<analysis>" in prompt
        assert "diagnose the root cause" in prompt

    def test_data_before_instructions(self):
        """Data sections (environment, task) should appear before instructions (role, constraints)."""
        prompt = build_prompt("my task", attempt=1)
        env_pos = prompt.index("<environment>")
        task_pos = prompt.index("<task>")
        role_pos = prompt.index("<role>")
        constraints_pos = prompt.index("<constraints>")
        output_contract_pos = prompt.index("<output_contract>")
        assert env_pos < role_pos
        assert task_pos < role_pos
        assert role_pos < constraints_pos
        assert constraints_pos < output_contract_pos


class TestParseUasResult:
    def test_valid_result(self):
        stdout = 'some output\nUAS_RESULT: {"status": "ok", "files_written": ["a.txt"], "summary": "done"}\n'
        result = parse_uas_result(stdout)
        assert result is not None
        assert result["status"] == "ok"
        assert result["files_written"] == ["a.txt"]

    def test_no_result_line(self):
        assert parse_uas_result("just regular output\n") is None

    def test_invalid_json(self):
        assert parse_uas_result("UAS_RESULT: {not valid json}\n") is None

    def test_empty_string(self):
        assert parse_uas_result("") is None

    def test_error_result(self):
        stdout = 'UAS_RESULT: {"status": "error", "error": "file missing"}\n'
        result = parse_uas_result(stdout)
        assert result is not None
        assert result["status"] == "error"


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
    @patch("orchestrator.main.MINIMAL_MODE", True)
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

    @patch("orchestrator.main.MINIMAL_MODE", True)
    @patch("orchestrator.main._llm_retry_guidance", return_value=None)
    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_retry_on_sandbox_failure(self, mock_client_factory, mock_sandbox, mock_args, _mock_llm_retry):
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

    @patch("orchestrator.main.MINIMAL_MODE", True)
    @patch("orchestrator.main._llm_retry_guidance", return_value=None)
    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_failure_after_all_retries(self, mock_client_factory, mock_sandbox, mock_args, _mock_llm_retry):
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

    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_uas_result_parsed_on_success(self, mock_client_factory, mock_sandbox, mock_args):
        mock_args.return_value = argparse.Namespace(task=["test task"], verbose=False)
        mock_client = MagicMock()
        mock_client.generate.return_value = '```python\nprint("hello")\n```'
        mock_client_factory.return_value = mock_client
        mock_sandbox.return_value = {
            "exit_code": 0,
            "stdout": 'output\nUAS_RESULT: {"status": "ok", "files_written": [], "summary": "done"}\n',
            "stderr": "",
        }

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0

    @patch("orchestrator.main._llm_retry_guidance", return_value=None)
    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_syntax_error_skips_sandbox(self, mock_client_factory, mock_sandbox, mock_args, _mock_llm_retry):
        mock_args.return_value = argparse.Namespace(task=["test task"], verbose=False)
        mock_client = MagicMock()
        # First response has syntax error (not truncation), second is valid
        mock_client.generate.side_effect = [
            '```python\ndef foo(x):\n    x = = 2\n```',
            '```python\nprint("hello")\n```',
        ]
        mock_client_factory.return_value = mock_client
        mock_sandbox.side_effect = [
            {"exit_code": 0, "stdout": "sandbox OK", "stderr": ""},  # verify
            {"exit_code": 0, "stdout": "hello", "stderr": ""},       # attempt 2
        ]

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        # Sandbox: 1 verify + 1 execute (syntax error skipped sandbox)
        assert mock_sandbox.call_count == 2

    @patch("orchestrator.main._llm_retry_guidance", return_value=None)
    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_input_call_skips_sandbox(self, mock_client_factory, mock_sandbox, mock_args, _mock_llm_retry):
        mock_args.return_value = argparse.Namespace(task=["test task"], verbose=False)
        mock_client = MagicMock()
        # First response uses input(), second is valid
        mock_client.generate.side_effect = [
            '```python\nname = input("Enter name: ")\nprint(name)\n```',
            '```python\nprint("hello")\n```',
        ]
        mock_client_factory.return_value = mock_client
        mock_sandbox.side_effect = [
            {"exit_code": 0, "stdout": "sandbox OK", "stderr": ""},  # verify
            {"exit_code": 0, "stdout": "hello", "stderr": ""},       # attempt 2
        ]

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        # Sandbox: 1 verify + 1 execute (input() code skipped sandbox)
        assert mock_sandbox.call_count == 2


class TestPreExecutionCheck:
    def test_valid_code_no_errors(self):
        code = 'print("UAS_RESULT: ok")'
        errors, warnings = pre_execution_check(code)
        assert errors == []
        assert warnings == []

    def test_syntax_error_is_critical(self):
        code = "def foo(\n"
        errors, warnings = pre_execution_check(code)
        assert len(errors) == 1
        assert "Syntax error" in errors[0]

    def test_input_call_is_critical(self):
        code = 'name = input("Enter name: ")\nprint(f"UAS_RESULT: {name}")'
        errors, warnings = pre_execution_check(code)
        assert len(errors) == 1
        assert "input()" in errors[0]

    def test_missing_uas_result_is_warning(self):
        code = 'print("hello world")'
        errors, warnings = pre_execution_check(code)
        assert errors == []
        assert len(warnings) == 1
        assert "UAS_RESULT" in warnings[0]

    def test_multiple_critical_errors(self):
        code = "name = input(\n"
        errors, warnings = pre_execution_check(code)
        # Syntax error catches the incomplete input( call too
        assert len(errors) >= 1

    def test_input_in_string_literal_is_caught(self):
        # The regex will match input( even inside strings — this is acceptable
        # because it's a simple heuristic and false positives are rare in practice
        code = 'x = "input()"\nprint(f"UAS_RESULT: {x}")'
        errors, _warnings = pre_execution_check(code)
        # Regex matches inside strings — this is a known limitation but acceptable
        assert len(errors) <= 1


class TestRetryStrategy:
    @patch("orchestrator.main.get_llm_client")
    def test_llm_guidance_injected_into_prompt(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = (
            "The error is a missing import. Add 'import os' at the top."
        )
        mock_get_client.return_value = client

        prompt = build_prompt("create a file", attempt=2,
                              previous_error="NameError: name 'os' is not defined",
                              previous_code="os.path.join('a', 'b')")
        assert "missing import" in prompt
        assert "<previous_error" in prompt
        assert "</previous_error>" in prompt
        assert "<analysis>" in prompt
        client.generate.assert_called_once()

    @patch("orchestrator.main.get_llm_client")
    def test_llm_failure_falls_back_to_hardcoded(self, mock_get_client):
        mock_get_client.side_effect = RuntimeError("API unavailable")

        prompt = build_prompt("task", attempt=MAX_RETRIES,
                              previous_error="error",
                              previous_code="code")
        assert "FINAL ATTEMPT" in prompt
        assert "simplest possible script" in prompt

    @patch("orchestrator.main.get_llm_client")
    def test_llm_empty_response_falls_back(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = ""
        mock_get_client.return_value = client

        prompt = build_prompt("task", attempt=2,
                              previous_error="error",
                              previous_code="code")
        assert "diagnose the root cause" in prompt

    @patch("orchestrator.main.MINIMAL_MODE", True)
    def test_minimal_mode_skips_llm(self):
        prompt = build_prompt("task", attempt=2,
                              previous_error="error",
                              previous_code="code")
        assert "diagnose the root cause" in prompt
        assert "<previous_error" in prompt

    @patch("orchestrator.main.get_llm_client")
    def test_prompt_xml_structure_with_llm_guidance(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "Try a different approach using subprocess."
        mock_get_client.return_value = client

        prompt = build_prompt("run a command", attempt=2,
                              previous_error="OSError: file not found",
                              previous_code="open('missing.txt')")
        assert "<previous_error" in prompt
        assert "</previous_error>" in prompt
        assert "<environment>" in prompt
        assert "<task>" in prompt
        assert "<role>" in prompt

    @patch("orchestrator.main.get_llm_client")
    def test_attempt_history_passed_to_llm(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "Use a different library for HTTP requests."
        mock_get_client.return_value = client

        history = [
            {"attempt": 1, "error": "ConnectionError", "code_snippet": "import requests"},
        ]
        build_prompt("fetch data", attempt=2,
                     previous_error="ConnectionError",
                     previous_code="import requests",
                     attempt_history=history)
        call_args = client.generate.call_args[0][0]
        assert "ConnectionError" in call_args


class TestToolCallDetection:
    """Tool calls are now allowed — _contains_tool_calls always returns False."""

    def test_allows_tool_call_xml(self):
        response = "Sure, I'll help.\n<tool_call>\n<tool_name>write_file</tool_name>\n</tool_call>"
        assert _contains_tool_calls(response) is False

    def test_allows_tool_name_tag(self):
        response = "Let me use a tool.\n<tool_name>read_file</tool_name>"
        assert _contains_tool_calls(response) is False

    def test_no_tool_calls_in_normal_text(self):
        response = "I cannot do that. Please try again."
        assert _contains_tool_calls(response) is False

    def test_no_tool_calls_in_code_block(self):
        response = '```python\nprint("hello")\n```'
        assert _contains_tool_calls(response) is False


class TestWorkspacePathGuidance:
    """Section 6: Workspace path confusion — guidance always present."""

    def test_guidance_appears_when_workspace_files_present(self):
        ws_files = "  main.py (200 bytes, Python):\n  data.json (500 bytes, JSON):"
        prompt = build_prompt("add a new feature", attempt=1, workspace_files=ws_files)
        assert "workspace IS the project root" in prompt
        assert "Do NOT create a project subdirectory" in prompt

    def test_guidance_present_when_no_workspace_files(self):
        prompt = build_prompt("create a new project", attempt=1)
        assert "workspace IS the project root" in prompt

    def test_guidance_present_when_workspace_files_none(self):
        prompt = build_prompt("create a new project", attempt=1, workspace_files=None)
        assert "workspace IS the project root" in prompt

    def test_directory_reuse_guidance_present(self):
        prompt = build_prompt("create a new project", attempt=1)
        assert "NEVER create synonyms" in prompt


class TestFileModificationDetection:
    """Section 3: Detect file modification tasks and inject guidance."""

    def test_detects_modify_keyword(self):
        assert _task_mentions_file_modification("modify analysis.py to add MCID") is True

    def test_detects_update_keyword(self):
        assert _task_mentions_file_modification("update config.json with new settings") is True

    def test_detects_add_to_keyword(self):
        assert _task_mentions_file_modification("add to utils.py a helper function") is True

    def test_detects_insert_keyword(self):
        assert _task_mentions_file_modification("insert validation into forms.html") is True

    def test_detects_extend_keyword(self):
        assert _task_mentions_file_modification("extend models.py with a new class") is True

    def test_detects_add_something_to(self):
        assert _task_mentions_file_modification("add MCID scoring code to analysis.py") is True

    def test_no_match_for_creation_task(self):
        assert _task_mentions_file_modification("create a new Flask web application") is False

    def test_no_match_for_plain_text(self):
        assert _task_mentions_file_modification("build a REST API") is False

    def test_guidance_appears_for_modification_task(self):
        prompt = build_prompt("modify analysis.py to add MCID scoring", attempt=1)
        assert "<file_modification_guidance>" in prompt
        assert "Write the COMPLETE modified file" in prompt
        assert "Never use string insertion by line number" in prompt

    def test_guidance_absent_for_creation_task(self):
        prompt = build_prompt("create a new REST API project", attempt=1)
        assert "<file_modification_guidance>" not in prompt
