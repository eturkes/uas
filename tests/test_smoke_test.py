"""Tests for smoke_test_entry_point() and _find_entry_points()."""

import os
import textwrap

import pytest

from architect.main import smoke_test_entry_point, _find_entry_points


class TestFindEntryPoints:
    """Test entry-point detection heuristics."""

    def test_detects_main_guard(self, tmp_path):
        (tmp_path / "app.py").write_text(
            textwrap.dedent("""\
                import sys
                def main():
                    print("hello")
                if __name__ == "__main__":
                    main()
            """),
            encoding="utf-8",
        )
        eps = _find_entry_points(str(tmp_path))
        assert "app.py" in eps

    def test_detects_well_known_names(self, tmp_path):
        (tmp_path / "server.py").write_text("# no main guard\n", encoding="utf-8")
        eps = _find_entry_points(str(tmp_path))
        assert "server.py" in eps

    def test_detects_from_run_sh(self, tmp_path):
        (tmp_path / "myapp.py").write_text("print('hi')\n", encoding="utf-8")
        (tmp_path / "run.sh").write_text(
            "#!/bin/bash\npython myapp.py\n", encoding="utf-8"
        )
        eps = _find_entry_points(str(tmp_path))
        assert "myapp.py" in eps

    def test_run_sh_takes_priority(self, tmp_path):
        """File from launcher script appears before well-known names."""
        (tmp_path / "custom.py").write_text("print('hi')\n", encoding="utf-8")
        (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
        (tmp_path / "run.sh").write_text(
            "#!/bin/bash\npython custom.py\n", encoding="utf-8"
        )
        eps = _find_entry_points(str(tmp_path))
        assert eps[0] == "custom.py"

    def test_empty_workspace(self, tmp_path):
        eps = _find_entry_points(str(tmp_path))
        assert eps == []

    def test_skips_state_and_venv(self, tmp_path):
        state_dir = tmp_path / ".state"
        state_dir.mkdir()
        (state_dir / "app.py").write_text(
            'if __name__ == "__main__": pass\n', encoding="utf-8"
        )
        venv = tmp_path / "venv"
        venv.mkdir()
        (venv / "main.py").write_text("print('hi')\n", encoding="utf-8")
        eps = _find_entry_points(str(tmp_path))
        assert eps == []

    def test_subdirectory_entry_point(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "app.py").write_text(
            textwrap.dedent("""\
                if __name__ == "__main__":
                    pass
            """),
            encoding="utf-8",
        )
        eps = _find_entry_points(str(tmp_path))
        assert any("app.py" in ep for ep in eps)


class TestSmokeTestEntryPoint:
    """Test the dry-import smoke test."""

    def test_valid_app_passes(self, tmp_path):
        """A self-contained app.py that imports from a local module passes."""
        (tmp_path / "utils.py").write_text(
            textwrap.dedent("""\
                GREETING = "hello"
                def greet():
                    return GREETING
            """),
            encoding="utf-8",
        )
        (tmp_path / "app.py").write_text(
            textwrap.dedent("""\
                from utils import greet, GREETING
                if __name__ == "__main__":
                    print(greet())
            """),
            encoding="utf-8",
        )
        state = {"steps": []}
        result = smoke_test_entry_point(str(tmp_path), state)
        assert result is None

    def test_broken_import_chain_detected(self, tmp_path):
        """An app with a broken transitive import chain returns an error."""
        (tmp_path / "helpers.py").write_text(
            textwrap.dedent("""\
                def real_func():
                    return 42
            """),
            encoding="utf-8",
        )
        # tab_data.py imports a name that doesn't exist in helpers
        (tmp_path / "tab_data.py").write_text(
            textwrap.dedent("""\
                from helpers import nonexistent_func
                def run():
                    return nonexistent_func()
            """),
            encoding="utf-8",
        )
        # app.py imports tab_data — the transitive chain is broken
        (tmp_path / "app.py").write_text(
            textwrap.dedent("""\
                from tab_data import run
                if __name__ == "__main__":
                    run()
            """),
            encoding="utf-8",
        )
        state = {"steps": []}
        result = smoke_test_entry_point(str(tmp_path), state)
        assert result is not None
        assert "ImportError" in result or "cannot import name" in result

    def test_entry_point_from_run_sh(self, tmp_path):
        """Smoke test picks up entry point referenced in run.sh."""
        (tmp_path / "dashboard.py").write_text(
            textwrap.dedent("""\
                print("dashboard loaded")
            """),
            encoding="utf-8",
        )
        (tmp_path / "run.sh").write_text(
            "#!/bin/bash\npython dashboard.py\n", encoding="utf-8"
        )
        state = {"steps": []}
        result = smoke_test_entry_point(str(tmp_path), state)
        assert result is None

    def test_no_entry_points_returns_none(self, tmp_path):
        """Workspace with no recognizable entry point skips gracefully."""
        (tmp_path / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        state = {"steps": []}
        result = smoke_test_entry_point(str(tmp_path), state)
        assert result is None

    def test_syntax_error_detected(self, tmp_path):
        """A file with a syntax error is caught by the smoke test."""
        (tmp_path / "app.py").write_text(
            "def broken(\n", encoding="utf-8"
        )
        state = {"steps": []}
        result = smoke_test_entry_point(str(tmp_path), state)
        assert result is not None
        assert "SyntaxError" in result

    def test_module_in_subdirectory(self, tmp_path):
        """Entry point in a subdirectory is tested correctly."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "__init__.py").write_text("", encoding="utf-8")
        (src / "app.py").write_text(
            textwrap.dedent("""\
                VALUE = 42
                if __name__ == "__main__":
                    print(VALUE)
            """),
            encoding="utf-8",
        )
        state = {"steps": []}
        result = smoke_test_entry_point(str(tmp_path), state)
        assert result is None


class TestSmokeTestInValidateWorkspace:
    """Test that validate_workspace() includes the launch test."""

    def test_launch_test_error_in_validation_data(self, tmp_path):
        """validate_workspace returns launch_test_error field."""
        from unittest.mock import patch
        from architect.main import validate_workspace

        # Valid workspace — no launch error expected
        (tmp_path / "output.txt").write_text("data", encoding="utf-8")
        state = {
            "goal": "test goal",
            "steps": [
                {"title": "Step 1", "status": "completed", "files_written": []},
            ],
        }
        with patch("architect.main.MINIMAL_MODE", True):
            result = validate_workspace(state, str(tmp_path))

        assert "launch_test_error" in result
        # No Python entry points, so should be None
        assert result["launch_test_error"] is None

    def test_broken_app_shows_in_report(self, tmp_path):
        """validate_workspace report includes Launch Test section on failure."""
        from unittest.mock import patch
        from architect.main import validate_workspace

        (tmp_path / "helpers.py").write_text(
            "def real_func(): pass\n", encoding="utf-8"
        )
        (tmp_path / "app.py").write_text(
            "from helpers import fake_func\n", encoding="utf-8"
        )
        state = {
            "goal": "test goal",
            "steps": [
                {
                    "id": 1,
                    "title": "Build app",
                    "status": "completed",
                    "files_written": ["app.py", "helpers.py"],
                },
            ],
        }
        with patch("architect.main.MINIMAL_MODE", True):
            result = validate_workspace(state, str(tmp_path))

        assert result["launch_test_error"] is not None
        report = (tmp_path / ".state" / "validation.md").read_text()
        assert "## Launch Test" in report
        assert "import failed" in report.lower() or "ImportError" in report
