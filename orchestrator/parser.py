"""Extract executable code from LLM responses."""

import re


def extract_code(response: str) -> str | None:
    """Extract the first fenced code block from a markdown-formatted response.

    Supports ```python and bare ``` fences.
    Falls back to the raw response if it looks like plain code.
    """
    match = re.search(r"```(?:python)?\s*\n(.*?)```", response, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Fallback: treat the whole response as code if it contains Python keywords
    if any(kw in response for kw in ("import ", "print(", "def ", "class ")):
        return response.strip()

    return None
