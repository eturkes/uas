"""Integration tests — run inside the uas-engine container.

These tests require:
1. A container engine (podman or docker) installed
2. Valid authentication credentials in ``.uas_auth/``

Before first run, authenticate with ``bash setup_auth.sh``.
Credentials are saved to the repo-local ``.uas_auth/`` directory
(gitignored) and reused by all subsequent runs.  The container image
is automatically rebuilt when source files change.

Run only integration tests (use ``-s`` to see Claude output)::

    pytest -s -m integration

Skip integration tests::

    pytest -m "not integration"
"""

import os
import sys

import pytest

# conftest.py is auto-loaded by pytest but not directly importable.
sys.path.insert(0, os.path.dirname(__file__))
from conftest import run_in_container  # noqa: E402


@pytest.mark.integration
class TestClaudeCLI:
    """Verify the Claude CLI inside the container responds to prompts."""

    def test_cli_responds(self, require_auth, uas_engine):
        """Send a trivial prompt and verify a non-empty response."""
        stdout, stderr, rc = run_in_container(
            uas_engine,
            ["claude", "-p", "Reply with exactly the word: PONG",
             "--dangerously-skip-permissions"],
            timeout=60,
        )
        assert rc == 0, f"Claude CLI failed (exit {rc}):\nstderr: {stderr}"
        assert stdout.strip(), "Claude CLI returned empty response"


@pytest.mark.integration
class TestPhase1Decomposition:
    """Test Phase 1 goal decomposition with the real LLM."""

    def test_decompose_trivial_goal(self, require_auth, uas_engine, tmp_path):
        """Decompose a trivial goal via --dry-run and verify step output."""
        stdout, stderr, rc = run_in_container(
            uas_engine,
            ["python3", "-P", "-m", "architect.main",
             "--dry-run", "Print the current date and time"],
            env={"UAS_WORKSPACE": "/workspace"},
            workspace=str(tmp_path),
            timeout=120,
        )

        assert rc == 0, (
            f"Phase 1 decomposition failed (exit {rc}):\n"
            f"stderr: {stderr}\nstdout: {stdout}"
        )
        assert "Step 1" in stderr, (
            f"No steps found in decomposition output:\n{stderr}"
        )


@pytest.mark.integration
class TestOrchestratorExecution:
    """Test the full orchestrator loop with real LLM and local sandbox."""

    def test_trivial_task(self, require_auth, uas_engine, tmp_path):
        """Execute a trivial task: create a file with known content."""
        stdout, stderr, rc = run_in_container(
            uas_engine,
            ["python3", "-P", "-m", "orchestrator.main"],
            env={
                "UAS_SANDBOX_MODE": "local",
                "UAS_WORKSPACE": "/workspace",
                "UAS_TASK": (
                    "Write a Python script that creates a file called hello.txt "
                    "in the workspace directory containing exactly the text: "
                    "Hello from UAS"
                ),
            },
            workspace=str(tmp_path),
            timeout=180,
        )

        assert rc == 0, (
            f"Orchestrator failed (exit {rc}):\n"
            f"stderr: {stderr}\nstdout: {stdout}"
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
