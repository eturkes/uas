"""Tests for architect.spec_generator module."""

import os

import architect.state as state_mod
from architect.spec_generator import generate_spec, build_task_from_spec


class TestGenerateSpec:
    def test_creates_spec_file(self, tmp_workspace):
        step = {
            "id": 1,
            "title": "Create file",
            "description": "Create a hello.txt file",
            "status": "pending",
            "depends_on": [],
            "spec_file": None,
        }
        path = generate_spec(step, total_steps=3)
        assert os.path.exists(path)
        assert path.endswith("step_001.md")

    def test_spec_contains_metadata(self, tmp_workspace):
        step = {
            "id": 2,
            "title": "Process data",
            "description": "Process the CSV",
            "status": "pending",
            "depends_on": [1],
            "spec_file": None,
        }
        path = generate_spec(step, total_steps=5)
        with open(path) as f:
            content = f.read()
        assert "Process data" in content
        assert "Step:** 2 of 5" in content
        assert "Depends On" in content
        assert "Process the CSV" in content

    def test_spec_with_context(self, tmp_workspace):
        step = {
            "id": 1,
            "title": "T",
            "description": "D",
            "status": "pending",
            "depends_on": [],
            "spec_file": None,
        }
        path = generate_spec(step, total_steps=1, context="previous output data")
        with open(path) as f:
            content = f.read()
        assert "previous output data" in content
        assert "## Context" in content

    def test_updates_step_spec_file(self, tmp_workspace):
        step = {
            "id": 1,
            "title": "T",
            "description": "D",
            "status": "pending",
            "depends_on": [],
            "spec_file": None,
        }
        path = generate_spec(step, total_steps=1)
        assert step["spec_file"] == path


class TestBuildTaskFromSpec:
    def test_basic_task(self):
        step = {"description": "Write a script"}
        assert build_task_from_spec(step) == "Write a script"

    def test_task_with_context(self):
        step = {"description": "Write a script"}
        result = build_task_from_spec(step, context="file.txt contains data")
        assert "Write a script" in result
        assert "file.txt contains data" in result

    def test_task_without_context(self):
        step = {"description": "Do something"}
        result = build_task_from_spec(step, context="")
        assert result == "Do something"
