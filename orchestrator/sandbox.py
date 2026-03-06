"""Sandbox for code execution: supports local subprocess and nested Podman modes."""

import logging
import os
import subprocess
import tempfile
import uuid

logger = logging.getLogger(__name__)

SANDBOX_IMAGE = os.environ.get(
    "UAS_SANDBOX_IMAGE", "docker.io/library/python:3.12-slim"
)
_sandbox_timeout_str = os.environ.get("UAS_SANDBOX_TIMEOUT")
SANDBOX_TIMEOUT = int(_sandbox_timeout_str) if _sandbox_timeout_str else None
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


def _kill_container(name: str):
    """Attempt to stop and remove a container by name."""
    try:
        subprocess.run(
            ["podman", "--storage-driver=vfs", "kill", name],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass
    try:
        subprocess.run(
            ["podman", "--storage-driver=vfs", "rm", "-f", name],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


def _run_container(code: str, timeout: int) -> dict:
    """Execute Python code inside a nested Podman container."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", dir="/tmp", delete=False
    ) as f:
        f.write(code)
        script_path = f.name

    container_name = f"uas-sandbox-{uuid.uuid4().hex[:8]}"

    try:
        result = subprocess.run(
            [
                "podman", "--storage-driver=vfs",
                "run", "--rm",
                "--name", container_name,
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
    except subprocess.CalledProcessError as e:
        return {
            "exit_code": e.returncode,
            "stdout": e.stdout or "",
            "stderr": e.stderr or "",
        }
    except subprocess.TimeoutExpired:
        logger.warning("Killing timed-out container %s...", container_name)
        _kill_container(container_name)
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Execution timed out after {timeout} seconds.",
        }
    finally:
        os.unlink(script_path)
