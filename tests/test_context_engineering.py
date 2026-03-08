"""Tests for Section 4: Context Engineering.

Covers: structured progress file (4a), recursive workspace scanning (4b),
tiered context compression (4c), and dependency output distillation (4d).
"""

import json
import os
from unittest.mock import patch, MagicMock

import pytest

from architect.state import update_progress_file, read_progress_file
from architect.executor import (
    scan_workspace_files,
    format_workspace_scan,
    _SKIP_DIRS,
    _MAX_SCAN_OUTPUT,
)
from architect.main import (
    build_context,
    compress_context,
    _distill_dependency_output,
)


# ── Section 4a: Structured progress file ─────────────────────────


class TestUpdateProgressFile:
    def test_creates_progress_file(self, tmp_workspace):
        state = {"goal": "test", "steps": []}
        update_progress_file(state)
        content = read_progress_file()
        assert "## Current State" in content
        assert "Steps completed: 0/0" in content

    def test_shows_completed_steps(self, tmp_workspace):
        state = {
            "goal": "test",
            "steps": [
                {
                    "id": 1, "title": "Download data", "status": "completed",
                    "summary": "Downloaded CSV",
                    "files_written": ["data.csv"],
                    "elapsed": 5.2, "output": "", "reflections": [],
                },
            ],
        }
        update_progress_file(state)
        content = read_progress_file()
        assert "Steps completed: 1/1" in content
        assert "## Completed Steps" in content
        assert "Download data" in content
        assert "Downloaded CSV" in content
        assert "data.csv" in content

    def test_shows_blockers_from_failed_steps(self, tmp_workspace):
        state = {
            "goal": "test",
            "steps": [
                {
                    "id": 1, "title": "Compile", "status": "failed",
                    "error": "syntax error on line 42",
                    "output": "", "reflections": [],
                },
            ],
        }
        update_progress_file(state)
        content = read_progress_file()
        assert "Known blockers" in content
        assert "syntax error" in content

    def test_shows_lessons_from_reflections(self, tmp_workspace):
        state = {
            "goal": "test",
            "steps": [
                {
                    "id": 1, "title": "Build", "status": "failed",
                    "error": "err", "output": "",
                    "reflections": [{
                        "attempt": 1, "error_type": "logic_error",
                        "root_cause": "bad logic",
                        "strategy_tried": "initial",
                        "lesson": "Need to validate input first",
                        "what_to_try_next": "Add input validation",
                    }],
                },
            ],
        }
        update_progress_file(state)
        content = read_progress_file()
        assert "## Lessons Learned" in content
        assert "validate input" in content

    def test_includes_event(self, tmp_workspace):
        state = {"goal": "test", "steps": []}
        update_progress_file(state, event="Step 1 completed")
        content = read_progress_file()
        assert "Step 1 completed" in content
        assert "Latest Event" in content

    def test_read_missing_returns_empty(self, tmp_workspace):
        assert read_progress_file() == ""

    def test_overwrites_on_each_call(self, tmp_workspace):
        state = {"goal": "test", "steps": []}
        update_progress_file(state, event="first")
        update_progress_file(state, event="second")
        content = read_progress_file()
        # File is overwritten, not appended
        assert content.count("## Current State") == 1
        assert "second" in content


# ── Section 4b: Recursive workspace scanning ─────────────────────


