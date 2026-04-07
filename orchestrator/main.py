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

from architect.git_state import (
    changed_py_files_since_uas_wip,
    create_attempt_branch,
    rollback_to_checkpoint,
)

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


_TOOL_BYPASS_PATTERNS = (
    # Shell / bash code fences — the LLM responded with shell instructions
    # instead of a Python script.
    re.compile(r"```(?:bash|sh|shell|zsh|console|terminal)\b", re.IGNORECASE),
    # Tool-use markup that occasionally leaks into the JSON result field
    # (different claude-code versions show tool invocations differently).
    re.compile(r"<tool_use\b", re.IGNORECASE),
    re.compile(r"<tool_call\b", re.IGNORECASE),
    re.compile(r"<tool_name\b", re.IGNORECASE),
    re.compile(r"\bTool:\s*Bash\b"),
    # First-person prose that means the LLM did the task with tools and is
    # reporting completion instead of emitting a script.  These are
    # restricted to the start-of-line / start-of-sentence position so they
    # don't false-positive on a Python script that happens to mention
    # "I created a list" in a docstring.
    re.compile(
        r"(?:^|\n)\s*(?:I(?:'ve|'ll| have| will)?\s+)?"
        r"(?:created|wrote|written|generated|installed|added|set\s+up|"
        r"set-up|setup)\s+(?:the\s+|a\s+|an\s+|all\s+|the\s+following\s+|"
        r"these\s+)?(?:files?|packages?|dependencies|deps|directory|"
        r"directories|venv|virtual\s*env(?:ironment)?s?|environments?|"
        r"project|skeleton|tests?|module|modules)\b",
        re.IGNORECASE,
    ),
)


def _contains_tool_calls(response: str) -> bool:
    """Detect when an LLM response was produced via tools instead of code.

    Section 4 of PLAN.md: even with ``--allowed-tools`` restricted to
    read-only research tools, an LLM may still respond with prose that
    describes work it *thought* it did with tools (e.g. "I've created the
    files...") instead of emitting a fenced Python code block.  This
    function flags such responses so the orchestrator can surface a
    diagnostic error to the user and to the LLM on the next attempt.

    The patterns deliberately err on the side of caution: this is only
    consulted in the failure path (after ``extract_code`` returned None),
    so a false positive does not cause a successful response to be
    rejected.  It just selects a more informative ``previous_error`` that
    tells the LLM what went wrong on the last attempt.
    """
    if not response:
        return False
    for pat in _TOOL_BYPASS_PATTERNS:
        if pat.search(response):
            return True
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

    # Section 4 (PLAN.md): the retry_clean prompt is intentionally lean,
    # but it MUST still tell the LLM about the output contract — without
    # this, the LLM falls back on its training prior of writing prose or
    # bash scripts and the parser's "Failed to extract code block" failure
    # repeats forever across all retry attempts.  This is the same
    # contradiction Section 4 fixed in build_prompt() above; the
    # retry_clean path needed the same treatment.
    output_format_section = (
        "<output_format>\n"
        "Respond with a single ```python fenced code block containing a "
        "complete, self-contained Python script that fulfils the <spec>. "
        "The orchestrator extracts that fenced block and runs it as a "
        "Python script in the real workspace.\n\n"
        "Do NOT respond with bash, shell, or sh code blocks — only "
        "```python is extracted. Do NOT respond with prose describing "
        "what you would do — emit the script directly. Do NOT split the "
        "script across multiple fences.\n\n"
        "Tools available to you right now (read-only research): Read, "
        "Grep, Glob, WebSearch, WebFetch.  Tools DISABLED: Write, Edit, "
        "NotebookEdit, Bash, Task.  Files you create with tools live in "
        "a throwaway temp directory and are discarded — they do not "
        "count toward task completion.  Only the ```python block in your "
        "text response counts.\n"
        "</output_format>"
    )

    return "\n\n".join([
        output_format_section,
        spec_section,
        current_code_section,
        error_section,
    ])


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
<output_format>
CRITICAL: This generation step is TEXT-only. The orchestrator extracts a
single ```python fenced code block from your text response and runs that
script later inside the real workspace directory. Anything you do via
tools right now is DISCARDED.

You are running in a throwaway temporary directory that is deleted the
moment your response completes. Files you create with tools — including
via Read/Grep/Glob — live ONLY in that temp dir and are NOT visible to
the orchestrator. Tool side effects do NOT count toward task completion.

The Write, Edit, NotebookEdit, Bash, and Task tools are DISABLED in this
session and will fail if you try to call them. Do NOT respond with bash
or shell code blocks — only ```python is extracted. Do NOT respond with
prose describing what you would do — emit the script directly.

