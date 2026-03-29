"""LLM client via the Claude Code CLI subprocess wrapper."""

import contextlib
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time

DEFAULT_TIMEOUT = None
MAX_RETRIES = 4
INITIAL_BACKOFF = 2
OVERLOADED_BACKOFF = 30

logger = logging.getLogger(__name__)

# Authentication / login error indicators.  When the CLI is not
# authenticated it prints a short message to stdout (e.g.
# "Not logged in · Please run /login") and may exit with code 0.
# These must never be returned as valid LLM content.
AUTH_ERROR_PATTERNS = [
    "not logged in",
    "please run /login",
    "authentication required",
    "unauthorized",
    "invalid api key",
    "invalid credentials",
    "expired token",
    "session expired",
]

# Transient error indicators (case-insensitive substring match on stderr).
TRANSIENT_PATTERNS = [
    "timed out",
    "timeout",
    "connection error",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "temporary failure",
    "rate limit",
    "rate_limit",
    "hit your limit",
    "too many requests",
    "429",
    "529",
    "overloaded",
    "overloaded_error",
    "503",
    "capacity",
]

# Patterns that indicate API overload / rate-limiting (need longer backoff).
_OVERLOADED_PATTERNS = [
    "529",
    "overloaded",
    "overloaded_error",
    "capacity",
    "rate limit",
    "rate_limit",
    "hit your limit",
    "too many requests",
    "429",
]


def _is_overloaded(error_message: str) -> bool:
    """Return True if the error indicates API overload or rate limiting."""
    lower = error_message.lower()
    return any(pat in lower for pat in _OVERLOADED_PATTERNS)


HEARTBEAT_INTERVAL = 15


