"""Tests for the JSONL persistence layer in integration/eval.py.

Phase 1 PLAN Section 5. Validates ``append_result_row`` shape, append
semantics, metadata stamping, and the ``--results-out`` override path.
No LLM, no container, fully synthetic.
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
def fake_metadata():
    return {
        "git_sha": "abc123" + "0" * 34,
        "git_branch": "main",
        "git_dirty": False,
        "timestamp_utc": "2025-04-08T10:00:00+00:00",
        "env_snapshot": {"UAS_TEST": "x"},
        "config_hash": "deadbeef" + "0" * 56,
        "harness_version": "phase1",
    }


@pytest.fixture
def fake_row():
    return {
        "name": "hello-file",
        "goal": "Create a file called hello.txt",
        "workspace": "/tmp/x",
        "checks": [
            {"type": "file_exists", "passed": True, "detail": "found"},
        ],
        "exit_code": 0,
        "elapsed": 1.5,
        "passed": True,
    }


class TestAppendResultRow:
    def test_creates_file_on_first_append(
        self, jsonl_path, fake_metadata, fake_row
    ):
        assert not os.path.exists(jsonl_path)
        ev.append_result_row(
            fake_row, run_metadata=fake_metadata, run_index=0,
            output_path=jsonl_path,
        )
        assert os.path.exists(jsonl_path)

    def test_single_line_round_trips_through_json_loads(
        self, jsonl_path, fake_metadata, fake_row
    ):
        ev.append_result_row(
            fake_row, run_metadata=fake_metadata, run_index=0,
            output_path=jsonl_path,
        )
        with open(jsonl_path) as f:
            lines = f.readlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        # Row fields preserved
        assert parsed["name"] == "hello-file"
        assert parsed["passed"] is True
        assert parsed["exit_code"] == 0
        assert parsed["checks"][0]["type"] == "file_exists"
        # Metadata stamped
        assert parsed["git_sha"] == fake_metadata["git_sha"]
        assert parsed["harness_version"] == "phase1"
        # run_index injected
        assert parsed["run_index"] == 0

    def test_appends_multiple_rows(
        self, jsonl_path, fake_metadata, fake_row
    ):
        for i in range(3):
            ev.append_result_row(
                fake_row, run_metadata=fake_metadata, run_index=i,
                output_path=jsonl_path,
            )
        with open(jsonl_path) as f:
            lines = f.readlines()
        assert len(lines) == 3
        rows = [json.loads(line) for line in lines]
        assert [r["run_index"] for r in rows] == [0, 1, 2]

    def test_existing_lines_preserved_on_subsequent_append(
        self, jsonl_path, fake_metadata, fake_row
    ):
        # First batch
        for i in range(2):
            ev.append_result_row(
                fake_row, run_metadata=fake_metadata, run_index=i,
                output_path=jsonl_path,
            )
        # Second batch
        for i in range(2, 5):
            ev.append_result_row(
                fake_row, run_metadata=fake_metadata, run_index=i,
                output_path=jsonl_path,
            )
        with open(jsonl_path) as f:
            lines = f.readlines()
        assert len(lines) == 5
        run_indices = [json.loads(line)["run_index"] for line in lines]
        assert run_indices == [0, 1, 2, 3, 4]

    def test_metadata_stamps_every_row(
        self, jsonl_path, fake_metadata, fake_row
    ):
        for i in range(4):
            ev.append_result_row(
                fake_row, run_metadata=fake_metadata, run_index=i,
                output_path=jsonl_path,
            )
        with open(jsonl_path) as f:
            for line in f:
                row = json.loads(line)
                for k in fake_metadata:
                    assert row[k] == fake_metadata[k]

    def test_default_str_handles_non_serialisable(
        self, jsonl_path, fake_metadata
    ):
        import datetime
        row = {
            "name": "x", "goal": "y", "workspace": "/tmp",
            "checks": [], "exit_code": 0, "elapsed": 1.0,
            "passed": True,
            "extra_obj": datetime.datetime(2025, 1, 1),
        }
        ev.append_result_row(
            row, run_metadata=fake_metadata, run_index=0,
            output_path=jsonl_path,
        )
        with open(jsonl_path) as f:
            parsed = json.loads(f.readline())
        # default=str converts datetime to its str() representation
        assert "2025-01-01" in parsed["extra_obj"]

    def test_lines_newline_terminated(
        self, jsonl_path, fake_metadata, fake_row
    ):
        ev.append_result_row(
            fake_row, run_metadata=fake_metadata, run_index=0,
            output_path=jsonl_path,
        )
        with open(jsonl_path) as f:
            content = f.read()
        assert content.endswith("\n")

    def test_no_internal_newlines_per_record(
        self, jsonl_path, fake_metadata, fake_row
    ):
        # JSONL spec: one JSON object per line, no embedded newlines.
        ev.append_result_row(
            fake_row, run_metadata=fake_metadata, run_index=0,
            output_path=jsonl_path,
        )
        with open(jsonl_path) as f:
            lines = f.readlines()
        for line in lines:
            stripped = line.rstrip("\n")
            assert "\n" not in stripped

    def test_creates_parent_directory_if_missing(
        self, tmp_path, fake_metadata, fake_row
    ):
        nested = str(tmp_path / "deep" / "subdir" / "results.jsonl")
        assert not os.path.exists(os.path.dirname(nested))
        ev.append_result_row(
            fake_row, run_metadata=fake_metadata, run_index=0,
            output_path=nested,
        )
        assert os.path.exists(nested)

    def test_default_output_path_is_results_jsonl(
        self, fake_metadata, fake_row, monkeypatch, tmp_path
    ):
        # Redirect the module-level default to a tmp file so we don't
        # touch the real one.
        sentinel = str(tmp_path / "default_target.jsonl")
        monkeypatch.setattr(ev, "RESULTS_JSONL", sentinel)
        ev.append_result_row(
            fake_row, run_metadata=fake_metadata, run_index=0,
        )
        assert os.path.exists(sentinel)