Your ONLY output mechanism is a single ```python ... ``` fenced code
block in your text response. If you do not produce that block, the
attempt is wasted.
</output_format>

<environment>
You are generating a Python script that the orchestrator will run inside
a disposable, root-privileged container with full network access. The
SCRIPT itself can do anything: install system packages with apt-get,
install Python packages with `uv pip install --system`, fetch URLs, run
shell commands via subprocess, write files anywhere under the workspace,
etc. None of those things are restricted at script-execution time.

But YOU (the model generating the script) have a restricted toolset for
THIS generation step:
- Available tools: Read, Grep, Glob, WebSearch, WebFetch (read-only
  research only — use them to verify package versions, read API docs,
  inspect any files already on disk).
- Disabled tools: Write, Edit, NotebookEdit, Bash, Task. Do not call
  them — they will fail.

Workspace path inside the script: os.environ.get("WORKSPACE", "/workspace").
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
You are an expert engineer producing a Python script that the orchestrator
will run inside a disposable, root-privileged container.

For THIS generation step you have a restricted toolset:
- Read, Grep, Glob: inspect any files already on disk in your cwd.
- WebSearch, WebFetch: verify current package versions against PyPI /
  registry pages, read API docs, look up best practices.
- Write, Edit, NotebookEdit, Bash, Task are DISABLED. Do not call them.

The SCRIPT you emit (which the orchestrator runs after this step) has
no such restrictions — it gets root, network, full filesystem, subprocess,
apt-get, uv pip install, etc. So:
- Research with WebFetch/WebSearch before coding when in doubt about a
  library version or API.
- Bake your research findings directly into the script as comments or
  defensive checks. Do not produce a separate research document.
- Encode package installation, environment setup, file creation, and
  verification ALL inside the single Python script.

