"""Interface to the Orchestrator: local subprocess or container modes."""

import logging
import os
import shutil
import subprocess
import sys
import uuid

SANDBOX_IMAGE_NAME = "uas-sandbox"
SANDBOX_BASE_IMAGE = "docker.io/library/python:3.12-slim"
RUN_TIMEOUT = 600  # 10 minutes max per orchestrator invocation
EXECUTION_MODE = os.environ.get("UAS_SANDBOX_MODE", "container")

logger = logging.getLogger(__name__)


def find_engine() -> str | None:
    for cmd in ("podman", "docker"):
        if shutil.which(cmd):
            return cmd
    return None


def _podman_cmd(engine: str, *args: str) -> list[str]:
    """Build a podman/docker command with --storage-driver=vfs for podman."""
    cmd = [engine]
    if engine == "podman":
        cmd.append("--storage-driver=vfs")
    cmd.extend(args)
    return cmd


def ensure_image(engine: str):
    """Ensure the lightweight uas-sandbox image exists.

    Builds a minimal Python-based image with just the orchestrator code,
    NOT the full uas-engine image (which would cause an inception loop
    when already running inside uas-engine).
    """
    check = subprocess.run(
        _podman_cmd(engine, "image", "inspect", SANDBOX_IMAGE_NAME),
        capture_output=True,
    )
    if check.returncode == 0:
        return

    framework_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

    # Dynamically generate a minimal Dockerfile for the sandbox.
    # Node.js + Claude Code CLI are required so the orchestrator
    # can call the LLM from inside the container.
    dockerfile_content = (
        f"FROM {SANDBOX_BASE_IMAGE}\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends "
        "nodejs npm && rm -rf /var/lib/apt/lists/*\n"
        "RUN npm install -g @anthropic-ai/claude-code\n"
        "WORKDIR /uas\n"
        "COPY orchestrator/ ./orchestrator/\n"
        "VOLUME /workspace\n"
        "WORKDIR /workspace\n"
    )

    dockerfile_path = os.path.join(framework_root, "Sandbox.Dockerfile")
    try:
        with open(dockerfile_path, "w") as f:
            f.write(dockerfile_content)

        logger.info("  Building lightweight sandbox image (first run)...")
        try:
            subprocess.run(
                _podman_cmd(
                    engine, "build", "-t", SANDBOX_IMAGE_NAME,
                    "-f", dockerfile_path,
                    framework_root,
                ),
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            logger.error(
                "  Sandbox image build failed (exit %d).", e.returncode
            )
            if e.stderr:
                logger.error("  Podman stderr:\n%s", e.stderr)
            if e.stdout:
                logger.error("  Podman stdout:\n%s", e.stdout)
            raise
    finally:
        if os.path.exists(dockerfile_path):
            os.unlink(dockerfile_path)


def run_orchestrator(task: str) -> dict:
    """Run the Orchestrator with the given task.

    Returns dict with exit_code, stdout, stderr.
    """
    if EXECUTION_MODE == "local":
        return _run_local(task)
    return _run_container(task)


def _run_local(task: str) -> dict:
    """Run the Orchestrator as a local subprocess (no container)."""
    framework_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..")
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = framework_root
    env["IS_SANDBOX"] = "1"
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


def _kill_container(engine: str, name: str):
    """Attempt to stop and remove a container by name."""
    try:
        subprocess.run(
            _podman_cmd(engine, "kill", name),
            capture_output=True, timeout=10,
        )
    except Exception:
        pass
    try:
        subprocess.run(
            _podman_cmd(engine, "rm", "-f", name),
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


def _run_container(task: str) -> dict:
    """Run the Orchestrator inside a lightweight sandbox container."""
    engine = find_engine()
    if not engine:
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": "No container engine found (checked podman, docker).",
        }

    try:
        ensure_image(engine)
    except subprocess.CalledProcessError:
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": "Failed to build sandbox image. See error output above.",
        }

    workspace = os.environ.get("UAS_WORKSPACE", "/workspace")
    container_name = f"uas-orchestrator-{uuid.uuid4().hex[:8]}"

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
    env_args.extend(["-e", "PYTHONPATH=/uas"])
    env_args.extend(["-e", "IS_SANDBOX=1"])

    # Force local sandbox mode inside the container since this lightweight
    # image does not have Podman -- the container itself provides isolation.
    env_args.extend(["-e", "UAS_SANDBOX_MODE=local"])

    # Mount auth credentials into the sandbox so the Claude CLI
    # can authenticate (credentials live at /root/.claude in the
    # outer uas-engine container).
    auth_args = []
    auth_dir = "/root/.claude"
    if os.path.isdir(auth_dir):
        auth_args = ["-v", f"{auth_dir}:/root/.claude:Z"]

    cmd = _podman_cmd(
        engine, "run", "--rm",
        "--name", container_name,
        "--entrypoint", "python3",
        "-v", f"{workspace}:/workspace:Z",
    ) + auth_args + env_args + [
        SANDBOX_IMAGE_NAME,
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
    except subprocess.CalledProcessError as e:
        logger.error(
            "  Container run failed (exit %d).", e.returncode
        )
        if e.stderr:
            logger.error("  Podman stderr:\n%s", e.stderr)
        if e.stdout:
            logger.error("  Podman stdout:\n%s", e.stdout)
        return {
            "exit_code": e.returncode,
            "stdout": e.stdout or "",
            "stderr": e.stderr or "",
        }
    except subprocess.TimeoutExpired:
        logger.warning("  Killing timed-out container %s...", container_name)
        _kill_container(engine, container_name)
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Orchestrator timed out after {RUN_TIMEOUT}s.",
        }


def extract_sandbox_stdout(orchestrator_output: str) -> str:
    """Extract the sandbox script's stdout from orchestrator log.

    The orchestrator sends log messages to stderr, so this function
    parses the orchestrator's stderr output for stdout/stderr markers.
    """
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
