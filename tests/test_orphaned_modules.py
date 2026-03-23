"""Tests for detect_orphaned_modules() orphaned module detection."""

import os
import textwrap

import pytest

from architect.main import detect_orphaned_modules


class TestDetectOrphanedModules:
    """Test that orphaned (never-imported) modules are detected."""

    def test_all_modules_imported_no_orphans(self, tmp_path):
        """When every module is imported by another, no orphans are reported."""
        (tmp_path / "app.py").write_text(
            textwrap.dedent("""\
                from utils import helper
                if __name__ == "__main__":
                    helper()
            """),
            encoding="utf-8",
        )
        (tmp_path / "utils.py").write_text(
            textwrap.dedent("""\
                def helper():
                    pass
            """),
            encoding="utf-8",
        )
        orphaned = detect_orphaned_modules(str(tmp_path))
        assert orphaned == []

    def test_orphan_detected(self, tmp_path):
        """A module not imported by any other file is flagged as orphaned."""
        (tmp_path / "app.py").write_text(
            textwrap.dedent("""\
                if __name__ == "__main__":
                    print("hello")
            """),
            encoding="utf-8",
        )
        (tmp_path / "utils.py").write_text(
            textwrap.dedent("""\
                def helper():
                    pass
            """),
            encoding="utf-8",
        )
        (tmp_path / "orphan.py").write_text(
            textwrap.dedent("""\
                def unused():
                    pass
            """),
            encoding="utf-8",
        )
        orphaned = detect_orphaned_modules(str(tmp_path))
        assert "orphan.py" in orphaned
        # utils is also not imported, so it should appear too
        assert "utils.py" in orphaned

    def test_init_py_excluded(self, tmp_path):
        """__init__.py files are never reported as orphaned."""
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "mod.py").write_text("def func(): pass\n", encoding="utf-8")
        (tmp_path / "app.py").write_text(
            textwrap.dedent("""\
                if __name__ == "__main__":
                    pass
            """),
            encoding="utf-8",
        )
        orphaned = detect_orphaned_modules(str(tmp_path))
        assert not any("__init__.py" in o for o in orphaned)

    def test_entry_point_excluded(self, tmp_path):
        """Entry-point files (app.py, main.py, etc.) are not orphan candidates."""
        (tmp_path / "app.py").write_text("print('run')\n", encoding="utf-8")
        (tmp_path / "main.py").write_text("print('run')\n", encoding="utf-8")
        orphaned = detect_orphaned_modules(str(tmp_path))
        assert "app.py" not in orphaned
        assert "main.py" not in orphaned

    def test_test_files_excluded(self, tmp_path):
        """Test files (test_*.py, *_test.py) are excluded from orphan detection."""
        (tmp_path / "app.py").write_text(
            "if __name__ == '__main__': pass\n", encoding="utf-8"
        )
        (tmp_path / "test_utils.py").write_text(
            "def test_something(): pass\n", encoding="utf-8"
        )
        (tmp_path / "integration_test.py").write_text(
            "def test_it(): pass\n", encoding="utf-8"
        )
        orphaned = detect_orphaned_modules(str(tmp_path))
        assert "test_utils.py" not in orphaned
        assert "integration_test.py" not in orphaned

    def test_conftest_excluded(self, tmp_path):
        """conftest.py is excluded from orphan detection."""
        (tmp_path / "app.py").write_text(
            "if __name__ == '__main__': pass\n", encoding="utf-8"
        )
        (tmp_path / "conftest.py").write_text(
            "import pytest\n", encoding="utf-8"
        )
        orphaned = detect_orphaned_modules(str(tmp_path))
        assert "conftest.py" not in orphaned

    def test_subpackage_import_detected(self, tmp_path):
        """Import via package path (from src.utils import ...) counts."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "__init__.py").write_text("", encoding="utf-8")
        (src / "utils.py").write_text("def helper(): pass\n", encoding="utf-8")
        (tmp_path / "app.py").write_text(
            textwrap.dedent("""\
                from src.utils import helper
                if __name__ == "__main__":
                    helper()
            """),
            encoding="utf-8",
        )
        orphaned = detect_orphaned_modules(str(tmp_path))
        assert orphaned == []

    def test_import_statement_detected(self, tmp_path):
        """Plain 'import utils' counts as a reference."""
        (tmp_path / "app.py").write_text(
            textwrap.dedent("""\
                import utils
                if __name__ == "__main__":
                    utils.helper()
            """),
            encoding="utf-8",
        )
        (tmp_path / "utils.py").write_text("def helper(): pass\n", encoding="utf-8")
        orphaned = detect_orphaned_modules(str(tmp_path))
        assert orphaned == []

    def test_skip_dirs(self, tmp_path):
        """Files in .state/, __pycache__/, venv/ etc. are ignored."""
        state_dir = tmp_path / ".state"
        state_dir.mkdir()
        (state_dir / "internal.py").write_text("x = 1\n", encoding="utf-8")
        pycache = tmp_path / "__pycache__"
        pycache.mkdir()
        (pycache / "cached.py").write_text("x = 1\n", encoding="utf-8")
        orphaned = detect_orphaned_modules(str(tmp_path))
        assert orphaned == []

    def test_empty_workspace(self, tmp_path):
        """Empty workspace returns no orphans."""
        orphaned = detect_orphaned_modules(str(tmp_path))
        assert orphaned == []

    def test_main_guard_entry_point_excluded(self, tmp_path):
        """A file with __name__ == '__main__' guard is treated as entry point."""
        (tmp_path / "run_pipeline.py").write_text(
            textwrap.dedent("""\
                def main():
                    pass
                if __name__ == "__main__":
                    main()
            """),
            encoding="utf-8",
        )
        (tmp_path / "helpers.py").write_text(
            "def help_func(): pass\n", encoding="utf-8"
        )
        orphaned = detect_orphaned_modules(str(tmp_path))
        assert "run_pipeline.py" not in orphaned
        # helpers.py IS orphaned since run_pipeline.py doesn't import it
        assert "helpers.py" in orphaned

    def test_relative_import_counts(self, tmp_path):
        """Relative imports prevent a module from being flagged as orphaned."""
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "core.py").write_text(
            "from .helpers import do_thing\n", encoding="utf-8"
        )
        (pkg / "helpers.py").write_text(
            "def do_thing(): pass\n", encoding="utf-8"
        )
        (tmp_path / "app.py").write_text(
            textwrap.dedent("""\
                from pkg.core import do_thing
                if __name__ == "__main__":
                    do_thing()
            """),
            encoding="utf-8",
        )
        orphaned = detect_orphaned_modules(str(tmp_path))
        # Both pkg/core.py and pkg/helpers.py should be imported
        assert orphaned == []

    def test_setup_py_excluded(self, tmp_path):
        """setup.py is excluded from orphan detection."""
        (tmp_path / "setup.py").write_text(
            "from setuptools import setup; setup()\n", encoding="utf-8"
        )
        (tmp_path / "mymod.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "app.py").write_text(
            textwrap.dedent("""\
                import mymod
                if __name__ == "__main__":
                    pass
            """),
            encoding="utf-8",
        )
        orphaned = detect_orphaned_modules(str(tmp_path))
        assert "setup.py" not in orphaned
