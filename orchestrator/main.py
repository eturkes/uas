"""Orchestrator entry point: Build-Run-Evaluate loop."""

import argparse
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Literal

import config

from pydantic import ValidationError

from uas.fuzzy import fuzzy_function
from uas.fuzzy_models import CodeQuality, ExecutionResult, UASResult
from uas.janitor import format_workspace, lint_workspace

from architect.git_state import create_attempt_branch, rollback_to_checkpoint

from .llm_client import get_llm_client
from .parser import extract_code, extract_truncated_block
from .sandbox import run_in_sandbox, run_pytest_in_sandbox

# Section 5d: Delimited output markers for reliable parsing by the architect.
STDOUT_START = "===STDOUT_START==="
STDOUT_END = "===STDOUT_END==="
STDERR_START = "===STDERR_START==="
STDERR_END = "===STDERR_END==="

MAX_RETRIES = 3
MINIMAL_MODE = config.get("minimal")

# Section 1: Module-level token usage accumulator for the orchestrator process.
_orch_usage = {"input": 0, "output": 0, "cost_usd": 0.0}

def _track_usage(usage: dict, model: str | None = None):
    """Accumulate token usage from an LLM call."""
    from .llm_client import estimate_cost
    inp = usage.get("input", 0)
    out = usage.get("output", 0)
    _orch_usage["input"] += inp
    _orch_usage["output"] += out
    _orch_usage["cost_usd"] += estimate_cost(model or "claude-opus-4-6", usage)

PRE_FLIGHT_PROMPT = """\
You are reviewing generated Python code before it runs in a sandbox.

<task>
{task}
</task>

<code>
{code}
</code>

Check for these common issues:
1. Importing a package that is never installed in the script (via uv pip install or pip install)
2. Using file paths without os.path.join(workspace, ...) where workspace = os.environ.get("WORKSPACE", "/workspace")
3. Missing the UAS_RESULT output line entirely
4. Obvious infinite loops or blocking operations (e.g. server.serve_forever() without a thread)
5. Using input() or other interactive operations that require stdin

Return ONLY a JSON object (no other text):
{{"issues": [{{"description": "...", "severity": "critical"}}], "safe_to_run": true}}

severity must be "critical" (code will definitely fail) or "warning" (potential problem).
safe_to_run should be false only when there are critical issues.
If the code looks fine, return: {{"issues": [], "safe_to_run": true}}"""

RETRY_STRATEGY_PROMPT = """\
You are advising a code generation system that is retrying after a failed attempt.

<task>
{task}
</task>

<attempt_info>
Attempt number: {attempt} of {max_retries}
</attempt_info>

<previous_code>
{code_section}
</previous_code>

<error_output>
{error_output}
</error_output>

{history_section}

Based on the error and attempt history, write a focused retry instruction (2-3 sentences) \
that tells the code generator exactly what to do differently. Be specific about the root \
cause and the fix. Do not include any JSON, code blocks, or XML tags — just the plain text \
instruction."""

logger = logging.getLogger(__name__)

_UAS_RESULT_PATTERN = re.compile(
    r"^UAS_RESULT:\s*(\{.*\})\s*$", re.MULTILINE | re.IGNORECASE,
)


@fuzzy_function
def parse_uas_output(stdout: str) -> UASResult:
    """Extract the UAS_RESULT JSON from sandbox stdout. Return structured fields."""


@fuzzy_function
def assess_code_quality(code: str, task: str) -> CodeQuality:
    """Assess generated Python code quality before sandbox execution.

    Analyze the code and task description to determine:
    - has_uas_result: True if the code contains or constructs a 'UAS_RESULT' output line.
    - has_input_call: True if the code calls input() which would block in a
      non-interactive sandbox. Ignore input() appearing only inside string literals.
    - is_file_modification: True if the task description involves modifying, updating,
      inserting into, or editing an existing file (as opposed to creating new files).
    - missing_imports: list of Python module names used in the code but not imported.
      Only include standard library or well-known third-party modules that are clearly
      referenced but missing an import statement.

    Either code or task may be empty when only one aspect is being checked.
    """


@fuzzy_function
def evaluate_sandbox(stdout: str, stderr: str, exit_code: int) -> ExecutionResult:
    """Evaluate the result of a sandbox code execution attempt.

    Analyze the stdout, stderr, and exit_code to produce a structured verdict:
    - success: True if the execution completed its task successfully. An exit_code
      of 0 with a valid UAS_RESULT line reporting status "ok" is the primary
      indicator. An exit_code of 0 without errors is also considered success.
    - revert_needed: True if the execution produced partial or corrupted output
      that could leave the workspace in a broken state (e.g. partially written
      files, import errors after file creation, syntax errors in generated code).
      False if execution either fully succeeded or cleanly failed without side
      effects.
    - error_category: A short label for the failure type if not successful, e.g.
      "syntax_error", "import_error", "runtime_error", "timeout", "test_failure",
      "missing_dependency", or None if successful.
    - summary: A concise one-sentence description of what happened during
      execution, suitable for logging and retry context.
    """


def _task_mentions_file_modification(task: str) -> bool:
    """Return True if the task description mentions modifying an existing file."""
    try:
        quality = assess_code_quality("", task)
        return quality.is_file_modification
    except Exception:
        logger.debug("assess_code_quality failed for file modification check",
                     exc_info=True)
        return False

# Section 17: Module-level cache for resolved PyPI versions.
_pypi_version_cache: dict[str, str] = {}


def _fetch_pypi_version(package: str) -> tuple[str, str | None]:
    """Fetch the latest stable version of *package* from PyPI.

    Returns (package_name, version_string) or (package_name, None) on failure.
    """
    url = f"https://pypi.org/pypi/{package}/json"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
        return package, data.get("info", {}).get("version")
    except Exception:
        return package, None


def resolve_versions(packages: list[str]) -> dict[str, str]:
    """Resolve current stable versions from PyPI for *packages*.

    Only queries packages that don't already have ``==`` in them.
    Results are cached in ``_pypi_version_cache`` for the process lifetime.
    Runs requests concurrently with a ``ThreadPoolExecutor``.
    Returns ``{package: version}``; skips any that failed.
    """
    to_query: list[str] = []
    result: dict[str, str] = {}

    for pkg in packages:
        if "==" in pkg:
            continue
        # Strip any version specifiers (>=, ~=, etc.) to get the bare name
        name = re.split(r"[><=!~]", pkg)[0].strip()
        if not name:
            continue
        if name in _pypi_version_cache:
            result[name] = _pypi_version_cache[name]
        else:
            to_query.append(name)

    if not to_query:
        return result

    with ThreadPoolExecutor(max_workers=min(len(to_query), 8)) as pool:
        futures = {pool.submit(_fetch_pypi_version, name): name
                   for name in to_query}
        for future in as_completed(futures):
            try:
                name, version = future.result()
            except Exception:
                continue
            if version:
                _pypi_version_cache[name] = version
                result[name] = version

    return result


def pre_execution_check(code: str, task: str = "") -> tuple[list[str], list[str]]:
    """Check generated code for guaranteed failures before sandbox execution.

    Returns (critical_errors, warnings). Critical errors mean the code should
    not be executed. Warnings are logged but don't block execution.
    """
    critical_errors: list[str] = []
    warnings: list[str] = []

    # Syntax check (deterministic — always runs)
    try:
        compile(code, "<generated>", "exec")
    except SyntaxError as exc:
        critical_errors.append(f"Syntax error: {exc}")

    # Fuzzy quality assessment
    try:
        quality = assess_code_quality(code, task)
    except Exception:
        logger.debug("assess_code_quality failed, skipping fuzzy checks",
                     exc_info=True)
        return critical_errors, warnings

    if quality.has_input_call:
        critical_errors.append(
            "Code uses input() which requires interactive stdin. "
            "The sandbox has no stdin — this will hang or crash."
        )

    if not quality.has_uas_result:
        warnings.append(
            "Code does not contain 'UAS_RESULT'. "
            "The output may lack the required machine-readable summary line."
        )

    return critical_errors, warnings


