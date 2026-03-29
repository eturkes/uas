"""Tests for Section 12: Project structure manifest with stale file detection."""

import json
import os
from unittest.mock import patch, MagicMock

from architect.main import (
    ProjectManifest,
    remove_superseded_files,
    confirm_supersession_llm,
    confirm_dir_supersession_llm,
    _remove_empty_dirs,
)


class TestProjectManifest:
    """Tests for the ProjectManifest class."""

    def test_add_step_output(self):
        """Files are recorded with their originating step ID."""
        m = ProjectManifest()
        m.add_step_output(1, ["src/rehab/cleaner.py", "src/rehab/loader.py"])
        assert m.files == {
            "src/rehab/cleaner.py": 1,
            "src/rehab/loader.py": 1,
        }

    def test_add_multiple_steps(self):
        """Multiple steps can register files; later steps overwrite."""
        m = ProjectManifest()
        m.add_step_output(1, ["src/utils.py"])
        m.add_step_output(2, ["src/main.py"])
        m.add_step_output(3, ["src/utils.py"])  # overwrites step 1
        assert m.files["src/utils.py"] == 3
        assert m.files["src/main.py"] == 2

    def test_remove(self):
        """Removing a file from the manifest works."""
        m = ProjectManifest()
        m.add_step_output(1, ["src/old.py", "src/keep.py"])
        m.remove("src/old.py")
        assert "src/old.py" not in m.files
        assert "src/keep.py" in m.files

    def test_remove_nonexistent(self):
        """Removing a file not in the manifest is a no-op."""
        m = ProjectManifest()
        m.remove("nothing.py")  # should not raise

    def test_to_dict_and_from_dict(self):
        """Manifest round-trips through dict serialisation."""
        m = ProjectManifest()
        m.add_step_output(1, ["a.py", "b.py"])
        m.add_step_output(2, ["c.py"])
        d = m.to_dict()
        m2 = ProjectManifest.from_dict(d)
        assert m2.files == m.files

    def test_from_dict_empty(self):
        """Empty dict produces empty manifest."""
        m = ProjectManifest.from_dict({})
        assert m.files == {}


class TestDetectSuperseded:
    """Tests for ProjectManifest.detect_superseded()."""

    def test_same_basename_different_path(self):
        """cleaner.py at root is flagged when data/cleaner.py is created."""
        m = ProjectManifest()
        m.add_step_output(1, ["src/rehab/cleaner.py"])
        superseded = m.detect_superseded(["src/rehab/data/cleaner.py"])
        assert "src/rehab/cleaner.py" in superseded

    def test_no_supersession_for_same_path(self):
        """Updating the same file is not supersession."""
        m = ProjectManifest()
        m.add_step_output(1, ["src/rehab/cleaner.py"])
        superseded = m.detect_superseded(["src/rehab/cleaner.py"])
        assert superseded == []

    def test_no_supersession_for_different_names(self):
        """Files with different basenames are not flagged."""
        m = ProjectManifest()
        m.add_step_output(1, ["src/rehab/cleaner.py"])
        superseded = m.detect_superseded(["src/rehab/data/loader.py"])
        assert superseded == []

    def test_ignores_init_files(self):
        """__init__.py files are never considered superseded."""
        m = ProjectManifest()
        m.add_step_output(1, ["src/rehab/__init__.py"])
        superseded = m.detect_superseded(["src/rehab/data/__init__.py"])
        assert superseded == []

    def test_multiple_supersessions(self):
        """Multiple files can be superseded at once."""
        m = ProjectManifest()
        m.add_step_output(1, ["src/rehab/tabs/overview.py",
                               "src/rehab/tabs/detail.py"])
        superseded = m.detect_superseded([
            "src/rehab/dashboard/overview.py",
            "src/rehab/dashboard/detail.py",
        ])
        assert sorted(superseded) == [
            "src/rehab/tabs/detail.py",
            "src/rehab/tabs/overview.py",
        ]

    def test_returns_sorted(self):
        """Superseded files are returned in sorted order."""
        m = ProjectManifest()
        m.add_step_output(1, ["z/mod.py", "a/mod.py"])
        superseded = m.detect_superseded(["new/mod.py"])
        # Only one match — both share basename "mod.py" but detect_superseded
        # maps basename to one new file; both old files match.
        assert superseded == sorted(superseded)


