"""Smoke tests for the full subprocess.run pipeline.

Verifies the simplified subprocess chain:
    architect.executor -> orchestrator.main -> llm_client -> claude CLI
                                            -> sandbox -> python3

Each layer communicates via subprocess.run (no Popen, no streaming pipes).
"""

import ast
import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cp(stdout="", stderr="", returncode=0):
    """Create a CompletedProcess for mocking subprocess.run."""
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def _claude_json_response(text="print('hello')", input_tokens=100,
                          output_tokens=50):
    """Build a JSON string mimicking claude CLI --output-format json."""
    return json.dumps({
        "result": text,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    })


# ---------------------------------------------------------------------------
# 1. No subprocess.Popen in the main code paths
# ---------------------------------------------------------------------------

class TestNoPopen:
    """Verify Popen was fully removed from orchestrator and architect modules."""

    @staticmethod
    def _scan_for_popen(filepath: str) -> list[str]:
        """Return lines containing 'Popen' calls in a Python source file."""
        with open(filepath, encoding="utf-8") as f:
            source = f.read()
        try:
            tree = ast.parse(source, filename=filepath)
        except SyntaxError:
            return []
        hits = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "Popen":
                hits.append(f"{filepath}:{node.lineno}")
            elif isinstance(node, ast.Name) and node.id == "Popen":
                hits.append(f"{filepath}:{node.lineno}")
        return hits

    def test_no_popen_in_llm_client(self):
        from orchestrator import llm_client
        hits = self._scan_for_popen(llm_client.__file__)
        assert hits == [], f"Popen found in llm_client: {hits}"

    def test_no_popen_in_executor(self):
        from architect import executor
        hits = self._scan_for_popen(executor.__file__)
        assert hits == [], f"Popen found in executor: {hits}"

    def test_no_popen_in_sandbox(self):
        from orchestrator import sandbox
        hits = self._scan_for_popen(sandbox.__file__)
        assert hits == [], f"Popen found in sandbox: {hits}"


# ---------------------------------------------------------------------------
# 2. Executor -> Orchestrator subprocess.run chain
# ---------------------------------------------------------------------------

