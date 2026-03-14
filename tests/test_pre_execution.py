"""Tests for orchestrator.main.pre_execution_check."""

from orchestrator.main import pre_execution_check


class TestPreExecutionCheck:
    def test_valid_code_no_issues(self):
        code = 'print("UAS_RESULT: ok")'
        errors, warnings = pre_execution_check(code)
        assert errors == []
        assert warnings == []

    def test_syntax_error_is_critical(self):
        code = "def foo(\n"
        errors, warnings = pre_execution_check(code)
        assert len(errors) == 1
        assert "Syntax error" in errors[0]

    def test_input_call_is_critical(self):
        code = 'name = input("Enter: ")\nprint(f"UAS_RESULT: {name}")'
        errors, warnings = pre_execution_check(code)
        assert any("input()" in e for e in errors)

    def test_missing_uas_result_is_warning(self):
        code = 'print("hello world")'
        errors, warnings = pre_execution_check(code)
        assert errors == []
        assert len(warnings) == 1
        assert "UAS_RESULT" in warnings[0]

    def test_valid_code_with_uas_result(self):
        code = 'import json\nprint(f"UAS_RESULT: {json.dumps({})}")'
        errors, warnings = pre_execution_check(code)
        assert errors == []
        assert warnings == []

    def test_input_with_spaces(self):
        code = 'x = input  ("prompt")\nprint(f"UAS_RESULT: {x}")'
        errors, warnings = pre_execution_check(code)
        assert any("input()" in e for e in errors)

    def test_multiple_issues(self):
        # Syntax error + no UAS_RESULT
        code = "def broken(\n"
        errors, warnings = pre_execution_check(code)
        assert len(errors) >= 1  # syntax error
        assert len(warnings) >= 1  # missing UAS_RESULT

    def test_valid_multiline_code(self):
        code = (
            'import os\n'
            'import json\n'
            'result = {"status": "ok"}\n'
            'print(f"UAS_RESULT: {json.dumps(result)}")\n'
        )
        errors, warnings = pre_execution_check(code)
        assert errors == []
        assert warnings == []

    def test_empty_code(self):
        # Empty string compiles fine but has no UAS_RESULT
        errors, warnings = pre_execution_check("")
        assert errors == []
        assert len(warnings) == 1
        assert "UAS_RESULT" in warnings[0]
