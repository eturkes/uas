"""Tests for input validation and error standardization (Step 5)."""

import logging
import os

import pytest

from architect.main import validate_uas_result
from architect.planner import validate_depends_on


class TestValidateDependsOn:
    def test_valid_no_dependencies(self):
        steps = [
            {"title": "A", "description": "Do A", "depends_on": []},
            {"title": "B", "description": "Do B", "depends_on": []},
        ]
        validate_depends_on(steps)  # Should not raise

    def test_valid_linear_chain(self):
        steps = [
            {"title": "A", "description": "Do A", "depends_on": []},
            {"title": "B", "description": "Do B", "depends_on": [1]},
            {"title": "C", "description": "Do C", "depends_on": [2]},
        ]
        validate_depends_on(steps)  # Should not raise

    def test_valid_diamond_dependency(self):
        steps = [
            {"title": "A", "description": "Do A", "depends_on": []},
            {"title": "B", "description": "Do B", "depends_on": [1]},
            {"title": "C", "description": "Do C", "depends_on": [1]},
            {"title": "D", "description": "Do D", "depends_on": [2, 3]},
        ]
        validate_depends_on(steps)  # Should not raise

    def test_out_of_range_high(self):
        steps = [
            {"title": "A", "description": "Do A", "depends_on": [5]},
        ]
        with pytest.raises(ValueError, match="only steps 1-1 exist"):
            validate_depends_on(steps)

    def test_out_of_range_zero(self):
        steps = [
            {"title": "A", "description": "Do A", "depends_on": [0]},
        ]
        with pytest.raises(ValueError, match="only steps 1-1 exist"):
            validate_depends_on(steps)

    def test_out_of_range_negative(self):
        steps = [
            {"title": "A", "description": "Do A", "depends_on": [-1]},
        ]
        with pytest.raises(ValueError, match="only steps 1-1 exist"):
            validate_depends_on(steps)

    def test_self_dependency(self):
        steps = [
            {"title": "A", "description": "Do A", "depends_on": [1]},
        ]
        with pytest.raises(ValueError, match="depends on itself"):
            validate_depends_on(steps)

    def test_circular_two_steps(self):
        steps = [
            {"title": "A", "description": "Do A", "depends_on": [2]},
            {"title": "B", "description": "Do B", "depends_on": [1]},
        ]
        with pytest.raises(ValueError, match="Circular dependency"):
            validate_depends_on(steps)

    def test_circular_three_steps(self):
        steps = [
            {"title": "A", "description": "Do A", "depends_on": [3]},
            {"title": "B", "description": "Do B", "depends_on": [1]},
            {"title": "C", "description": "Do C", "depends_on": [2]},
        ]
        with pytest.raises(ValueError, match="Circular dependency"):
            validate_depends_on(steps)

    def test_non_list_depends_on(self):
        steps = [
            {"title": "A", "description": "Do A", "depends_on": "1"},
        ]
        with pytest.raises(ValueError, match="non-list depends_on"):
            validate_depends_on(steps)

    def test_non_integer_in_depends_on(self):
        steps = [
            {"title": "A", "description": "Do A", "depends_on": ["1"]},
        ]
        with pytest.raises(ValueError, match="non-integer"):
            validate_depends_on(steps)

    def test_missing_depends_on_defaults_empty(self):
        steps = [
            {"title": "A", "description": "Do A"},
        ]
        validate_depends_on(steps)  # Should not raise

    def test_empty_steps_list(self):
        validate_depends_on([])  # Should not raise

    def test_single_step_no_deps(self):
        steps = [{"title": "A", "description": "Do A", "depends_on": []}]
        validate_depends_on(steps)  # Should not raise


class TestSubdirectorySearch:
    """Files in subdirectories are found without warnings (project subdir convention)."""

    def test_file_found_in_subdirectory(self, tmp_path):
        subdir = tmp_path / "myproject"
        subdir.mkdir()
        (subdir / "main.py").write_text("print('hello')")

        step = {
            "uas_result": {
                "status": "ok",
                "files_written": ["main.py"],
            }
        }
        result = validate_uas_result(step, str(tmp_path))
        assert result is None

    def test_file_at_root(self, tmp_path):
        (tmp_path / "main.py").write_text("print('hello')")

        step = {
            "uas_result": {
                "status": "ok",
                "files_written": ["main.py"],
            }
        }
        result = validate_uas_result(step, str(tmp_path))
        assert result is None

    def test_file_missing(self, tmp_path):
        step = {
            "uas_result": {
                "status": "ok",
                "files_written": ["nonexistent.py"],
            }
        }
        result = validate_uas_result(step, str(tmp_path))
        assert result is not None
        assert "does not exist" in result


class TestMaxErrorLengthConfigurable:
    def test_max_error_length_configurable(self, monkeypatch):
        """MAX_ERROR_LENGTH should be configurable via env var."""
        monkeypatch.setenv("UAS_MAX_ERROR_LENGTH", "5000")
        # Need to reload the module to pick up the new env var
        import importlib
        import architect.main as main_mod
        importlib.reload(main_mod)
        assert main_mod.MAX_ERROR_LENGTH == 5000
        # Reset
        monkeypatch.delenv("UAS_MAX_ERROR_LENGTH")
        importlib.reload(main_mod)

    def test_max_error_length_default_unlimited(self):
        """MAX_ERROR_LENGTH defaults to 0 (unlimited)."""
        from architect.main import MAX_ERROR_LENGTH
        assert MAX_ERROR_LENGTH == 0
