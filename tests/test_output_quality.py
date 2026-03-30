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

    def test_directory_skipped(self, tmp_path):
        d = tmp_path / "notebooks"
        d.mkdir()
        step = {"files_written": ["notebooks"]}
        issues = check_output_quality(step, str(tmp_path))
        assert issues == []

    def test_empty_init_py_allowed(self, tmp_path):
        f = tmp_path / "__init__.py"
        f.write_text("", encoding="utf-8")
        step = {"files_written": ["__init__.py"]}
        issues = check_output_quality(step, str(tmp_path))
        assert issues == []

    def test_empty_gitkeep_allowed(self, tmp_path):
        f = tmp_path / ".gitkeep"
        f.write_text("", encoding="utf-8")
        step = {"files_written": [".gitkeep"]}
        issues = check_output_quality(step, str(tmp_path))
        assert issues == []

    def test_empty_regular_file_still_flagged(self, tmp_path):
        f = tmp_path / "data.txt"
        f.write_text("", encoding="utf-8")
        step = {"files_written": ["data.txt"]}
        issues = check_output_quality(step, str(tmp_path))
        assert len(issues) == 1
        assert "empty" in issues[0].lower()


class TestDataLeakageDetection:
    """Data leakage detection in check_output_quality."""

    def _make_model_and_metrics(self, tmp_path, metrics_data):
        """Helper to create a model file and metrics JSON."""
        (tmp_path / "model.joblib").write_bytes(b"\x00")
        (tmp_path / "metrics.json").write_text(
            json.dumps(metrics_data), encoding="utf-8",
        )
        return {"files_written": ["model.joblib", "metrics.json"]}

    def test_same_timepoint_features_flagged(self, tmp_path):
        step = self._make_model_and_metrics(tmp_path, {
            "target": "Discharge_Score",
            "feature_names": ["Discharge_Grade", "Age"],
        })
        issues = check_output_quality(step, str(tmp_path))
        assert any("leakage" in i.lower() for i in issues)

    def test_baseline_suffix_not_flagged(self, tmp_path):
        """Metric_Admission predicting Metric_Outcome is valid."""
        step = self._make_model_and_metrics(tmp_path, {
            "target": "Metric_Outcome",
            "feature_names": ["Metric_Admission", "Age", "Category"],
        })
        issues = check_output_quality(step, str(tmp_path))
        assert not any("leakage" in i.lower() for i in issues)

    def test_baseline_suffix_case_insensitive(self, tmp_path):
        step = self._make_model_and_metrics(tmp_path, {
            "target": "Motor_Outcome",
            "feature_names": ["Motor_BASELINE", "Motor_Initial"],
        })
        issues = check_output_quality(step, str(tmp_path))
        assert not any("leakage" in i.lower() for i in issues)

    def test_same_side_temporal_still_flagged(self, tmp_path):
        """Both 'latest' and 'outcome' are late-time tokens → same side."""
        step = self._make_model_and_metrics(tmp_path, {
            "target": "Metric_Outcome",
            "feature_names": ["Metric_Latest", "Age"],
        })
        issues = check_output_quality(step, str(tmp_path))
        assert any("leakage" in i.lower() for i in issues)

    def test_non_temporal_suffix_still_flagged(self, tmp_path):
        """'Grade' is not a temporal token → no exemption."""
        step = self._make_model_and_metrics(tmp_path, {
            "target": "Discharge_Score",
            "feature_names": ["Discharge_Grade", "Age"],
        })
        issues = check_output_quality(step, str(tmp_path))
        assert any("leakage" in i.lower() for i in issues)

    def test_early_predicting_late_not_flagged(self, tmp_path):
        """General early→late pattern across domains."""
        step = self._make_model_and_metrics(tmp_path, {
            "target": "Score_Final",
            "feature_names": ["Score_Baseline", "Score_Initial", "Score_Pre"],
        })
        issues = check_output_quality(step, str(tmp_path))
        assert not any("leakage" in i.lower() for i in issues)

    def test_late_predicting_early_not_flagged(self, tmp_path):
        """Reverse direction also recognised as different timepoints."""
        step = self._make_model_and_metrics(tmp_path, {
            "target": "Metric_Baseline",
            "feature_names": ["Metric_Outcome"],
        })
        issues = check_output_quality(step, str(tmp_path))
        assert not any("leakage" in i.lower() for i in issues)

    def test_no_model_file_no_check(self, tmp_path):
        (tmp_path / "metrics.json").write_text(
            json.dumps({"target": "X_Y", "feature_names": ["X_Z"]}),
            encoding="utf-8",
        )
        step = {"files_written": ["metrics.json"]}
        issues = check_output_quality(step, str(tmp_path))
        assert not any("leakage" in i.lower() for i in issues)
