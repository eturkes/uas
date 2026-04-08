"""Section 4 integration test: planner sees pre-existing workspace files.

The rehab failure (run id 12f634a8f886) was caused by the planner
inventing a ``simulation_spec.json`` schema with keys ``structure``,
``temporal_patterns``, ``sensory_lt``, and ``sensory_pp`` because it
never inspected the workspace before generating step descriptions.
Section 4 wires ``build_planner_workspace_context`` into the architect
main flow so the planner is grounded in the real file contents. This
test exercises that wiring end-to-end via the same helper main.py
calls, asserting the captured decomposition prompt mentions the real
top-level keys and omits the hallucinated ones.
"""

import json
from unittest.mock import MagicMock, patch

from architect.executor import build_planner_workspace_context
from architect.planner import (
    decompose_goal,
    generate_project_spec,
    research_goal,
)


class TestPlannerWorkspaceAwareness:
    """End-to-end check that real schema reaches planner prompts."""

    def _make_workspace(self, tmp_path):
        """Create a workspace with the rehab-style simulation_spec.json."""
        spec_path = tmp_path / "simulation_spec.json"
        spec_path.write_text(json.dumps({
            "metadata": {"version": 1, "description": "rehab"},
            "anomalies": [],
        }))
        return tmp_path

    def test_decompose_prompt_contains_real_keys_not_hallucinated(
        self, tmp_path,
    ):
        workspace = self._make_workspace(tmp_path)
        workspace_context = build_planner_workspace_context(str(workspace))

        assert workspace_context, (
            "helper should produce a non-empty summary for a workspace "
            "containing simulation_spec.json"
        )
        assert "simulation_spec.json" in workspace_context
        assert "metadata" in workspace_context
        assert "anomalies" in workspace_context

        steps_json = json.dumps(
            [{"title": "s1", "description": "d1", "depends_on": []}]
        )
        client = MagicMock()
        client.generate.return_value = (
            steps_json, {"input": 0, "output": 0},
        )

        with patch(
            "architect.planner.get_llm_client", return_value=client,
        ):
            decompose_goal("noop", workspace_context=workspace_context)

        prompts = [c.args[0] for c in client.generate.call_args_list]
        assert prompts, "expected at least one LLM call"
        decompose_prompt = prompts[0]

        # Real keys flow through into the prompt.
        assert "metadata" in decompose_prompt
        assert "anomalies" in decompose_prompt
        assert "simulation_spec.json" in decompose_prompt

        # Hallucinated keys from the failed rehab run never appear in
        # the planner template, so their absence here proves the planner
        # is grounded in the real schema rather than guessing.
        assert "temporal_patterns" not in decompose_prompt
        assert "sensory_lt" not in decompose_prompt
        assert "sensory_pp" not in decompose_prompt

    def test_research_prompt_contains_real_keys(self, tmp_path):
        workspace = self._make_workspace(tmp_path)
        workspace_context = build_planner_workspace_context(str(workspace))

        client = MagicMock()
        client.generate.return_value = (
            "research summary", {"input": 0, "output": 0},
        )

        with patch(
            "architect.planner.get_llm_client", return_value=client,
        ):
            research_goal("noop", workspace_context=workspace_context)

        prompts = [c.args[0] for c in client.generate.call_args_list]
        assert prompts
        research_prompt = prompts[0]
        assert "simulation_spec.json" in research_prompt
        assert "metadata" in research_prompt
        assert "temporal_patterns" not in research_prompt

    def test_spec_prompt_contains_real_keys(self, tmp_path):
        workspace = self._make_workspace(tmp_path)
        workspace_context = build_planner_workspace_context(str(workspace))

        client = MagicMock()
        client.generate.return_value = (
            "spec markdown", {"input": 0, "output": 0},
        )

        with patch(
            "architect.planner.get_llm_client", return_value=client,
        ):
            generate_project_spec(
                "noop", workspace_context=workspace_context,
            )

        prompts = [c.args[0] for c in client.generate.call_args_list]
        assert prompts
        spec_prompt = prompts[0]
        assert "simulation_spec.json" in spec_prompt
        assert "metadata" in spec_prompt
        assert "temporal_patterns" not in spec_prompt
