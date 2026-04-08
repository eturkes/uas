"""Tests for the multi-run aggregation in integration/eval.py.

Phase 1 PLAN Section 6. Validates ``aggregate_results`` math against
``statistics.pstdev`` and the metric extraction from per-row Section 1
output. No LLM, no container.
"""

import os
import statistics
import sys

import pytest  # noqa: F401

_INTEG_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "integration")
)
if _INTEG_DIR not in sys.path:
    sys.path.insert(0, _INTEG_DIR)

import eval as ev  # noqa: E402


def _make_row(
    name, passed=True, elapsed=1.0, llm_time=0.5, sandbox_time=0.5,
    attempts=1, tok_in=10, tok_out=20,
):
    """Build a synthetic result row in the same shape run_case returns."""
    return {
        "name": name,
        "goal": "x",
        "workspace": "/tmp",
        "checks": [],
        "exit_code": 0 if passed else 1,
        "elapsed": elapsed,
        "passed": passed,
        "output": {
            "status": "completed" if passed else "failed",
            "steps": [{
                "id": 1,
                "title": "step",
                "status": "completed" if passed else "failed",
                "elapsed": elapsed,
                "timing": {
                    "llm_time": llm_time,
                    "sandbox_time": sandbox_time,
                    "total_time": llm_time + sandbox_time,
                },
            }],
            "step_count": 1,
            "step_status_counts": {
                ("completed" if passed else "failed"): 1,
            },
            "attempt_total": attempts,
            "total_elapsed": elapsed,
            "total_tokens": {"input": tok_in, "output": tok_out},
            "total_cost_usd": 0.001,
            "workspace_size_bytes": 100,
        },
    }


class TestAggregateResults:
    def test_empty_input(self):
        assert ev.aggregate_results([]) == {}

    def test_single_run_single_case(self):
        agg = ev.aggregate_results([_make_row("hello", elapsed=2.0)])
        h = agg["hello"]
        assert h["n_runs"] == 1
        assert h["pass_rate_mean"] == 1.0
        assert h["pass_rate_stdev"] == 0.0
        assert h["elapsed_mean"] == 2.0
        assert h["elapsed_stdev"] == 0.0

    def test_three_runs_all_pass(self):
        rows = [
            _make_row("hello", elapsed=1.0),
            _make_row("hello", elapsed=2.0),
            _make_row("hello", elapsed=3.0),
        ]
        agg = ev.aggregate_results(rows)
        h = agg["hello"]
        assert h["n_runs"] == 3
        assert h["pass_rate_mean"] == 1.0
        assert h["elapsed_mean"] == 2.0
        # population stdev of [1, 2, 3]
        assert (
            abs(h["elapsed_stdev"] - statistics.pstdev([1.0, 2.0, 3.0]))
            < 1e-9
        )

    def test_mixed_pass_fail(self):
        rows = [
            _make_row("x", passed=True),
            _make_row("x", passed=False),
            _make_row("x", passed=True),
        ]
        agg = ev.aggregate_results(rows)
        # 2 of 3 passed → 0.6667
        assert abs(agg["x"]["pass_rate_mean"] - 2 / 3) < 1e-9
        # pstdev of [1, 0, 1]
        expected_stdev = statistics.pstdev([1.0, 0.0, 1.0])
        assert abs(agg["x"]["pass_rate_stdev"] - expected_stdev) < 1e-9

    def test_all_fail(self):
        rows = [_make_row("z", passed=False) for _ in range(3)]
        agg = ev.aggregate_results(rows)
        assert agg["z"]["pass_rate_mean"] == 0.0
        assert agg["z"]["pass_rate_stdev"] == 0.0

    def test_token_aggregation(self):
        rows = [
            _make_row("y", tok_in=100, tok_out=200),
            _make_row("y", tok_in=200, tok_out=400),
        ]
        agg = ev.aggregate_results(rows)
        assert agg["y"]["tokens_input_mean"] == 150
        assert agg["y"]["tokens_output_mean"] == 300
        assert (
            abs(agg["y"]["tokens_input_stdev"]
                - statistics.pstdev([100, 200])) < 1e-9
        )

    def test_llm_and_sandbox_time(self):
        rows = [
            _make_row("z", llm_time=2.0, sandbox_time=1.0),
            _make_row("z", llm_time=4.0, sandbox_time=3.0),
        ]
        agg = ev.aggregate_results(rows)
        assert agg["z"]["llm_time_mean"] == 3.0
        assert agg["z"]["sandbox_time_mean"] == 2.0

    def test_attempts_aggregation(self):
        rows = [
            _make_row("a", attempts=1),
            _make_row("a", attempts=3),
            _make_row("a", attempts=2),
        ]
        agg = ev.aggregate_results(rows)
        assert agg["a"]["attempts_mean"] == 2.0

    def test_no_output_field_defaults_to_zero(self):
        # Error-path row: invocation crashed before output.json
        row = {
            "name": "missing-output",
            "goal": "x",
            "workspace": "/tmp",
            "checks": [],
            "exit_code": -1,
            "elapsed": 5.0,
            "passed": False,
            "error": "boom",
        }
        agg = ev.aggregate_results([row])
        h = agg["missing-output"]
        assert h["n_runs"] == 1
        assert h["pass_rate_mean"] == 0.0
        assert h["elapsed_mean"] == 5.0
        assert h["llm_time_mean"] == 0.0
        assert h["sandbox_time_mean"] == 0.0
        assert h["attempts_mean"] == 0
        assert h["tokens_input_mean"] == 0
        assert h["tokens_output_mean"] == 0

    def test_multiple_cases_independent(self):
        rows = [
            _make_row("a", elapsed=1.0),
            _make_row("b", elapsed=10.0),
            _make_row("a", elapsed=3.0),
            _make_row("b", elapsed=20.0),
        ]
        agg = ev.aggregate_results(rows)
        assert "a" in agg and "b" in agg
        assert agg["a"]["n_runs"] == 2
        assert agg["b"]["n_runs"] == 2
        assert agg["a"]["elapsed_mean"] == 2.0
        assert agg["b"]["elapsed_mean"] == 15.0

    def test_aggregate_keys_complete(self):
        # Verify every documented metric key is present
        agg = ev.aggregate_results([_make_row("k")])
        h = agg["k"]
        expected_keys = {
            "n_runs",
            "pass_rate_mean", "pass_rate_stdev",
            "elapsed_mean", "elapsed_stdev",
            "llm_time_mean", "llm_time_stdev",
            "sandbox_time_mean", "sandbox_time_stdev",
            "attempts_mean", "attempts_stdev",
            "tokens_input_mean", "tokens_input_stdev",
            "tokens_output_mean", "tokens_output_stdev",
        }
        assert set(h.keys()) == expected_keys
