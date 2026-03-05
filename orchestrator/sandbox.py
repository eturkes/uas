"""Nested Podman sandbox for code execution."""

import os
import subprocess
import tempfile

SANDBOX_IMAGE = os.environ.get(
    "UAS_SANDBOX_IMAGE", "docker.io/library/python:3.12-slim"
)
SANDBOX_TIMEOUT = int(os.environ.get("UAS_SANDBOX_TIMEOUT", "60"))


def run_in_sandbox(code: str, timeout: int | None = None) -> dict:
    """Execute Python code inside a nested Podman container.

    Returns dict with keys: exit_code, stdout, stderr.
    """
    timeout = timeout or SANDBOX_TIMEOUT

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
                SANDBOX_IMAGE,
                "python3", "/sandbox/script.py",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
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
