"""Orchestrator entry point: Build-Run-Evaluate loop."""

import argparse
import logging
import os
import sys

from .llm_client import get_llm_client
from .parser import extract_code
from .sandbox import run_in_sandbox

MAX_RETRIES = 3

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
    prompt = (
        "You are a code generator. Write a Python script that accomplishes the "
        "following task.\n"
        "Respond with ONLY a single markdown code block containing the complete "
        "Python script.\n"
        "Do not include any explanation outside the code block.\n"
        "IMPORTANT: The workspace directory path is available via the WORKSPACE "
        "environment variable. Always use: "
        "import os; workspace = os.environ.get('WORKSPACE', '/workspace')\n"
        "Then build file paths with os.path.join(workspace, filename).\n\n"
        f"Task: {task}\n"
    )
    if previous_error and attempt > 1:
        prompt += (
            f"\nThe previous attempt (attempt {attempt - 1}) failed with this "
            f"error:\n```\n{previous_error}\n```\n"
            "Fix the error and provide a corrected script.\n"
        )
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

    logger.info("Task: %s", task)

    logger.info("Verifying nested Podman...")
    verify = run_in_sandbox("print('sandbox OK')", timeout=120)
    if verify["exit_code"] != 0:
        logger.error(
            "Nested Podman verification failed:\n%s", verify["stderr"]
        )
        sys.exit(1)
    logger.info("Nested Podman verified successfully.")

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
