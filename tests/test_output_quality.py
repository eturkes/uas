"""Tests for architect.main.check_output_quality."""

import json
import os

from architect.main import check_output_quality


class TestCheckOutputQuality:
    def test_valid_json_no_issues(self, tmp_path):
        f = tmp_path / "data.json"
        f.write_text(json.dumps({"key": "value"}), encoding="utf-8")
        step = {"files_written": ["data.json"]}
        issues = check_output_quality(step, str(tmp_path))
        assert issues == []

    def test_invalid_json_detected(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("{not valid json", encoding="utf-8")
        step = {"files_written": ["bad.json"]}
        issues = check_output_quality(step, str(tmp_path))
        assert len(issues) == 1
        assert "invalid JSON" in issues[0]

    def test_valid_csv_no_issues(self, tmp_path):
        f = tmp_path / "data.csv"
        f.write_text("name,age\nalice,30\n", encoding="utf-8")
        step = {"files_written": ["data.csv"]}
        issues = check_output_quality(step, str(tmp_path))
        assert issues == []

    def test_csv_empty_header(self, tmp_path):
        f = tmp_path / "empty.csv"
        f.write_text("\n", encoding="utf-8")
        step = {"files_written": ["empty.csv"]}
        issues = check_output_quality(step, str(tmp_path))
        assert any("no header" in i for i in issues)

    def test_valid_python_no_issues(self, tmp_path):
        f = tmp_path / "script.py"
        f.write_text("x = 1\nprint(x)\n", encoding="utf-8")
        step = {"files_written": ["script.py"]}
        issues = check_output_quality(step, str(tmp_path))
        assert issues == []

    def test_python_syntax_error_detected(self, tmp_path):
        f = tmp_path / "broken.py"
        f.write_text("def foo(\n", encoding="utf-8")
        step = {"files_written": ["broken.py"]}
        issues = check_output_quality(step, str(tmp_path))
        assert len(issues) == 1
        assert "syntax error" in issues[0].lower()

    def test_empty_file_detected(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")
        step = {"files_written": ["empty.txt"]}
        issues = check_output_quality(step, str(tmp_path))
        assert len(issues) == 1
        assert "empty" in issues[0].lower()

    def test_missing_file_skipped(self, tmp_path):
        step = {"files_written": ["nonexistent.txt"]}
        issues = check_output_quality(step, str(tmp_path))
        # Missing files are skipped (caught by validate_uas_result)
        assert issues == []

    def test_no_files_written(self, tmp_path):
        step = {"files_written": []}
        issues = check_output_quality(step, str(tmp_path))
        assert issues == []

    def test_step_without_files_written_key(self, tmp_path):
        step = {}
        issues = check_output_quality(step, str(tmp_path))
        assert issues == []

    def test_multiple_files_mixed_validity(self, tmp_path):
        good = tmp_path / "good.json"
        good.write_text('{"ok": true}', encoding="utf-8")
        bad = tmp_path / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        step = {"files_written": ["good.json", "bad.json"]}
        issues = check_output_quality(step, str(tmp_path))
        assert len(issues) == 1
        assert "bad.json" in issues[0]

    def test_absolute_path_supported(self, tmp_path):
        f = tmp_path / "abs.json"
        f.write_text('{"a": 1}', encoding="utf-8")
        step = {"files_written": [str(f)]}
        issues = check_output_quality(step, str(tmp_path))
        assert issues == []
