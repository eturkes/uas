"""Post-run quality checks for a completed UAS project.

Validates that a completed run produces usable, correct output.
Point PROJECT_WORKSPACE at a workspace directory after a full UAS run,
or use as a template for project-specific quality gates.

Usage:
    python -m pytest integration/test_project_quality.py -x --tb=short
    PROJECT_WORKSPACE=/path/to/workspace python -m pytest integration/test_project_quality.py
"""

import csv
import glob
import json
import os
import re
import subprocess
import sys

import pytest

# ---------------------------------------------------------------------------
# Workspace discovery
# ---------------------------------------------------------------------------

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPT_DIR)

WORKSPACE = os.environ.get("PROJECT_WORKSPACE", "")


@pytest.fixture(autouse=True)
def _require_workspace():
    """Skip the entire module if the workspace does not exist."""
    if not WORKSPACE or not os.path.isdir(WORKSPACE):
        pytest.skip(
            f"Project workspace not found at {WORKSPACE!r}. "
            "Set PROJECT_WORKSPACE to the workspace directory."
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_file(*candidates):
    """Return the first path that exists under WORKSPACE, or None."""
    for name in candidates:
        matches = glob.glob(os.path.join(WORKSPACE, "**", name), recursive=True)
        if matches:
            return matches[0]
    return None


def _load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _csv_nan_fractions(path):
    """Return a dict mapping column name -> fraction of NaN/empty values."""
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    if not rows:
        return {}
    fracs = {}
    for col in rows[0]:
        empty = sum(
            1 for r in rows
            if r[col] is None or r[col].strip() == "" or r[col].strip().lower() == "nan"
        )
        fracs[col] = empty / len(rows)
    return fracs


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestModelQuality:
    """Model must outperform a trivial baseline."""

    def test_metrics_file_exists(self):
        path = _find_file("model_metrics.json")
        assert path is not None, "model_metrics.json not found in workspace"

    def test_accuracy_above_baseline(self):
        path = _find_file("model_metrics.json")
        if path is None:
            pytest.skip("model_metrics.json not found")
        data = _load_json(path)
        accuracy = data.get("accuracy")
        baseline = data.get("baseline_accuracy")
        if accuracy is None or baseline is None:
            pytest.skip(
                "model_metrics.json missing 'accuracy' or 'baseline_accuracy'"
            )
        assert accuracy >= baseline, (
            f"Model accuracy ({accuracy:.3f}) is worse than baseline ({baseline:.3f})"
        )


class TestNoDataLeakage:
    """No future-time features should predict future-time outcomes."""

    def test_no_target_prefix_in_features(self):
        path = _find_file("model_metrics.json")
        if path is None:
            pytest.skip("model_metrics.json not found")
        data = _load_json(path)
        feature_names = data.get("feature_names", [])
        if not feature_names:
            pytest.skip("model_metrics.json has no 'feature_names' field")

        target = data.get("target", "")
        if not target or "_" not in target:
            pytest.skip("target variable not set or has no prefix")

        target_prefix = target.split("_")[0].lower()
        if len(target_prefix) < 3:
            pytest.skip("target prefix too short for leakage check")

        leaky = [
            feat for feat in feature_names
            if feat.lower().startswith(target_prefix + "_")
            and feat.lower() != target.lower()
        ]
        assert not leaky, (
            f"Potential data leakage: features {leaky} share prefix "
            f"'{target_prefix}' with target '{target}'"
        )


class TestFeatureDataQuality:
    """Features used for modeling must have reasonable completeness."""

    def test_feature_nan_rate(self):
        # Try common feature file names
        path = _find_file("features.csv", "admission_features.csv",
                          "training_features.csv")
        if path is None:
            pytest.skip("Feature CSV not found")

        metrics_path = _find_file("model_metrics.json")
        if metrics_path is not None:
            metrics = _load_json(metrics_path)
            model_features = set(metrics.get("feature_names", []))
        else:
            model_features = None

        fracs = _csv_nan_fractions(path)
        bad_cols = {}
        for col, frac in fracs.items():
            if model_features is not None and col not in model_features:
                continue
            if frac >= 0.50:
                bad_cols[col] = frac

        assert not bad_cols, (
            f"Model feature columns with >=50% NaN: "
            + ", ".join(f"{c} ({v:.0%})" for c, v in bad_cols.items())
        )


class TestSubgroupAnalysis:
    """Subgroup/segmentation results must contain statistical tests."""

    def test_results_have_stats(self):
        path = _find_file("subgroup_results.json", "segment_results.json",
                          "cluster_results.json")
        if path is None:
            pytest.skip("Subgroup/segment results JSON not found")
        data = _load_json(path)

        content = json.dumps(data).lower()
        has_stats = any(
            kw in content
            for kw in ["p_value", "p-value", "pvalue", "statistic", "u_statistic",
                        "confidence_interval", "ci_lower", "ci_upper", "effect_size",
                        "mann_whitney", "mann-whitney", "bootstrap"]
        )
        assert has_stats, (
            f"{os.path.basename(path)} appears to be a stub -- no statistical "
            "test results found (expected p-values, test statistics, or CIs)"
        )


class TestNoHardcodedPaths:
    """No .py file should contain hardcoded /workspace paths."""

    def test_no_hardcoded_workspace_paths(self):
        py_files = glob.glob(os.path.join(WORKSPACE, "**", "*.py"), recursive=True)
        if not py_files:
            pytest.skip("No .py files found in workspace")

        offenders = []
        for py_file in py_files:
            with open(py_file, "r", encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, 1):
                    if re.search(r'["\']\/workspace(?:\/|["\'])', line):
                        stripped = line.lstrip()
                        if stripped.startswith("#"):
                            continue
                        relpath = os.path.relpath(py_file, WORKSPACE)
                        offenders.append(f"{relpath}:{lineno}")
        assert not offenders, (
            "Hardcoded /workspace paths found (will break outside container):\n"
            + "\n".join(f"  {o}" for o in offenders)
        )


class TestDashboardImport:
    """Dashboard app module should be importable."""

    def test_dashboard_app_imports(self):
        app_file = _find_file("app.py")
        if app_file is None:
            pytest.skip("dashboard app.py not found in workspace")

        app_dir = os.path.dirname(app_file)

        result = subprocess.run(
            [sys.executable, "-c", "from dashboard.app import app"],
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": WORKSPACE},
            timeout=30,
        )
        if result.returncode != 0:
            result2 = subprocess.run(
                [sys.executable, "-c", "import app"],
                cwd=app_dir,
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONPATH": app_dir},
                timeout=30,
            )
            if result2.returncode != 0:
                pytest.fail(
                    f"Dashboard import failed.\n"
                    f"  from dashboard.app: {result.stderr.strip()[-300:]}\n"
                    f"  import app: {result2.stderr.strip()[-300:]}"
                )
