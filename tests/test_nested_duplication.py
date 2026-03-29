"""Tests for Section 11: Detect and prevent nested project duplication."""

import os

from architect.main import (
    detect_nested_duplication,
    resolve_nested_duplication,
    _NESTED_PROJECT_MARKERS,
    _SNAPSHOT_SKIP_DIRS,
)


class TestDetectNestedDuplication:
    """Tests for detect_nested_duplication()."""

    def test_detects_mirrored_structure(self, tmp_path):
        """A nested dir sharing project markers with root is detected."""
        (tmp_path / "src" / "app").mkdir(parents=True)
        (tmp_path / "tests").mkdir()
        (tmp_path / "README.md").write_text("# Root\n")

        nested = tmp_path / "myproject"
        (nested / "src" / "app").mkdir(parents=True)
        (nested / "tests").mkdir()
        (nested / "README.md").write_text("# Nested\n")

        result = detect_nested_duplication(str(tmp_path))
        assert result == "myproject"

    def test_no_duplication_when_root_lacks_markers(self, tmp_path):
        """Returns None when root doesn't have enough project markers."""
        (tmp_path / "src" / "app").mkdir(parents=True)

        nested = tmp_path / "myproject"
        (nested / "src").mkdir(parents=True)
        (nested / "tests").mkdir()

        assert detect_nested_duplication(str(tmp_path)) is None

    def test_no_duplication_when_nested_lacks_markers(self, tmp_path):
        """Returns None when nested dir doesn't have enough markers."""
        (tmp_path / "src").mkdir()
        (tmp_path / "tests").mkdir()

        nested = tmp_path / "myproject"
        (nested / "src").mkdir(parents=True)

        assert detect_nested_duplication(str(tmp_path)) is None

    def test_skips_snapshot_skip_dirs(self, tmp_path):
        """Directories in _SNAPSHOT_SKIP_DIRS are ignored."""
        (tmp_path / "src").mkdir()
        (tmp_path / "tests").mkdir()

        git = tmp_path / ".git"
        (git / "src").mkdir(parents=True)
        (git / "tests").mkdir()

        assert detect_nested_duplication(str(tmp_path)) is None

    def test_returns_none_for_empty_workspace(self, tmp_path):
        """Empty workspace returns None."""
        assert detect_nested_duplication(str(tmp_path)) is None

    def test_marker_dirs_themselves_not_detected(self, tmp_path):
        """Project marker directories (src, tests) are not flagged as nested."""
        (tmp_path / "src" / "tests").mkdir(parents=True)
        (tmp_path / "src" / "data").mkdir()
        (tmp_path / "tests").mkdir()
        (tmp_path / "data").mkdir()

        assert detect_nested_duplication(str(tmp_path)) is None

    def test_rehab_scenario(self, tmp_path):
        """The exact rehab-run nesting scenario is detected."""
        (tmp_path / "src" / "rehab").mkdir(parents=True)
        (tmp_path / "tests").mkdir()
        (tmp_path / "scripts").mkdir()

        rehab = tmp_path / "rehab"
        (rehab / "src" / "rehab").mkdir(parents=True)
        (rehab / "tests").mkdir()
        (rehab / "scripts").mkdir()

        result = detect_nested_duplication(str(tmp_path))
        assert result == "rehab"

    def test_returns_first_alphabetically(self, tmp_path):
        """When multiple nested dirs match, returns first alphabetically."""
        (tmp_path / "src").mkdir()
        (tmp_path / "tests").mkdir()

        for name in ("zebra", "alpha"):
            nested = tmp_path / name
            (nested / "src").mkdir(parents=True)
            (nested / "tests").mkdir()

        result = detect_nested_duplication(str(tmp_path))
        assert result == "alpha"


