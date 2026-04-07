"""Tests for uas.janitor: post-edit formatting and Pyflakes-only linting.

Covers ``format_workspace`` and ``lint_workspace`` across the configured
formatter values (``"ruff"``, ``"black"``, ``"none"``), the no-tool
fallback paths, and the integration end-to-end check that runs the real
``ruff format`` binary on intentionally messy code (skipped if ruff is
not installed).
"""

import os
import shutil
import subprocess
from unittest.mock import patch

import pytest

from uas.janitor import _find_formatter, format_workspace, lint_workspace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MESSY_CODE = (
    "import os,sys\n"
    "import json\n"
    "def  foo( x,y ):\n"
    "    z=x+y\n"
    "    return  z\n"
    "\n"
    "\n"
    "\n"
    "class   Bar :\n"
    "    def baz(self,a,b,c) :\n"
    "        return [a ,b,  c]\n"
)


def _write(workspace, name, contents):
    """Create *name* under *workspace* and return its absolute path."""
    path = os.path.join(str(workspace), name)
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(name) else None
    with open(path, "w", encoding="utf-8") as f:
        f.write(contents)
    return path


def _ruff_available() -> bool:
    return shutil.which("ruff") is not None


# ---------------------------------------------------------------------------
# _find_formatter
# ---------------------------------------------------------------------------


class TestFindFormatter:
    """Tests for the formatter resolution logic."""

    @patch("uas.janitor.shutil.which")
    @patch("uas.janitor.config.get")
    def test_default_prefers_ruff(self, mock_cfg, mock_which):
        mock_cfg.return_value = "ruff"
        mock_which.side_effect = lambda tool: (
            "/usr/bin/ruff" if tool == "ruff" else None
        )
        assert _find_formatter() == "ruff"

    @patch("uas.janitor.shutil.which")
    @patch("uas.janitor.config.get")
    def test_default_falls_back_to_black(self, mock_cfg, mock_which):
        mock_cfg.return_value = "ruff"
        mock_which.side_effect = lambda tool: (
            "/usr/bin/black" if tool == "black" else None
        )
        assert _find_formatter() == "black"

    @patch("uas.janitor.shutil.which")
    @patch("uas.janitor.config.get")
    def test_default_no_tools_returns_none(self, mock_cfg, mock_which):
        mock_cfg.return_value = "ruff"
        mock_which.return_value = None
        assert _find_formatter() is None

    @patch("uas.janitor.shutil.which")
    @patch("uas.janitor.config.get")
    def test_explicit_black(self, mock_cfg, mock_which):
        mock_cfg.return_value = "black"
        mock_which.side_effect = lambda tool: (
            "/usr/bin/black" if tool == "black" else None
        )
        assert _find_formatter() == "black"

    @patch("uas.janitor.shutil.which")
    @patch("uas.janitor.config.get")
    def test_explicit_black_no_fallback_to_ruff(self, mock_cfg, mock_which):
        mock_cfg.return_value = "black"
        mock_which.side_effect = lambda tool: (
            "/usr/bin/ruff" if tool == "ruff" else None
        )
        assert _find_formatter() is None

    @patch("uas.janitor.config.get")
    def test_none_disables_formatter(self, mock_cfg):
        mock_cfg.return_value = "none"
        assert _find_formatter() is None

    @patch("uas.janitor.shutil.which")
    @patch("uas.janitor.config.get")
    def test_config_value_is_case_insensitive(self, mock_cfg, mock_which):
        mock_cfg.return_value = "RUFF"
        mock_which.side_effect = lambda tool: (
            "/usr/bin/ruff" if tool == "ruff" else None
        )
        assert _find_formatter() == "ruff"


# ---------------------------------------------------------------------------
# format_workspace - mocked subprocess
# ---------------------------------------------------------------------------


