"""Tests for architect.planner.parse_steps_json."""

import pytest

from architect.planner import parse_steps_json


class TestParseStepsJson:
    def test_direct_json_array(self):
        raw = '[{"title": "step1", "description": "do X", "depends_on": []}]'
        result = parse_steps_json(raw)
        assert len(result) == 1
        assert result[0]["title"] == "step1"

    def test_json_in_code_fence(self):
        raw = 'Here are the steps:\n```json\n[{"title": "a", "description": "b"}]\n```'
        result = parse_steps_json(raw)
        assert len(result) == 1
        assert result[0]["title"] == "a"

    def test_json_in_bare_fence(self):
        raw = '```\n[{"title": "a", "description": "b"}]\n```'
        result = parse_steps_json(raw)
        assert len(result) == 1

    def test_bracket_extraction(self):
        raw = 'The steps are: [{"title": "x", "description": "y"}] end.'
        result = parse_steps_json(raw)
        assert len(result) == 1
        assert result[0]["title"] == "x"

    def test_multiple_steps(self):
        raw = '[{"title": "a", "description": "b"}, {"title": "c", "description": "d"}]'
        result = parse_steps_json(raw)
        assert len(result) == 2

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="Could not parse"):
            parse_steps_json("this is not json at all")

    def test_json_object_not_array_raises(self):
        with pytest.raises(ValueError, match="Could not parse"):
            parse_steps_json('{"title": "not an array"}')

    def test_whitespace_padded(self):
        raw = '  \n  [{"title": "a", "description": "b"}]  \n  '
        result = parse_steps_json(raw)
        assert len(result) == 1
