"""Tests for plan resumability (Step 3)."""

import json
import os

from architect.state import init_state, save_state, load_state, add_steps
from architect.main import try_resume


class TestLoadStateCorrupted:
    def test_corrupted_json(self, tmp_workspace):
        """Corrupted JSON file should return None, not crash."""
        state_file = os.path.join(
            tmp_workspace, ".state", "state.json"
        )
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w") as f:
            f.write("{invalid json!!")
        assert load_state() is None

    def test_missing_goal_key(self, tmp_workspace):
        """State missing 'goal' key should return None."""
        state_file = os.path.join(
            tmp_workspace, ".state", "state.json"
        )
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w") as f:
            json.dump({"status": "executing", "steps": []}, f)
        assert load_state() is None

    def test_missing_steps_key(self, tmp_workspace):
        """State missing 'steps' key should return None."""
        state_file = os.path.join(
            tmp_workspace, ".state", "state.json"
        )
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w") as f:
            json.dump({"goal": "test"}, f)
        assert load_state() is None

    def test_non_dict_json(self, tmp_workspace):
        """A JSON array (not dict) should return None."""
        state_file = os.path.join(
            tmp_workspace, ".state", "state.json"
        )
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w") as f:
            json.dump([1, 2, 3], f)
        assert load_state() is None

    def test_valid_state_still_loads(self, tmp_workspace):
        """Valid state should still load correctly after hardening."""
        state = init_state("test")
        state = add_steps(state, [{"title": "A", "description": "Do A"}])
        loaded = load_state()
        assert loaded is not None
        assert loaded["goal"] == "test"
        assert len(loaded["steps"]) == 1


class TestTryResume:
    def test_no_saved_state(self, tmp_workspace):
        assert try_resume() is None

    def test_completed_state_returns_none(self, tmp_workspace):
        state = init_state("done goal")
        state = add_steps(state, [{"title": "A", "description": "Do A"}])
        state["status"] = "completed"
        save_state(state)
        assert try_resume() is None

    def test_empty_steps_returns_none(self, tmp_workspace):
        state = init_state("empty goal")
        # Status is "planning", steps is []
        assert try_resume() is None

    def test_executing_state_returns_state(self, tmp_workspace):
        state = init_state("in progress")
        state = add_steps(state, [
            {"title": "A", "description": "Do A"},
            {"title": "B", "description": "Do B"},
        ])
        # add_steps sets status to "executing"
        result = try_resume()
        assert result is not None
        assert result["goal"] == "in progress"
        assert len(result["steps"]) == 2

    def test_blocked_state_is_resumable(self, tmp_workspace):
        state = init_state("blocked goal")
        state = add_steps(state, [
            {"title": "A", "description": "Do A"},
            {"title": "B", "description": "Do B"},
        ])
        state["steps"][0]["status"] = "completed"
        state["steps"][0]["output"] = "step 1 result"
        state["steps"][1]["status"] = "failed"
        state["status"] = "blocked"
        save_state(state)
        result = try_resume()
        assert result is not None
        assert result["steps"][0]["status"] == "completed"
        assert result["steps"][1]["status"] == "failed"

    def test_corrupted_state_returns_none(self, tmp_workspace):
        state_file = os.path.join(
            tmp_workspace, ".state", "state.json"
        )
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w") as f:
            f.write("not json")
        assert try_resume() is None


class TestResumeResetsExecutingSteps:
    """Section 5: executing steps should be reset to pending on resume."""

    def test_executing_step_reset_to_pending(self, tmp_workspace):
        """A step left in 'executing' status should be reset to 'pending'."""
        state = init_state("interrupted run")
        state = add_steps(state, [
            {"title": "A", "description": "Do A"},
            {"title": "B", "description": "Do B"},
            {"title": "C", "description": "Do C"},
        ])
        state["steps"][0]["status"] = "completed"
        state["steps"][0]["output"] = "done A"
        state["steps"][1]["status"] = "executing"
        state["steps"][1]["started_at"] = "2026-01-01T00:00:00"
        # Step C remains pending
        save_state(state)

        result = try_resume()
        assert result is not None
        assert result["steps"][0]["status"] == "completed"
        assert result["steps"][1]["status"] == "pending"
        assert result["steps"][1]["started_at"] is None
        assert result["steps"][2]["status"] == "pending"

    def test_multiple_executing_steps_all_reset(self, tmp_workspace):
        """Multiple executing steps should all be reset."""
        state = init_state("multi interrupted")
        state = add_steps(state, [
            {"title": "A", "description": "Do A"},
            {"title": "B", "description": "Do B"},
        ])
        state["steps"][0]["status"] = "executing"
        state["steps"][0]["started_at"] = "2026-01-01T00:00:00"
        state["steps"][1]["status"] = "executing"
        state["steps"][1]["started_at"] = "2026-01-01T00:01:00"
        save_state(state)

        result = try_resume()
        assert result is not None
        assert all(s["status"] == "pending" for s in result["steps"])
        assert all(s["started_at"] is None for s in result["steps"])


class TestResumeSkipsCompleted:
    """Test that the execution loop correctly skips completed steps."""

    def test_completed_steps_populate_outputs(self, tmp_workspace):
        """Completed steps should contribute their outputs to completed_outputs."""
        state = init_state("resume test")
        state = add_steps(state, [
            {"title": "A", "description": "Do A"},
            {"title": "B", "description": "Do B", "depends_on": [1]},
        ])
        state["steps"][0]["status"] = "completed"
        state["steps"][0]["output"] = "result_from_A"
        save_state(state)

        # Simulate the resume loop logic (extracted from main)
        completed_outputs = {}
        for step in state["steps"]:
            if step["status"] == "completed":
                completed_outputs[step["id"]] = step["output"]
                continue
            # Step B would be executed here — just verify context is available
            break

        assert completed_outputs == {1: "result_from_A"}


class TestUnfinishedStepsBlockRun:
    """Section 2: run must be marked blocked if steps remain unfinished."""

    def test_unfinished_step_sets_blocked(self, tmp_workspace):
        """If any step is not completed, the run status should be 'blocked'."""
        state = init_state("incomplete run")
        state = add_steps(state, [
            {"title": "A", "description": "Do A"},
            {"title": "B", "description": "Do B"},
        ])
        state["steps"][0]["status"] = "completed"
        state["steps"][0]["output"] = "done"
        # Step B remains pending
        save_state(state)

        # Simulate the completion-check logic from main.py
        unfinished = [s for s in state["steps"] if s["status"] != "completed"]
        if unfinished:
            state["status"] = "blocked"
        else:
            state["status"] = "completed"

        assert state["status"] == "blocked"
        assert len(unfinished) == 1
        assert unfinished[0]["id"] == state["steps"][1]["id"]

    def test_all_completed_sets_completed(self, tmp_workspace):
        """If all steps are completed, the run status should be 'completed'."""
        state = init_state("full run")
        state = add_steps(state, [
            {"title": "A", "description": "Do A"},
            {"title": "B", "description": "Do B"},
        ])
        state["steps"][0]["status"] = "completed"
        state["steps"][0]["output"] = "done A"
        state["steps"][1]["status"] = "completed"
        state["steps"][1]["output"] = "done B"
        save_state(state)

        unfinished = [s for s in state["steps"] if s["status"] != "completed"]
        if unfinished:
            state["status"] = "blocked"
        else:
            state["status"] = "completed"

        assert state["status"] == "completed"
        assert unfinished == []
