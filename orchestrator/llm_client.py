"""LLM client via the Claude Code CLI subprocess wrapper."""

import logging
import os
import shutil
import subprocess
import time

DEFAULT_TIMEOUT = 120
MAX_RETRIES = 2
INITIAL_BACKOFF = 2

logger = logging.getLogger(__name__)

# Transient error indicators (case-insensitive substring match on stderr).
TRANSIENT_PATTERNS = [
    "timed out",
    "timeout",
    "connection error",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "temporary failure",
]


def _is_transient(error_message: str) -> bool:
    """Return True if the error looks transient and worth retrying."""
    lower = error_message.lower()
    return any(pat in lower for pat in TRANSIENT_PATTERNS)


class ClaudeCodeClient:
    """Calls the locally installed Claude Code CLI to generate responses."""

    def __init__(self, timeout: int = DEFAULT_TIMEOUT, model: str | None = None):
        self.timeout = timeout
        self.model = model

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

        if self.model:
            cmd.extend(["--model", self.model])

        # Copy the full current environment to preserve PATH and other vars.
        # Only strip session-specific vars that cause nested-session detection.
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_SESSION", None)
        env["IS_SANDBOX"] = "1"

        last_error: RuntimeError | None = None
        for attempt in range(1 + MAX_RETRIES):
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
                last_error = RuntimeError(
                    f"Claude Code CLI timed out after {self.timeout} seconds."
                )
                if attempt < MAX_RETRIES:
                    wait = INITIAL_BACKOFF * (2 ** attempt)
                    logger.warning(
                        "Transient error (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1, 1 + MAX_RETRIES, wait, last_error,
                    )
                    time.sleep(wait)
                    continue
                raise last_error
            except FileNotFoundError as e:
                raise RuntimeError(
                    f"Claude CLI executable not found in PATH: {e}"
                )

            if result.returncode != 0:
                stderr = result.stderr.strip()
                error = RuntimeError(
                    f"Claude Code CLI exited with code {result.returncode}: {stderr}"
                )
                if _is_transient(stderr) and attempt < MAX_RETRIES:
                    wait = INITIAL_BACKOFF * (2 ** attempt)
                    logger.warning(
                        "Transient error (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1, 1 + MAX_RETRIES, wait, error,
                    )
                    time.sleep(wait)
                    last_error = error
                    continue
                raise error

            return result.stdout.strip()

        # Should not be reached, but satisfy type checker.
        raise last_error  # type: ignore[misc]


def get_llm_client() -> ClaudeCodeClient:
    """Factory: return a ClaudeCodeClient instance."""
    timeout_str = os.environ.get("UAS_LLM_TIMEOUT")
    timeout = int(timeout_str) if timeout_str else DEFAULT_TIMEOUT
    model = os.environ.get("UAS_MODEL") or None
    logger.info("Using Claude Code CLI")
    return ClaudeCodeClient(timeout=timeout, model=model)
