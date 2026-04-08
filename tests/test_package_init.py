"""Tests for the sys.path scrub installed by architect/__init__.py and
orchestrator/__init__.py (PLAN.md Section 6).

These scrubs remove the empty-string entry that Python prepends to
sys.path when a package is imported via ``python -m architect.X`` from an
arbitrary working directory. The empty entry would otherwise cause a
workspace-local file (e.g. ``config.py``) to shadow a same-named
framework module on import.
"""

import os
import subprocess
import sys


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _run_python(code: str) -> subprocess.CompletedProcess:
    """Run a probe Python subprocess with the framework on PYTHONPATH.

    Uses ``-P`` so the interpreter does NOT auto-prepend an empty-string
    entry to ``sys.path``. The probe code can then explicitly insert ``''``
    at position 0 to verify the package ``__init__.py`` scrub removes it.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = REPO_ROOT
    return subprocess.run(
        [sys.executable, "-P", "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_architect_init_scrubs_empty_sys_path():
    """Importing the architect package strips a leading '' from sys.path."""
    code = (
        "import sys; "
        "sys.path.insert(0, ''); "
        "import architect; "
        "print('' in sys.path)"
    )
    result = _run_python(code)
    assert result.returncode == 0, (
        f"subprocess failed: stderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "False", (
        f"architect package failed to scrub empty sys.path entry. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_orchestrator_init_scrubs_empty_sys_path():
    """Importing the orchestrator package strips a leading '' from sys.path."""
    code = (
        "import sys; "
        "sys.path.insert(0, ''); "
        "import orchestrator; "
        "print('' in sys.path)"
    )
    result = _run_python(code)
    assert result.returncode == 0, (
        f"subprocess failed: stderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "False", (
        f"orchestrator package failed to scrub empty sys.path entry. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_architect_init_scrubs_absolute_cwd_sys_path(tmp_path):
    """Verify the scrub also removes the absolute cwd entry that
    Python's ``-m`` mode prepends. This is the actual real-world bug
    case (see PLAN.md Section 6 smoke check) — Python 3.11+ does NOT
    prepend ``""`` for ``-m``; it prepends the absolute cwd.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = REPO_ROOT
    code = (
        "import os, sys; "
        f"sys.path.insert(0, {str(tmp_path)!r}); "
        "import architect; "
        f"print({str(tmp_path)!r} in sys.path)"
    )
    result = subprocess.run(
        [sys.executable, "-P", "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
        timeout=30,
    )
    assert result.returncode == 0, (
        f"subprocess failed: stderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "False", (
        f"architect package failed to scrub absolute cwd sys.path entry. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_orchestrator_init_scrubs_absolute_cwd_sys_path(tmp_path):
    """Same as above but for the orchestrator package."""
    env = os.environ.copy()
    env["PYTHONPATH"] = REPO_ROOT
    code = (
        "import os, sys; "
        f"sys.path.insert(0, {str(tmp_path)!r}); "
        "import orchestrator; "
        f"print({str(tmp_path)!r} in sys.path)"
    )
    result = subprocess.run(
        [sys.executable, "-P", "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
        timeout=30,
    )
    assert result.returncode == 0, (
        f"subprocess failed: stderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "False", (
        f"orchestrator package failed to scrub absolute cwd sys.path entry. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_architect_init_does_not_scrub_framework_root(tmp_path):
    """Safety: the scrub must NOT pop sys.path[0] when the cwd happens
    to be the framework root itself, otherwise the framework's own
    imports would break.
    """
    env = os.environ.copy()
    code = (
        "import sys; "
        f"sys.path.insert(0, {REPO_ROOT!r}); "
        "import architect; "
        f"print(sys.path[0] == {REPO_ROOT!r})"
    )
    result = subprocess.run(
        [sys.executable, "-P", "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"subprocess failed: stderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "True", (
        f"scrub incorrectly removed framework root from sys.path. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
