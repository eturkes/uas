"""Tests for architect.state module."""

import json
import os

from architect.state import init_state, save_state, load_state, add_steps, append_scratchpad, read_scratchpad


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
        assert "run_id" in state
        assert len(state["run_id"]) == 12

    def test_run_id_is_unique(self, tmp_workspace):
        s1 = init_state("goal 1")
        s2 = init_state("goal 2")
        assert s1["run_id"] != s2["run_id"]

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


class TestScratchpad:
    def test_append_creates_file(self, tmp_workspace):
        append_scratchpad("first entry")
        content = read_scratchpad()
        assert "first entry" in content

    def test_append_adds_timestamp(self, tmp_workspace):
        append_scratchpad("timestamped")
        content = read_scratchpad()
        # Timestamp format: [YYYY-MM-DDTHH:MM:SSZ]
        assert "[20" in content
        assert "timestamped" in content

    def test_multiple_entries(self, tmp_workspace):
        append_scratchpad("entry one")
        append_scratchpad("entry two")
        content = read_scratchpad()
        assert "entry one" in content
        assert "entry two" in content

    def test_read_empty_returns_empty(self, tmp_workspace):
        assert read_scratchpad() == ""

    def test_read_truncates_to_max_chars(self, tmp_workspace):
        # Write a large entry
        append_scratchpad("x" * 5000)
        content = read_scratchpad(max_chars=200)
        assert len(content) <= 250  # 200 + prefix overhead
        assert "earlier entries omitted" in content

    def test_tail_based_reading(self, tmp_workspace):
        append_scratchpad("old entry")
        append_scratchpad("a" * 3000)
        append_scratchpad("recent entry")
        content = read_scratchpad(max_chars=500)
        # Recent entry should be present, old may be truncated
        assert "recent entry" in content

    def test_run_id_tag_in_header(self, tmp_workspace):
        append_scratchpad("tagged", run_id="abc123")
        content = read_scratchpad()
        assert "[run:abc123]" in content
        assert "tagged" in content

    def test_filter_by_run_id(self, tmp_workspace):
        append_scratchpad("run1 entry", run_id="run1")
        append_scratchpad("run2 entry", run_id="run2")
        content = read_scratchpad(run_id="run1")
        assert "run1 entry" in content
        assert "run2 entry" not in content

    def test_filter_excludes_untagged(self, tmp_workspace):
        append_scratchpad("legacy entry")
        append_scratchpad("tagged entry", run_id="current")
        content = read_scratchpad(run_id="current")
        assert "tagged entry" in content
        assert "legacy entry" not in content

    def test_no_filter_returns_all(self, tmp_workspace):
        append_scratchpad("run1 entry", run_id="run1")
        append_scratchpad("run2 entry", run_id="run2")
        append_scratchpad("untagged entry")
        content = read_scratchpad()
        assert "run1 entry" in content
        assert "run2 entry" in content
        assert "untagged entry" in content

    def test_filter_empty_run_id_returns_all(self, tmp_workspace):
        append_scratchpad("entry", run_id="abc")
        content = read_scratchpad(run_id="")
        assert "entry" in content
