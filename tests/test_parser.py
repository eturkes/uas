"""Tests for orchestrator.parser.extract_code."""

from orchestrator.parser import extract_code, _looks_like_python


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

    def test_multiple_blocks_picks_longest_python(self):
        response = (
            "Here is a helper:\n"
            '```python\nx = 1\n```\n'
            "And here is the full script:\n"
            '```python\nimport os\nimport sys\n\ndef main():\n    print("done")\n\nmain()\n```\n'
        )
        result = extract_code(response)
        assert "import os" in result
        assert "def main():" in result

    def test_multiple_blocks_prefers_python_over_bare(self):
        response = (
            '```\necho "this is bash"\necho "more bash"\necho "even more"\n```\n'
            '```python\nprint("hello")\n```\n'
        )
        result = extract_code(response)
        assert result == 'print("hello")'

    def test_multiple_bare_blocks_picks_longest(self):
        response = (
            '```\nx = 1\n```\n'
            '```\nimport os\nprint(os.getcwd())\n```\n'
        )
        result = extract_code(response)
        assert "import os" in result

    def test_fallback_from_keyword(self):
        response = "from pathlib import Path\nPath('.').resolve()"
        assert extract_code(response) == response.strip()

    def test_fallback_if_name_main(self):
        response = 'if __name__ == "__main__":\n    pass'
        assert extract_code(response) == response.strip()

    def test_non_python_tagged_block_ignored(self):
        response = '```javascript\nconsole.log("hi")\n```'
        assert extract_code(response) is None


class TestLooksLikePython:
    def test_import_statement(self):
        assert _looks_like_python("import os") is True

    def test_from_import(self):
        assert _looks_like_python("from sys import argv") is True

    def test_plain_text(self):
        assert _looks_like_python("Hello world") is False

    def test_if_name_main(self):
        assert _looks_like_python('if __name__ == "__main__":') is True
