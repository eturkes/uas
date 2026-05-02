"""Engine-image precondition for Phase 3 headless workers.

Builds ``uas-engine:latest`` from the repo-root ``Containerfile`` when
the image is missing locally. Idempotent: when the image already
exists the call is a no-op. Resolves substrate doc §1 Gap ("Phase 3
needs an explicit 'image is ready' precondition").

Staleness vs. source-tree mtime is intentionally **not** checked here.
``integration/eval.py::_ensure_image`` does that for the eval harness;
the orchestrator's contract is narrower — guarantee the image exists
before a worker spawn — so we keep the precondition mechanical and
predictable. Operators who want a rebuild remove the image (``podman
rmi uas-engine:latest``) or rely on the eval harness's path.
"""

import os
import shutil
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
CONTAINERFILE = os.path.join(REPO_ROOT, "Containerfile")
IMAGE_TAG = "uas-engine:latest"


class EngineUnavailable(RuntimeError):
    """Raised when no container engine is on PATH."""


def find_engine() -> str:
    """Return the container-engine binary path (podman preferred, docker fallback)."""
    for cmd in ("podman", "docker"):
        path = shutil.which(cmd)
        if path:
            return path
    raise EngineUnavailable(
        "No container engine found on PATH (looked for podman, docker)."
    )


def _image_exists(engine: str) -> bool:
    """Return True iff ``uas-engine:latest`` is present locally."""
    try:
        proc = subprocess.run(
            [engine, "image", "inspect", IMAGE_TAG],
            capture_output=True, timeout=10, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def ensure_engine_image() -> None:
    """Build ``uas-engine:latest`` from ``Containerfile`` if absent.

    Raises ``EngineUnavailable`` when no engine is on PATH and
    ``subprocess.CalledProcessError`` when the build itself fails.
    """
    engine = find_engine()
    if _image_exists(engine):
        return
    if not os.path.isfile(CONTAINERFILE):
        raise FileNotFoundError(
            f"Containerfile missing at {CONTAINERFILE}; cannot build "
            f"{IMAGE_TAG}."
        )
    print(
        f"Building {IMAGE_TAG} (absent locally)...", file=sys.stderr,
    )
    subprocess.run(
        [engine, "build", "-t", IMAGE_TAG,
         "-f", CONTAINERFILE, REPO_ROOT],
        check=True, stdin=subprocess.DEVNULL,
    )
