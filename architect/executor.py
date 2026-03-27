"""Interface to the Orchestrator: local subprocess or container modes."""

import ast
import csv
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time as _time

from orchestrator.claude_config import get_claude_md_content
from orchestrator.llm_client import heartbeat_log
from .events import EventType, get_event_log
from .provenance import get_provenance_graph

SANDBOX_IMAGE_NAME = "uas-sandbox"
SANDBOX_TARBALL = "/var/lib/containers/sandbox.tar"
MAX_CONTEXT_LENGTH = int(os.environ.get("UAS_MAX_CONTEXT_LENGTH", "0"))
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

    Prefers loading from a pre-built tarball (created by install.sh on
    the host) to avoid running podman build inside a container.  Falls
    back to building in-place for local development without containers.
    """
    check = subprocess.run(
        _podman_cmd(engine, "image", "inspect", SANDBOX_IMAGE_NAME),
        capture_output=True,
    )
    if check.returncode == 0:
        return

    # Prefer pre-built tarball (created by install.sh on the host).
    if os.path.isfile(SANDBOX_TARBALL):
        logger.info("  Loading sandbox image from pre-built tarball...")
        subprocess.run(
            _podman_cmd(engine, "load", "-i", SANDBOX_TARBALL),
            check=True,
            capture_output=True,
            text=True,
        )
        return

    # Fallback: build in-place (works when running outside containers).
    framework_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

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
                    engine, "build", "--network=host",
                    "-t", SANDBOX_IMAGE_NAME,
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


def ensure_claude_md(workspace: str, step_context: dict | None = None) -> None:
    """Write .claude/CLAUDE.md to the workspace if missing or outdated."""
    claude_dir = os.path.join(workspace, ".claude")
    claude_md_path = os.path.join(claude_dir, "CLAUDE.md")
    content = get_claude_md_content(step_context=step_context)
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


def run_orchestrator(task: str, extra_env: dict | None = None,
                     output_callback=None,
                     step_context: dict | None = None) -> dict:
    """Run the Orchestrator with the given task.

    Args:
        task: The task string to pass to the orchestrator.
        extra_env: Optional extra environment variables (e.g. UAS_STEP_ID).
        output_callback: Optional callable(line: str) invoked for each stderr
            line as it arrives, enabling real-time output in the dashboard.
        step_context: Optional dict with step metadata for dynamic CLAUDE.md.

    Returns dict with exit_code, stdout, stderr.
    """
    workspace = os.environ.get("UAS_WORKSPACE", "/workspace")
    try:
        ensure_claude_md(workspace, step_context=step_context)
    except OSError as e:
        logger.warning("Could not write .claude/CLAUDE.md: %s", e)

    event_log = get_event_log()
    event_log.emit(EventType.SANDBOX_START, data={"mode": EXECUTION_MODE})
    sandbox_start = _time.monotonic()

    with heartbeat_log("Orchestrator running", interval=30, log=logger):
        if EXECUTION_MODE == "local":
            result = _run_local(task, extra_env, output_callback)
        else:
            result = _run_container(task, extra_env, output_callback)

    sandbox_elapsed = _time.monotonic() - sandbox_start
    event_log.emit(EventType.SANDBOX_COMPLETE,
                   duration=sandbox_elapsed,
                   data={"exit_code": result["exit_code"]})
    return result


def _run_streaming(cmd, env=None, cwd=None, callback=None,
                   container_cleanup=None) -> dict:
    """Run a subprocess, streaming stderr lines to callback in real time.

    Args:
        cmd: Command list.
        env: Environment dict (uses current env if None).
        cwd: Working directory.
        callback: callable(line: str) invoked for each stderr line.
        container_cleanup: Optional (engine, name) tuple; if the process
            times out, kill and remove the container.

    Returns dict with exit_code, stdout, stderr.
    """
    import threading as _threading

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            stdin=subprocess.DEVNULL,
            env=env,
            cwd=cwd,
        )
    except FileNotFoundError as e:
        return {"exit_code": -1, "stdout": "", "stderr": str(e)}

    # Read stdout in a background thread to avoid deadlocks
    stdout_chunks: list[str] = []

    def _read_stdout():
        stdout_chunks.append(proc.stdout.read())

    stdout_thread = _threading.Thread(target=_read_stdout, daemon=True)
    stdout_thread.start()

    # Stream stderr line by line, invoking callback
    stderr_lines: list[str] = []
    try:
        for line in proc.stderr:
            stderr_lines.append(line)
            if callback:
                callback(line.rstrip("\n"))

        proc.wait(timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        if container_cleanup:
            _kill_container(*container_cleanup)
        return {"exit_code": -1, "stdout": "", "stderr": "Orchestrator timed out."}

    stdout_thread.join(timeout=5)
    stdout = stdout_chunks[0] if stdout_chunks else ""
    stderr = "".join(stderr_lines)
    return {"exit_code": proc.returncode, "stdout": stdout, "stderr": stderr}


def _run_local(task: str, extra_env: dict | None = None,
               output_callback=None) -> dict:
    """Run the Orchestrator as a local subprocess (no container)."""
    framework_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..")
    )
    workspace = os.environ.get("UAS_WORKSPACE", "/workspace")
    cwd = workspace
    env = os.environ.copy()
    env["PYTHONPATH"] = framework_root
    env["IS_SANDBOX"] = "1"
    env["UAS_TASK"] = task
    if extra_env:
        env.update(extra_env)

    if output_callback:
        return _run_streaming(
            [sys.executable, "-m", "orchestrator.main"],
            env=env, cwd=cwd, callback=output_callback,
        )

    try:
        result = subprocess.run(
            [sys.executable, "-m", "orchestrator.main"],
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT,
            cwd=cwd,
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


def _project_id() -> str:
    """Derive a short project identifier from the host workspace path."""
    host_ws = os.environ.get("UAS_HOST_WORKSPACE", "")
    if not host_ws:
        host_ws = os.environ.get("UAS_WORKSPACE", "/workspace")
    return hashlib.sha256(host_ws.encode()).hexdigest()[:12]


def _project_container_name() -> str:
    """Deterministic container name for the current project."""
    return f"uas-project-{_project_id()}"


def _project_image_name() -> str:
    """Deterministic image name for committed project containers."""
    return f"uas-project-{_project_id()}"


def _ensure_project_container(engine: str) -> str:
    """Ensure the persistent project container exists and is running.

    Creates from a previously committed project image if available,
    otherwise from the uas-sandbox base image.  The container stays
    alive across steps so installed packages persist.
    """
    name = _project_container_name()
    workspace = os.environ.get("UAS_WORKSPACE", "/workspace")

    # Check if container already exists
    result = subprocess.run(
        _podman_cmd(engine, "container", "inspect",
                    "--format", "{{.State.Running}}", name),
        capture_output=True, text=True,
    )

    if result.returncode == 0:
        if result.stdout.strip().lower() == "true":
            return name
        # Exists but stopped — start it
        subprocess.run(
            _podman_cmd(engine, "start", name),
            check=True, capture_output=True,
        )
        return name

    # Container doesn't exist — check for a committed project image
    project_image = _project_image_name()
    base_image = SANDBOX_IMAGE_NAME

    check = subprocess.run(
        _podman_cmd(engine, "image", "inspect", project_image),
        capture_output=True,
    )
    if check.returncode == 0:
        base_image = project_image
        logger.info("  Reusing committed project image: %s", project_image)

    # Mount auth credentials for Claude CLI
    auth_args: list[str] = []
    for auth_dir in ["/root/.claude",
                     os.path.join(os.path.expanduser("~"), ".claude")]:
        if os.path.isdir(auth_dir):
            auth_args = ["-v", f"{auth_dir}:/root/.claude:Z"]
            break

    cmd = _podman_cmd(
        engine, "run", "-d",
        "--network=host",
        "--name", name,
        "-v", f"{workspace}:/workspace:Z",
    ) + auth_args + [
        base_image,
        "sleep", "infinity",
    ]

    subprocess.run(cmd, check=True, capture_output=True, text=True)
    logger.info("  Created persistent project container: %s", name)
    return name


def commit_project_image():
    """Commit the persistent project container as a reusable image.

    Saves the container state (including installed packages) as a
    project-specific image, then stops and removes the container.
    """
    engine = find_engine()
    if not engine:
        return

    name = _project_container_name()
    image = _project_image_name()

    # Check if container exists
    result = subprocess.run(
        _podman_cmd(engine, "container", "inspect", name),
        capture_output=True,
    )
    if result.returncode != 0:
        return

    try:
        subprocess.run(
            _podman_cmd(engine, "commit", name, image),
            check=True, capture_output=True, text=True,
        )
        logger.info("  Committed project image: %s", image)
    except subprocess.CalledProcessError as e:
        logger.warning("  Failed to commit project image: %s",
                       e.stderr if e.stderr else str(e))

    _stop_project_container(engine, name)


def _stop_project_container(engine: str, name: str):
    """Stop and remove the persistent project container."""
    try:
        subprocess.run(
            _podman_cmd(engine, "stop", "-t", "5", name),
            capture_output=True, timeout=30,
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


def _run_container(task: str, extra_env: dict | None = None,
                   output_callback=None) -> dict:
    """Run the Orchestrator inside the persistent project container."""
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

    try:
        container_name = _ensure_project_container(engine)
    except subprocess.CalledProcessError as e:
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Failed to create project container: {e.stderr or str(e)}",
        }

    # Build env args for podman exec
    env_args = []
    for var in [
        "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL",
        "UAS_SANDBOX_IMAGE", "UAS_SANDBOX_TIMEOUT",
        "UAS_LLM_TIMEOUT", "UAS_MODEL", "UAS_VERBOSE",
        "UAS_HOST_UID", "UAS_HOST_GID",
        # Section 5c: Model tiering env vars
        "UAS_MODEL_PLANNER", "UAS_MODEL_CODER",
        # Package requirements and best-of-N for orchestrator
        "UAS_BEST_OF_N", "UAS_STEP_ENVIRONMENT",
    ]:
        val = os.environ.get(var)
        if val:
            env_args.extend(["-e", f"{var}={val}"])

    env_args.extend(["-e", f"UAS_TASK={task}"])
    env_args.extend(["-e", "PYTHONPATH=/uas"])
    env_args.extend(["-e", "IS_SANDBOX=1"])
    env_args.extend(["-e", "HOME=/root"])

    # Force local sandbox mode inside the container since this lightweight
    # image does not have Podman -- the container itself provides isolation.
    env_args.extend(["-e", "UAS_SANDBOX_MODE=local"])

    if extra_env:
        for k, v in extra_env.items():
            env_args.extend(["-e", f"{k}={v}"])

    cmd = _podman_cmd(
        engine, "exec",
        "-w", "/workspace",
    ) + env_args + [
        container_name,
        "python3", "-m", "orchestrator.main",
    ]

    if output_callback:
        # No container_cleanup — don't destroy the persistent container
        return _run_streaming(cmd, callback=output_callback)

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
            "  Container exec failed (exit %d).", e.returncode
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

# Section 5d: Delimited output patterns (preferred over regex).
_DELIMITED_STDOUT = re.compile(
    r"===STDOUT_START===\n(.*?)\n===STDOUT_END===",
    re.DOTALL,
)
_DELIMITED_STDERR = re.compile(
    r"===STDERR_START===\n(.*?)\n===STDERR_END===",
    re.DOTALL,
)

_FILES_PATTERN = re.compile(r"(/workspace/[\w./\-]+)")

_UAS_RESULT_PATTERN = re.compile(
    r"^UAS_RESULT:\s*(\{.*\})\s*$", re.MULTILINE | re.IGNORECASE,
)


def truncate_output(text: str, max_length: int = MAX_CONTEXT_LENGTH) -> str:
    """Truncate text to max_length, appending a note if truncated."""
    if max_length <= 0 or len(text) <= max_length:
        return text
    return text[:max_length] + f"\n... [truncated, {len(text)} chars total]"


def extract_sandbox_stdout(orchestrator_output: str) -> str:
    """Extract the sandbox script's stdout from orchestrator log.

    Prefers Section 5d delimited markers (``===STDOUT_START===`` /
    ``===STDOUT_END===``).  Falls back to regex-based extraction for
    backwards compatibility.  If multiple blocks exist (from retries),
    returns the last one.
    """
    # Section 5d: Try delimited extraction first
    delimited = list(_DELIMITED_STDOUT.finditer(orchestrator_output))
    if delimited:
        result = delimited[-1].group(1).strip()
        return truncate_output(result)

    # Fallback to regex
    matches = list(_STDOUT_PATTERN.finditer(orchestrator_output))
    if not matches:
        return ""
    result = matches[-1].group(1).strip()
    return truncate_output(result)


def extract_sandbox_stderr(orchestrator_output: str) -> str:
    """Extract the sandbox script's stderr from orchestrator log.

    Prefers Section 5d delimited markers (``===STDERR_START===`` /
    ``===STDERR_END===``).  Falls back to regex-based extraction for
    backwards compatibility.  If multiple blocks exist (from retries),
    returns the last one.
    """
    # Section 5d: Try delimited extraction first
    delimited = list(_DELIMITED_STDERR.finditer(orchestrator_output))
    if delimited:
        result = delimited[-1].group(1).strip()
        return truncate_output(result)

    # Fallback to regex
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


_SKIP_DIRS = {".uas_state", ".git", "__pycache__", "node_modules", "venv", ".venv"}
_MAX_SCAN_OUTPUT = 4000


def scan_workspace_files(workspace_path: str, recursive: bool = True,
                         max_depth: int = 3) -> dict:
    """List files in workspace directory, optionally recursive.

    Scans up to max_depth levels deep (Section 4b). Skips hidden dirs
    and common non-essential directories (.uas_state, .git, __pycache__,
    node_modules, venv).

    Returns dict of {relative_path: {size, type, preview}} where preview
    is the first 200 chars for text files under 50KB. Files are grouped
    by directory in the output.
    """
    if not os.path.isdir(workspace_path):
        return {}
    results = {}
    total_output_size = 0

    def _scan_dir(dir_path: str, depth: int):
        nonlocal total_output_size
        if depth > max_depth or total_output_size >= _MAX_SCAN_OUTPUT:
            return
        try:
            entries = sorted(os.listdir(dir_path))
        except OSError:
            return
        for entry in entries:
            if total_output_size >= _MAX_SCAN_OUTPUT:
                return
            if entry.startswith("."):
                continue
            full_path = os.path.join(dir_path, entry)
            if os.path.isdir(full_path):
                if recursive and entry not in _SKIP_DIRS:
                    _scan_dir(full_path, depth + 1)
                continue
            if not os.path.isfile(full_path):
                continue
            try:
                stat = os.stat(full_path)
            except OSError:
                continue
            rel_path = os.path.relpath(full_path, workspace_path)
            file_info = {
                "size": stat.st_size,
                "type": _guess_file_type(entry),
                "preview": "",
            }
            if entry.endswith((".csv", ".tsv")):
                # Always read at least the header row for CSV/TSV regardless
                # of file size — column names are the critical data contract.
                try:
                    with open(full_path, "r", errors="replace") as f:
                        file_info["preview"] = f.readline() + f.readline()
                except OSError:
                    pass
            elif entry.endswith(".json") and stat.st_size < 50000:
                # Read the full JSON file (up to 50KB) so the key
                # extractor can map the complete schema.  The 200-char
                # default is far too short for nested JSON — without the
                # full structure, the coder guesses wrong key paths.
                try:
                    with open(full_path, "r", errors="replace") as f:
                        file_info["preview"] = f.read()
                except OSError:
                    pass
            elif file_info["type"] == "text" and stat.st_size < 50000:
                try:
                    with open(full_path, "r", errors="replace") as f:
                        file_info["preview"] = f.read(200)
                except OSError:
                    pass
            results[rel_path] = file_info
            # Estimate output size to cap at _MAX_SCAN_OUTPUT
            total_output_size += len(rel_path) + 40 + len(file_info["preview"])

    _scan_dir(workspace_path, 0)
    return results


def _extract_csv_columns(rel_path: str, ws_files: dict) -> str:
    """Extract column headers from a CSV file's preview.

    Parses the first line of the preview to get column names, giving
    downstream steps the exact schema to code against.
    """
    info = ws_files.get(rel_path, {})
    preview = info.get("preview", "")
    if not preview:
        return "(no preview)"
    first_line = preview.split("\n", 1)[0].strip()
    if not first_line:
        return "(empty header)"
    sep = "\t" if rel_path.endswith(".tsv") else ","
    cols = [c.strip().strip('"').strip("'") for c in first_line.split(sep)]
    result = str(cols)
    # Cap output length but always include the count
    if len(result) > 2000:
        result = result[:2000] + f"... ] ({len(cols)} columns total)"
    return f"{result} ({len(cols)} columns)"


def format_workspace_scan(ws_files: dict,
                          json_key_extractor=None) -> str:
    """Format workspace scan results grouped by directory.

    Args:
        ws_files: Dict from scan_workspace_files.
        json_key_extractor: Optional callable(preview_str) -> str for
            extracting JSON keys from .json file previews.

    Returns a string suitable for inclusion in context, capped at
    _MAX_SCAN_OUTPUT chars.
    """
    if not ws_files:
        return ""
    # Group files by directory
    by_dir: dict[str, list[tuple[str, dict]]] = {}
    for fpath, info in sorted(ws_files.items()):
        dirname = os.path.dirname(fpath) or "."
        by_dir.setdefault(dirname, []).append((fpath, info))

    lines = []
    total_len = 0
    for dirname in sorted(by_dir.keys()):
        if total_len >= _MAX_SCAN_OUTPUT:
            lines.append("  ... [scan output capped]")
            break
        if dirname != ".":
            lines.append(f"  [{dirname}/]")
        for fpath, info in by_dir[dirname]:
            fname = os.path.basename(fpath) if dirname != "." else fpath
            line = f"  {fname} ({info['size']} bytes, {info['type']})"
            preview = info.get("preview", "")
            if preview:
                if fpath.endswith(".json") and json_key_extractor:
                    line += f"\n    keys: {json_key_extractor(preview)}"
                elif fpath.endswith((".csv", ".tsv")):
                    line += f"\n    columns: {_extract_csv_columns(fpath, ws_files)}"
                else:
                    line += f"\n    preview: {preview[:200]}"
            if total_len + len(line) > _MAX_SCAN_OUTPUT:
                lines.append("  ... [scan output capped]")
                total_len = _MAX_SCAN_OUTPUT
                break
            lines.append(line)
            total_len += len(line)

    return "\n".join(lines)


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
    Uses the **last** match, since scripts are instructed to print
    UAS_RESULT as the final line of stdout.  When a script runs
    sub-scripts, their UAS_RESULT lines may also appear in the
    output; the last one is the authoritative result.
    Tolerates case variations, missing space after colon, and
    single-quoted JSON as a fallback.
    Returns the parsed dict or None if not found/invalid.
    """
    import json
    matches = list(_UAS_RESULT_PATTERN.finditer(orchestrator_output))
    if not matches:
        return None
    # Try matches from last to first, returning the first parseable one.
    for match in reversed(matches):
        raw = match.group(1)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            pass
        # Fallback: replace single quotes with double quotes
        try:
            return json.loads(raw.replace("'", '"'))
        except (json.JSONDecodeError, ValueError):
            continue
    return None


