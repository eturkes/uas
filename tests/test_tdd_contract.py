"""Tests for TDD test-step contract enforcement (Tasks 4.3, 4.4, 4.6)."""

import os
import subprocess
import sys
from unittest.mock import patch, MagicMock

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


class TestDiscoverAllTestFiles:
    """Phase 4.6: Test discovery of all test files in the workspace."""

    def test_discovers_root_test_files(self, tmp_path):
        (tmp_path / "test_foo.py").write_text("pass\n")
        (tmp_path / "bar_test.py").write_text("pass\n")
        (tmp_path / "helpers.py").write_text("pass\n")
        from architect.main import _discover_all_test_files
        result = _discover_all_test_files(str(tmp_path))
        assert sorted(result) == ["bar_test.py", "test_foo.py"]

    def test_discovers_nested_test_files(self, tmp_path):
        sub = tmp_path / "tests"
        sub.mkdir()
        (sub / "test_core.py").write_text("pass\n")
        (tmp_path / "main.py").write_text("pass\n")
        from architect.main import _discover_all_test_files
        result = _discover_all_test_files(str(tmp_path))
        assert result == [os.path.join("tests", "test_core.py")]

    def test_skips_hidden_and_venv_dirs(self, tmp_path):
        for d in [".git", "__pycache__", ".venv", "node_modules"]:
            skip = tmp_path / d
            skip.mkdir()
            (skip / "test_hidden.py").write_text("pass\n")
        (tmp_path / "test_real.py").write_text("pass\n")
        from architect.main import _discover_all_test_files
        result = _discover_all_test_files(str(tmp_path))
        assert result == ["test_real.py"]

    def test_empty_workspace(self, tmp_path):
        from architect.main import _discover_all_test_files
        result = _discover_all_test_files(str(tmp_path))
        assert result == []

    def test_non_python_test_files_ignored(self, tmp_path):
        (tmp_path / "test_something.txt").write_text("pass\n")
        (tmp_path / "test_something.js").write_text("pass\n")
        from architect.main import _discover_all_test_files
        result = _discover_all_test_files(str(tmp_path))
        assert result == []


class TestRunFullPytestSuite:
    """Phase 4.6: Test full test suite execution."""

    def test_no_test_files_returns_none(self, tmp_path):
        from architect.main import _run_full_pytest_suite
        assert _run_full_pytest_suite(str(tmp_path)) is None

    @patch("architect.main.subprocess.run")
    @patch("architect.main._discover_all_test_files", return_value=["test_a.py"])
    def test_all_pass_returns_none(self, _mock_discover, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="1 passed", stderr="")
        from architect.main import _run_full_pytest_suite
        assert _run_full_pytest_suite("/fake/workspace") is None

    @patch("architect.main.subprocess.run")
    @patch("architect.main._discover_all_test_files",
           return_value=["test_a.py", "test_b.py"])
    def test_failure_returns_error_string(self, _mock_discover, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="FAILED test_a.py::test_x", stderr=""
        )
        from architect.main import _run_full_pytest_suite
        result = _run_full_pytest_suite("/fake/workspace")
        assert result is not None
        assert "Full test suite FAILED" in result
        assert "FAILED test_a.py::test_x" in result

    @patch("architect.main.subprocess.run", side_effect=FileNotFoundError("no pytest"))
    @patch("architect.main._discover_all_test_files", return_value=["test_a.py"])
    def test_pytest_not_found_returns_none(self, _mock_discover, _mock_run):
        from architect.main import _run_full_pytest_suite
        assert _run_full_pytest_suite("/fake/workspace") is None

    @patch("architect.main.subprocess.run",
           side_effect=__import__("subprocess").TimeoutExpired(cmd="pytest", timeout=300))
    @patch("architect.main._discover_all_test_files", return_value=["test_a.py"])
    def test_timeout_returns_error(self, _mock_discover, _mock_run):
        from architect.main import _run_full_pytest_suite
        result = _run_full_pytest_suite("/fake/workspace")
        assert result is not None
        assert "timed out" in result