class TestFormatWorkspaceMocked:
    """Mocked-subprocess tests for format_workspace dispatch logic."""

    @patch("uas.janitor.subprocess.run")
    @patch("uas.janitor._find_formatter")
    def test_calls_ruff_with_explicit_files(self, mock_find, mock_run, tmp_path):
        mock_find.return_value = "ruff"
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        _write(tmp_path, "a.py", MESSY_CODE)

        format_workspace(str(tmp_path), files=["a.py"])

        assert mock_run.call_count == 1
        cmd = mock_run.call_args.args[0]
        assert cmd[:4] == ["ruff", "format", "--quiet", "--"]
        assert "a.py" in cmd
        assert mock_run.call_args.kwargs["cwd"] == str(tmp_path)

    @patch("uas.janitor.subprocess.run")
    @patch("uas.janitor._find_formatter")
    def test_calls_black_when_selected(self, mock_find, mock_run, tmp_path):
        mock_find.return_value = "black"
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        _write(tmp_path, "a.py", MESSY_CODE)

        format_workspace(str(tmp_path), files=["a.py"])

        cmd = mock_run.call_args.args[0]
        assert cmd[:3] == ["black", "--quiet", "--"]
        assert "a.py" in cmd

    @patch("uas.janitor.subprocess.run")
    @patch("uas.janitor._find_formatter")
    def test_no_formatter_is_noop(self, mock_find, mock_run, tmp_path):
        mock_find.return_value = None
        _write(tmp_path, "a.py", MESSY_CODE)

        format_workspace(str(tmp_path), files=["a.py"])

        mock_run.assert_not_called()

    @patch("uas.janitor.subprocess.run")
    @patch("uas.janitor._find_formatter")
    def test_empty_files_list_skips_subprocess(self, mock_find, mock_run, tmp_path):
        mock_find.return_value = "ruff"
        format_workspace(str(tmp_path), files=[])
        mock_run.assert_not_called()

    @patch("uas.janitor.subprocess.run")
    @patch("uas.janitor._find_formatter")
    def test_discovers_files_when_none_passed(self, mock_find, mock_run, tmp_path):
        mock_find.return_value = "ruff"
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        _write(tmp_path, "x.py", MESSY_CODE)
        _write(tmp_path, "y.py", MESSY_CODE)

        format_workspace(str(tmp_path))

        assert mock_run.call_count == 1
        cmd = mock_run.call_args.args[0]
        assert "x.py" in cmd
        assert "y.py" in cmd

    @patch("uas.janitor.subprocess.run")
    @patch("uas.janitor._find_formatter")
    def test_no_python_files_skips_subprocess(self, mock_find, mock_run, tmp_path):
        mock_find.return_value = "ruff"
        _write(tmp_path, "readme.txt", "hello")
        format_workspace(str(tmp_path))
        mock_run.assert_not_called()

    @patch("uas.janitor.subprocess.run")
    @patch("uas.janitor._find_formatter")
    def test_nonzero_exit_logs_warning_no_raise(
        self, mock_find, mock_run, tmp_path, caplog
    ):
        mock_find.return_value = "ruff"
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=2, stdout="", stderr="boom"
        )
        _write(tmp_path, "a.py", MESSY_CODE)

        with caplog.at_level("WARNING"):
            format_workspace(str(tmp_path), files=["a.py"])

        assert any(
            "ruff" in rec.message and "boom" in rec.message for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# lint_workspace - mocked subprocess
# ---------------------------------------------------------------------------


class TestLintWorkspaceMocked:
    """Mocked-subprocess tests for lint_workspace logic."""

    @patch("uas.janitor.subprocess.run")
    @patch("uas.janitor.shutil.which")
    def test_returns_empty_when_ruff_missing(self, mock_which, mock_run, tmp_path):
        mock_which.return_value = None
        _write(tmp_path, "a.py", MESSY_CODE)
        assert lint_workspace(str(tmp_path), files=["a.py"]) == []
        mock_run.assert_not_called()

    @patch("uas.janitor.subprocess.run")
    @patch("uas.janitor.shutil.which")
    def test_returns_empty_on_clean_exit(self, mock_which, mock_run, tmp_path):
        mock_which.return_value = "/usr/bin/ruff"
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        _write(tmp_path, "a.py", "x = 1\n")
        assert lint_workspace(str(tmp_path), files=["a.py"]) == []

    @patch("uas.janitor.subprocess.run")
    @patch("uas.janitor.shutil.which")
    def test_returns_error_lines_on_nonzero_exit(self, mock_which, mock_run, tmp_path):
        mock_which.return_value = "/usr/bin/ruff"
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="a.py:1:1: F821 undefined name 'foo'\n\na.py:2:1: F401 unused import\n",
            stderr="",
        )
        _write(tmp_path, "a.py", "foo()\n")

        errors = lint_workspace(str(tmp_path), files=["a.py"])

        assert len(errors) == 2
        assert any("F821" in line for line in errors)
        assert any("F401" in line for line in errors)

    @patch("uas.janitor.subprocess.run")
    @patch("uas.janitor.shutil.which")
    def test_uses_correct_ruff_arguments(self, mock_which, mock_run, tmp_path):
        mock_which.return_value = "/usr/bin/ruff"
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        _write(tmp_path, "a.py", "x = 1\n")

        lint_workspace(str(tmp_path), files=["a.py"])

        cmd = mock_run.call_args.args[0]
        assert cmd[:5] == ["ruff", "check", "--select=F", "--no-fix", "--quiet"]
        assert "a.py" in cmd
        assert mock_run.call_args.kwargs["cwd"] == str(tmp_path)

    @patch("uas.janitor.subprocess.run")
    @patch("uas.janitor.shutil.which")
    def test_empty_workspace_skips_subprocess(self, mock_which, mock_run, tmp_path):
        mock_which.return_value = "/usr/bin/ruff"
        assert lint_workspace(str(tmp_path)) == []
        mock_run.assert_not_called()

    @patch("uas.janitor.subprocess.run")
    @patch("uas.janitor.shutil.which")
    def test_discovers_files_when_none_passed(self, mock_which, mock_run, tmp_path):
        mock_which.return_value = "/usr/bin/ruff"
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        _write(tmp_path, "p.py", "y = 2\n")

        lint_workspace(str(tmp_path))

        cmd = mock_run.call_args.args[0]
        assert "p.py" in cmd