def pre_execution_check_llm(code: str, task: str) -> tuple[list[str], list[str]]:
    critical_errors, warnings = pre_execution_check(code, task)
    if critical_errors:
        return critical_errors, warnings

    try:
        from architect.events import EventType, get_event_log

        prompt = PRE_FLIGHT_PROMPT.format(
            task=task,
            code=code,
        )

        event_log = get_event_log()
        event_log.emit(EventType.LLM_CALL_START, data={"purpose": "pre_flight_review"})
        client = get_llm_client(role="planner")
        response, _usage = client.generate(prompt)
        _track_usage(_usage, model=client.model)
        event_log.emit(EventType.LLM_CALL_COMPLETE, data={"purpose": "pre_flight_review"})

        text = response.strip()
        fence_match = re.search(
            r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL,
        )
        if fence_match:
            text = fence_match.group(1)
        else:
            brace_match = re.search(r"\{.*\}", text, re.DOTALL)
            if brace_match:
                text = brace_match.group(0)

        data = json.loads(text)
        issues = data.get("issues", [])
        safe_to_run = data.get("safe_to_run", True)

        for issue in issues:
            desc = issue.get("description", "")
            severity = issue.get("severity", "warning")
            if severity == "critical":
                critical_errors.append(desc)
            else:
                warnings.append(desc)

        if not safe_to_run and not critical_errors:
            critical_errors.append("LLM pre-flight review determined code is not safe to run")

    except Exception:
        logger.debug("LLM pre-flight review failed, using heuristic fallback", exc_info=True)

    return critical_errors, warnings


def collect_system_state() -> str:
    """Collect system state for prompt context."""
    lines = []
    lines.append(f"- Date: {datetime.now().strftime('%Y-%m-%d')}")
    lines.append(f"- Python: {platform.python_version()}")
    lines.append(f"- OS: {platform.system()} {platform.machine()}")
    try:
        usage = shutil.disk_usage(os.environ.get("WORKSPACE", "/workspace"))
        lines.append(f"- Disk free: {round(usage.free / (1024**3), 1)} GB")
    except Exception:
        pass
    return "\n".join(lines)


def parse_uas_result(stdout: str) -> UASResult | None:
    """Extract the UAS_RESULT JSON from stdout if present.

    Uses a two-tier strategy:
    1. **Fast path** — regex extraction + Pydantic validation (no API call).
    2. **Fuzzy fallback** — LLM-backed ``parse_uas_output`` for malformed or
       non-standard output that the regex cannot handle.

    Returns a validated ``UASResult`` or ``None`` if no result is found.
    """
    # Fast path: regex extraction, last match wins.
    matches = list(_UAS_RESULT_PATTERN.finditer(stdout))
    if matches:
        for match in reversed(matches):
            raw = match.group(1)
            for candidate in (raw, raw.replace("'", '"')):
                try:
                    data = json.loads(candidate)
                    return UASResult.model_validate(data)
                except (json.JSONDecodeError, ValueError, ValidationError):
                    continue

    # Fuzzy fallback: only attempt if stdout plausibly contains a UAS result.
    if "uas_result" in stdout.lower():
        try:
            return parse_uas_output(stdout)
        except Exception:
            logger.debug("fuzzy parse_uas_output failed", exc_info=True)

    return None


MAX_CONTINUATIONS = 5


def _extract_header_context(code: str, max_lines: int = 40) -> str:
    """Extract import statements and function/class signatures from the start of code.

    Provides structural context so continuation prompts can reference
    earlier definitions without sending the entire script.
    """
    header_parts: list[str] = []
    for line in code.splitlines()[:max_lines]:
        stripped = line.strip()
        if (stripped.startswith(("import ", "from "))
                or stripped.startswith(("def ", "class "))
                or stripped.startswith("#")
                or not stripped):
            header_parts.append(line)
    return "\n".join(header_parts)


def _contains_tool_calls(response: str) -> bool:
    """Check if an LLM response contains tool call patterns instead of code.

    With full tool access enabled, tool calls are expected and handled by
    the CLI. This now always returns False.
    """
    return False


def _request_continuation(client, truncated_code: str) -> str | None:
    """Ask the LLM to finish a truncated code block.

    Sends the last portion of the truncated code back and asks for
    the remaining lines.  Returns the complete code (prefix + continuation)
    if successful, or None if the continuation also fails.
    """
    # Send the last ~200 lines as context so the LLM knows where it left off.
    lines = truncated_code.splitlines()
    tail_size = min(len(lines), 200)
    tail = "\n".join(lines[-tail_size:])

    # Extract header context (imports, function signatures) so the LLM
    # can reference earlier definitions when continuing.
    header = _extract_header_context(truncated_code)
    header_block = ""
    if header and len(lines) > tail_size:
        header_block = (
            "For reference, here are the imports and definitions from the "
            "START of the script:\n"
            f"```python\n{header}\n```\n\n"
        )

    prompt = (
        "Your previous response was truncated mid-line.  The code block "
        "was cut off before completion.\n\n"
        f"{header_block}"
        "Here is the END of what was generated (the last portion of the script):\n"
        f"```python\n{tail}\n```\n\n"
        "Continue the script from EXACTLY where it was cut off.  "
        "Output ONLY the remaining code that comes after the last line shown above, "
        "inside a ```python fence.  Do NOT repeat any code already shown.  "
        "Do NOT include explanatory text outside the code fence."
    )
    for _attempt in range(MAX_CONTINUATIONS):
        logger.info("Requesting continuation for truncated code "
                     "(attempt %d/%d)...", _attempt + 1, MAX_CONTINUATIONS)
        try:
            response, _usage = client.generate(prompt)
            _track_usage(_usage, model=client.model)
        except Exception as exc:
            logger.warning("Continuation request failed: %s", exc)
            return None

        continuation = extract_code(response)
        if not continuation:
            # Try raw extraction — the continuation may be just a few lines.
            continuation = response.strip()
            # Strip markdown fences if present.
            continuation = re.sub(r"^```(?:python)?\s*\n?", "", continuation)
            continuation = re.sub(r"\n?```\s*$", "", continuation)

        if not continuation:
            logger.warning("Continuation produced no code.")
            return None

        combined = truncated_code + "\n" + continuation
        try:
            compile(combined, "<combined>", "exec")
            logger.info("Continuation succeeded (%d + %d lines).",
                         len(lines), len(continuation.splitlines()))
            return combined
        except SyntaxError:
            logger.warning("Combined code still invalid, retrying continuation...")
            # Update context for next attempt with longer tail.
            tail_size = min(len(combined.splitlines()), 300)
            tail = "\n".join(combined.splitlines()[-tail_size:])
            truncated_code = combined
            prompt = (
                "The continuation was not complete.  Here is the END of the "
                "script so far:\n"
                f"{header_block}"
                f"```python\n{tail}\n```\n\n"
                "Continue from EXACTLY where it ends.  Output ONLY the remaining "
                "code inside a ```python fence."
            )
    return None


