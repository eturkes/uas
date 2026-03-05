"""Shared fixtures for UAS tests."""

import os
import tempfile

import pytest


@pytest.fixture
def tmp_workspace(tmp_path, monkeypatch):
    """Provide a temporary workspace directory and patch UAS_WORKSPACE."""
    monkeypatch.setenv("UAS_WORKSPACE", str(tmp_path))
    # Patch module-level constants in state.py so they use tmp_path
    import architect.state as state_mod
    import architect.spec_generator as spec_mod

    state_dir = os.path.join(str(tmp_path), "architect_state")
    state_file = os.path.join(state_dir, "plan_state.json")
    specs_dir = os.path.join(state_dir, "specs")

    monkeypatch.setattr(state_mod, "WORKSPACE", str(tmp_path))
    monkeypatch.setattr(state_mod, "STATE_DIR", state_dir)
    monkeypatch.setattr(state_mod, "STATE_FILE", state_file)
    monkeypatch.setattr(state_mod, "SPECS_DIR", specs_dir)
    # spec_generator imports SPECS_DIR at module level, so patch it there too
    monkeypatch.setattr(spec_mod, "SPECS_DIR", specs_dir)
    return tmp_path
