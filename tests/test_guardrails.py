"""Tests for best-practice guardrail checks."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from architect.main import (
    check_guardrails,
    check_guardrails_llm,
    check_project_guardrails,
    check_project_guardrails_llm,
)


class TestCheckGuardrails:
    def test_clean_code_no_violations(self):
        code = '''\
import os
import sys

workspace = os.environ.get("WORKSPACE", "/workspace")

def main():
    """Process data."""
    try:
        with open(os.path.join(workspace, "data.txt"), encoding="utf-8") as f:
            data = f.read()
        print(data)
    except FileNotFoundError:
        print("File not found", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
'''
        violations = check_guardrails(code)
        assert violations == []

    def test_bare_except_detected(self):
        code = "try:\n    x = 1\nexcept:\n    pass\n"
        violations = check_guardrails(code)
        assert any("bare except" in v["description"] for v in violations)

    def test_specific_except_ok(self):
        code = "try:\n    x = 1\nexcept Exception:\n    pass\n"
        violations = check_guardrails(code)
        assert not any("bare except" in v["description"] for v in violations)

    def test_except_value_error_ok(self):
        code = "try:\n    x = 1\nexcept ValueError:\n    pass\n"
        violations = check_guardrails(code)
        assert not any("bare except" in v["description"] for v in violations)

    def test_eval_detected(self):
        code = 'result = eval("1 + 2")\n'
        violations = check_guardrails(code)
        assert any("eval()" in v["description"] for v in violations)

    def test_exec_detected(self):
        code = 'exec("print(1)")\n'
        violations = check_guardrails(code)
        assert any("exec()" in v["description"] for v in violations)

    def test_shell_true_detected(self):
        code = 'subprocess.run("ls", shell=True)\n'
        violations = check_guardrails(code)
        assert any("shell=True" in v["description"] for v in violations)

    def test_http_url_detected(self):
        code = 'url = "http://example.com/data.csv"\n'
        violations = check_guardrails(code)
        assert any("HTTP" in v["description"] for v in violations)

    def test_https_url_ok(self):
        code = 'url = "https://example.com/data.csv"\n'
        violations = check_guardrails(code)
        assert not any("HTTP" in v["description"] for v in violations)

    def test_localhost_http_ok(self):
        code = 'url = "http://localhost:8080/api"\n'
        violations = check_guardrails(code)
        assert not any("HTTP" in v["description"] for v in violations)

    def test_127_http_ok(self):
        code = 'url = "http://127.0.0.1:5000/api"\n'
        violations = check_guardrails(code)
        assert not any("HTTP" in v["description"] for v in violations)

    def test_hardcoded_api_key_detected(self):
        code = 'api_key = "sk-abc123def456ghi789jkl012mno"\n'
        violations = check_guardrails(code)
        assert any("hardcoded secret" in v["description"] for v in violations)
        assert any(v["severity"] == "error" for v in violations)

    def test_env_var_api_key_ok(self):
        code = 'api_key = os.environ.get("API_KEY")\n'
        violations = check_guardrails(code)
        assert not any("hardcoded secret" in v["description"] for v in violations)

    def test_git_init_without_branch_detected(self):
        code = 'subprocess.run(["git", "init"])\n'
        violations = check_guardrails(code)
        assert any("git init" in v["description"] for v in violations)

    def test_git_init_with_branch_also_detected(self):
        code = 'subprocess.run(["git", "init", "-b", "main"])\n'
        violations = check_guardrails(code)
        assert any("git init" in v["description"] for v in violations)

    def test_violations_include_line_numbers(self):
        code = "line1\nline2\ntry:\n    pass\nexcept:\n    pass\n"
        violations = check_guardrails(code)
        assert violations[0]["line"] == 5

    def test_multiple_violations(self):
        code = (
            'eval("1")\n'
            'exec("2")\n'
            'try:\n    pass\nexcept:\n    pass\n'
        )
        violations = check_guardrails(code)
        assert len(violations) >= 3

    def test_aws_key_detected(self):
        code = 'key = "AKIAIOSFODNN7EXAMPLE"\n'
        violations = check_guardrails(code)
        assert any("hardcoded secret" in v["description"] for v in violations)

    def test_github_token_detected(self):
        code = 'token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"\n'
        violations = check_guardrails(code)
        assert any("hardcoded secret" in v["description"] for v in violations)


class TestCheckProjectGuardrails:
    def test_empty_workspace(self, tmp_path):
        warnings = check_project_guardrails(str(tmp_path))
        assert warnings == []

    def test_single_script_no_warnings(self, tmp_path):
        (tmp_path / "script.py").write_text("print('hello')")
        warnings = check_project_guardrails(str(tmp_path))
        assert warnings == []

    def test_multi_file_project_no_git(self, tmp_path):
        (tmp_path / "main.py").write_text("print('hello')")
        (tmp_path / "utils.py").write_text("def helper(): pass")
        warnings = check_project_guardrails(str(tmp_path))
        assert any("Git repository" in w for w in warnings)

    def test_project_with_git_main_branch(self, tmp_path):
        (tmp_path / "main.py").write_text("print('hello')")
        (tmp_path / "utils.py").write_text("def helper(): pass")
        (tmp_path / ".gitignore").write_text("__pycache__/\n")
        (tmp_path / "README.md").write_text("# Project\n")
        (tmp_path / "requirements.txt").write_text("requests==2.32.3\n")
        # Simulate git init -b main
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        warnings = check_project_guardrails(str(tmp_path))
        assert warnings == []

    def test_project_with_master_branch(self, tmp_path):
        (tmp_path / "main.py").write_text("print('hello')")
        (tmp_path / "utils.py").write_text("def helper(): pass")
        (tmp_path / ".gitignore").write_text("__pycache__/\n")
        (tmp_path / "README.md").write_text("# Project\n")
        (tmp_path / "requirements.txt").write_text("requests==2.32.3\n")
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/master\n")
        warnings = check_project_guardrails(str(tmp_path))
        assert any("master" in w for w in warnings)

    def test_project_missing_gitignore(self, tmp_path):
        (tmp_path / "main.py").write_text("print('hello')")
        (tmp_path / "utils.py").write_text("def helper(): pass")
        (tmp_path / "README.md").write_text("# Project\n")
        (tmp_path / "requirements.txt").write_text("requests==2.32.3\n")
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        warnings = check_project_guardrails(str(tmp_path))
        assert any(".gitignore" in w for w in warnings)

    def test_project_missing_readme(self, tmp_path):
        (tmp_path / "main.py").write_text("print('hello')")
        (tmp_path / "utils.py").write_text("def helper(): pass")
        (tmp_path / ".gitignore").write_text("__pycache__/\n")
        (tmp_path / "requirements.txt").write_text("requests==2.32.3\n")
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        warnings = check_project_guardrails(str(tmp_path))
        assert any("README" in w for w in warnings)

    def test_project_missing_requirements(self, tmp_path):
        (tmp_path / "main.py").write_text("print('hello')")
        (tmp_path / "utils.py").write_text("def helper(): pass")
        (tmp_path / ".gitignore").write_text("__pycache__/\n")
        (tmp_path / "README.md").write_text("# Project\n")
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        warnings = check_project_guardrails(str(tmp_path))
        assert any("dependency" in w.lower() for w in warnings)

    def test_pyproject_toml_satisfies_deps_check(self, tmp_path):
        (tmp_path / "main.py").write_text("print('hello')")
        (tmp_path / "utils.py").write_text("def helper(): pass")
        (tmp_path / ".gitignore").write_text("__pycache__/\n")
        (tmp_path / "README.md").write_text("# Project\n")
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        warnings = check_project_guardrails(str(tmp_path))
        assert not any("dependency" in w.lower() for w in warnings)

    def test_setup_py_triggers_project_detection(self, tmp_path):
        (tmp_path / "setup.py").write_text("from setuptools import setup; setup()")
        warnings = check_project_guardrails(str(tmp_path))
        # setup.py presence marks it as a project, so we'd get warnings
        assert any("Git repository" in w for w in warnings)

    def test_nonexistent_workspace(self, tmp_path):
        warnings = check_project_guardrails(str(tmp_path / "nonexistent"))
        assert warnings == []


class TestLLMGuardrailsDefault:
    """Tests for Section 5: LLM guardrails run by default."""

    def _run_guardrail_scan(self, code, minimal=False, env_override=None):
        """Replicate the call-site guardrail decision logic for testing."""
        env = env_override or {}
        use_llm = (
            not minimal
            and env.get("UAS_NO_LLM_GUARDRAILS", "") != "1"
        )
        violations = check_guardrails(code)
        has_regex_errors = any(v["severity"] == "error" for v in violations)
        if use_llm and not has_regex_errors:
            violations = check_guardrails_llm(code)
        return violations

    def test_llm_guardrails_run_by_default(self):
        code = 'import os\nprint("hello")\n'
        llm_response = json.dumps({"violations": [], "clean": True})
        mock_client = MagicMock()
        mock_client.generate.return_value = llm_response

        with patch("orchestrator.llm_client.get_llm_client",
                    return_value=mock_client):
            violations = self._run_guardrail_scan(code)

        mock_client.generate.assert_called_once()
        assert violations == []

    def test_no_llm_guardrails_env_skips_llm(self):
        code = 'import os\nprint("hello")\n'
        with patch("orchestrator.llm_client.get_llm_client") as mock_factory:
            violations = self._run_guardrail_scan(
                code, env_override={"UAS_NO_LLM_GUARDRAILS": "1"}
            )
            mock_factory.assert_not_called()

    def test_minimal_mode_skips_llm(self):
        code = 'import os\nprint("hello")\n'
        with patch("orchestrator.llm_client.get_llm_client") as mock_factory:
            violations = self._run_guardrail_scan(code, minimal=True)
            mock_factory.assert_not_called()

    def test_regex_errors_short_circuit_llm(self):
        code = 'api_key = "sk-abc123def456ghi789jkl012mno"\n'
        with patch("orchestrator.llm_client.get_llm_client") as mock_factory:
            violations = self._run_guardrail_scan(code)
            mock_factory.assert_not_called()
            assert any(v["severity"] == "error" for v in violations)
            assert any("hardcoded secret" in v["description"] for v in violations)


class TestLLMProjectStructure:
    """Tests for Section 6: LLM-assessed project structure checks."""

    def _make_multi_file_project(self, tmp_path):
        (tmp_path / "main.py").write_text("print('hello')")
        (tmp_path / "utils.py").write_text("def helper(): pass")
        (tmp_path / ".gitignore").write_text("__pycache__/\n")
        (tmp_path / "README.md").write_text("# Project\n")
        (tmp_path / "requirements.txt").write_text("requests==2.32.3\n")
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")

    def test_llm_identifies_missing_artifacts(self, tmp_path):
        self._make_multi_file_project(tmp_path)
        llm_response = json.dumps({
            "warnings": ["Missing Dockerfile for web application"],
            "suggestions": ["Consider adding a Makefile"],
        })
        mock_client = MagicMock()
        mock_client.generate.return_value = llm_response

        with patch("orchestrator.llm_client.get_llm_client",
                    return_value=mock_client):
            warnings = check_project_guardrails_llm(
                str(tmp_path), "Build a web API", [
                    {"title": "Set up Flask app", "status": "completed"},
                    {"title": "Add endpoints", "status": "completed"},
                ]
            )

        mock_client.generate.assert_called_once()
        assert len(warnings) == 1
        assert "Dockerfile" in warnings[0]

    def test_llm_no_warnings_for_simple_script(self, tmp_path):
        (tmp_path / "script.py").write_text("print('hello')")
        llm_response = json.dumps({
            "warnings": [],
            "suggestions": [],
        })
        mock_client = MagicMock()
        mock_client.generate.return_value = llm_response

        with patch("orchestrator.llm_client.get_llm_client",
                    return_value=mock_client):
            warnings = check_project_guardrails_llm(
                str(tmp_path), "Print hello world", [
                    {"title": "Write script", "status": "completed"},
                ]
            )

        assert warnings == []

    def test_llm_failure_falls_back_to_heuristic(self, tmp_path):
        (tmp_path / "main.py").write_text("print('hello')")
        (tmp_path / "utils.py").write_text("def helper(): pass")
        mock_client = MagicMock()
        mock_client.generate.side_effect = RuntimeError("LLM unavailable")

        with patch("orchestrator.llm_client.get_llm_client",
                    return_value=mock_client):
            warnings = check_project_guardrails_llm(
                str(tmp_path), "Build a project", []
            )

        assert any("Git repository" in w for w in warnings)

    def test_minimal_mode_uses_heuristic(self, tmp_path):
        self._make_multi_file_project(tmp_path)
        with patch("architect.main.MINIMAL_MODE", True):
            from architect.main import validate_workspace
            state = {
                "goal": "Build a project",
                "steps": [
                    {"title": "Step 1", "status": "completed",
                     "files_written": []},
                ],
            }
            with patch("orchestrator.llm_client.get_llm_client") as mock_factory:
                validate_workspace(state, str(tmp_path))
                mock_factory.assert_not_called()
