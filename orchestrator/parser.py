"""Extract executable code from LLM responses."""

import json
import re


def extract_code_from_json(response: str) -> str | None:
    """Try to extract code from a JSON-formatted CLI response (Section 5a).

    If the response is a JSON object with a ``result`` key, extracts code
    from that field.  Returns None if response isn't JSON or contains no code.
    """
    try:
        data = json.loads(response)
        if isinstance(data, dict) and "result" in data:
            return extract_code(data["result"])
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def extract_code(response: str) -> str | None:
    """Extract the best Python code block from a markdown-formatted response.

    Strategy:
    1. Try JSON extraction first (Section 5a) — if the response is a
       JSON-wrapped CLI output, extract the ``result`` field and parse that.
    2. Find all fenced code blocks (```python and bare ```).
    3. Prefer blocks explicitly tagged as Python.
    4. Among candidates, pick the longest one.
    5. Fall back to the raw response if it looks like plain Python code.
    """
    # Section 5a: Try JSON extraction first
    if response.lstrip().startswith("{"):
        json_result = extract_code_from_json(response)
        if json_result is not None:
            return json_result

    # Find all fenced code blocks with their language tag (if any)
    blocks = re.findall(
        r"```(\w*)\s*\n(.*?)```", response, re.DOTALL
    )

    if blocks:
        # Separate Python-tagged blocks from untagged/other blocks
        python_blocks = [code.strip() for lang, code in blocks if lang == "python"]
        bare_blocks = [code.strip() for lang, code in blocks if lang == ""]

        # Prefer Python-tagged blocks, then bare blocks
        candidates = python_blocks or bare_blocks
        if candidates:
            return max(candidates, key=len)

    # Fallback: treat the whole response as code if it looks like Python
    if _looks_like_python(response):
        return response.strip()

    return None


def _looks_like_python(text: str) -> bool:
    """Heuristic check for whether text looks like raw Python code."""
    keywords = ("import ", "from ", "print(", "def ", "class ", "if __name__")
    return any(kw in text for kw in keywords)
