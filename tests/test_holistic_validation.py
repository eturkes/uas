"""Tests for Section 13 — Holistic end-of-run workspace validation.

Verifies:
- _check_readme_accuracy() detects references to non-existent scripts/paths
- _check_import_resolution() detects broken local imports
- _check_orphaned_files() detects files no step claims as output
- _check_entry_points() detects broken pyproject.toml entry points
- holistic_validation() aggregates all checks
"""

import os
import textwrap

import pytest

from architect.main import (
    _check_readme_accuracy,
    _check_import_resolution,
    _check_orphaned_files,
    _check_entry_points,
    _check_entry_points_regex,
    holistic_validation,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(files_written_per_step=None):
    """Build a minimal state dict with optional files_written."""
    if files_written_per_step is None:
        files_written_per_step = {}
    steps = []
    for step_id, files in files_written_per_step.items():
        steps.append({
            "id": step_id,
            "title": f"Step {step_id}",
            "description": f"Description for step {step_id}",
            "status": "completed",
            "depends_on": [],
            "files_written": files,
        })
    return {"goal": "Build a dashboard", "steps": steps}


# ---------------------------------------------------------------------------
# _check_readme_accuracy
# ---------------------------------------------------------------------------

class TestCheckReadmeAccuracy:

    def test_missing_script_via_python_command(self, tmp_path):
        """README with `python scripts/run.py` flags missing file."""
        readme = tmp_path / "README.md"
        readme.write_text("Run with:\n```\npython scripts/run_dashboard.py\n```\n")
        issues = _check_readme_accuracy(str(tmp_path))
        assert len(issues) == 1
        assert "scripts/run_dashboard.py" in issues[0]
        assert "does not exist" in issues[0]

    def test_existing_script_no_issue(self, tmp_path):
        """README with python command referencing existing file is fine."""
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "run.py").write_text("print('hello')")
        readme = tmp_path / "README.md"
        readme.write_text("Run: `python scripts/run.py`\n")
        issues = _check_readme_accuracy(str(tmp_path))
        assert issues == []

    def test_missing_backtick_path(self, tmp_path):
        """README referencing `src/app/main.py` in backticks flags missing."""
        readme = tmp_path / "README.md"
        readme.write_text("Edit `src/app/main.py` to configure.\n")
        issues = _check_readme_accuracy(str(tmp_path))
        assert len(issues) == 1
        assert "src/app/main.py" in issues[0]

    def test_existing_backtick_path_no_issue(self, tmp_path):
        """README referencing an existing file in backticks is fine."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("pass")
        readme = tmp_path / "README.md"
        readme.write_text("See `src/main.py` for details.\n")
        issues = _check_readme_accuracy(str(tmp_path))
        assert issues == []

    def test_urls_ignored(self, tmp_path):
        """URLs in backticks are not treated as file paths."""
        readme = tmp_path / "README.md"
        readme.write_text("Visit `https://example.com/foo/bar.html`\n")
        issues = _check_readme_accuracy(str(tmp_path))
        assert issues == []

    def test_no_readme_no_issues(self, tmp_path):
        """No README file means no issues."""
        issues = _check_readme_accuracy(str(tmp_path))
        assert issues == []

    def test_no_duplicates(self, tmp_path):
        """Same missing path referenced twice only produces one issue."""
        readme = tmp_path / "README.md"
        readme.write_text(
            "Run `python scripts/run.py` or `python scripts/run.py --debug`\n"
        )
        issues = _check_readme_accuracy(str(tmp_path))
        assert len(issues) == 1


# ---------------------------------------------------------------------------
# _check_import_resolution
# ---------------------------------------------------------------------------

class TestCheckImportResolution:

    def test_broken_relative_import(self, tmp_path):
        """Relative import to non-existent module is flagged."""
        pkg = tmp_path / "myapp"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "main.py").write_text("from .missing_module import foo\n")
        issues = _check_import_resolution(str(tmp_path))
        assert len(issues) == 1
        assert "cannot be resolved" in issues[0]
        assert "missing_module" in issues[0]

    def test_valid_relative_import_no_issue(self, tmp_path):
        """Relative import to existing module is fine."""
        pkg = tmp_path / "myapp"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "utils.py").write_text("def helper(): pass\n")
        (pkg / "main.py").write_text("from .utils import helper\n")
        issues = _check_import_resolution(str(tmp_path))
        assert issues == []

    def test_broken_absolute_local_import(self, tmp_path):
        """Absolute import targeting local package but missing module."""
        pkg = tmp_path / "myapp"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "main.py").write_text("from myapp.data.loader import load\n")
        issues = _check_import_resolution(str(tmp_path))
        assert len(issues) == 1
        assert "not found" in issues[0]
        assert "myapp.data.loader" in issues[0]

    def test_valid_absolute_local_import_no_issue(self, tmp_path):
        """Absolute import targeting existing local module is fine."""
        pkg = tmp_path / "myapp"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        data = pkg / "data"
        data.mkdir()
        (data / "__init__.py").write_text("")
        (data / "loader.py").write_text("def load(): pass\n")
        (pkg / "main.py").write_text("from myapp.data.loader import load\n")
        issues = _check_import_resolution(str(tmp_path))
        assert issues == []

    def test_stdlib_import_ignored(self, tmp_path):
        """Imports of stdlib or third-party modules are not flagged."""
        (tmp_path / "script.py").write_text(
            "from os.path import join\nimport json\nfrom pathlib import Path\n"
        )
        issues = _check_import_resolution(str(tmp_path))
        assert issues == []

    def test_empty_workspace(self, tmp_path):
        """Empty workspace produces no issues."""
        issues = _check_import_resolution(str(tmp_path))
        assert issues == []


# ---------------------------------------------------------------------------
# _check_orphaned_files
# ---------------------------------------------------------------------------

class TestCheckOrphanedFiles:

    def test_orphaned_file_detected(self, tmp_path):
        """A file not claimed by any step is flagged."""
        (tmp_path / "step_output.py").write_text("pass")
        (tmp_path / "orphan.py").write_text("pass")
        state = _make_state({1: ["step_output.py"]})
        issues = _check_orphaned_files(str(tmp_path), state)
        assert len(issues) == 1
        assert "orphan.py" in issues[0]

    def test_claimed_file_not_flagged(self, tmp_path):
        """Files claimed by steps are not flagged."""
        (tmp_path / "module.py").write_text("pass")
        state = _make_state({1: ["module.py"]})
        issues = _check_orphaned_files(str(tmp_path), state)
        assert issues == []

    def test_expected_root_files_not_flagged(self, tmp_path):
        """Standard project files are never flagged as orphans."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        (tmp_path / "requirements.txt").write_text("flask\n")
        (tmp_path / ".gitignore").write_text("__pycache__/\n")
        state = _make_state({})
        issues = _check_orphaned_files(str(tmp_path), state)
        assert issues == []

    def test_readme_not_flagged(self, tmp_path):
        """README files are expected and not flagged."""
        (tmp_path / "README.md").write_text("# Project")
        state = _make_state({})
        issues = _check_orphaned_files(str(tmp_path), state)
        assert issues == []

    def test_empty_state_all_orphans(self, tmp_path):
        """If no step claims any files, all workspace files are orphans."""
        (tmp_path / "a.py").write_text("pass")
        (tmp_path / "b.py").write_text("pass")
        state = _make_state({})
        issues = _check_orphaned_files(str(tmp_path), state)
        assert len(issues) == 1
        assert "2 orphaned" in issues[0]

    def test_hidden_files_not_flagged(self, tmp_path):
        """Hidden files (starting with .) are ignored."""
        (tmp_path / ".hidden").write_text("secret")
        state = _make_state({})
        issues = _check_orphaned_files(str(tmp_path), state)
        assert issues == []