class TestRunFullPytestSuiteSection7Regression:
    """Section 7 of PLAN.md: the architect's full-pytest gate must scope
    to test files this attempt actually touched, instead of every test
    file ``_discover_all_test_files`` finds. Pre-existing test files
    committed to ``uas-wip`` from a prior failed run that reference
    modules built by not-yet-run steps must NOT fail the current step.

    These tests build a real git workspace with a ``uas-wip`` baseline so
    they exercise the scope-by-diff path of ``_run_full_pytest_suite``
    end-to-end.
    """

    @staticmethod
    def _git(workspace, *args):
        subprocess.run(
            ["git"] + list(args),
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True,
        )

    @pytest.fixture()
    def real_workspace_with_orphan(self, tmp_path, monkeypatch):
        """Real git workspace with a pre-existing orphan test file
        (``tests/test_orphan.py``) that imports a module which does not
        exist. Mirrors the rehab/ failure mode where
        ``tests/test_config.py`` was committed to ``uas-wip`` from the
        user's original failed run and imports
        ``from rehab.config import PROJECT_ROOT`` even though
        ``rehab/config.py`` is scheduled for a later step.
        """
        monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.com")
        monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
        monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.com")

        ws = str(tmp_path)
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_orphan.py").write_text(
            "from notyetbuilt.module import VALUE\n\n"
            "def test_value_is_set():\n"
            "    assert VALUE is not None\n",
            encoding="utf-8",
        )
        self._git(ws, "init", "-b", "main")
        self._git(ws, "add", "-A")
        self._git(ws, "commit", "-m", "Initial workspace state")
        self._git(ws, "tag", "-f", "uas-main")
        self._git(ws, "checkout", "-b", "uas-wip")
        return ws

    def test_pre_existing_orphan_test_does_not_fail_step(
        self, real_workspace_with_orphan,
    ):
        """The exact rehab/ failure mode: an orphan test importing a
        not-yet-built module is committed to ``uas-wip``, nothing else
        has changed since. Direct pytest invocation against the orphan
        would error with ``ModuleNotFoundError`` and exit non-zero.
        ``_run_full_pytest_suite`` must scope it out and return ``None``.
        """
        from architect.main import _run_full_pytest_suite

        # Sanity check: direct pytest against the orphan does fail.
        # This proves the orphan is genuinely broken — the gate's
        # success below must come from scoping, not a healthy file.
        direct = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_orphan.py", "-q",
             "--tb=no", "-p", "no:cacheprovider"],
            cwd=real_workspace_with_orphan,
            capture_output=True,
            text=True,
        )
        assert direct.returncode != 0, (
            "Orphan test unexpectedly passed; the regression scenario is "
            "not actually broken and the gate test below is meaningless."
        )

        # The architect's scoped gate must NOT fail the step.
        assert _run_full_pytest_suite(real_workspace_with_orphan) is None

    def test_test_file_added_by_step_runs_pytest_passing(
        self, real_workspace_with_orphan,
    ):
        """A new passing test file added by the current attempt
        (untracked vs ``uas-wip``) IS included in the gate's pytest
        run. Result: gate returns None (success).
        """
        with open(
            os.path.join(real_workspace_with_orphan, "tests", "test_new.py"),
            "w",
        ) as f:
            f.write("def test_passes():\n    assert True\n")

        from architect.main import _run_full_pytest_suite
        assert _run_full_pytest_suite(real_workspace_with_orphan) is None

    def test_test_file_added_by_step_failure_propagates(
        self, real_workspace_with_orphan,
    ):
        """A new FAILING test file added by the current attempt produces
        the canonical failure string from the gate. This proves the
        gate still catches genuine regressions caused by this attempt.
        """
        with open(
            os.path.join(real_workspace_with_orphan, "tests", "test_new.py"),
            "w",
        ) as f:
            f.write("def test_fails():\n    assert False\n")

        from architect.main import _run_full_pytest_suite
        result = _run_full_pytest_suite(real_workspace_with_orphan)
        assert result is not None
        assert "Full test suite FAILED" in result

    def test_modified_pre_existing_test_runs_pytest(
        self, real_workspace_with_orphan,
    ):
        """A pre-existing test file MODIFIED by the current attempt
        is included in the gate's pytest run. Replacing the orphan
        contents with a passing test should let the gate return None.
        """
        with open(
            os.path.join(
                real_workspace_with_orphan, "tests", "test_orphan.py",
            ),
            "w",
        ) as f:
            f.write("def test_passes():\n    assert True\n")

        from architect.main import _run_full_pytest_suite
        assert _run_full_pytest_suite(real_workspace_with_orphan) is None

    def test_no_git_falls_back_to_legacy_discovery(self, tmp_path):
        """A non-git workspace must use the legacy
        ``_discover_all_test_files`` path so non-git callers and
        unit-test mocks keep working. A passing test file in the
        workspace must be picked up and the gate must return None.
        """
        (tmp_path / "test_legacy.py").write_text(
            "def test_passes():\n    assert True\n",
            encoding="utf-8",
        )

        from architect.main import _run_full_pytest_suite
        assert _run_full_pytest_suite(str(tmp_path)) is None

    def test_no_git_legacy_discovery_propagates_failure(self, tmp_path):
        """In the no-git fallback path, a failing test in the workspace
        is still surfaced. Confirms the legacy code path is intact.
        """
        (tmp_path / "test_legacy.py").write_text(
            "def test_fails():\n    assert False\n",
            encoding="utf-8",
        )

        from architect.main import _run_full_pytest_suite
        result = _run_full_pytest_suite(str(tmp_path))
        assert result is not None
        assert "Full test suite FAILED" in result
