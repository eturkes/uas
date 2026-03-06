"""Interface to the Orchestrator: local subprocess or container modes."""

import logging
import os
import re
import shutil
import subprocess
import sys
import time as _time
import uuid

from orchestrator.claude_config import get_claude_md_content
from .events import EventType, get_event_log
from .provenance import get_provenance_graph

SANDBOX_IMAGE_NAME = "uas-sandbox"
MAX_CONTEXT_LENGTH = int(os.environ.get("UAS_MAX_CONTEXT_LENGTH", "8000"))
SANDBOX_BASE_IMAGE = "docker.io/library/python:3.12-slim"
RUN_TIMEOUT = None
EXECUTION_MODE = os.environ.get("UAS_SANDBOX_MODE", "container")

logger = logging.getLogger(__name__)


def find_engine() -> str | None:
    for cmd in ("podman", "docker"):
        if shutil.which(cmd):
            return cmd
    return None


def _in_container() -> bool:
    """Detect if we're running inside a container."""
    return os.path.exists("/run/.containerenv") or os.path.exists("/.dockerenv")


def _podman_cmd(engine: str, *args: str) -> list[str]:
    """Build a podman/docker command, adding --storage-driver=vfs when inside a container."""
    cmd = [engine]
    if engine == "podman" and _in_container():
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
        "ca-certificates curl && rm -rf /var/lib/apt/lists/*\n"
        "RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - "
        "&& apt-get install -y --no-install-recommends nodejs "
        "&& rm -rf /var/lib/apt/lists/*\n"
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


def ensure_claude_md(workspace: str) -> None:
    """Write .claude/CLAUDE.md to the workspace if missing or outdated."""
    claude_dir = os.path.join(workspace, ".claude")
    claude_md_path = os.path.join(claude_dir, "CLAUDE.md")
    content = get_claude_md_content()
    if os.path.isfile(claude_md_path):
        try:
            with open(claude_md_path, "r") as f:
                if f.read() == content:
                    return
        except OSError:
            pass
    os.makedirs(claude_dir, exist_ok=True)
    with open(claude_md_path, "w") as f:
        f.write(content)
    logger.debug("Wrote .claude/CLAUDE.md to %s", workspace)


def run_orchestrator(task: str) -> dict:
    """Run the Orchestrator with the given task.

    Returns dict with exit_code, stdout, stderr.
    """
    workspace = os.environ.get("UAS_WORKSPACE", os.getcwd())
    try:
        ensure_claude_md(workspace)
    except OSError as e:
        logger.warning("Could not write .claude/CLAUDE.md: %s", e)

    event_log = get_event_log()
    event_log.emit(EventType.SANDBOX_START, data={"mode": EXECUTION_MODE})
    sandbox_start = _time.monotonic()

    if EXECUTION_MODE == "local":
        result = _run_local(task)
    else:
        result = _run_container(task)

    sandbox_elapsed = _time.monotonic() - sandbox_start
    event_log.emit(EventType.SANDBOX_COMPLETE,
                   duration=sandbox_elapsed,
                   data={"exit_code": result["exit_code"]})
    return result


def _run_local(task: str) -> dict:
    """Run the Orchestrator as a local subprocess (no container)."""
    framework_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..")
    )
    workspace = os.environ.get("UAS_WORKSPACE", os.getcwd())
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
            cwd=workspace,
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
            "stderr": "Orchestrator timed out.",
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
        "UAS_LLM_TIMEOUT", "UAS_MODEL", "UAS_VERBOSE",
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

    # Mount auth credentials read-only so the Claude CLI can
    # authenticate. Check /root/.claude (inside uas-engine container)
    # then fall back to ~/.claude (running from host).
    auth_args = []
    for auth_dir in ["/root/.claude", os.path.join(os.path.expanduser("~"), ".claude")]:
        if os.path.isdir(auth_dir):
            auth_args = ["-v", f"{auth_dir}:/root/.claude:ro,Z"]
            break

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
            "stderr": "Orchestrator timed out.",
        }