# ---------------------------------------------------------------------------
# _check_entry_points
# ---------------------------------------------------------------------------

class TestCheckEntryPoints:

    def test_missing_entry_point_module(self, tmp_path):
        """pyproject.toml entry targeting non-existent module is flagged."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent("""\
            [project]
            name = "myapp"

            [project.scripts]
            myapp = "myapp.cli:main"
        """))
        issues = _check_entry_points(str(tmp_path))
        assert len(issues) == 1
        assert "myapp.cli" in issues[0]
        assert "not found" in issues[0]

    def test_valid_entry_point_no_issue(self, tmp_path):
        """pyproject.toml entry targeting existing module is fine."""
        pkg = tmp_path / "myapp"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "cli.py").write_text("def main(): pass\n")
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent("""\
            [project]
            name = "myapp"

            [project.scripts]
            myapp = "myapp.cli:main"
        """))
        issues = _check_entry_points(str(tmp_path))
        assert issues == []

    def test_no_pyproject_no_issues(self, tmp_path):
        """No pyproject.toml means no issues."""
        issues = _check_entry_points(str(tmp_path))
        assert issues == []

    def test_pyproject_without_scripts_no_issues(self, tmp_path):
        """pyproject.toml without scripts section is fine."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent("""\
            [project]
            name = "myapp"
            version = "0.1.0"
        """))
        issues = _check_entry_points(str(tmp_path))
        assert issues == []


class TestCheckEntryPointsRegex:
    """Test the regex fallback for pyproject.toml parsing."""

    def test_missing_entry_point(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent("""\
            [project]
            name = "myapp"

            [project.scripts]
            myapp = "myapp.cli:main"
        """))
        issues = _check_entry_points_regex(str(tmp_path), str(pyproject))
        assert len(issues) == 1
        assert "myapp.cli" in issues[0]

    def test_valid_entry_point(self, tmp_path):
        pkg = tmp_path / "myapp"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "cli.py").write_text("def main(): pass\n")
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent("""\
            [project.scripts]
            myapp = "myapp.cli:main"
        """))
        issues = _check_entry_points_regex(str(tmp_path), str(pyproject))
        assert issues == []


# ---------------------------------------------------------------------------
# holistic_validation — integration
# ---------------------------------------------------------------------------

class TestHolisticValidation:

    def test_aggregates_all_checks(self, tmp_path):
        """holistic_validation returns issues from all sub-checks."""
        # README referencing missing script
        (tmp_path / "README.md").write_text(
            "Run: `python scripts/run.py`\n"
        )
        # Broken relative import
        pkg = tmp_path / "myapp"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "main.py").write_text("from .missing import foo\n")
        # Orphaned file
        (tmp_path / "stale_build.py").write_text("pass")
        # pyproject.toml with broken entry
        (tmp_path / "pyproject.toml").write_text(textwrap.dedent("""\
            [project]
            name = "myapp"

            [project.scripts]
            myapp = "myapp.cli:main"
        """))

        state = _make_state({
            1: [os.path.join("myapp", "__init__.py")],
            2: [os.path.join("myapp", "main.py")],
        })
        issues = holistic_validation(str(tmp_path), state)

        # Should have at least one issue from each checker
        readme_issues = [i for i in issues if "README" in i]
        import_issues = [i for i in issues if "import" in i.lower() or "resolved" in i.lower()]
        orphan_issues = [i for i in issues if "orphan" in i.lower()]
        entry_issues = [i for i in issues if "pyproject" in i.lower()]

        assert len(readme_issues) >= 1
        assert len(import_issues) >= 1
        assert len(orphan_issues) >= 1
        assert len(entry_issues) >= 1

    def test_clean_workspace_no_issues(self, tmp_path):
        """A well-formed workspace produces no issues."""
        pkg = tmp_path / "myapp"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "main.py").write_text("from .utils import helper\n")
        (pkg / "utils.py").write_text("def helper(): pass\n")
        (tmp_path / "README.md").write_text("# MyApp\n\nA simple app.\n")

        state = _make_state({
            1: [
                os.path.join("myapp", "__init__.py"),
                os.path.join("myapp", "main.py"),
                os.path.join("myapp", "utils.py"),
            ],
        })
        issues = holistic_validation(str(tmp_path), state)
        assert issues == []
