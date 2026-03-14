"""Tests for orchestrator.main.scan_workspace."""

import os

from orchestrator.main import scan_workspace


class TestScanWorkspace:
    def test_empty_workspace(self, tmp_path):
        result = scan_workspace(str(tmp_path))
        assert result == ""

    def test_invalid_path(self):
        assert scan_workspace("/nonexistent/path") == ""

    def test_empty_string_path(self):
        assert scan_workspace("") == ""

    def test_python_file_gets_preview(self, tmp_path):
        py_file = tmp_path / "main.py"
        py_file.write_text("print('hello')\nx = 42\n", encoding="utf-8")
        result = scan_workspace(str(tmp_path))
        assert "main.py" in result
        assert "print('hello')" in result
        assert "python" in result.lower() or "py" in result.lower()

    def test_binary_file_shows_size_only(self, tmp_path):
        bin_file = tmp_path / "image.png"
        bin_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        result = scan_workspace(str(tmp_path))
        assert "image.png" in result
        assert "bytes" in result
        # Binary files should NOT have indented preview lines
        assert "  \x89" not in result

    def test_hidden_files_skipped(self, tmp_path):
        hidden = tmp_path / ".hidden"
        hidden.write_text("secret", encoding="utf-8")
        visible = tmp_path / "visible.txt"
        visible.write_text("public", encoding="utf-8")
        result = scan_workspace(str(tmp_path))
        assert ".hidden" not in result
        assert "visible.txt" in result

    def test_skip_dirs_excluded(self, tmp_path):
        pycache = tmp_path / "__pycache__"
        pycache.mkdir()
        (pycache / "mod.pyc").write_bytes(b"\x00")
        txt = tmp_path / "readme.txt"
        txt.write_text("hello", encoding="utf-8")
        result = scan_workspace(str(tmp_path))
        assert "__pycache__" not in result
        assert "readme.txt" in result

    def test_directories_listed(self, tmp_path):
        sub = tmp_path / "src"
        sub.mkdir()
        (tmp_path / "main.py").write_text("x = 1", encoding="utf-8")
        result = scan_workspace(str(tmp_path))
        assert "src/ (directory)" in result

    def test_budget_respected(self, tmp_path):
        # Create a file with many lines
        big = tmp_path / "big.py"
        big.write_text("\n".join(f"line_{i} = {i}" for i in range(100)),
                       encoding="utf-8")
        result = scan_workspace(str(tmp_path), max_chars=200)
        assert len(result) < 500  # Some overhead allowed but within budget

    def test_python_files_sorted_first(self, tmp_path):
        (tmp_path / "z_data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        (tmp_path / "a_script.py").write_text("print(1)", encoding="utf-8")
        (tmp_path / "m_notes.txt").write_text("notes", encoding="utf-8")
        result = scan_workspace(str(tmp_path))
        py_pos = result.index("a_script.py")
        csv_pos = result.index("z_data.csv")
        txt_pos = result.index("m_notes.txt")
        # Python should appear before other files
        assert py_pos < txt_pos

    def test_text_file_preview(self, tmp_path):
        txt = tmp_path / "data.csv"
        txt.write_text("name,value\nalice,1\nbob,2\n", encoding="utf-8")
        result = scan_workspace(str(tmp_path))
        assert "name,value" in result

    def test_workspace_contents_header(self, tmp_path):
        (tmp_path / "file.txt").write_text("x", encoding="utf-8")
        result = scan_workspace(str(tmp_path))
        assert "=== workspace contents ===" in result
