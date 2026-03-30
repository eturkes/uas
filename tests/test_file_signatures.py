"""Tests for Section 4 — Richer dependency context with file signatures.

Verifies that extract_file_signatures() correctly extracts structural
signatures from Python, CSV, and JSON files, and that signatures are
integrated into the dependency context XML.
"""

import ast
import csv
import json
import os

import pytest

from architect.executor import (
    extract_file_signatures,
    _extract_py_signatures,
    _extract_csv_file_signatures,
    _extract_json_file_signatures,
    _format_func_sig,
)


# ---------------------------------------------------------------------------
# _format_func_sig
# ---------------------------------------------------------------------------

class TestFormatFuncSig:
    def test_simple_typed_function(self):
        tree = ast.parse("def foo(x: int, y: str) -> bool: pass")
        node = tree.body[0]
        result = _format_func_sig(node)
        assert result == "def foo(x: int, y: str) -> bool"

    def test_default_params(self):
        tree = ast.parse("def bar(a, b=10, c='hello'): pass")
        node = tree.body[0]
        result = _format_func_sig(node)
        assert "a" in result
        assert "b = ..." in result
        assert "c = ..." in result

    def test_star_args(self):
        tree = ast.parse("def baz(*args, **kwargs): pass")
        node = tree.body[0]
        result = _format_func_sig(node)
        assert "*args" in result
        assert "**kwargs" in result

    def test_async_function(self):
        tree = ast.parse("async def fetch(url: str) -> dict: pass")
        node = tree.body[0]
        result = _format_func_sig(node)
        assert result.startswith("async def fetch")
        assert "url: str" in result
        assert "-> dict" in result

    def test_no_params(self):
        tree = ast.parse("def noop(): pass")
        node = tree.body[0]
        assert _format_func_sig(node) == "def noop()"

    def test_return_type_only(self):
        tree = ast.parse("def get() -> list: pass")
        node = tree.body[0]
        result = _format_func_sig(node)
        assert "-> list" in result


# ---------------------------------------------------------------------------
# Python file signatures
# ---------------------------------------------------------------------------

