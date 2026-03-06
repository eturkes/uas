"""Tests for architect.code_tracker module."""

import json
import os

from architect.code_tracker import (
    CodeVersion,
    CodeTracker,
    get_code_tracker,
    reset_code_tracker,
)


class TestCodeVersion:
    def test_create(self):
        v = CodeVersion(
            step_id=1, spec_attempt=0, orch_attempt=0,
            code="print('hello')", prompt_hash="abc123",
            exit_code=0, error_summary="", timestamp="2024-01-01T00:00:00Z",
        )
        assert v.step_id == 1
        assert v.code == "print('hello')"
        assert v.exit_code == 0

    def test_to_dict(self):
        v = CodeVersion(
            step_id=1, spec_attempt=0, orch_attempt=0,
            code="x=1", prompt_hash="abc", exit_code=0,
            error_summary="", timestamp="2024-01-01T00:00:00Z",
        )
        d = v.to_dict()
        assert d["step_id"] == 1
        assert d["code"] == "x=1"
        assert d["exit_code"] == 0
        assert "timestamp" in d

    def test_to_dict_roundtrip(self):
        v = CodeVersion(
            step_id=2, spec_attempt=1, orch_attempt=2,
            code="y=2", prompt_hash="def456", exit_code=1,
            error_summary="some error", timestamp="2024-06-01T12:00:00Z",
        )
        d = v.to_dict()
        v2 = CodeVersion(**d)
        assert v2 == v


