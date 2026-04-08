"""Tests for the deterministic check types in integration/eval.py.

Phase 1 PLAN Section 3. Pure unit tests over synthetic workspaces —
no LLM, no container, no network. Each new check type
(``pytest_pass``, ``exit_code``, ``file_shape``, ``command_succeeds``)
is exercised on at least one positive and one negative path.
"""

import json
import os
import sys

import pytest

# `integration/` is not a package (no __init__.py), so import the
# eval module by adding its parent to sys.path. Avoids polluting the
# tests/ namespace with a stray import path.
_INTEG_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "integration")
)
if _INTEG_DIR not in sys.path:
    sys.path.insert(0, _INTEG_DIR)

import eval as ev  # noqa: E402


@pytest.fixture
def workspace(tmp_path):
    """A clean temporary workspace directory for each test."""
    return str(tmp_path)


# ============================================================
# pytest_pass
# ============================================================


class TestPytestPassCheck:
    def test_passes_when_all_tests_pass(self, workspace):
        with open(os.path.join(workspace, "test_ok.py"), "w") as f:
            f.write("def test_one():\n    assert 1 == 1\n")
        result = ev.run_check(
            {"type": "pytest_pass", "path": "test_ok.py"},
            workspace,
        )
        assert result["passed"] is True
        assert result["type"] == "pytest_pass"
        assert "all tests passed" in result["detail"]

    def test_fails_when_test_fails(self, workspace):
        with open(os.path.join(workspace, "test_bad.py"), "w") as f:
            f.write("def test_bad():\n    assert False\n")
        result = ev.run_check(
            {"type": "pytest_pass", "path": "test_bad.py"},
            workspace,
        )
        assert result["passed"] is False
        # Either the FAILED line is surfaced, or the exit code is.
        assert "exit" in result["detail"]

    def test_path_not_found(self, workspace):
        result = ev.run_check(
            {"type": "pytest_pass", "path": "nonexistent.py"},
            workspace,
        )
        assert result["passed"] is False
        assert "not found" in result["detail"]

    def test_runs_directory_when_path_is_dir(self, workspace):
        sub = os.path.join(workspace, "subtests")
        os.makedirs(sub)
        with open(os.path.join(sub, "test_a.py"), "w") as f:
            f.write("def test_a():\n    assert True\n")
        result = ev.run_check(
            {"type": "pytest_pass", "path": "subtests"},
            workspace,
        )
        assert result["passed"] is True


# ============================================================
# exit_code
# ============================================================


class TestExitCodeCheck:
    def test_passes_when_exit_matches_default(self, workspace):
        result = ev.run_check(
            {"type": "exit_code"},
            workspace,
            invocation={"exit_code": 0, "elapsed": 1.0, "stderr_tail": ""},
        )
        assert result["passed"] is True
        assert result["expected"] == 0

    def test_passes_when_exit_matches_explicit(self, workspace):
        result = ev.run_check(
            {"type": "exit_code", "expected": 2},
            workspace,
            invocation={"exit_code": 2, "elapsed": 1.0, "stderr_tail": ""},
        )
        assert result["passed"] is True

    def test_fails_when_exit_differs(self, workspace):
        result = ev.run_check(
            {"type": "exit_code", "expected": 0},
            workspace,
            invocation={"exit_code": 1, "elapsed": 1.0, "stderr_tail": ""},
        )
        assert result["passed"] is False
        assert "exit_code=1" in result["detail"]

    def test_fails_when_invocation_missing(self, workspace):
        result = ev.run_check({"type": "exit_code"}, workspace)
        assert result["passed"] is False
        assert "invocation" in result["detail"]


# ============================================================
# file_shape — CSV
# ============================================================


