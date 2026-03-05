"""Tests for architect.state module."""

import json
import os

from architect.state import init_state, save_state, load_state, add_steps


class TestInitState:
    def test_creates_state_dir(self, tmp_workspace):
        state = init_state("test goal")
        assert os.path.isdir(os.path.join(tmp_workspace, ".state"))

    def test_returns_correct_structure(self, tmp_workspace):
        state = init_state("my goal")
        assert state["goal"] == "my goal"
        assert state["status"] == "planning"
        assert state["steps"] == []
        assert "created_at" in state

    def test_persists_to_disk(self, tmp_workspace):
        init_state("persist test")
        state_file = os.path.join(
            tmp_workspace, ".state", "state.json"
        )
        assert os.path.exists(state_file)
        with open(state_file) as f:
            data = json.load(f)
        assert data["goal"] == "persist test"


class TestSaveLoadState:
    def test_round_trip(self, tmp_workspace):
        original = {"goal": "test", "status": "running", "steps": []}
        save_state(original)
        loaded = load_state()
        assert loaded == original

    def test_load_missing_returns_none(self, tmp_workspace):
        assert load_state() is None


class TestAddSteps:
    def test_adds_steps_with_correct_fields(self, tmp_workspace):
        state = init_state("goal")
        steps = [
            {"title": "Step A", "description": "Do A", "depends_on": []},
            {"title": "Step B", "description": "Do B", "depends_on": [1]},
        ]
        state = add_steps(state, steps)

        assert len(state["steps"]) == 2
        assert state["steps"][0]["id"] == 1
        assert state["steps"][0]["title"] == "Step A"
        assert state["steps"][0]["status"] == "pending"
        assert state["steps"][0]["spec_file"] is None
        assert state["steps"][0]["rewrites"] == 0
        assert state["steps"][1]["depends_on"] == [1]

    def test_sets_status_to_executing(self, tmp_workspace):
        state = init_state("goal")
        steps = [{"title": "S", "description": "D"}]
        state = add_steps(state, steps)
        assert state["status"] == "executing"

    def test_persists_after_add(self, tmp_workspace):
        state = init_state("goal")
        add_steps(state, [{"title": "S", "description": "D"}])
        loaded = load_state()
        assert len(loaded["steps"]) == 1
