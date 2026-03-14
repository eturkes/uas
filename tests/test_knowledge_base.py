"""Tests for architect.state knowledge base functions."""

import json
import os

from architect.state import read_knowledge_base, append_knowledge


class TestReadKnowledgeBase:
    def test_returns_empty_structure_when_missing(self, tmp_workspace):
        result = read_knowledge_base()
        assert result == {"package_versions": {}, "lessons": []}

    def test_reads_existing_file(self, tmp_workspace):
        kb_path = os.path.join(str(tmp_workspace), ".state", "knowledge.json")
        os.makedirs(os.path.dirname(kb_path), exist_ok=True)
        data = {"package_versions": {"requests": "2.31.0"}, "lessons": []}
        with open(kb_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        result = read_knowledge_base()
        assert result["package_versions"]["requests"] == "2.31.0"


class TestAppendKnowledge:
    def test_package_version_round_trip(self, tmp_workspace):
        append_knowledge("package_version", {"numpy": "1.26.0"})
        kb = read_knowledge_base()
        assert kb["package_versions"]["numpy"] == "1.26.0"

    def test_lesson_round_trip(self, tmp_workspace):
        append_knowledge("lesson", {"text": "Always pin versions"})
        kb = read_knowledge_base()
        assert len(kb["lessons"]) == 1
        assert kb["lessons"][0]["text"] == "Always pin versions"

    def test_multiple_package_versions(self, tmp_workspace):
        append_knowledge("package_version", {"requests": "2.31.0"})
        append_knowledge("package_version", {"flask": "3.0.0"})
        kb = read_knowledge_base()
        assert kb["package_versions"]["requests"] == "2.31.0"
        assert kb["package_versions"]["flask"] == "3.0.0"

    def test_lesson_cap_at_50(self, tmp_workspace):
        for i in range(55):
            append_knowledge("lesson", {"text": f"lesson {i}"})
        kb = read_knowledge_base()
        assert len(kb["lessons"]) == 50
        # Should keep the most recent 50 (indices 5-54)
        assert kb["lessons"][0]["text"] == "lesson 5"
        assert kb["lessons"][-1]["text"] == "lesson 54"

    def test_creates_state_directory(self, tmp_workspace):
        # Ensure .state doesn't exist yet
        state_dir = os.path.join(str(tmp_workspace), ".state")
        if os.path.exists(state_dir):
            os.rmdir(state_dir)
        append_knowledge("package_version", {"pip": "24.0"})
        assert os.path.isdir(state_dir)

    def test_package_version_overwrites(self, tmp_workspace):
        append_knowledge("package_version", {"requests": "2.30.0"})
        append_knowledge("package_version", {"requests": "2.31.0"})
        kb = read_knowledge_base()
        assert kb["package_versions"]["requests"] == "2.31.0"

    def test_unknown_entry_type_no_crash(self, tmp_workspace):
        # Unknown type should write file but not modify data meaningfully
        append_knowledge("unknown_type", {"key": "val"})
        kb = read_knowledge_base()
        assert kb["package_versions"] == {}
        assert kb["lessons"] == []

    def test_corrupt_file_raises(self, tmp_workspace):
        kb_path = os.path.join(str(tmp_workspace), ".state", "knowledge.json")
        os.makedirs(os.path.dirname(kb_path), exist_ok=True)
        with open(kb_path, "w", encoding="utf-8") as f:
            f.write("not valid json{{{")
        # read_knowledge_base should raise on corrupt JSON
        try:
            read_knowledge_base()
            assert False, "Expected json.JSONDecodeError"
        except json.JSONDecodeError:
            pass
