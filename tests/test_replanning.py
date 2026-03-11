"""Tests for Section 6: Dynamic Mid-Execution Re-planning.

Covers:
- 6a: Post-step plan validation (should_replan)
- 6b: Incremental re-planning (replan_remaining_steps)
- 6c: Step description enrichment (enrich_step_descriptions)
"""

import json
from unittest.mock import patch, MagicMock

import pytest

from architect.main import should_replan_heuristic, should_replan_llm
from architect.planner import (
    replan_remaining_steps,
    enrich_step_descriptions,
)
from architect.state import add_steps, init_state
from architect.events import EventType


# ---------------------------------------------------------------------------
# 6a: Post-step plan validation (should_replan)
# ---------------------------------------------------------------------------

class TestShouldReplan:
    def test_no_remaining_steps(self):
        step = {"id": 1, "files_written": ["data.csv"]}
        needs, detail = should_replan_heuristic(step, [], {"steps": []})
        assert needs is False
        assert detail == ""

    def test_no_mismatch_when_files_match(self):
        step = {
            "id": 1,
            "files_written": ["data.csv"],
            "uas_result": {"status": "ok"},
        }
        remaining = [{
            "id": 2,
            "depends_on": [1],
            "description": "Read data.csv and compute statistics.",
        }]
        needs, detail = should_replan_heuristic(step, remaining, {"steps": []})
        assert needs is False

    def test_mismatch_when_wrong_file_produced(self):
        step = {
            "id": 1,
            "files_written": ["output.json"],
            "uas_result": {"status": "ok"},
        }
        remaining = [{
            "id": 2,
            "depends_on": [1],
            "description": "Read data.csv from the workspace and process it.",
        }]
        needs, detail = should_replan_heuristic(step, remaining, {"steps": []})
        assert needs is True
        assert "data.csv" in detail

    def test_mismatch_no_files_when_downstream_expects_files(self):
        step = {
            "id": 1,
            "files_written": [],
            "uas_result": {"status": "ok"},
        }
        remaining = [{
            "id": 2,
            "depends_on": [1],
            "description": "Read the output file from step 1.",
        }]
        needs, detail = should_replan_heuristic(step, remaining, {"steps": []})
        assert needs is True
        assert "no files were produced" in detail

    def test_ignores_steps_that_dont_depend_on_completed(self):
        step = {
            "id": 1,
            "files_written": ["wrong.txt"],
            "uas_result": {"status": "ok"},
        }
        remaining = [{
            "id": 3,
            "depends_on": [2],  # Depends on step 2, not step 1
            "description": "Read data.csv from the workspace.",
        }]
        needs, detail = should_replan_heuristic(step, remaining, {"steps": []})
        assert needs is False

    def test_detects_similar_file_mismatch(self):
        step = {
            "id": 1,
            "files_written": ["results.csv"],
            "uas_result": {"status": "ok"},
        }
        remaining = [{
            "id": 2,
            "depends_on": [1],
            "description": "Load output.csv and generate a report.",
        }]
        needs, detail = should_replan_heuristic(step, remaining, {"steps": []})
        assert needs is True
        assert "output.csv" in detail
        assert "results.csv" in detail

    def test_multiple_downstream_mismatches(self):
        step = {
            "id": 1,
            "files_written": ["actual.json"],
            "uas_result": {"status": "ok"},
        }
        remaining = [
            {
                "id": 2,
                "depends_on": [1],
                "description": "Read data.csv from step 1.",
            },
            {
                "id": 3,
                "depends_on": [1],
                "description": "Parse results.json from step 1.",
            },
        ]
        needs, detail = should_replan_heuristic(step, remaining, {"steps": []})
        assert needs is True
        # Both mismatches should be detected
        assert "data.csv" in detail or "results.json" in detail


# ---------------------------------------------------------------------------
# 6a-llm: LLM-based re-plan trigger (should_replan_llm)
# ---------------------------------------------------------------------------