class TestRecursiveWorkspaceScan:
    def test_scans_top_level_files(self, tmp_path):
        (tmp_path / "file1.txt").write_text("hello")
        (tmp_path / "file2.py").write_text("print(1)")
        result = scan_workspace_files(str(tmp_path))
        assert "file1.txt" in result
        assert "file2.py" in result

    def test_scans_subdirectories(self, tmp_path):
        sub = tmp_path / "src"
        sub.mkdir()
        (sub / "main.py").write_text("code")
        result = scan_workspace_files(str(tmp_path))
        assert os.path.join("src", "main.py") in result

    def test_respects_max_depth(self, tmp_path):
        # Create 5-level deep structure
        deep = tmp_path
        for i in range(5):
            deep = deep / f"level{i}"
            deep.mkdir()
        (deep / "deep.txt").write_text("deep file")
        result = scan_workspace_files(str(tmp_path), max_depth=3)
        # level0/level1/level2/level3/level4/deep.txt is depth 5, beyond max_depth=3
        deep_key = os.path.join(*[f"level{i}" for i in range(5)], "deep.txt")
        assert deep_key not in result

    def test_skips_excluded_directories(self, tmp_path):
        for dirname in _SKIP_DIRS:
            d = tmp_path / dirname
            d.mkdir()
            (d / "skip_me.txt").write_text("skip")
        (tmp_path / "keep.txt").write_text("keep")
        result = scan_workspace_files(str(tmp_path))
        assert "keep.txt" in result
        for dirname in _SKIP_DIRS:
            key = os.path.join(dirname, "skip_me.txt")
            assert key not in result

    def test_skips_hidden_files(self, tmp_path):
        (tmp_path / ".hidden").write_text("hidden")
        (tmp_path / "visible.txt").write_text("visible")
        result = scan_workspace_files(str(tmp_path))
        assert ".hidden" not in result
        assert "visible.txt" in result

    def test_non_recursive_mode(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "nested.txt").write_text("nested")
        (tmp_path / "top.txt").write_text("top")
        result = scan_workspace_files(str(tmp_path), recursive=False)
        assert "top.txt" in result
        assert os.path.join("sub", "nested.txt") not in result

    def test_preview_limited_to_200_chars(self, tmp_path):
        (tmp_path / "long.txt").write_text("x" * 1000)
        result = scan_workspace_files(str(tmp_path))
        assert len(result["long.txt"]["preview"]) <= 200

    def test_empty_workspace(self, tmp_path):
        assert scan_workspace_files(str(tmp_path)) == {}

    def test_nonexistent_path(self):
        assert scan_workspace_files("/nonexistent/path") == {}


class TestFormatWorkspaceScan:
    def test_formats_single_file(self):
        files = {"test.txt": {"size": 100, "type": "text", "preview": "hello"}}
        result = format_workspace_scan(files)
        assert "test.txt" in result
        assert "100 bytes" in result
        assert "preview: hello" in result

    def test_groups_by_directory(self):
        files = {
            "top.txt": {"size": 10, "type": "text", "preview": ""},
            os.path.join("src", "main.py"): {"size": 50, "type": "text", "preview": ""},
        }
        result = format_workspace_scan(files)
        assert "[src/]" in result
        assert "main.py" in result

    def test_json_key_extraction(self):
        files = {
            "data.json": {
                "size": 50, "type": "text",
                "preview": '{"name": "test", "value": 42}',
            },
        }
        def extract(s):
            return str(json.loads(s).keys())
        result = format_workspace_scan(files, json_key_extractor=extract)
        assert "keys:" in result
        assert "name" in result

    def test_empty_returns_empty(self):
        assert format_workspace_scan({}) == ""

    def test_caps_output_length(self):
        # Create many files to exceed _MAX_SCAN_OUTPUT
        files = {}
        for i in range(200):
            files[f"file_{i:03d}.txt"] = {
                "size": 100, "type": "text",
                "preview": "a" * 200,
            }
        result = format_workspace_scan(files)
        assert len(result) <= _MAX_SCAN_OUTPUT + 200  # Allow for final line


# ── Section 4c: Tiered context compression ───────────────────────


class TestCompressContext:
    def test_tier1_no_compression(self):
        context = "x" * 500
        result = compress_context(context, max_length=1000)
        assert result == context

    def test_tier2_removes_previews(self):
        context = (
            '<dependency step="1" title="Step 1">\n'
            "  <key_outputs>important data</key_outputs>\n"
            "</dependency>\n"
            "<workspace_files>\n"
            "  file.txt (100 bytes, text)\n"
            "    preview: some long preview content here\n"
            "    keys: [a, b, c]\n"
            "</workspace_files>"
        )
        # Set max_length so ratio is 0.6-0.8 (tier 2)
        max_length = int(len(context) / 0.7)
        result = compress_context(context, max_length=max_length)
        assert "important data" in result
        # Preview and keys lines should be removed
        assert "preview:" not in result
        assert "keys:" not in result

    def test_tier4_emergency_truncation(self):
        progress = "## Current State\n- Steps completed: 5/10"
        context = "x" * 10000
        result = compress_context(
            context, max_length=500,
            progress_content=progress,
        )
        assert "## Current State" in result
        assert len(result) <= 550  # Allow slight overflow from header

    def test_tier4_without_progress(self):
        context = "x" * 10000
        result = compress_context(context, max_length=500)
        assert "truncated" in result
        assert len(result) < len(context)

    def test_no_limit_returns_original(self):
        context = "x" * 10000
        assert compress_context(context, max_length=0) == context


