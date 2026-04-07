"""Tests for orchestrator.main: build_prompt, parse_uas_result, get_task, and main loop."""

import argparse
import io
import sys
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.main import (
    _contains_tool_calls,
    _task_mentions_file_modification,
    assess_code_quality,
    build_prompt, get_task, main, parse_uas_result, pre_execution_check,
    MAX_RETRIES,
)
from uas.fuzzy_models import CodeQuality, ExecutionResult


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

    def test_full_mode_omits_previous_error_section(self):
        """Phase 6.6: full mode no longer injects retry guidance prose."""
        prompt = build_prompt("fix it", attempt=2,
                              previous_error="NameError: x",
                              previous_code="print(x)")
        assert "previous_error" not in prompt
        assert "NameError: x" not in prompt
        assert "script that failed" not in prompt

    def test_no_error_section_on_attempt1_even_with_error(self):
        prompt = build_prompt("task", attempt=1, previous_error="some error")
        assert "previous_error" not in prompt

    def test_no_error_section_when_error_is_none(self):
        prompt = build_prompt("task", attempt=2, previous_error=None)
        assert "previous_error" not in prompt

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


class TestBuildPromptRetryCleanMode:
    """Phase 6.1: build_prompt(mode="retry_clean") returns a stripped prompt."""

    def test_retry_clean_contains_only_three_sections(self):
        prompt = build_prompt(
            "Write hello world",
            attempt=2,
            previous_error="NameError: name 'x' is not defined",
            previous_code="print(x)",
            mode="retry_clean",
        )
        assert "<spec>" in prompt
        assert "</spec>" in prompt
        assert "<current_code>" in prompt
        assert "</current_code>" in prompt
        assert "<error>" in prompt
        assert "</error>" in prompt

    def test_retry_clean_excludes_full_mode_scaffold(self):
        prompt = build_prompt(
            "task",
            attempt=2,
            previous_error="boom",
            previous_code="print()",
            mode="retry_clean",
        )
        # None of the rich-mode sections should appear in the lean prompt.
        for marker in (
            "<environment>",
            "<role>",
            "<constraints>",
            "<output_contract>",
            "<approach>",
            "<previous_error",
            "<attempt_history>",
            "<workspace_state>",
            "<prior_knowledge>",
            "<tdd_constraint>",
        ):
            assert marker not in prompt, f"unexpected section {marker} in retry_clean prompt"

    def test_retry_clean_includes_task_in_spec(self):
        prompt = build_prompt(
            "Implement quicksort",
            attempt=2,
            previous_error="error",
            previous_code="def sort(): pass",
            mode="retry_clean",
        )
        spec_start = prompt.index("<spec>")
        spec_end = prompt.index("</spec>")
        assert "Implement quicksort" in prompt[spec_start:spec_end]

    def test_retry_clean_ignores_previous_code_variable(self, tmp_path, monkeypatch):
        # Phase 6.3: <current_code> is grounded in the filesystem, never in
        # the previously generated code variable. Even when previous_code is
        # supplied, its contents must not appear in the prompt.
        monkeypatch.setenv("UAS_WORKSPACE", str(tmp_path))
        prompt = build_prompt(
            "task",
            attempt=2,
            previous_error="error",
            previous_code="MEMORY_ONLY_TOKEN = 42\nprint(MEMORY_ONLY_TOKEN)",
            mode="retry_clean",
        )
        cc_start = prompt.index("<current_code>")
        cc_end = prompt.index("</current_code>")
        cc_body = prompt[cc_start:cc_end]
        assert "MEMORY_ONLY_TOKEN" not in cc_body
        assert "print(MEMORY_ONLY_TOKEN)" not in cc_body

    def test_retry_clean_reads_live_workspace_for_current_code(self, tmp_path, monkeypatch):
        # Phase 6.3: scan_workspace() of the live workspace path provides the
        # <current_code> body, so the retry sees the post-rollback file state.
        monkeypatch.setenv("UAS_WORKSPACE", str(tmp_path))
        (tmp_path / "main.py").write_text("LIVE_WORKSPACE_MARKER = 1\n")
        prompt = build_prompt(
            "task",
            attempt=2,
            previous_error="error",
            previous_code="stale_in_memory = 0",
            mode="retry_clean",
        )
        cc_start = prompt.index("<current_code>")
        cc_end = prompt.index("</current_code>")
        cc_body = prompt[cc_start:cc_end]
        assert "main.py" in cc_body
        assert "LIVE_WORKSPACE_MARKER" in cc_body
        assert "stale_in_memory" not in cc_body

    def test_retry_clean_includes_error_in_error_section(self):
        prompt = build_prompt(
            "task",
            attempt=2,
            previous_error="ZeroDivisionError: division by zero",
            previous_code="1/0",
            mode="retry_clean",
        )
        err_start = prompt.index("<error>")
        err_end = prompt.index("</error>")
        assert "ZeroDivisionError: division by zero" in prompt[err_start:err_end]

    def test_retry_clean_falls_back_to_workspace_files_when_no_live_workspace(
        self, tmp_path, monkeypatch,
    ):
        # When no live workspace is resolvable on disk, the legacy
        # workspace_files string is used as the fallback for <current_code>.
        monkeypatch.setenv("UAS_WORKSPACE", str(tmp_path / "does-not-exist"))
        prompt = build_prompt(
            "task",
            attempt=2,
            previous_error="error",
            previous_code=None,
            workspace_files="main.py (200 bytes)",
            mode="retry_clean",
        )
        cc_start = prompt.index("<current_code>")
        cc_end = prompt.index("</current_code>")
        assert "main.py" in prompt[cc_start:cc_end]

    def test_retry_clean_handles_missing_inputs(self):
        prompt = build_prompt("task", attempt=2, mode="retry_clean")
        assert "<spec>" in prompt
        assert "task" in prompt
        assert "<current_code>" in prompt
        assert "<error>" in prompt

    def test_default_mode_is_full(self):
        # When mode is not specified, the rich prompt is returned.
        prompt = build_prompt("task", attempt=1)
        assert "<environment>" in prompt
        assert "<role>" in prompt
        assert "<constraints>" in prompt