# ---------------------------------------------------------------------------
# Section 4 — File signature extraction
# ---------------------------------------------------------------------------

def extract_file_signatures(files_written: list[str],
                            max_chars_per_file: int = 2000) -> str:
    """Extract structural signatures from files produced by a step.

    For .py files: function signatures with parameter types, class outlines
    with method signatures, module-level constants, and docstring excerpts.
    For .csv/.tsv files: column names and row count.
    For .json files: top-level keys and first 3 list entries.

    Returns a structured string suitable for inclusion in ``<file_signatures>``
    XML blocks.  Each file's output is capped at *max_chars_per_file* chars.
    """
    parts = []
    for fpath in files_written:
        if not os.path.isfile(fpath):
            continue
        ext = os.path.splitext(fpath)[1].lower()
        sig = ""
        if ext == ".py":
            sig = _extract_py_signatures(fpath)
        elif ext in (".csv", ".tsv"):
            sig = _extract_csv_file_signatures(fpath, ext)
        elif ext == ".json":
            sig = _extract_json_file_signatures(fpath)
        if not sig:
            continue
        if len(sig) > max_chars_per_file:
            sig = sig[:max_chars_per_file] + "\n    ... [truncated]"
        parts.append(f'  <file path="{fpath}">\n{sig}\n  </file>')
    return "\n".join(parts)


