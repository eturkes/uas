"""Tests for architect.main.build_context."""

from architect.main import build_context


class TestBuildContext:
    def test_no_dependencies(self):
        step = {"depends_on": []}
        assert build_context(step, {}) == ""

    def test_single_dependency(self):
        step = {"depends_on": [1]}
        outputs = {1: "result from step 1"}
        result = build_context(step, outputs)
        assert "step 1" in result
        assert "result from step 1" in result

    def test_multiple_dependencies(self):
        step = {"depends_on": [1, 2]}
        outputs = {1: "out1", 2: "out2"}
        result = build_context(step, outputs)
        assert "out1" in result
        assert "out2" in result

    def test_missing_dependency_output(self):
        step = {"depends_on": [1, 2]}
        outputs = {1: "out1"}
        result = build_context(step, outputs)
        assert "out1" in result
        # Step 2 has no output, should not crash
        assert "out2" not in result

    def test_empty_dependency_output(self):
        step = {"depends_on": [1]}
        outputs = {1: ""}
        result = build_context(step, outputs)
        # Empty output should not produce a context line
        assert result == ""