def configure_logging(verbose: bool = False):
    """Configure logging: INFO by default, DEBUG with --verbose. Logs go to stderr."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, stream=sys.stderr, format="%(message)s")


def parse_args():
    parser = argparse.ArgumentParser(description="UAS Execution Orchestrator")
    parser.add_argument("task", nargs="*", help="Task to execute")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug output"
    )
    return parser.parse_args()


def get_task(args) -> str:
    """Get task from CLI args, env var, or stdin."""
    if args.task:
        return " ".join(args.task)
    task = config.get("task")
    if task:
        return task
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    print("Enter task (submit with Ctrl+D):", file=sys.stderr)
    return sys.stdin.read().strip()


_TEXT_EXTENSIONS = {
    ".txt", ".csv", ".json", ".py", ".md", ".html", ".xml",
    ".yaml", ".yml", ".log", ".tsv", ".sh", ".cfg", ".ini", ".toml",
}
_SKIP_DIRS = {".uas_state", ".git", "__pycache__", "node_modules", "venv", ".venv", ".claude"}


_EXTENSION_LABELS = {
    ".py": "Python", ".json": "JSON", ".csv": "CSV", ".yaml": "YAML",
    ".yml": "YAML", ".md": "Markdown", ".html": "HTML", ".xml": "XML",
    ".txt": "text", ".log": "Log", ".tsv": "TSV", ".sh": "Shell",
    ".cfg": "Config", ".ini": "Config", ".toml": "TOML",
}

# Priority tiers for file ordering: Python first, then data, then others.
_PRIORITY_EXTENSIONS = {
    ".py": 0,
    ".json": 1, ".csv": 1, ".yaml": 1, ".yml": 1, ".tsv": 1,
}

_PREVIEW_LINES = 30


def _file_sort_key(entry: str) -> tuple[int, str]:
    """Sort key: priority tier first, then alphabetical."""
    _, ext = os.path.splitext(entry.lower())
    tier = _PRIORITY_EXTENSIONS.get(ext, 2)
    return (tier, entry)


def scan_workspace(workspace_path: str, max_chars: int = 8000) -> str:
    """Scan workspace and return a formatted listing with content previews.

    For text files, includes the first 30 lines as an indented preview.
    Binary files show name and size only. Python files are listed first,
    then data files, then everything else. Stays within max_chars budget.
    """
    if not workspace_path or not os.path.isdir(workspace_path):
        return ""
    entries: list[str] = []
    dirs: list[str] = []
    for entry in os.listdir(workspace_path):
        if entry.startswith(".") or entry in _SKIP_DIRS:
            continue
        full = os.path.join(workspace_path, entry)
        if os.path.isfile(full):
            entries.append(entry)
        elif os.path.isdir(full):
            dirs.append(entry)

    if not entries and not dirs:
        return ""

    # Sort files by priority tier, directories alphabetically
    entries.sort(key=_file_sort_key)
    dirs.sort()

    lines: list[str] = ["=== workspace contents ==="]
    total = len(lines[0])

    for entry in entries:
        if total >= max_chars:
            break
        full = os.path.join(workspace_path, entry)
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        _, ext = os.path.splitext(entry.lower())
        is_text = ext in _TEXT_EXTENSIONS
        label = _EXTENSION_LABELS.get(ext, "text" if is_text else "binary")
        header = f"{entry} ({size} bytes, {label}):"
        total += len(header) + 1  # +1 for newline

        if is_text:
            # Read first 30 lines as preview
            preview_lines: list[str] = []
            try:
                with open(full, encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f):
                        if i >= _PREVIEW_LINES:
                            break
                        preview_lines.append(line.rstrip("\n\r"))
            except OSError:
                pass

            if preview_lines:
                preview = "\n".join(f"  {pl}" for pl in preview_lines)
                # Check budget before adding preview
                entry_text = f"{header}\n{preview}\n"
                if total + len(preview) + len(preview_lines) * 2 > max_chars:
                    # Truncate preview to fit budget
                    remaining = max_chars - total - 10
                    if remaining > 0:
                        preview = preview[:remaining]
                        entry_text = f"{header}\n{preview}\n  ...\n"
                    else:
                        entry_text = f"{header}\n"
                else:
                    entry_text = f"{header}\n{preview}\n"
                lines.append(entry_text.rstrip("\n"))
                total += len(entry_text)
            else:
                lines.append(header)
        else:
            # Binary: name and size only
            lines.append(header)

    for d in sorted(dirs):
        if total >= max_chars:
            break
        # List subdirectory contents (one level deep) so later steps
        # can see existing directory names and reuse them consistently.
        subdir = os.path.join(workspace_path, d)
        try:
            sub_entries = [
                e for e in os.listdir(subdir)
                if not e.startswith(".") and e not in _SKIP_DIRS
            ]
        except OSError:
            sub_entries = []
        if sub_entries:
            sub_entries.sort()
            sub_list = ", ".join(sub_entries[:15])
            if len(sub_entries) > 15:
                sub_list += f", ... ({len(sub_entries)} total)"
            line = f"{d}/ (directory: {sub_list})"
        else:
            line = f"{d}/ (empty directory)"
        lines.append(line)
        total += len(line) + 1

    return "\n\n".join(lines)


def _hardcoded_retry_guidance(attempt: int, code_section: str,
                             previous_error: str) -> str:
    if attempt >= MAX_RETRIES:
        return (
            "FINAL ATTEMPT. All previous approaches have failed.\n\n"
            "Write the simplest possible script that accomplishes the core goal:\n"
            "- Use only the standard library if third-party packages are causing issues.\n"
            "- Wrap every external call in try/except with a meaningful fallback.\n"
            "- If the task involves network resources that may be unreliable, include\n"
            "  offline fallback behavior.\n"
            "- Validate every input and assumption.\n\n"
            "Write your analysis in <analysis> tags, then write the defensive script."
        )
    elif attempt > 2:
        return (
            "Your script has failed twice. The previous approach is fundamentally flawed.\n\n"
            "Do NOT repeat the same approach. Step back and consider:\n"
            "- Is there a completely different way to accomplish this task?\n"
            "- Is the task description itself ambiguous? Interpret it more conservatively.\n"
            "- Are you relying on an assumption that's incorrect (API format, file location,\n"
            "  data schema)? Use the network to verify.\n\n"
            "Write your new approach in <analysis> tags, then write a new script from scratch."
        )
    else:
        return (
            "Your previous script failed. Here is the full output:\n\n"
            "Before writing the fix, diagnose the root cause:\n"
            "- Read the error message carefully. What specific line/operation failed?\n"
            "- Is this a missing dependency, a wrong file path, a network issue, a logic error,\n"
            "  or a data format mismatch?\n"
            "- What is the minimal change needed to fix it?\n\n"
            "Write your diagnosis in <analysis> tags, then write the corrected script."
        )


def _llm_retry_guidance(task: str, attempt: int, code_section: str,
                        previous_error: str,
                        attempt_history: list[dict] | None) -> str | None:
    try:
        history_section = ""
        if attempt_history:
            lines = []
            for entry in attempt_history:
                a = entry.get("attempt", "?")
                err = entry.get("error", "")
                lines.append(f"Attempt {a}: {err}")
            history_section = (
                "<attempt_history>\n" + "\n".join(lines) + "\n</attempt_history>"
            )

        prompt = RETRY_STRATEGY_PROMPT.format(
            task=task,
            attempt=attempt,
            max_retries=MAX_RETRIES,
            code_section=code_section or "(no code)",
            error_output=previous_error,
            history_section=history_section,
        )

        client = get_llm_client(role="planner")
        response, _usage = client.generate(prompt)
        _track_usage(_usage, model=client.model)

        guidance = response.strip()
        if not guidance or len(guidance) < 10:
            return None
        return (
            guidance
            + "\n\nWrite your analysis in <analysis> tags, then write the corrected script."
        )
    except Exception:
        logger.debug(
            "LLM retry guidance failed, using hardcoded fallback", exc_info=True,
        )
        return None


# Phase 6.2: Marker appended by ``architect.spec_generator.build_task_from_spec``
# when prior-step context is concatenated onto the immutable step description.
# Splitting on this marker recovers just the Architect's directive.
_TASK_CONTEXT_MARKER = "\n\nContext from previous steps:"

# Phase 6.4: ANSI escape sequence pattern. Strips terminal color/cursor codes
# from sandbox output before injecting it into the retry_clean ``<error>``
# section so the LLM does not waste tokens parsing escape sequences.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

# Phase 6.4: Maximum number of stdout lines retained in the retry_clean
# ``<error>`` section. stderr is included verbatim; stdout is tail-truncated
# because successful sandbox runs can produce arbitrarily long traces.
_RETRY_CLEAN_STDOUT_TAIL_LINES = 50


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from *text*."""
    if not text:
        return ""
    return _ANSI_ESCAPE_RE.sub("", text)


def _tail_lines(text: str, max_lines: int) -> str:
    """Return the last *max_lines* lines of *text*, right-stripped."""
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text.rstrip()
    return "\n".join(lines[-max_lines:]).rstrip()


def _build_error_section_body(previous_stderr: str | None,
                              previous_stdout: str | None,
                              previous_error: str | None) -> str:
    """Assemble the body of the retry_clean ``<error>`` section.

    Phase 6.4: the section contains ONLY the prior attempt's
    ``result["stderr"]`` and the last ``_RETRY_CLEAN_STDOUT_TAIL_LINES``
    lines of ``result["stdout"]``, with ANSI escape codes stripped from
    both. No attempt history, no prior code snippets, no retry guidance
    prose. ``previous_error`` (the legacy synthesized string assembled in
    the orchestrator main loop) is used only as a last-resort fallback for
    callers that have not yet been migrated to thread the structured
    sandbox output through ``build_prompt``.
    """
    parts: list[str] = []

    stderr = _strip_ansi(previous_stderr or "").strip()
    if stderr:
        parts.append(f"stderr:\n{stderr}")

    stdout = _strip_ansi(previous_stdout or "").strip()
    if stdout:
        tail = _tail_lines(stdout, _RETRY_CLEAN_STDOUT_TAIL_LINES)
        if tail:
            parts.append(
                f"stdout (last {_RETRY_CLEAN_STDOUT_TAIL_LINES} lines):\n{tail}"
            )

    if parts:
        return "\n\n".join(parts)

    legacy = _strip_ansi(previous_error or "").strip()
    if legacy:
        return legacy

    return "(no error output captured)"


