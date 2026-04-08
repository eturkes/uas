"""Tests for architect.executor: run_orchestrator, extract_sandbox_stdout, find_engine."""

import os
import subprocess
import sys
from unittest.mock import patch, MagicMock

import pytest

import architect.executor as _executor_module
from architect.executor import (
    run_orchestrator,
    extract_sandbox_stdout,
    extract_sandbox_stderr,
    extract_workspace_files,
    parse_uas_result,
    truncate_output,
    find_engine,
    build_planner_workspace_context,
    _run_local,
    RUN_TIMEOUT,
    _STDOUT_PATTERN,
    _STDERR_PATTERN,
)
from architect.main import _sanitize_files_written
from uas.fuzzy_models import SandboxOutput


def _mock_fuzzy_extract(raw: str) -> SandboxOutput:
    """Test helper: mimic fuzzy extraction using the old regex patterns."""
    stdout_m = list(_STDOUT_PATTERN.finditer(raw))
    stderr_m = list(_STDERR_PATTERN.finditer(raw))
    return SandboxOutput(
        stdout=stdout_m[-1].group(1).strip() if stdout_m else "",
        stderr=stderr_m[-1].group(1).strip() if stderr_m else "",
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


@patch("architect.executor._fuzzy_extract", side_effect=_mock_fuzzy_extract)
class TestExtractSandboxStdout:
    def test_basic_stdout(self, _mock):
        log = "stdout:\nhello world\nExit code: 0"
        assert extract_sandbox_stdout(log) == "hello world"

    def test_inline_stdout(self, _mock):
        log = "stdout: hello\nExit code: 0"
        assert extract_sandbox_stdout(log) == "hello"

    def test_multiline_stdout(self, _mock):
        log = "stdout:\nline1\nline2\nline3\nExit code: 0"
        assert extract_sandbox_stdout(log) == "line1\nline2\nline3"

    def test_stdout_terminated_by_stderr(self, _mock):
        log = "stdout:\nresult\nstderr:\nwarning"
        assert extract_sandbox_stdout(log) == "result"

    def test_stdout_terminated_by_success(self, _mock):
        log = "stdout:\nresult\nSUCCESS on attempt 1."
        assert extract_sandbox_stdout(log) == "result"

    def test_stdout_terminated_by_failed(self, _mock):
        log = "stdout:\nresult\nFAILED on attempt 1."
        assert extract_sandbox_stdout(log) == "result"

    def test_stdout_terminated_by_attempt(self, _mock):
        log = "stdout:\nresult\n--- Attempt 2/3 ---"
        assert extract_sandbox_stdout(log) == "result"

    def test_no_stdout_returns_empty(self, _mock):
        log = "stderr:\nsome error\nExit code: 1"
        assert extract_sandbox_stdout(log) == ""

    def test_empty_string(self, _mock):
        assert extract_sandbox_stdout("") == ""

    def test_realistic_orchestrator_output(self, _mock):
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


@patch("architect.executor._fuzzy_extract", side_effect=_mock_fuzzy_extract)
class TestExtractSandboxStderr:
    def test_basic_stderr(self, _mock):
        log = "stderr:\nsome warning\nExit code: 0"
        assert extract_sandbox_stderr(log) == "some warning"

    def test_inline_stderr(self, _mock):
        log = "stderr: warning msg\nExit code: 0"
        assert extract_sandbox_stderr(log) == "warning msg"

    def test_multiline_stderr(self, _mock):
        log = "stderr:\nwarn1\nwarn2\nExit code: 0"
        assert extract_sandbox_stderr(log) == "warn1\nwarn2"

    def test_stderr_terminated_by_stdout(self, _mock):
        log = "stderr:\nwarn\nstdout:\nresult"
        assert extract_sandbox_stderr(log) == "warn"

    def test_no_stderr_returns_empty(self, _mock):
        log = "stdout:\nresult\nExit code: 0"
        assert extract_sandbox_stderr(log) == ""

    def test_empty_string(self, _mock):
        assert extract_sandbox_stderr("") == ""

    def test_realistic_output_with_both(self, _mock):
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

    def test_last_stderr_block_on_retry(self, _mock):
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

    def test_no_limit_by_default(self):
        text = "x" * 50000
        assert truncate_output(text) == text

    def test_explicit_limit_at_boundary(self):
        text = "x" * 100
        assert truncate_output(text, max_length=100) == text

    def test_explicit_limit_above_boundary(self):
        text = "x" * 200
        result = truncate_output(text, max_length=100)
        assert len(result) < len(text)
        assert result.startswith("x" * 100)
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


class TestSanitizeExtractedFiles:
    def test_annotated_paths_stripped(self):
        log = (
            "stdout:\n"
            "Saved /workspace/data/file.csv (symlink)\n"
            "Created /workspace/output/ (directory)\n"
            "Wrote /workspace/model.pkl (overwritten)\n"
        )
        raw = extract_workspace_files(log)
        sanitized = _sanitize_files_written(raw)
        assert "/workspace/data/file.csv" in sanitized
        assert "/workspace/output/" in sanitized
        assert "/workspace/model.pkl" in sanitized
        for path in sanitized:
            assert "(" not in path
            assert ")" not in path

    def test_unannotated_paths_unchanged(self):
        log = "Written to /workspace/clean.txt\n"
        raw = extract_workspace_files(log)
        sanitized = _sanitize_files_written(raw)
        assert sanitized == ["/workspace/clean.txt"]


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


@patch("architect.executor._fuzzy_extract", side_effect=_mock_fuzzy_extract)
class TestStdoutNoTruncationByDefault:
    def test_long_stdout_not_truncated(self, _mock):
        content = "x" * 10000
        log = f"stdout:\n{content}\nExit code: 0"
        result = extract_sandbox_stdout(log)
        assert result == content

    def test_long_stderr_not_truncated(self, _mock):
        content = "y" * 10000
        log = f"stderr:\n{content}\nExit code: 0"
        result = extract_sandbox_stderr(log)
        assert result == content


class TestDynamicClaudeMd:
    def test_get_claude_md_without_context(self):
        from orchestrator.claude_config import get_claude_md_content
        content = get_claude_md_content()
        assert "Workspace Instructions" in content
        assert "Current Task Context" not in content

    def test_get_claude_md_with_step_context(self):
        from orchestrator.claude_config import get_claude_md_content
        ctx = {
            "step_number": 3,
            "total_steps": 5,
            "step_title": "Process data",
            "dependencies": [1, 2],
            "prior_steps": [
                {"id": 1, "title": "Download", "summary": "Downloaded CSV", "files": ["data.csv"]},
                {"id": 2, "title": "Clean", "summary": "Cleaned data", "files": ["clean.csv"]},
            ],
        }
        content = get_claude_md_content(step_context=ctx)
        assert "Current Task Context" in content
        assert "3 of 5" in content
        assert "Process data" in content
        assert "steps [1, 2]" in content
        assert "Downloaded CSV" in content
        assert "data.csv" in content

    def test_get_claude_md_independent_step(self):
        from orchestrator.claude_config import get_claude_md_content
        ctx = {
            "step_number": 1,
            "total_steps": 3,
            "step_title": "Init repo",
            "dependencies": [],
            "prior_steps": [],
        }
        content = get_claude_md_content(step_context=ctx)
        assert "none (independent step)" in content
        assert "Prior Steps Output" not in content


class TestBuildPlannerWorkspaceContext:
    def test_build_planner_workspace_context_empty(self, tmp_path):
        result = build_planner_workspace_context(str(tmp_path))
        assert result == ""

    def test_build_planner_workspace_context_with_json(self, tmp_path):
        (tmp_path / "data.json").write_text(
            '{"metadata": {"version": 1}, "anomalies": []}'
        )
        result = build_planner_workspace_context(str(tmp_path))
        assert "data.json" in result
        assert "keys:" in result
        assert "metadata" in result

    def test_build_planner_workspace_context_truncation(self, tmp_path):
        (tmp_path / "a.txt").write_text("hello world")
        (tmp_path / "b.txt").write_text("another file")
        result = build_planner_workspace_context(str(tmp_path), max_chars=20)
        assert "[planner workspace scan truncated]" in result

    def test_build_planner_workspace_context_circular_import_safety(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / "data.json").write_text('{"k": "v"}')

        import architect.main as architect_main

        class _Raiser:
            def __getattr__(self, name):
                if name == "_extract_json_keys":
                    raise ImportError("simulated circular import")
                return getattr(architect_main, name)

        import sys
        monkeypatch.setitem(sys.modules, "architect.main", _Raiser())
        result = build_planner_workspace_context(str(tmp_path))
        assert result != ""
        assert "data.json" in result


class TestRunLocalDashPFlag:
    """PLAN.md Section 6: -P flag prevents workspace files from shadowing
    framework modules in the orchestrator subprocess invocation.
    """

    def test_run_local_passes_dash_p_flag(self, tmp_path, monkeypatch):
        """The non-streaming branch must inject -P after sys.executable."""
        monkeypatch.setenv("UAS_WORKSPACE", str(tmp_path))

        captured: dict = {}

        def fake_run(cmd, *args, **kwargs):
            captured["cmd"] = list(cmd)
            captured["kwargs"] = kwargs
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(_executor_module.subprocess, "run", fake_run)
        monkeypatch.setattr(
            _executor_module.config, "get",
            lambda key, default=None: (
                str(tmp_path) if key == "workspace" else default
            ),
        )

        _run_local("noop")

        cmd = captured["cmd"]
        assert "-P" in cmd, f"-P flag missing from command: {cmd}"
        py_idx = cmd.index(sys.executable)
        assert cmd[py_idx + 1] == "-P", (
            f"-P must immediately follow sys.executable; got: {cmd}"
        )
        assert "-m" in cmd
        m_idx = cmd.index("-m")
        assert m_idx > py_idx + 1, "-m must come after -P"
        assert cmd[m_idx + 1] == "orchestrator.main"

    def test_run_local_streaming_passes_dash_p_flag(self, tmp_path, monkeypatch):
        """The streaming branch (used when output_callback is set) must
        also inject -P after sys.executable."""
        monkeypatch.setenv("UAS_WORKSPACE", str(tmp_path))

        captured: dict = {}

        def fake_streaming(cmd, *args, **kwargs):
            captured["cmd"] = list(cmd)
            captured["kwargs"] = kwargs
            return {"exit_code": 0, "stdout": "", "stderr": ""}

        monkeypatch.setattr(
            _executor_module, "_run_streaming", fake_streaming,
        )
        monkeypatch.setattr(
            _executor_module.config, "get",
            lambda key, default=None: (
                str(tmp_path) if key == "workspace" else default
            ),
        )

        _run_local("noop", output_callback=lambda line: None)

        assert "cmd" in captured, "_run_streaming was not invoked"
        cmd = captured["cmd"]
        assert "-P" in cmd, f"-P flag missing from streaming command: {cmd}"
        py_idx = cmd.index(sys.executable)
        assert cmd[py_idx + 1] == "-P", (
            f"-P must immediately follow sys.executable; got: {cmd}"
        )
        assert "orchestrator.main" in cmd

    def test_run_local_does_not_shadow_framework_modules(
        self, tmp_path, monkeypatch,
    ):
        """End-to-end check: with -P, a workspace cwd containing junk
        config.py / state.py / executor.py / events.py / hooks.py does
        NOT shadow the framework's same-named modules.

        We don't actually run the orchestrator (it would call the LLM
        and hang). Instead we intercept _run_local's subprocess.run and
        substitute a tiny ``python3 -P -c "import config; ..."`` invocation
        that uses the same cwd and env _run_local would have used. The
        substituted subprocess proves the -P flag is the structural
        defense by failing if any of the workspace shadow modules win
        the import race.
        """
        for name in (
            "config.py", "state.py", "executor.py", "events.py", "hooks.py",
        ):
            (tmp_path / name).write_text(
                'raise ImportError("workspace shadow")\n'
            )

        monkeypatch.setenv("UAS_WORKSPACE", str(tmp_path))
        monkeypatch.setattr(
            _executor_module.config, "get",
            lambda key, default=None: (
                str(tmp_path) if key == "workspace" else default
            ),
        )

        captured_cmds: list = []
        real_run = subprocess.run

        def intercepted_run(cmd, *args, **kwargs):
            captured_cmds.append(list(cmd))
            # Replace the orchestrator invocation with a controlled probe:
            # import the framework's top-level ``config`` module (the only
            # actually-bare-imported module today, and the original Root
            # Cause B trigger). Verify it resolves to the framework copy
            # and not the workspace shadow file. The -P flag (preserved
            # from the original cmd at index 1) is what makes this work.
            probe = (
                "import config; "
                "assert hasattr(config, 'get'), "
                "f'wrong module: {config.__file__}'; "
                "print('OK')"
            )
            new_cmd = [cmd[0], cmd[1], "-c", probe]
            return real_run(new_cmd, *args, **kwargs)

        monkeypatch.setattr(
            _executor_module.subprocess, "run", intercepted_run,
        )

        result = _run_local("noop")

        assert captured_cmds, "subprocess.run was never called"
        cmd = captured_cmds[0]
        assert "-P" in cmd, (
            f"-P flag absent from intercepted command: {cmd}"
        )
        assert "workspace shadow" not in result["stderr"], (
            f"Workspace junk modules shadowed framework modules despite -P. "
            f"stderr={result['stderr']!r}"
        )
        assert result["exit_code"] == 0, (
            f"Probe subprocess failed (exit {result['exit_code']}). "
            f"stderr={result['stderr']!r} stdout={result['stdout']!r}"
        )
        assert "OK" in result["stdout"]

    def test_run_local_safe_path_does_not_propagate_to_grandchildren(
        self, tmp_path, monkeypatch,
    ):
        """Justify -P flag over PYTHONSAFEPATH=1 env var: -P is process-local
        and does NOT propagate to grandchild processes (e.g. pytest spawned
        by the orchestrator), so user tests still see workspace-relative
        imports normally.
        """
        monkeypatch.setenv("UAS_WORKSPACE", str(tmp_path))
        monkeypatch.delenv("PYTHONSAFEPATH", raising=False)

        captured: dict = {}

        def fake_run(cmd, *args, **kwargs):
            captured["env"] = kwargs.get("env")
            captured["cmd"] = list(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(_executor_module.subprocess, "run", fake_run)
        monkeypatch.setattr(
            _executor_module.config, "get",
            lambda key, default=None: (
                str(tmp_path) if key == "workspace" else default
            ),
        )

        _run_local("noop")

        env = captured.get("env") or {}
        assert "PYTHONSAFEPATH" not in env, (
            f"PYTHONSAFEPATH must NOT be set in subprocess env (would "
            f"propagate to grandchildren); got env keys: {sorted(env)}"
        )
