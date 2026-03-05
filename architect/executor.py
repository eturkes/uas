"""Interface to the existing Orchestrator via subprocess."""

import os
import shutil
import subprocess

PROJECT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
IMAGE_NAME = "uas-orchestrator"
RUN_TIMEOUT = 600  # 10 minutes max per orchestrator invocation


def find_engine() -> str | None:
    for cmd in ("podman", "docker"):
        if shutil.which(cmd):
            return cmd
    return None


def ensure_image(engine: str):
    check = subprocess.run(
        [engine, "image", "inspect", IMAGE_NAME],
        capture_output=True,
    )
    if check.returncode != 0:
        print("  Building Orchestrator image (first run)...")
        subprocess.run(
            [
                engine, "build", "-t", IMAGE_NAME,
                "-f", os.path.join(PROJECT_ROOT, "Containerfile"),
                PROJECT_ROOT,
            ],
            check=True,
            capture_output=True,
            text=True,
        )


def run_orchestrator(task: str) -> dict:
    """Run the Orchestrator container with the given task.

    Returns dict with exit_code, stdout, stderr.
    """
    engine = find_engine()
    if not engine:
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": "No container engine found (checked podman, docker).",
        }

    ensure_image(engine)

    # Pass through API keys and config from host environment
    env_args = []
    for var in [
        "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL",
        "UAS_SANDBOX_IMAGE", "UAS_SANDBOX_TIMEOUT",
    ]:
        val = os.environ.get(var)
        if val:
            env_args.extend(["-e", f"{var}={val}"])

    env_args.extend(["-e", f"UAS_TASK={task}"])

    cmd = [engine, "run", "--rm", "--privileged"] + env_args + [IMAGE_NAME]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT,
        )
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Orchestrator timed out after {RUN_TIMEOUT}s.",
        }


def extract_sandbox_stdout(orchestrator_output: str) -> str:
    """Extract the sandbox script's stdout from orchestrator log."""
    lines = orchestrator_output.split("\n")
    captured = []
    capturing = False
    for line in lines:
        if line.startswith("stdout:"):
            capturing = True
            captured = []
            # Handle inline content after "stdout:"
            rest = line[len("stdout:"):].strip()
            if rest:
                captured.append(rest)
            continue
        if capturing:
            if line.startswith(("stderr:", "Exit code:", "SUCCESS", "FAILED", "--- Attempt")):
                capturing = False
            else:
                captured.append(line)
    return "\n".join(captured).strip()