def _extract_immutable_spec(task: str,
                            step_context: dict | None = None) -> str:
    """Return the Architect's immutable step description.

    Phase 6.2: the ``<spec>`` section in the ``retry_clean`` prompt is the
    single source of truth for what the worker must produce.  It is sourced
    from (in priority order):

    1. ``step_context["step_spec"]`` if a caller provides it.  Phase 6.8 will
       populate this from ``architect._build_step_context()`` so the full
       step spec (title + description + verify criteria + outputs) is
       available without re-parsing the ``UAS_TASK`` blob.
    2. The ``task`` argument, which the Orchestrator reads from the
       ``UAS_TASK`` env var (set by ``architect.executor.run_orchestrator``).
       Any prior-step context appended by ``build_task_from_spec`` is
       stripped so only the immutable directive remains.
    3. The ``UAS_TASK`` env var read directly, as a last-resort fallback for
       in-process callers that did not thread ``task`` through.
    """
    if step_context and isinstance(step_context, dict):
        spec = step_context.get("step_spec")
        if isinstance(spec, str) and spec.strip():
            return spec.strip()

    source = task or os.environ.get("UAS_TASK", "")
    if not source:
        return ""
    spec, _, _ = source.partition(_TASK_CONTEXT_MARKER)
    return spec.strip()


def _build_retry_clean_prompt(task: str,
                              previous_error: str | None,
                              workspace_files: str | None,
                              step_context: dict | None = None,
                              previous_stderr: str | None = None,
                              previous_stdout: str | None = None) -> str:
    """Build the stripped-down retry prompt for Phase 6 context pruning.

    Contains only three sections: ``<spec>`` (immutable task spec),
    ``<current_code>`` (the current state of the code on disk), and
    ``<error>`` (the failure output from the prior attempt). No
    environment scaffold, no knowledge base, no attempt history, no
    retry guidance prose.

    Phase 6.1 establishes the prompt skeleton.  Phase 6.2 grounds the
    ``<spec>`` section in the Architect's immutable step description via
    ``_extract_immutable_spec``.  Phase 6.3 grounds the ``<current_code>``
    section in the workspace filesystem by re-scanning it via
    ``scan_workspace`` at prompt-build time, so the retry sees the
    post-rollback / post-format file state — never the previously
    generated code variable, which lives only in the LLM's memory.
    Phase 6.4 grounds the ``<error>`` section in the prior attempt's raw
    ``result["stderr"]`` and the last 50 lines of ``result["stdout"]``,
    with ANSI escape codes stripped, via ``_build_error_section_body``.
    The legacy ``previous_error`` synthesized string is retained only as a
    fallback for callers that have not yet been migrated to thread the
    structured sandbox output through.
    """
    spec_text = _extract_immutable_spec(task, step_context)
    if not spec_text:
        spec_text = "(no spec available)"
    spec_section = f"<spec>\n{spec_text}\n</spec>"

    # Phase 6.3: source <current_code> from filesystem reality. Re-scan the
    # workspace at prompt-build time so the retry observes the actual file
    # contents after any rollback / format step, not a stale snapshot or the
    # LLM's prior output.
    workspace_path = config.get("workspace") or os.environ.get("WORKSPACE")
    live_workspace = ""
    if isinstance(workspace_path, str) and os.path.isdir(workspace_path):
        live_workspace = scan_workspace(workspace_path)

    if live_workspace:
        current_code_body = live_workspace
    elif workspace_files:
        current_code_body = workspace_files
    else:
        current_code_body = "(no current code state available)"
    current_code_section = f"<current_code>\n{current_code_body}\n</current_code>"

    # Phase 6.4: <error> contains only the prior attempt's stderr and the
    # last 50 lines of stdout, ANSI-stripped. No attempt history, no prior
    # code snippets, no retry guidance prose.
    error_body = _build_error_section_body(
        previous_stderr=previous_stderr,
        previous_stdout=previous_stdout,
        previous_error=previous_error,
    )
    error_section = f"<error>\n{error_body}\n</error>"

    return "\n\n".join([spec_section, current_code_section, error_section])