@contextlib.contextmanager
def heartbeat_log(label, interval=HEARTBEAT_INTERVAL, log=None):
    """Log periodic heartbeat messages during long-running operations.

    Usage::

        with heartbeat_log("LLM responding"):
            result = subprocess.run(...)

    Prints ``label... (Ns elapsed)`` every *interval* seconds until the
    block exits.
    """
    if log is None:
        log = logger
    stop = threading.Event()

    def _beat():
        start = time.monotonic()
        while not stop.wait(interval):
            elapsed = time.monotonic() - start
            log.info("  %s... (%ds elapsed)", label, int(elapsed))

    t = threading.Thread(target=_beat, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(timeout=2)


def _is_auth_error(message: str) -> bool:
    """Return True if the message indicates an authentication/login error."""
    lower = message.lower()
    return any(pat in lower for pat in AUTH_ERROR_PATTERNS)


def _is_transient(error_message: str) -> bool:
    """Return True if the error looks transient and worth retrying."""
    lower = error_message.lower()
    return any(pat in lower for pat in TRANSIENT_PATTERNS)


class ClaudeCodeClient:
    """Calls the locally installed Claude Code CLI to generate responses."""

    def __init__(self, timeout: int | None = DEFAULT_TIMEOUT, model: str | None = None,
                 role: str | None = None):
        self.timeout = timeout
        self.model = model
        self.role = role

    def _run_streaming(self, cmd, env, input_text=None, cwd=None):
        """Run the CLI streaming stdout to the logger line-by-line.

        When *input_text* is provided it is written to the process's stdin
        and the pipe is closed before stdout is consumed.  This avoids
        passing large prompts as command-line arguments which can exceed
        the OS ARG_MAX limit.
        """
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            stdin=subprocess.PIPE if input_text else subprocess.DEVNULL,
            env=env,
            cwd=cwd,
        )
        # Feed the prompt via stdin and close the pipe so the process
        # knows input is complete.  Must happen before reading stdout
        # to avoid deadlocks.
        if input_text and proc.stdin:
            try:
                proc.stdin.write(input_text)
                proc.stdin.close()
            except BrokenPipeError:
                pass  # Process may have exited early
        # Collect stderr in a background thread to avoid pipe deadlock
        stderr_chunks = []
        stderr_thread = threading.Thread(
            target=lambda: stderr_chunks.append(proc.stderr.read()),
            daemon=True,
        )
        stderr_thread.start()

        stdout_lines = []
        for line in proc.stdout:
            logger.info("  %s", line.rstrip())
            stdout_lines.append(line)

        try:
            proc.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise

        stderr_thread.join(timeout=5)
        stdout = "".join(stdout_lines)
        stderr = stderr_chunks[0] if stderr_chunks else ""
        return stdout, stderr, proc.returncode

    def generate(self, prompt: str, stream: bool = False) -> str:
        """Send a prompt to Claude Code CLI and return the text response.

        Output is always streamed line-by-line to the logger, providing
        real-time visibility into LLM generation.  All calls use
        ultrathink for maximum reasoning depth.
        """
        # Always use maximum thinking for all agents.
        prompt = f"ultrathink\n\n{prompt}"

        # Resolve the absolute path to the claude binary so subprocess
        # never fails due to a missing or overwritten PATH.
        # The prompt is passed via stdin (not as a CLI argument) to
        # avoid hitting the OS ARG_MAX limit on large prompts.
        claude_path = shutil.which("claude")
        if claude_path:
            cmd = [claude_path, "-p", "--dangerously-skip-permissions"]
        else:
            cmd = [
                "npx", "-y", "@anthropic-ai/claude-code",
                "-p", "--dangerously-skip-permissions",
            ]

        # All agents have full tool access — they can research APIs,
        # install packages, modify their environment, and use any
        # available tools and skills.

        cmd.extend(["--model", self.model or "claude-opus-4-6"])
        cmd.extend(["--effort", "max"])

        # Copy the full current environment to preserve PATH and other vars.
        # Only strip session-specific vars that cause nested-session detection.
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_SESSION", None)
        env["IS_SANDBOX"] = "1"
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = "128000"

        # Section 9: Isolate the CLI so tool side effects (file writes,
        # .uas_auth/, step scripts) don't land in the workspace.
        env.pop("WORKSPACE", None)
        env.pop("UAS_WORKSPACE", None)
        isolation_dir = tempfile.mkdtemp(prefix="uas_llm_")
        env["CLAUDE_CONFIG_DIR"] = os.path.join(isolation_dir, ".claude")

        try:
            last_error: RuntimeError | None = None
            for attempt in range(1 + MAX_RETRIES):
                try:
                    stdout, stderr, returncode = self._run_streaming(
                        cmd, env, input_text=prompt, cwd=isolation_dir,
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

                if returncode != 0:
                    stderr_s = stderr.strip()
                    stdout_s = stdout.strip()
                    # Check both stdout and stderr for transient errors
                    # (rate limits, network issues, etc.) BEFORE attempting
                    # to salvage partial output — a rate-limit message in
                    # stdout must not be returned as valid LLM content.
                    combined = f"{stderr_s} {stdout_s}"

                    # Auth errors are fatal — never retry or return as content.
                    if _is_auth_error(combined):
                        raise RuntimeError(
                            "Claude Code CLI is not authenticated. "
                            "Please run 'claude /login' or check .uas_auth/ "
                            f"credentials. CLI output: {combined[:200]}"
                        )

                    error = RuntimeError(
                        f"Claude Code CLI exited with code {returncode}: {stderr_s}"
                    )
                    is_transient = _is_transient(combined)
                    if is_transient and attempt < MAX_RETRIES:
                        # Use longer backoff for API overload/rate-limit errors
                        # (529, 429, "overloaded", etc.) since hammering the API
                        # only makes things worse.
                        if _is_overloaded(combined):
                            wait = OVERLOADED_BACKOFF * (2 ** attempt)
                        else:
                            wait = INITIAL_BACKOFF * (2 ** attempt)
                        logger.warning(
                            "Transient error (attempt %d/%d), retrying in %ds: %s",
                            attempt + 1, 1 + MAX_RETRIES, wait, error,
                        )
                        time.sleep(wait)
                        last_error = error
                        continue
                    # If the CLI produced substantial output before failing
                    # (e.g. truncated due to output token limit), return what
                    # we have so downstream truncation handling can attempt
                    # a continuation rather than wasting the partial output.
                    # Only do this for non-transient failures — a rate-limit
                    # or network error message must never be returned as
                    # valid LLM content.
                    if stdout_s and not is_transient:
                        logger.warning(
                            "Claude Code CLI exited with code %d but produced "
                            "output (%d chars); returning partial output for "
                            "truncation recovery.",
                            returncode, len(stdout_s),
                        )
                        return stdout_s
                    raise error

                # Auth errors can arrive on stdout with exit code 0.
                result = stdout.strip()
                if result and _is_auth_error(result):
                    raise RuntimeError(
                        "Claude Code CLI is not authenticated. "
                        "Please run 'claude /login' or check .uas_auth/ "
                        f"credentials. CLI output: {result[:200]}"
                    )
                return result

            # Should not be reached, but satisfy type checker.
            raise last_error  # type: ignore[misc]
        finally:
            shutil.rmtree(isolation_dir, ignore_errors=True)


def get_llm_client(role: str | None = None) -> ClaudeCodeClient:
    """Factory: return a ClaudeCodeClient instance.

    Args:
        role: Optional role hint for model tiering (Section 5c).
            ``"planner"`` uses ``UAS_MODEL_PLANNER`` env var,
            ``"coder"`` uses ``UAS_MODEL_CODER`` env var.
            Falls back to ``UAS_MODEL`` when the role-specific var
            is unset.
    """
    timeout_str = os.environ.get("UAS_LLM_TIMEOUT")
    timeout = int(timeout_str) if timeout_str else DEFAULT_TIMEOUT

    # Section 5c: Model tiering — role-specific model selection
    model = None
    if role == "planner":
        model = os.environ.get("UAS_MODEL_PLANNER") or None
    elif role == "coder":
        model = os.environ.get("UAS_MODEL_CODER") or None
    if not model:
        model = os.environ.get("UAS_MODEL") or None

    if model:
        logger.info("Using Claude Code CLI (role=%s, model=%s)", role, model)
    else:
        logger.info("Using Claude Code CLI (role=%s)", role)
    return ClaudeCodeClient(timeout=timeout, model=model, role=role)
