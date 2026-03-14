"""LLM client via the Claude Code CLI subprocess wrapper."""

import contextlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time

DEFAULT_TIMEOUT = None
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

    def _run_streaming(self, cmd, env):
        """Run the CLI streaming stdout to the logger line-by-line."""
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            stdin=subprocess.DEVNULL,
            env=env,
        )
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

        When stream=True, output is printed to stderr line-by-line as it
        arrives, providing real-time visibility into LLM generation.

        For non-streaming calls, uses ``--output-format json`` for cleaner
        response extraction (Section 5a), falling back to text mode on
        JSON parse failure.
        """
        # "ultrathink" is only beneficial for planning/reasoning tasks.
        # For code generation, it can cause the LLM to exhaust output tokens
        # on thinking without producing the actual code block.
        if stream:
            prompt = f"ultrathink\n\n{prompt}"

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

        # Section 5a: Use JSON output for non-streaming calls for cleaner
        # response extraction.  Streaming keeps text mode since it reads
        # stdout line-by-line.
        use_json = not stream
        if use_json:
            cmd.extend(["--output-format", "json"])
            # Only coder calls have tools disabled, to force fenced code
            # output instead of a multi-turn tool-use conversation.
            # Planner calls intentionally keep tools enabled so the LLM
            # can research APIs and libraries during decomposition.
            if self.role == "coder":
                cmd.extend(["--tools", ""])

        if self.model:
            cmd.extend(["--model", self.model])

        # Copy the full current environment to preserve PATH and other vars.
        # Only strip session-specific vars that cause nested-session detection.
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_SESSION", None)
        env["IS_SANDBOX"] = "1"
        env.setdefault("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "128000")
        # Planner streams with tool access so it can research APIs and
        # libraries during decomposition.  Streaming calls also benefit
        # from deep thinking.  Non-streaming calls (code generation) need
        # output tokens for the actual code, so use medium effort to
        # avoid exhausting the budget on extended thinking.
        env["CLAUDE_CODE_EFFORT_LEVEL"] = "high" if stream else "medium"

        last_error: RuntimeError | None = None
        for attempt in range(1 + MAX_RETRIES):
            try:
                if stream:
                    stdout, stderr, returncode = self._run_streaming(cmd, env)
                else:
                    with heartbeat_log("LLM responding"):
                        r = subprocess.run(
                            cmd,
                            capture_output=True,
                            text=True,
                            timeout=self.timeout,
                            stdin=subprocess.DEVNULL,
                            env=env,
                        )
                    stdout, stderr, returncode = r.stdout, r.stderr, r.returncode
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
                error = RuntimeError(
                    f"Claude Code CLI exited with code {returncode}: {stderr_s}"
                )
                if _is_transient(stderr_s) and attempt < MAX_RETRIES:
                    wait = INITIAL_BACKOFF * (2 ** attempt)
                    logger.warning(
                        "Transient error (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1, 1 + MAX_RETRIES, wait, error,
                    )
                    time.sleep(wait)
                    last_error = error
                    continue
                raise error

            raw = stdout.strip()

            # Section 5a: Parse JSON output when available
            if use_json:
                parsed = _extract_from_json(raw)
                if parsed is not None:
                    return parsed
                logger.debug("JSON parse failed, falling back to text mode.")

            return raw

        # Should not be reached, but satisfy type checker.
        raise last_error  # type: ignore[misc]


def _extract_from_json(raw: str) -> str | None:
    """Try to extract the ``result`` field from a JSON CLI response.

    Claude Code's ``--output-format json`` emits a JSON object with a
    ``result`` field containing the LLM's text response.  Returns the
    extracted text, or None if parsing fails.
    """
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "result" in data:
            return data["result"].strip()
    except (json.JSONDecodeError, ValueError, AttributeError):
        pass
    return None


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

    logger.info("Using Claude Code CLI (role=%s, model=%s)", role, model)
    return ClaudeCodeClient(timeout=timeout, model=model, role=role)
