"""Tests for check_cross_module_imports() cross-module import validation."""

import os
import textwrap

import pytest

from architect.main import check_cross_module_imports


class TestCrossModuleImports:
    """Test that broken cross-module imports are detected."""

    def test_broken_import_detected(self, tmp_path):
        """Import of nonexistent name from a sibling module is flagged."""
        (tmp_path / "utils.py").write_text(
            textwrap.dedent("""\
                def make_card(title):
                    pass

                def create_kpi_card(value):
                    pass
            """),
            encoding="utf-8",
        )
        (tmp_path / "app.py").write_text(
            textwrap.dedent("""\
                from utils import create_card
            """),
            encoding="utf-8",
        )
        errors = check_cross_module_imports(str(tmp_path))
        assert len(errors) == 1
        assert errors[0]["imports"] == "create_card"
        assert errors[0]["from_module"] == "utils"
        assert errors[0]["severity"] == "error"
        assert "make_card" in errors[0]["description"]

    def test_valid_import_no_errors(self, tmp_path):
        """Correct imports produce no errors."""
        (tmp_path / "utils.py").write_text(
            textwrap.dedent("""\
                def make_card(title):
                    pass

                COLORS = ["red", "blue"]
            """),
            encoding="utf-8",
        )
        (tmp_path / "app.py").write_text(
            textwrap.dedent("""\
                from utils import make_card, COLORS
            """),
            encoding="utf-8",
        )
        errors = check_cross_module_imports(str(tmp_path))
        assert errors == []

    def test_relative_import_broken(self, tmp_path):
        """Relative imports with wrong names are detected."""
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "helpers.py").write_text(
            textwrap.dedent("""\
                def real_helper():
                    pass
            """),
            encoding="utf-8",
        )
        (pkg / "main.py").write_text(
            textwrap.dedent("""\
                from .helpers import wrong_helper
            """),
            encoding="utf-8",
        )
        errors = check_cross_module_imports(str(tmp_path))
        assert len(errors) == 1
        assert errors[0]["imports"] == "wrong_helper"
        assert "real_helper" in errors[0]["description"]

    def test_relative_import_valid(self, tmp_path):
        """Correct relative imports produce no errors."""
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "helpers.py").write_text(
            textwrap.dedent("""\
                def real_helper():
                    pass
            """),
            encoding="utf-8",
        )
        (pkg / "main.py").write_text(
            textwrap.dedent("""\
                from .helpers import real_helper
            """),
            encoding="utf-8",
        )
        errors = check_cross_module_imports(str(tmp_path))
        assert errors == []

    def test_nonlocal_import_ignored(self, tmp_path):
        """Imports from external packages (not in workspace) are skipped."""
        (tmp_path / "app.py").write_text(
            textwrap.dedent("""\
                from flask import Flask
                import pandas as pd
            """),
            encoding="utf-8",
        )
        errors = check_cross_module_imports(str(tmp_path))
        assert errors == []

    def test_skip_dirs(self, tmp_path):
        """Files in .uas_state/, __pycache__/, venv/ etc. are skipped."""
        state_dir = tmp_path / ".uas_state"
        state_dir.mkdir()
        (state_dir / "internal.py").write_text(
            "from nonexistent import foo\n", encoding="utf-8"
        )
        pycache = tmp_path / "__pycache__"
        pycache.mkdir()
        (pycache / "cached.py").write_text(
            "from nonexistent import bar\n", encoding="utf-8"
        )
        errors = check_cross_module_imports(str(tmp_path))
        assert errors == []

    def test_multiple_broken_imports(self, tmp_path):
        """Multiple broken names in one import statement are each reported."""
        (tmp_path / "utils.py").write_text(
            textwrap.dedent("""\
                def real_func():
                    pass
            """),
            encoding="utf-8",
        )
        (tmp_path / "app.py").write_text(
            textwrap.dedent("""\
                from utils import wrong_a, wrong_b
            """),
            encoding="utf-8",
        )
        errors = check_cross_module_imports(str(tmp_path))
        assert len(errors) == 2
        names = {e["imports"] for e in errors}
        assert names == {"wrong_a", "wrong_b"}

    def test_star_import_skipped(self, tmp_path):
        """Star imports are not flagged (can't validate them easily)."""
        (tmp_path / "utils.py").write_text(
            "def func(): pass\n", encoding="utf-8"
        )
        (tmp_path / "app.py").write_text(
            "from utils import *\n", encoding="utf-8"
        )
        errors = check_cross_module_imports(str(tmp_path))
        assert errors == []

    def test_package_submodule_import(self, tmp_path):
        """Import from a package submodule (src.module) resolves correctly."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "__init__.py").write_text("", encoding="utf-8")
        (src / "layout.py").write_text(
            textwrap.dedent("""\
                def make_card():
                    pass
            """),
            encoding="utf-8",
        )
        (tmp_path / "app.py").write_text(
            textwrap.dedent("""\
                from src.layout import make_card
            """),
            encoding="utf-8",
        )
        errors = check_cross_module_imports(str(tmp_path))
        assert errors == []

    def test_package_submodule_broken(self, tmp_path):
        """Broken import from a package submodule is detected."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "__init__.py").write_text("", encoding="utf-8")
        (src / "layout.py").write_text(
            textwrap.dedent("""\
                def make_card():
                    pass
            """),
            encoding="utf-8",
        )
        (tmp_path / "app.py").write_text(
            textwrap.dedent("""\
                from src.layout import create_card
            """),
            encoding="utf-8",
        )
        errors = check_cross_module_imports(str(tmp_path))
        assert len(errors) == 1
        assert errors[0]["imports"] == "create_card"

    def test_class_import_valid(self, tmp_path):
        """Importing a class by name works correctly."""
        (tmp_path / "models.py").write_text(
            textwrap.dedent("""\
                class DataProcessor:
                    pass
            """),
            encoding="utf-8",
        )
        (tmp_path / "app.py").write_text(
            textwrap.dedent("""\
                from models import DataProcessor
            """),
            encoding="utf-8",
        )
        errors = check_cross_module_imports(str(tmp_path))
        assert errors == []

    def test_empty_workspace(self, tmp_path):
        """Empty workspace returns no errors."""
        errors = check_cross_module_imports(str(tmp_path))
        assert errors == []

    def test_parse_error_skipped(self, tmp_path):
        """Files with syntax errors are skipped, not crash."""
        (tmp_path / "broken.py").write_text(
            "def broken(\n", encoding="utf-8"
        )
        (tmp_path / "app.py").write_text(
            "from broken import something\n", encoding="utf-8"
        )
        # broken.py can't be parsed, so its API is empty -- skip rather
        # than false-positive.
        errors = check_cross_module_imports(str(tmp_path))
        assert errors == []

    def test_error_includes_line_number(self, tmp_path):
        """Error dicts include the correct line number."""
        (tmp_path / "utils.py").write_text(
            "def func(): pass\n", encoding="utf-8"
        )
        (tmp_path / "app.py").write_text(
            textwrap.dedent("""\
                import os
                import sys
                from utils import wrong_name
            """),
            encoding="utf-8",
        )
        errors = check_cross_module_imports(str(tmp_path))
        assert len(errors) == 1
        assert errors[0]["line"] == 3

    def test_module_level_alias_valid(self, tmp_path):
        """Importing a module-level alias (e.g., generate_dataset = simulate) works."""
        (tmp_path / "core.py").write_text(
            textwrap.dedent("""\
                def simulate():
                    pass

                generate_dataset = simulate
            """),
            encoding="utf-8",
        )
        (tmp_path / "app.py").write_text(
            textwrap.dedent("""\
                from core import generate_dataset
            """),
            encoding="utf-8",
        )
        errors = check_cross_module_imports(str(tmp_path))
        assert errors == []

    def test_module_level_variable_valid(self, tmp_path):
        """Importing a non-uppercase module-level variable works."""
        (tmp_path / "config.py").write_text(
            textwrap.dedent("""\
                default_seed = 42
                app_name: str = "my_app"
            """),
            encoding="utf-8",
        )
        (tmp_path / "app.py").write_text(
            textwrap.dedent("""\
                from config import default_seed, app_name
            """),
            encoding="utf-8",
        )
        errors = check_cross_module_imports(str(tmp_path))
        assert errors == []

    def test_private_variable_not_exposed(self, tmp_path):
        """Underscore-prefixed module-level variables are not part of public API."""
        (tmp_path / "core.py").write_text(
            textwrap.dedent("""\
                def public_func():
                    pass

                _internal = 42
            """),
            encoding="utf-8",
        )
        (tmp_path / "app.py").write_text(
            textwrap.dedent("""\
                from core import _internal
            """),
            encoding="utf-8",
        )
        errors = check_cross_module_imports(str(tmp_path))
        assert len(errors) == 1
        assert errors[0]["imports"] == "_internal"
