"""Shared fixtures for UAS tests."""

import json
import os
import shutil

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UAS_AUTH_DIR = os.path.join(PROJECT_ROOT, ".uas_auth")


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


@pytest.fixture(scope="session")
def require_auth():
    """Ensure Claude CLI auth is available for integration tests.

    Credentials are stored in the repo-local ``.uas_auth/`` directory
    (gitignored), completely separate from the host ``~/.claude/`` config.

    If credentials are missing, the test is skipped with instructions
    to run ``bash setup_auth.sh``.
    """
    if not shutil.which("claude"):
        pytest.skip(
            "Claude CLI binary not found in PATH. "
            "Install with: npm install -g @anthropic-ai/claude-code"
        )

    if not _has_valid_auth():
        pytest.skip(
            "No credentials found in .uas_auth/. "
            "Run `bash setup_auth.sh` to authenticate, then re-run tests."
        )

    # Point all Claude CLI invocations at the repo-local credentials.
    os.environ["CLAUDE_CONFIG_DIR"] = UAS_AUTH_DIR
    return UAS_AUTH_DIR
