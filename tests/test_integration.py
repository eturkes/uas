"""Integration tests that exercise the real Claude CLI.

These tests require:
1. The ``claude`` CLI binary installed and in PATH
2. Valid authentication credentials in ~/.claude/

On first successful run, a .uas_auth symlink is created in the project
root pointing to ~/.claude, matching the container-mode auth pattern.

Run only integration tests::

    pytest -m integration

Skip integration tests::

    pytest -m "not integration"
"""

import os
import subprocess
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.integration
class TestClaudeCLI:
    """Verify the Claude CLI is available and responds to prompts."""

    def test_cli_responds(self, require_auth):
        """Send a trivial prompt and verify a non-empty response."""
        from orchestrator.llm_client import ClaudeCodeClient

        client = ClaudeCodeClient(timeout=60)
        response = client.generate("Reply with exactly the word: PONG")
        assert response, "Claude CLI returned empty response"
        assert len(response.strip()) > 0


@pytest.mark.integration
class TestPhase1Decomposition:
    """Test Phase 1 goal decomposition with the real LLM."""

    def test_decompose_trivial_goal(self, require_auth, tmp_path):
        """Decompose a trivial goal via --dry-run and verify step output."""
        env = os.environ.copy()
        env["UAS_WORKSPACE"] = str(tmp_path)
        env["PYTHONPATH"] = PROJECT_ROOT

        result = subprocess.run(
            [
                sys.executable, "-m", "architect.main",
                "--dry-run", "Print the current date and time",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(tmp_path),
            env=env,
            stdin=subprocess.DEVNULL,
        )

        assert result.returncode == 0, (
            f"Phase 1 decomposition failed (exit {result.returncode}):\n"
            f"stderr: {result.stderr}\n"
            f"stdout: {result.stdout}"
        )
        # Dry-run prints the step DAG to stderr
        assert "Step 1" in result.stderr, (
            f"No steps found in decomposition output:\n{result.stderr}"
        )


@pytest.mark.integration
class TestOrchestratorExecution:
    """Test the full orchestrator loop with real LLM and local sandbox."""

    def test_trivial_task(self, require_auth, tmp_path):
        """Execute a trivial task: create a file with known content."""
        env = os.environ.copy()
        env["UAS_SANDBOX_MODE"] = "local"
        env["UAS_WORKSPACE"] = str(tmp_path)
        env["PYTHONPATH"] = PROJECT_ROOT
        env["UAS_TASK"] = (
            "Write a Python script that creates a file called hello.txt "
            "in the workspace directory containing exactly the text: "
            "Hello from UAS"
        )

        result = subprocess.run(
            [sys.executable, "-m", "orchestrator.main"],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(tmp_path),
            env=env,
            stdin=subprocess.DEVNULL,
        )

        assert result.returncode == 0, (
            f"Orchestrator failed (exit {result.returncode}):\n"
            f"stderr: {result.stderr}\n"
            f"stdout: {result.stdout}"
        )

        hello_file = os.path.join(str(tmp_path), "hello.txt")
        assert os.path.isfile(hello_file), (
            f"hello.txt was not created. "
            f"Workspace contents: {os.listdir(str(tmp_path))}"
        )
        with open(hello_file, "r", encoding="utf-8") as f:
            content = f.read()
        assert "Hello from UAS" in content, (
            f"hello.txt has unexpected content: {content!r}"
        )
