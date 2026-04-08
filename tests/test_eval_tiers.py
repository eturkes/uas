"""Tests for the tier schema and tiered reporting in integration/eval.py.

Phase 1 PLAN Section 7. Validates ``aggregate_by_tier``,
``load_prompts(tier=...)`` filter, the silent tier-default backfill,
and the ``ALLOWED_TIERS`` constant. No LLM, no container.
"""

import json
import os
import sys
import tempfile

import pytest

_INTEG_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "integration")
)
if _INTEG_DIR not in sys.path:
    sys.path.insert(0, _INTEG_DIR)

import eval as ev  # noqa: E402


def _row(name, tier, passed=True):
    return {
        "name": name,
        "tier": tier,
        "goal": "x",
        "workspace": "/tmp",
        "checks": [],
        "exit_code": 0 if passed else 1,
        "elapsed": 1.0,
        "passed": passed,
    }


class TestAllowedTiersConstant:
    def test_canonical_order(self):
        assert ev.ALLOWED_TIERS == (
            "trivial", "moderate", "hard", "open_ended"
        )


class TestAggregateByTier:
    def test_empty_input(self):
        assert ev.aggregate_by_tier([]) == {}

    def test_single_tier_single_row(self):
        out = ev.aggregate_by_tier([_row("a", "trivial")])
        assert "trivial" in out
        t = out["trivial"]
        assert t["pass_rate_mean"] == 1.0
        assert t["pass_rate_stdev"] == 0.0
        assert t["n_cases"] == 1
        assert t["n_rows"] == 1

    def test_n_cases_distinct(self):
        rows = [
            _row("a", "trivial"),
            _row("a", "trivial"),  # same case, second run
            _row("b", "trivial"),
        ]
        out = ev.aggregate_by_tier(rows)
        assert out["trivial"]["n_cases"] == 2  # a, b
        assert out["trivial"]["n_rows"] == 3   # a, a, b

    def test_multiple_tiers_independent(self):
        rows = [
            _row("a", "trivial"),
            _row("b", "moderate"),
            _row("c", "hard"),
            _row("d", "open_ended"),
        ]
        out = ev.aggregate_by_tier(rows)
        assert set(out.keys()) == {
            "trivial", "moderate", "hard", "open_ended",
        }
        for t in out.values():
            assert t["n_cases"] == 1
            assert t["pass_rate_mean"] == 1.0

    def test_pass_rate_mixed(self):
        rows = [
            _row("a", "hard", passed=True),
            _row("b", "hard", passed=False),
            _row("c", "hard", passed=True),
            _row("d", "hard", passed=True),
        ]
        out = ev.aggregate_by_tier(rows)
        assert out["hard"]["pass_rate_mean"] == 0.75
        assert out["hard"]["n_cases"] == 4
        assert out["hard"]["n_rows"] == 4

    def test_default_tier_when_missing(self):
        # Row without explicit tier should land in "trivial"
        row = {
            "name": "x", "goal": "y", "workspace": "/tmp",
            "checks": [], "exit_code": 0, "elapsed": 1.0, "passed": True,
        }
        out = ev.aggregate_by_tier([row])
        assert "trivial" in out
        assert out["trivial"]["n_cases"] == 1


class TestLoadPromptsTierFilter:
    def _write_prompts(self, td, prompts):
        path = os.path.join(td, "prompts.json")
        with open(path, "w") as f:
            json.dump(prompts, f)
        return path

    def test_no_tier_filter_returns_all(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_prompts(td, [
                {"name": "a", "tier": "trivial", "goal": "g"},
                {"name": "b", "tier": "moderate", "goal": "g"},
                {"name": "c", "tier": "hard", "goal": "g"},
            ])
            monkeypatch.setattr(ev, "PROMPTS_FILE", path)
            cases = ev.load_prompts()
            assert {c["name"] for c in cases} == {"a", "b", "c"}

    def test_tier_exact_match(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_prompts(td, [
                {"name": "a", "tier": "trivial", "goal": "g"},
                {"name": "b", "tier": "moderate", "goal": "g"},
                {"name": "c", "tier": "hard", "goal": "g"},
            ])
            monkeypatch.setattr(ev, "PROMPTS_FILE", path)
            cases = ev.load_prompts(tier="moderate")
            assert {c["name"] for c in cases} == {"b"}

    def test_missing_tier_defaults_to_trivial(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_prompts(td, [
                {"name": "no-tier", "goal": "g"},
                {"name": "yes-tier", "tier": "hard", "goal": "g"},
            ])
            monkeypatch.setattr(ev, "PROMPTS_FILE", path)
            cases = ev.load_prompts()
            no_tier_case = next(c for c in cases if c["name"] == "no-tier")
            assert no_tier_case["tier"] == "trivial"

    def test_tier_filter_and_name_filter_combined(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_prompts(td, [
                {"name": "alpha-1", "tier": "trivial", "goal": "g"},
                {"name": "alpha-2", "tier": "moderate", "goal": "g"},
                {"name": "beta-1", "tier": "trivial", "goal": "g"},
            ])
            monkeypatch.setattr(ev, "PROMPTS_FILE", path)
            cases = ev.load_prompts(filter_pattern="alpha", tier="trivial")
            assert {c["name"] for c in cases} == {"alpha-1"}

    def test_tier_filter_no_matches(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_prompts(td, [
                {"name": "a", "tier": "trivial", "goal": "g"},
            ])
            monkeypatch.setattr(ev, "PROMPTS_FILE", path)
            assert ev.load_prompts(tier="open_ended") == []


class TestExistingPromptsBackfilled:
    """The 4 cases shipped in prompts.json should all carry tier=trivial."""

    def test_all_existing_cases_are_trivial(self):
        cases = ev.load_prompts()
        assert len(cases) == 4
        for c in cases:
            assert c["tier"] == "trivial"
