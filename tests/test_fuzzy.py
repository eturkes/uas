"""Tests for uas.fuzzy: decorator, caching, error handling, and all fuzzy functions.

Tests use mocked Anthropic responses — no real API calls are made.
Covers both the happy path (valid JSON returned) and failure modes
(malformed response, API error, disabled toggle).
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from uas.fuzzy import FuzzyDisabledError, _build_system_prompt, _cache_key, fuzzy_function
from uas.fuzzy_models import (
    CodeQuality,
    ErrorClassification,
    ExecutionResult,
    SandboxOutput,
    UASResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(text: str) -> SimpleNamespace:
    """Build a fake Anthropic API response with the given text content."""
    block = SimpleNamespace(text=text)
    return SimpleNamespace(content=[block])


def _json_response(data: dict) -> SimpleNamespace:
    """Build a fake Anthropic response from a dict (serialised to JSON)."""
    return _make_response(json.dumps(data))


# ---------------------------------------------------------------------------
# Decorator basics
# ---------------------------------------------------------------------------


class TestFuzzyFunctionDecorator:
    """Tests for @fuzzy_function setup and validation."""

    def test_rejects_missing_return_annotation(self):
        with pytest.raises(TypeError, match="pydantic.BaseModel return type"):
            @fuzzy_function
            def bad_fn(x: str):
                """No return annotation."""

    def test_rejects_non_basemodel_return(self):
        with pytest.raises(TypeError, match="pydantic.BaseModel return type"):
            @fuzzy_function
            def bad_fn(x: str) -> str:
                """Returns str, not BaseModel."""

    def test_accepts_basemodel_return(self):
        class DummyModel(BaseModel):
            value: int

        @fuzzy_function
        def ok_fn(x: str) -> DummyModel:
            """Valid function."""

        assert callable(ok_fn)

    def test_preserves_function_name(self):
        class DummyModel(BaseModel):
            value: int

        @fuzzy_function
        def my_func(x: str) -> DummyModel:
            """Doc."""

        assert my_func.__name__ == "my_func"

    def test_has_cache_clear(self):
        class DummyModel(BaseModel):
            value: int

        @fuzzy_function
        def fn(x: str) -> DummyModel:
            """Doc."""

        assert hasattr(fn, "cache_clear")
        assert hasattr(fn, "_cache")


# ---------------------------------------------------------------------------
# Core call path (happy path)
# ---------------------------------------------------------------------------

class DummyResult(BaseModel):
    answer: str
    score: float


@fuzzy_function
def _dummy_fn(text: str) -> DummyResult:
    """Return a dummy result for the given text."""


class TestFuzzyFunctionHappyPath:
    """Tests for successful fuzzy function invocation."""

    @patch("uas.fuzzy.anthropic.Anthropic")
    @patch("uas.fuzzy.config.get")
    def test_returns_validated_model(self, mock_cfg, mock_anthropic_cls):
        _dummy_fn.cache_clear()
        mock_cfg.side_effect = lambda key, default=None: {
            "fuzzy_enabled": True,
            "fuzzy_model": "test-model",
        }.get(key, default)

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _json_response(
            {"answer": "hello", "score": 0.9}
        )

        result = _dummy_fn("some text")

        assert isinstance(result, DummyResult)
        assert result.answer == "hello"
        assert result.score == 0.9

    @patch("uas.fuzzy.anthropic.Anthropic")
    @patch("uas.fuzzy.config.get")
    def test_api_called_with_correct_prompt(self, mock_cfg, mock_anthropic_cls):
        _dummy_fn.cache_clear()
        mock_cfg.side_effect = lambda key, default=None: {
            "fuzzy_enabled": True,
            "fuzzy_model": "test-model",
        }.get(key, default)

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _json_response(
            {"answer": "x", "score": 1.0}
        )

        _dummy_fn("test input")

        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["model"] == "test-model"
        assert call_kwargs["max_tokens"] == 1024
        assert "text = 'test input'" in call_kwargs["messages"][0]["content"]
        assert "JSON schema" in call_kwargs["system"]


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


class TestFuzzyFunctionCache:
    """Tests for the per-function LRU cache."""

    @patch("uas.fuzzy.anthropic.Anthropic")
    @patch("uas.fuzzy.config.get")
    def test_cache_hit_avoids_api_call(self, mock_cfg, mock_anthropic_cls):
        _dummy_fn.cache_clear()
        mock_cfg.side_effect = lambda key, default=None: {
            "fuzzy_enabled": True,
            "fuzzy_model": "test-model",
        }.get(key, default)

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _json_response(
            {"answer": "cached", "score": 0.5}
        )

        r1 = _dummy_fn("same input")
        r2 = _dummy_fn("same input")

        assert r1 == r2
        assert mock_client.messages.create.call_count == 1

    @patch("uas.fuzzy.anthropic.Anthropic")
    @patch("uas.fuzzy.config.get")
    def test_different_inputs_not_cached(self, mock_cfg, mock_anthropic_cls):
        _dummy_fn.cache_clear()
        mock_cfg.side_effect = lambda key, default=None: {
            "fuzzy_enabled": True,
            "fuzzy_model": "test-model",
        }.get(key, default)

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.side_effect = [
            _json_response({"answer": "a", "score": 1.0}),
            _json_response({"answer": "b", "score": 2.0}),
        ]

        r1 = _dummy_fn("input A")
        r2 = _dummy_fn("input B")

        assert r1.answer == "a"
        assert r2.answer == "b"
        assert mock_client.messages.create.call_count == 2

    @patch("uas.fuzzy.anthropic.Anthropic")
    @patch("uas.fuzzy.config.get")
    def test_cache_clear_works(self, mock_cfg, mock_anthropic_cls):
        _dummy_fn.cache_clear()
        mock_cfg.side_effect = lambda key, default=None: {
            "fuzzy_enabled": True,
            "fuzzy_model": "test-model",
        }.get(key, default)

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _json_response(
            {"answer": "x", "score": 0.0}
        )

        _dummy_fn("text")
        assert len(_dummy_fn._cache) == 1

        _dummy_fn.cache_clear()
        assert len(_dummy_fn._cache) == 0

    @patch("uas.fuzzy._CACHE_SIZE", 2)
    @patch("uas.fuzzy.anthropic.Anthropic")
    @patch("uas.fuzzy.config.get")
    def test_cache_evicts_oldest_when_full(self, mock_cfg, mock_anthropic_cls):
        _dummy_fn.cache_clear()
        mock_cfg.side_effect = lambda key, default=None: {
            "fuzzy_enabled": True,
            "fuzzy_model": "test-model",
        }.get(key, default)

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _json_response(
            {"answer": "x", "score": 0.0}
        )

        _dummy_fn("a")
        _dummy_fn("b")
        assert len(_dummy_fn._cache) == 2

        _dummy_fn("c")
        # Oldest entry ("a") evicted, cache still has 2 entries.
        assert len(_dummy_fn._cache) == 2


# ---------------------------------------------------------------------------
# Disabled toggle
# ---------------------------------------------------------------------------


class TestFuzzyDisabled:
    """Tests for UAS_FUZZY_ENABLED=false behavior."""

    @patch("uas.fuzzy.config.get")
    def test_raises_fuzzy_disabled_error(self, mock_cfg):
        _dummy_fn.cache_clear()
        mock_cfg.side_effect = lambda key, default=None: {
            "fuzzy_enabled": False,
        }.get(key, default)

        with pytest.raises(FuzzyDisabledError, match="UAS_FUZZY_ENABLED is false"):
            _dummy_fn("should fail")

    @patch("uas.fuzzy.config.get")
    def test_error_is_runtime_error_subclass(self, mock_cfg):
        mock_cfg.side_effect = lambda key, default=None: {
            "fuzzy_enabled": False,
        }.get(key, default)

        with pytest.raises(RuntimeError):
            _dummy_fn("x")


# ---------------------------------------------------------------------------
# Malformed / invalid responses
# ---------------------------------------------------------------------------


class TestFuzzyFunctionErrors:
    """Tests for malformed LLM responses, API errors, and timeouts."""

    @patch("uas.fuzzy.anthropic.Anthropic")
    @patch("uas.fuzzy.config.get")
    def test_malformed_json_raises_validation_error(
        self, mock_cfg, mock_anthropic_cls
    ):
        _dummy_fn.cache_clear()
        mock_cfg.side_effect = lambda key, default=None: {
            "fuzzy_enabled": True,
            "fuzzy_model": "test-model",
        }.get(key, default)

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_response(
            "This is not JSON at all"
        )

        with pytest.raises(Exception):
            _dummy_fn("trigger malformed")

    @patch("uas.fuzzy.anthropic.Anthropic")
    @patch("uas.fuzzy.config.get")
    def test_wrong_schema_raises_validation_error(
        self, mock_cfg, mock_anthropic_cls
    ):
        _dummy_fn.cache_clear()
        mock_cfg.side_effect = lambda key, default=None: {
            "fuzzy_enabled": True,
            "fuzzy_model": "test-model",
        }.get(key, default)

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        # Missing required field "score".
        mock_client.messages.create.return_value = _json_response(
            {"answer": "hello"}
        )

        with pytest.raises(Exception):
            _dummy_fn("trigger wrong schema")

    @patch("uas.fuzzy.anthropic.Anthropic")
    @patch("uas.fuzzy.config.get")
    def test_api_error_propagates(self, mock_cfg, mock_anthropic_cls):
        _dummy_fn.cache_clear()
        mock_cfg.side_effect = lambda key, default=None: {
            "fuzzy_enabled": True,
            "fuzzy_model": "test-model",
        }.get(key, default)

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.side_effect = RuntimeError("API unreachable")

        with pytest.raises(RuntimeError, match="API unreachable"):
            _dummy_fn("trigger api error")

    @patch("uas.fuzzy.anthropic.Anthropic")
    @patch("uas.fuzzy.config.get")
    def test_malformed_response_not_cached(self, mock_cfg, mock_anthropic_cls):
        _dummy_fn.cache_clear()
        mock_cfg.side_effect = lambda key, default=None: {
            "fuzzy_enabled": True,
            "fuzzy_model": "test-model",
        }.get(key, default)

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_response("not json")

        with pytest.raises(Exception):
            _dummy_fn("bad input")

        assert len(_dummy_fn._cache) == 0


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestBuildSystemPrompt:
    def test_includes_schema(self):
        prompt = _build_system_prompt(DummyResult)
        assert "JSON schema" in prompt
        assert "answer" in prompt
        assert "score" in prompt

    def test_no_markdown_instruction(self):
        prompt = _build_system_prompt(DummyResult)
        assert "No markdown" in prompt


class TestCacheKey:
    def test_deterministic(self):
        k1 = _cache_key("a", "b", x=1)
        k2 = _cache_key("a", "b", x=1)
        assert k1 == k2

    def test_different_args_different_keys(self):
        k1 = _cache_key("a")
        k2 = _cache_key("b")
        assert k1 != k2

    def test_kwarg_order_independent(self):
        k1 = _cache_key(x=1, y=2)
        k2 = _cache_key(y=2, x=1)
        assert k1 == k2


# ---------------------------------------------------------------------------
# Concrete fuzzy function tests: classify_llm_error
# ---------------------------------------------------------------------------


class TestClassifyLlmError:
    """Tests for classify_llm_error and its classify_error wrapper."""

    @patch("uas.fuzzy.anthropic.Anthropic")
    @patch("uas.fuzzy.config.get")
    def test_happy_path(self, mock_cfg, mock_anthropic_cls):
        from orchestrator.llm_client import classify_llm_error
        classify_llm_error.cache_clear()
        mock_cfg.side_effect = lambda key, default=None: {
            "fuzzy_enabled": True,
            "fuzzy_model": "test-model",
        }.get(key, default)

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _json_response({
            "category": "rate_limit",
            "retryable": True,
            "recommended_backoff": 30.0,
            "message": "Rate limited",
        })

        result = classify_llm_error(1, "", "429 rate limit")
        assert isinstance(result, ErrorClassification)
        assert result.category == "rate_limit"
        assert result.retryable is True

    @patch("uas.fuzzy.anthropic.Anthropic")
    @patch("uas.fuzzy.config.get")
    def test_classify_error_wrapper_fallback(self, mock_cfg, mock_anthropic_cls):
        from orchestrator.llm_client import classify_error, classify_llm_error
        classify_llm_error.cache_clear()
        mock_cfg.side_effect = lambda key, default=None: {
            "fuzzy_enabled": True,
            "fuzzy_model": "test-model",
        }.get(key, default)

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.side_effect = RuntimeError("API down")

        result = classify_error(1, "", "error")
        assert result.category == "unknown"
        assert result.retryable is False


# ---------------------------------------------------------------------------
# Concrete fuzzy function tests: parse_uas_output
# ---------------------------------------------------------------------------


class TestParseUasOutput:
    @patch("uas.fuzzy.anthropic.Anthropic")
    @patch("uas.fuzzy.config.get")
    def test_happy_path(self, mock_cfg, mock_anthropic_cls):
        from orchestrator.main import parse_uas_output
        parse_uas_output.cache_clear()
        mock_cfg.side_effect = lambda key, default=None: {
            "fuzzy_enabled": True,
            "fuzzy_model": "test-model",
        }.get(key, default)

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _json_response({
            "status": "ok",
            "files_written": ["main.py"],
            "summary": "Created file",
            "error": None,
        })

        result = parse_uas_output("UAS_RESULT: {\"status\": \"ok\"}")
        assert isinstance(result, UASResult)
        assert result.status == "ok"
        assert result.files_written == ["main.py"]

    @patch("uas.fuzzy.anthropic.Anthropic")
    @patch("uas.fuzzy.config.get")
    def test_error_status(self, mock_cfg, mock_anthropic_cls):
        from orchestrator.main import parse_uas_output
        parse_uas_output.cache_clear()
        mock_cfg.side_effect = lambda key, default=None: {
            "fuzzy_enabled": True,
            "fuzzy_model": "test-model",
        }.get(key, default)

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _json_response({
            "status": "error",
            "files_written": [],
            "summary": "Failed to run",
            "error": "ImportError",
        })

        result = parse_uas_output("some output with errors")
        assert result.status == "error"
        assert result.error == "ImportError"

    @patch("uas.fuzzy.anthropic.Anthropic")
    @patch("uas.fuzzy.config.get")
    def test_malformed_response(self, mock_cfg, mock_anthropic_cls):
        from orchestrator.main import parse_uas_output
        parse_uas_output.cache_clear()
        mock_cfg.side_effect = lambda key, default=None: {
            "fuzzy_enabled": True,
            "fuzzy_model": "test-model",
        }.get(key, default)

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_response("garbage")

        with pytest.raises(Exception):
            parse_uas_output("any stdout")


# ---------------------------------------------------------------------------
# Concrete fuzzy function tests: assess_code_quality
# ---------------------------------------------------------------------------


class TestAssessCodeQuality:
    @patch("uas.fuzzy.anthropic.Anthropic")
    @patch("uas.fuzzy.config.get")
    def test_happy_path(self, mock_cfg, mock_anthropic_cls):
        from orchestrator.main import assess_code_quality
        assess_code_quality.cache_clear()
        mock_cfg.side_effect = lambda key, default=None: {
            "fuzzy_enabled": True,
            "fuzzy_model": "test-model",
        }.get(key, default)

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _json_response({
            "has_uas_result": True,
            "has_input_call": False,
            "is_file_modification": False,
            "missing_imports": [],
        })

        result = assess_code_quality("print('hello')", "write hello world")
        assert isinstance(result, CodeQuality)
        assert result.has_uas_result is True
        assert result.has_input_call is False

    @patch("uas.fuzzy.anthropic.Anthropic")
    @patch("uas.fuzzy.config.get")
    def test_detects_issues(self, mock_cfg, mock_anthropic_cls):
        from orchestrator.main import assess_code_quality
        assess_code_quality.cache_clear()
        mock_cfg.side_effect = lambda key, default=None: {
            "fuzzy_enabled": True,
            "fuzzy_model": "test-model",
        }.get(key, default)

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _json_response({
            "has_uas_result": False,
            "has_input_call": True,
            "is_file_modification": True,
            "missing_imports": ["requests"],
        })

        result = assess_code_quality("x = input()", "modify config.py")
        assert result.has_uas_result is False
        assert result.has_input_call is True
        assert result.is_file_modification is True
        assert result.missing_imports == ["requests"]

    @patch("uas.fuzzy.anthropic.Anthropic")
    @patch("uas.fuzzy.config.get")
    def test_api_error_propagates(self, mock_cfg, mock_anthropic_cls):
        from orchestrator.main import assess_code_quality
        assess_code_quality.cache_clear()
        mock_cfg.side_effect = lambda key, default=None: {
            "fuzzy_enabled": True,
            "fuzzy_model": "test-model",
        }.get(key, default)

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.side_effect = ConnectionError("offline")

        with pytest.raises(ConnectionError):
            assess_code_quality("code", "task")


# ---------------------------------------------------------------------------
# Concrete fuzzy function tests: evaluate_sandbox
# ---------------------------------------------------------------------------


class TestEvaluateSandbox:
    @patch("uas.fuzzy.anthropic.Anthropic")
    @patch("uas.fuzzy.config.get")
    def test_success(self, mock_cfg, mock_anthropic_cls):
        from orchestrator.main import evaluate_sandbox
        evaluate_sandbox.cache_clear()
        mock_cfg.side_effect = lambda key, default=None: {
            "fuzzy_enabled": True,
            "fuzzy_model": "test-model",
        }.get(key, default)

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _json_response({
            "success": True,
            "revert_needed": False,
            "error_category": None,
            "summary": "All good",
        })

        result = evaluate_sandbox("output ok", "", 0)
        assert isinstance(result, ExecutionResult)
        assert result.success is True
        assert result.revert_needed is False

    @patch("uas.fuzzy.anthropic.Anthropic")
    @patch("uas.fuzzy.config.get")
    def test_failure_with_revert(self, mock_cfg, mock_anthropic_cls):
        from orchestrator.main import evaluate_sandbox
        evaluate_sandbox.cache_clear()
        mock_cfg.side_effect = lambda key, default=None: {
            "fuzzy_enabled": True,
            "fuzzy_model": "test-model",
        }.get(key, default)

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _json_response({
            "success": False,
            "revert_needed": True,
            "error_category": "syntax_error",
            "summary": "SyntaxError in generated code",
        })

        result = evaluate_sandbox("", "SyntaxError: invalid", 1)
        assert result.success is False
        assert result.revert_needed is True
        assert result.error_category == "syntax_error"

    @patch("uas.fuzzy.anthropic.Anthropic")
    @patch("uas.fuzzy.config.get")
    def test_malformed_response(self, mock_cfg, mock_anthropic_cls):
        from orchestrator.main import evaluate_sandbox
        evaluate_sandbox.cache_clear()
        mock_cfg.side_effect = lambda key, default=None: {
            "fuzzy_enabled": True,
            "fuzzy_model": "test-model",
        }.get(key, default)

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_response("{invalid json")

        with pytest.raises(Exception):
            evaluate_sandbox("stdout", "stderr", 1)


# ---------------------------------------------------------------------------
# Concrete fuzzy function tests: parse_sandbox_output
# ---------------------------------------------------------------------------


class TestParseSandboxOutput:
    @patch("uas.fuzzy.anthropic.Anthropic")
    @patch("uas.fuzzy.config.get")
    def test_happy_path(self, mock_cfg, mock_anthropic_cls):
        from architect.executor import parse_sandbox_output
        parse_sandbox_output.cache_clear()
        mock_cfg.side_effect = lambda key, default=None: {
            "fuzzy_enabled": True,
            "fuzzy_model": "test-model",
        }.get(key, default)

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _json_response({
            "stdout": "hello world",
            "stderr": "",
            "uas_result": {"status": "ok"},
        })

        result = parse_sandbox_output(raw="stdout:\nhello world\n")
        assert isinstance(result, SandboxOutput)
        assert result.stdout == "hello world"
        assert result.uas_result == {"status": "ok"}

    @patch("uas.fuzzy.anthropic.Anthropic")
    @patch("uas.fuzzy.config.get")
    def test_no_uas_result(self, mock_cfg, mock_anthropic_cls):
        from architect.executor import parse_sandbox_output
        parse_sandbox_output.cache_clear()
        mock_cfg.side_effect = lambda key, default=None: {
            "fuzzy_enabled": True,
            "fuzzy_model": "test-model",
        }.get(key, default)

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _json_response({
            "stdout": "some output",
            "stderr": "warning: something",
            "uas_result": None,
        })

        result = parse_sandbox_output(raw="raw text without result")
        assert result.uas_result is None
        assert result.stderr == "warning: something"

    @patch("uas.fuzzy.anthropic.Anthropic")
    @patch("uas.fuzzy.config.get")
    def test_fuzzy_extract_wrapper_fallback(self, mock_cfg, mock_anthropic_cls):
        """_fuzzy_extract returns empty SandboxOutput on API error."""
        from architect.executor import _fuzzy_extract, parse_sandbox_output
        parse_sandbox_output.cache_clear()
        mock_cfg.side_effect = lambda key, default=None: {
            "fuzzy_enabled": True,
            "fuzzy_model": "test-model",
        }.get(key, default)

        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.side_effect = RuntimeError("API down")

        result = _fuzzy_extract("some output")
        assert isinstance(result, SandboxOutput)
        assert result.stdout == ""
        assert result.stderr == ""
        assert result.uas_result is None


# ---------------------------------------------------------------------------
# Pydantic model validation
# ---------------------------------------------------------------------------


class TestFuzzyModels:
    """Tests that model schemas reject invalid data."""

    def test_execution_result_requires_summary(self):
        with pytest.raises(Exception):
            ExecutionResult(success=True, revert_needed=False)

    def test_uas_result_rejects_invalid_status(self):
        with pytest.raises(Exception):
            UASResult(status="invalid", summary="x")

    def test_error_classification_rejects_invalid_category(self):
        with pytest.raises(Exception):
            ErrorClassification(
                category="nonexistent",
                retryable=True,
                recommended_backoff=0,
                message="x",
            )

    def test_sandbox_output_defaults(self):
        s = SandboxOutput()
        assert s.stdout == ""
        assert s.stderr == ""
        assert s.uas_result is None

    def test_code_quality_defaults(self):
        q = CodeQuality(
            has_uas_result=True,
            has_input_call=False,
            is_file_modification=False,
        )
        assert q.missing_imports == []

    def test_uas_result_defaults(self):
        u = UASResult(status="ok", summary="done")
        assert u.files_written == []
        assert u.error is None