After researching, output a complete, self-contained Python script as a
single ```python fenced code block in your text response. That fenced
block is the ONLY thing the orchestrator extracts. Do not respond with
bash/shell code blocks (they will be ignored), do not respond with prose
descriptions of what the script would do, and do not split the script
across multiple fences.
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
    # Phase 6.7: Track raw stderr/stdout from the prior sandbox execution
    # so the retry_clean prompt's <error> section can be grounded in the
    # actual sandbox output rather than a synthesized summary string.
    previous_stderr: str | None = None
    previous_stdout: str | None = None
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

    # Phase 6.8: The Architect forwards its immutable step spec via
    # UAS_STEP_SPEC. Build a step_context dict so build_prompt() can ground
    # the retry_clean <spec> section in the structured Architect fields
    # rather than re-parsing the UAS_TASK blob.
    _step_spec = config.get("step_spec")
    step_context: dict | None = None
    if isinstance(_step_spec, str) and _step_spec.strip():
        step_context = {"step_spec": _step_spec}

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

        # Phase 6.7: First attempt uses the rich "full" prompt; every retry
        # is built in "retry_clean" mode so the LLM has zero memory of prior
        # attempts. The retry_clean prompt is grounded in the post-rollback,
        # post-format workspace state and the prior attempt's raw sandbox
        # output (passed via previous_stderr / previous_stdout).
        prompt_mode: Literal["full", "retry_clean"] = (
            "full" if attempt == 1 else "retry_clean"
        )
        prompt = build_prompt(task, attempt, previous_error, previous_code,
                              environment=environment,
                              workspace_files=workspace_files,
                              system_state=system_state,
                              knowledge=knowledge,
                              test_files=test_files,
                              step_context=step_context,
                              previous_stderr=previous_stderr,
                              previous_stdout=previous_stdout,
                              mode=prompt_mode)

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
                # Phase 6.7: No execution happened — clear any stale sandbox
                # output so the next retry_clean prompt does not leak it.
                previous_stderr = None
                previous_stdout = None
                logger.error("%s", previous_error)
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
                previous_stderr = None
                previous_stdout = None
                logger.error("LLM generation failed: %s", exc)
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
                        "LLM bypassed code-block contract via Bash or tool "
                        "actions. Your previous response described work you "
                        "did with tools (or contained shell/tool markup) "
                        "instead of a Python script.  Files you create with "
                        "tools live in a throwaway temp directory and are "
                        "discarded — they do NOT count toward task "
                        "completion.  You MUST output your complete Python "
                        "script in a single ```python fenced code block in "
                        "your text response.  The orchestrator extracts and "
                        "runs that block in the real workspace."
                    )
                else:
                    previous_error = "Failed to extract code block from LLM response."
                previous_code = None
                previous_stderr = None
                previous_stdout = None
                logger.error("%s", previous_error)
                logger.debug("Raw LLM response (%d chars):\n%s",
                             len(response), response[:2000])
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
                previous_stderr = None
                previous_stdout = None
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
        #
        # Section 5 of PLAN.md: scope the lint to ONLY the Python files this
        # attempt's script reported writing.  Linting every *.py in the
        # workspace blames the current attempt for pre-existing errors in
        # files it never touched (e.g. files committed to uas-wip from a
        # prior failed run), which then re-poisons every rollback forever.
        # Parse UAS_RESULT here (the variable is reused by the success
        # branch below so we never invoke parse_uas_result twice for the
        # same stdout — the fuzzy fallback can trigger an LLM call).
        uas_result = parse_uas_result(result["stdout"] or "")
        py_files_written: list[str] | None = None
        if uas_result and uas_result.files_written:
            py_files_written = [
                f for f in uas_result.files_written if f.endswith(".py")
            ]

        # Section 6 of PLAN.md: when UAS_RESULT is missing (e.g. verifier
        # scripts launched by architect.verify_step_output never emit it),
        # the legacy "lint everything" fallback re-introduced the exact
        # bug Section 5 fixed. Compute git-changed .py files relative to
        # uas-wip instead, so caller paths that don't speak the UAS_RESULT
        # contract still get correctly scoped lint without blaming
        # pre-existing files. Combine UAS_RESULT-claimed files with
        # git-detected changes for defense in depth.
        lint_errors: list[str] = []
        if _workspace:
            git_changed_py: list[str] | None = (
                changed_py_files_since_uas_wip(_workspace)
            )

            files_to_lint: set[str] = set()
            if py_files_written:
                files_to_lint.update(py_files_written)
            if git_changed_py:
                files_to_lint.update(git_changed_py)

            if files_to_lint:
                lint_errors = lint_workspace(
                    _workspace, files=sorted(files_to_lint),
                )
            elif (
                py_files_written is None
                and git_changed_py is None
            ):
                # Neither scoping signal is available: no UAS_RESULT and
                # no git repo / no uas-wip ref. Fall back to linting the
                # whole workspace so non-git callers still get checked.
                lint_errors = lint_workspace(_workspace)
            # Otherwise (at least one signal returned an empty list):
            # the attempt provably did not change any .py files, so
            # there is nothing this attempt could have broken. Skip lint.

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
                    # Phase 6.7: ground the next retry_clean prompt in the
                    # pytest output so it shows the actual test failure.
                    previous_stderr = pytest_result["stderr"]
                    previous_stdout = pytest_result["stdout"]
                    logger.error("Pytest gate FAILED on attempt %d.", attempt)
                    continue
                logger.info("Pytest gate PASSED.")

            # Section 5 of PLAN.md: uas_result was already parsed above
            # the lint pre-check, so we just log it here instead of
            # re-invoking parse_uas_result (which can trigger an LLM call
            # via the fuzzy fallback path).
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
        # Phase 6.7: Capture the prior attempt's raw sandbox output so the
        # next iteration's retry_clean prompt can ground its <error> section
        # in stderr/stdout instead of the synthesized summary string.
        previous_stderr = result["stderr"]
        previous_stdout = result["stdout"]
        logger.error("FAILED on attempt %d.", attempt)

        # Phase 3.4 / 6.7: Roll back the workspace to the last uas-wip
        # checkpoint and format the checkpoint state so the next attempt's
        # retry_clean prompt sees a pristine, normalized filesystem.
        if exec_result.revert_needed and _workspace and _step_id is not None:
            rollback_to_checkpoint(_workspace, _step_id)
            logger.info("Rolled back workspace to uas-wip checkpoint.")
            format_workspace(_workspace)
            logger.info("Formatted workspace after rollback.")

    logger.error("FAILED after %d attempts.", MAX_RETRIES)
    import json as _json
    logger.info("__UAS_ORCH_USAGE__:%s", _json.dumps(_orch_usage))
    sys.exit(1)


if __name__ == "__main__":
    main()