class TestDetectSupersededDirs:
    """Tests for ProjectManifest.detect_superseded_dirs()."""

    def test_tabs_to_dashboard(self):
        """tabs/ directory is flagged when dashboard/ has overlapping modules."""
        m = ProjectManifest()
        m.add_step_output(1, [
            "src/rehab/tabs/overview.py",
            "src/rehab/tabs/detail.py",
            "src/rehab/tabs/simulator.py",
        ])
        pairs = m.detect_superseded_dirs([
            "src/rehab/dashboard/overview.py",
            "src/rehab/dashboard/detail.py",
            "src/rehab/dashboard/simulator.py",
        ])
        assert len(pairs) == 1
        assert pairs[0] == ("src/rehab/tabs", "src/rehab/dashboard")

    def test_no_overlap_no_flag(self):
        """Directories with no overlapping module names are not flagged."""
        m = ProjectManifest()
        m.add_step_output(1, ["src/tabs/overview.py", "src/tabs/detail.py"])
        pairs = m.detect_superseded_dirs([
            "src/dashboard/charts.py",
            "src/dashboard/widgets.py",
        ])
        assert pairs == []

    def test_single_overlap_not_enough(self):
        """A single overlapping module name is not sufficient."""
        m = ProjectManifest()
        m.add_step_output(1, [
            "src/tabs/overview.py",
            "src/tabs/unique_old.py",
        ])
        pairs = m.detect_superseded_dirs([
            "src/dashboard/overview.py",
            "src/dashboard/unique_new.py",
        ])
        assert pairs == []

    def test_ignores_init_in_overlap(self):
        """__init__.py does not count toward overlap."""
        m = ProjectManifest()
        m.add_step_output(1, [
            "src/tabs/__init__.py",
            "src/tabs/view.py",
        ])
        pairs = m.detect_superseded_dirs([
            "src/dashboard/__init__.py",
            "src/dashboard/view.py",
        ])
        # Only 1 overlapping module (view), need >= 2
        assert pairs == []

    def test_same_directory_not_flagged(self):
        """Adding files to the same directory is never supersession."""
        m = ProjectManifest()
        m.add_step_output(1, ["src/tabs/a.py", "src/tabs/b.py"])
        pairs = m.detect_superseded_dirs(["src/tabs/a.py", "src/tabs/c.py"])
        assert pairs == []