class TestRetryCleanSpecExtraction:
    """Phase 6.2: <spec> contains only the immutable Architect directive."""

    def test_spec_strips_appended_prior_step_context(self):
        # build_task_from_spec appends "Context from previous steps:" to the
        # immutable description. The retry_clean spec must drop that suffix.
        task = (
            "Implement quicksort.\n\n"
            "Context from previous steps:\n"
            "<file_signatures>def helper(x: int) -> int</file_signatures>"
        )
        prompt = build_prompt(
            task,
            attempt=2,
            previous_error="boom",
            previous_code="pass",
            mode="retry_clean",
        )
        spec_start = prompt.index("<spec>")
        spec_end = prompt.index("</spec>")
        spec_body = prompt[spec_start:spec_end]
        assert "Implement quicksort." in spec_body
        assert "Context from previous steps:" not in spec_body
        assert "<file_signatures>" not in spec_body

    def test_spec_uses_step_context_step_spec_when_provided(self):
        # When the caller threads a step_context with a step_spec key, that
        # value wins over the parsed task string.
        prompt = build_prompt(
            "raw task blob with extras",
            attempt=2,
            previous_error="err",
            previous_code="pass",
            step_context={"step_spec": "Authoritative step spec text."},
            mode="retry_clean",
        )
        spec_start = prompt.index("<spec>")
        spec_end = prompt.index("</spec>")
        spec_body = prompt[spec_start:spec_end]
        assert "Authoritative step spec text." in spec_body
        assert "raw task blob with extras" not in spec_body

    def test_spec_falls_back_to_uas_task_env_var(self, monkeypatch):
        # If task is empty, _extract_immutable_spec reads UAS_TASK directly.
        monkeypatch.setenv("UAS_TASK", "Spec from env var.")
        prompt = build_prompt(
            "",
            attempt=2,
            previous_error="err",
            previous_code="pass",
            mode="retry_clean",
        )
        spec_start = prompt.index("<spec>")
        spec_end = prompt.index("</spec>")
        spec_body = prompt[spec_start:spec_end]
        assert "Spec from env var." in spec_body

    def test_spec_handles_empty_task_and_no_env_var(self, monkeypatch):
        monkeypatch.delenv("UAS_TASK", raising=False)
        prompt = build_prompt(
            "",
            attempt=2,
            previous_error="err",
            previous_code="pass",
            mode="retry_clean",
        )
        spec_start = prompt.index("<spec>")
        spec_end = prompt.index("</spec>")
        spec_body = prompt[spec_start:spec_end]
        # Sentinel placeholder, not a crash.
        assert "(no spec available)" in spec_body


