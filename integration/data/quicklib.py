"""quicklib — a tiny utility module for the readme-from-source eval case.

Provides a handful of pure functions over strings, lists, and numbers.
No external dependencies. Public functions do not start with an
underscore; helpers prefixed with an underscore are private.
"""

from typing import Iterable, List, Sequence


def reverse_string(text: str) -> str:
    """Return ``text`` with its characters in reverse order."""
    return text[::-1]


def is_palindrome(text: str) -> bool:
    """Return True if ``text`` reads the same forward and backward,
    ignoring case and ASCII whitespace.
    """
    cleaned = "".join(ch for ch in text.lower() if not ch.isspace())
    return cleaned == cleaned[::-1]


def chunks(items: Sequence, size: int) -> List[list]:
    """Split ``items`` into consecutive sub-lists of length ``size``.

    The final chunk may be shorter than ``size`` if the input length
    is not a multiple of ``size``. Raises ``ValueError`` if size < 1.
    """
    if size < 1:
        raise ValueError("size must be >= 1")
    return [list(items[i:i + size]) for i in range(0, len(items), size)]


def flatten(nested: Iterable) -> list:
    """Flatten a one-level-nested iterable into a single list."""
    out: list = []
    for sub in nested:
        out.extend(sub)
    return out


def mean(numbers: Sequence[float]) -> float:
    """Arithmetic mean of ``numbers``. Raises ``ValueError`` if empty."""
    if not numbers:
        raise ValueError("mean of empty sequence")
    return sum(numbers) / len(numbers)


def clamp(value: float, lo: float, hi: float) -> float:
    """Clamp ``value`` into the inclusive range ``[lo, hi]``.

    Raises ``ValueError`` if ``lo > hi``.
    """
    if lo > hi:
        raise ValueError("lo must be <= hi")
    return max(lo, min(hi, value))


def _internal_helper(text: str) -> str:
    # Private — should NOT appear in the README's API reference.
    return text.strip().lower()
