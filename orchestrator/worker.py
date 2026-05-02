"""Headless Claude Code worker primitive (Phase 3 §2).

Spawns a sandboxed ``claude --print --dangerously-skip-permissions
--output-format stream-json --verbose <prompt>`` invocation against
the ``uas-engine:latest`` image, captures the stream-json output
line-by-line, and returns a structured result dict.

Per ``docs/substrate.md`` §1 the orchestrator calls ``podman run``
directly here rather than going through ``orchestrator/sandbox.py``;
the sandbox primitive's ``run_in_sandbox`` API is shaped for
arbitrary Python snippets, not for the headless-Claude command line
this worker needs.
"""

import json
import os
import re
import subprocess
import threading
import uuid

from integration import auth
from orchestrator import container

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
UAS_AUTH_DIR = os.path.join(REPO_ROOT, ".uas_auth")
CLAUDE_JSON = os.path.join(UAS_AUTH_DIR, "claude.json")

_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_segment(s: str) -> str:
    """Sanitise a segment for use inside a container name."""
    return _NAME_SAFE_RE.sub("-", s).strip("-") or "x"


def _engine_prefix(engine: str) -> list[str]:
    """Return the engine-specific argv prefix.

    ``--storage-driver=vfs`` is podman-specific (mirrors
    ``orchestrator/sandbox.py``); docker rejects the flag, and
    ``integration/eval.py``'s container path already runs without it.
    Gating on the binary basename keeps both engines working.
    """
    base = os.path.basename(engine)
    if base == "podman":
        return [engine, "--storage-driver=vfs"]
    return [engine]


def _kill_container(engine: str, name: str) -> None:
    """Best-effort kill+rm; mirrors orchestrator/sandbox.py precedent."""
    prefix = _engine_prefix(engine)
    for sub in (["kill", name], ["rm", "-f", name]):
        try:
            subprocess.run(
                [*prefix, *sub],
                capture_output=True, timeout=10,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            pass


def _build_command(
    *, engine: str, name: str, workspace: str, prompt: str,
) -> list[str]:
    """Assemble the ``podman run`` argv for the headless worker.

    Sets up workspace + ``.uas_auth`` mounts, forwards ``UAS_HOST_UID``
    / ``UAS_HOST_GID`` so the inline shell wrapper can chown
    ``/workspace`` back to the host on exit (mirroring
    ``entrypoint.sh``), and overrides the image's default entrypoint
    with ``/bin/bash`` running a minimal trap+exec shell. The prompt
    is passed via the ``UAS_WORKER_PROMPT`` env var so callers do not
    have to worry about shell-quoting arbitrary text.
    """
    host_uid = os.environ.get("UAS_HOST_UID", "")
    host_gid = os.environ.get("UAS_HOST_GID", "")

    cmd = [
        *_engine_prefix(engine), "run", "--rm",
        "--network=host",
        "--name", name,
        "--volume", f"{workspace}:/workspace:Z",
        "--volume", f"{UAS_AUTH_DIR}:/root/.claude:Z",
    ]
    if os.path.isfile(CLAUDE_JSON):
        cmd += ["--volume", f"{CLAUDE_JSON}:/root/.claude.json:Z"]

    cmd += [
        "-e", f"UAS_WORKER_PROMPT={prompt}",
        "-e", "CLAUDE_CONFIG_DIR=/root/.claude",
    ]
    if host_uid:
        cmd += ["-e", f"UAS_HOST_UID={host_uid}"]
    if host_gid:
        cmd += ["-e", f"UAS_HOST_GID={host_gid}"]

    cmd += [
        "--entrypoint", "/bin/bash",
        container.IMAGE_TAG,
        "-c",
        (
            'set -e; '
            'if [ -n "${UAS_HOST_UID:-}" ] && [ "$UAS_HOST_UID" != "0" ]; '
            'then trap '
            '"chown -R $UAS_HOST_UID:${UAS_HOST_GID:-$UAS_HOST_UID} '
            '/workspace 2>/dev/null || true" EXIT; '
            'fi; '
            'cd /workspace; '
            'exec claude --print --dangerously-skip-permissions '
            '--output-format stream-json --verbose '
            '"$UAS_WORKER_PROMPT"'
        ),
    ]
    return cmd


def spawn_worker(
    prompt: str,
    *,
    workspace: str,
    task_id: str,
    subtask_id: str,
    timeout_seconds: int | None = None,
) -> dict:
    """Spawn one headless Claude Code worker and return its result.

    Refreshes OAuth via ``integration.auth._maybe_refresh_oauth`` and
    ensures ``uas-engine:latest`` exists before the spawn (substrate
    §1 Gap precondition). Reads the worker's stream-json output
    line-by-line on a background thread so a long-running spawn
    cannot block the reader's pipe buffer.

    Returns a dict with keys ``exit_code``, ``rate_limit_events``,
    ``result``, ``output``, ``raw_lines``. On timeout the container
    is killed (fire-and-forget ``podman kill`` + ``podman rm -f``)
    and the returned ``result`` carries
    ``{"terminal_reason": "timeout", ...}``; on hard failure before
    the terminal stream-json ``result`` event ``result`` is ``None``.
    """
    auth._maybe_refresh_oauth()
    container.ensure_engine_image()
    engine = container.find_engine()

    name = (
        f"uas-orchestrator-{_safe_segment(task_id)}-"
        f"{_safe_segment(subtask_id)}-{uuid.uuid4().hex[:8]}"
    )
    cmd = _build_command(
        engine=engine, name=name, workspace=workspace, prompt=prompt,
    )

    raw_lines: list[str] = []
    rate_limit_events: list[dict] = []
    output_chunks: list[str] = []
    state = {"result": None}

    def consume(stream) -> None:
        for raw in stream:
            line = raw.rstrip("\n")
            if not line:
                continue
            raw_lines.append(line)
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            evt_type = obj.get("type")
            if evt_type == "rate_limit_event":
                rate_limit_events.append(obj)
            elif evt_type == "result":
                state["result"] = obj
            elif evt_type == "assistant":
                msg = obj.get("message", {}) or {}
                for block in msg.get("content", []) or []:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "text"
                    ):
                        text = block.get("text", "")
                        if text:
                            output_chunks.append(text)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        stdin=subprocess.DEVNULL,
    )
    reader = threading.Thread(target=consume, args=(proc.stdout,))
    reader.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_container(engine, name)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    finally:
        reader.join(timeout=5)
        if proc.stdout is not None:
            proc.stdout.close()

    if timed_out:
        return {
            "exit_code": -1,
            "rate_limit_events": rate_limit_events,
            "result": {
                "terminal_reason": "timeout",
                "subtask_id": subtask_id,
                "container_name": name,
            },
            "output": "".join(output_chunks),
            "raw_lines": raw_lines,
        }

    return {
        "exit_code": proc.returncode if proc.returncode is not None else -1,
        "rate_limit_events": rate_limit_events,
        "result": state["result"],
        "output": "".join(output_chunks),
        "raw_lines": raw_lines,
    }