class TestExecutorSubprocessChain:
    """Verify executor invokes orchestrator via subprocess.run."""

    @patch("architect.executor.EXECUTION_MODE", "local")
    @patch("architect.executor.subprocess.run")
    def test_invokes_orchestrator_module(self, mock_run):
        """run_orchestrator() calls subprocess.run with python -m orchestrator.main."""
        mock_run.return_value = _cp(stdout="SUCCESS on attempt 1.", stderr="")
        from architect.executor import run_orchestrator
        result = run_orchestrator("create a hello world script")

        assert mock_run.called
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == sys.executable
        assert "-m" in cmd
        assert "orchestrator.main" in cmd

    @patch("architect.executor.EXECUTION_MODE", "local")
    @patch("architect.executor.subprocess.run")
    def test_passes_task_via_env(self, mock_run):
        """The task string reaches orchestrator via UAS_TASK env var."""
        mock_run.return_value = _cp(stdout="", stderr="")
        from architect.executor import run_orchestrator
        run_orchestrator("create hello.py")

        env = mock_run.call_args.kwargs.get("env") or mock_run.call_args[1].get("env")
        assert env["UAS_TASK"] == "create hello.py"
        assert env["IS_SANDBOX"] == "1"

    @patch("architect.executor.EXECUTION_MODE", "local")
    @patch("architect.executor.subprocess.run")
    def test_uses_capture_output(self, mock_run):
        """subprocess.run is called with capture_output=True (not Popen pipes)."""
        mock_run.return_value = _cp()
        from architect.executor import run_orchestrator
        run_orchestrator("task")

        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is True

    @patch("architect.executor.EXECUTION_MODE", "local")
    @patch("architect.executor.subprocess.run")
    def test_returns_stdout_stderr_exit_code(self, mock_run):
        """Result dict contains exit_code, stdout, stderr from subprocess."""
        mock_run.return_value = _cp(
            stdout="===STDOUT_START===\nhello\n===STDOUT_END===",
            stderr="some logs",
            returncode=0,
        )
        from architect.executor import run_orchestrator
        result = run_orchestrator("task")

        assert result["exit_code"] == 0
        assert "hello" in result["stdout"]
        assert result["stderr"] == "some logs"

    @patch("architect.executor.EXECUTION_MODE", "local")
    @patch("architect.executor.subprocess.run")
    def test_timeout_handled(self, mock_run):
        """TimeoutExpired is caught and returns exit_code -1."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=600)
        from architect.executor import run_orchestrator
        result = run_orchestrator("slow task")

        assert result["exit_code"] == -1
        assert "timed out" in result["stderr"].lower()


# ---------------------------------------------------------------------------
# 3. LLM Client -> Claude CLI subprocess.run chain
# ---------------------------------------------------------------------------

class TestLLMClientSubprocessChain:
    """Verify llm_client.generate() invokes claude CLI via subprocess.run."""

    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_invokes_claude_binary(self, _mock_which, mock_run):
        """generate() calls subprocess.run with the claude binary."""
        mock_run.return_value = _cp(stdout=_claude_json_response())
        from orchestrator.llm_client import ClaudeCodeClient
        client = ClaudeCodeClient()
        client.generate("write hello world")

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/bin/claude"
        assert "-p" in cmd
        assert "--dangerously-skip-permissions" in cmd

    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_prompt_via_stdin(self, _mock_which, mock_run):
        """Prompt is passed via input= kwarg (stdin), not CLI arg."""
        mock_run.return_value = _cp(stdout=_claude_json_response())
        from orchestrator.llm_client import ClaudeCodeClient
        client = ClaudeCodeClient()
        client.generate("my prompt")

        kwargs = mock_run.call_args.kwargs
        assert "my prompt" in kwargs["input"]

    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_output_format_json(self, _mock_which, mock_run):
        """Claude CLI is invoked with --output-format json."""
        mock_run.return_value = _cp(stdout=_claude_json_response())
        from orchestrator.llm_client import ClaudeCodeClient
        client = ClaudeCodeClient()
        client.generate("test")

        cmd = mock_run.call_args[0][0]
        assert "--output-format" in cmd
        idx = cmd.index("--output-format")
        assert cmd[idx + 1] == "json"

    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_parses_json_response(self, _mock_which, mock_run):
        """JSON output from claude CLI is parsed into LLMResult."""
        mock_run.return_value = _cp(
            stdout=_claude_json_response("the answer", 200, 100)
        )
        from orchestrator.llm_client import ClaudeCodeClient
        client = ClaudeCodeClient()
        text, usage = client.generate("test")

        assert text == "the answer"
        assert usage["input"] == 200
        assert usage["output"] == 100

    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value=None)
    def test_npx_fallback(self, _mock_which, mock_run):
        """Falls back to npx when claude binary is not found."""
        mock_run.return_value = _cp(stdout=_claude_json_response())
        from orchestrator.llm_client import ClaudeCodeClient
        client = ClaudeCodeClient()
        client.generate("test")

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "npx"


# ---------------------------------------------------------------------------
# 4. Sandbox -> Python subprocess.run chain
# ---------------------------------------------------------------------------

class TestSandboxSubprocessChain:
    """Verify sandbox executes code via subprocess.run."""

    @patch("orchestrator.sandbox.SANDBOX_MODE", "local")
    @patch("orchestrator.sandbox.WORKSPACE_PATH", "/tmp/test_ws")
    @patch("orchestrator.sandbox.subprocess.run")
    def test_invokes_python3(self, mock_run):
        """run_in_sandbox() calls subprocess.run with python3."""
        mock_run.return_value = _cp(stdout="hello", stderr="")
        from orchestrator.sandbox import run_in_sandbox

        os.makedirs("/tmp/test_ws", exist_ok=True)
        result = run_in_sandbox("print('hello')")

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "python3"
        assert result["exit_code"] == 0
        assert result["stdout"] == "hello"

    @patch("orchestrator.sandbox.SANDBOX_MODE", "local")
    @patch("orchestrator.sandbox.WORKSPACE_PATH", "/tmp/test_ws")
    @patch("orchestrator.sandbox.subprocess.run")
    def test_uses_capture_output(self, mock_run):
        """subprocess.run is called with capture_output=True."""
        mock_run.return_value = _cp()
        from orchestrator.sandbox import run_in_sandbox

        os.makedirs("/tmp/test_ws", exist_ok=True)
        run_in_sandbox("pass")

        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is True

    @patch("orchestrator.sandbox.SANDBOX_MODE", "local")
    @patch("orchestrator.sandbox.WORKSPACE_PATH", "/tmp/test_ws")
    @patch("orchestrator.sandbox.subprocess.run")
    def test_timeout_handled(self, mock_run):
        """TimeoutExpired returns exit_code -1."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="python3", timeout=60)
        from orchestrator.sandbox import run_in_sandbox

        os.makedirs("/tmp/test_ws", exist_ok=True)
        result = run_in_sandbox("import time; time.sleep(999)", timeout=60)

        assert result["exit_code"] == -1
        assert "timed out" in result["stderr"].lower()


# ---------------------------------------------------------------------------
# 5. End-to-end pipeline: executor -> orchestrator -> llm + sandbox
# ---------------------------------------------------------------------------