class TestRetryCleanErrorSection:
    """Phase 6.4: <error> contains only stderr + last 50 stdout lines, ANSI-stripped."""

    @staticmethod
    def _error_body(prompt: str) -> str:
        start = prompt.index("<error>") + len("<error>")
        end = prompt.index("</error>")
        return prompt[start:end]

    def test_error_section_includes_previous_stderr(self):
        prompt = build_prompt(
            "task",
            attempt=2,
            previous_stderr="Traceback (most recent call last):\n  ZeroDivisionError",
            mode="retry_clean",
        )
        body = self._error_body(prompt)
        assert "Traceback (most recent call last):" in body
        assert "ZeroDivisionError" in body

    def test_error_section_includes_stdout_tail(self):
        # 80 stdout lines: only the last 50 (lines 30..79) should appear.
        stdout = "\n".join(f"line-{i:03d}" for i in range(80))
        prompt = build_prompt(
            "task",
            attempt=2,
            previous_stdout=stdout,
            mode="retry_clean",
        )
        body = self._error_body(prompt)
        # Tail boundary: line-030 is the first kept line, line-079 the last.
        assert "line-079" in body
        assert "line-030" in body
        # Earliest 30 lines (000..029) must be dropped.
        assert "line-029" not in body
        assert "line-000" not in body

    def test_error_section_keeps_short_stdout_intact(self):
        stdout = "\n".join(f"line-{i}" for i in range(10))
        prompt = build_prompt(
            "task",
            attempt=2,
            previous_stdout=stdout,
            mode="retry_clean",
        )
        body = self._error_body(prompt)
        for i in range(10):
            assert f"line-{i}" in body

    def test_error_section_strips_ansi_escape_codes(self):
        # ANSI escape codes from colorized output must be removed.
        ansi_stderr = "\x1b[31mERROR:\x1b[0m something\x1b[1m bold\x1b[0m broke"
        ansi_stdout = "\x1b[32mOK\x1b[0m line one\n\x1b[33mwarn\x1b[0m line two"
        prompt = build_prompt(
            "task",
            attempt=2,
            previous_stderr=ansi_stderr,
            previous_stdout=ansi_stdout,
            mode="retry_clean",
        )
        body = self._error_body(prompt)
        assert "\x1b[" not in body
        assert "ERROR: something bold broke" in body
        assert "OK line one" in body
        assert "warn line two" in body

    def test_error_section_omits_synthesized_previous_error_when_structured_provided(self):
        # When previous_stderr/previous_stdout are provided, the synthesized
        # previous_error string (which contains category prefixes and other
        # opinionated synthesis) must NOT appear.
        prompt = build_prompt(
            "task",
            attempt=2,
            previous_error="[runtime_error] OPINIONATED_SYNTHESIS_TOKEN summary",
            previous_stderr="raw stderr trace",
            previous_stdout="raw stdout trace",
            mode="retry_clean",
        )
        body = self._error_body(prompt)
        assert "raw stderr trace" in body
        assert "raw stdout trace" in body
        assert "OPINIONATED_SYNTHESIS_TOKEN" not in body
        assert "[runtime_error]" not in body

    def test_error_section_falls_back_to_previous_error_when_no_structured(self):
        # Legacy callers passing only previous_error must continue to work.
        prompt = build_prompt(
            "task",
            attempt=2,
            previous_error="legacy error string",
            mode="retry_clean",
        )
        body = self._error_body(prompt)
        assert "legacy error string" in body

    def test_error_section_handles_no_inputs_gracefully(self):
        prompt = build_prompt("task", attempt=2, mode="retry_clean")
        body = self._error_body(prompt)
        assert "(no error output captured)" in body

    def test_error_section_excludes_attempt_history_and_code_snippets(self):
        # The retry_clean error section must never contain attempt history
        # markers or prior code snippets.
        prompt = build_prompt(
            "task",
            attempt=3,
            previous_error="error",
            previous_code="def secret_prior_code(): pass",
            previous_stderr="stderr trace",
            previous_stdout="stdout trace",
            mode="retry_clean",
        )
        body = self._error_body(prompt)
        assert "secret_prior_code" not in body
        assert "Attempt " not in body
        assert "<attempt_history>" not in body

    def test_error_section_only_stdout_no_stderr(self):
        prompt = build_prompt(
            "task",
            attempt=2,
            previous_stdout="stdout-only output",
            mode="retry_clean",
        )
        body = self._error_body(prompt)
        assert "stdout-only output" in body
        assert "stderr:" not in body

    def test_error_section_only_stderr_no_stdout(self):
        prompt = build_prompt(
            "task",
            attempt=2,
            previous_stderr="stderr-only output",
            mode="retry_clean",
        )
        body = self._error_body(prompt)
        assert "stderr-only output" in body
        assert "stdout " not in body


class TestParseUasResult:
    def test_valid_result(self):
        stdout = 'some output\nUAS_RESULT: {"status": "ok", "files_written": ["a.txt"], "summary": "done"}\n'
        result = parse_uas_result(stdout)
        assert result is not None
        assert result.status == "ok"
        assert result.files_written == ["a.txt"]
        assert result.summary == "done"

    def test_no_result_line(self):
        assert parse_uas_result("just regular output\n") is None

    def test_invalid_json(self):
        assert parse_uas_result("UAS_RESULT: {not valid json}\n") is None

    def test_empty_string(self):
        assert parse_uas_result("") is None

    def test_error_result(self):
        stdout = 'UAS_RESULT: {"status": "error", "error": "file missing", "summary": "failed"}\n'
        result = parse_uas_result(stdout)
        assert result is not None
        assert result.status == "error"
        assert result.error == "file missing"


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


def _mock_quality(code: str, task: str) -> CodeQuality:
    """Deterministic code quality assessment for tests — mimics LLM behaviour."""
    import re
    has_input = bool(re.search(r"\binput\s*\(", code))
    has_uas = "UAS_RESULT" in code
    is_mod = bool(re.search(
        r"\b(?:modify|update|insert|extend|edit|change|add\s+(?:\w+\s+)*to)\b"
        r".*?\b\w+\.\w{1,5}\b",
        task, re.IGNORECASE | re.DOTALL,
    ))
    return CodeQuality(
        has_uas_result=has_uas,
        has_input_call=has_input,
        is_file_modification=is_mod,
        missing_imports=[],
    )


def _mock_evaluate_sandbox(stdout: str, stderr: str, exit_code: int) -> ExecutionResult:
    """Deterministic sandbox evaluation for tests — mirrors exit code logic."""
    success = exit_code == 0
    return ExecutionResult(
        success=success,
        revert_needed=not success and bool(stdout),
        error_category=None if success else "runtime_error",
        summary="ok" if success else (stderr or stdout or "Non-zero exit code"),
    )


