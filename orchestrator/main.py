"""Orchestrator entry point: Build-Run-Evaluate loop."""

import argparse
import logging
import os
import sys

from .llm_client import get_llm_client
from .parser import extract_code
from .sandbox import run_in_sandbox

MAX_RETRIES = 3
MAX_TASK_LENGTH = 10000

logger = logging.getLogger(__name__)


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


def build_prompt(task: str, attempt: int, previous_error: str | None = None) -> str:
    prompt = """\
You are a Python code generator running inside a sandboxed container.

## Instructions
- Respond with a SINGLE fenced code block tagged as ```python.
- Do NOT include any explanation, commentary, or text outside the code block.
- The script must be complete and self-contained (all imports included).

## Environment
- Python 3.x is available.
- The workspace directory is in the WORKSPACE environment variable.
- Always resolve file paths relative to the workspace:
  ```python
  import os
  workspace = os.environ.get("WORKSPACE", "/workspace")
  path = os.path.join(workspace, "myfile.txt")
  ```
- The script runs inside a sandboxed container with full network access.
- You may install packages freely (e.g. pip install) and use any libraries needed.
- Output results to stdout/stderr.

## Constraints
- Exit with code 0 on success, non-zero on failure.
- Print results to stdout.
- Print errors to stderr.
- Do not use input() or any interactive prompts.

## Task
"""
    prompt += task + "\n"

    if previous_error and attempt > 1:
        prompt += f"""
## Previous Error (attempt {attempt - 1})
```
{previous_error}
```
Fix the error above and provide a corrected script.
"""
    return prompt


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

    if len(task) > MAX_TASK_LENGTH:
        logger.warning(
            "Task is very long (%d chars, max recommended %d). "
            "Consider simplifying.",
            len(task),
            MAX_TASK_LENGTH,
        )

    logger.info("Task: %s", task)

    logger.info("Verifying sandbox...")
    verify = run_in_sandbox("print('sandbox OK')", timeout=120)
    if verify["exit_code"] != 0:
        logger.error(
            "Sandbox verification failed:\n%s", verify["stderr"]
        )
        sys.exit(1)
    logger.info("Sandbox verified.")

    client = get_llm_client()
    previous_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        logger.info("\n--- Attempt %d/%d ---", attempt, MAX_RETRIES)

        prompt = build_prompt(task, attempt, previous_error)
        logger.info("Querying LLM...")
        response = client.generate(prompt)

        code = extract_code(response)
        if not code:
            previous_error = "Failed to extract code block from LLM response."
            logger.error("%s", previous_error)
            continue

        logger.debug("Generated code (%d chars):\n---\n%s\n---", len(code), code)

        logger.info("Executing in sandbox...")
        result = run_in_sandbox(code)

        logger.info("Exit code: %s", result["exit_code"])
        if result["stdout"]:
            logger.info("stdout:\n%s", result["stdout"])
        if result["stderr"]:
            logger.info("stderr:\n%s", result["stderr"])

        if result["exit_code"] == 0:
            logger.info("\nSUCCESS on attempt %d.", attempt)
            sys.exit(0)

        previous_error = (
            result["stderr"] or result["stdout"] or "Non-zero exit code"
        )
        logger.error("FAILED on attempt %d.", attempt)

    logger.error("FAILED after %d attempts.", MAX_RETRIES)
    sys.exit(1)


if __name__ == "__main__":
    main()