class TestFileShapeCheckCSV:
    def test_min_rows_pass(self, workspace):
        with open(os.path.join(workspace, "data.csv"), "w") as f:
            f.write("a,b,c\n1,2,3\n4,5,6\n7,8,9\n")
        result = ev.run_check(
            {"type": "file_shape", "path": "data.csv",
             "format": "csv", "min_rows": 3},
            workspace,
        )
        assert result["passed"] is True

    def test_min_rows_fail(self, workspace):
        with open(os.path.join(workspace, "data.csv"), "w") as f:
            f.write("a,b,c\n1,2,3\n")
        result = ev.run_check(
            {"type": "file_shape", "path": "data.csv",
             "format": "csv", "min_rows": 5},
            workspace,
        )
        assert result["passed"] is False
        assert "min_rows" in result["detail"]

    def test_max_rows_fail(self, workspace):
        with open(os.path.join(workspace, "data.csv"), "w") as f:
            f.write("a\n1\n2\n3\n4\n5\n")
        result = ev.run_check(
            {"type": "file_shape", "path": "data.csv",
             "format": "csv", "max_rows": 3},
            workspace,
        )
        assert result["passed"] is False
        assert "max_rows" in result["detail"]

    def test_min_columns_pass(self, workspace):
        with open(os.path.join(workspace, "data.csv"), "w") as f:
            f.write("a,b,c\n1,2,3\n")
        result = ev.run_check(
            {"type": "file_shape", "path": "data.csv",
             "format": "csv", "min_columns": 3},
            workspace,
        )
        assert result["passed"] is True

    def test_required_columns_pass(self, workspace):
        with open(os.path.join(workspace, "data.csv"), "w") as f:
            f.write("name,age,email\nAlice,30,a@x\n")
        result = ev.run_check(
            {"type": "file_shape", "path": "data.csv",
             "format": "csv", "required_columns": ["name", "age"]},
            workspace,
        )
        assert result["passed"] is True

    def test_required_columns_fail(self, workspace):
        with open(os.path.join(workspace, "data.csv"), "w") as f:
            f.write("name,age\nAlice,30\n")
        result = ev.run_check(
            {"type": "file_shape", "path": "data.csv",
             "format": "csv", "required_columns": ["email"]},
            workspace,
        )
        assert result["passed"] is False
        assert "email" in result["detail"]


# ============================================================
# file_shape — JSON
# ============================================================


class TestFileShapeCheckJSON:
    def test_required_keys_pass_object(self, workspace):
        with open(os.path.join(workspace, "data.json"), "w") as f:
            json.dump({"name": "x", "value": 42}, f)
        result = ev.run_check(
            {"type": "file_shape", "path": "data.json",
             "format": "json", "required_keys": ["name", "value"]},
            workspace,
        )
        assert result["passed"] is True

    def test_required_keys_fail(self, workspace):
        with open(os.path.join(workspace, "data.json"), "w") as f:
            json.dump({"name": "x"}, f)
        result = ev.run_check(
            {"type": "file_shape", "path": "data.json",
             "format": "json", "required_keys": ["name", "value"]},
            workspace,
        )
        assert result["passed"] is False
        assert "value" in result["detail"]

    def test_min_rows_on_array(self, workspace):
        with open(os.path.join(workspace, "data.json"), "w") as f:
            json.dump([{"i": 1}, {"i": 2}, {"i": 3}], f)
        result = ev.run_check(
            {"type": "file_shape", "path": "data.json",
             "format": "json", "min_rows": 3},
            workspace,
        )
        assert result["passed"] is True

    def test_parse_error(self, workspace):
        with open(os.path.join(workspace, "bad.json"), "w") as f:
            f.write("{not valid json}")
        result = ev.run_check(
            {"type": "file_shape", "path": "bad.json", "format": "json"},
            workspace,
        )
        assert result["passed"] is False
        assert "parse error" in result["detail"]

    def test_file_missing(self, workspace):
        result = ev.run_check(
            {"type": "file_shape", "path": "missing.json", "format": "json"},
            workspace,
        )
        assert result["passed"] is False
        assert "not found" in result["detail"]


# ============================================================
# file_shape — JSONL
# ============================================================


class TestFileShapeCheckJSONL:
    def test_jsonl_min_rows_pass(self, workspace):
        with open(os.path.join(workspace, "data.jsonl"), "w") as f:
            for i in range(5):
                f.write(json.dumps({"i": i, "v": str(i)}) + "\n")
        result = ev.run_check(
            {"type": "file_shape", "path": "data.jsonl",
             "format": "jsonl", "min_rows": 5},
            workspace,
        )
        assert result["passed"] is True

    def test_jsonl_required_keys(self, workspace):
        with open(os.path.join(workspace, "data.jsonl"), "w") as f:
            f.write(json.dumps({"a": 1}) + "\n")
            f.write(json.dumps({"a": 2}) + "\n")
        result = ev.run_check(
            {"type": "file_shape", "path": "data.jsonl",
             "format": "jsonl", "required_keys": ["a", "b"]},
            workspace,
        )
        assert result["passed"] is False
        assert "b" in result["detail"]

    def test_jsonl_skips_blank_lines(self, workspace):
        with open(os.path.join(workspace, "data.jsonl"), "w") as f:
            f.write(json.dumps({"i": 1}) + "\n")
            f.write("\n")
            f.write(json.dumps({"i": 2}) + "\n")
        result = ev.run_check(
            {"type": "file_shape", "path": "data.jsonl",
             "format": "jsonl", "min_rows": 2},
            workspace,
        )
        assert result["passed"] is True


