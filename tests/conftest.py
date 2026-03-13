"""Shared fixtures for UAS tests."""

import datetime
import glob as globmod
import json
import os
import re
import shutil
import subprocess
import sys
import threading

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UAS_AUTH_DIR = os.path.join(PROJECT_ROOT, ".uas_auth")
CLAUDE_JSON = os.path.join(UAS_AUTH_DIR, "claude.json")
IMAGE_TAG = "uas-engine:latest"


@pytest.fixture
def tmp_workspace(tmp_path, monkeypatch):
    """Provide a temporary workspace directory and patch UAS_WORKSPACE."""
    monkeypatch.setenv("UAS_WORKSPACE", str(tmp_path))
    import architect.state as state_mod

    state_dir = os.path.join(str(tmp_path), ".state")
    scratchpad_file = os.path.join(state_dir, "scratchpad.md")

    monkeypatch.setattr(state_mod, "WORKSPACE", str(tmp_path))
    monkeypatch.setattr(state_mod, "STATE_DIR", state_dir)
    monkeypatch.setattr(state_mod, "SCRATCHPAD_FILE", scratchpad_file)
    return tmp_path


def _has_valid_auth():
    """Check whether .uas_auth/ contains valid Claude CLI credentials."""
    cred_file = os.path.join(UAS_AUTH_DIR, ".credentials.json")
    if not os.path.isfile(cred_file):
        return False
    try:
        with open(cred_file, "r", encoding="utf-8") as f:
            creds = json.load(f)
        return bool(creds)
    except (json.JSONDecodeError, OSError):
        return False


# ---------------------------------------------------------------------------
# Container helpers
# ---------------------------------------------------------------------------

def find_engine():
    """Return 'podman' or 'docker', whichever is found first."""
    for cmd in ["podman", "docker"]:
        if shutil.which(cmd):
            return cmd
    return None


def _image_build_time(engine):
    """Return uas-engine image creation time as a Unix timestamp, or 0."""
    try:
        r = subprocess.run(
            [engine, "image", "inspect", IMAGE_TAG,
             "--format", "{{.Created}}"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return 0.0
        raw = r.stdout.strip()
        # Truncate Go nanoseconds (9 digits) to Python microseconds (6).
        raw = re.sub(r'(\.\d{6})\d+', r'\1', raw)
        raw = raw.replace('Z', '+00:00')
        return datetime.datetime.fromisoformat(raw).timestamp()
    except Exception:
        return 0.0


def _latest_source_mtime():
    """Newest mtime among files baked into the container image."""
    patterns = [
        "Containerfile", "requirements.txt", "entrypoint.sh",
        "architect/*.py", "orchestrator/*.py",
    ]
    latest = 0.0
    for pattern in patterns:
        for path in globmod.glob(os.path.join(PROJECT_ROOT, pattern)):
            latest = max(latest, os.path.getmtime(path))
    return latest


def ensure_image(engine):
    """Rebuild uas-engine:latest if it is missing or stale."""
    build_time = _image_build_time(engine)
    if build_time > 0 and build_time >= _latest_source_mtime():
        return
    print(
        "\n  Rebuilding uas-engine:latest (image is stale or missing)...\n",
        flush=True,
    )
    subprocess.run(
        [engine, "build", "-t", IMAGE_TAG,
         "-f", os.path.join(PROJECT_ROOT, "Containerfile"), PROJECT_ROOT],
        check=True,
    )


def run_in_container(engine, cmd, *, env=None, workspace=None, timeout=120):
    """Run *cmd* inside uas-engine, streaming output to the terminal.

    Returns ``(stdout, stderr, returncode)``.
    """
    container_cmd = [
        engine, "run", "--rm",
        "--privileged",
        "-e", "IS_SANDBOX=1",
        "-v", f"{UAS_AUTH_DIR}:/root/.claude:Z",
        "-v", f"{CLAUDE_JSON}:/root/.claude.json:Z",
    ]
    if workspace:
        container_cmd.extend(["-v", f"{workspace}:/workspace:Z"])
    if env:
        for k, v in env.items():
            container_cmd.extend(["-e", f"{k}={v}"])
    container_cmd.extend(["--entrypoint", "", "-w", "/uas", IMAGE_TAG])
    container_cmd.extend(cmd)

    proc = subprocess.Popen(
        container_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        stdin=subprocess.DEVNULL,
    )

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def _reader(stream, dest, fd):
        for line in stream:
            dest.append(line)
            fd.write(line)
            fd.flush()

    t_out = threading.Thread(
        target=_reader, args=(proc.stdout, stdout_lines, sys.stdout),
        daemon=True,
    )
    t_err = threading.Thread(
        target=_reader, args=(proc.stderr, stderr_lines, sys.stderr),
        daemon=True,
    )
    t_out.start()
    t_err.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise

    t_out.join(timeout=5)
    t_err.join(timeout=5)

    return "".join(stdout_lines), "".join(stderr_lines), proc.returncode


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def require_auth():
    """Ensure Claude CLI auth is available for integration tests.

    Credentials are stored in the repo-local ``.uas_auth/`` directory
    (gitignored), completely separate from the host ``~/.claude/`` config.

    If credentials are missing, the test is skipped with instructions
    to run ``bash setup_auth.sh``.
    """
    if not _has_valid_auth():
        pytest.skip(
            "No credentials found in .uas_auth/. "
            "Run `bash setup_auth.sh` to authenticate, then re-run tests."
        )

    # Seed claude.json if missing.
    if not os.path.isfile(CLAUDE_JSON):
        with open(CLAUDE_JSON, "w", encoding="utf-8") as f:
            f.write("{}")

    # Print active config so the user can verify the right model/settings.
    settings_file = os.path.join(UAS_AUTH_DIR, "settings.json")
    settings = {}
    if os.path.isfile(settings_file):
        with open(settings_file, "r", encoding="utf-8") as f:
            settings = json.load(f)
    print(
        f"\n"
        f"  UAS Auth\n"
        f"  Config dir    = {UAS_AUTH_DIR}\n"
        f"  settings.json = {json.dumps(settings)}\n",
        flush=True,
    )

    return UAS_AUTH_DIR


@pytest.fixture(scope="session")
def uas_engine():
    """Provide a container engine name, rebuilding the image if stale."""
    engine = find_engine()
    if engine is None:
        pytest.skip("No container engine found (podman or docker required)")
    ensure_image(engine)
    return engine