class TestRemoveSupersededFiles:
    """Tests for remove_superseded_files()."""

    def test_removes_superseded_file_no_llm(self, tmp_path):
        """Superseded file is removed from disk and manifest when LLM is off."""
        old = tmp_path / "src" / "rehab"
        old.mkdir(parents=True)
        (old / "cleaner.py").write_text("# old cleaner\n")

        new_dir = tmp_path / "src" / "rehab" / "data"
        new_dir.mkdir(parents=True)
        (new_dir / "cleaner.py").write_text("# new cleaner\n")

        m = ProjectManifest()
        m.add_step_output(1, ["src/rehab/cleaner.py"])

        removed = remove_superseded_files(
            str(tmp_path), m, 2,
            ["src/rehab/data/cleaner.py"],
            use_llm=False,
        )

        assert "src/rehab/cleaner.py" in removed
        assert not (old / "cleaner.py").exists()
        assert "src/rehab/cleaner.py" not in m.files

    def test_preserves_when_not_superseded(self, tmp_path):
        """Files with different basenames are not removed."""
        (tmp_path / "loader.py").write_text("# loader\n")

        m = ProjectManifest()
        m.add_step_output(1, ["loader.py"])

        removed = remove_superseded_files(
            str(tmp_path), m, 2, ["processor.py"], use_llm=False,
        )

        assert removed == []
        assert (tmp_path / "loader.py").exists()

    @patch("architect.main.confirm_supersession_llm", return_value=True)
    @patch("architect.main.MINIMAL_MODE", False)
    def test_uses_llm_confirmation(self, mock_confirm, tmp_path):
        """When use_llm=True, the LLM confirmation function is called."""
        old = tmp_path / "src"
        old.mkdir()
        (old / "cleaner.py").write_text("# old\n")

        new = tmp_path / "data"
        new.mkdir()
        (new / "cleaner.py").write_text("# new\n")

        m = ProjectManifest()
        m.add_step_output(1, ["src/cleaner.py"])

        removed = remove_superseded_files(
            str(tmp_path), m, 2, ["data/cleaner.py"], use_llm=True,
        )

        mock_confirm.assert_called_once()
        assert "src/cleaner.py" in removed

    @patch("architect.main.confirm_supersession_llm", return_value=False)
    @patch("architect.main.MINIMAL_MODE", False)
    def test_llm_rejection_preserves_file(self, mock_confirm, tmp_path):
        """When LLM says not superseded, the file is preserved."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "cleaner.py").write_text("# keep\n")
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "cleaner.py").write_text("# new\n")

        m = ProjectManifest()
        m.add_step_output(1, ["src/cleaner.py"])

        removed = remove_superseded_files(
            str(tmp_path), m, 2, ["data/cleaner.py"], use_llm=True,
        )

        assert removed == []
        assert (tmp_path / "src" / "cleaner.py").exists()
        assert "src/cleaner.py" in m.files

    def test_directory_supersession_no_llm(self, tmp_path):
        """Entire directory is removed when superseded (LLM off)."""
        tabs = tmp_path / "src" / "rehab" / "tabs"
        tabs.mkdir(parents=True)
        (tabs / "overview.py").write_text("# old\n")
        (tabs / "detail.py").write_text("# old\n")
        (tabs / "simulator.py").write_text("# old\n")

        dash = tmp_path / "src" / "rehab" / "dashboard"
        dash.mkdir(parents=True)
        (dash / "overview.py").write_text("# new\n")
        (dash / "detail.py").write_text("# new\n")
        (dash / "simulator.py").write_text("# new\n")

        m = ProjectManifest()
        m.add_step_output(3, [
            "src/rehab/tabs/overview.py",
            "src/rehab/tabs/detail.py",
            "src/rehab/tabs/simulator.py",
        ])

        removed = remove_superseded_files(
            str(tmp_path), m, 9, [
                "src/rehab/dashboard/overview.py",
                "src/rehab/dashboard/detail.py",
                "src/rehab/dashboard/simulator.py",
            ],
            use_llm=False,
        )

        # All three old files should be removed
        assert "src/rehab/tabs/overview.py" in removed
        assert "src/rehab/tabs/detail.py" in removed
        assert "src/rehab/tabs/simulator.py" in removed
        assert not tabs.exists()  # empty dir cleaned up

    def test_manifest_tracks_step_after_removal(self, tmp_path):
        """After removing stale files, new files are still tracked properly."""
        (tmp_path / "old.py").write_text("# old\n")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "old.py").write_text("# new location\n")

        m = ProjectManifest()
        m.add_step_output(1, ["old.py"])

        remove_superseded_files(
            str(tmp_path), m, 2, ["sub/old.py"], use_llm=False,
        )

        assert "old.py" not in m.files


class TestConfirmSupersessionLlm:
    """Tests for LLM confirmation functions."""

    @patch("architect.main.get_event_log")
    def test_returns_true_on_confirmation(self, mock_event_log):
        """Returns True when LLM confirms supersession."""
        mock_log = MagicMock()
        mock_event_log.return_value = mock_log

        mock_client = MagicMock()
        mock_client.generate.return_value = '{"superseded": true}'

        with patch("orchestrator.llm_client.get_llm_client",
                    return_value=mock_client):
            result = confirm_supersession_llm(
                "src/cleaner.py", 1, "src/data/cleaner.py", 5,
            )
        assert result is True

    @patch("architect.main.get_event_log")
    def test_returns_false_on_rejection(self, mock_event_log):
        """Returns False when LLM rejects supersession."""
        mock_log = MagicMock()
        mock_event_log.return_value = mock_log

        mock_client = MagicMock()
        mock_client.generate.return_value = '{"superseded": false}'

        with patch("orchestrator.llm_client.get_llm_client",
                    return_value=mock_client):
            result = confirm_supersession_llm(
                "src/cleaner.py", 1, "src/data/cleaner.py", 5,
            )
        assert result is False

    def test_returns_false_on_error(self):
        """Returns False when LLM call fails."""
        with patch("orchestrator.llm_client.get_llm_client",
                    side_effect=RuntimeError("fail")):
            result = confirm_supersession_llm(
                "src/cleaner.py", 1, "src/data/cleaner.py", 5,
            )
        assert result is False

    @patch("architect.main.get_event_log")
    def test_handles_markdown_fenced_json(self, mock_event_log):
        """Parses JSON even when wrapped in markdown fences."""
        mock_log = MagicMock()
        mock_event_log.return_value = mock_log

        mock_client = MagicMock()
        mock_client.generate.return_value = '```json\n{"superseded": true}\n```'

        with patch("orchestrator.llm_client.get_llm_client",
                    return_value=mock_client):
            result = confirm_supersession_llm(
                "src/old.py", 1, "src/new/old.py", 2,
            )
        assert result is True


class TestConfirmDirSupersessionLlm:
    """Tests for directory-level LLM confirmation."""

    @patch("architect.main.get_event_log")
    def test_returns_true_on_confirmation(self, mock_event_log):
        """Returns True when LLM confirms directory supersession."""
        mock_log = MagicMock()
        mock_event_log.return_value = mock_log

        mock_client = MagicMock()
        mock_client.generate.return_value = '{"superseded": true}'

        with patch("orchestrator.llm_client.get_llm_client",
                    return_value=mock_client):
            result = confirm_dir_supersession_llm(
                "src/tabs", "src/dashboard",
                {"overview", "detail"}, {"overview", "detail", "chart"},
            )
        assert result is True

    def test_returns_false_on_error(self):
        """Returns False when LLM call fails."""
        with patch("orchestrator.llm_client.get_llm_client",
                    side_effect=RuntimeError("fail")):
            result = confirm_dir_supersession_llm(
                "src/tabs", "src/dashboard", {"a"}, {"a"},
            )
        assert result is False
