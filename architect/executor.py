"""Interface to the Orchestrator: local subprocess or container modes."""

import os
import shutil
import subprocess
import sys

IMAGE_NAME = "uas-engine"
RUN_TIMEOUT = 600  # 10 minutes max per orchestrator invocation
EXECUTION_MODE = os.environ.get("UAS_SANDBOX_MODE", "container")


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
        framework_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        print("  Building Orchestrator image (first run)...")
        subprocess.run(
            [
                engine, "build", "-t", IMAGE_NAME,
                "-f", os.path.join(framework_root, "Containerfile"),
                framework_root,
            ],
            check=True,
            capture_output=True,
            text=True,
        )


def run_orchestrator(task: str) -> dict:
    """Run the Orchestrator with the given task.

    Returns dict with exit_code, stdout, stderr.
    """
    if EXECUTION_MODE == "local":
        return _run_local(task)
    return _run_container(task)


def _run_local(task: str) -> dict:
    """Run the Orchestrator as a local subprocess (no container)."""
    framework_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    env = os.environ.copy()
    env["UAS_TASK"] = task

    try:
        result = subprocess.run(
            [sys.executable, "-m", "orchestrator.main"],
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT,
            cwd=framework_root,
            env=env,
            stdin=subprocess.DEVNULL,
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


def _run_container(task: str) -> dict:
    """Run the Orchestrator inside a container with proper entrypoint and mounts."""
    engine = find_engine()
    if not engine:
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": "No container engine found (checked podman, docker).",
        }

    ensure_image(engine)

    workspace = os.environ.get("UAS_WORKSPACE", "/workspace")

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

    # Override entrypoint to run the Orchestrator directly (not the interactive
    # entrypoint.sh which launches 'claude' and then the Architect).
    # Mount workspace so sandbox containers can persist files between steps.
    cmd = [
        engine, "run", "--rm", "--privileged",
        "--entrypoint", "python3",
        "-v", f"{workspace}:/workspace:Z",
    ] + env_args + [
        IMAGE_NAME,
        "-m", "orchestrator.main",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT,
            stdin=subprocess.DEVNULL,
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