def build_prompt(task: str, attempt: int, previous_error: str | None = None,
                 previous_code: str | None = None,
                 environment: list[str] | None = None,
                 workspace_files: str | None = None,
                 system_state: str | None = None,
                 knowledge: dict | None = None,
                 attempt_history: list[dict] | None = None,
                 test_files: dict[str, str] | None = None,
                 step_context: dict | None = None,
                 previous_stderr: str | None = None,
                 previous_stdout: str | None = None,
                 mode: Literal["full", "retry_clean"] = "full") -> str:
    """Build the structured prompt for code generation.

    Uses XML tags with data sections (environment, task, workspace state)
    at the top and instruction sections (role, constraints, output_contract)
    at the bottom for optimal Claude response quality.

    Phase 6.1: ``mode`` selects between two prompt strategies.
    - ``"full"`` (attempt 1, default): the rich prompt that includes
      environment, knowledge, approach, workspace files, and full retry
      context. This is the historical behavior.
    - ``"retry_clean"`` (attempt 2+): a stripped-down prompt with only three
      sections — ``<spec>`` (the immutable task spec), ``<current_code>``
      (the current state of the code), and ``<error>`` (the failure output
      from the prior attempt). The LLM is given no memory of prior attempts;
      the workspace filesystem is the source of truth.

    Phase 6.2: ``step_context`` carries the Architect's structured step
    metadata (e.g. ``step_spec``).  When supplied, the ``retry_clean``
    branch uses it as the authoritative source for the ``<spec>`` section
    instead of parsing the ``UAS_TASK`` blob.

    Phase 6.4: ``previous_stderr`` and ``previous_stdout`` carry the prior
    attempt's raw sandbox output.  In ``retry_clean`` mode they become the
    sole source of the ``<error>`` section (stderr verbatim, stdout
    tail-truncated to 50 lines, both ANSI-stripped). The legacy
    ``previous_error`` synthesized string is retained as a fallback for
    callers that have not yet been migrated.
    """
    if mode == "retry_clean":
        return _build_retry_clean_prompt(
            task=task,
            previous_error=previous_error,
            workspace_files=workspace_files,
            step_context=step_context,
            previous_stderr=previous_stderr,
            previous_stdout=previous_stdout,
        )

    pkg_hint = ""
    if environment:
        pkgs = " ".join(environment)
        pkg_hint = (
            f"\nSuggested packages for this task: {pkgs}\n"
            "Install these if appropriate, but use your own judgment — add or substitute\n"
            "packages if you know a better option.\n"
        )

        # Section 17: Resolve current stable versions from PyPI for packages
        # without version pins. Prefer knowledge base versions when available.
        # Section 18: Skip in minimal mode.
        kb_versions = (knowledge or {}).get("package_versions", {})
        unpinned = [p for p in environment if "==" not in p]
        if unpinned and not MINIMAL_MODE:
            # Gather versions: knowledge base first, then live PyPI
            version_map: dict[str, str] = {}
            still_need: list[str] = []
            for pkg in unpinned:
                name = re.split(r"[><=!~]", pkg)[0].strip()
                if not name:
                    continue
                if name in kb_versions:
                    version_map[name] = kb_versions[name]
                else:
                    still_need.append(name)
            if still_need:
                live = resolve_versions(still_need)
                version_map.update(live)
            if version_map:
                version_lines = "\n".join(
                    f"- {name}=={ver}" for name, ver in sorted(version_map.items())
                )
                pkg_hint += (
                    f"\nCurrent stable versions from PyPI (use these for installation):\n"
                    f"{version_lines}\n"
                )

    system_state_block = system_state or collect_system_state()

    # Section 8: Format prior knowledge for prompt injection
    # Section 18: Skip in minimal mode.
    knowledge_block = ""
    if knowledge and not MINIMAL_MODE:
        pkg_versions = knowledge.get("package_versions", {})
        lessons = knowledge.get("lessons", [])
        if pkg_versions or lessons:
            parts = []
            if pkg_versions:
                formatted_pkgs = "\n".join(
                    f"  {pkg}=={ver}" for pkg, ver in sorted(pkg_versions.items())
                )
                parts.append(
                    f"Package versions known to work in this environment:\n{formatted_pkgs}"
                )
            if lessons:
                formatted_lessons = "\n".join(
                    f"  - [{l.get('step_title', 'unknown')}] "
                    f"Error: {l.get('error_snippet', '')} -> "
                    f"Fix: {l.get('solution_snippet', '')}"
                    for l in lessons[-10:]  # Show most recent 10
                )
                parts.append(
                    f"Lessons from previous runs:\n{formatted_lessons}"
                )
            knowledge_content = "\n\n".join(parts)
            knowledge_block = (
                f"\n<prior_knowledge>\n{knowledge_content}\n\n"
                "Use this information to avoid repeating past mistakes "
                "and to use known-good versions.\n</prior_knowledge>\n"
            )

    # Section 18: Skip <approach> section in minimal mode.
    approach_block = ""
    if not MINIMAL_MODE:
        approach_block = """
<approach>
Before writing code, reason through these questions:
1. What is the best approach for this task? Are there multiple strategies?
   Pick the most robust one.
2. What packages or tools does this require? For EACH dependency, ask: is
   there a more modern, faster, or better-maintained alternative? Every
   ecosystem evolves fast — what was standard two years ago may be obsolete.
   If you're not sure, check the relevant package registry or docs. Always
   use the latest best-in-class option for the target ecosystem.
3. Are there known pitfalls, breaking changes, or deprecations in the
   libraries you plan to use? If uncertain, check the docs.
4. If the task involves an external API or data source, what is its current
   format/schema? Don't assume — verify if possible.
5. Would any development tools improve the quality of your output? Install
   and use them if they'd catch bugs or improve code quality.

Encode your research findings directly into your code as comments or as
defensive checks. Don't produce a separate research document — just write
better code because you researched first.
</approach>
"""

    # Data sections at top (environment, task, workspace state)
    prompt = f"""\
<environment>
You are running inside an isolated, disposable container. You have FULL AUTONOMY with ALL TOOLS ENABLED:
- ROOT ACCESS. Install any system packages with apt-get. No sudo needed.
- UNRESTRICTED NETWORK. Fetch any URL, call any API, clone any repo. No firewall, no proxy.
- PACKAGE INSTALLATION. `uv` is pre-installed. Use `uv pip install --system` for fast package installation. Do it proactively at the top of your script.
- COMMAND EXECUTION. Run any shell command via subprocess. No restrictions whatsoever.
- FULL TOOL ACCESS. All tools are enabled — file operations, web search, web fetch, bash, and any other available tools or skills. Use them aggressively to research, verify, and build.
- AGENT SKILLS. Install and use any agent skills that would help. You can extend your own capabilities.
- WEB SEARCH. If you need to look something up — current library versions, API docs, best practices — you can and should use your tools and the network.
- FILESYSTEM. Full read/write. Workspace: os.environ.get("WORKSPACE", "/workspace").

This container is disposable. Nothing here affects the host. Be bold, not cautious. Use every tool at your disposal.
{pkg_hint}
System info:
{system_state_block}
</environment>
{knowledge_block}{approach_block}
<task>
{task}
</task>"""

    if workspace_files:
        prompt += f"""

<workspace_state>
Files already present in the workspace from prior steps:
{workspace_files}
Do not regenerate these files unless the task explicitly requires modifying them.
Reference them by path using os.path.join(workspace, ...).
Reuse any existing subdirectory names exactly as shown above.
</workspace_state>"""

    # Section 3: File modification guidance.
    # When the task involves modifying existing files, steer the LLM toward
    # full-file rewrites instead of fragile surgical insertions.
    if _task_mentions_file_modification(task):
        prompt += """

<file_modification_guidance>
When modifying existing files:
1. Read the entire file first to understand its structure
2. Write the COMPLETE modified file, not just the diff or insertion
3. Use a write-then-verify pattern: write the file, then compile-check it
4. Never use string insertion by line number — it's fragile
</file_modification_guidance>"""

    # Phase 4.4: TDD constraint injection — include test file content and
    # require pytest validation when a preceding test step produced test files.
    if test_files and config.get("tdd_enforce"):
        tdd_parts = []
        for tpath, tcontent in sorted(test_files.items()):
            tdd_parts.append(f"<test_file path=\"{tpath}\">\n{tcontent}\n</test_file>")
        test_file_list = " ".join(test_files.keys())
        tdd_block = "\n".join(tdd_parts)
        prompt += f"""

<tdd_constraint>
A preceding test step has already written the following test files for this task.
Your implementation MUST make all of these tests pass.

{tdd_block}

MANDATORY: After writing your implementation, run `pytest {test_file_list} --tb=short -q`
as your final validation. All tests in the above files must pass. If any test fails,
fix your implementation until they pass. Do NOT modify the test files.
</tdd_constraint>"""

    # Section 19: Truncation-aware code length guidance.
    # When prior attempts for this step produced code that was truncated,
    # instruct the LLM to produce more concise output.
    if config.get("truncation_detected"):
        prompt += """

<code_length_warning>
CRITICAL: Previous attempts to generate code for this task were TRUNCATED because
the script was too long. You MUST keep your script concise to avoid truncation:
- Use helper functions to avoid repeating similar code patterns.
- Minimize inline comments — only comment non-obvious logic.
- Use compact data structures (dicts, lists) instead of verbose if/elif chains.
- Prefer library functions over manual implementations.
- Save intermediate artifacts (models, data) to disk files in the workspace so
  that separate scripts can load and reuse them if needed.
- Aim for under 300 lines of code. If you cannot fit everything, prioritize
  correctness of the core logic over extra features or verbose output formatting.
</code_length_warning>"""

    # Instruction sections at bottom (role, constraints, output_contract)
    prompt += """

<role>
You are an expert engineer with full system access in a disposable container.
You have ALL tools enabled and should use them aggressively:

- USE TOOLS FREELY. You have full tool access — file read/write, web search,
  web fetch, bash execution, and any other available tools or skills. Use them
  whenever they would help you produce a better result.
- RESEARCH FIRST. Before writing code, use your tools to check current library
  versions, read API docs, verify best practices. Don't guess — look it up.
- INSTALL ANYTHING. Use apt-get for system packages, uv/pip for Python packages.
  Install linters, formatters, dev tools — whatever improves quality.
- MODIFY YOUR ENVIRONMENT. Configure your workspace, install editor plugins or
  agent skills, set up whatever tooling you need. This container is disposable.
- ACCESS THE NETWORK. Fetch URLs, call APIs, clone repos, download datasets.
  No restrictions. Use the network proactively to verify assumptions.

Act like a senior engineer with root access who sets up their own environment
and uses every tool at their disposal before starting work.

After using tools to research and prepare, generate a complete, self-contained
Python script in a ```python code fence. The script must be complete and
self-contained.
</role>

<constraints>
- Exit with code 0 on success, non-zero on failure.
- Print results to stdout, errors to stderr.
- Do not use input() or any interactive prompts.
- Wrap network requests in retries with exponential backoff.
- Always use os.path.join(workspace, ...) for file paths.
- Check if files exist before reading them.
- Use HTTPS for all URLs -- never use plain http://.
- Never hardcode secrets or API keys -- use os.environ.get().
- Use subprocess.run() with list args -- never shell=True.
- Do not use eval(), exec(), or pickle.loads() on untrusted data.
- Catch specific exceptions -- never use bare except:.
- Use context managers (with statements) for file I/O.
- Specify encoding="utf-8" when opening text files.
- Do NOT run git init or any git commands -- version control is managed by the framework.
- Pin dependency versions.
- The workspace IS the project root. Write files directly to os.path.join(workspace, ...).
  Do NOT create a project subdirectory (e.g., os.path.join(workspace, "myproject", "main.py")).
- When the workspace already contains subdirectories (e.g., "outputs/", "data/", "models/"),
  reuse those exact names. NEVER create synonyms like "output/" vs "outputs/" vs "results/".
  Check the workspace listing and match existing directory names exactly.
</constraints>

<output_contract>
YOUR SCRIPT MUST PRODUCE THIS OUTPUT. This is not optional.

At the end of your script, print a result summary as the last line of stdout:

    import json
    result = {
        "status": "ok",
        "files_written": ["list", "of", "files", "you", "created"],
        "summary": "One sentence describing what was accomplished"
    }
    print(f"UAS_RESULT: {json.dumps(result)}")

If your script encounters an unrecoverable error:

    import json
    result = {"status": "error", "error": "What went wrong and why"}
    print(f"UAS_RESULT: {json.dumps(result)}")
    sys.exit(1)

The calling system parses this line to determine success or failure.
If you don't print UAS_RESULT, the system cannot tell if you succeeded.
</output_contract>"""

    # Section 11: Include full attempt history so the LLM sees all prior
    # attempts and avoids repeating failed approaches.
    if attempt_history:
        history_lines = [
            f"You have tried {len(attempt_history)} time(s). "
            "Here is what happened:"
        ]
        for entry in attempt_history:
            a = entry.get("attempt", "?")
            err = entry.get("error", "")
            snippet = entry.get("code_snippet", "")
            history_lines.append(f"\nAttempt {a}: {err}")
            if snippet:
                history_lines.append(f"Code approach: {snippet}")
        history_lines.append(
            "\nDo NOT repeat any of these approaches. Each new attempt must be "
            "fundamentally different from all previous ones."
        )
        prompt += "\n\n<attempt_history>\n"
        prompt += "\n".join(history_lines)
        prompt += "\n</attempt_history>"

    if previous_error and attempt > 1:
        code_section = ""
        if previous_code:
            code_section = (
                f"\nThe script that failed:\n```python\n{previous_code}\n```\n"
            )

        guidance = None
        if not MINIMAL_MODE:
            guidance = _llm_retry_guidance(
                task, attempt, code_section, previous_error, attempt_history,
            )
        if guidance is None:
            guidance = _hardcoded_retry_guidance(
                attempt, code_section, previous_error,
            )

        prompt += f"""

<previous_error attempt="{attempt - 1}">
{code_section}
```
{previous_error}
```

{guidance}
</previous_error>"""

    return prompt