class TestCodeTracker:
    def test_record_and_get_versions(self):
        tracker = CodeTracker()
        tracker.record(1, 0, 0, "v1", exit_code=1, error_summary="err")
        tracker.record(1, 0, 1, "v2", exit_code=0)
        versions = tracker.get_versions(1)
        assert len(versions) == 2
        assert versions[0].code == "v1"
        assert versions[0].exit_code == 1
        assert versions[1].code == "v2"
        assert versions[1].exit_code == 0

    def test_get_versions_empty(self):
        tracker = CodeTracker()
        assert tracker.get_versions(99) == []

    def test_record_multiple_steps(self):
        tracker = CodeTracker()
        tracker.record(1, 0, 0, "step1_code")
        tracker.record(2, 0, 0, "step2_code")
        assert len(tracker.get_versions(1)) == 1
        assert len(tracker.get_versions(2)) == 1
        assert tracker.get_versions(1)[0].code == "step1_code"
        assert tracker.get_versions(2)[0].code == "step2_code"

    def test_get_diff(self):
        tracker = CodeTracker()
        tracker.record(1, 0, 0, "line1\nline2\n")
        tracker.record(1, 0, 1, "line1\nline3\n")
        diff = tracker.get_diff(1, 0, 1)
        assert "-line2" in diff
        assert "+line3" in diff
        assert "attempt-0" in diff
        assert "attempt-1" in diff

    def test_get_diff_identical_code(self):
        tracker = CodeTracker()
        tracker.record(1, 0, 0, "same\n")
        tracker.record(1, 0, 1, "same\n")
        diff = tracker.get_diff(1, 0, 1)
        assert diff == ""

    def test_get_diff_empty_step(self):
        tracker = CodeTracker()
        assert tracker.get_diff(1, 0, 1) == ""

    def test_get_diff_out_of_range(self):
        tracker = CodeTracker()
        tracker.record(1, 0, 0, "x=1")
        assert tracker.get_diff(1, 0, 5) == ""
        assert tracker.get_diff(1, -1, 0) == ""

    def test_serialization_to_disk(self, tmp_path):
        output_dir = str(tmp_path / "code_versions")
        tracker = CodeTracker(output_dir=output_dir)
        tracker.record(1, 0, 0, "v1", exit_code=1, error_summary="err")
        tracker.record(1, 0, 1, "v2", exit_code=0)
        tracker.record(2, 0, 0, "other", exit_code=0)

        assert os.path.exists(os.path.join(output_dir, "1.json"))
        assert os.path.exists(os.path.join(output_dir, "2.json"))

        with open(os.path.join(output_dir, "1.json")) as f:
            data = json.load(f)
        assert len(data) == 2
        assert data[0]["code"] == "v1"
        assert data[1]["code"] == "v2"

    def test_load_step(self, tmp_path):
        versions_file = str(tmp_path / "1.json")
        data = [
            {
                "step_id": 1, "spec_attempt": 0, "orch_attempt": 0,
                "code": "v1", "prompt_hash": "", "exit_code": 1,
                "error_summary": "err", "timestamp": "2024-01-01T00:00:00Z",
            },
            {
                "step_id": 1, "spec_attempt": 0, "orch_attempt": 1,
                "code": "v2", "prompt_hash": "", "exit_code": 0,
                "error_summary": "", "timestamp": "2024-01-01T00:00:01Z",
            },
        ]
        with open(versions_file, "w") as f:
            json.dump(data, f)

        tracker = CodeTracker()
        tracker.load_step(1, versions_file)
        versions = tracker.get_versions(1)
        assert len(versions) == 2
        assert versions[0].code == "v1"
        assert versions[1].exit_code == 0

    def test_load_step_nonexistent(self):
        tracker = CodeTracker()
        tracker.load_step(1, "/nonexistent/file.json")
        assert tracker.get_versions(1) == []

    def test_load_step_overwrites(self, tmp_path):
        tracker = CodeTracker()
        tracker.record(1, 0, 0, "original")

        versions_file = str(tmp_path / "1.json")
        data = [{
            "step_id": 1, "spec_attempt": 0, "orch_attempt": 0,
            "code": "loaded", "prompt_hash": "", "exit_code": 0,
            "error_summary": "", "timestamp": "2024-01-01T00:00:00Z",
        }]
        with open(versions_file, "w") as f:
            json.dump(data, f)

        tracker.load_step(1, versions_file)
        versions = tracker.get_versions(1)
        assert len(versions) == 1
        assert versions[0].code == "loaded"

    def test_load_from_dir(self, tmp_path):
        cv_dir = str(tmp_path / "code_versions")
        os.makedirs(cv_dir)
        for step_id, code in [(1, "s1code"), (2, "s2code")]:
            data = [{
                "step_id": step_id, "spec_attempt": 0, "orch_attempt": 0,
                "code": code, "prompt_hash": "", "exit_code": 0,
                "error_summary": "", "timestamp": "2024-01-01T00:00:00Z",
            }]
            with open(os.path.join(cv_dir, f"{step_id}.json"), "w") as f:
                json.dump(data, f)

        tracker = CodeTracker()
        tracker.load_from_dir(cv_dir)
        assert len(tracker.get_versions(1)) == 1
        assert len(tracker.get_versions(2)) == 1
        assert tracker.get_versions(1)[0].code == "s1code"

    def test_load_from_dir_nonexistent(self):
        tracker = CodeTracker()
        tracker.load_from_dir("/nonexistent/path")
        assert tracker.get_all_versions() == {}

    def test_load_from_dir_skips_non_json(self, tmp_path):
        cv_dir = str(tmp_path / "code_versions")
        os.makedirs(cv_dir)
        with open(os.path.join(cv_dir, "readme.txt"), "w") as f:
            f.write("not json")
        with open(os.path.join(cv_dir, "notanumber.json"), "w") as f:
            json.dump([], f)

        tracker = CodeTracker()
        tracker.load_from_dir(cv_dir)
        assert tracker.get_all_versions() == {}

    def test_get_all_versions(self):
        tracker = CodeTracker()
        tracker.record(1, 0, 0, "a")
        tracker.record(2, 0, 0, "b")
        all_v = tracker.get_all_versions()
        assert 1 in all_v
        assert 2 in all_v
        assert len(all_v[1]) == 1
        assert len(all_v[2]) == 1

    def test_error_summary_truncated(self):
        tracker = CodeTracker()
        long_error = "x" * 500
        tracker.record(1, 0, 0, "code", error_summary=long_error)
        versions = tracker.get_versions(1)
        assert len(versions[0].error_summary) == 200

    def test_prompt_hash_stored(self):
        tracker = CodeTracker()
        tracker.record(1, 0, 0, "code", prompt_hash="abc123")
        versions = tracker.get_versions(1)
        assert versions[0].prompt_hash == "abc123"


class TestSingleton:
    def test_get_code_tracker_returns_same(self):
        reset_code_tracker()
        t1 = get_code_tracker()
        t2 = get_code_tracker()
        assert t1 is t2
        reset_code_tracker()

    def test_reset_creates_new(self):
        reset_code_tracker()
        t1 = get_code_tracker()
        reset_code_tracker()
        t2 = get_code_tracker()
        assert t1 is not t2
        reset_code_tracker()

    def test_get_with_output_dir(self, tmp_path):
        reset_code_tracker()
        output_dir = str(tmp_path / "cv")
        t = get_code_tracker(output_dir=output_dir)
        assert t.output_dir == output_dir
        assert os.path.isdir(output_dir)
        reset_code_tracker()