class TestPythonSignatures:
    def test_function_with_types(self, tmp_path):
        py_file = tmp_path / "clean.py"
        py_file.write_text(
            "def clean_dataset(df: 'pd.DataFrame', threshold: float = 0.1)"
            " -> 'pd.DataFrame':\n"
            '    """Clean the dataset by removing outliers.\n'
            "\n"
            "    Uses IQR method for outlier detection.\n"
            '    """\n'
            "    pass\n"
        )
        sig = _extract_py_signatures(str(py_file))
        assert "def clean_dataset" in sig
        assert "pd.DataFrame" in sig
        assert "threshold: float" in sig
        assert "-> " in sig
        assert "Clean the dataset" in sig

    def test_class_with_methods(self, tmp_path):
        py_file = tmp_path / "pipeline.py"
        py_file.write_text(
            "class Pipeline:\n"
            '    """Data processing pipeline."""\n'
            "    def __init__(self, config: dict):\n"
            "        pass\n"
            "    def run(self, data: list) -> list:\n"
            "        pass\n"
            "    def _internal(self):\n"
            "        pass\n"
        )
        sig = _extract_py_signatures(str(py_file))
        assert "class Pipeline:" in sig
        assert "Data processing pipeline" in sig
        assert "__init__" in sig
        assert "config: dict" in sig
        assert "def run" in sig
        assert "_internal" not in sig

    def test_constants_and_variables(self, tmp_path):
        py_file = tmp_path / "config.py"
        py_file.write_text(
            "THRESHOLD = 0.5\n"
            "MAX_RETRIES = 3\n"
            'default_name = "test"\n'
            "_private = True\n"
        )
        sig = _extract_py_signatures(str(py_file))
        assert "THRESHOLD = ..." in sig
        assert "MAX_RETRIES = ..." in sig
        assert "default_name = ..." in sig
        assert "_private" not in sig

    def test_private_functions_excluded(self, tmp_path):
        py_file = tmp_path / "module.py"
        py_file.write_text(
            "def public_func():\n    pass\n"
            "def _private_func():\n    pass\n"
        )
        sig = _extract_py_signatures(str(py_file))
        assert "public_func" in sig
        assert "_private_func" not in sig

    def test_private_class_excluded(self, tmp_path):
        py_file = tmp_path / "module.py"
        py_file.write_text(
            "class Public:\n    pass\n"
            "class _Private:\n    pass\n"
        )
        sig = _extract_py_signatures(str(py_file))
        assert "Public" in sig
        assert "_Private" not in sig

    def test_async_function(self, tmp_path):
        py_file = tmp_path / "async_mod.py"
        py_file.write_text(
            "async def fetch_data(url: str) -> dict:\n    pass\n"
        )
        sig = _extract_py_signatures(str(py_file))
        assert "async def fetch_data" in sig
        assert "url: str" in sig
        assert "-> dict" in sig

    def test_annotated_assignment(self, tmp_path):
        py_file = tmp_path / "types.py"
        py_file.write_text(
            'VERSION: str = "1.0"\n'
            "count: int\n"
        )
        sig = _extract_py_signatures(str(py_file))
        assert "VERSION: str" in sig
        assert "count: int" in sig

    def test_empty_file(self, tmp_path):
        py_file = tmp_path / "empty.py"
        py_file.write_text("")
        assert _extract_py_signatures(str(py_file)) == ""

    def test_syntax_error_returns_empty(self, tmp_path):
        py_file = tmp_path / "bad.py"
        py_file.write_text("def broken(:\n  pass\n")
        assert _extract_py_signatures(str(py_file)) == ""

    def test_docstring_first_two_lines_only(self, tmp_path):
        py_file = tmp_path / "doc.py"
        py_file.write_text(
            "def example():\n"
            '    """First line of docstring.\n'
            "    Second line of docstring.\n"
            "    Third line should not appear.\n"
            '    """\n'
            "    pass\n"
        )
        sig = _extract_py_signatures(str(py_file))
        assert "First line" in sig
        assert "Second line" in sig
        assert "Third line" not in sig


# ---------------------------------------------------------------------------
# CSV file signatures
# ---------------------------------------------------------------------------

class TestCSVSignatures:
    def test_csv_columns_and_rows(self, tmp_path):
        csv_file = tmp_path / "data.csv"
        with open(csv_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "name", "score"])
            writer.writerow([1, "Alice", 95])
            writer.writerow([2, "Bob", 87])
        sig = _extract_csv_file_signatures(str(csv_file), ".csv")
        assert "id" in sig
        assert "name" in sig
        assert "score" in sig
        assert "3 columns" in sig
        assert "rows: 2" in sig

    def test_tsv_file(self, tmp_path):
        tsv_file = tmp_path / "data.tsv"
        with open(tsv_file, "w", newline="") as f:
            writer = csv.writer(f, delimiter="\t")
            writer.writerow(["col_a", "col_b"])
            writer.writerow(["x", "y"])
        sig = _extract_csv_file_signatures(str(tsv_file), ".tsv")
        assert "col_a" in sig
        assert "col_b" in sig
        assert "2 columns" in sig
        assert "rows: 1" in sig

    def test_empty_csv(self, tmp_path):
        csv_file = tmp_path / "empty.csv"
        csv_file.write_text("")
        assert _extract_csv_file_signatures(str(csv_file), ".csv") == ""

    def test_header_only_csv(self, tmp_path):
        csv_file = tmp_path / "header.csv"
        csv_file.write_text("a,b,c\n")
        sig = _extract_csv_file_signatures(str(csv_file), ".csv")
        assert "3 columns" in sig
        assert "rows: 0" in sig


# ---------------------------------------------------------------------------
# JSON file signatures
# ---------------------------------------------------------------------------

