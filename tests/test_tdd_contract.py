"""Tests for TDD test-step contract enforcement (Tasks 4.3, 4.4)."""

import os

import pytest

from architect.planner import (
    _is_test_file,
    validate_test_step_contract,
    validate_tdd_coverage,
)
from architect.main import _collect_test_files_for_step


class TestIsTestFile:
    def test_test_prefix(self):
        assert _is_test_file("test_parser.py") is True

    def test_test_suffix(self):
        assert _is_test_file("parser_test.py") is True

    def test_nested_path_prefix(self):
        assert _is_test_file("tests/test_utils.py") is True

    def test_nested_path_suffix(self):
        assert _is_test_file("tests/utils_test.py") is True

    def test_non_test_file(self):
        assert _is_test_file("parser.py") is False

    def test_non_python_file(self):
        assert _is_test_file("test_parser.txt") is False

    def test_test_in_middle(self):
        assert _is_test_file("my_test_helper.py") is False

    def test_empty_string(self):
        assert _is_test_file("") is False

    def test_just_test_prefix(self):
        assert _is_test_file("test_.py") is True

    def test_conftest(self):
        assert _is_test_file("conftest.py") is False


class TestValidateTestStepContract:
    def test_valid_test_step(self):
        steps = [
            {
                "title": "test: Write tests for parser",
                "description": "Write pytest tests for the CSV parser module.",
                "depends_on": [],
                "outputs": ["test_csv_parser.py"],
            },
        ]
        assert validate_test_step_contract(steps) == []

    def test_valid_test_step_suffix_pattern(self):
        steps = [
            {
                "title": "test: Write tests for utils",
                "description": "Write pytest tests for the utility functions.",
                "depends_on": [],
                "outputs": ["utils_test.py"],
            },
        ]
        assert validate_test_step_contract(steps) == []

    def test_missing_test_file_in_outputs(self):
        steps = [
            {
                "title": "test: Write tests for parser",
                "description": "Write pytest tests for the parser.",
                "depends_on": [],
                "outputs": ["parser.py"],
            },
        ]
        violations = validate_test_step_contract(steps)
        assert len(violations) == 1
        assert "test_*.py or *_test.py" in violations[0]

    def test_empty_outputs(self):
        steps = [
            {
                "title": "test: Write tests for parser",
                "description": "Write pytest tests for the parser.",
                "depends_on": [],
                "outputs": [],
            },
        ]
        violations = validate_test_step_contract(steps)
        assert len(violations) == 1
        assert "test_*.py or *_test.py" in violations[0]

    def test_missing_outputs_key(self):
        steps = [
            {
                "title": "test: Write tests for parser",
                "description": "Write pytest tests for the parser.",
                "depends_on": [],
            },
        ]
        violations = validate_test_step_contract(steps)
        assert len(violations) == 1
        assert "test_*.py or *_test.py" in violations[0]

    def test_missing_description_keyword(self):
        steps = [
            {
                "title": "test: Write tests for parser",
                "description": "Create unit tests for the parser module.",
                "depends_on": [],
                "outputs": ["test_parser.py"],
            },
        ]
        violations = validate_test_step_contract(steps)
        assert len(violations) == 1
        assert "Write pytest tests for" in violations[0]

    def test_both_violations(self):
        steps = [
            {
                "title": "test: Parser tests",
                "description": "Some tests for parser.",
                "depends_on": [],
                "outputs": ["parser.py"],
            },
        ]
        violations = validate_test_step_contract(steps)
        assert len(violations) == 2

    def test_non_test_steps_ignored(self):
        steps = [
            {
                "title": "Build parser",
                "description": "Implement the parser.",
                "depends_on": [],
                "outputs": ["parser.py"],
            },
        ]
        assert validate_test_step_contract(steps) == []

    def test_case_insensitive_title(self):
        steps = [
            {
                "title": "Test: Write tests for parser",
                "description": "Write pytest tests for the parser module.",
                "depends_on": [],
                "outputs": ["test_parser.py"],
            },
        ]
        assert validate_test_step_contract(steps) == []

    def test_case_insensitive_description(self):
        steps = [
            {
                "title": "test: Write tests for parser",
                "description": "WRITE PYTEST TESTS FOR the parser.",
                "depends_on": [],
                "outputs": ["test_parser.py"],
            },
        ]
        assert validate_test_step_contract(steps) == []

    def test_multiple_test_steps_mixed(self):
        steps = [
            {
                "title": "test: Write tests for parser",
                "description": "Write pytest tests for the parser.",
                "depends_on": [],
                "outputs": ["test_parser.py"],
            },
            {
                "title": "Build parser",
                "description": "Implement parser.",
                "depends_on": [1],
                "outputs": ["parser.py"],
            },
            {
                "title": "test: Tests for utils",
                "description": "Create utility tests.",
                "depends_on": [],
                "outputs": ["utils.py"],
            },
        ]
        violations = validate_test_step_contract(steps)
        # Step 3 has both violations
        assert len(violations) == 2
        assert all("Step 3" in v for v in violations)