def _format_func_sig(node) -> str:
    """Format a function/method signature from an AST node."""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    params = []
    args = node.args

    # Positional-only args
    for arg in args.posonlyargs:
        p = arg.arg
        if arg.annotation:
            p += f": {ast.unparse(arg.annotation)}"
        params.append(p)
    if args.posonlyargs:
        params.append("/")

    # Regular args
    defaults_offset = len(args.args) - len(args.defaults)
    for i, arg in enumerate(args.args):
        p = arg.arg
        if arg.annotation:
            p += f": {ast.unparse(arg.annotation)}"
        if i >= defaults_offset:
            p += " = ..."
        params.append(p)

    # *args
    if args.vararg:
        p = f"*{args.vararg.arg}"
        if args.vararg.annotation:
            p += f": {ast.unparse(args.vararg.annotation)}"
        params.append(p)
    elif args.kwonlyargs:
        params.append("*")

    # Keyword-only args
    for i, arg in enumerate(args.kwonlyargs):
        p = arg.arg
        if arg.annotation:
            p += f": {ast.unparse(arg.annotation)}"
        if args.kw_defaults[i] is not None:
            p += " = ..."
        params.append(p)

    # **kwargs
    if args.kwarg:
        p = f"**{args.kwarg.arg}"
        if args.kwarg.annotation:
            p += f": {ast.unparse(args.kwarg.annotation)}"
        params.append(p)

    ret = ""
    if node.returns:
        ret = f" -> {ast.unparse(node.returns)}"
    return f"{prefix} {node.name}({', '.join(params)}){ret}"