class TestJSONSignatures:
    def test_dict_keys(self, tmp_path):
        json_file = tmp_path / "config.json"
        json_file.write_text(json.dumps({
            "name": "test", "version": 1, "items": [1, 2, 3],
        }))
        sig = _extract_json_file_signatures(str(json_file))
        assert "name" in sig
        assert "version" in sig
        assert "items" in sig
        assert "3 items" in sig

    def test_list_of_dicts(self, tmp_path):
        json_file = tmp_path / "data.json"
        data = [
            {"id": 1, "value": "a"},
            {"id": 2, "value": "b"},
            {"id": 3, "value": "c"},
            {"id": 4, "value": "d"},
        ]
        json_file.write_text(json.dumps(data))
        sig = _extract_json_file_signatures(str(json_file))
        assert "4 items" in sig
        assert "id" in sig
        assert "value" in sig

    def test_nested_dict(self, tmp_path):
        json_file = tmp_path / "nested.json"
        json_file.write_text(json.dumps({
            "config": {"host": "localhost", "port": 8080},
            "data": [1, 2, 3],
        }))
        sig = _extract_json_file_signatures(str(json_file))
        assert "config" in sig
        assert "host" in sig
        assert "port" in sig

    def test_list_shows_first_three_entries(self, tmp_path):
        json_file = tmp_path / "list.json"
        data = [
            {"alpha": 1}, {"bravo": 2}, {"charlie": 3},
            {"delta": 4}, {"echo": 5},
        ]
        json_file.write_text(json.dumps(data))
        sig = _extract_json_file_signatures(str(json_file))
        assert "alpha" in sig
        assert "bravo" in sig
        assert "charlie" in sig
        # 4th and 5th should not appear
        assert "delta" not in sig
        assert "echo" not in sig

    def test_invalid_json(self, tmp_path):
        json_file = tmp_path / "bad.json"
        json_file.write_text("not json {")
        assert _extract_json_file_signatures(str(json_file)) == ""

    def test_scalar_json(self, tmp_path):
        json_file = tmp_path / "scalar.json"
        json_file.write_text("42")
        sig = _extract_json_file_signatures(str(json_file))
        assert "int" in sig
        assert "42" in sig


# ---------------------------------------------------------------------------
# extract_file_signatures (main entry point)
# ---------------------------------------------------------------------------

class TestExtractFileSignatures:
    def test_mixed_file_types(self, tmp_path):
        py_file = tmp_path / "module.py"
        py_file.write_text(
            "def process(data: list) -> dict:\n"
            '    """Process the data."""\n'
            "    pass\n"
        )
        csv_file = tmp_path / "data.csv"
        with open(csv_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["x", "y", "z"])
            writer.writerow([1, 2, 3])

        sigs = extract_file_signatures([str(py_file), str(csv_file)])
        assert "<file" in sigs
        assert "def process" in sigs
        assert "columns:" in sigs
        assert "x" in sigs

    def test_missing_file_skipped(self, tmp_path):
        assert extract_file_signatures(
            [str(tmp_path / "nonexistent.py")]
        ) == ""

    def test_per_file_char_cap(self, tmp_path):
        lines = []
        for i in range(200):
            lines.append(
                f"def func_{i}(param_{i}: str) -> str:\n    pass\n"
            )
        py_file = tmp_path / "big.py"
        py_file.write_text("\n".join(lines))

        sigs = extract_file_signatures(
            [str(py_file)], max_chars_per_file=500
        )
        assert "truncated" in sigs

    def test_empty_files_list(self):
        assert extract_file_signatures([]) == ""

    def test_unsupported_extension_skipped(self, tmp_path):
        txt_file = tmp_path / "readme.txt"
        txt_file.write_text("Just a text file")
        assert extract_file_signatures([str(txt_file)]) == ""

    def test_xml_structure(self, tmp_path):
        py_file = tmp_path / "mod.py"
        py_file.write_text("X = 1\n")
        sigs = extract_file_signatures([str(py_file)])
        assert '<file path="' in sigs
        assert "</file>" in sigs


