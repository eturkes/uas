"""Tests for Section 11: LLM-Targeted Dependency Output Distillation."""

from unittest.mock import MagicMock, patch

from architect.main import (
    _distill_dependency_output,
    _distill_dependency_output_llm,
    TARGETED_DISTILL_PROMPT,
)


class TestTargetedDistillPrompt:
    def test_prompt_has_placeholders(self):
        assert "{dep_id}" in TARGETED_DISTILL_PROMPT
        assert "{dep_title}" in TARGETED_DISTILL_PROMPT
        assert "{files}" in TARGETED_DISTILL_PROMPT
        assert "{output_preview}" in TARGETED_DISTILL_PROMPT
        assert "{consumer_desc}" in TARGETED_DISTILL_PROMPT

    def test_prompt_formats_without_error(self):
        result = TARGETED_DISTILL_PROMPT.format(
            dep_id=1,
            dep_title="Download data",
            files="data.csv",
            output_preview="Downloaded 100 rows",
            consumer_desc="Analyze the CSV data",
        )
        assert "Download data" in result
        assert "Analyze the CSV data" in result


class TestDistillDependencyOutputLlm:
    @patch("orchestrator.llm_client.get_llm_client")
    def test_llm_extracts_csv_schema(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.generate.return_value = (
            "File: data.csv\nColumns: name (str), age (int), salary (float)"
        )
        mock_get_client.return_value = mock_client

        dep_step = {
            "title": "Download data",
            "summary": "Downloaded CSV with columns name, age, salary",
            "files_written": ["data.csv"],
            "verify": "",
        }
        output = {"stdout": "Downloaded 100 rows to data.csv", "stderr": ""}

        result = _distill_dependency_output_llm(
            1, dep_step, output, "Analyze the data and compute average salary",
        )
        assert "<dependency" in result
        assert "data.csv" in result
        prompt = mock_client.generate.call_args[0][0]
        assert "Analyze the data and compute average salary" in prompt

    @patch("orchestrator.llm_client.get_llm_client")
    def test_llm_extracts_file_path(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.generate.return_value = "File path: output.json"
        mock_get_client.return_value = mock_client

        dep_step = {
            "title": "Generate config",
            "summary": "Created config file",
            "files_written": ["output.json"],
            "verify": "",
        }
        output = "Config written to output.json"

        result = _distill_dependency_output_llm(
            1, dep_step, output, "Read the output file and validate it",
        )
        assert "<dependency" in result
        assert "output.json" in result

    @patch("orchestrator.llm_client.get_llm_client")
    def test_llm_failure_falls_back_to_template(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.generate.side_effect = RuntimeError("API down")
        mock_get_client.return_value = mock_client

        dep_step = {
            "title": "Prep",
            "summary": "Prepared data",
            "files_written": ["out.txt"],
            "verify": "",
        }
        output = {"stdout": "raw output", "stderr": ""}

        result = _distill_dependency_output_llm(
            1, dep_step, output, "process the data",
        )
        expected = _distill_dependency_output(1, dep_step, output)
        assert result == expected

    @patch("orchestrator.llm_client.get_llm_client")
    def test_import_failure_falls_back_to_template(self, mock_get_client):
        mock_get_client.side_effect = ImportError("no module")

        dep_step = {
            "title": "Step A",
            "summary": "Did something",
            "files_written": [],
            "verify": "",
        }
        output = "some output"

        result = _distill_dependency_output_llm(
            1, dep_step, output, "use output of step A",
        )
        expected = _distill_dependency_output(1, dep_step, output)
        assert result == expected

    @patch("orchestrator.llm_client.get_llm_client")
    def test_output_more_concise_than_template(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.generate.return_value = "File: data.csv"
        mock_get_client.return_value = mock_client

        dep_step = {
            "title": "Download dataset",
            "summary": "Downloaded a large dataset with many columns and rows",
            "files_written": ["data.csv", "metadata.json", "schema.txt"],
            "verify": "check data.csv exists",
        }
        output = {"stdout": "x" * 500, "stderr": "some warning " * 10}

        template_result = _distill_dependency_output(1, dep_step, output)
        llm_result = _distill_dependency_output_llm(
            1, dep_step, output, "read data.csv",
        )
        assert len(llm_result) < len(template_result)

    @patch("orchestrator.llm_client.get_llm_client")
    def test_empty_llm_response_falls_back(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.generate.return_value = ""
        mock_get_client.return_value = mock_client

        dep_step = {
            "title": "Step 1",
            "summary": "summary text",
            "files_written": ["f.txt"],
            "verify": "",
        }
        output = "output text"

        result = _distill_dependency_output_llm(
            1, dep_step, output, "next step desc",
        )
        expected = _distill_dependency_output(1, dep_step, output)
        assert result == expected

    @patch("orchestrator.llm_client.get_llm_client")
    def test_verification_preserved_in_llm_output(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.generate.return_value = "File: out.txt"
        mock_get_client.return_value = mock_client

        dep_step = {
            "title": "Build",
            "summary": "Built artifact",
            "files_written": ["out.txt"],
            "verify": "check out.txt exists",
        }
        output = "built successfully"

        result = _distill_dependency_output_llm(
            1, dep_step, output, "deploy the artifact",
        )
        assert "<verification>" in result
        assert "check out.txt exists" in result

    @patch("orchestrator.llm_client.get_llm_client")
    def test_events_emitted(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.generate.return_value = "summary"
        mock_get_client.return_value = mock_client

        dep_step = {
            "title": "Step",
            "summary": "done",
            "files_written": [],
            "verify": "",
        }

        with patch("architect.main.get_event_log") as mock_event_log:
            mock_log = MagicMock()
            mock_event_log.return_value = mock_log

            _distill_dependency_output_llm(1, dep_step, "out", "desc")

            calls = mock_log.emit.call_args_list
            purposes = [c[1]["data"]["purpose"] for c in calls]
            assert "targeted_distill" in purposes
            assert len([p for p in purposes if p == "targeted_distill"]) == 2
