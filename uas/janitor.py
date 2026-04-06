"""Post-edit formatting and linting for workspace files.

Provides two functions:

- ``format_workspace``: runs ``ruff format`` (or ``black``) on workspace files.
- ``lint_workspace``: runs ``ruff check --select=F`` and returns fatal errors.
"""

import glob
import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)


def _find_formatter() -> str | None:
    """Return the name of the first available formatter, or None."""
    for tool in ("ruff", "black"):
        if shutil.which(tool):
            return tool
    return None


def format_workspace(
    workspace: str, files: list[str] | None = None
) -> None:
    """Format Python files in *workspace* using ``ruff format`` or ``black``.

    If *files* is ``None``, all ``.py`` files under *workspace* are formatted.
    Falls back to ``black`` when ``ruff`` is unavailable, and to a no-op when
    neither formatter is installed.
    """
    formatter = _find_formatter()
    if formatter is None:
        logger.info("No formatter available (ruff/black); skipping format")
        return

    if files is None:
        files = glob.glob("**/*.py", root_dir=workspace, recursive=True)
    if not files:
        return

    if formatter == "ruff":
        cmd = ["ruff", "format", "--quiet", "--"] + files
    else:
        cmd = ["black", "--quiet", "--"] + files

    logger.debug("Running %s on %d files", formatter, len(files))
    proc = subprocess.run(
        cmd,
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        logger.warning("%s exited %d: %s", formatter, proc.returncode, proc.stderr)


def lint_workspace(
    workspace: str, files: list[str] | None = None
) -> list[str]:
    """Run Pyflakes-only lint on Python files and return fatal error lines.

    Uses ``ruff check --select=F`` when available.  Returns an empty list when
    ``ruff`` is not installed or no errors are found.
    """
    if not shutil.which("ruff"):
        logger.info("ruff not available; skipping lint")
        return []

    if files is None:
        files = glob.glob("**/*.py", root_dir=workspace, recursive=True)
    if not files:
        return []

    cmd = ["ruff", "check", "--select=F", "--no-fix", "--quiet", "--"] + files

    logger.debug("Running ruff check on %d files", len(files))
    proc = subprocess.run(
        cmd,
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode == 0:
        return []

    errors = [
        line
        for line in proc.stdout.splitlines()
        if line.strip()
    ]
    return errors