class TestEndToEndPipeline:
    """Simulate the full pipeline with mocked subprocess.run at leaf layers.

    This verifies the complete chain:
    executor calls orchestrator.main via subprocess.run, which in turn
    calls llm_client (subprocess.run -> claude) and sandbox (subprocess.run
    -> python3).  All layers use subprocess.run, not Popen.
    """

    @patch("architect.executor.EXECUTION_MODE", "local")
    @patch("architect.executor.subprocess.run")
    def test_full_chain_success(self, mock_run):
        """Executor -> orchestrator succeeds and returns UAS_RESULT."""
        uas_result = json.dumps({"status": "ok", "files_written": ["hello.py"],
                                 "summary": "Created hello.py"})
        orchestrator_stdout = (
            "Task: create hello.py\n"
            "Verifying sandbox...\n"
            "Sandbox verified.\n"
            "\n--- Attempt 1/3 ---\n"
            "Querying LLM...\n"
            "Executing in sandbox...\n"
            "Exit code: 0\n"
            "===STDOUT_START===\n"
            f"UAS_RESULT: {uas_result}\n"
            "===STDOUT_END===\n"
            "\nSUCCESS on attempt 1."
        )
        mock_run.return_value = _cp(stdout=orchestrator_stdout, returncode=0)

        from architect.executor import run_orchestrator, parse_uas_result, extract_sandbox_stdout
        result = run_orchestrator("create hello.py")

        assert result["exit_code"] == 0
        stdout = extract_sandbox_stdout(result["stdout"])
        assert "UAS_RESULT" in stdout
        parsed = parse_uas_result(result["stdout"])
        assert parsed is not None
        assert parsed["status"] == "ok"

    @patch("architect.executor.EXECUTION_MODE", "local")
    @patch("architect.executor.subprocess.run")
    def test_full_chain_failure_propagation(self, mock_run):
        """Non-zero exit code propagates through the chain."""
        mock_run.return_value = _cp(
            stdout="FAILED after 3 attempts.",
            stderr="Error: something went wrong",
            returncode=1,
        )
        from architect.executor import run_orchestrator
        result = run_orchestrator("impossible task")

        assert result["exit_code"] == 1
        assert "FAILED" in result["stdout"]

    @patch("architect.executor.EXECUTION_MODE", "local")
    @patch("architect.executor.subprocess.run")
    def test_extra_env_forwarded(self, mock_run):
        """Extra env vars (step_id, step_context) reach orchestrator."""
        mock_run.return_value = _cp()
        from architect.executor import run_orchestrator

        extra = {"UAS_STEP_ID": "3", "UAS_STEP_CONTEXT": '{"title":"test"}'}
        run_orchestrator("task", extra_env=extra)

        env = mock_run.call_args.kwargs.get("env") or mock_run.call_args[1].get("env")
        assert env["UAS_STEP_ID"] == "3"
        assert env["UAS_STEP_CONTEXT"] == '{"title":"test"}'


# ---------------------------------------------------------------------------
# 6. Verify subprocess.run keyword arguments across all layers
# ---------------------------------------------------------------------------

class TestSubprocessRunSignatures:
    """All layers must use subprocess.run with consistent keyword patterns."""

    @patch("architect.executor.EXECUTION_MODE", "local")
    @patch("architect.executor.subprocess.run")
    def test_executor_stdin_devnull(self, mock_run):
        """Executor passes stdin=DEVNULL (orchestrator reads env, not stdin)."""
        mock_run.return_value = _cp()
        from architect.executor import run_orchestrator
        run_orchestrator("task")

        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("stdin") == subprocess.DEVNULL

    @patch("orchestrator.llm_client.subprocess.run")
    @patch("orchestrator.llm_client.shutil.which", return_value="/usr/bin/claude")
    def test_llm_client_uses_input_kwarg(self, _mock_which, mock_run):
        """LLM client passes prompt via input= (stdin pipe)."""
        mock_run.return_value = _cp(stdout=_claude_json_response())
        from orchestrator.llm_client import ClaudeCodeClient
        client = ClaudeCodeClient()
        client.generate("test prompt")

        kwargs = mock_run.call_args.kwargs
        assert "input" in kwargs
        assert "test prompt" in kwargs["input"]
        # When input= is used, stdin should NOT be DEVNULL
        assert kwargs.get("stdin") is None or kwargs.get("stdin") != subprocess.DEVNULL

    @patch("orchestrator.sandbox.SANDBOX_MODE", "local")
    @patch("orchestrator.sandbox.WORKSPACE_PATH", "/tmp/test_ws")
    @patch("orchestrator.sandbox.subprocess.run")
    def test_sandbox_stdin_devnull(self, mock_run):
        """Sandbox passes stdin=DEVNULL (code is in a script file)."""
        mock_run.return_value = _cp()
        from orchestrator.sandbox import run_in_sandbox

        os.makedirs("/tmp/test_ws", exist_ok=True)
        run_in_sandbox("pass")

        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("stdin") == subprocess.DEVNULL
