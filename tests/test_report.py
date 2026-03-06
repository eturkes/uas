"""Tests for architect.report module."""

import json
import os

from architect.report import (
    generate_report,
    _mermaid_dag,
    _mermaid_provenance,
    _timeline_data,
    _summary_metrics,
    _step_details,
)


def _fixture_state():
    return {
        "goal": "Build a test dashboard",
        "status": "completed",
        "total_elapsed": 42.5,
        "steps": [
            {
                "id": 1,
                "title": "Download data",
                "description": "Download CSV from URL",
                "depends_on": [],
                "status": "completed",
                "elapsed": 15.2,
                "timing": {"llm_time": 8.0, "sandbox_time": 5.0, "total_time": 15.2},
                "output": "Downloaded 100 rows",
                "error": "",
                "rewrites": 0,
                "files_written": ["/workspace/data.csv"],
                "uas_result": {"status": "ok", "files_written": ["data.csv"], "summary": "done"},
                "verify": "data.csv exists",
            },
            {
                "id": 2,
                "title": "Process data",
                "description": "Clean and transform the data",
                "depends_on": [1],
                "status": "completed",
                "elapsed": 20.3,
                "timing": {"llm_time": 12.0, "sandbox_time": 6.0, "total_time": 20.3},
                "output": "Processed 95 rows",
                "error": "",
                "rewrites": 1,
                "files_written": ["/workspace/clean.csv"],
                "uas_result": None,
                "verify": "",
            },
            {
                "id": 3,
                "title": "Generate report",
                "description": "Create summary stats",
                "depends_on": [1, 2],
                "status": "failed",
                "elapsed": 7.0,
                "timing": {"llm_time": 4.0, "sandbox_time": 2.0, "total_time": 7.0},
                "output": "",
                "error": "ImportError: no module named foo",
                "rewrites": 2,
                "files_written": [],
                "uas_result": None,
                "verify": "summary.json exists",
            },
        ],
    }


def _fixture_events():
    return [
        {"timestamp": "2024-01-01T00:00:00Z", "event_type": "goal_received", "data": {"goal": "test"}},
        {"timestamp": "2024-01-01T00:00:01Z", "event_type": "step_start", "step_id": 1},
        {"timestamp": "2024-01-01T00:00:10Z", "event_type": "step_complete", "step_id": 1},
    ]


def _fixture_provenance():
    return {
        "nodes": {
            "abc123": {"id": "abc123", "node_type": "entity", "label": "goal", "content": "test"},
            "def456": {"id": "def456", "node_type": "activity", "label": "decompose", "content": ""},
            "ghi789": {"id": "ghi789", "node_type": "agent", "label": "planner_llm"},
        },
        "edges": [
            {"edge_type": "used", "source": "def456", "target": "abc123"},
            {"edge_type": "wasAssociatedWith", "source": "def456", "target": "ghi789"},
        ],
    }


class TestMermaidDag:
    def test_generates_graph(self):
        state = _fixture_state()
        result = _mermaid_dag(state)
        assert "graph TD" in result
        assert "s1" in result
        assert "s2" in result
        assert "s1 --> s2" in result
        assert "s1 --> s3" in result
        assert "s2 --> s3" in result

    def test_colors_by_status(self):
        state = _fixture_state()
        result = _mermaid_dag(state)
        assert "fill:#28a745" in result  # completed (green)
        assert "fill:#dc3545" in result  # failed (red)

    def test_empty_state(self):
        result = _mermaid_dag({"steps": []})
        assert "No steps" in result

    def test_no_steps_key(self):
        result = _mermaid_dag({})
        assert "No steps" in result


class TestMermaidProvenance:
    def test_generates_graph(self):
        prov = _fixture_provenance()
        result = _mermaid_provenance(prov)
        assert "graph LR" in result
        assert "goal" in result
        assert "decompose" in result
        assert "planner_llm" in result

    def test_empty_provenance(self):
        result = _mermaid_provenance({"nodes": {}, "edges": []})
        assert "No provenance data" in result

    def test_edge_types(self):
        prov = _fixture_provenance()
        result = _mermaid_provenance(prov)
        # Edge labels should be present
        assert "-->|" in result


class TestTimelineData:
    def test_builds_entries(self):
        state = _fixture_state()
        data = _timeline_data(state, [])
        assert len(data) == 3
        assert data[0]["step_id"] == 1
        assert data[0]["title"] == "Download data"
        assert data[0]["llm_time"] == 8.0
        assert data[0]["sandbox_time"] == 5.0
        assert data[0]["elapsed"] == 15.2

    def test_empty_steps(self):
        data = _timeline_data({"steps": []}, [])
        assert data == []


