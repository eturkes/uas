"""Tests for extract_module_api() and module API in distilled output."""

import os
import textwrap

import pytest

from architect.main import extract_module_api, _distill_dependency_output


class TestExtractModuleApi:
    """Test extract_module_api() on sample Python source."""

    def test_functions(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text(textwrap.dedent("""\
            def make_card(title):
                pass

            def create_kpi_card(value):
                pass
        """), encoding="utf-8")
        api = extract_module_api(str(src))
        assert api["functions"] == ["make_card", "create_kpi_card"]
        assert "classes" not in api
        assert "constants" not in api

    def test_classes(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text(textwrap.dedent("""\
            class DataProcessor:
                pass

            class ChartBuilder:
                pass
        """), encoding="utf-8")
        api = extract_module_api(str(src))
        assert api["classes"] == ["DataProcessor", "ChartBuilder"]
        assert "functions" not in api

    def test_constants(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text(textwrap.dedent("""\
            CHART_COLORS = ["red", "blue"]
            DEFAULT_PADDING = 10
            some_var = "not a constant"
        """), encoding="utf-8")
        api = extract_module_api(str(src))
        assert api["constants"] == ["CHART_COLORS", "DEFAULT_PADDING"]
        assert "functions" not in api

    def test_annotated_constants(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text(textwrap.dedent("""\
            MAX_RETRIES: int = 3
        """), encoding="utf-8")
        api = extract_module_api(str(src))
        assert api["constants"] == ["MAX_RETRIES"]

    def test_mixed(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text(textwrap.dedent("""\
            COLORS = ["red"]

            class Theme:
                pass

            def apply_theme():
                pass

            def _private_helper():
                pass
        """), encoding="utf-8")
        api = extract_module_api(str(src))
        assert api["functions"] == ["apply_theme"]
        assert api["classes"] == ["Theme"]
        assert api["constants"] == ["COLORS"]

    def test_private_excluded(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text(textwrap.dedent("""\
            def _helper():
                pass

            class _Internal:
                pass
        """), encoding="utf-8")
        api = extract_module_api(str(src))
        assert api == {}

    def test_parse_error_returns_empty(self, tmp_path):
        src = tmp_path / "bad.py"
        src.write_text("def broken(\n", encoding="utf-8")
        api = extract_module_api(str(src))
        assert api == {}

    def test_nonexistent_file(self):
        api = extract_module_api("/nonexistent/file.py")
        assert api == {}

    def test_empty_file(self, tmp_path):
        src = tmp_path / "empty.py"
        src.write_text("", encoding="utf-8")
        api = extract_module_api(str(src))
        assert api == {}

    def test_async_functions(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text(textwrap.dedent("""\
            async def fetch_data():
                pass
        """), encoding="utf-8")
        api = extract_module_api(str(src))
        assert api["functions"] == ["fetch_data"]


class TestDistillIncludesModuleApi:
    """Test that _distill_dependency_output includes <module_api> for .py files."""

    def test_module_api_in_distill(self, tmp_path):
        src = tmp_path / "layout.py"
        src.write_text(textwrap.dedent("""\
            CARD_STYLE = {}
            DEFAULT_PADDING = 8

            def make_card(title):
                pass

            def create_kpi_card(value):
                pass
        """), encoding="utf-8")

        dep_step = {
            "title": "Create layout",
            "files_written": [str(src)],
            "summary": "Created layout components",
        }
        result = _distill_dependency_output(1, dep_step, "ok")

        assert "<module_api" in result
        assert 'file="' in result
        assert "make_card" in result
        assert "create_kpi_card" in result
        assert "CARD_STYLE" in result
        assert "DEFAULT_PADDING" in result

    def test_no_module_api_for_non_py(self, tmp_path):
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("a,b\n1,2\n", encoding="utf-8")

        dep_step = {
            "title": "Load data",
            "files_written": [str(csv_file)],
            "summary": "Loaded data",
        }
        result = _distill_dependency_output(1, dep_step, "ok")
        assert "<module_api" not in result

    def test_no_module_api_for_empty_module(self, tmp_path):
        src = tmp_path / "empty.py"
        src.write_text("# just a comment\n", encoding="utf-8")

        dep_step = {
            "title": "Create placeholder",
            "files_written": [str(src)],
            "summary": "Created placeholder",
        }
        result = _distill_dependency_output(1, dep_step, "ok")
        assert "<module_api" not in result

    def test_multiple_py_files(self, tmp_path):
        mod_a = tmp_path / "a.py"
        mod_a.write_text("def func_a(): pass\n", encoding="utf-8")
        mod_b = tmp_path / "b.py"
        mod_b.write_text("def func_b(): pass\n", encoding="utf-8")

        dep_step = {
            "title": "Create modules",
            "files_written": [str(mod_a), str(mod_b)],
            "summary": "Created modules",
        }
        result = _distill_dependency_output(1, dep_step, "ok")
        assert "func_a" in result
        assert "func_b" in result
        assert result.count("<module_api") == 2

    def test_nonexistent_py_file_skipped(self):
        dep_step = {
            "title": "Create module",
            "files_written": ["/nonexistent/mod.py"],
            "summary": "Created",
        }
        result = _distill_dependency_output(1, dep_step, "ok")
        assert "<module_api" not in result
