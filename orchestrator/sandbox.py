"""Sandbox for code execution: supports local subprocess and nested Podman modes."""

import os
import subprocess
import tempfile

SANDBOX_IMAGE = os.environ.get(
    "UAS_SANDBOX_IMAGE", "docker.io/library/python:3.12-slim"
)
SANDBOX_TIMEOUT = int(os.environ.get("UAS_SANDBOX_TIMEOUT", "60"))
SANDBOX_MODE = os.environ.get("UAS_SANDBOX_MODE", "container")
WORKSPACE_PATH = os.environ.get("UAS_WORKSPACE", "/workspace")


def run_in_sandbox(code: str, timeout: int | None = None) -> dict:
    """Execute Python code in the configured sandbox mode.

    Returns dict with keys: exit_code, stdout, stderr.
    """
    timeout = timeout or SANDBOX_TIMEOUT

    if SANDBOX_MODE == "local":
        return _run_local(code, timeout)
    return _run_container(code, timeout)


def _run_local(code: str, timeout: int) -> dict:
    """Execute Python code in a local subprocess (no container)."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", dir="/tmp", delete=False
    ) as f:
        f.write(code)
        script_path = f.name

    env = os.environ.copy()
    env["WORKSPACE"] = WORKSPACE_PATH

    try:
        result = subprocess.run(
            ["python3", script_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=WORKSPACE_PATH,
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
            "stderr": f"Execution timed out after {timeout} seconds.",
        }
    finally:
        os.unlink(script_path)


def _run_container(code: str, timeout: int) -> dict:
    """Execute Python code inside a nested Podman container."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", dir="/tmp", delete=False
    ) as f:
        f.write(code)
        script_path = f.name

    try:
        result = subprocess.run(
            [
                "podman", "run", "--rm",
                "--network=none",
                "--memory=256m",
                "--cpus=1",
                "--read-only",
                "--tmpfs", "/tmp:rw,size=64m",
                "-v", f"{script_path}:/sandbox/script.py:ro,Z",
                "-v", f"{WORKSPACE_PATH}:/workspace:Z",
                "-e", "WORKSPACE=/workspace",
                SANDBOX_IMAGE,
                "python3", "/sandbox/script.py",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
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
            "stderr": f"Execution timed out after {timeout} seconds.",
        }
    finally:
        os.unlink(script_path)
