"""LLM client via the Claude Code CLI subprocess wrapper."""

import os
import subprocess

CLAUDE_TIMEOUT = 120


class ClaudeCodeClient:
    """Calls the locally installed Claude Code CLI to generate responses."""

    def __init__(self, timeout: int = CLAUDE_TIMEOUT):
        self.timeout = timeout

    def generate(self, prompt: str) -> str:
        """Send a prompt to Claude Code CLI and return the text response."""
        # Remove CLAUDECODE env var to prevent nested-session detection,
        # and strip other session-specific vars that leak from parent.
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("CLAUDECODE", "CLAUDE_CODE_SESSION")
        }
        env["IS_SANDBOX"] = "1"
        try:
            result = subprocess.run(
                [
                    "claude",
                    "-p",
                    prompt,
                    "--dangerously-skip-permissions",
                ],
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
        except FileNotFoundError:
            raise RuntimeError(
                "Claude Code CLI not found. Ensure @anthropic-ai/claude-code "
                "is installed globally."
            )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise RuntimeError(
                f"Claude Code CLI exited with code {result.returncode}: {stderr}"
            )

        return result.stdout.strip()


def get_llm_client() -> ClaudeCodeClient:
    """Factory: return a ClaudeCodeClient instance."""
    print("Using Claude Code CLI")
    return ClaudeCodeClient()