class TestSummaryMetrics:
    def test_computes_metrics(self):
        state = _fixture_state()
        m = _summary_metrics(state)
        assert m["total_steps"] == 3
        assert m["completed"] == 2
        assert m["failed"] == 1
        assert m["total_elapsed"] == 42.5
        assert m["total_llm_time"] == 24.0
        assert m["total_sandbox_time"] == 13.0
        assert m["total_rewrites"] == 3  # 0 + 1 + 2


class TestStepDetails:
    def test_builds_details(self):
        state = _fixture_state()
        details = _step_details(state)
        assert len(details) == 3
        assert details[0]["id"] == 1
        assert details[0]["title"] == "Download data"
        assert details[0]["status"] == "completed"
        assert details[2]["error"] == "ImportError: no module named foo"
        assert details[0]["files_written"] == ["/workspace/data.csv"]


class TestGenerateReport:
    def test_generates_html_file(self, tmp_path):
        state = _fixture_state()
        events = _fixture_events()
        prov = _fixture_provenance()
        output = os.path.join(str(tmp_path), "report.html")

        result = generate_report(state, events, prov, output)
        assert result == output
        assert os.path.exists(output)

        with open(output) as f:
            html = f.read()

        assert "<!DOCTYPE html>" in html
        assert "UAS Run Report" in html

    def test_html_contains_goal(self, tmp_path):
        state = _fixture_state()
        output = os.path.join(str(tmp_path), "report.html")
        generate_report(state, [], {"nodes": {}, "edges": []}, output)

        with open(output) as f:
            html = f.read()

        assert "Build a test dashboard" in html

    def test_html_contains_step_titles(self, tmp_path):
        state = _fixture_state()
        output = os.path.join(str(tmp_path), "report.html")
        generate_report(state, [], {"nodes": {}, "edges": []}, output)

        with open(output) as f:
            html = f.read()

        assert "Download data" in html
        assert "Process data" in html
        assert "Generate report" in html

    def test_html_contains_timeline_data(self, tmp_path):
        state = _fixture_state()
        output = os.path.join(str(tmp_path), "report.html")
        generate_report(state, [], {"nodes": {}, "edges": []}, output)

        with open(output) as f:
            html = f.read()

        # Timeline data is embedded as JSON
        assert "timeline" in html.lower()
        assert "15.2" in html  # elapsed for step 1

    def test_html_contains_error_info(self, tmp_path):
        state = _fixture_state()
        output = os.path.join(str(tmp_path), "report.html")
        generate_report(state, [], {"nodes": {}, "edges": []}, output)

        with open(output) as f:
            html = f.read()

        assert "ImportError" in html

    def test_html_contains_dag_mermaid(self, tmp_path):
        state = _fixture_state()
        output = os.path.join(str(tmp_path), "report.html")
        generate_report(state, [], {"nodes": {}, "edges": []}, output)

        with open(output) as f:
            html = f.read()

        assert "graph TD" in html
        assert "mermaid" in html

    def test_html_contains_provenance(self, tmp_path):
        state = _fixture_state()
        prov = _fixture_provenance()
        output = os.path.join(str(tmp_path), "report.html")
        generate_report(state, [], prov, output)

        with open(output) as f:
            html = f.read()

        assert "Provenance" in html
        assert "goal" in html
        assert "decompose" in html

    def test_creates_parent_directory(self, tmp_path):
        state = _fixture_state()
        output = os.path.join(str(tmp_path), "subdir", "deep", "report.html")
        generate_report(state, [], {"nodes": {}, "edges": []}, output)
        assert os.path.exists(output)

    def test_tabs_present(self, tmp_path):
        state = _fixture_state()
        output = os.path.join(str(tmp_path), "report.html")
        generate_report(state, [], {"nodes": {}, "edges": []}, output)

        with open(output) as f:
            html = f.read()

        assert "tab-overview" in html
        assert "tab-timeline" in html
        assert "tab-steps" in html
        assert "tab-provenance" in html

    def test_files_written_listed(self, tmp_path):
        state = _fixture_state()
        output = os.path.join(str(tmp_path), "report.html")
        generate_report(state, [], {"nodes": {}, "edges": []}, output)

        with open(output) as f:
            html = f.read()

        assert "data.csv" in html
        assert "clean.csv" in html
