"""Extract executable code from LLM responses."""

import re


def extract_code(response: str) -> str | None:
    """Extract the best Python code block from a markdown-formatted response.

    Strategy:
    1. Find all fenced code blocks (```python and bare ```).
    2. Prefer blocks explicitly tagged as Python.
    3. Among candidates, pick the longest one.
    4. Fall back to the raw response if it looks like plain Python code.
    """
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
