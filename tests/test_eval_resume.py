"""Tests for the resume-from-JSONL layer in integration/eval.py.

Phase 1 PLAN Section 10. Validates ``load_prior_rows`` filtering
semantics and the ``--no-resume`` CLI bypass. No LLM, no container,
fully synthetic.
"""

import json
import os
import sys

import pytest

_INTEG_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "integration")
)
if _INTEG_DIR not in sys.path:
    sys.path.insert(0, _INTEG_DIR)

import eval as ev  # noqa: E402


@pytest.fixture
def jsonl_path(tmp_path):
    return str(tmp_path / "eval_results.jsonl")


@pytest.fixture
def metadata():
    return {
        "git_sha": "abc123" + "0" * 34,
        "git_branch": "main",
        "git_dirty": False,
        "timestamp_utc": "2025-04-08T10:00:00+00:00",
        "env_snapshot": {"UAS_TEST": "x"},
        "config_hash": "deadbeef" + "0" * 56,
        "harness_version": "phase1",
    }


def _write_rows(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r))
            f.write("\n")


def _row(metadata, *, run_index, name, passed=True, **overrides):
    base = {
        **metadata,
        "run_index": run_index,
        "name": name,
        "passed": passed,
        "elapsed": 1.0,
        "tier": "trivial",
    }
    base.update(overrides)
    return base


class TestLoadPriorRowsFiltering:
    def test_missing_file_returns_empty(self, jsonl_path, metadata):
        assert not os.path.exists(jsonl_path)
        assert ev.load_prior_rows(jsonl_path, metadata) == []

    def test_none_path_returns_empty(self, metadata):
        assert ev.load_prior_rows(None, metadata) == []
        assert ev.load_prior_rows("", metadata) == []

    def test_empty_file_returns_empty(self, jsonl_path, metadata):
        open(jsonl_path, "w").close()
        assert ev.load_prior_rows(jsonl_path, metadata) == []

    def test_blank_lines_skipped(self, jsonl_path, metadata):
        rows = [_row(metadata, run_index=0, name="hello-file")]
        _write_rows(jsonl_path, rows)
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write("\n   \n\n")
        got = ev.load_prior_rows(jsonl_path, metadata)
        assert len(got) == 1

    def test_all_matching_rows_returned(self, jsonl_path, metadata):
        rows = [
            _row(metadata, run_index=0, name="hello-file"),
            _row(metadata, run_index=0, name="hello-json"),
            _row(metadata, run_index=1, name="hello-file"),
        ]
        _write_rows(jsonl_path, rows)
        got = ev.load_prior_rows(jsonl_path, metadata)
        assert len(got) == 3
        keys = {(r["run_index"], r["name"]) for r in got}
        assert keys == {
            (0, "hello-file"), (0, "hello-json"), (1, "hello-file"),
        }

    def test_mismatched_git_sha_filtered(self, jsonl_path, metadata):
        other_meta = {**metadata, "git_sha": "ffff" + "0" * 36}
        rows = [
            _row(metadata, run_index=0, name="keep-me"),
            _row(other_meta, run_index=0, name="drop-me"),
        ]
        _write_rows(jsonl_path, rows)
        got = ev.load_prior_rows(jsonl_path, metadata)
        names = {r["name"] for r in got}
        assert names == {"keep-me"}

    def test_mismatched_git_dirty_filtered(self, jsonl_path, metadata):
        dirty_meta = {**metadata, "git_dirty": True}
        rows = [
            _row(metadata, run_index=0, name="clean-row"),
            _row(dirty_meta, run_index=0, name="dirty-row"),
        ]
        _write_rows(jsonl_path, rows)
        got = ev.load_prior_rows(jsonl_path, metadata)
        names = {r["name"] for r in got}
        assert names == {"clean-row"}

    def test_mismatched_harness_version_filtered(
        self, jsonl_path, metadata
    ):
        old_meta = {**metadata, "harness_version": "phase0"}
        rows = [
            _row(metadata, run_index=0, name="new-row"),
            _row(old_meta, run_index=0, name="old-row"),
        ]
        _write_rows(jsonl_path, rows)
        got = ev.load_prior_rows(jsonl_path, metadata)
        names = {r["name"] for r in got}
        assert names == {"new-row"}

    def test_missing_run_index_filtered(self, jsonl_path, metadata):
        rows = [_row(metadata, run_index=0, name="has-idx")]
        _write_rows(jsonl_path, rows)
        with open(jsonl_path, "a", encoding="utf-8") as f:
            bad = {**metadata, "name": "no-idx"}
            f.write(json.dumps(bad) + "\n")
        got = ev.load_prior_rows(jsonl_path, metadata)
        names = {r["name"] for r in got}
        assert names == {"has-idx"}

    def test_missing_name_filtered(self, jsonl_path, metadata):
        rows = [_row(metadata, run_index=0, name="has-name")]
        _write_rows(jsonl_path, rows)
        with open(jsonl_path, "a", encoding="utf-8") as f:
            bad = {**metadata, "run_index": 1}
            f.write(json.dumps(bad) + "\n")
        got = ev.load_prior_rows(jsonl_path, metadata)
        names = {r["name"] for r in got}
        assert names == {"has-name"}

    def test_corrupt_line_skipped(self, jsonl_path, metadata, capsys):
        good_row = _row(metadata, run_index=0, name="good")
        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.write("not-json\n")
            f.write(json.dumps(good_row) + "\n")
            f.write("{also-not-json\n")
        got = ev.load_prior_rows(jsonl_path, metadata)
        names = {r["name"] for r in got}
        assert names == {"good"}
        err = capsys.readouterr().err
        assert "corrupt JSONL line 1" in err
        assert "corrupt JSONL line 3" in err

    def test_preserves_row_shape(self, jsonl_path, metadata):
        """Returned rows are the full dicts (used as aggregate input)."""
        rich = _row(
            metadata, run_index=0, name="hello",
            output={
                "steps": [
                    {"timing": {"llm_time": 5.0, "sandbox_time": 1.0}},
                ],
                "total_tokens": {"input": 10, "output": 20},
                "attempt_total": 2,
            },
        )
        _write_rows(jsonl_path, [rich])
        got = ev.load_prior_rows(jsonl_path, metadata)
        assert len(got) == 1
        assert got[0]["output"]["total_tokens"] == {
            "input": 10, "output": 20,
        }
        assert got[0]["output"]["attempt_total"] == 2