def _extract_py_signatures(fpath: str) -> str:
    """Extract function/class/constant signatures from a Python file."""
    try:
        with open(fpath, encoding="utf-8", errors="replace") as f:
            source = f.read()
        tree = ast.parse(source, filename=fpath)
    except Exception:
        return ""

    lines = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            lines.append(f"    {_format_func_sig(node)}")
            doc = ast.get_docstring(node)
            if doc:
                for dl in doc.strip().split("\n")[:2]:
                    lines.append(f"      # {dl.strip()}")

        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            lines.append(f"    class {node.name}:")
            doc = ast.get_docstring(node)
            if doc:
                for dl in doc.strip().split("\n")[:2]:
                    lines.append(f"      # {dl.strip()}")
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not child.name.startswith("_") or child.name in (
                        "__init__", "__call__", "__repr__", "__str__",
                    ):
                        lines.append(f"      {_format_func_sig(child)}")

        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if target.id.isupper() or not target.id.startswith("_"):
                        lines.append(f"    {target.id} = ...")

        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                name = node.target.id
                if name.isupper() or not name.startswith("_"):
                    ann = ast.unparse(node.annotation)
                    lines.append(f"    {name}: {ann}")

    return "\n".join(lines)


def _extract_csv_file_signatures(fpath: str, ext: str) -> str:
    """Extract column names and row count from a CSV/TSV file."""
    try:
        sep = "\t" if ext == ".tsv" else ","
        with open(fpath, encoding="utf-8", errors="replace",
                  newline="") as f:
            reader = csv.reader(f, delimiter=sep)
            header = next(reader, None)
            if not header:
                return ""
            row_count = sum(1 for _ in reader)
        cols_str = ", ".join(header)
        return (f"    columns: [{cols_str}] ({len(header)} columns)\n"
                f"    rows: {row_count}")
    except Exception:
        return ""


def _extract_json_file_signatures(fpath: str) -> str:
    """Extract top-level keys and list structure from a JSON file."""
    try:
        with open(fpath, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except Exception:
        return ""

    if isinstance(data, dict):
        keys = list(data.keys())
        lines = [f"    keys: {keys}"]
        for k, v in list(data.items())[:5]:
            if isinstance(v, list):
                lines.append(f"    {k}: list[{len(v)} items]")
                for item in v[:3]:
                    if isinstance(item, dict):
                        lines.append(
                            f"      - {{{', '.join(item.keys())}}}")
                    else:
                        lines.append(f"      - {type(item).__name__}")
            elif isinstance(v, dict):
                lines.append(f"    {k}: {{{', '.join(v.keys())}}}")
        return "\n".join(lines)

    if isinstance(data, list):
        lines = [f"    list[{len(data)} items]"]
        for item in data[:3]:
            if isinstance(item, dict):
                lines.append(f"      - {{{', '.join(item.keys())}}}")
            else:
                lines.append(f"      - {type(item).__name__}")
        return "\n".join(lines)

    return f"    {type(data).__name__}: {str(data)[:200]}"
