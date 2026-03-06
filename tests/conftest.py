"""Shared fixtures for UAS tests."""

import json
import os
import shutil
import tempfile

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def tmp_workspace(tmp_path, monkeypatch):
    """Provide a temporary workspace directory and patch UAS_WORKSPACE."""
    monkeypatch.setenv("UAS_WORKSPACE", str(tmp_path))
    # Patch module-level constants in state.py so they use tmp_path
    import architect.state as state_mod
    import architect.spec_generator as spec_mod

    state_dir = os.path.join(str(tmp_path), ".state")
    state_file = os.path.join(state_dir, "state.json")
    specs_dir = os.path.join(state_dir, "specs")
    scratchpad_file = os.path.join(state_dir, "scratchpad.md")

    monkeypatch.setattr(state_mod, "WORKSPACE", str(tmp_path))
    monkeypatch.setattr(state_mod, "STATE_DIR", state_dir)
    monkeypatch.setattr(state_mod, "STATE_FILE", state_file)
    monkeypatch.setattr(state_mod, "SPECS_DIR", specs_dir)
    monkeypatch.setattr(state_mod, "SCRATCHPAD_FILE", scratchpad_file)
    # spec_generator imports SPECS_DIR at module level, so patch it there too
    monkeypatch.setattr(spec_mod, "SPECS_DIR", specs_dir)
    return tmp_path


def _find_claude_auth():
    """Find valid Claude CLI auth credentials.

    Checks .uas_auth/ (project-level) first, then ~/.claude/ (user-level).
    Returns the real path to the auth directory, or None if not found.
    """
    uas_auth_dir = os.path.join(PROJECT_ROOT, ".uas_auth")
    home_claude = os.path.join(os.path.expanduser("~"), ".claude")

    for candidate in [uas_auth_dir, home_claude]:
        real_path = os.path.realpath(candidate)
        cred_file = os.path.join(real_path, ".credentials.json")
        if not os.path.isfile(cred_file):
            continue
        try:
            with open(cred_file, "r", encoding="utf-8") as f:
                creds = json.load(f)
            if creds:
                return real_path
        except (json.JSONDecodeError, OSError):
            continue
    return None


@pytest.fixture(scope="session")
def require_auth():
    """Ensure Claude CLI auth is available for integration tests.

    On first successful run, creates a .uas_auth symlink in the project
    root pointing to ~/.claude so credentials are shared across all tests
    and match the container-mode auth mechanism.

    Skips the test if no valid credentials or claude binary are found.
    """
    auth_source = _find_claude_auth()
    if auth_source is None:
        pytest.skip(
            "Claude CLI authentication required. "
            "Run `claude` to authenticate, then re-run tests."
        )

    if not shutil.which("claude"):
        pytest.skip(
            "Claude CLI binary not found in PATH. "
            "Install with: npm install -g @anthropic-ai/claude-code"
        )

    # Create .uas_auth symlink if it doesn't exist
    uas_auth_dir = os.path.join(PROJECT_ROOT, ".uas_auth")
    if not os.path.exists(uas_auth_dir):
        try:
            os.symlink(auth_source, uas_auth_dir)
        except OSError:
            pass  # Non-critical -- auth still works via ~/.claude

    return auth_source