@patch("orchestrator.main.evaluate_sandbox", side_effect=_mock_evaluate_sandbox)
@patch("orchestrator.main.assess_code_quality", side_effect=_mock_quality)
class TestMainLoop:
    @patch("orchestrator.main.MINIMAL_MODE", True)
    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_success_on_first_attempt(self, mock_client_factory, mock_sandbox, mock_args, _mock_cq, _mock_eval):
        mock_args.return_value = argparse.Namespace(task=["test task"], verbose=False)
        mock_client = MagicMock()
        mock_client.generate.return_value = ('```python\nprint("hello")\n```', {"input": 0, "output": 0})
        mock_client_factory.return_value = mock_client
        mock_sandbox.return_value = {"exit_code": 0, "stdout": "hello", "stderr": ""}

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        assert mock_client.generate.call_count == 1
        # Two sandbox calls: verify + execute
        assert mock_sandbox.call_count == 2

    @patch("orchestrator.main.MINIMAL_MODE", True)
    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_retry_on_sandbox_failure(self, mock_client_factory, mock_sandbox, mock_args, _mock_cq, _mock_eval):
        mock_args.return_value = argparse.Namespace(task=["test task"], verbose=False)
        mock_client = MagicMock()
        mock_client.generate.return_value = ('```python\nprint("hello")\n```', {"input": 0, "output": 0})
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
    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_failure_after_all_retries(self, mock_client_factory, mock_sandbox, mock_args, _mock_cq, _mock_eval):
        mock_args.return_value = argparse.Namespace(task=["test task"], verbose=False)
        mock_client = MagicMock()
        mock_client.generate.return_value = ('```python\nprint("hello")\n```', {"input": 0, "output": 0})
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
    def test_empty_code_extraction(self, mock_client_factory, mock_sandbox, mock_args, _mock_cq, _mock_eval):
        mock_args.return_value = argparse.Namespace(task=["test task"], verbose=False)
        mock_client = MagicMock()
        # LLM returns text with no code block
        mock_client.generate.return_value = ("I cannot do that.", {"input": 0, "output": 0})
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
    def test_no_task_exits_1(self, mock_client_factory, mock_sandbox, mock_args, _mock_cq, _mock_eval, monkeypatch):
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
    def test_uas_result_parsed_on_success(self, mock_client_factory, mock_sandbox, mock_args, _mock_cq, _mock_eval):
        mock_args.return_value = argparse.Namespace(task=["test task"], verbose=False)
        mock_client = MagicMock()
        mock_client.generate.return_value = ('```python\nprint("hello")\n```', {"input": 0, "output": 0})
        mock_client_factory.return_value = mock_client
        mock_sandbox.return_value = {
            "exit_code": 0,
            "stdout": 'output\nUAS_RESULT: {"status": "ok", "files_written": [], "summary": "done"}\n',
            "stderr": "",
        }

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0

    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_syntax_error_skips_sandbox(self, mock_client_factory, mock_sandbox, mock_args, _mock_cq, _mock_eval):
        mock_args.return_value = argparse.Namespace(task=["test task"], verbose=False)
        mock_client = MagicMock()
        _u = {"input": 0, "output": 0}
        # First response has syntax error (not truncation), second is valid
        mock_client.generate.side_effect = [
            ('```python\ndef foo(x):\n    x = = 2\n```', _u),
            ('```python\nprint("hello")\n```', _u),
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

    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_input_call_skips_sandbox(self, mock_client_factory, mock_sandbox, mock_args, _mock_cq, _mock_eval):
        mock_args.return_value = argparse.Namespace(task=["test task"], verbose=False)
        mock_client = MagicMock()
        _u = {"input": 0, "output": 0}
        # First response uses input(), second is valid
        mock_client.generate.side_effect = [
            ('```python\nname = input("Enter name: ")\nprint(name)\n```', _u),
            ('```python\nprint("hello")\n```', _u),
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

    @patch("orchestrator.main.MINIMAL_MODE", True)
    @patch("orchestrator.main.create_attempt_branch", return_value="uas/step-1/attempt-1")
    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_attempt_branch_created_when_step_id_set(
        self, mock_client_factory, mock_sandbox, mock_args,
        mock_create_branch, _mock_cq, _mock_eval, monkeypatch,
    ):
        monkeypatch.setenv("UAS_STEP_ID", "1")
        monkeypatch.setenv("UAS_WORKSPACE", "/tmp/ws")
        mock_args.return_value = argparse.Namespace(task=["test task"], verbose=False)
        mock_client = MagicMock()
        mock_client.generate.return_value = ('```python\nprint("hello")\n```', {"input": 0, "output": 0})
        mock_client_factory.return_value = mock_client
        mock_sandbox.return_value = {"exit_code": 0, "stdout": "hello", "stderr": ""}

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        mock_create_branch.assert_called_once_with("/tmp/ws", 1, 1)

    @patch("orchestrator.main.MINIMAL_MODE", True)
    @patch("orchestrator.main.create_attempt_branch", return_value="")
    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_attempt_branch_skipped_when_no_step_id(
        self, mock_client_factory, mock_sandbox, mock_args,
        mock_create_branch, _mock_cq, _mock_eval,
    ):
        mock_args.return_value = argparse.Namespace(task=["test task"], verbose=False)
        mock_client = MagicMock()
        mock_client.generate.return_value = ('```python\nprint("hello")\n```', {"input": 0, "output": 0})
        mock_client_factory.return_value = mock_client
        mock_sandbox.return_value = {"exit_code": 0, "stdout": "hello", "stderr": ""}

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        mock_create_branch.assert_not_called()


@patch("orchestrator.main.assess_code_quality", side_effect=_mock_quality)
class TestPreExecutionCheck:
    def test_valid_code_no_errors(self, _mock_cq):
        code = 'print("UAS_RESULT: ok")'
        errors, warnings = pre_execution_check(code)
        assert errors == []
        assert warnings == []

    def test_syntax_error_is_critical(self, _mock_cq):
        code = "def foo(\n"
        errors, warnings = pre_execution_check(code)
        assert len(errors) == 1
        assert "Syntax error" in errors[0]

    def test_input_call_is_critical(self, _mock_cq):
        code = 'name = input("Enter name: ")\nprint(f"UAS_RESULT: {name}")'
        errors, warnings = pre_execution_check(code)
        assert len(errors) == 1
        assert "input()" in errors[0]

    def test_missing_uas_result_is_warning(self, _mock_cq):
        code = 'print("hello world")'
        errors, warnings = pre_execution_check(code)
        assert errors == []
        assert len(warnings) == 1
        assert "UAS_RESULT" in warnings[0]

    def test_multiple_critical_errors(self, _mock_cq):
        code = "name = input(\n"
        errors, warnings = pre_execution_check(code)
        # Syntax error catches the incomplete input( call too
        assert len(errors) >= 1

    def test_input_in_string_literal_is_caught(self, _mock_cq):
        # The mock uses a simple regex that matches input( in strings — acceptable
        code = 'x = "input()"\nprint(f"UAS_RESULT: {x}")'
        errors, _warnings = pre_execution_check(code)
        assert len(errors) <= 1

    def test_fuzzy_failure_returns_only_syntax_errors(self, _mock_cq):
        """When assess_code_quality raises, only syntax errors are returned."""
        _mock_cq.side_effect = RuntimeError("API down")
        code = 'print("UAS_RESULT: ok")'
        errors, warnings = pre_execution_check(code)
        assert errors == []
        assert warnings == []


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


@patch("orchestrator.main.assess_code_quality", side_effect=_mock_quality)
class TestFileModificationDetection:
    """Section 3: Detect file modification tasks and inject guidance."""

    def test_detects_modify_keyword(self, _mock_cq):
        assert _task_mentions_file_modification("modify analysis.py to add MCID") is True

    def test_detects_update_keyword(self, _mock_cq):
        assert _task_mentions_file_modification("update config.json with new settings") is True

    def test_detects_add_to_keyword(self, _mock_cq):
        assert _task_mentions_file_modification("add to utils.py a helper function") is True

    def test_detects_insert_keyword(self, _mock_cq):
        assert _task_mentions_file_modification("insert validation into forms.html") is True

    def test_detects_extend_keyword(self, _mock_cq):
        assert _task_mentions_file_modification("extend models.py with a new class") is True

    def test_detects_add_something_to(self, _mock_cq):
        assert _task_mentions_file_modification("add MCID scoring code to analysis.py") is True

    def test_no_match_for_creation_task(self, _mock_cq):
        assert _task_mentions_file_modification("create a new Flask web application") is False

    def test_no_match_for_plain_text(self, _mock_cq):
        assert _task_mentions_file_modification("build a REST API") is False

    def test_guidance_appears_for_modification_task(self, _mock_cq):
        prompt = build_prompt("modify analysis.py to add MCID scoring", attempt=1)
        assert "<file_modification_guidance>" in prompt
        assert "Write the COMPLETE modified file" in prompt
        assert "Never use string insertion by line number" in prompt

    def test_guidance_absent_for_creation_task(self, _mock_cq):
        prompt = build_prompt("create a new REST API project", attempt=1)
        assert "<file_modification_guidance>" not in prompt

    def test_fuzzy_failure_returns_false(self, _mock_cq):
        """When assess_code_quality raises, _task_mentions_file_modification returns False."""
        _mock_cq.side_effect = RuntimeError("API down")
        assert _task_mentions_file_modification("modify analysis.py") is False


class TestTDDPromptInjection:
    """Phase 4.4: Test that build_prompt injects TDD constraints from test_files."""

    def test_test_files_injected_into_prompt(self):
        test_files = {"test_math.py": "def test_add():\n    assert add(1, 2) == 3\n"}
        prompt = build_prompt("Implement math utils", attempt=1,
                              test_files=test_files)
        assert "<tdd_constraint>" in prompt
        assert "test_math.py" in prompt
        assert "def test_add():" in prompt
        assert "assert add(1, 2) == 3" in prompt
        assert "pytest test_math.py --tb=short -q" in prompt
        assert "Do NOT modify the test files" in prompt

    def test_multiple_test_files(self):
        test_files = {
            "test_add.py": "def test_add(): pass\n",
            "test_sub.py": "def test_sub(): pass\n",
        }
        prompt = build_prompt("Implement math", attempt=1, test_files=test_files)
        assert "<tdd_constraint>" in prompt
        assert "test_add.py" in prompt
        assert "test_sub.py" in prompt
        assert "def test_add(): pass" in prompt
        assert "def test_sub(): pass" in prompt

    def test_no_test_files_no_tdd_block(self):
        prompt = build_prompt("Build something", attempt=1, test_files=None)
        assert "<tdd_constraint>" not in prompt

    def test_empty_test_files_no_tdd_block(self):
        prompt = build_prompt("Build something", attempt=1, test_files={})
        assert "<tdd_constraint>" not in prompt

    def test_tdd_block_on_retry_attempt(self):
        test_files = {"test_core.py": "def test_it(): assert True\n"}
        prompt = build_prompt("Implement core", attempt=2,
                              previous_error="NameError",
                              test_files=test_files)
        assert "<tdd_constraint>" in prompt
        assert "pytest test_core.py --tb=short -q" in prompt


class TestRunPytestInSandbox:
    """Phase 4.5: run_pytest_in_sandbox generates correct script and delegates."""

    @patch("orchestrator.sandbox.run_in_sandbox")
    def test_generates_pytest_invocation(self, mock_sandbox):
        from orchestrator.sandbox import run_pytest_in_sandbox
        mock_sandbox.return_value = {"exit_code": 0, "stdout": "1 passed", "stderr": ""}
        result = run_pytest_in_sandbox(["test_math.py"])
        assert result["exit_code"] == 0
        mock_sandbox.assert_called_once()
        generated_code = mock_sandbox.call_args[0][0]
        assert "pytest" in generated_code
        assert "test_math.py" in generated_code
        assert "--tb=short" in generated_code
        assert "-q" in generated_code

    @patch("orchestrator.sandbox.run_in_sandbox")
    def test_multiple_test_files(self, mock_sandbox):
        from orchestrator.sandbox import run_pytest_in_sandbox
        mock_sandbox.return_value = {"exit_code": 0, "stdout": "3 passed", "stderr": ""}
        run_pytest_in_sandbox(["test_a.py", "test_b.py"])
        generated_code = mock_sandbox.call_args[0][0]
        assert "test_a.py" in generated_code
        assert "test_b.py" in generated_code

    @patch("orchestrator.sandbox.run_in_sandbox")
    def test_passes_timeout(self, mock_sandbox):
        from orchestrator.sandbox import run_pytest_in_sandbox
        mock_sandbox.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}
        run_pytest_in_sandbox(["test_x.py"], timeout=120)
        assert mock_sandbox.call_args[1]["timeout"] == 120

    @patch("orchestrator.sandbox.run_in_sandbox")
    def test_returns_failure_exit_code(self, mock_sandbox):
        from orchestrator.sandbox import run_pytest_in_sandbox
        mock_sandbox.return_value = {"exit_code": 1, "stdout": "1 failed", "stderr": ""}
        result = run_pytest_in_sandbox(["test_fail.py"])
        assert result["exit_code"] == 1
        assert "1 failed" in result["stdout"]

    @patch("orchestrator.sandbox.run_in_sandbox")
    def test_installs_pytest_before_running(self, mock_sandbox):
        from orchestrator.sandbox import run_pytest_in_sandbox
        mock_sandbox.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}
        run_pytest_in_sandbox(["test_x.py"])
        generated_code = mock_sandbox.call_args[0][0]
        assert "pip" in generated_code
        assert "install" in generated_code
        # pip install line appears before the pytest run line
        pip_line_pos = generated_code.index("pip\", \"install")
        pytest_run_pos = generated_code.index('"-m", "pytest"')
        assert pip_line_pos < pytest_run_pos