# ============================================================
# file_shape — unknown format
# ============================================================


def test_file_shape_unknown_format(workspace):
    with open(os.path.join(workspace, "x.bin"), "w") as f:
        f.write("data")
    result = ev.run_check(
        {"type": "file_shape", "path": "x.bin", "format": "binary"},
        workspace,
    )
    assert result["passed"] is False
    assert "unknown format" in result["detail"]


# ============================================================
# command_succeeds
# ============================================================


class TestCommandSucceedsCheck:
    def test_simple_true(self, workspace):
        result = ev.run_check(
            {"type": "command_succeeds", "cmd": ["true"]},
            workspace,
        )
        assert result["passed"] is True
        assert "exit_code=0" in result["detail"]

    def test_simple_false(self, workspace):
        result = ev.run_check(
            {"type": "command_succeeds", "cmd": ["false"]},
            workspace,
        )
        assert result["passed"] is False

    def test_missing_cmd_key(self, workspace):
        result = ev.run_check({"type": "command_succeeds"}, workspace)
        assert result["passed"] is False
        assert "cmd" in result["detail"]

    def test_cmd_not_a_list(self, workspace):
        result = ev.run_check(
            {"type": "command_succeeds", "cmd": "true"},
            workspace,
        )
        assert result["passed"] is False
        assert "list" in result["detail"]

    def test_command_not_found(self, workspace):
        result = ev.run_check(
            {"type": "command_succeeds",
             "cmd": ["nonexistent_xyzzy_binary_section3_test"]},
            workspace,
        )
        assert result["passed"] is False
        assert "not found" in result["detail"]

    def test_runs_in_workspace(self, workspace):
        with open(os.path.join(workspace, "marker"), "w") as f:
            f.write("ok")
        result = ev.run_check(
            {"type": "command_succeeds", "cmd": ["test", "-f", "marker"]},
            workspace,
        )
        assert result["passed"] is True

    def test_cwd_relative(self, workspace):
        sub = os.path.join(workspace, "subdir")
        os.makedirs(sub)
        with open(os.path.join(sub, "marker"), "w") as f:
            f.write("ok")
        result = ev.run_check(
            {"type": "command_succeeds",
             "cmd": ["test", "-f", "marker"],
             "cwd_relative": "subdir"},
            workspace,
        )
        assert result["passed"] is True


# ============================================================
# Existing types still work after the run_check signature change
# ============================================================


class TestExistingChecksUnchanged:
    def test_file_exists_still_works(self, workspace):
        with open(os.path.join(workspace, "x.txt"), "w") as f:
            f.write("y")
        result = ev.run_check(
            {"type": "file_exists", "path": "x.txt"},
            workspace,
        )
        assert result["passed"] is True

    def test_file_contains_still_works(self, workspace):
        with open(os.path.join(workspace, "x.txt"), "w") as f:
            f.write("hello world")
        result = ev.run_check(
            {"type": "file_contains", "path": "x.txt",
             "pattern": "hello"},
            workspace,
        )
        assert result["passed"] is True

    def test_glob_exists_still_works(self, workspace):
        with open(os.path.join(workspace, "a.py"), "w") as f:
            f.write("# x")
        with open(os.path.join(workspace, "b.py"), "w") as f:
            f.write("# y")
        result = ev.run_check(
            {"type": "glob_exists", "pattern": "*.py"},
            workspace,
        )
        assert result["passed"] is True

    def test_unknown_type(self, workspace):
        result = ev.run_check({"type": "made_up"}, workspace)
        assert result["passed"] is False
        assert "unknown check type" in result["detail"]
