"""Tests for Section 2: Self-Consistency Planning (Multi-Plan Voting)."""

import json
from unittest.mock import patch, MagicMock, call

import pytest

from architect.planner import (
    estimate_complexity,
    score_plan,
    decompose_goal_with_voting,
)


class TestEstimateComplexity:
    @patch("architect.planner.get_llm_client")
    def test_returns_trivial(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "trivial"
        mock_get_client.return_value = client

        result = estimate_complexity("print hello world")
        assert result == "trivial"

    @patch("architect.planner.get_llm_client")
    def test_returns_simple(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "simple"
        mock_get_client.return_value = client

        result = estimate_complexity("download a file and parse it")
        assert result == "simple"

    @patch("architect.planner.get_llm_client")
    def test_returns_medium(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "medium"
        mock_get_client.return_value = client

        result = estimate_complexity("build a web scraper")
        assert result == "medium"

    @patch("architect.planner.get_llm_client")
    def test_returns_complex(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "complex"
        mock_get_client.return_value = client

        result = estimate_complexity("build a full web app with auth")
        assert result == "complex"

    @patch("architect.planner.get_llm_client")
    def test_extracts_from_sentence(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "I think this is medium complexity."
        mock_get_client.return_value = client

        result = estimate_complexity("some goal")
        assert result == "medium"

    @patch("architect.planner.get_llm_client")
    def test_unparseable_defaults_medium(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "I'm not sure"
        mock_get_client.return_value = client

        result = estimate_complexity("some goal")
        assert result == "medium"

    @patch("architect.planner.get_llm_client")
    def test_exception_defaults_medium(self, mock_get_client):
        client = MagicMock()
        client.generate.side_effect = RuntimeError("API down")
        mock_get_client.return_value = client

        result = estimate_complexity("some goal")
        assert result == "medium"

    @patch("architect.planner.get_llm_client")
    def test_prompt_includes_goal(self, mock_get_client):
        client = MagicMock()
        client.generate.return_value = "simple"
        mock_get_client.return_value = client

        estimate_complexity("build a calculator")
        prompt = client.generate.call_args[0][0]
        assert "build a calculator" in prompt

    @patch("architect.planner.get_llm_client")
    def test_first_matching_category_wins(self, mock_get_client):
        """If response contains multiple categories, the first match wins."""
        client = MagicMock()
        # "trivial" comes before "complex" in the check order
        client.generate.return_value = "trivial but could be complex"
        mock_get_client.return_value = client

        result = estimate_complexity("ambiguous goal")
        assert result == "trivial"


class TestScorePlan:
    def test_empty_plan(self):
        assert score_plan([]) == 0.0

    def test_single_step(self):
        steps = [{"title": "A", "description": "Do something specific", "depends_on": []}]
        score = score_plan(steps)
        assert score > 0.0
        # Single step: parallelism=0, compactness=1.0, specificity varies
        # compactness contribution: 1.0 * 0.3 = 0.3
        assert score >= 0.3

    def test_parallel_steps_score_higher(self):
        """Two parallel steps should score higher on parallelism than two sequential."""
        parallel = [
            {"title": "A", "description": "Do task A with details", "depends_on": []},
            {"title": "B", "description": "Do task B with details", "depends_on": []},
        ]
        sequential = [
            {"title": "A", "description": "Do task A with details", "depends_on": []},
            {"title": "B", "description": "Do task B with details", "depends_on": [1]},
        ]
        parallel_score = score_plan(parallel)
        sequential_score = score_plan(sequential)
        assert parallel_score > sequential_score

    def test_fewer_steps_score_higher_compactness(self):
        """Plan with fewer steps has higher compactness component."""
        short = [
            {"title": "A", "description": "x" * 200, "depends_on": []},
            {"title": "B", "description": "x" * 200, "depends_on": [1]},
        ]
        long = [
            {"title": "A", "description": "x" * 200, "depends_on": []},
            {"title": "B", "description": "x" * 200, "depends_on": [1]},
            {"title": "C", "description": "x" * 200, "depends_on": [2]},
            {"title": "D", "description": "x" * 200, "depends_on": [3]},
        ]
        # Compactness: 1/2=0.5 vs 1/4=0.25. Both sequential so parallelism=0.
        # Specificity same.
        short_score = score_plan(short)
        long_score = score_plan(long)
        assert short_score > long_score

    def test_more_specific_descriptions_score_higher(self):
        """Plans with longer descriptions score higher on specificity."""
        specific = [
            {"title": "A", "description": "x" * 400, "depends_on": []},
        ]
        vague = [
            {"title": "A", "description": "do it", "depends_on": []},
        ]
        assert score_plan(specific) > score_plan(vague)

    def test_specificity_capped_at_500(self):
        """Description lengths above 500 don't increase specificity further."""
        at_cap = [{"title": "A", "description": "x" * 500, "depends_on": []}]
        above_cap = [{"title": "A", "description": "x" * 1000, "depends_on": []}]
        assert score_plan(at_cap) == pytest.approx(score_plan(above_cap))

    def test_invalid_dag_gets_worst_parallelism(self):
        """Plans with circular deps get worst parallelism score."""
        # Circular: step 1 depends on step 2, step 2 depends on step 1
        steps = [
            {"title": "A", "description": "x" * 100, "depends_on": [2]},
            {"title": "B", "description": "x" * 100, "depends_on": [1]},
        ]
        score = score_plan(steps)
        # parallelism should be 0 (num_levels=n => ratio=0)
        assert score >= 0.0

    def test_score_between_zero_and_one(self):
        """Score should always be in [0, 1] range."""
        plans = [
            [{"title": "A", "description": "d", "depends_on": []}],
            [
                {"title": "A", "description": "x" * 500, "depends_on": []},
                {"title": "B", "description": "x" * 500, "depends_on": []},
                {"title": "C", "description": "x" * 500, "depends_on": [1, 2]},
            ],
        ]
        for plan in plans:
            score = score_plan(plan)
            assert 0.0 <= score <= 1.0


class TestDecomposeGoalWithVoting:
    def _make_plan_json(self, n, prefix="step"):
        """Create a valid JSON response for n steps."""
        steps = []
        for i in range(1, n + 1):
            steps.append({
                "title": f"{prefix}{i}",
                "description": f"Do {prefix}{i} with detailed instructions for the task",
                "depends_on": [i - 1] if i > 1 else [],
                "verify": f"Check {prefix}{i}",
                "environment": [],
            })
        return json.dumps(steps)

    @patch("architect.planner.decompose_goal")
    @patch("architect.planner.estimate_complexity")
    def test_trivial_skips_voting(self, mock_complexity, mock_decompose):
        mock_complexity.return_value = "trivial"
        mock_decompose.return_value = [
            {"title": "A", "description": "do A", "depends_on": [],
             "verify": "", "environment": []}
        ]

        result = decompose_goal_with_voting("print hello")
        mock_decompose.assert_called_once_with("print hello", spec="")
        assert len(result) == 1

    @patch("architect.planner.decompose_goal")
    @patch("architect.planner.estimate_complexity")
    def test_simple_skips_voting(self, mock_complexity, mock_decompose):
        mock_complexity.return_value = "simple"
        mock_decompose.return_value = [
            {"title": "A", "description": "do A", "depends_on": [],
             "verify": "", "environment": []}
        ]

        result = decompose_goal_with_voting("simple task")
        mock_decompose.assert_called_once()
        assert len(result) == 1

    @patch("architect.planner.get_llm_client")
    @patch("architect.planner.estimate_complexity")
    def test_medium_uses_voting(self, mock_complexity, mock_get_client):
        mock_complexity.return_value = "medium"
        client = MagicMock()
        plan_json = self._make_plan_json(3)
        analysis = "<analysis>Analysis here</analysis>\n<complexity_assessment>medium</complexity_assessment>\n"
        client.generate.return_value = analysis + plan_json
        mock_get_client.return_value = client

        result = decompose_goal_with_voting("build a scraper", n_samples=3)
        # Should have called generate 4 times (3 plan variants + 1 plan selection)
        assert client.generate.call_count == 4
        assert len(result) == 3

    @patch("architect.planner.get_llm_client")
    @patch("architect.planner.estimate_complexity")
    def test_complex_uses_voting(self, mock_complexity, mock_get_client):
        mock_complexity.return_value = "complex"
        client = MagicMock()
        plan_json = self._make_plan_json(5)
        analysis = "<analysis>Complex task</analysis>\n<complexity_assessment>complex</complexity_assessment>\n"
        client.generate.return_value = analysis + plan_json
        mock_get_client.return_value = client

        result = decompose_goal_with_voting("full web app", n_samples=3)
        # 3 plan variants + 1 plan selection
        assert client.generate.call_count == 4
        assert len(result) == 5

    @patch("architect.planner.get_llm_client")
    @patch("architect.planner.estimate_complexity")
    def test_selects_best_scoring_plan(self, mock_complexity, mock_get_client):
        """When multiple plans are generated, the best-scoring one is selected."""
        mock_complexity.return_value = "medium"
        client = MagicMock()

        # Plan A: 5 sequential steps with short descriptions
        plan_a = json.dumps([
            {"title": f"s{i}", "description": f"d{i}", "depends_on": [i-1] if i > 1 else [],
             "verify": "", "environment": []}
            for i in range(1, 6)
        ])
        # Plan B: 3 steps, 2 parallel + 1 dependent, with long descriptions
        plan_b = json.dumps([
            {"title": "fetch_a", "description": "Fetch data from source A with detailed retry logic and error handling " * 5,
             "depends_on": [], "verify": "", "environment": []},
            {"title": "fetch_b", "description": "Fetch data from source B with detailed retry logic and error handling " * 5,
             "depends_on": [], "verify": "", "environment": []},
            {"title": "merge", "description": "Merge data from both sources with detailed validation " * 5,
             "depends_on": [1, 2], "verify": "", "environment": []},
        ])
        # Plan C: invalid JSON (should be filtered out)
        plan_c = "not valid json at all"

        analysis = "<analysis>Some analysis</analysis>\n<complexity_assessment>medium</complexity_assessment>\n"
        client.generate.side_effect = [
            analysis + plan_a,
            analysis + plan_b,
            plan_c,
        ]
        mock_get_client.return_value = client

        result = decompose_goal_with_voting("some medium goal", n_samples=3)
        # Plan B should win: fewer steps (compactness), parallel (parallelism),
        # longer descriptions (specificity)
        assert len(result) == 3
        assert result[0]["title"] == "fetch_a"

    @patch("architect.planner.decompose_goal")
    @patch("architect.planner.get_llm_client")
    @patch("architect.planner.estimate_complexity")
    def test_all_plans_fail_falls_back(self, mock_complexity, mock_get_client, mock_decompose):
        """If all voting plans fail, falls back to single decompose_goal."""
        mock_complexity.return_value = "medium"
        client = MagicMock()
        client.generate.side_effect = RuntimeError("API error")
        mock_get_client.return_value = client

        mock_decompose.return_value = [
            {"title": "fallback", "description": "fallback plan", "depends_on": [],
             "verify": "", "environment": []}
        ]

        result = decompose_goal_with_voting("goal", n_samples=3)
        mock_decompose.assert_called_once()
        assert result[0]["title"] == "fallback"

    @patch("architect.planner.get_llm_client")
    @patch("architect.planner.estimate_complexity")
    def test_single_valid_plan_used_directly(self, mock_complexity, mock_get_client):
        """If only one plan is valid out of N, use it without scoring."""
        mock_complexity.return_value = "complex"
        client = MagicMock()

        good_plan = json.dumps([
            {"title": "A", "description": "do A well", "depends_on": [],
             "verify": "", "environment": []}
        ])
        analysis = "<analysis>x</analysis>\n<complexity_assessment>x</complexity_assessment>\n"
        client.generate.side_effect = [
            analysis + good_plan,
            "invalid json",
            "also invalid",
        ]
        mock_get_client.return_value = client

        result = decompose_goal_with_voting("some goal", n_samples=3)
        assert len(result) == 1
        assert result[0]["title"] == "A"

    @patch("architect.planner.estimate_complexity")
    def test_stores_complexity(self, mock_complexity):
        """last_complexity attribute is set after call."""
        mock_complexity.return_value = "trivial"

        with patch("architect.planner.decompose_goal") as mock_decompose:
            mock_decompose.return_value = [
                {"title": "A", "description": "d", "depends_on": [],
                 "verify": "", "environment": []}
            ]
            decompose_goal_with_voting("goal")
            assert decompose_goal_with_voting.last_complexity == "trivial"

    @patch("architect.planner.get_llm_client")
    @patch("architect.planner.estimate_complexity")
    def test_voting_uses_different_suffixes(self, mock_complexity, mock_get_client):
        """Each plan generation should use a different prompt suffix."""
        mock_complexity.return_value = "medium"
        client = MagicMock()
        plan_json = self._make_plan_json(3)
        analysis = "<analysis>x</analysis>\n<complexity_assessment>x</complexity_assessment>\n"
        client.generate.return_value = analysis + plan_json
        mock_get_client.return_value = client

        decompose_goal_with_voting("some goal", n_samples=3)
        prompts = [c[0][0] for c in client.generate.call_args_list]

        # At least one prompt should contain "SIMPLICITY" and one "ROBUSTNESS"
        prompt_texts = " ".join(prompts)
        assert "SIMPLICITY" in prompt_texts
        assert "ROBUSTNESS" in prompt_texts

    @patch("architect.planner.get_llm_client")
    @patch("architect.planner.estimate_complexity")
    def test_zero_index_normalization_in_voting(self, mock_complexity, mock_get_client):
        """Plans with 0-based depends_on should be normalized to 1-based."""
        mock_complexity.return_value = "medium"
        client = MagicMock()
        # Plan with 0-based deps
        plan = json.dumps([
            {"title": "A", "description": "do A in detail", "depends_on": [],
             "verify": "", "environment": []},
            {"title": "B", "description": "do B in detail", "depends_on": [0],
             "verify": "", "environment": []},
        ])
        analysis = "<analysis>x</analysis>\n<complexity_assessment>x</complexity_assessment>\n"
        client.generate.return_value = analysis + plan
        mock_get_client.return_value = client

        result = decompose_goal_with_voting("goal", n_samples=1)
        assert result[1]["depends_on"] == [1]