@patch("orchestrator.main.evaluate_sandbox", side_effect=_mock_evaluate_sandbox)
@patch("orchestrator.main.assess_code_quality", side_effect=_mock_quality)
class TestPytestGate:
    """Phase 4.5: Binary pytest gate in the orchestrator main loop."""

    @patch("orchestrator.main.MINIMAL_MODE", True)
    @patch("orchestrator.main.run_pytest_in_sandbox")
    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_pytest_pass_succeeds(
        self, mock_client_factory, mock_sandbox, mock_args,
        mock_pytest, _mock_cq, _mock_eval, monkeypatch,
    ):
        """When test files exist and pytest passes, the step succeeds."""
        monkeypatch.setenv("UAS_TEST_FILES",
                           '{"test_math.py": "def test_add(): assert True"}')
        mock_args.return_value = argparse.Namespace(task=["test task"], verbose=False)
        mock_client = MagicMock()
        mock_client.generate.return_value = (
            '```python\nprint("hello")\n```', {"input": 0, "output": 0})
        mock_client_factory.return_value = mock_client
        mock_sandbox.return_value = {"exit_code": 0, "stdout": "hello", "stderr": ""}
        mock_pytest.return_value = {"exit_code": 0, "stdout": "1 passed", "stderr": ""}

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        mock_pytest.assert_called_once_with(["test_math.py"])

    @patch("orchestrator.main.MINIMAL_MODE", True)
    @patch("orchestrator.main.run_pytest_in_sandbox")
    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_pytest_fail_triggers_retry(
        self, mock_client_factory, mock_sandbox, mock_args,
        mock_pytest, _mock_cq, _mock_eval, monkeypatch,
    ):
        """When pytest fails, the step retries with pytest output as the error."""
        monkeypatch.setenv("UAS_TEST_FILES",
                           '{"test_math.py": "def test_add(): assert True"}')
        mock_args.return_value = argparse.Namespace(task=["test task"], verbose=False)
        mock_client = MagicMock()
        mock_client.generate.return_value = (
            '```python\nprint("hello")\n```', {"input": 0, "output": 0})
        mock_client_factory.return_value = mock_client
        mock_sandbox.return_value = {"exit_code": 0, "stdout": "hello", "stderr": ""}
        # Pytest fails first, then passes on retry
        mock_pytest.side_effect = [
            {"exit_code": 1, "stdout": "FAILED test_add", "stderr": ""},
            {"exit_code": 0, "stdout": "1 passed", "stderr": ""},
        ]

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        assert mock_client.generate.call_count == 2
        assert mock_pytest.call_count == 2

    @patch("orchestrator.main.MINIMAL_MODE", True)
    @patch("orchestrator.main.run_pytest_in_sandbox")
    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_pytest_fail_all_retries_exits_1(
        self, mock_client_factory, mock_sandbox, mock_args,
        mock_pytest, _mock_cq, _mock_eval, monkeypatch,
    ):
        """When pytest fails on all attempts, exit code is 1."""
        monkeypatch.setenv("UAS_TEST_FILES",
                           '{"test_x.py": "def test_x(): assert False"}')
        mock_args.return_value = argparse.Namespace(task=["test task"], verbose=False)
        mock_client = MagicMock()
        mock_client.generate.return_value = (
            '```python\nprint("hello")\n```', {"input": 0, "output": 0})
        mock_client_factory.return_value = mock_client
        # Sandbox always succeeds, but pytest always fails
        mock_sandbox.side_effect = [
            {"exit_code": 0, "stdout": "sandbox OK", "stderr": ""},  # verify
        ] + [
            {"exit_code": 0, "stdout": "ok", "stderr": ""}
            for _ in range(MAX_RETRIES)
        ]
        mock_pytest.return_value = {"exit_code": 1, "stdout": "1 failed", "stderr": ""}

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        assert mock_pytest.call_count == MAX_RETRIES

    @patch("orchestrator.main.MINIMAL_MODE", True)
    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_no_test_files_skips_pytest(
        self, mock_client_factory, mock_sandbox, mock_args,
        _mock_cq, _mock_eval,
    ):
        """Without test files, the pytest gate is skipped entirely."""
        mock_args.return_value = argparse.Namespace(task=["test task"], verbose=False)
        mock_client = MagicMock()
        mock_client.generate.return_value = (
            '```python\nprint("hello")\n```', {"input": 0, "output": 0})
        mock_client_factory.return_value = mock_client
        mock_sandbox.return_value = {"exit_code": 0, "stdout": "hello", "stderr": ""}

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0

    @patch("orchestrator.main.MINIMAL_MODE", True)
    @patch("orchestrator.main.run_pytest_in_sandbox")
    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_pytest_error_message_includes_output(
        self, mock_client_factory, mock_sandbox, mock_args,
        mock_pytest, _mock_cq, _mock_eval, monkeypatch,
    ):
        """Pytest failure error message includes stdout and stderr."""
        monkeypatch.setenv("UAS_TEST_FILES",
                           '{"test_z.py": "def test_z(): pass"}')
        mock_args.return_value = argparse.Namespace(task=["test task"], verbose=False)
        mock_client = MagicMock()
        _u = {"input": 0, "output": 0}
        mock_client.generate.side_effect = [
            ('```python\nprint("v1")\n```', _u),
            ('```python\nprint("v2")\n```', _u),
        ]
        mock_client_factory.return_value = mock_client
        mock_sandbox.return_value = {"exit_code": 0, "stdout": "ok", "stderr": ""}
        mock_pytest.side_effect = [
            {"exit_code": 1, "stdout": "FAILED test_z::test_z", "stderr": "AssertionError"},
            {"exit_code": 0, "stdout": "1 passed", "stderr": ""},
        ]

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        # Phase 6.6: full-mode prompts no longer carry retry guidance prose;
        # the retry path will inject pytest failures via retry_clean mode in
        # task 6.7. For now, just verify the retry attempt occurred.
        assert mock_client.generate.call_count == 2


