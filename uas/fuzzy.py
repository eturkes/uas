"""LLM-backed fuzzy function decorator for structured parsing.

Provides ``@fuzzy_function`` which turns a plain Python function into an
LLM call that returns a validated Pydantic model.  The decorated function's
docstring becomes the LLM prompt, and the return-type annotation (a
``BaseModel`` subclass) defines the expected JSON schema.

Usage::

    from pydantic import BaseModel
    from uas.fuzzy import fuzzy_function

    class Sentiment(BaseModel):
        label: str
        score: float

    @fuzzy_function
    def classify_sentiment(text: str) -> Sentiment:
        \"\"\"Classify the sentiment of the given text.\"\"\"

    result = classify_sentiment("I love this!")  # -> Sentiment(label="positive", score=0.95)
"""

import functools
import hashlib
import inspect
import json
import logging
import typing

import anthropic
from pydantic import BaseModel

import uas_config as config

logger = logging.getLogger(__name__)


class FuzzyDisabledError(RuntimeError):
    """Raised when a fuzzy function is called but fuzzy mode is disabled."""


# Default model for fuzzy calls — overridable via UAS_FUZZY_MODEL or config.
# UAS framework policy: track Claude's current default (Opus 4.7). The
# Anthropic SDK requires an explicit model id, so this constant must be
# bumped in lockstep with new Claude releases.
_DEFAULT_MODEL = "claude-opus-4-7"

# Maximum cached (prompt, args) pairs per decorated function.
_CACHE_SIZE = 256


def _build_system_prompt(model_cls: type[BaseModel]) -> str:
    """Build a system prompt enforcing JSON output matching *model_cls*."""
    schema = model_cls.model_json_schema()
    return (
        "You are a structured data extraction assistant. "
        "Respond with ONLY a valid JSON object matching the following "
        "JSON schema. No markdown fences, no commentary, no extra text.\n\n"
        f"Schema:\n{json.dumps(schema, indent=2)}"
    )


def _cache_key(*args: object, **kwargs: object) -> str:
    """Deterministic hash of all function arguments."""
    raw = json.dumps(
        {
            "a": [str(a) for a in args],
            "k": {k: str(v) for k, v in sorted(kwargs.items())},
        },
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def fuzzy_function(fn: typing.Callable) -> typing.Callable:
    """Decorator: LLM-backed function that returns a validated Pydantic model.

    Requirements on the decorated function:

    * Return-type annotation must be a ``pydantic.BaseModel`` subclass.
    * A docstring that describes what the LLM should extract / compute.

    At call time the decorator:

    1. Builds a user prompt from the docstring + stringified arguments.
    2. Calls the Anthropic API (model from ``config.get("fuzzy_model")``)
       with a system prompt enforcing the Pydantic JSON schema.
    3. Validates the response with ``Model.model_validate_json()``.
    4. Returns the validated model instance.

    Identical inputs are cached (bounded LRU) within the process lifetime.
    """
    hints = typing.get_type_hints(fn)
    model_cls = hints.get("return")
    if model_cls is None or not (
        isinstance(model_cls, type) and issubclass(model_cls, BaseModel)
    ):
        raise TypeError(
            f"@fuzzy_function requires a pydantic.BaseModel return type, "
            f"got {model_cls!r} on {fn.__qualname__}"
        )

    system_prompt = _build_system_prompt(model_cls)
    sig = inspect.signature(fn)

    # Bounded dict cache — oldest entry evicted when full.
    _cache: dict[str, BaseModel] = {}

    @functools.wraps(fn)
    def wrapper(*args: object, **kwargs: object) -> BaseModel:
        if not config.get("fuzzy_enabled", True):
            raise FuzzyDisabledError(
                f"Fuzzy function {fn.__qualname__} skipped: "
                "UAS_FUZZY_ENABLED is false"
            )

        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()

        docstring = fn.__doc__ or ""
        arg_lines = "\n".join(
            f"  {k} = {v!r}" for k, v in bound.arguments.items()
        )
        user_prompt = f"{docstring.strip()}\n\nArguments:\n{arg_lines}"

        key = _cache_key(*args, **kwargs)
        if key in _cache:
            logger.debug("fuzzy cache hit for %s", fn.__qualname__)
            return _cache[key]

        model = config.get("fuzzy_model", _DEFAULT_MODEL) or _DEFAULT_MODEL

        client = anthropic.Anthropic()
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw_text = response.content[0].text
        result = model_cls.model_validate_json(raw_text)

        # Evict oldest entry when cache is full.
        if len(_cache) >= _CACHE_SIZE:
            del _cache[next(iter(_cache))]
        _cache[key] = result

        logger.debug("fuzzy call %s -> %s", fn.__qualname__, result)
        return result

    wrapper.cache_clear = lambda: _cache.clear()  # type: ignore[attr-defined]
    wrapper._cache = _cache  # type: ignore[attr-defined]
    return wrapper
