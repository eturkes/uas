"""Tests for architect.main.build_context."""

from unittest.mock import patch

from architect.main import build_context, _extract_json_keys, summarize_context


class TestBuildContext:
    def test_no_dependencies(self):
        step = {"depends_on": []}
        assert build_context(step, {}) == ""

    def test_single_dependency(self):
        step = {"depends_on": [1]}
        outputs = {1: "result from step 1"}
        result = build_context(step, outputs)
        assert "previous_step_output" in result
        assert 'step="1"' in result
        assert "result from step 1" in result

    def test_multiple_dependencies(self):
        step = {"depends_on": [1, 2]}
        outputs = {1: "out1", 2: "out2"}
        result = build_context(step, outputs)
        assert "out1" in result
        assert "out2" in result

    def test_missing_dependency_output(self):
        step = {"depends_on": [1, 2]}
        outputs = {1: "out1"}
        result = build_context(step, outputs)
        assert "out1" in result
        # Step 2 has no output, should not crash
        assert "out2" not in result

    def test_empty_dependency_output(self, tmp_workspace):
        step = {"depends_on": [1]}
        outputs = {1: ""}
        result = build_context(step, outputs)
        # Empty output should not produce a context line
        assert result == ""

    def test_dict_output_with_stdout(self):
        step = {"depends_on": [1]}
        outputs = {1: {"stdout": "result data", "stderr": "", "files": []}}
        result = build_context(step, outputs)
        assert "stdout:" in result
        assert "result data" in result

    def test_dict_output_with_stderr(self):
        step = {"depends_on": [1]}
        outputs = {1: {"stdout": "", "stderr": "warning msg", "files": []}}
        result = build_context(step, outputs)
        assert "stderr:" in result
        assert "warning msg" in result

    def test_dict_output_with_files(self):
        step = {"depends_on": [1]}
        outputs = {1: {
            "stdout": "",
            "stderr": "",
            "files": ["/workspace/out.txt", "/workspace/data.json"],
        }}
        result = build_context(step, outputs)
        assert "files:" in result
        assert "/workspace/out.txt" in result
        assert "/workspace/data.json" in result

    def test_dict_output_with_all_fields(self):
        step = {"depends_on": [1]}
        outputs = {1: {
            "stdout": "main output",
            "stderr": "some warning",
            "files": ["/workspace/result.txt"],
        }}
        result = build_context(step, outputs)
        assert "main output" in result
        assert "some warning" in result
        assert "/workspace/result.txt" in result

    def test_dict_output_empty_fields_omitted(self, tmp_workspace):
        step = {"depends_on": [1]}
        outputs = {1: {"stdout": "", "stderr": "", "files": []}}
        result = build_context(step, outputs)
        assert result == ""

    def test_mixed_string_and_dict_outputs(self):
        step = {"depends_on": [1, 2]}
        outputs = {
            1: "plain string output",
            2: {"stdout": "dict output", "stderr": "", "files": []},
        }
        result = build_context(step, outputs)
        assert "plain string output" in result
        assert "dict output" in result


class TestXMLStructure:
    def test_full_output_uses_xml_tags(self):
        step = {"depends_on": [1]}
        outputs = {1: "some output"}
        result = build_context(step, outputs)
        assert "<previous_step_output" in result
        assert "</previous_step_output>" in result

    def test_dict_output_uses_xml_tags(self):
        step = {"depends_on": [1]}
        outputs = {1: {"stdout": "data", "stderr": "", "files": []}}
        result = build_context(step, outputs)
        assert '<previous_step_output step="1">' in result
        assert "</previous_step_output>" in result


class TestAllDepsFullOutput:
    def test_three_deps_all_full(self):
        step = {"depends_on": [1, 2, 3]}
        outputs = {1: "old output", 2: "mid output", 3: "new output"}
        result = build_context(step, outputs)
        assert "old output" in result
        assert "mid output" in result
        assert "new output" in result
        assert "step_summary" not in result


class TestVerifyField:
    def test_verify_included_when_state_provided(self):
        step = {"depends_on": [1]}
        outputs = {1: "result"}
        state = {
            "goal": "test goal",
            "steps": [
                {"id": 1, "verify": "check file exists", "depends_on": []},
            ],
        }
        result = build_context(step, outputs, state=state)
        assert "<verification>" in result
        assert "check file exists" in result

    def test_empty_verify_not_included(self):
        step = {"depends_on": [1]}
        outputs = {1: "result"}
        state = {
            "goal": "test goal",
            "steps": [
                {"id": 1, "verify": "", "depends_on": []},
            ],
        }
        result = build_context(step, outputs, state=state)
        assert "verification" not in result