class TestResumeIntegratesWithAggregate:
    def test_prior_rows_flow_through_aggregate_results(
        self, jsonl_path, metadata
    ):
        """Resumed rows should produce the same aggregate as fresh rows."""
        rows = [
            _row(
                metadata, run_index=0, name="hello",
                elapsed=10.0,
                output={
                    "steps": [{"timing": {
                        "llm_time": 3.0, "sandbox_time": 0.5,
                    }}],
                    "total_tokens": {"input": 100, "output": 200},
                    "attempt_total": 1,
                },
            ),
            _row(
                metadata, run_index=1, name="hello",
                elapsed=12.0,
                output={
                    "steps": [{"timing": {
                        "llm_time": 4.0, "sandbox_time": 0.6,
                    }}],
                    "total_tokens": {"input": 110, "output": 220},
                    "attempt_total": 2,
                },
            ),
        ]
        _write_rows(jsonl_path, rows)
        got = ev.load_prior_rows(jsonl_path, metadata)
        agg = ev.aggregate_results(got)
        assert agg["hello"]["n_runs"] == 2
        assert agg["hello"]["pass_rate_mean"] == 1.0
        assert agg["hello"]["elapsed_mean"] == 11.0
        assert agg["hello"]["tokens_input_mean"] == 105.0
        assert agg["hello"]["attempts_mean"] == 1.5


