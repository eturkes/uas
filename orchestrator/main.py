"""Orchestrator entry point: Build-Run-Evaluate loop."""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from .llm_client import get_llm_client
from .parser import extract_code
from .sandbox import run_in_sandbox

# Section 5d: Delimited output markers for reliable parsing by the architect.
STDOUT_START = "===STDOUT_START==="
STDOUT_END = "===STDOUT_END==="
STDERR_START = "===STDERR_START==="
STDERR_END = "===STDERR_END==="

MAX_RETRIES = 3

logger = logging.getLogger(__name__)

_UAS_RESULT_PATTERN = re.compile(r"^UAS_RESULT:\s*(\{.*\})\s*$", re.MULTILINE)


def parse_uas_result(stdout: str) -> dict | None:
    """Extract the UAS_RESULT JSON from stdout if present.

    Looks for a line matching: UAS_RESULT: {"status": "ok", ...}
    Returns the parsed dict or None if not found/invalid.
    """
    match = _UAS_RESULT_PATTERN.search(stdout)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
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
    task = os.environ.get("UAS_TASK")
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
_SKIP_DIRS = {".state", ".git", "__pycache__", "node_modules", "venv", ".venv", ".claude"}


def scan_workspace(workspace_path: str, max_chars: int = 4000) -> str:
    """Scan workspace and return a formatted listing of existing files.

    Section 5b: Allows the orchestrator to be aware of workspace contents
    even when the architect hasn't passed UAS_WORKSPACE_FILES.
    """
    if not workspace_path or not os.path.isdir(workspace_path):
        return ""
    lines: list[str] = []
    total = 0
    for entry in sorted(os.listdir(workspace_path)):
        if total >= max_chars:
            break
        if entry.startswith(".") or entry in _SKIP_DIRS:
            continue
        full = os.path.join(workspace_path, entry)
        if os.path.isfile(full):
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            _, ext = os.path.splitext(entry.lower())
            ftype = "text" if ext in _TEXT_EXTENSIONS else "binary"
            line = f"  {entry} ({size} bytes, {ftype})"
            lines.append(line)
            total += len(line)
        elif os.path.isdir(full):
            lines.append(f"  {entry}/")
            total += len(entry) + 3
    return "\n".join(lines)


