"""Orchestrator entry point: Build-Run-Evaluate loop."""

import os
import sys

from .llm_client import get_llm_client
from .parser import extract_code
from .sandbox import run_in_sandbox

MAX_RETRIES = 3


def get_task() -> str:
    """Get task from CLI args, env var, or stdin."""
    if len(sys.argv) > 1:
        return " ".join(sys.argv[1:])
    task = os.environ.get("UAS_TASK")
    if task:
        return task
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    print("Enter task (submit with Ctrl+D):")
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
    task = get_task()
    if not task:
        print("ERROR: No task provided.", file=sys.stderr)
        sys.exit(1)

    print(f"Task: {task}")

    print("Verifying nested Podman...")
    verify = run_in_sandbox("print('sandbox OK')", timeout=120)
    if verify["exit_code"] != 0:
        print(
            f"ERROR: Nested Podman verification failed:\n{verify['stderr']}",
            file=sys.stderr,
        )
        sys.exit(1)
    print("Nested Podman verified successfully.")

    client = get_llm_client()
    previous_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        print(f"\n--- Attempt {attempt}/{MAX_RETRIES} ---")

        prompt = build_prompt(task, attempt, previous_error)
        print("Querying LLM...")
        response = client.generate(prompt)

        code = extract_code(response)
        if not code:
            previous_error = "Failed to extract code block from LLM response."
            print(f"ERROR: {previous_error}")
            continue

        print(f"Generated code ({len(code)} chars):\n---\n{code}\n---")

        print("Executing in sandbox...")
        result = run_in_sandbox(code)

        print(f"Exit code: {result['exit_code']}")
        if result["stdout"]:
            print(f"stdout:\n{result['stdout']}")
        if result["stderr"]:
            print(f"stderr:\n{result['stderr']}")

        if result["exit_code"] == 0:
            print(f"\nSUCCESS on attempt {attempt}.")
            sys.exit(0)

        previous_error = (
            result["stderr"] or result["stdout"] or "Non-zero exit code"
        )
        print(f"FAILED on attempt {attempt}.")

    print(f"\nFAILED after {MAX_RETRIES} attempts.", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