_STDOUT_PATTERN = re.compile(
    r"^stdout:[ \t]*\n?(.*?)(?=^(?:stderr:|Exit code:|SUCCESS|FAILED|--- Attempt)|\Z)",
    re.MULTILINE | re.DOTALL,
)

_STDERR_PATTERN = re.compile(
    r"^stderr:[ \t]*\n?(.*?)(?=^(?:stdout:|Exit code:|SUCCESS|FAILED|--- Attempt)|\Z)",
    re.MULTILINE | re.DOTALL,
)

_FILES_PATTERN = re.compile(r"(/workspace/[\w./\-]+)")

_UAS_RESULT_PATTERN = re.compile(r"^UAS_RESULT:\s*(\{.*\})\s*$", re.MULTILINE)


def truncate_output(text: str, max_length: int = MAX_CONTEXT_LENGTH) -> str:
    """Truncate text to max_length, appending a note if truncated."""
    if len(text) <= max_length:
        return text
    return text[:max_length] + f"\n... [truncated, {len(text)} chars total]"


def extract_sandbox_stdout(orchestrator_output: str) -> str:
    """Extract the sandbox script's stdout from orchestrator log.

    Uses regex-based extraction. If multiple stdout blocks exist (from
    retries), returns the last one. Output is truncated to
    MAX_CONTEXT_LENGTH.
    """
    matches = list(_STDOUT_PATTERN.finditer(orchestrator_output))
    if not matches:
        return ""
    result = matches[-1].group(1).strip()
    return truncate_output(result)


def extract_sandbox_stderr(orchestrator_output: str) -> str:
    """Extract the sandbox script's stderr from orchestrator log.

    Uses regex-based extraction. If multiple stderr blocks exist (from
    retries), returns the last one. Output is truncated to
    MAX_CONTEXT_LENGTH.
    """
    matches = list(_STDERR_PATTERN.finditer(orchestrator_output))
    if not matches:
        return ""
    result = matches[-1].group(1).strip()
    return truncate_output(result)


TEXT_EXTENSIONS = {
    ".txt", ".csv", ".json", ".py", ".md", ".html", ".xml",
    ".yaml", ".yml", ".log", ".tsv", ".sh", ".cfg", ".ini", ".toml",
}


def _guess_file_type(filename: str) -> str:
    """Classify a file as 'text' or 'binary' based on extension."""
    _, ext = os.path.splitext(filename.lower())
    return "text" if ext in TEXT_EXTENSIONS else "binary"


def scan_workspace_files(workspace_path: str) -> dict:
    """List files in workspace directory (non-recursive).

    Returns dict of {filename: {size, type, preview}} where preview
    is the first 500 chars for text files under 50KB.
    """
    if not os.path.isdir(workspace_path):
        return {}
    results = {}
    for entry in os.listdir(workspace_path):
        if entry.startswith("."):
            continue
        path = os.path.join(workspace_path, entry)
        if not os.path.isfile(path):
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        file_info = {
            "size": stat.st_size,
            "type": _guess_file_type(entry),
            "preview": "",
        }
        if file_info["type"] == "text" and stat.st_size < 50000:
            try:
                with open(path, "r", errors="replace") as f:
                    file_info["preview"] = f.read(500)
            except OSError:
                pass
        results[entry] = file_info
    return results


def extract_workspace_files(orchestrator_output: str) -> list[str]:
    """Extract file paths under /workspace/ mentioned in orchestrator output."""
    matches = _FILES_PATTERN.findall(orchestrator_output)
    seen = set()
    files = []
    for m in matches:
        m = m.rstrip(".,;:)'\"")
        if m not in seen:
            seen.add(m)
            files.append(m)
    return files


def parse_uas_result(orchestrator_output: str) -> dict | None:
    """Extract a UAS_RESULT JSON line from orchestrator output.

    Searches the full orchestrator stderr/stdout for a line matching:
        UAS_RESULT: {"status": "ok", ...}
    Returns the parsed dict or None if not found/invalid.
    """
    import json
    match = _UAS_RESULT_PATTERN.search(orchestrator_output)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