class TestNoResumeBypassesLoad:
    """main() should skip load_prior_rows() entirely when --no-resume."""

    def test_flag_short_circuits_resume(
        self, tmp_path, monkeypatch, metadata
    ):
        jsonl = str(tmp_path / "r.jsonl")
        _write_rows(jsonl, [_row(metadata, run_index=0, name="hello")])

        # Force load_prior_rows to explode if called — proves the flag
        # short-circuits before the read.
        def _boom(*a, **kw):
            raise AssertionError("load_prior_rows was called")
        monkeypatch.setattr(ev, "load_prior_rows", _boom)
        monkeypatch.setattr(ev, "capture_run_metadata", lambda: metadata)

        calls = []

        def fake_run_case(case, **kw):
            calls.append(case["name"])
            return {
                "name": case["name"], "passed": True, "elapsed": 0.1,
                "checks": [], "exit_code": 0, "workspace": "/tmp/x",
                "goal": case["goal"],
            }
        monkeypatch.setattr(ev, "run_case", fake_run_case)
        monkeypatch.setattr(ev, "_find_engine", lambda: None)
        monkeypatch.setattr(ev, "_maybe_refresh_oauth", lambda: None)
        monkeypatch.setattr(
            ev, "load_prompts",
            lambda filter_pattern=None, tier=None: [
                {"name": "hello", "goal": "g", "tier": "trivial",
                 "checks": []},
            ],
        )

        monkeypatch.setattr(
            sys, "argv",
            [
                "eval.py", "--runs", "1",
                "--results-out", jsonl,
                "--no-resume",
                "--local",
            ],
        )
        monkeypatch.setattr(
            ev, "RESULTS_FILE", str(tmp_path / "results.json"),
        )
        monkeypatch.setattr(
            ev, "RESULTS_AGGREGATE", str(tmp_path / "agg.json"),
        )
        rc = ev.main()
        assert rc == 0
        # run_case was called (not short-circuited by a stale resume).
        assert calls == ["hello"]


class TestResumeSkipsMatchingCases:
    def test_resumed_case_not_rerun(
        self, tmp_path, monkeypatch, metadata
    ):
        jsonl = str(tmp_path / "r.jsonl")
        _write_rows(
            jsonl,
            [_row(metadata, run_index=0, name="hello")],
        )
        monkeypatch.setattr(ev, "capture_run_metadata", lambda: metadata)
        monkeypatch.setattr(ev, "_find_engine", lambda: None)
        monkeypatch.setattr(ev, "_maybe_refresh_oauth", lambda: None)
        monkeypatch.setattr(
            ev, "load_prompts",
            lambda filter_pattern=None, tier=None: [
                {"name": "hello", "goal": "g", "tier": "trivial",
                 "checks": []},
                {"name": "world", "goal": "g", "tier": "trivial",
                 "checks": []},
            ],
        )

        ran = []

        def fake_run_case(case, **kw):
            ran.append(case["name"])
            return {
                "name": case["name"], "passed": True, "elapsed": 0.1,
                "checks": [], "exit_code": 0, "workspace": "/tmp/x",
                "goal": case["goal"],
            }
        monkeypatch.setattr(ev, "run_case", fake_run_case)
        monkeypatch.setattr(
            sys, "argv",
            [
                "eval.py", "--runs", "1",
                "--results-out", jsonl,
                "--local",
            ],
        )
        monkeypatch.setattr(
            ev, "RESULTS_FILE", str(tmp_path / "results.json"),
        )
        monkeypatch.setattr(
            ev, "RESULTS_AGGREGATE", str(tmp_path / "agg.json"),
        )

        rc = ev.main()
        assert rc == 0
        # `hello` was resumed; only `world` was re-run.
        assert ran == ["world"]

        # Aggregate includes BOTH resumed and newly-run rows.
        with open(str(tmp_path / "agg.json")) as f:
            agg = json.load(f)
        assert set(agg["by_case"].keys()) == {"hello", "world"}
        assert agg["by_case"]["hello"]["n_runs"] == 1
        assert agg["by_case"]["world"]["n_runs"] == 1
