"""LLM client via the Claude Code CLI subprocess wrapper."""

import contextlib
import dataclasses
import json as _json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import typing

DEFAULT_TIMEOUT = None
MAX_RETRIES = 4
INITIAL_BACKOFF = 2
OVERLOADED_BACKOFF = 30

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Structured error classification (Section 2 of PLAN.md)
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class LLMError:
    """Structured classification of an LLM CLI error."""
    category: str          # "rate_limit" | "capacity" | "auth" | "connection" | "timeout" | "prompt_too_long" | "output_truncated" | "unknown"
    message: str
    retryable: bool
    recommended_backoff: float  # seconds, 0 if not retryable
    raw_output: str


# Pattern lists used by classify_error — kept module-level for testability.
_AUTH_PATTERNS = [
    "not logged in",
    "please run /login",
    "authentication required",
    "unauthorized",
    "invalid api key",
    "invalid credentials",
    "expired token",
    "session expired",
]

_RATE_LIMIT_PATTERNS = [
    "rate limit",
    "rate_limit",
    "hit your limit",
    "too many requests",
    "out of extra usage",
    "out of usage",
    "429",
]

_CAPACITY_PATTERNS = [
    "529",
    "overloaded",
    "overloaded_error",
    "capacity",
    "503",
]

_CONNECTION_PATTERNS = [
    "connection error",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "temporary failure",
]

_TIMEOUT_PATTERNS = [
    "timed out",
    "timeout",
]

_PROMPT_TOO_LONG_PATTERNS = [
    "prompt too long",
    "prompt is too long",
    "context length exceeded",
    "max.*token.*exceeded",
    "input too long",
]


def classify_error(returncode: int, stdout: str, stderr: str) -> LLMError:
    """Classify a CLI error into a structured ``LLMError``.

    Pure function — no I/O.  Examines *returncode*, *stdout*, and *stderr*
    to determine the error category and recommended recovery action.
    """
    combined = f"{stderr} {stdout}".lower()
    raw = f"{stderr} {stdout}".strip()

    def _matches(patterns: list[str]) -> bool:
        return any(pat in combined for pat in patterns)

    if _matches(_AUTH_PATTERNS):
        return LLMError(
            category="auth",
            message="Claude Code CLI is not authenticated. "
                    "Please run 'claude /login' or check .uas_auth/ credentials.",
            retryable=False,
            recommended_backoff=0,
            raw_output=raw,
        )

    if _matches(_PROMPT_TOO_LONG_PATTERNS):
        return LLMError(
            category="prompt_too_long",
            message="Prompt exceeds maximum context length.",
            retryable=False,
            recommended_backoff=0,
            raw_output=raw,
        )

    if _matches(_RATE_LIMIT_PATTERNS):
        return LLMError(
            category="rate_limit",
            message="API rate limit hit.",
            retryable=True,
            recommended_backoff=OVERLOADED_BACKOFF,
            raw_output=raw,
        )

    if _matches(_CAPACITY_PATTERNS):
        return LLMError(
            category="capacity",
            message="API at capacity.",
            retryable=True,
            recommended_backoff=OVERLOADED_BACKOFF,
            raw_output=raw,
        )

    if _matches(_CONNECTION_PATTERNS):
        return LLMError(
            category="connection",
            message="Network/connection error.",
            retryable=True,
            recommended_backoff=INITIAL_BACKOFF,
            raw_output=raw,
        )

    if _matches(_TIMEOUT_PATTERNS):
        return LLMError(
            category="timeout",
            message="Request timed out.",
            retryable=True,
            recommended_backoff=INITIAL_BACKOFF,
            raw_output=raw,
        )

    # Check for output truncation: non-empty stdout with non-zero exit code
    # suggests the LLM produced partial output before the process was killed.
    if returncode != 0 and stdout.strip():
        return LLMError(
            category="output_truncated",
            message=f"CLI exited with code {returncode} but produced partial output.",
            retryable=False,
            recommended_backoff=0,
            raw_output=raw,
        )

    return LLMError(
        category="unknown",
        message=f"CLI exited with code {returncode}.",
        retryable=False,
        recommended_backoff=0,
        raw_output=raw,
    )


HEARTBEAT_INTERVAL = 15

# ---------------------------------------------------------------------------
# Token & cost tracking (Section 1 of PLAN.md)
# ---------------------------------------------------------------------------

COST_PER_1K = {
    "claude-opus-4-6":   {"input": 0.015, "output": 0.075},
    "claude-sonnet-4-6": {"input": 0.003, "output": 0.015},
    "claude-haiku-4-5":  {"input": 0.0008, "output": 0.004},
}


class LLMResult(typing.NamedTuple):
    """Return value of ``ClaudeCodeClient.generate()``.

    Supports tuple unpacking (``text, usage = client.generate(...)``)
    and attribute access (``result.text``, ``result.usage``).
    """
    text: str
    usage: dict  # {"input": int, "output": int}