# ---------------------------------------------------------------------------
# Integration: signatures in dependency context
# ---------------------------------------------------------------------------

class TestSignaturesInDistilledOutput:
    def test_py_signatures_in_distilled_output(self, tmp_path):
        from architect.main import _distill_dependency_output

        py_file = tmp_path / "analysis.py"
        py_file.write_text(
            "def analyze(df: 'pd.DataFrame') -> dict:\n"
            '    """Run statistical analysis."""\n'
            "    pass\n"
        )
        dep_step = {
            "title": "Build analysis module",
            "summary": "Created analysis.py",
            "files_written": [str(py_file)],
            "verify": "analysis.py exists",
        }
        result = _distill_dependency_output(1, dep_step, "")
        assert "<file_signatures>" in result
        assert "def analyze" in result
        assert "pd.DataFrame" in result
        assert "</file_signatures>" in result

    def test_csv_signatures_in_distilled_output(self, tmp_path):
        from architect.main import _distill_dependency_output

        csv_file = tmp_path / "output.csv"
        with open(csv_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["record_id", "score", "duration_days"])
            writer.writerow([1, 42, 180])

        dep_step = {
            "title": "Generate data",
            "summary": "Created output.csv",
            "files_written": [str(csv_file)],
            "verify": "",
        }
        result = _distill_dependency_output(1, dep_step, "")
        assert "<file_signatures>" in result
        assert "record_id" in result
        assert "score" in result
        assert "duration_days" in result

    def test_no_signatures_when_no_files(self):
        from architect.main import _distill_dependency_output

        dep_step = {
            "title": "Setup",
            "summary": "Setup complete",
            "files_written": [],
            "verify": "",
        }
        result = _distill_dependency_output(1, dep_step, "")
        assert "file_signatures" not in result


# ---------------------------------------------------------------------------
# Integration: signatures label in spec generator
# ---------------------------------------------------------------------------

class TestSignaturesInSpec:
    def test_spec_includes_signatures_label(self, tmp_path):
        from architect.spec_generator import generate_spec

        step = {
            "id": 2,
            "title": "Use analysis",
            "description": "Use the analysis module",
            "status": "pending",
            "depends_on": [1],
            "_run_id": "",
        }
        context = (
            '<dependency step="1" title="Analysis">\n'
            "  <file_signatures>\n"
            '    <file path="analysis.py">\n'
            "      def analyze(df) -> dict\n"
            "    </file>\n"
            "  </file_signatures>\n"
            "</dependency>"
        )
        spec_path = generate_spec(
            step, 3, context=context, specs_dir=str(tmp_path)
        )
        with open(spec_path) as f:
            content = f.read()
        assert "File signatures" in content
        assert "exact function names" in content

    def test_spec_no_label_without_signatures(self, tmp_path):
        from architect.spec_generator import generate_spec

        step = {
            "id": 1,
            "title": "Setup",
            "description": "Setup project",
            "status": "pending",
            "depends_on": [],
            "_run_id": "",
        }
        spec_path = generate_spec(
            step, 1, context="some plain context",
            specs_dir=str(tmp_path),
        )
        with open(spec_path) as f:
            content = f.read()
        assert "File signatures" not in content

    def test_task_includes_signatures_instruction(self):
        from architect.spec_generator import build_task_from_spec

        step = {"description": "Use the module"}
        context = "<file_signatures>...</file_signatures>"
        task = build_task_from_spec(step, context=context)
        assert "exact" in task.lower()
        assert "do not guess" in task.lower()

    def test_task_no_instruction_without_signatures(self):
        from architect.spec_generator import build_task_from_spec

        step = {"description": "Use the module"}
        context = "plain context only"
        task = build_task_from_spec(step, context=context)
        assert "do not guess" not in task.lower()