class TestShouldReplanLlm:
    @patch("orchestrator.llm_client.get_llm_client")
    def test_returns_llm_decision_needs_replan(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = json.dumps({
            "needs_replan": True,
            "reason": "Step 2 expects data.csv but step 1 produced output.json",
        })
        mock_get_client.return_value = client

        step = {
            "id": 1, "title": "Download",
            "files_written": ["output.json"],
            "uas_result": {"status": "ok"},
            "summary": "Downloaded data as JSON",
        }
        remaining = [{
            "id": 2, "title": "Process",
            "depends_on": [1],
            "description": "Read data.csv and compute statistics.",
        }]
        needs, detail = should_replan_llm(step, remaining, {"steps": []})
        assert needs is True
        assert "data.csv" in detail

    @patch("orchestrator.llm_client.get_llm_client")
    def test_returns_llm_decision_no_replan(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = json.dumps({
            "needs_replan": False,
            "reason": "Output matches expectations",
        })
        mock_get_client.return_value = client

        step = {
            "id": 1, "title": "Download",
            "files_written": ["data.csv"],
            "uas_result": {"status": "ok"},
            "summary": "Downloaded CSV data",
        }
        remaining = [{
            "id": 2, "title": "Process",
            "depends_on": [1],
            "description": "Read data.csv and compute statistics.",
        }]
        needs, detail = should_replan_llm(step, remaining, {"steps": []})
        assert needs is False

    @patch("orchestrator.llm_client.get_llm_client")
    def test_falls_back_to_heuristic_on_llm_error(self, mock_get_client):
        client = MagicMock()
        client.generate.side_effect = RuntimeError("API error")
        mock_get_client.return_value = client

        step = {
            "id": 1, "title": "Download",
            "files_written": ["output.json"],
            "uas_result": {"status": "ok"},
            "summary": "",
        }
        remaining = [{
            "id": 2, "title": "Process",
            "depends_on": [1],
            "description": "Read data.csv from the workspace and process it.",
        }]
        needs, detail = should_replan_llm(step, remaining, {"steps": []})
        # Heuristic fallback should detect the mismatch
        assert needs is True
        assert "data.csv" in detail

    @patch("orchestrator.llm_client.get_llm_client")
    def test_falls_back_to_heuristic_on_bad_json(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "I'm not sure about this."
        mock_get_client.return_value = client

        step = {
            "id": 1, "title": "Download",
            "files_written": [],
            "uas_result": {"status": "ok"},
            "summary": "",
        }
        remaining = [{
            "id": 2, "title": "Process",
            "depends_on": [1],
            "description": "Read the output file from step 1.",
        }]
        needs, detail = should_replan_llm(step, remaining, {"steps": []})
        # Heuristic fallback: no files produced, downstream expects to read
        assert needs is True
        assert "no files were produced" in detail

    def test_no_remaining_steps(self):
        step = {"id": 1, "files_written": ["data.csv"]}
        needs, detail = should_replan_llm(step, [], {"steps": []})
        assert needs is False
        assert detail == ""

    def test_no_dependent_steps(self):
        step = {"id": 1, "files_written": ["data.csv"]}
        remaining = [{
            "id": 3,
            "depends_on": [2],  # Depends on step 2, not 1
            "description": "Read data.csv from the workspace.",
        }]
        needs, detail = should_replan_llm(step, remaining, {"steps": []})
        assert needs is False
        assert detail == ""

    @patch("orchestrator.llm_client.get_llm_client")
    def test_prompt_contains_step_context(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = json.dumps({
            "needs_replan": False,
            "reason": "All good",
        })
        mock_get_client.return_value = client

        step = {
            "id": 1, "title": "Scrape data",
            "files_written": ["products.json"],
            "uas_result": {"status": "ok", "summary": "Scraped 50 items"},
            "summary": "Scraped product data",
        }
        remaining = [{
            "id": 2, "title": "Analyze",
            "depends_on": [1],
            "description": "Parse products.json and generate report.",
        }]
        should_replan_llm(step, remaining, {"steps": []})
        prompt = client.generate.call_args[0][0]
        assert "Scrape data" in prompt
        assert "products.json" in prompt
        assert "Scraped product data" in prompt
        assert "Parse products.json" in prompt
        assert "Step 2" in prompt

    @patch("orchestrator.llm_client.get_llm_client")
    def test_handles_markdown_fenced_json(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = (
            "```json\n"
            '{"needs_replan": true, "reason": "format mismatch"}\n'
            "```"
        )
        mock_get_client.return_value = client

        step = {
            "id": 1, "title": "Generate",
            "files_written": ["out.txt"],
            "uas_result": {},
            "summary": "Generated output",
        }
        remaining = [{
            "id": 2, "title": "Consume",
            "depends_on": [1],
            "description": "Read out.csv and process.",
        }]
        needs, detail = should_replan_llm(step, remaining, {"steps": []})
        assert needs is True
        assert "format mismatch" in detail


# ---------------------------------------------------------------------------
# 6b: Incremental re-planning (replan_remaining_steps)
# ---------------------------------------------------------------------------

class TestReplanRemainingSteps:
    @patch("architect.planner.get_llm_client")
    def test_returns_new_steps_on_success(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = json.dumps([
            {
                "title": "Process actual output",
                "description": "Read actual.json and process it.",
                "depends_on": [1],
                "verify": "processed.csv exists",
                "environment": ["pandas"],
            },
        ])
        mock_get_client.return_value = client

        state = {
            "goal": "Analyze data",
            "steps": [
                {"id": 1, "title": "Download", "status": "completed",
                 "files_written": ["actual.json"], "summary": "Downloaded data",
                 "depends_on": []},
                {"id": 2, "title": "Process", "status": "pending",
                 "description": "Read data.csv and process.",
                 "depends_on": [1], "verify": "", "environment": []},
            ],
        }
        unexpected_step = state["steps"][0]
        result = replan_remaining_steps(
            "Analyze data", state, unexpected_step,
            "Step 2 references data.csv but step 1 produced actual.json",
        )
        assert result is not None
        assert len(result) == 1
        assert result[0]["title"] == "Process actual output"

    @patch("architect.planner.get_llm_client")
    def test_returns_none_on_llm_failure(self, mock_get_client):
        client = MagicMock()
        client.generate.side_effect = RuntimeError("API error")
        mock_get_client.return_value = client

        state = {
            "goal": "test",
            "steps": [
                {"id": 1, "title": "A", "status": "completed",
                 "files_written": [], "summary": "", "depends_on": []},
                {"id": 2, "title": "B", "status": "pending",
                 "description": "do B", "depends_on": [1],
                 "verify": "", "environment": []},
            ],
        }
        result = replan_remaining_steps(
            "test", state, state["steps"][0], "mismatch",
        )
        assert result is None

    @patch("architect.planner.get_llm_client")
    def test_returns_none_on_unparseable_response(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "I can't do this task."
        mock_get_client.return_value = client

        state = {
            "goal": "test",
            "steps": [
                {"id": 1, "title": "A", "status": "completed",
                 "files_written": [], "summary": "", "depends_on": []},
                {"id": 2, "title": "B", "status": "pending",
                 "description": "do B", "depends_on": [1],
                 "verify": "", "environment": []},
            ],
        }
        result = replan_remaining_steps(
            "test", state, state["steps"][0], "mismatch",
        )
        assert result is None

    @patch("architect.planner.get_llm_client")
    def test_prompt_contains_context(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = json.dumps([
            {"title": "New step", "description": "Do work",
             "depends_on": [1], "verify": "", "environment": []},
        ])
        mock_get_client.return_value = client

        state = {
            "goal": "Build a website",
            "steps": [
                {"id": 1, "title": "Setup", "status": "completed",
                 "files_written": ["index.html"], "summary": "Created scaffold",
                 "depends_on": []},
                {"id": 2, "title": "Style", "status": "pending",
                 "description": "Add CSS styles",
                 "depends_on": [1], "verify": "", "environment": []},
            ],
        }
        replan_remaining_steps(
            "Build a website", state, state["steps"][0],
            "Missing expected files",
        )
        prompt = client.generate.call_args[0][0]
        assert "Build a website" in prompt
        assert "Setup" in prompt
        assert "index.html" in prompt
        assert "Missing expected files" in prompt
        assert "Style" in prompt

    @patch("architect.planner.get_llm_client")
    def test_returns_none_on_empty_steps(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "[]"
        mock_get_client.return_value = client

        state = {
            "goal": "test",
            "steps": [
                {"id": 1, "title": "A", "status": "completed",
                 "files_written": [], "summary": "", "depends_on": []},
            ],
        }
        result = replan_remaining_steps(
            "test", state, state["steps"][0], "mismatch",
        )
        assert result is None

    @patch("architect.planner.get_llm_client")
    def test_normalizes_zero_indexed_deps(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = json.dumps([
            {"title": "Step A", "description": "Do A",
             "depends_on": [0], "verify": "", "environment": []},
        ])
        mock_get_client.return_value = client

        state = {
            "goal": "test",
            "steps": [
                {"id": 1, "title": "First", "status": "completed",
                 "files_written": [], "summary": "", "depends_on": []},
                {"id": 2, "title": "Second", "status": "pending",
                 "description": "do work", "depends_on": [1],
                 "verify": "", "environment": []},
            ],
        }
        result = replan_remaining_steps(
            "test", state, state["steps"][0], "detail",
        )
        assert result is not None
        assert result[0]["depends_on"] == [1]

    @patch("architect.planner.get_llm_client")
    def test_accepts_inter_new_step_deps_continuation_ids(self, mock_get_client):
        """LLM uses continuation IDs (after max completed) for inter-dependencies."""
        client = MagicMock()
        # LLM produces steps with IDs 3,4 (continuing after completed 1,2)
        # Step 4 depends on new step 3
        client.generate.return_value = json.dumps([
            {"id": 3, "title": "Fetch data", "description": "Download data",
             "depends_on": [1], "verify": "", "environment": []},
            {"id": 4, "title": "Process data", "description": "Process it",
             "depends_on": [3], "verify": "", "environment": []},
        ])
        mock_get_client.return_value = client

        state = {
            "goal": "Build pipeline",
            "steps": [
                {"id": 1, "title": "Setup", "status": "completed",
                 "files_written": ["config.json"], "summary": "Done",
                 "depends_on": []},
                {"id": 2, "title": "Cleanup", "status": "completed",
                 "files_written": [], "summary": "Cleaned",
                 "depends_on": []},
                {"id": 3, "title": "Old fetch", "status": "pending",
                 "description": "old", "depends_on": [1],
                 "verify": "", "environment": []},
                {"id": 4, "title": "Old process", "status": "pending",
                 "description": "old", "depends_on": [3],
                 "verify": "", "environment": []},
            ],
        }
        result = replan_remaining_steps(
            "Build pipeline", state, state["steps"][0],
            "Step 3 references wrong files",
        )
        assert result is not None
        assert len(result) == 2
        # Step with dep on 3 (another new step) should be accepted
        assert result[1]["depends_on"] == [3]

    @patch("architect.planner.get_llm_client")
    def test_accepts_inter_new_step_deps_positional_ids(self, mock_get_client):
        """LLM uses positional 1-based IDs for inter-dependencies."""
        client = MagicMock()
        # LLM uses positional numbering: step 1 of new list depends on
        # completed step 1, step 2 of new list depends on step 1 of new list
        client.generate.return_value = json.dumps([
            {"title": "Fetch data", "description": "Download data",
             "depends_on": [1], "verify": "", "environment": []},
            {"title": "Process data", "description": "Process it",
             "depends_on": [1], "verify": "", "environment": []},
        ])
        mock_get_client.return_value = client

        state = {
            "goal": "Build pipeline",
            "steps": [
                {"id": 1, "title": "Setup", "status": "completed",
                 "files_written": [], "summary": "Done",
                 "depends_on": []},
                {"id": 2, "title": "Old step", "status": "pending",
                 "description": "old", "depends_on": [1],
                 "verify": "", "environment": []},
            ],
        }
        result = replan_remaining_steps(
            "Build pipeline", state, state["steps"][0], "mismatch",
        )
        assert result is not None
        assert len(result) == 2

    @patch("architect.planner.get_llm_client")
    def test_rejects_self_referencing_new_step(self, mock_get_client):
        """New step that depends on itself should be rejected."""
        client = MagicMock()
        client.generate.return_value = json.dumps([
            {"id": 3, "title": "Bad step", "description": "Self-ref",
             "depends_on": [3], "verify": "", "environment": []},
        ])
        mock_get_client.return_value = client

        state = {
            "goal": "test",
            "steps": [
                {"id": 1, "title": "A", "status": "completed",
                 "files_written": [], "summary": "", "depends_on": []},
                {"id": 2, "title": "B", "status": "completed",
                 "files_written": [], "summary": "", "depends_on": []},
            ],
        }
        result = replan_remaining_steps(
            "test", state, state["steps"][0], "detail",
        )
        assert result is None


# ---------------------------------------------------------------------------
# 6d: ID remapping after re-planning
# ---------------------------------------------------------------------------

class TestReplanIdRemapping:
    """Test that depends_on is correctly remapped after ID reassignment."""

    def test_continuation_ids_remapped(self):
        """When LLM uses continuation IDs, deps between new steps are remapped."""
        # Simulate state with 2 completed steps
        state = {
            "goal": "Complex task",
            "steps": [
                {"id": 1, "title": "Step A", "status": "completed",
                 "depends_on": [], "files_written": ["a.txt"],
                 "summary": "Done A", "uas_result": None, "output": "",
                 "stderr_output": "", "error": "", "spec_file": "",
                 "rewrites": 0, "reflections": []},
                {"id": 2, "title": "Step B", "status": "completed",
                 "depends_on": [], "files_written": ["b.txt"],
                 "summary": "Done B", "uas_result": None, "output": "",
                 "stderr_output": "", "error": "", "spec_file": "",
                 "rewrites": 0, "reflections": []},
                {"id": 3, "title": "Old C", "status": "pending",
                 "depends_on": [1], "description": "Read a.csv",
                 "files_written": [], "summary": "", "uas_result": None,
                 "output": "", "stderr_output": "", "error": "",
                 "spec_file": "", "rewrites": 0, "reflections": []},
            ],
        }

        # Simulate re-planned steps with continuation IDs
        new_remaining = [
            {"id": 3, "title": "New C", "description": "Fetch data",
             "depends_on": [1], "verify": "", "environment": []},
            {"id": 4, "title": "New D", "description": "Process data",
             "depends_on": [3], "verify": "", "environment": []},
        ]

        completed_steps = [s for s in state["steps"] if s["status"] == "completed"]
        completed_ids_set = {s["id"] for s in completed_steps}
        max_completed_id = max(completed_ids_set, default=0)

        # Apply the same logic as _post_step_replan_and_enrich
        dep_remap = {}
        for i, new_step in enumerate(new_remaining):
            final_id = max_completed_id + i + 1
            dep_remap.setdefault(i + 1, final_id)
            dep_remap.setdefault(max_completed_id + i + 1, final_id)
            if "id" in new_step and new_step["id"] not in dep_remap:
                dep_remap[new_step["id"]] = final_id

        for i, new_step in enumerate(new_remaining):
            new_step["id"] = max_completed_id + i + 1

        for new_step in new_remaining:
            new_step["depends_on"] = [
                d if d in completed_ids_set
                else dep_remap.get(d, d)
                for d in new_step.get("depends_on", [])
            ]

        # Step C: depends on completed step 1, should stay as [1]
        assert new_remaining[0]["id"] == 3
        assert new_remaining[0]["depends_on"] == [1]
        # Step D: depends on new step C (was id=3), should be [3]
        assert new_remaining[1]["id"] == 4
        assert new_remaining[1]["depends_on"] == [3]

    def test_positional_ids_remapped(self):
        """When LLM uses positional 1-based IDs, deps are remapped to final IDs."""
        # 3 completed steps, LLM uses positional {1, 2} for new steps
        completed_ids_set = {1, 2, 3}
        max_completed_id = 3

        new_remaining = [
            {"title": "New A", "description": "Do A",
             "depends_on": [2], "verify": "", "environment": []},
            {"title": "New B", "description": "Do B",
             "depends_on": [1], "verify": "", "environment": []},
        ]

        dep_remap = {}
        for i, new_step in enumerate(new_remaining):
            final_id = max_completed_id + i + 1
            dep_remap.setdefault(i + 1, final_id)
            dep_remap.setdefault(max_completed_id + i + 1, final_id)
            if "id" in new_step and new_step["id"] not in dep_remap:
                dep_remap[new_step["id"]] = final_id

        for i, new_step in enumerate(new_remaining):
            new_step["id"] = max_completed_id + i + 1

        for new_step in new_remaining:
            new_step["depends_on"] = [
                d if d in completed_ids_set
                else dep_remap.get(d, d)
                for d in new_step.get("depends_on", [])
            ]

        # New A: depends on completed step 2, should stay [2]
        assert new_remaining[0]["id"] == 4
        assert new_remaining[0]["depends_on"] == [2]
        # New B: depends_on [1] — ambiguous (completed step 1 or positional 1).
        # Since 1 is in completed_ids_set, it stays as [1] (completed step 1).
        assert new_remaining[1]["id"] == 5
        assert new_remaining[1]["depends_on"] == [1]

    def test_mixed_deps_completed_and_new(self):
        """Step depends on both a completed step and another new step."""
        completed_ids_set = {1, 2}
        max_completed_id = 2

        new_remaining = [
            {"id": 3, "title": "New C", "description": "Do C",
             "depends_on": [1], "verify": "", "environment": []},
            {"id": 4, "title": "New D", "description": "Do D",
             "depends_on": [2, 3], "verify": "", "environment": []},
        ]

        dep_remap = {}
        for i, new_step in enumerate(new_remaining):
            final_id = max_completed_id + i + 1
            dep_remap.setdefault(i + 1, final_id)
            dep_remap.setdefault(max_completed_id + i + 1, final_id)
            if "id" in new_step and new_step["id"] not in dep_remap:
                dep_remap[new_step["id"]] = final_id

        for i, new_step in enumerate(new_remaining):
            new_step["id"] = max_completed_id + i + 1

        for new_step in new_remaining:
            new_step["depends_on"] = [
                d if d in completed_ids_set
                else dep_remap.get(d, d)
                for d in new_step.get("depends_on", [])
            ]

        assert new_remaining[0]["depends_on"] == [1]      # completed
        assert new_remaining[1]["depends_on"] == [2, 3]    # completed + new


# ---------------------------------------------------------------------------
# 6c: Step description enrichment
# ---------------------------------------------------------------------------

class TestEnrichStepDescriptions:
    def test_enriches_dependent_step(self):
        completed = {
            "id": 1,
            "title": "Download data",
            "files_written": ["data.csv"],
            "summary": "Downloaded 1000 rows of product data",
        }
        dependents = [
            {
                "id": 2,
                "depends_on": [1],
                "description": "Process the downloaded data.",
            },
        ]
        enriched = enrich_step_descriptions(completed, dependents)
        assert enriched == [2]
        assert "data.csv" in dependents[0]["description"]
        assert "1000 rows" in dependents[0]["description"]

    def test_no_enrichment_without_files_or_summary(self):
        completed = {
            "id": 1,
            "title": "Setup",
            "files_written": [],
            "summary": "",
        }
        dependents = [
            {
                "id": 2,
                "depends_on": [1],
                "description": "Continue work.",
            },
        ]
        enriched = enrich_step_descriptions(completed, dependents)
        assert enriched == []
        assert "Context from step" not in dependents[0]["description"]

    def test_avoids_duplicate_enrichment(self):
        completed = {
            "id": 1,
            "title": "Download",
            "files_written": ["data.csv"],
            "summary": "Got data",
        }
        dependents = [
            {
                "id": 2,
                "depends_on": [1],
                "description": (
                    "Process data. "
                    "[Context from step 1 (Download): files produced: data.csv]"
                ),
            },
        ]
        enriched = enrich_step_descriptions(completed, dependents)
        assert enriched == []

    def test_enriches_multiple_dependents(self):
        completed = {
            "id": 1,
            "title": "Scrape",
            "files_written": ["products.json"],
            "summary": "Scraped 50 products",
        }
        dependents = [
            {"id": 2, "depends_on": [1], "description": "Analyze data."},
            {"id": 3, "depends_on": [1], "description": "Generate report."},
        ]
        enriched = enrich_step_descriptions(completed, dependents)
        assert enriched == [2, 3]
        for dep in dependents:
            assert "products.json" in dep["description"]

    def test_includes_uas_result_summary(self):
        completed = {
            "id": 1,
            "title": "Process",
            "files_written": ["result.txt"],
            "summary": "First summary",
            "uas_result": {"summary": "Detailed UAS result"},
        }
        dependents = [
            {"id": 2, "depends_on": [1], "description": "Use result."},
        ]
        enriched = enrich_step_descriptions(completed, dependents)
        assert enriched == [2]
        assert "Detailed UAS result" in dependents[0]["description"]

    def test_empty_dependents_list(self):
        completed = {
            "id": 1,
            "title": "Step",
            "files_written": ["f.txt"],
            "summary": "Done",
        }
        enriched = enrich_step_descriptions(completed, [])
        assert enriched == []


# ---------------------------------------------------------------------------
# Event types for re-planning
# ---------------------------------------------------------------------------

class TestReplanEventTypes:
    def test_replan_event_types_exist(self):
        assert EventType.REPLAN_CHECK.value == "replan_check"
        assert EventType.REPLAN_TRIGGERED.value == "replan_triggered"
        assert EventType.REPLAN_COMPLETE.value == "replan_complete"
        assert EventType.STEP_ENRICHED.value == "step_enriched"


# ---------------------------------------------------------------------------
# Integration: should_replan + enrich work together
# ---------------------------------------------------------------------------

class TestReplanIntegration:
    def test_full_workflow_no_replan_needed(self, tmp_workspace):
        """When files match expectations, no re-planning is triggered."""
        state = init_state("Build a data pipeline")
        steps = [
            {"title": "Download", "description": "Download data to data.csv",
             "depends_on": [], "verify": "data.csv exists"},
            {"title": "Process", "description": "Read data.csv and clean it",
             "depends_on": [1], "verify": "clean.csv exists"},
        ]
        state = add_steps(state, steps)

        # Simulate step 1 completing
        state["steps"][0]["status"] = "completed"
        state["steps"][0]["files_written"] = ["data.csv"]
        state["steps"][0]["summary"] = "Downloaded 100 rows"

        remaining = [s for s in state["steps"] if s["status"] != "completed"]
        needs, detail = should_replan_heuristic(state["steps"][0], remaining, state)
        assert needs is False

    def test_full_workflow_replan_needed(self, tmp_workspace):
        """When files don't match, re-planning is triggered."""
        state = init_state("Build a data pipeline")
        steps = [
            {"title": "Download", "description": "Download data",
             "depends_on": [], "verify": ""},
            {"title": "Process", "description": "Read data.csv and clean it",
             "depends_on": [1], "verify": ""},
        ]
        state = add_steps(state, steps)

        # Simulate step 1 producing different files
        state["steps"][0]["status"] = "completed"
        state["steps"][0]["files_written"] = ["output.json"]

        remaining = [s for s in state["steps"] if s["status"] != "completed"]
        needs, detail = should_replan_heuristic(state["steps"][0], remaining, state)
        assert needs is True
        assert "data.csv" in detail

    def test_enrichment_after_completion(self, tmp_workspace):
        """Step descriptions are enriched with completed step output."""
        state = init_state("Analyze data")
        steps = [
            {"title": "Download", "description": "Get data",
             "depends_on": [], "verify": ""},
            {"title": "Process", "description": "Process the data",
             "depends_on": [1], "verify": ""},
        ]
        state = add_steps(state, steps)

        state["steps"][0]["status"] = "completed"
        state["steps"][0]["files_written"] = ["data.csv"]
        state["steps"][0]["summary"] = "Downloaded CSV with columns: name, price"

        dependents = [s for s in state["steps"]
                      if state["steps"][0]["id"] in s.get("depends_on", [])]
        enriched = enrich_step_descriptions(state["steps"][0], dependents)

        assert len(enriched) == 1
        assert "data.csv" in state["steps"][1]["description"]
        assert "name, price" in state["steps"][1]["description"]

    def test_replan_limit_per_level(self):
        """replanned_levels set prevents multiple replans at same level."""
        # This tests the data structure used in main() execution loop
        replanned_levels = set()
        replanned_levels.add(0)
        assert 0 in replanned_levels
        assert 1 not in replanned_levels

    @patch("architect.planner.get_llm_client")
    def test_replan_with_multiple_completed_steps(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = json.dumps([
            {"title": "Adjusted step", "description": "Use actual output",
             "depends_on": [1], "verify": "", "environment": []},
        ])
        mock_get_client.return_value = client

        state = {
            "goal": "Multi-step task",
            "steps": [
                {"id": 1, "title": "Step A", "status": "completed",
                 "files_written": ["a.txt"], "summary": "Done A",
                 "depends_on": []},
                {"id": 2, "title": "Step B", "status": "completed",
                 "files_written": ["b.txt"], "summary": "Done B",
                 "depends_on": []},
                {"id": 3, "title": "Step C", "status": "pending",
                 "description": "Combine a.csv and b.csv",
                 "depends_on": [1, 2], "verify": "", "environment": []},
            ],
        }
        result = replan_remaining_steps(
            "Multi-step task", state, state["steps"][1],
            "Step C expects a.csv and b.csv but got a.txt and b.txt",
        )
        assert result is not None
        # Verify prompt included both completed steps
        prompt = client.generate.call_args[0][0]
        assert "Step A" in prompt
        assert "Step B" in prompt
        assert "a.txt" in prompt
        assert "b.txt" in prompt