# Section 7a: Prompt variation hints for best-of-N code generation.
_APPROACH_HINTS = [
    "",  # Approach A: no hint (original prompt)
    (
        "\n\n<approach_hint>"
        "Prioritize robustness: add thorough input validation, comprehensive "
        "error handling, and defensive checks throughout."
        "</approach_hint>"
    ),
    (
        "\n\n<approach_hint>"
        "Prioritize simplicity and efficiency: use the most direct approach, "
        "minimize dependencies, and prefer standard-library solutions."
        "</approach_hint>"
    ),
]


def _get_best_of_n(attempt: int) -> int:
    """Return the number of parallel samples to generate for this attempt.

    Section 7c: Budget-aware gating.
    - Attempt 1 is always single-sample (N=1).
    - On retries, N scales with attempt count, capped by UAS_BEST_OF_N.
    - If UAS_BEST_OF_N is unset or 1, best-of-N is disabled entirely.
    """
    max_n = config.get("best_of_n")
    if max_n <= 1 or attempt <= 1:
        return 1
    # attempt 2 → N=2, attempt 3 → N=3, capped by max_n
    return min(attempt, max_n)


BEST_OF_N_PROMPT = """\
You are advising a code generation system on how many alternative solutions to generate for a retry.

<task>
{task}
</task>

<error>
{error}
</error>

<attempt>{attempt}</attempt>

Should the system generate 1, 2, or 3 alternative solutions for this retry?
Consider:
- If the error suggests a clear, obvious fix (e.g. typo, missing import, wrong variable name), N=1 suffices.
- If the error is ambiguous and multiple approaches could work, N=2 helps.
- If the error is complex or fundamental (e.g. wrong algorithm, architectural issue), N=3 gives the best chance.

Return ONLY a JSON object (no other text):
{{"n": 1}}

n must be 1, 2, or 3."""


def _get_best_of_n_llm(attempt: int, task: str, previous_error: str) -> int:
    max_n = config.get("best_of_n")
    if max_n <= 1 or attempt <= 1:
        return 1

    try:
        from architect.events import EventType, get_event_log

        prompt = BEST_OF_N_PROMPT.format(
            task=task,
            error=previous_error,
            attempt=attempt,
        )

        event_log = get_event_log()
        event_log.emit(EventType.LLM_CALL_START, data={"purpose": "best_of_n_budget"})
        client = get_llm_client(role="planner")
        response, _usage = client.generate(prompt)
        _track_usage(_usage, model=client.model)
        event_log.emit(EventType.LLM_CALL_COMPLETE, data={"purpose": "best_of_n_budget"})

        text = response.strip()
        fence_match = re.search(
            r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL,
        )
        if fence_match:
            text = fence_match.group(1)
        else:
            brace_match = re.search(r"\{.*\}", text, re.DOTALL)
            if brace_match:
                text = brace_match.group(0)

        data = json.loads(text)
        recommended = int(data.get("n", attempt))
        recommended = max(1, min(recommended, 3))
        return min(recommended, max_n)
    except Exception:
        logger.debug("LLM best-of-N budget failed, using linear formula", exc_info=True)

    return _get_best_of_n(attempt)


SCORE_GUIDANCE_PROMPT = """\
Given this task, what are the most important success signals?

<task>
{task}
</task>

Return ONLY a JSON object (no other text):
{{"priorities": ["files", "stdout_content", "exit_code"]}}

priorities must be a list ordered from most to least important, using these signal names:
- "files": the script creates output files
- "stdout_content": the script prints meaningful results to stdout
- "exit_code": the script exits successfully (code 0)

Order them by what matters most for THIS specific task."""

_score_guidance_cache: dict[str, list[str]] = {}


def _get_score_priorities(task: str) -> list[str] | None:
    if task in _score_guidance_cache:
        return _score_guidance_cache[task]

    try:
        from architect.events import EventType, get_event_log

        prompt = SCORE_GUIDANCE_PROMPT.format(task=task)

        event_log = get_event_log()
        event_log.emit(EventType.LLM_CALL_START, data={"purpose": "score_guidance"})
        client = get_llm_client(role="planner")
        response, _usage = client.generate(prompt)
        _track_usage(_usage, model=client.model)
        event_log.emit(EventType.LLM_CALL_COMPLETE, data={"purpose": "score_guidance"})

        text = response.strip()
        fence_match = re.search(
            r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL,
        )
        if fence_match:
            text = fence_match.group(1)
        else:
            brace_match = re.search(r"\{.*\}", text, re.DOTALL)
            if brace_match:
                text = brace_match.group(0)

        data = json.loads(text)
        priorities = data.get("priorities", [])
        valid_signals = {"files", "stdout_content", "exit_code"}
        priorities = [p for p in priorities if p in valid_signals]
        if priorities:
            _score_guidance_cache[task] = priorities
            return priorities
    except Exception:
        logger.debug("LLM score guidance failed, using static scoring", exc_info=True)

    return None