class TestResolveNestedDuplication:
    """Tests for resolve_nested_duplication()."""

    def test_promotes_nested_files_to_root(self, tmp_path):
        """Files from nested dir are moved to workspace root."""
        nested = tmp_path / "myproject"
        nested.mkdir()
        (nested / "README.md").write_text("# Nested\n")
        (nested / "setup.py").write_text("# setup\n")

        promoted = resolve_nested_duplication(str(tmp_path), "myproject")

        assert sorted(promoted) == ["README.md", "setup.py"]
        assert (tmp_path / "README.md").read_text() == "# Nested\n"
        assert (tmp_path / "setup.py").read_text() == "# setup\n"
        assert not nested.exists()

    def test_promotes_nested_directories(self, tmp_path):
        """Directories from nested copy are merged into root."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "old.py").write_text("# old\n")

        nested = tmp_path / "myproject"
        (nested / "src").mkdir(parents=True)
        (nested / "src" / "new.py").write_text("# new\n")

        resolve_nested_duplication(str(tmp_path), "myproject")

        assert (tmp_path / "src" / "old.py").read_text() == "# old\n"
        assert (tmp_path / "src" / "new.py").read_text() == "# new\n"
        assert not nested.exists()

    def test_nested_overwrites_root_on_conflict(self, tmp_path):
        """On file conflicts, nested version wins."""
        (tmp_path / "README.md").write_text("# Root (stale)\n")

        nested = tmp_path / "myproject"
        nested.mkdir()
        (nested / "README.md").write_text("# Nested (current)\n")

        resolve_nested_duplication(str(tmp_path), "myproject")

        assert (tmp_path / "README.md").read_text() == "# Nested (current)\n"

    def test_returns_empty_for_nonexistent(self, tmp_path):
        """Returns empty list if nested dir doesn't exist."""
        result = resolve_nested_duplication(str(tmp_path), "nonexistent")
        assert result == []

    def test_removes_nested_directory_after_promotion(self, tmp_path):
        """The nested directory is completely removed."""
        nested = tmp_path / "rehab"
        (nested / "src" / "rehab").mkdir(parents=True)
        (nested / "src" / "rehab" / "main.py").write_text("pass\n")
        (nested / "scripts").mkdir()
        (nested / "scripts" / "run.py").write_text("pass\n")

        resolve_nested_duplication(str(tmp_path), "rehab")

        assert not nested.exists()
        assert (tmp_path / "src" / "rehab" / "main.py").exists()
        assert (tmp_path / "scripts" / "run.py").exists()

    def test_full_rehab_scenario(self, tmp_path):
        """After resolution, workspace has single flat project structure."""
        (tmp_path / "src" / "rehab").mkdir(parents=True)
        (tmp_path / "tests").mkdir()
        (tmp_path / "src" / "rehab" / "stale.py").write_text("# stale\n")

        nested = tmp_path / "rehab"
        (nested / "src" / "rehab").mkdir(parents=True)
        (nested / "tests").mkdir()
        (nested / "scripts").mkdir()
        (nested / "src" / "rehab" / "main.py").write_text("# real main\n")
        (nested / "src" / "rehab" / "stale.py").write_text("# updated\n")
        (nested / "tests" / "test_main.py").write_text("pass\n")
        (nested / "scripts" / "run_dashboard.py").write_text("pass\n")

        resolve_nested_duplication(str(tmp_path), "rehab")

        assert not nested.exists()
        assert (tmp_path / "src" / "rehab" / "main.py").read_text() == "# real main\n"
        assert (tmp_path / "src" / "rehab" / "stale.py").read_text() == "# updated\n"
        assert (tmp_path / "tests" / "test_main.py").exists()
        assert (tmp_path / "scripts" / "run_dashboard.py").exists()

    def test_returns_sorted_items(self, tmp_path):
        """Promoted items are returned in sorted order."""
        nested = tmp_path / "proj"
        nested.mkdir()
        (nested / "z_file.py").write_text("")
        (nested / "a_file.py").write_text("")
        (nested / "m_file.py").write_text("")

        promoted = resolve_nested_duplication(str(tmp_path), "proj")
        assert promoted == ["a_file.py", "m_file.py", "z_file.py"]
