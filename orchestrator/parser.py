"""Extract executable code from LLM responses."""

import json
import re


def _is_valid_python(code: str) -> bool:
    """Check whether *code* can be parsed as valid Python."""
    try:
        compile(code, "<extracted>", "exec")
        return True
    except SyntaxError:
        return False


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


def _extract_greedy_python_blocks(response: str) -> list[str]:
    """Extract Python code using greedy matching from each ```python fence.

    For each ```python opening, tries progressively shorter spans ending at
    each subsequent ``` fence (longest first).  This handles cases where the
    generated Python code contains string literals with embedded ``` markers
    (e.g., README content with markdown code blocks).
    """
    candidates: list[str] = []
    # Find all ```python opening positions
    opener_re = re.compile(r"```python\s*\n", re.IGNORECASE)
    fence_re = re.compile(r"\n```\s*(?:\n|$)")

    for m in opener_re.finditer(response):
        start = m.end()
        # Find ALL closing fence positions after this opener
        closers = [c.start() for c in fence_re.finditer(response, start)]
        # Try from the LAST closer backwards (greedy = longest match first)
        for end in reversed(closers):
            block = response[start:end].strip()
            if block:
                candidates.append(block)
        # Also try to end-of-string for truncated output (no closing fence)
        tail = response[start:].strip()
        if tail and tail not in candidates:
            candidates.append(tail)

    return candidates


def extract_code(response: str) -> str | None:
    """Extract the best Python code block from a markdown-formatted response.

    Strategy:
    1. Try JSON extraction first (Section 5a) — if the response is a
       JSON-wrapped CLI output, extract the ``result`` field and parse that.
    2. Greedy extraction from ```python fences — try the longest span from
       each opening fence to each subsequent closing fence.  This correctly
       handles code that embeds ``` inside string literals (e.g., README
       content with markdown code blocks).
    3. Fall back to non-greedy extraction for bare/other fenced blocks.
    4. Fall back to the raw response if it looks like plain Python code.

    Every candidate is validated with ``compile()`` before being accepted.
    """
    # Section 5a: Try JSON extraction first
    if response.lstrip().startswith("{"):
        json_result = extract_code_from_json(response)
        if json_result is not None:
            return json_result

    # Strategy 1: Greedy extraction from ```python fences.
    # Handles nested backticks in string literals by trying the longest
    # possible span first.
    greedy_candidates = _extract_greedy_python_blocks(response)
    for block in sorted(greedy_candidates, key=len, reverse=True):
        if _is_valid_python(block):
            return block

    # Strategy 2: Non-greedy extraction for bare/other fenced blocks.
    blocks = re.findall(
        r"```(\w*)\s*\n(.*?)```", response, re.DOTALL
    )

    if blocks:
        bare_blocks = [
            code.strip() for lang, code in blocks
            if lang == "" and _looks_like_python(code)
        ]
        for block in sorted(bare_blocks, key=len, reverse=True):
            if _is_valid_python(block):
                return block

    # Strategy 3: Treat the whole response as code if it looks like Python
    if _looks_like_python(response) and _is_valid_python(response.strip()):
        return response.strip()

    return None


def _is_truncation_syntax_error(code: str) -> bool:
    """Check whether a SyntaxError looks like mid-line truncation.

    Distinguishes truncation (code was cut off) from normal generation
    errors (wrong syntax, typos).  Truncation typically produces:
    - Unterminated string literals or f-strings
    - Unexpected EOF inside brackets, parens, or braces
    - Incomplete expressions at end of file
    """
    try:
        compile(code, "<truncation_check>", "exec")
        return False  # Valid Python — not truncated.
    except SyntaxError as e:
        msg = str(e).lower()
        truncation_signals = (
            "unterminated",          # unterminated string/f-string
            "unexpected eof",        # unexpected EOF in expression
            "eof while scanning",    # EOF while scanning string
            "was never closed",      # bracket/paren was never closed
            "expected an indented",  # block left open
            "unexpected end",        # unexpected end of input
        )
        return any(sig in msg for sig in truncation_signals)


def extract_truncated_block(response: str) -> str | None:
    """Return truncated code from a ``python block, whether or not the fence was closed.

    Detects two truncation patterns:
    1. **Open fence**: a ```python block was opened but never closed —
       the response was cut off before the closing fence.
    2. **Closed fence with truncated code**: the LLM emitted a closing
       fence near the token limit, but the code inside is syntactically
       broken in a way characteristic of truncation (unterminated string,
       unexpected EOF, unclosed brackets).

    Returns the incomplete code so the caller can request continuation,
    or None if there is no evidence of truncation.
    """
    opener_re = re.compile(r"```python\s*\n", re.IGNORECASE)
    fence_re = re.compile(r"\n```\s*(?:\n|$)")

    openers = list(opener_re.finditer(response))
    if not openers:
        return None

    # Use the last opener — the LLM may have prose-then-code structure.
    last_opener = openers[-1]
    start = last_opener.end()

    # Check if there is a closing fence after this opener.
    closers = list(fence_re.finditer(response, start))
    if closers:
        # A closing fence exists.  Normally this means the LLM finished
        # the code block.  But when the model approaches the token limit
        # it sometimes emits a closing fence even though the code inside
        # is incomplete.  Detect this by checking for truncation-specific
        # SyntaxErrors.
        last_closer = closers[-1]
        code = response[start:last_closer.start()].strip()
        if (code
                and _looks_like_python(code)
                and _is_truncation_syntax_error(code)):
            return code
        return None

    tail = response[start:].strip()
    # Strip a trailing partial closing fence (e.g., trailing "``")
    tail = re.sub(r"`{1,2}\s*$", "", tail).strip()
    if not tail:
        return None
    # Sanity: the tail should look like Python, not just random prose.
    if not _looks_like_python(tail):
        return None
    # Final check: is the tail already valid Python?
    if _is_valid_python(tail):
        return None  # Not truncated — extract_code would have found it.
    return tail


def _looks_like_python(text: str) -> bool:
    """Heuristic check for whether text looks like raw Python code."""
    # Quick reject: if the text contains heavy markdown formatting it's
    # almost certainly not Python source code.
    _markdown_signals = (
        "\n| ",       # markdown table rows
        "\n---",      # markdown horizontal rules / table separators
        "\n## ",      # markdown headers
        "\n### ",     # markdown headers
        "**",         # bold
    )
    md_hits = sum(1 for sig in _markdown_signals if sig in text)
    if md_hits >= 2:
        return False

    keywords = ("import ", "from ", "print(", "def ", "class ", "if __name__")
    if any(kw in text for kw in keywords):
        return True
    # Check for common Python patterns: assignment, function calls, comments
    for line in text.strip().splitlines()[:5]:
        line = line.strip()
        if not line:
            continue
        # Python comment or shebang (but not markdown header ##)
        if line.startswith("#") and not line.startswith("##"):
            return True
        # Variable assignment (x = ..., foo_bar = ...)
        if re.match(r"^[a-zA-Z_]\w*\s*=\s*", line):
            return True
    return False