# ── Section 4d: Dependency output distillation ───────────────────


class TestDistillDependencyOutput:
    def test_structured_output_with_summary(self):
        dep_step = {
            "title": "Download data",
            "summary": "Downloaded 1000 rows of CSV",
            "files_written": ["data.csv"],
            "verify": "",
        }
        result = _distill_dependency_output(1, dep_step, "raw stdout")
        assert '<dependency step="1"' in result
        assert "Download data" in result
        assert "<files_produced>data.csv</files_produced>" in result
        assert "<key_outputs>Downloaded 1000 rows of CSV</key_outputs>" in result
        assert "</dependency>" in result

    def test_fallback_to_stdout_when_no_summary(self):
        dep_step = {
            "title": "Run script",
            "summary": "",
            "files_written": [],
            "verify": "",
        }
        result = _distill_dependency_output(
            2, dep_step,
            {"stdout": "some stdout output", "stderr": "", "files": []},
        )
        assert "<key_outputs>some stdout output</key_outputs>" in result

    def test_includes_verification(self):
        dep_step = {
            "title": "Build",
            "summary": "Built OK",
            "files_written": ["app.py"],
            "verify": "check app.py exists",
        }
        result = _distill_dependency_output(1, dep_step, "")
        assert "<verification>check app.py exists</verification>" in result

    def test_dict_output_with_stderr(self):
        dep_step = {
            "title": "Build",
            "summary": "OK",
            "files_written": [],
            "verify": "",
        }
        output = {"stdout": "", "stderr": "warning: deprecated", "files": []}
        result = _distill_dependency_output(1, dep_step, output)
        assert "stderr: warning: deprecated" in result

    def test_string_output_no_summary(self):
        dep_step = {
            "title": "Process",
            "summary": "",
            "files_written": [],
            "verify": "",
        }
        result = _distill_dependency_output(1, dep_step, "plain string output")
        assert "plain string output" in result


# ── Section 4: Integration with build_context ─────────────────────


class TestBuildContextWithDistillation:
    def test_uses_distilled_format_when_step_has_summary(self):
        step = {"depends_on": [1]}
        outputs = {1: {"stdout": "raw output", "stderr": "", "files": ["out.txt"]}}
        state = {
            "goal": "test",
            "steps": [{
                "id": 1, "title": "Prep", "depends_on": [],
                "verify": "", "summary": "Prepared data",
                "files_written": ["out.txt"],
                "status": "completed",
            }],
        }
        result = build_context(step, outputs, state=state)
        assert "<dependency" in result
        assert "Prepared data" in result
        assert "<files_produced>" in result

    def test_falls_back_to_legacy_for_plain_output(self):
        step = {"depends_on": [1]}
        outputs = {1: "plain string output"}
        # No state provided, so no dep_step metadata
        result = build_context(step, outputs)
        assert "previous_step_output" in result
        assert "plain string output" in result

    @patch("architect.main.read_progress_file")
    @patch("architect.main.scan_workspace_files")
    def test_includes_progress_file(self, mock_scan, mock_progress):
        mock_scan.return_value = {}
        mock_progress.return_value = "## Current State\n- Steps completed: 2/5"
        step = {"depends_on": [1]}
        outputs = {1: "output"}
        result = build_context(step, outputs)
        assert "<progress>" in result
        assert "Steps completed: 2/5" in result

    @patch("architect.main.read_progress_file")
    @patch("architect.main.read_scratchpad")
    @patch("architect.main.scan_workspace_files")
    def test_falls_back_to_scratchpad_when_no_progress(
        self, mock_scan, mock_scratchpad, mock_progress,
    ):
        mock_scan.return_value = {}
        mock_progress.return_value = ""
        mock_scratchpad.return_value = "old scratchpad entry"
        step = {"depends_on": [1]}
        outputs = {1: "output"}
        result = build_context(step, outputs)
        assert "<scratchpad>" in result
        assert "old scratchpad entry" in result