def estimate_cost(model: str, usage: dict) -> float:
    """Estimate cost in USD from token counts and model name."""
    rates = COST_PER_1K.get(model)
    if not rates or not usage:
        return 0.0
    inp = usage.get("input", 0)
    out = usage.get("output", 0)
    return (inp / 1000) * rates["input"] + (out / 1000) * rates["output"]


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

    @staticmethod
    def _parse_json_output(stdout: str, model: str) -> LLMResult:
        """Try to parse CLI JSON output and extract text + usage.

        Falls back to treating *stdout* as plain text when parsing fails.
        """
        try:
            data = _json.loads(stdout)
            text = data.get("result", "")
            raw_usage = data.get("usage") or {}
            usage = {
                "input": raw_usage.get("input_tokens", 0),
                "output": raw_usage.get("output_tokens", 0),
            }
            return LLMResult(text=text, usage=usage)
        except (_json.JSONDecodeError, TypeError, AttributeError):
            return LLMResult(text=stdout.strip(), usage={"input": 0, "output": 0})

    def generate(self, prompt: str, stream: bool = False) -> LLMResult:
        """Send a prompt to Claude Code CLI and return text + token usage.

        Returns an ``LLMResult(text, usage)`` named tuple.  Callers can
        unpack (``text, usage = client.generate(...)``) or use attribute
        access.  All calls use ultrathink for maximum reasoning depth.
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

        model = self.model or "claude-opus-4-6"
        cmd.extend(["--model", model])
        cmd.extend(["--effort", "max"])
        cmd.extend(["--output-format", "json"])

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
        iso_config = os.path.join(isolation_dir, ".claude")
        os.makedirs(iso_config, exist_ok=True)

        # Copy credentials from the real config dir so the CLI can
        # authenticate.  The original CLAUDE_CONFIG_DIR (or ~/.claude)
        # is set by run_local.sh / the container entrypoint; we must
        # not lose it when redirecting to the isolation dir.
        _orig_config = env.get("CLAUDE_CONFIG_DIR") or os.path.join(
            os.path.expanduser("~"), ".claude",
        )
        for _cred_name in (".credentials.json", "credentials.json"):
            _src = os.path.join(_orig_config, _cred_name)
            if os.path.isfile(_src):
                shutil.copy2(_src, os.path.join(iso_config, _cred_name))

        env["CLAUDE_CONFIG_DIR"] = iso_config

        try:
            capacity_retries = 0
            MAX_CAPACITY_RETRIES = 3  # Matches Claude Code's MAX_529_RETRIES

            last_error: RuntimeError | None = None
            for attempt in range(1 + MAX_RETRIES):
                try:
                    stdout, stderr, returncode = self._run_streaming(
                        cmd, env, input_text=prompt, cwd=isolation_dir,
                    )
                except subprocess.TimeoutExpired:
                    err = classify_error(
                        -1, "", f"timed out after {self.timeout}s",
                    )
                    last_error = RuntimeError(err.message)
                    if attempt < MAX_RETRIES:
                        wait = err.recommended_backoff * (2 ** attempt)
                        logger.warning(
                            "[%s] error (attempt %d/%d), retrying in %ds: %s",
                            err.category, attempt + 1, 1 + MAX_RETRIES,
                            int(wait), err.message,
                        )
                        time.sleep(wait)
                        continue
                    raise last_error
                except FileNotFoundError as e:
                    raise RuntimeError(
                        f"Claude CLI executable not found in PATH: {e}"
                    )

                if returncode != 0:
                    err = classify_error(returncode, stdout, stderr)

                    # Non-retryable errors: raise immediately.
                    if not err.retryable:
                        if err.category == "auth":
                            raise RuntimeError(
                                f"{err.message} CLI output: {err.raw_output[:200]}"
                            )
                        if err.category == "prompt_too_long":
                            raise RuntimeError(err.message)
                        if err.category == "output_truncated":
                            logger.warning(
                                "Claude Code CLI exited with code %d but "
                                "produced output (%d chars); returning "
                                "partial output for truncation recovery.",
                                returncode, len(stdout.strip()),
                            )
                            return self._parse_json_output(stdout.strip(), model)
                        # unknown — raise
                        raise RuntimeError(
                            f"Claude Code CLI exited with code {returncode}: "
                            f"{err.raw_output[:500]}"
                        )

                    # Capacity errors have a limited retry budget (3).
                    if err.category == "capacity":
                        capacity_retries += 1
                        if capacity_retries > MAX_CAPACITY_RETRIES:
                            raise RuntimeError(
                                f"Exceeded max capacity retries "
                                f"({MAX_CAPACITY_RETRIES}): {err.message}"
                            )

                    if attempt < MAX_RETRIES:
                        wait = err.recommended_backoff * (2 ** attempt)
                        logger.warning(
                            "[%s] error (attempt %d/%d), retrying in %ds: %s",
                            err.category, attempt + 1, 1 + MAX_RETRIES,
                            int(wait), err.message,
                        )
                        time.sleep(wait)
                        last_error = RuntimeError(err.message)
                        continue

                    raise RuntimeError(
                        f"Claude Code CLI exited with code {returncode}: "
                        f"{err.raw_output[:500]}"
                    )

                # Auth errors can arrive on stdout with exit code 0.
                parsed = self._parse_json_output(stdout, model)
                if parsed.text:
                    auth_err = classify_error(0, parsed.text, "")
                    if auth_err.category == "auth":
                        raise RuntimeError(
                            f"{auth_err.message} CLI output: {parsed.text[:200]}"
                        )
                return parsed

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