# ---------------------------------------------------------------------------
# Real ruff integration: messy code in -> ruff-format-compliant code out
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _ruff_available(), reason="ruff binary not installed")
class TestFormatWorkspaceRealRuff:
    """End-to-end check that the janitor produces ruff-compliant output."""

    @patch("uas.janitor.config.get")
    def test_messy_file_becomes_ruff_compliant(self, mock_cfg, tmp_path):
        mock_cfg.return_value = "ruff"
        path = _write(tmp_path, "messy.py", MESSY_CODE)

        # Sanity: pristine ruff should disagree with the input.
        pre = subprocess.run(
            ["ruff", "format", "--check", "--quiet", "--", "messy.py"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
        )
        assert pre.returncode != 0, "MESSY_CODE was already ruff-compliant"

        format_workspace(str(tmp_path), files=["messy.py"])

        post = subprocess.run(
            ["ruff", "format", "--check", "--quiet", "--", "messy.py"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
        )
        assert post.returncode == 0, (
            "format_workspace did not produce ruff-compliant output:\n"
            + post.stdout
            + post.stderr
        )

        with open(path, "r", encoding="utf-8") as f:
            formatted = f.read()
        # Spot-check a few normalisations ruff applies.
        assert "def foo(x, y):" in formatted
        assert "z = x + y" in formatted
        # Original had three blank lines between functions; ruff collapses to two.
        assert "\n\n\n\nclass" not in formatted

    @patch("uas.janitor.config.get")
    def test_discover_all_python_files(self, mock_cfg, tmp_path):
        mock_cfg.return_value = "ruff"
        _write(tmp_path, "a.py", MESSY_CODE)
        os.makedirs(os.path.join(str(tmp_path), "pkg"))
        _write(tmp_path, "pkg/b.py", MESSY_CODE)

        format_workspace(str(tmp_path))

        check = subprocess.run(
            ["ruff", "format", "--check", "--quiet", "."],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
        )
        assert check.returncode == 0

    @patch("uas.janitor.config.get")
    def test_lint_detects_undefined_name(self, mock_cfg, tmp_path):
        mock_cfg.return_value = "ruff"
        _write(tmp_path, "broken.py", "result = undefined_symbol()\n")

        errors = lint_workspace(str(tmp_path), files=["broken.py"])

        assert errors, "expected lint errors for undefined name"
        assert any("F821" in line or "undefined" in line.lower() for line in errors)

    @patch("uas.janitor.config.get")
    def test_lint_clean_file_returns_empty(self, mock_cfg, tmp_path):
        mock_cfg.return_value = "ruff"
        _write(tmp_path, "ok.py", "x = 1\nprint(x)\n")
        assert lint_workspace(str(tmp_path), files=["ok.py"]) == []

    @patch("uas.janitor.config.get")
    def test_lint_files_filter_ignores_unrelated_files(self, mock_cfg, tmp_path):
        """Section 5 of PLAN.md: when ``files=[a.py]`` is passed,
        ``lint_workspace`` must only inspect ``a.py``. Errors in unrelated
        files (e.g. ``b.py`` left over from a prior run) must NOT appear
        in the returned error list, otherwise the orchestrator's lint
        pre-check would blame the current attempt for damage it did not
        cause and re-poison every rollback forever.
        """
        mock_cfg.return_value = "ruff"
        # a.py is clean.  b.py has a fatal F401 unused-import error that
        # mirrors the rehab/tests/test_config.py reproduction in Section 5.
        _write(tmp_path, "a.py", "x = 1\nprint(x)\n")
        _write(tmp_path, "b.py", "import os\nimport pytest\n")

        # Sanity: the unfiltered call sees the b.py errors. Ruff's
        # grouped output puts the error code on one line and the file
        # path on a separate ``-->`` line, so we verify both appear
        # somewhere in the returned list rather than on the same line.
        all_errors = lint_workspace(str(tmp_path))
        all_text = "\n".join(all_errors)
        assert "F401" in all_text and "b.py" in all_text, (
            f"sanity check failed: expected b.py F401 in {all_errors!r}"
        )

        # The fix: passing files=[a.py] must produce zero errors because
        # b.py is not in the inspected file list.
        scoped_errors = lint_workspace(str(tmp_path), files=["a.py"])
        assert scoped_errors == [], (
            f"lint_workspace(files=[a.py]) leaked b.py errors: {scoped_errors!r}"
        )