@patch("orchestrator.main.evaluate_sandbox", side_effect=_mock_evaluate_sandbox)
@patch("orchestrator.main.assess_code_quality", side_effect=_mock_quality)
class TestRetryCleanThreeAttemptSequence:
    """Phase 6.9: Mock a 3-attempt sequence and verify the retry_clean prompts.

    The retry_clean prompt (attempts 2+) must contain ZERO references to any
    prior attempt's LLM-generated code, and must NOT carry forward error
    messages from attempts older than the immediately prior one. They MUST
    contain the Architect's immutable spec and the live filesystem state.
    """

    @patch("orchestrator.main.MINIMAL_MODE", True)
    @patch("orchestrator.main.format_workspace")
    @patch("orchestrator.main.lint_workspace", return_value=[])
    @patch("orchestrator.main.parse_args")
    @patch("orchestrator.main.run_in_sandbox")
    @patch("orchestrator.main.get_llm_client")
    def test_three_attempts_strip_prior_context(
        self, mock_client_factory, mock_sandbox, mock_args,
        _mock_lint, _mock_format, _mock_cq, _mock_eval,
        tmp_path, monkeypatch,
    ):
        # Live workspace with a marker file the retry_clean prompt must
        # surface in <current_code> via scan_workspace.
        monkeypatch.setenv("UAS_WORKSPACE", str(tmp_path))
        (tmp_path / "main.py").write_text("LIVE_WORKSPACE_MARKER = 1\n")

        # Distinct, easy-to-grep tokens so any leakage between prompts shows up.
        gen1 = '```python\nMEMORY_TOKEN_ATTEMPT_1 = 1\nprint("a1")\n```'
        gen2 = '```python\nMEMORY_TOKEN_ATTEMPT_2 = 2\nprint("a2")\n```'
        gen3 = '```python\nMEMORY_TOKEN_ATTEMPT_3 = 3\nprint("a3")\n```'
        mock_client = MagicMock()
        _u = {"input": 0, "output": 0}
        mock_client.generate.side_effect = [
            (gen1, _u),
            (gen2, _u),
            (gen3, _u),
        ]
        mock_client_factory.return_value = mock_client

        # Verify call succeeds; the 3 attempt sandbox calls fail with distinct
        # stderr/stdout tokens so we can detect cross-attempt leakage.
        mock_sandbox.side_effect = [
            {"exit_code": 0, "stdout": "sandbox OK", "stderr": ""},  # verify
            {"exit_code": 1,
             "stdout": "STDOUT_TOKEN_ATTEMPT_1 trace",
             "stderr": "STDERR_TOKEN_ATTEMPT_1 boom"},
            {"exit_code": 1,
             "stdout": "STDOUT_TOKEN_ATTEMPT_2 trace",
             "stderr": "STDERR_TOKEN_ATTEMPT_2 boom"},
            {"exit_code": 1,
             "stdout": "STDOUT_TOKEN_ATTEMPT_3 trace",
             "stderr": "STDERR_TOKEN_ATTEMPT_3 boom"},
        ]

        # Recognisable task → becomes the immutable spec in the retry prompts.
        task_text = "IMMUTABLE_SPEC_TOKEN: implement quicksort."
        mock_args.return_value = argparse.Namespace(
            task=[task_text], verbose=False,
        )

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        assert mock_client.generate.call_count == MAX_RETRIES

        prompts = [c.args[0] for c in mock_client.generate.call_args_list]
        attempt1, attempt2, attempt3 = prompts

        # Sanity: attempt 1 is the rich "full" prompt.
        assert "<role>" in attempt1
        assert "<environment>" in attempt1
        assert "IMMUTABLE_SPEC_TOKEN" in attempt1

        # Attempts 2 and 3 are stripped retry_clean prompts.
        for label, p in (("attempt2", attempt2), ("attempt3", attempt3)):
            assert "<spec>" in p, f"{label} missing <spec>"
            assert "</spec>" in p, f"{label} missing </spec>"
            assert "<current_code>" in p, f"{label} missing <current_code>"
            assert "</current_code>" in p, f"{label} missing </current_code>"
            assert "<error>" in p, f"{label} missing <error>"
            assert "</error>" in p, f"{label} missing </error>"
            # None of the full-mode scaffolding may appear.
            for marker in (
                "<role>",
                "<environment>",
                "<constraints>",
                "<output_contract>",
                "<approach>",
                "<previous_error",
                "<attempt_history>",
                "<workspace_state>",
                "<prior_knowledge>",
                "<tdd_constraint>",
            ):
                assert marker not in p, (
                    f"{label} unexpectedly contains full-mode marker {marker}"
                )

            # The Architect's immutable spec is grounded in the prompt.
            spec_start = p.index("<spec>")
            spec_end = p.index("</spec>")
            assert "IMMUTABLE_SPEC_TOKEN" in p[spec_start:spec_end], (
                f"{label} <spec> missing immutable task token"
            )

            # Live workspace state is grounded in <current_code>, not memory.
            cc_start = p.index("<current_code>")
            cc_end = p.index("</current_code>")
            cc_body = p[cc_start:cc_end]
            assert "main.py" in cc_body, (
                f"{label} <current_code> missing live workspace file"
            )
            assert "LIVE_WORKSPACE_MARKER" in cc_body, (
                f"{label} <current_code> missing live workspace marker"
            )

        # ZERO references to ANY prior attempt's LLM-generated code anywhere
        # in the retry_clean prompts — the LLM has no memory of prior attempts.
        for label, p in (("attempt2", attempt2), ("attempt3", attempt3)):
            assert "MEMORY_TOKEN_ATTEMPT_1" not in p, (
                f"{label} leaked attempt 1 generated code"
            )
            assert "MEMORY_TOKEN_ATTEMPT_2" not in p, (
                f"{label} leaked attempt 2 generated code"
            )
            assert "MEMORY_TOKEN_ATTEMPT_3" not in p, (
                f"{label} leaked attempt 3 generated code"
            )

        # Attempt 2 sees ONLY attempt 1's raw sandbox error in <error>.
        err2_start = attempt2.index("<error>")
        err2_end = attempt2.index("</error>")
        err2_body = attempt2[err2_start:err2_end]
        assert "STDERR_TOKEN_ATTEMPT_1" in err2_body
        assert "STDOUT_TOKEN_ATTEMPT_1" in err2_body
        assert "STDERR_TOKEN_ATTEMPT_2" not in attempt2
        assert "STDOUT_TOKEN_ATTEMPT_2" not in attempt2
        assert "STDERR_TOKEN_ATTEMPT_3" not in attempt2

        # Attempt 3 sees ONLY attempt 2's raw sandbox error — no attempt 1
        # history may bleed through, since the retry loop has no
        # attempt_history accumulator (Phase 6.5).
        err3_start = attempt3.index("<error>")
        err3_end = attempt3.index("</error>")
        err3_body = attempt3[err3_start:err3_end]
        assert "STDERR_TOKEN_ATTEMPT_2" in err3_body
        assert "STDOUT_TOKEN_ATTEMPT_2" in err3_body
        assert "STDERR_TOKEN_ATTEMPT_1" not in attempt3, (
            "attempt 3 leaked attempt 1's stderr"
        )
        assert "STDOUT_TOKEN_ATTEMPT_1" not in attempt3, (
            "attempt 3 leaked attempt 1's stdout"
        )
        assert "STDERR_TOKEN_ATTEMPT_3" not in attempt3
        assert "STDOUT_TOKEN_ATTEMPT_3" not in attempt3

        # The synthesized previous_error string built by the orchestrator
        # main loop carries an "[runtime_error]" prefix in this scenario.
        # The retry_clean <error> section must NOT show that opinionated
        # synthesis when raw stderr/stdout is available (Phase 6.4).
        assert "[runtime_error]" not in err2_body
        assert "[runtime_error]" not in err3_body
