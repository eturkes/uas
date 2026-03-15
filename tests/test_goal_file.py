"""Tests for --goal-file flag and UAS_GOAL_FILE env var."""

import sys
from unittest.mock import patch

import pytest

from architect.main import parse_args, get_goal


class TestGoalFileFlag:
    def test_flag_parsed(self):
        with patch("sys.argv", ["prog", "--goal-file", "goal.txt"]):
            args = parse_args()
            assert args.goal_file == "goal.txt"

    def test_flag_default_none(self):
        with patch("sys.argv", ["prog", "inline goal"]):
            args = parse_args()
            assert args.goal_file is None


class TestGetGoalFromFile:
    def test_reads_goal_from_flag(self, tmp_path):
        goal_file = tmp_path / "goal.txt"
        goal_file.write_text("Build a REST API\n", encoding="utf-8")
        with patch("sys.argv", ["prog", "--goal-file", str(goal_file)]):
            args = parse_args()
            result = get_goal(args)
        assert result == "Build a REST API"

    def test_reads_goal_from_env(self, tmp_path, monkeypatch):
        goal_file = tmp_path / "goal.txt"
        goal_file.write_text("Deploy the app\n", encoding="utf-8")
        monkeypatch.setenv("UAS_GOAL_FILE", str(goal_file))
        with patch("sys.argv", ["prog"]):
            args = parse_args()
            result = get_goal(args)
        assert result == "Deploy the app"

    def test_cli_args_take_priority_over_file(self, tmp_path):
        goal_file = tmp_path / "goal.txt"
        goal_file.write_text("file goal", encoding="utf-8")
        with patch("sys.argv", ["prog", "--goal-file", str(goal_file),
                                "inline goal"]):
            args = parse_args()
            result = get_goal(args)
        assert result == "inline goal"

    def test_uas_goal_env_takes_priority_over_file(self, tmp_path, monkeypatch):
        goal_file = tmp_path / "goal.txt"
        goal_file.write_text("file goal", encoding="utf-8")
        monkeypatch.setenv("UAS_GOAL", "env goal")
        monkeypatch.setenv("UAS_GOAL_FILE", str(goal_file))
        with patch("sys.argv", ["prog"]):
            args = parse_args()
            result = get_goal(args)
        assert result == "env goal"

    def test_multiline_goal_preserved(self, tmp_path):
        goal_file = tmp_path / "goal.txt"
        goal_file.write_text("Line one\nLine two\nLine three\n",
                             encoding="utf-8")
        with patch("sys.argv", ["prog", "--goal-file", str(goal_file)]):
            args = parse_args()
            result = get_goal(args)
        assert result == "Line one\nLine two\nLine three"

    def test_missing_file_raises(self, tmp_path):
        missing = str(tmp_path / "nonexistent.txt")
        with patch("sys.argv", ["prog", "--goal-file", missing]):
            args = parse_args()
            with pytest.raises(FileNotFoundError):
                get_goal(args)

    def test_whitespace_stripped(self, tmp_path):
        goal_file = tmp_path / "goal.txt"
        goal_file.write_text("  padded goal  \n\n", encoding="utf-8")
        with patch("sys.argv", ["prog", "--goal-file", str(goal_file)]):
            args = parse_args()
            result = get_goal(args)
        assert result == "padded goal"