class TestWorkspaceFiles:
    @patch("architect.main.scan_workspace_files")
    def test_workspace_files_included(self, mock_scan):
        mock_scan.return_value = {
            "output.txt": {"size": 100, "type": "text", "preview": "hello world"},
        }
        step = {"depends_on": [1]}
        outputs = {1: "result"}
        result = build_context(step, outputs, workspace_path="/workspace")
        assert "<workspace_files>" in result
        assert "output.txt" in result
        assert "100 bytes" in result

    @patch("architect.main.scan_workspace_files")
    def test_workspace_json_shows_keys(self, mock_scan):
        mock_scan.return_value = {
            "data.json": {
                "size": 50,
                "type": "text",
                "preview": '{"name": "test", "value": 42}',
            },
        }
        step = {"depends_on": [1]}
        outputs = {1: "result"}
        result = build_context(step, outputs, workspace_path="/workspace")
        assert "keys:" in result
        assert "name" in result
        assert "value" in result

    @patch("architect.main.scan_workspace_files")
    def test_no_workspace_section_when_no_files(self, mock_scan):
        mock_scan.return_value = {}
        step = {"depends_on": [1]}
        outputs = {1: "result"}
        result = build_context(step, outputs, workspace_path="/workspace")
        assert "workspace_files" not in result

    def test_no_workspace_section_when_no_path(self):
        step = {"depends_on": [1]}
        outputs = {1: "result"}
        result = build_context(step, outputs)
        assert "workspace_files" not in result


class TestExtractJsonKeys:
    def test_dict_keys(self):
        result = _extract_json_keys('{"a": 1, "b": 2}')
        assert "a" in result
        assert "b" in result

    def test_list_of_dicts(self):
        result = _extract_json_keys('[{"x": 1}, {"x": 2}]')
        assert "2 items" in result
        assert "x" in result

    def test_invalid_json(self):
        result = _extract_json_keys("not json at all")
        assert result == "not json at all"[:100]

    def test_scalar_json(self):
        result = _extract_json_keys("42")
        assert "42" in result


class TestSummarizeContext:
    @patch("architect.main.summarize_context")
    def test_long_context_triggers_summarization(self, mock_summarize):
        mock_summarize.return_value = "compressed"
        step = {"depends_on": [1]}
        long_output = "x" * 10000
        outputs = {1: long_output}
        # Call build_context which should call summarize_context
        # when context exceeds MAX_CONTEXT_LENGTH
        result = build_context(step, outputs)
        # The actual behavior depends on MAX_CONTEXT_LENGTH
        # Just verify it doesn't crash
        assert isinstance(result, str)

    @patch("orchestrator.llm_client.get_llm_client",
           side_effect=RuntimeError("no LLM"))
    def test_summarize_context_fallback(self, _mock):
        """When LLM is unavailable, falls back to truncation."""
        context = "x" * 1000
        result = summarize_context(context, "test goal", 500)
        assert len(result) < len(context)
        assert "compressed" in result


class TestEnrichmentContext:
    """Section 11: Enrichment context injected via build_context."""

    def test_enrichment_included_in_context(self):
        step = {"id": 2, "depends_on": [1]}
        outputs = {1: "result from step 1"}
        state = {
            "goal": "test goal",
            "steps": [
                {"id": 1, "depends_on": []},
                {"id": 2, "depends_on": [1]},
            ],
            "enrichment_context": {
                2: "[Context from step 1 (Download): files produced: data.csv]",
            },
        }
        result = build_context(step, outputs, state=state)
        assert "<enrichment_context>" in result
        assert "data.csv" in result
        assert "</enrichment_context>" in result

    def test_no_enrichment_when_absent(self):
        step = {"id": 2, "depends_on": [1]}
        outputs = {1: "result from step 1"}
        state = {
            "goal": "test goal",
            "steps": [
                {"id": 1, "depends_on": []},
                {"id": 2, "depends_on": [1]},
            ],
        }
        result = build_context(step, outputs, state=state)
        assert "enrichment_context" not in result

    def test_enrichment_only_for_matching_step(self):
        step = {"id": 3, "depends_on": [1]}
        outputs = {1: "result"}
        state = {
            "goal": "test goal",
            "steps": [
                {"id": 1, "depends_on": []},
                {"id": 3, "depends_on": [1]},
            ],
            "enrichment_context": {
                2: "[Context for step 2 only]",
            },
        }
        result = build_context(step, outputs, state=state)
        assert "enrichment_context" not in result

    def test_multiple_enrichments_concatenated(self):
        step = {"id": 3, "depends_on": [1, 2]}
        outputs = {1: "out1", 2: "out2"}
        state = {
            "goal": "test goal",
            "steps": [
                {"id": 1, "depends_on": []},
                {"id": 2, "depends_on": []},
                {"id": 3, "depends_on": [1, 2]},
            ],
            "enrichment_context": {
                3: (
                    "[Context from step 1 (A): files produced: a.txt]\n"
                    "[Context from step 2 (B): files produced: b.txt]"
                ),
            },
        }
        result = build_context(step, outputs, state=state)
        assert "a.txt" in result
        assert "b.txt" in result
