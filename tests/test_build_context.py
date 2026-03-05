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

    def test_dict_output_with_stdout(self):
        step = {"depends_on": [1]}
        outputs = {1: {"stdout": "result data", "stderr": "", "files": []}}
        result = build_context(step, outputs)
        assert "stdout" in result
        assert "result data" in result

    def test_dict_output_with_stderr(self):
        step = {"depends_on": [1]}
        outputs = {1: {"stdout": "", "stderr": "warning msg", "files": []}}
        result = build_context(step, outputs)
        assert "stderr" in result
        assert "warning msg" in result

    def test_dict_output_with_files(self):
        step = {"depends_on": [1]}
        outputs = {1: {
            "stdout": "",
            "stderr": "",
            "files": ["/workspace/out.txt", "/workspace/data.json"],
        }}
        result = build_context(step, outputs)
        assert "Files from step 1" in result
        assert "/workspace/out.txt" in result
        assert "/workspace/data.json" in result

    def test_dict_output_with_all_fields(self):
        step = {"depends_on": [1]}
        outputs = {1: {
            "stdout": "main output",
            "stderr": "some warning",
            "files": ["/workspace/result.txt"],
        }}
        result = build_context(step, outputs)
        assert "main output" in result
        assert "some warning" in result
        assert "/workspace/result.txt" in result

    def test_dict_output_empty_fields_omitted(self):
        step = {"depends_on": [1]}
        outputs = {1: {"stdout": "", "stderr": "", "files": []}}
        result = build_context(step, outputs)
        assert result == ""

    def test_mixed_string_and_dict_outputs(self):
        step = {"depends_on": [1, 2]}
        outputs = {
            1: "plain string output",
            2: {"stdout": "dict output", "stderr": "", "files": []},
        }
        result = build_context(step, outputs)
        assert "plain string output" in result
        assert "dict output" in result