class TestValidateTddCoverageWithContract:
    """Test that validate_tdd_coverage includes test step contract checks."""

    def test_valid_tdd_plan(self):
        steps = [
            {
                "title": "test: Write tests for parser",
                "description": "Write pytest tests for the CSV parser.",
                "depends_on": [],
                "outputs": ["test_csv_parser.py"],
            },
            {
                "title": "Implement CSV parser",
                "description": "Build the parser to pass all tests.",
                "depends_on": [1],
                "outputs": ["csv_parser.py"],
            },
        ]
        assert validate_tdd_coverage(steps) == []

    def test_contract_violations_included(self):
        """validate_tdd_coverage catches test step contract violations."""
        steps = [
            {
                "title": "test: Parser tests",
                "description": "Some tests.",
                "depends_on": [],
                "outputs": ["parser.py"],  # wrong pattern
            },
            {
                "title": "Implement parser",
                "description": "Build it.",
                "depends_on": [1],
                "outputs": ["parser.py"],
            },
        ]
        violations = validate_tdd_coverage(steps)
        # Should have contract violations for step 1 (outputs + description)
        assert any("test_*.py or *_test.py" in v for v in violations)
        assert any("Write pytest tests for" in v for v in violations)

    def test_both_contract_and_dependency_violations(self):
        """Catches both malformed test steps and missing test dependencies."""
        steps = [
            {
                "title": "test: Bad test step",
                "description": "No proper description.",
                "depends_on": [],
                "outputs": [],  # missing test file
            },
            {
                "title": "Implement A",
                "description": "Build A.",
                "depends_on": [1],
                "outputs": ["a.py"],
            },
            {
                "title": "Implement B",
                "description": "Build B.",
                "depends_on": [],  # no test dep
                "outputs": ["b.py"],
            },
        ]
        violations = validate_tdd_coverage(steps)
        # Contract violations for step 1 + dependency violation for step 3
        assert len(violations) >= 3


class TestCollectTestFilesForStep:
    """Phase 4.4: Test that _collect_test_files_for_step finds test file content."""

    def test_collects_from_completed_test_dependency(self, tmp_path):
        # Create a test file in the workspace.
        test_file = tmp_path / "test_math.py"
        test_file.write_text("def test_add():\n    assert 1 + 1 == 2\n",
                             encoding="utf-8")
        state = {
            "steps": [
                {
                    "id": 1,
                    "title": "test: Write tests for math",
                    "status": "completed",
                    "depends_on": [],
                    "outputs": ["test_math.py"],
                    "files_written": ["test_math.py"],
                },
                {
                    "id": 2,
                    "title": "Implement math utils",
                    "status": "pending",
                    "depends_on": [1],
                    "outputs": ["math_utils.py"],
                },
            ],
        }
        import architect.main as am
        orig = am.PROJECT_DIR
        am.PROJECT_DIR = str(tmp_path)
        try:
            result = _collect_test_files_for_step(state["steps"][1], state)
        finally:
            am.PROJECT_DIR = orig
        assert "test_math.py" in result
        assert "def test_add():" in result["test_math.py"]

    def test_skips_non_test_dependency(self, tmp_path):
        state = {
            "steps": [
                {
                    "id": 1,
                    "title": "Setup environment",
                    "status": "completed",
                    "depends_on": [],
                    "outputs": ["setup.py"],
                },
                {
                    "id": 2,
                    "title": "Build it",
                    "status": "pending",
                    "depends_on": [1],
                    "outputs": ["main.py"],
                },
            ],
        }
        import architect.main as am
        orig = am.PROJECT_DIR
        am.PROJECT_DIR = str(tmp_path)
        try:
            result = _collect_test_files_for_step(state["steps"][1], state)
        finally:
            am.PROJECT_DIR = orig
        assert result == {}

    def test_skips_if_step_is_test_step_itself(self, tmp_path):
        state = {
            "steps": [
                {
                    "id": 1,
                    "title": "test: Write tests",
                    "status": "pending",
                    "depends_on": [],
                    "outputs": ["test_foo.py"],
                },
            ],
        }
        import architect.main as am
        orig = am.PROJECT_DIR
        am.PROJECT_DIR = str(tmp_path)
        try:
            result = _collect_test_files_for_step(state["steps"][0], state)
        finally:
            am.PROJECT_DIR = orig
        assert result == {}

    def test_no_dependencies_returns_empty(self, tmp_path):
        state = {
            "steps": [
                {
                    "id": 1,
                    "title": "Build it",
                    "status": "pending",
                    "depends_on": [],
                    "outputs": ["main.py"],
                },
            ],
        }
        result = _collect_test_files_for_step(state["steps"][0], state)
        assert result == {}

    def test_skips_incomplete_test_step(self, tmp_path):
        state = {
            "steps": [
                {
                    "id": 1,
                    "title": "test: Tests for parser",
                    "status": "pending",
                    "depends_on": [],
                    "outputs": ["test_parser.py"],
                },
                {
                    "id": 2,
                    "title": "Build parser",
                    "status": "pending",
                    "depends_on": [1],
                    "outputs": ["parser.py"],
                },
            ],
        }
        import architect.main as am
        orig = am.PROJECT_DIR
        am.PROJECT_DIR = str(tmp_path)
        try:
            result = _collect_test_files_for_step(state["steps"][1], state)
        finally:
            am.PROJECT_DIR = orig
        assert result == {}

    def test_collects_from_files_written_field(self, tmp_path):
        """Test files in files_written but not outputs are also collected."""
        test_file = tmp_path / "test_utils.py"
        test_file.write_text("def test_helper(): pass\n", encoding="utf-8")
        state = {
            "steps": [
                {
                    "id": 1,
                    "title": "test: Write tests for utils",
                    "status": "completed",
                    "depends_on": [],
                    "outputs": [],
                    "files_written": ["test_utils.py"],
                },
                {
                    "id": 2,
                    "title": "Implement utils",
                    "status": "pending",
                    "depends_on": [1],
                    "outputs": ["utils.py"],
                },
            ],
        }
        import architect.main as am
        orig = am.PROJECT_DIR
        am.PROJECT_DIR = str(tmp_path)
        try:
            result = _collect_test_files_for_step(state["steps"][1], state)
        finally:
            am.PROJECT_DIR = orig
        assert "test_utils.py" in result
