"""Orchestrator entry point: Build-Run-Evaluate loop."""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

from .llm_client import get_llm_client
from .parser import extract_code
from .sandbox import run_in_sandbox

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


def build_prompt(task: str, attempt: int, previous_error: str | None = None,
                 previous_code: str | None = None,
                 environment: list[str] | None = None) -> str:
    """Build the structured prompt for code generation.

    Uses XML tags to separate role, environment, task, constraints,
    and verification sections for clarity.
    """
    env_setup = ""
    if environment:
        pkgs = " ".join(environment)
        env_setup = (
            f"\n- Required packages for this task: {pkgs}\n"
            "- Install them at the top of your script using subprocess:\n"
            "  subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', "
            f"'{pkgs}'], check=True)\n"
        )

    prompt = f"""\
<role>
You are an expert Python engineer generating production-quality scripts
inside an isolated container. Respond with a SINGLE fenced code block
tagged as ```python. Do NOT include any explanation or text outside the
code block. The script must be complete and self-contained.
</role>

<environment>
- Python 3.12 with full root access.
- Workspace directory: use os.environ.get("WORKSPACE", "/workspace").
- Always resolve file paths with os.path.join(workspace, ...).
- Full unrestricted network access. Fetch URLs, call APIs, scrape freely.
- Install any packages you need without hesitation (pip install, apt-get).
- You have complete autonomy. No resource limits.{env_setup}
</environment>

<task>
{task}
</task>

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
   UAS_RESULT: {{"status": "ok", "files_written": [...], "summary": "..."}}
   If verification fails, exit with code 1 and print:
   UAS_RESULT: {{"status": "error", "error": "description of what failed"}}
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


def _record_code_version(step_id, spec_attempt, orch_attempt, code, prompt,
                         exit_code=-1, error_summary=""):
    """Record a code version to disk for the architect's code tracker."""
    workspace = os.environ.get("UAS_WORKSPACE", os.getcwd())
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

    client = get_llm_client()
    previous_error = None
    previous_code = None

    for attempt in range(1, MAX_RETRIES + 1):
        logger.info("\n--- Attempt %d/%d ---", attempt, MAX_RETRIES)

        prompt = build_prompt(task, attempt, previous_error, previous_code)
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

        logger.debug("Generated code (%d chars):\n---\n%s\n---", len(code), code)

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
        if result["stdout"]:
            logger.info("stdout:\n%s", result["stdout"])
        if result["stderr"]:
            logger.info("stderr:\n%s", result["stderr"])

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