def build_prompt(task: str, attempt: int, previous_error: str | None = None,
                 previous_code: str | None = None,
                 environment: list[str] | None = None,
                 workspace_files: str | None = None) -> str:
    """Build the structured prompt for code generation.

    Uses XML tags with data sections (environment, task, workspace state)
    at the top and instruction sections (role, constraints, verification)
    at the bottom for optimal Claude response quality.
    """
    pkg_hint = ""
    if environment:
        pkgs = " ".join(environment)
        pkg_hint = (
            f"\nSuggested packages for this task: {pkgs}\n"
            "Install these if appropriate, but use your own judgment — add or substitute\n"
            "packages if you know a better option.\n"
        )

    # Data sections at top (environment, task, workspace state)
    prompt = f"""\
<environment>
You are running inside an isolated, disposable container. You have FULL AUTONOMY:
- ROOT ACCESS. Install any system packages with apt-get. No sudo needed.
- UNRESTRICTED NETWORK. Fetch any URL, call any API, clone any repo. No firewall, no proxy.
- PACKAGE INSTALLATION. pip install anything you need. Do it proactively at the top of your script.
- COMMAND EXECUTION. Run any shell command via subprocess. No restrictions whatsoever.
- WEB SEARCH. If you need to look something up — current library versions, API docs, best practices — you can and should use the network.
- FILESYSTEM. Full read/write. Workspace: os.environ.get("WORKSPACE", "/workspace").

This container is disposable. Nothing here affects the host. Be bold, not cautious.
{pkg_hint}
</environment>

<approach>
Before writing code, reason through these questions:
1. What is the best approach for this task? Are there multiple strategies?
   Pick the most robust one.
2. What packages or tools does this require? What are their current stable
   versions? If you're not sure, check PyPI (https://pypi.org/pypi/PACKAGE/json)
   or use pip's --dry-run flag to verify availability.
3. Are there known pitfalls, breaking changes, or deprecations in the
   libraries you plan to use? If uncertain, check the docs.
4. If the task involves an external API or data source, what is its current
   format/schema? Don't assume — verify if possible.

Encode your research findings directly into your code as comments or as
defensive checks. Don't produce a separate research document — just write
better code because you researched first.
</approach>

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
</workspace_state>"""

    # Instruction sections at bottom (role, constraints, verification)
    prompt += """

<role>
You are an expert engineer with full system access in a disposable container.
Generate a complete, self-contained Python script.

Before writing code, think about what you need:
- What packages does this task require? Install them.
- Are there tools that would improve quality (linters, formatters, test runners)?
  Install and use them if it would meaningfully improve the result.
- Is there information you're uncertain about (API formats, library versions,
  current best practices)? Use the network to check.

Act like a senior engineer who sets up their own environment before starting work.

CRITICAL OUTPUT FORMAT: Your response must contain exactly ONE fenced code block
tagged as ```python ... ```. The script must be complete and self-contained.
Do NOT use any XML tags, tool_call blocks, or analysis sections.
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
- If creating a project, use git init -b main (not master).
- Pin dependency versions in pip install commands.
</constraints>

<verification>
At the end of your script, include a self-verification section that:
1. Checks that any output files actually exist and are non-empty.
2. Validates the output format if applicable.
3. Prints a machine-readable summary as the LAST line of stdout:
   UAS_RESULT: {"status": "ok", "files_written": [...], "summary": "..."}
   If verification fails, exit with code 1 and print:
   UAS_RESULT: {"status": "error", "error": "description of what failed"}
</verification>"""

    if previous_error and attempt > 1:
        code_section = ""
        if previous_code:
            code_section = (
                f"\nThe script that failed:\n```python\n{previous_code}\n```\n"
            )

        if attempt >= MAX_RETRIES:
            # Final attempt — maximally defensive
            prompt += f"""

<previous_error attempt="{attempt - 1}">
{code_section}
Error output:
```
{previous_error}
```

This is the FINAL attempt. Be maximally defensive:
- Add try/except around every external call.
- Validate all inputs before use.
- Include detailed error messages in every except block.
- Do NOT repeat the same approach if it failed before.
- Use a fundamentally different strategy if needed.

Before writing the script, analyze the root cause in <analysis> tags.
</previous_error>"""
        elif attempt > 2:
            # Second retry — different strategy
            prompt += f"""

<previous_error attempt="{attempt - 1}">
{code_section}
Error output:
```
{previous_error}
```

The previous approach failed. Do NOT repeat the same approach.
Identify what went wrong and use a fundamentally different strategy.

Before writing the script, analyze the root cause in <analysis> tags.
</previous_error>"""
        else:
            # First retry
            prompt += f"""

<previous_error attempt="{attempt - 1}">
{code_section}
Error output:
```
{previous_error}
```

Before writing the corrected script, analyze the root cause of this
error in <analysis> tags, then write the fixed script.
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
    max_n = int(os.environ.get("UAS_BEST_OF_N", "1"))
    if max_n <= 1 or attempt <= 1:
        return 1
    # attempt 2 → N=2, attempt 3 → N=3, capped by max_n
    return min(attempt, max_n)


def score_result(result: dict) -> int:
    """Score a sandbox execution result for selection among candidates.

    Section 7b: Prefer successful runs, then richer UAS_RESULT output.
    Returns an integer score (higher is better).
    """
    score = 0
    if result["exit_code"] == 0:
        score += 1000

    uas = parse_uas_result(result.get("stdout", ""))
    if uas:
        score += 100  # has any UAS_RESULT
        if uas.get("files_written"):
            score += 50 + len(uas["files_written"]) * 10
        if uas.get("summary"):
            score += 50
        if uas.get("status") == "ok":
            score += 50

    # Prefer runs with more stdout (more informative)
    stdout_len = len(result.get("stdout", ""))
    score += min(stdout_len // 100, 50)
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

    # Build candidates section with truncated previews
    sections = []
    for code, result, idx in candidates:
        exit_code = result.get("exit_code", -1)
        stdout = (result.get("stdout", "") or "")[:2000]
        stderr = (result.get("stderr", "") or "")[:1000]
        code_preview = (code or "")[:3000]

        sections.append(
            f"<candidate index=\"{idx}\">\n"
            f"Exit code: {exit_code}\n"
            f"Code:\n```python\n{code_preview}\n```\n"
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
        response = client.generate(prompt)

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
    return sorted(candidates, key=lambda x: score_result(x[1]), reverse=True)


def _generate_one(client, prompt: str, hint: str):
    """Generate code from a single prompt variant and execute it.

    Returns (code, sandbox_result) or (None, None) on extraction failure.
    """
    full_prompt = prompt + hint if hint else prompt
    response = client.generate(full_prompt)
    code = extract_code(response)
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
        scored = [(code, result, idx, score_result(result))
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
    workspace = os.environ.get("UAS_WORKSPACE", os.getcwd())
    run_id = os.environ.get("UAS_RUN_ID", "")
    if run_id:
        versions_dir = os.path.join(workspace, ".state", "runs", run_id,
                                    "code_versions")
    else:
        versions_dir = os.path.join(workspace, ".state", "code_versions")
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
    verbose = args.verbose or os.environ.get("UAS_VERBOSE", "").lower() in (
        "1", "true", "yes",
    )
    configure_logging(verbose)

    task = get_task(args)
    if not task:
        logger.error("No task provided.")
        sys.exit(1)

    # Read step context for code tracking
    _step_id_str = os.environ.get("UAS_STEP_ID")
    _spec_attempt = int(os.environ.get("UAS_SPEC_ATTEMPT", "0"))
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

    # Section 5c: Use coder-specific model for code generation
    client = get_llm_client(role="coder")
    previous_error = None
    previous_code = None
    workspace_files = os.environ.get("UAS_WORKSPACE_FILES")

    # Read step's package requirements from the architect
    environment = None
    env_str = os.environ.get("UAS_STEP_ENVIRONMENT")
    if env_str:
        try:
            environment = json.loads(env_str)
            if not isinstance(environment, list):
                environment = None
        except (json.JSONDecodeError, ValueError):
            pass

    # Section 5b: If workspace files aren't provided by the architect,
    # scan the workspace directly so the LLM knows what already exists.
    if not workspace_files:
        workspace_path = os.environ.get("UAS_WORKSPACE") or os.environ.get("WORKSPACE")
        if workspace_path:
            workspace_files = scan_workspace(workspace_path) or None

    for attempt in range(1, MAX_RETRIES + 1):
        logger.info("\n--- Attempt %d/%d ---", attempt, MAX_RETRIES)

        prompt = build_prompt(task, attempt, previous_error, previous_code,
                              environment=environment,
                              workspace_files=workspace_files)

        # Section 7c: Determine N for this attempt (budget-aware gating).
        n = _get_best_of_n(attempt)

        if n > 1:
            # Section 7a/7b: Parallel best-of-N generation + execution voting.
            code, result = generate_and_vote(client, prompt, n, task=task)
            if code is None:
                previous_error = "Failed to extract code block from LLM response."
                previous_code = None
                logger.error("%s", previous_error)
                continue
        else:
            # Standard single-sample path.
            logger.info("Querying LLM...")
            response = client.generate(prompt)

            code = extract_code(response)
            if not code:
                previous_error = "Failed to extract code block from LLM response."
                previous_code = None
                logger.error("%s", previous_error)
                logger.debug("Raw LLM response (%d chars):\n%s",
                             len(response), response[:2000])
                continue

            logger.debug("Generated code (%d chars):\n---\n%s\n---",
                         len(code), code)

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

        if result["exit_code"] == 0:
            uas_result = parse_uas_result(result["stdout"] or "")
            if uas_result:
                logger.info("UAS_RESULT: %s", json.dumps(uas_result))
            logger.info("\nSUCCESS on attempt %d.", attempt)
            sys.exit(0)

        previous_code = code
        previous_error = (
            result["stderr"] or result["stdout"] or "Non-zero exit code"
        )
        logger.error("FAILED on attempt %d.", attempt)

    logger.error("FAILED after %d attempts.", MAX_RETRIES)
    sys.exit(1)


if __name__ == "__main__":
    main()
