"""Pydantic result models for fuzzy-function-backed state parsing.

These models define the structured return types used by ``@fuzzy_function``
decorated callables throughout UAS.  Each model replaces a brittle regex or
substring-matching parser with a typed schema that the LLM must satisfy.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ExecutionResult(BaseModel):
    """Structured verdict for a sandbox execution attempt."""

    success: bool
    revert_needed: bool
    error_category: str | None = None
    summary: str


class UASResult(BaseModel):
    """Parsed UAS_RESULT output from sandbox stdout."""

    status: Literal["ok", "error"]
    files_written: list[str] = Field(default_factory=list)
    summary: str
    error: str | None = None


class ErrorClassification(BaseModel):
    """LLM-backed classification of a CLI / subprocess error."""

    category: Literal[
        "rate_limit",
        "capacity",
        "auth",
        "connection",
        "timeout",
        "prompt_too_long",
        "output_truncated",
        "unknown",
    ]
    retryable: bool
    recommended_backoff: float
    message: str


class CodeQuality(BaseModel):
    """Pre-execution quality assessment of generated code."""

    has_uas_result: bool
    has_input_call: bool
    is_file_modification: bool
    missing_imports: list[str] = Field(default_factory=list)
