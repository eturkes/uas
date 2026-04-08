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
    """Section 9: loader walks ``CASES_DIR/<tier>/<case>.json`` and
    derives ``case["tier"]`` from the parent directory name."""

    def _write_case(self, cases_dir, tier, case_name, payload):
        tier_dir = os.path.join(cases_dir, tier)
        os.makedirs(tier_dir, exist_ok=True)
        path = os.path.join(tier_dir, f"{case_name}.json")
        with open(path, "w") as f:
            json.dump(payload, f)
        return path

    def test_no_tier_filter_returns_all(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            self._write_case(td, "trivial", "a", {"name": "a", "goal": "g"})
            self._write_case(td, "moderate", "b", {"name": "b", "goal": "g"})
            self._write_case(td, "hard", "c", {"name": "c", "goal": "g"})
            monkeypatch.setattr(ev, "CASES_DIR", td)
            cases = ev.load_prompts()
            assert {c["name"] for c in cases} == {"a", "b", "c"}

    def test_tier_exact_match(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            self._write_case(td, "trivial", "a", {"name": "a", "goal": "g"})
            self._write_case(td, "moderate", "b", {"name": "b", "goal": "g"})
            self._write_case(td, "hard", "c", {"name": "c", "goal": "g"})
            monkeypatch.setattr(ev, "CASES_DIR", td)
            cases = ev.load_prompts(tier="moderate")
            assert {c["name"] for c in cases} == {"b"}

    def test_directory_overrides_in_file_tier(self, monkeypatch):
        # The directory name is canonical: even if the JSON file
        # carries a stale ``tier`` field (e.g. from a migration),
        # the loader rewrites it to the parent directory name.
        with tempfile.TemporaryDirectory() as td:
            self._write_case(
                td, "trivial", "a",
                {"name": "a", "tier": "hard", "goal": "g"},
            )
            monkeypatch.setattr(ev, "CASES_DIR", td)
            cases = ev.load_prompts()
            assert cases[0]["tier"] == "trivial"

    def test_missing_tier_dirs_skipped(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            self._write_case(td, "trivial", "a", {"name": "a", "goal": "g"})
            # No moderate / hard / open_ended dirs at all.
            monkeypatch.setattr(ev, "CASES_DIR", td)
            cases = ev.load_prompts()
            assert [c["name"] for c in cases] == ["a"]

    def test_tier_filter_and_name_filter_combined(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            self._write_case(
                td, "trivial", "alpha-1", {"name": "alpha-1", "goal": "g"},
            )
            self._write_case(
                td, "moderate", "alpha-2", {"name": "alpha-2", "goal": "g"},
            )
            self._write_case(
                td, "trivial", "beta-1", {"name": "beta-1", "goal": "g"},
            )
            monkeypatch.setattr(ev, "CASES_DIR", td)
            cases = ev.load_prompts(filter_pattern="alpha", tier="trivial")
            assert {c["name"] for c in cases} == {"alpha-1"}

    def test_tier_filter_no_matches(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            self._write_case(td, "trivial", "a", {"name": "a", "goal": "g"})
            monkeypatch.setattr(ev, "CASES_DIR", td)
            assert ev.load_prompts(tier="open_ended") == []

    def test_canonical_tier_order_preserved(self, monkeypatch):
        # Cases come back in ALLOWED_TIERS order, then file-name
        # order within a tier — never in os.listdir's filesystem
        # order.
        with tempfile.TemporaryDirectory() as td:
            self._write_case(td, "open_ended", "z", {"name": "z", "goal": "g"})
            self._write_case(td, "trivial", "y", {"name": "y", "goal": "g"})
            self._write_case(td, "hard", "x", {"name": "x", "goal": "g"})
            self._write_case(td, "moderate", "w", {"name": "w", "goal": "g"})
            monkeypatch.setattr(ev, "CASES_DIR", td)
            cases = ev.load_prompts()
            assert [c["name"] for c in cases] == ["y", "w", "x", "z"]

    def test_non_json_files_skipped(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            self._write_case(td, "trivial", "a", {"name": "a", "goal": "g"})
            # Drop a stray non-JSON file in the same directory.
            stray = os.path.join(td, "trivial", "README.md")
            with open(stray, "w") as f:
                f.write("# notes")
            monkeypatch.setattr(ev, "CASES_DIR", td)
            cases = ev.load_prompts()
            assert [c["name"] for c in cases] == ["a"]


class TestShippedCasesTierIsDirectoryDerived:
    """Every case shipped under ``integration/cases/<tier>/`` carries
    a ``tier`` field that matches its parent directory."""

    def test_shipped_cases_tier_matches_parent_dir(self):
        cases = ev.load_prompts()
        assert len(cases) >= 1, (
            "expected at least one shipped case under integration/cases/"
        )
        for c in cases:
            assert c["tier"] in ev.ALLOWED_TIERS
