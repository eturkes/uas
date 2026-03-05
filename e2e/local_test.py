#!/usr/bin/env python3
"""E2E test: drives the Architect/Orchestrator loop with a simple two-step task.

Bypasses the interactive Stage 1 entrypoint and runs entirely unattended.
Monitors for: subprocess hangs, nested Podman failures, volume mount errors.
"""

import os
import shutil
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
WORKSPACE = os.path.join(SCRIPT_DIR, "workspace")
TIMEOUT = 600  # 10 minutes max


def main():
    # Clean up from previous runs
    for path in [
        WORKSPACE,
        os.path.join(SCRIPT_DIR, "step1.txt"),
        os.path.join(SCRIPT_DIR, "step2.txt"),
    ]:
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.isfile(path):
            os.unlink(path)

    os.makedirs(WORKSPACE, exist_ok=True)

    goal = (
        "Step 1: Write a file named 'step1.txt' containing the word 'HELLO' "
        "to /workspace. "
        "Step 2: Read 'step1.txt' from /workspace and write a new file "
        "'step2.txt' to /workspace containing 'HELLO WORLD'."
    )

    env = os.environ.copy()
    env["UAS_GOAL"] = goal
    env["UAS_WORKSPACE"] = WORKSPACE
    env["UAS_SANDBOX_MODE"] = "local"

    print("=" * 60)
    print("  UAS End-to-End Test")
    print("=" * 60)
    print(f"  Goal: {goal}")
    print(f"  Workspace: {WORKSPACE}")
    print(f"  Timeout: {TIMEOUT}s")
    print("=" * 60)
    print()

    try:
        result = subprocess.run(
            [sys.executable, "-m", "architect.main"],
            env=env,
            cwd=REPO_ROOT,
            timeout=TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        print(f"\nE2E TEST FAILED: Process timed out after {TIMEOUT}s (subprocess hang)")
        return 1
    except Exception as e:
        print(f"\nE2E TEST FAILED: Unexpected error: {e}")
        return 1

    print(f"\nArchitect exited with code: {result.returncode}")

    # Copy results from workspace to project root
    for fname in ["step1.txt", "step2.txt"]:
        src = os.path.join(WORKSPACE, fname)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(SCRIPT_DIR, fname))
            print(f"  Copied {fname} to project root")

    # Verify results
    step1_path = os.path.join(SCRIPT_DIR, "step1.txt")
    step2_path = os.path.join(SCRIPT_DIR, "step2.txt")

    if os.path.exists(step1_path):
        content = open(step1_path).read().strip()
        print(f"  step1.txt: {content!r}")
    else:
        print("  step1.txt: NOT FOUND")

    if os.path.exists(step2_path):
        content = open(step2_path).read().strip()
        if "HELLO WORLD" in content:
            print(f"  step2.txt: {content!r}")
            print("\nE2E TEST PASSED")
            return 0
        else:
            print(f"\nE2E TEST FAILED: step2.txt contains {content!r}, expected 'HELLO WORLD'")
            return 1
    else:
        print("\nE2E TEST FAILED: step2.txt was not created")
        return 1


if __name__ == "__main__":
    sys.exit(main())
