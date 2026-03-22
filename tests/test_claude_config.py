"""Tests for orchestrator/claude_config.py template and context formatting."""

import pytest

from orchestrator.claude_config import (
    CLAUDE_MD_TEMPLATE,
    _collect_module_apis,
    _format_step_context,
    get_claude_md_content,
)


class TestTemplate:
    """Verify the CLAUDE.md template content."""

    def test_contains_import_conventions_section(self):
        assert "## Import Conventions" in CLAUDE_MD_TEMPLATE

    def test_import_conventions_after_coding_standards(self):
        cs_pos = CLAUDE_MD_TEMPLATE.index("## Coding Standards")
        ic_pos = CLAUDE_MD_TEMPLATE.index("## Import Conventions")
        or_pos = CLAUDE_MD_TEMPLATE.index("## Output Requirements")
        assert cs_pos < ic_pos < or_pos

    def test_import_conventions_forbids_fallback_chains(self):
        assert "NEVER use try/except ImportError fallback chains" in CLAUDE_MD_TEMPLATE

    def test_import_conventions_exact_names(self):
        assert "use the EXACT" in CLAUDE_MD_TEMPLATE

    def test_template_is_valid_markdown(self):
        # Basic structural check: every ## heading is preceded by a blank line
        # (except the very first one)
        lines = CLAUDE_MD_TEMPLATE.split("\n")
        for i, line in enumerate(lines):
            if line.startswith("## ") and i > 0:
                # Previous line should be blank or start of file
                prev = lines[i - 1].strip()
                assert prev == "", (
                    f"Heading '{line}' at line {i} not preceded by blank line"
                )


class TestCollectModuleApis:
    """Tests for _collect_module_apis helper."""

    def test_empty_prior_steps(self):
        assert _collect_module_apis([]) == []

    def test_steps_without_module_apis(self):
        steps = [{"id": 1, "title": "Setup", "files": ["README.md"]}]
        assert _collect_module_apis(steps) == []

    def test_collects_apis_from_prior_steps(self):
        steps = [
            {
                "id": 1,
                "title": "Layout",
                "module_apis": {
                    "src/layout.py": {
                        "functions": ["make_card", "make_dropdown"],
                        "classes": [],
                        "constants": ["CARD_STYLE"],
                    }
                },
            }
        ]
        result = _collect_module_apis(steps)
        assert len(result) == 1
        assert result[0][0] == "src/layout.py"
        assert "make_card" in result[0][1]["functions"]

    def test_skips_empty_apis(self):
        steps = [
            {
                "id": 1,
                "title": "Data",
                "module_apis": {
                    "data/raw.csv": {
                        "functions": [],
                        "classes": [],
                        "constants": [],
                    }
                },
            }
        ]
        assert _collect_module_apis(steps) == []

    def test_multiple_steps_multiple_files(self):
        steps = [
            {
                "id": 1,
                "title": "Styles",
                "module_apis": {
                    "src/styles.py": {
                        "functions": ["apply_theme"],
                        "classes": [],
                        "constants": ["COLORS"],
                    }
                },
            },
            {
                "id": 2,
                "title": "Layout",
                "module_apis": {
                    "src/layout.py": {
                        "functions": ["make_card"],
                        "classes": ["Dashboard"],
                        "constants": [],
                    }
                },
            },
        ]
        result = _collect_module_apis(steps)
        assert len(result) == 2
        paths = [r[0] for r in result]
        assert "src/styles.py" in paths
        assert "src/layout.py" in paths


class TestFormatStepContext:
    """Tests for _format_step_context()."""

    def test_basic_context(self):
        ctx = {
            "step_number": 3,
            "total_steps": 7,
            "step_title": "Build dashboard",
            "dependencies": [1, 2],
        }
        output = _format_step_context(ctx)
        assert "## Current Task Context" in output
        assert "Step:** 3 of 7" in output
        assert "Build dashboard" in output
        assert "steps [1, 2]" in output

    def test_no_dependencies(self):
        ctx = {
            "step_number": 1,
            "total_steps": 5,
            "step_title": "Init",
            "dependencies": [],
        }
        output = _format_step_context(ctx)
        assert "none (independent step)" in output

    def test_prior_steps_output(self):
        ctx = {
            "step_number": 2,
            "total_steps": 3,
            "step_title": "Build UI",
            "dependencies": [1],
            "prior_steps": [
                {
                    "id": 1,
                    "title": "Create styles",
                    "summary": "Created style constants",
                    "files": ["src/styles.py"],
                }
            ],
        }
        output = _format_step_context(ctx)
        assert "### Prior Steps Output" in output
        assert "Create styles" in output
        assert "src/styles.py" in output

    def test_includes_module_apis_section(self):
        ctx = {
            "step_number": 3,
            "total_steps": 5,
            "step_title": "Build tabs",
            "dependencies": [1, 2],
            "prior_steps": [
                {
                    "id": 1,
                    "title": "Styles",
                    "module_apis": {
                        "src/styles.py": {
                            "functions": ["apply_chart_theme"],
                            "classes": [],
                            "constants": ["COLOR_PALETTE", "CHART_COLORS"],
                        }
                    },
                },
                {
                    "id": 2,
                    "title": "Layout",
                    "module_apis": {
                        "src/layout_components.py": {
                            "functions": ["make_card", "create_kpi_card"],
                            "classes": [],
                            "constants": [],
                        }
                    },
                },
            ],
        }
        output = _format_step_context(ctx)
        assert "### Available Module APIs" in output
        assert "`src/styles.py`" in output
        assert "apply_chart_theme" in output
        assert "COLOR_PALETTE" in output
        assert "`src/layout_components.py`" in output
        assert "make_card" in output

    def test_no_module_apis_section_when_none(self):
        ctx = {
            "step_number": 1,
            "total_steps": 3,
            "step_title": "Init",
            "dependencies": [],
            "prior_steps": [],
        }
        output = _format_step_context(ctx)
        assert "### Available Module APIs" not in output

    def test_module_apis_shows_classes(self):
        ctx = {
            "step_number": 2,
            "total_steps": 3,
            "step_title": "Build",
            "dependencies": [1],
            "prior_steps": [
                {
                    "id": 1,
                    "title": "Models",
                    "module_apis": {
                        "src/models.py": {
                            "functions": [],
                            "classes": ["Patient", "Record"],
                            "constants": [],
                        }
                    },
                }
            ],
        }
        output = _format_step_context(ctx)
        assert "classes=[Patient, Record]" in output


class TestGetClaudeMdContent:
    """Tests for get_claude_md_content()."""

    def test_without_context(self):
        content = get_claude_md_content()
        assert content == CLAUDE_MD_TEMPLATE
        assert "## Import Conventions" in content

    def test_with_context_appends_section(self):
        ctx = {
            "step_number": 1,
            "total_steps": 3,
            "step_title": "Init",
            "dependencies": [],
        }
        content = get_claude_md_content(step_context=ctx)
        assert content.startswith(CLAUDE_MD_TEMPLATE)
        assert "## Current Task Context" in content

    def test_with_module_apis_in_context(self):
        ctx = {
            "step_number": 2,
            "total_steps": 3,
            "step_title": "Build UI",
            "dependencies": [1],
            "prior_steps": [
                {
                    "id": 1,
                    "title": "Styles",
                    "module_apis": {
                        "src/styles.py": {
                            "functions": ["apply_theme"],
                            "classes": [],
                            "constants": ["COLORS"],
                        }
                    },
                }
            ],
        }
        content = get_claude_md_content(step_context=ctx)
        assert "### Available Module APIs" in content
        assert "apply_theme" in content
