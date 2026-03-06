"""Tests for best-practice guardrail checks."""

import os

import pytest

from architect.main import check_guardrails, check_project_guardrails


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

    def test_git_init_with_branch_ok(self):
        code = 'subprocess.run(["git", "init", "-b", "main"])\n'
        violations = check_guardrails(code)
        assert not any("git init" in v["description"] for v in violations)

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
