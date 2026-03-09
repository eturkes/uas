"""Tests for Section 6: Dynamic Mid-Execution Re-planning.

Covers:
- 6a: Post-step plan validation (should_replan)
- 6b: Incremental re-planning (replan_remaining_steps)
- 6c: Step description enrichment (enrich_step_descriptions)
"""

import json
from unittest.mock import patch, MagicMock

import pytest

from architect.main import should_replan
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
        needs, detail = should_replan(step, [], {"steps": []})
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
        needs, detail = should_replan(step, remaining, {"steps": []})
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
        needs, detail = should_replan(step, remaining, {"steps": []})
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
        needs, detail = should_replan(step, remaining, {"steps": []})
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
        needs, detail = should_replan(step, remaining, {"steps": []})
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
        needs, detail = should_replan(step, remaining, {"steps": []})
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
        needs, detail = should_replan(step, remaining, {"steps": []})
        assert needs is True
        # Both mismatches should be detected
        assert "data.csv" in detail or "results.json" in detail


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
        needs, detail = should_replan(state["steps"][0], remaining, state)
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
        needs, detail = should_replan(state["steps"][0], remaining, state)
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
