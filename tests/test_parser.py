"""Tests for orchestrator.parser.extract_code."""

from orchestrator.parser import extract_code


class TestExtractCode:
    def test_python_fenced_block(self):
        response = '```python\nprint("hello")\n```'
        assert extract_code(response) == 'print("hello")'

    def test_bare_fenced_block(self):
        response = '```\nx = 1\n```'
        assert extract_code(response) == "x = 1"

    def test_fenced_block_with_surrounding_text(self):
        response = 'Here is the code:\n```python\nimport os\nprint(os.getcwd())\n```\nDone.'
        assert extract_code(response) == "import os\nprint(os.getcwd())"

    def test_fallback_import(self):
        response = "import sys\nprint(sys.argv)"
        assert extract_code(response) == response.strip()

    def test_fallback_def(self):
        response = "def foo():\n    pass"
        assert extract_code(response) == response.strip()

    def test_fallback_class(self):
        response = "class Foo:\n    pass"
        assert extract_code(response) == response.strip()

    def test_fallback_print(self):
        response = 'print("hi")'
        assert extract_code(response) == response.strip()

    def test_no_code(self):
        response = "I'm sorry, I can't help with that."
        assert extract_code(response) is None

    def test_empty_string(self):
        assert extract_code("") is None

    def test_multiline_fenced_block(self):
        response = '```python\nimport os\n\ndef main():\n    print("done")\n\nmain()\n```'
        result = extract_code(response)
        assert "import os" in result
        assert "def main():" in result
        assert "main()" in result