def score_result(result: dict, task: str | None = None) -> int:
    """Score a sandbox execution result for selection among candidates.

    Section 7b: Prefer successful runs, then richer UAS_RESULT output.
    When *task* is provided and not in MINIMAL_MODE, uses LLM guidance to
    weight scoring priorities for the specific task type.
    Returns an integer score (higher is better).
    """
    score = 0
    if result["exit_code"] == 0:
        score += 1000

    uas = parse_uas_result(result.get("stdout", ""))
    if uas:
        score += 100  # has any UAS_RESULT
        if uas.files_written:
            score += 50 + len(uas.files_written) * 10
        if uas.summary:
            score += 50
        if uas.status == "ok":
            score += 50

    # Prefer runs with more stdout (more informative)
    stdout_len = len(result.get("stdout", ""))
    score += min(stdout_len // 100, 50)

    if task is not None and not MINIMAL_MODE:
        priorities = _get_score_priorities(task)
        if priorities:
            bonus_weights = {priorities[i]: 3 - i for i in range(len(priorities))}
            files_bonus = 0
            if uas and uas.files_written:
                files_bonus = len(uas.files_written) * 20
            stdout_bonus = min(stdout_len // 50, 100)
            exit_bonus = 100 if result["exit_code"] == 0 else 0

            signal_scores = {
                "files": files_bonus,
                "stdout_content": stdout_bonus,
                "exit_code": exit_bonus,
            }
            for signal, weight in bonus_weights.items():
                score += signal_scores.get(signal, 0) * weight

    return score


CODE_EVALUATION_PROMPT = """\
You are evaluating multiple code solutions for the same programming task.
Each candidate was executed in a sandbox. Select the best one.

<task>
{task}
</task>

{candidates_section}

Rank ALL candidates from best to worst. Consider:
1. Correctness: Did the code complete the task? (exit code 0 is critical)
2. Output quality: Does it produce a valid UAS_RESULT with status, files, and summary?
3. Robustness: Does the code handle errors and follow best practices?
4. Approach: Is the solution well-structured?

Return ONLY a JSON object (no other text):
{{"ranking": [<candidate indices from best to worst>], "reasoning": "brief explanation"}}"""


def evaluate_candidates(
    client, task: str, candidates: list[tuple[str, dict, int]],
) -> list[tuple[str, dict, int]]:
    """Use LLM to evaluate and rank code generation candidates.

    Args:
        client: LLM client for making API calls.
        task: The original task description.
        candidates: List of (code, result, idx) tuples.

    Returns:
        List of (code, result, idx) sorted best-first.
        Falls back to score_result() ranking on failure.
    """
    if len(candidates) < 2:
        return list(candidates)

    sections = []
    for code, result, idx in candidates:
        exit_code = result.get("exit_code", -1)
        stdout = result.get("stdout", "") or ""
        stderr = result.get("stderr", "") or ""

        sections.append(
            f"<candidate index=\"{idx}\">\n"
            f"Exit code: {exit_code}\n"
            f"Code:\n```python\n{code or ''}\n```\n"
            f"Stdout:\n```\n{stdout}\n```\n"
            f"Stderr:\n```\n{stderr}\n```\n"
            f"</candidate>"
        )

    candidates_section = "\n\n".join(sections)
    prompt = CODE_EVALUATION_PROMPT.format(
        task=task,
        candidates_section=candidates_section,
    )

    try:
        response, _usage = client.generate(prompt)
        _track_usage(_usage, model=client.model)

        # Parse JSON from response (may be wrapped in code fences)
        text = response.strip()
        fence_match = re.search(
            r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL,
        )
        if fence_match:
            text = fence_match.group(1)
        else:
            brace_match = re.search(r"\{.*\}", text, re.DOTALL)
            if brace_match:
                text = brace_match.group(0)

        data = json.loads(text)
        ranking = data.get("ranking", [])
        if ranking and isinstance(ranking, list):
            idx_map = {idx: (code, result, idx)
                       for code, result, idx in candidates}
            ranked: list[tuple[str, dict, int]] = []
            for r_idx in ranking:
                r_idx_int = int(r_idx)
                if r_idx_int in idx_map:
                    ranked.append(idx_map.pop(r_idx_int))
            # Append any candidates not mentioned in ranking
            ranked.extend(idx_map.values())
            if ranked:
                logger.info(
                    "LLM evaluation selected candidate %d. Reason: %s",
                    ranked[0][2],
                    data.get("reasoning", "N/A")[:200],
                )
                return ranked
    except Exception as exc:
        logger.warning(
            "LLM candidate evaluation failed, falling back to score_result: %s",
            exc,
        )

    # Fallback: use score_result heuristic
    return sorted(candidates, key=lambda x: score_result(x[1], task=task), reverse=True)


def _generate_one(client, prompt: str, hint: str):
    """Generate code from a single prompt variant and execute it.

    Returns (code, sandbox_result) or (None, None) on extraction failure.
    """
    full_prompt = prompt + hint if hint else prompt
    response, _usage = client.generate(full_prompt)
    _track_usage(_usage, model=client.model)
    code = extract_code(response)
    if not code:
        # Attempt truncation recovery before giving up.
        truncated = extract_truncated_block(response)
        if truncated:
            logger.warning(
                "Detected truncated code in parallel sample (%d lines), "
                "requesting continuation...", len(truncated.splitlines()),
            )
            code = _request_continuation(client, truncated)
    if not code:
        return None, None
    result = run_in_sandbox(code)
    return code, result


def generate_and_vote(client, prompt: str, n: int,
                      task: str | None = None) -> tuple[str | None, dict | None]:
    """Generate N code samples in parallel, execute each, and pick the best.

    Section 7a/7b: Parallel code generation with execution-based selection.
    When *task* is provided and there are 2+ valid candidates, uses LLM-based
    evaluation instead of the heuristic ``score_result()`` scorer.

    Returns (best_code, best_result). If all extractions fail, returns
    (None, None).
    """
    hints = [_APPROACH_HINTS[i % len(_APPROACH_HINTS)] for i in range(n)]

    logger.info("Best-of-N: generating %d samples in parallel...", n)
    candidates: list[tuple[str | None, dict | None, int]] = []

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = {
            pool.submit(_generate_one, client, prompt, hint): i
            for i, hint in enumerate(hints)
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                code, result = future.result()
            except Exception as exc:
                logger.warning("Best-of-N sample %d failed: %s", idx, exc)
                code, result = None, None
            candidates.append((code, result, idx))

    # Filter out extraction failures
    valid = [(code, result, idx) for code, result, idx in candidates
             if code is not None and result is not None]

    if not valid:
        logger.warning("Best-of-N: all %d samples failed code extraction.", n)
        return None, None

    # Select best candidate: LLM evaluation when task is available, else heuristic
    if task and len(valid) >= 2:
        ranked = evaluate_candidates(client, task, valid)
        best_code, best_result, best_idx = ranked[0]
    else:
        scored = [(code, result, idx, score_result(result, task=task))
                  for code, result, idx in valid]
        scored.sort(key=lambda x: x[3], reverse=True)
        best_code, best_result, best_idx = scored[0][0], scored[0][1], scored[0][2]

    successes = sum(1 for _, r, _ in valid if r["exit_code"] == 0)
    logger.info(
        "Best-of-N: %d/%d succeeded, selected sample %d.",
        successes, len(valid), best_idx,
    )
    return best_code, best_result


def _record_code_version(step_id, spec_attempt, orch_attempt, code, prompt,
                         exit_code=-1, error_summary=""):
    """Record a code version to disk for the architect's code tracker."""
    workspace = config.get("workspace")
    run_id = config.get("run_id")
    if run_id:
        versions_dir = os.path.join(workspace, ".uas_state", "runs", run_id,
                                    "code_versions")
    else:
        versions_dir = os.path.join(workspace, ".uas_state", "code_versions")
    try:
        os.makedirs(versions_dir, exist_ok=True)
    except OSError:
        return

    version = {
        "step_id": step_id,
        "spec_attempt": spec_attempt,
        "orch_attempt": orch_attempt,
        "code": code,
        "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest()[:16],
        "exit_code": exit_code,
        "error_summary": error_summary[:200],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    path = os.path.join(versions_dir, f"{step_id}.json")
    existing = []
    if os.path.exists(path):
        try:
            with open(path) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    existing.append(version)
    try:
        with open(path, "w") as f:
            json.dump(existing, f, indent=2)
    except OSError:
        pass


def main():
    args = parse_args()
    verbose = args.verbose or config.get("verbose")
    configure_logging(verbose)

    task = get_task(args)
    if not task:
        logger.error("No task provided.")
        sys.exit(1)

    # Read step context for code tracking
    _step_id_str = config.get("step_id")
    _spec_attempt = config.get("spec_attempt")
    _step_id = int(_step_id_str) if _step_id_str else None

    logger.info("Task: %s", task)

    logger.info("Verifying sandbox...")
    verify = run_in_sandbox("print('sandbox OK')")
    if verify["exit_code"] != 0:
        logger.error(
            "Sandbox verification failed:\n%s", verify["stderr"]
        )
        sys.exit(1)
    logger.info("Sandbox verified.")

    # Section 7: Collect system state once and cache for all attempts.
    system_state = collect_system_state()

    # Section 8: Load cross-run knowledge base at startup.
    # Section 18: Skip in minimal mode.
    knowledge = None
    if not MINIMAL_MODE:
        try:
            from architect.state import read_knowledge_base
            kb = read_knowledge_base()
            if kb.get("package_versions") or kb.get("lessons"):
                knowledge = kb
        except Exception:
            pass

    # Section 5c: Use coder-specific model for code generation
    client = get_llm_client(role="coder")
    previous_error = None
    previous_code = None
    # Section 11: Accumulate full attempt history across retries.
    attempt_history: list[dict] = []
    workspace_files = config.get("workspace_files")

    # Read step's package requirements from the architect
    environment = None
    env_str = config.get("step_environment")
    if env_str:
        try:
            environment = json.loads(env_str)
            if not isinstance(environment, list):
                environment = None
        except (json.JSONDecodeError, ValueError):
            pass

    # Phase 4.4: Read test file content passed by the architect for TDD.
    test_files: dict[str, str] | None = None
    if config.get("tdd_enforce"):
        test_files_str = config.get("test_files")
        if test_files_str:
            try:
                parsed = json.loads(test_files_str)
                if isinstance(parsed, dict):
                    test_files = parsed
            except (json.JSONDecodeError, ValueError):
                pass

    # Section 5b: If workspace files aren't provided by the architect,
    # scan the workspace directly so the LLM knows what already exists.
    if not workspace_files:
        workspace_path = config.get("workspace") or os.environ.get("WORKSPACE")
        if workspace_path:
            workspace_files = scan_workspace(workspace_path) or None

    # Resolve workspace path once for git branch management.
    _workspace = config.get("workspace") or os.environ.get("WORKSPACE")

    for attempt in range(1, MAX_RETRIES + 1):
        logger.info("\n--- Attempt %d/%d ---", attempt, MAX_RETRIES)

        # Phase 3.3: Each attempt starts on its own git branch forked from
        # the last uas-wip checkpoint.
        _attempt_branch = ""
        if _workspace and _step_id is not None:
            _attempt_branch = create_attempt_branch(
                _workspace, _step_id, attempt,
            )
            if _attempt_branch:
                logger.info("On branch %s", _attempt_branch)

        prompt = build_prompt(task, attempt, previous_error, previous_code,
                              environment=environment,
                              workspace_files=workspace_files,
                              system_state=system_state,
                              knowledge=knowledge,
                              attempt_history=attempt_history or None,
                              test_files=test_files)

        # Section 7c: Determine N for this attempt (budget-aware gating).
        if not MINIMAL_MODE and previous_error:
            n = _get_best_of_n_llm(attempt, task, previous_error)
        else:
            n = _get_best_of_n(attempt)

        if n > 1:
            # Section 7a/7b: Parallel best-of-N generation + execution voting.
            code, result = generate_and_vote(client, prompt, n, task=task)
            if code is None:
                previous_error = "Failed to extract code block from LLM response."
                previous_code = None
                logger.error("%s", previous_error)
                attempt_history.append({
                    "attempt": attempt,
                    "error": previous_error,
                    "code_snippet": "",
                })
                continue
        else:
            # Standard single-sample path.
            logger.info("Querying LLM...")
            try:
                response, _usage = client.generate(prompt)
                _track_usage(_usage, model=client.model)
            except RuntimeError as exc:
                previous_error = str(exc)
                previous_code = None
                logger.error("LLM generation failed: %s", exc)
                attempt_history.append({
                    "attempt": attempt,
                    "error": previous_error,
                    "code_snippet": "",
                })
                continue

            code = extract_code(response)
            if not code:
                # Check for truncation before giving up — if the LLM
                # produced a ```python block that was cut off, request
                # a continuation rather than wasting an attempt.
                truncated = extract_truncated_block(response)
                if truncated:
                    logger.warning(
                        "Detected truncated code block (%d lines), "
                        "requesting continuation...", len(truncated.splitlines()),
                    )
                    code = _request_continuation(client, truncated)

            if not code:
                if _contains_tool_calls(response):
                    previous_error = (
                        "Your response contained tool calls (e.g. <tool_call> XML) "
                        "but tools are disabled. You MUST respond with a single "
                        "```python code fence containing your complete script. "
                        "Do not use tool calls."
                    )
                else:
                    previous_error = "Failed to extract code block from LLM response."
                previous_code = None
                logger.error("%s", previous_error)
                logger.debug("Raw LLM response (%d chars):\n%s",
                             len(response), response[:2000])
                attempt_history.append({
                    "attempt": attempt,
                    "error": previous_error,
                    "code_snippet": "",
                })
                continue

            logger.debug("Generated code (%d chars):\n---\n%s\n---",
                         len(code), code)

            # Section 9: Pre-execution sanity checks
            if not MINIMAL_MODE:
                critical_errors, warnings = pre_execution_check_llm(code, task)
            else:
                critical_errors, warnings = pre_execution_check(code, task)
            for w in warnings:
                logger.warning("Pre-execution warning: %s", w)
            if critical_errors:
                previous_code = code
                previous_error = (
                    "Your code was not executed because it has a fatal issue:\n"
                    + "\n".join(critical_errors)
                    + "\nFix this issue and regenerate."
                )
                logger.error("Pre-execution check failed: %s",
                             "; ".join(critical_errors))
                continue

            logger.info("Executing in sandbox...")
            result = run_in_sandbox(code)

        # Record code version for tracking
        if _step_id is not None:
            cv_error = ""
            if result["exit_code"] != 0:
                cv_error = (result["stderr"] or result["stdout"]
                            or "Non-zero exit code")
            _record_code_version(
                _step_id, _spec_attempt, attempt - 1, code, prompt,
                exit_code=result["exit_code"], error_summary=cv_error,
            )

        logger.info("Exit code: %s", result["exit_code"])
        # Section 5d: Emit delimited stdout/stderr blocks for reliable
        # parsing by the architect's executor.
        if result["stdout"]:
            logger.info("%s\n%s\n%s", STDOUT_START, result["stdout"], STDOUT_END)
        if result["stderr"]:
            logger.info("%s\n%s\n%s", STDERR_START, result["stderr"], STDERR_END)

        # Phase 5.4: Lint pre-check — if the linter finds fatal errors
        # (e.g. undefined names), short-circuit with revert_needed=True
        # without burning an LLM call on evaluate_sandbox.
        lint_errors: list[str] = []
        if _workspace:
            lint_errors = lint_workspace(_workspace)

        if lint_errors:
            logger.warning("Lint pre-check found %d fatal error(s):", len(lint_errors))
            for err in lint_errors[:10]:
                logger.warning("  %s", err)
            exec_result = ExecutionResult(
                success=False,
                revert_needed=True,
                error_category="lint_fatal",
                summary=f"Linter found {len(lint_errors)} fatal error(s): {lint_errors[0]}",
            )
        else:
            # Evaluate sandbox outcome via structured ExecutionResult.
            try:
                exec_result = evaluate_sandbox(
                    stdout=result["stdout"] or "",
                    stderr=result["stderr"] or "",
                    exit_code=result["exit_code"],
                )
            except Exception:
                logger.debug("evaluate_sandbox fuzzy call failed, using exit-code fallback",
                             exc_info=True)
                _success = result["exit_code"] == 0
                exec_result = ExecutionResult(
                    success=_success,
                    revert_needed=not _success,
                    error_category=None if _success else "unknown",
                    summary="exit code 0" if _success else f"exit code {result['exit_code']}",
                )
        logger.info("ExecutionResult: %s", exec_result.model_dump_json())

        if exec_result.success:
            # Phase 4.5: Binary pytest gate — when test files are present,
            # run pytest as the authoritative success criterion.
            if test_files:
                logger.info("Running pytest gate on %d test file(s)...",
                            len(test_files))
                pytest_result = run_pytest_in_sandbox(list(test_files.keys()))
                logger.info("Pytest exit code: %s", pytest_result["exit_code"])
                if pytest_result["stdout"]:
                    logger.info("Pytest stdout:\n%s", pytest_result["stdout"])
                if pytest_result["stderr"]:
                    logger.info("Pytest stderr:\n%s", pytest_result["stderr"])
                if pytest_result["exit_code"] != 0:
                    pytest_output = (
                        (pytest_result["stdout"] or "")
                        + "\n"
                        + (pytest_result["stderr"] or "")
                    ).strip()
                    previous_code = code
                    previous_error = (
                        "Your code ran successfully but the pytest test suite FAILED.\n"
                        f"pytest output:\n{pytest_output}\n\n"
                        "Fix your implementation to make all tests pass. "
                        "Do NOT modify the test files."
                    )
                    attempt_history.append({
                        "attempt": attempt,
                        "error": previous_error,
                        "code_snippet": code or "",
                        "revert_needed": False,
                    })
                    logger.error("Pytest gate FAILED on attempt %d.", attempt)
                    continue
                logger.info("Pytest gate PASSED.")

            uas_result = parse_uas_result(result["stdout"] or "")
            if uas_result:
                logger.info("UAS_RESULT: %s", uas_result.model_dump_json())

            # Phase 5.2: Format all files listed in UAS_RESULT before exiting.
            if uas_result and uas_result.files_written and _workspace:
                format_workspace(_workspace, uas_result.files_written)

            logger.info("\nSUCCESS on attempt %d.", attempt)
            import json as _json
            logger.info("__UAS_ORCH_USAGE__:%s", _json.dumps(_orch_usage))
            sys.exit(0)

        previous_code = code
        previous_error = exec_result.summary or (
            result["stderr"] or result["stdout"] or "Non-zero exit code"
        )
        if exec_result.error_category:
            previous_error = (
                f"[{exec_result.error_category}] {previous_error}"
            )
        # Section 11: Accumulate attempt history for retry context.
        attempt_history.append({
            "attempt": attempt,
            "error": previous_error,
            "code_snippet": code or "",
            "revert_needed": exec_result.revert_needed,
        })
        logger.error("FAILED on attempt %d.", attempt)

        # Phase 3.4: Roll back the workspace to the last uas-wip checkpoint
        # so the next attempt starts from a clean filesystem state.
        if exec_result.revert_needed and _workspace and _step_id is not None:
            rollback_to_checkpoint(_workspace, _step_id)
            logger.info("Rolled back workspace to uas-wip checkpoint.")

    logger.error("FAILED after %d attempts.", MAX_RETRIES)
    import json as _json
    logger.info("__UAS_ORCH_USAGE__:%s", _json.dumps(_orch_usage))
    sys.exit(1)


if __name__ == "__main__":
    main()
