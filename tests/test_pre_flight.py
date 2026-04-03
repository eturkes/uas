"""Tests for Section 4: LLM Pre-Flight Review."""

import json
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.main import pre_execution_check, pre_execution_check_llm


class TestPreFlightLLM:
    def test_llm_identifies_missing_pip_install(self):
        code = (
            'import pandas\n'
            'import json\n'
            'print(f"UAS_RESULT: {json.dumps({})}")\n'
        )
        llm_response = json.dumps({
            "issues": [
                {"description": "pandas is imported but never pip-installed", "severity": "critical"}
            ],
            "safe_to_run": False,
        })
        mock_client = MagicMock()
        mock_client.generate.return_value = (llm_response, {"input": 0, "output": 0})

        with patch("orchestrator.main.get_llm_client", return_value=mock_client):
            errors, warnings = pre_execution_check_llm(code, "analyze data")

        assert any("pandas" in e for e in errors)

    def test_llm_identifies_missing_workspace_path(self):
        code = (
            'import json, subprocess, sys\n'
            'subprocess.run([sys.executable, "-m", "pip", "install", "requests==2.32.3"], check=True)\n'
            'with open("/workspace/data.csv") as f:\n'
            '    data = f.read()\n'
            'print(f"UAS_RESULT: {json.dumps({})}")\n'
        )
        llm_response = json.dumps({
            "issues": [
                {"description": "Uses hardcoded /workspace instead of os.path.join(workspace, ...)", "severity": "warning"}
            ],
            "safe_to_run": True,
        })
        mock_client = MagicMock()
        mock_client.generate.return_value = (llm_response, {"input": 0, "output": 0})

        with patch("orchestrator.main.get_llm_client", return_value=mock_client):
            errors, warnings = pre_execution_check_llm(code, "read data")

        assert len(errors) == 0
        assert any("workspace" in w.lower() for w in warnings)

    def test_llm_failure_falls_back_to_heuristic(self):
        code = 'print("UAS_RESULT: ok")'
        mock_client = MagicMock()
        mock_client.generate.side_effect = RuntimeError("API down")

        with patch("orchestrator.main.get_llm_client", return_value=mock_client):
            errors, warnings = pre_execution_check_llm(code, "some task")

        assert errors == []
        assert warnings == []

    def test_heuristic_still_catches_syntax_error(self):
        code = "def foo(\n"
        errors, warnings = pre_execution_check_llm(code, "some task")
        assert any("Syntax error" in e for e in errors)

    def test_minimal_mode_skips_llm(self):
        code = (
            'import pandas\n'
            'print(f"UAS_RESULT: ok")\n'
        )
        with patch("orchestrator.main.MINIMAL_MODE", True), \
             patch("orchestrator.main.get_llm_client") as mock_factory:
            from orchestrator.main import pre_execution_check as pec
            errors, warnings = pec(code)
            mock_factory.assert_not_called()

    def test_llm_good_code_no_issues(self):
        code = (
            'import json, os, subprocess, sys\n'
            'subprocess.run([sys.executable, "-m", "pip", "install", "requests==2.32.3"], check=True)\n'
            'workspace = os.environ.get("WORKSPACE", "/workspace")\n'
            'print(f"UAS_RESULT: {json.dumps({})}")\n'
        )
        llm_response = json.dumps({"issues": [], "safe_to_run": True})
        mock_client = MagicMock()
        mock_client.generate.return_value = (llm_response, {"input": 0, "output": 0})

        with patch("orchestrator.main.get_llm_client", return_value=mock_client):
            errors, warnings = pre_execution_check_llm(code, "simple task")

        assert errors == []
        assert warnings == []

    def test_llm_response_in_code_fence(self):
        code = 'print(f"UAS_RESULT: ok")\n'
        llm_response = '```json\n{"issues": [{"description": "no pip install", "severity": "critical"}], "safe_to_run": false}\n```'
        mock_client = MagicMock()
        mock_client.generate.return_value = (llm_response, {"input": 0, "output": 0})

        with patch("orchestrator.main.get_llm_client", return_value=mock_client):
            errors, warnings = pre_execution_check_llm(code, "task")

        assert any("pip install" in e for e in errors)

    def test_heuristic_syntax_error_skips_llm(self):
        code = "def foo(\n"
        with patch("orchestrator.main.get_llm_client") as mock_factory:
            errors, warnings = pre_execution_check_llm(code, "task")
            mock_factory.assert_not_called()
        assert any("Syntax error" in e for e in errors)

    def test_safe_to_run_false_without_critical_issues(self):
        code = 'print(f"UAS_RESULT: ok")\n'
        llm_response = json.dumps({"issues": [], "safe_to_run": False})
        mock_client = MagicMock()
        mock_client.generate.return_value = (llm_response, {"input": 0, "output": 0})

        with patch("orchestrator.main.get_llm_client", return_value=mock_client):
            errors, warnings = pre_execution_check_llm(code, "task")

        assert len(errors) == 1
        assert "not safe to run" in errors[0].lower()

    def test_event_log_emissions(self):
        code = 'print(f"UAS_RESULT: ok")\n'
        llm_response = json.dumps({"issues": [], "safe_to_run": True})
        mock_client = MagicMock()
        mock_client.generate.return_value = (llm_response, {"input": 0, "output": 0})
        mock_event_log = MagicMock()

        with patch("orchestrator.main.get_llm_client", return_value=mock_client), \
             patch("architect.events.get_event_log", return_value=mock_event_log):
            pre_execution_check_llm(code, "task")

        assert mock_event_log.emit.call_count == 2
