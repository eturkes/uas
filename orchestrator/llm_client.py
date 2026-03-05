"""LLM client via the Claude Code CLI subprocess wrapper."""

import logging
import os
import shutil
import subprocess

CLAUDE_TIMEOUT = 120

logger = logging.getLogger(__name__)


class ClaudeCodeClient:
    """Calls the locally installed Claude Code CLI to generate responses."""

    def __init__(self, timeout: int = CLAUDE_TIMEOUT):
        self.timeout = timeout

    def generate(self, prompt: str) -> str:
        """Send a prompt to Claude Code CLI and return the text response."""
        # Resolve the absolute path to the claude binary so subprocess
        # never fails due to a missing or overwritten PATH.
        claude_path = shutil.which("claude")
        if claude_path:
            cmd = [claude_path, "-p", prompt, "--dangerously-skip-permissions"]
        else:
            cmd = [
                "npx", "-y", "@anthropic-ai/claude-code",
                "-p", prompt, "--dangerously-skip-permissions",
            ]

        # Copy the full current environment to preserve PATH and other vars.
        # Only strip session-specific vars that cause nested-session detection.
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_SESSION", None)
        env["IS_SANDBOX"] = "1"

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                stdin=subprocess.DEVNULL,
                env=env,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"Claude Code CLI timed out after {self.timeout} seconds."
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                f"Claude CLI executable not found in PATH: {e}"
            )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise RuntimeError(
                f"Claude Code CLI exited with code {result.returncode}: {stderr}"
            )

        return result.stdout.strip()


def get_llm_client() -> ClaudeCodeClient:
    """Factory: return a ClaudeCodeClient instance."""
    logger.info("Using Claude Code CLI")
    return ClaudeCodeClient()
