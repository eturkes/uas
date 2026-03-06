"""Tests for architect.trace_export module."""

import json
import os

from architect.trace_export import (
    TraceExporter,
    PID_ARCHITECT,
    PID_ORCHESTRATOR,
    PID_SANDBOX,
    PH_COMPLETE,
    PH_DURATION_BEGIN,
    PH_DURATION_END,
    PH_COUNTER,
    PH_METADATA,
    _iso_to_us,
)


def _make_event(event_type, ts_offset_s=0, **kwargs):
    """Create a fixture event dict with a timestamp offset from a base time."""
    from datetime import datetime, timezone, timedelta
    base = datetime(2024, 6, 15, 10, 0, 0, tzinfo=timezone.utc)
    ts = base + timedelta(seconds=ts_offset_s)
    event = {
        "timestamp": ts.isoformat(),
        "event_type": event_type,
    }
    for key in ("step_id", "attempt", "duration", "data"):
        if key in kwargs:
            event[key] = kwargs[key]
    return event


def _fixture_events():
    """Build a realistic sequence of events for a 2-step run."""
    return [
        _make_event("goal_received", 0, data={"goal": "test goal"}),
        _make_event("decomposition_start", 1),
        _make_event("decomposition_complete", 3, duration=2.0,
                     data={"num_steps": 2}),
        # Step 1
        _make_event("step_start", 4, step_id=1, data={"title": "Setup"}),
        _make_event("llm_call_start", 5, step_id=1, attempt=1),
        _make_event("llm_call_complete", 10, step_id=1, attempt=1,
                     duration=5.0, data={"exit_code": 0}),
        _make_event("step_complete", 11, step_id=1, duration=7.0,
                     data={"files_written": ["setup.py"]}),
        # Step 2 with rewrite
        _make_event("step_start", 12, step_id=2, data={"title": "Build"}),
        _make_event("llm_call_start", 13, step_id=2, attempt=1),
        _make_event("llm_call_complete", 18, step_id=2, attempt=1,
                     duration=5.0, data={"exit_code": 1}),
        _make_event("rewrite_start", 19, step_id=2, attempt=1),
        _make_event("rewrite_complete", 20, step_id=2, attempt=1),
        _make_event("llm_call_start", 21, step_id=2, attempt=2),
        _make_event("llm_call_complete", 26, step_id=2, attempt=2,
                     duration=5.0, data={"exit_code": 0}),
        _make_event("verification_start", 27, step_id=2),
        _make_event("verification_complete", 28, step_id=2,
                     data={"passed": True}),
        _make_event("step_complete", 29, step_id=2, duration=17.0,
                     data={"files_written": ["build.py"]}),
        _make_event("run_complete", 30, data={"status": "completed",
                                               "total_elapsed": 30.0}),
    ]


class TestIsoToUs:
    def test_converts_utc_timestamp(self):
        us = _iso_to_us("2024-01-01T00:00:00+00:00")
        assert isinstance(us, float)
        assert us > 0

    def test_naive_timestamp_treated_as_utc(self):
        us = _iso_to_us("2024-01-01T00:00:00")
        assert isinstance(us, float)
        assert us > 0

    def test_different_timestamps_differ(self):
        us1 = _iso_to_us("2024-01-01T00:00:00+00:00")
        us2 = _iso_to_us("2024-01-01T00:00:01+00:00")
        assert us2 - us1 == pytest.approx(1_000_000, abs=1)


class TestTraceExporter:
    def test_empty_events(self):
        exporter = TraceExporter([])
        trace = exporter.export()
        assert trace == []

    def test_metadata_events_present(self):
        events = [_make_event("goal_received", 0, data={"goal": "test"})]
        exporter = TraceExporter(events)
        trace = exporter.export()
        metadata = [e for e in trace if e["ph"] == PH_METADATA]
        assert len(metadata) == 3
        pids = {e["pid"] for e in metadata}
        assert pids == {PID_ARCHITECT, PID_ORCHESTRATOR, PID_SANDBOX}

    def test_decomposition_span(self):
        events = [
            _make_event("decomposition_start", 0),
            _make_event("decomposition_complete", 2, duration=2.0,
                         data={"num_steps": 3}),
        ]
        exporter = TraceExporter(events)
        trace = exporter.export()
        spans = [e for e in trace if e.get("cat") == "planning"]
        assert len(spans) == 2
        assert spans[0]["ph"] == PH_DURATION_BEGIN
        assert spans[1]["ph"] == PH_DURATION_END
        assert spans[0]["pid"] == PID_ARCHITECT
        assert spans[1]["args"]["num_steps"] == 3

    def test_step_span(self):
        events = [
            _make_event("step_start", 0, step_id=1,
                         data={"title": "My Step"}),
            _make_event("step_complete", 5, step_id=1, duration=5.0,
                         data={"files_written": ["out.txt"]}),
        ]
        exporter = TraceExporter(events)
        trace = exporter.export()
        step_events = [e for e in trace if e.get("cat") == "step"]
        assert len(step_events) == 2
        assert step_events[0]["ph"] == PH_DURATION_BEGIN
        assert step_events[0]["pid"] == PID_ARCHITECT
        assert step_events[0]["tid"] == 1
        assert "My Step" in step_events[0]["name"]
        assert step_events[1]["ph"] == PH_DURATION_END

    def test_step_failed_span(self):
        events = [
            _make_event("step_start", 0, step_id=1,
                         data={"title": "Fail Step"}),
            _make_event("step_failed", 5, step_id=1,
                         data={"error": "something broke"}),
        ]
        exporter = TraceExporter(events)
        trace = exporter.export()
        step_events = [e for e in trace if e.get("cat") == "step"]
        assert len(step_events) == 2
        assert step_events[1]["ph"] == PH_DURATION_END
        assert "FAILED" in step_events[1]["name"]

    def test_llm_call_span(self):
        events = [
            _make_event("llm_call_start", 0, step_id=1, attempt=1),
            _make_event("llm_call_complete", 3, step_id=1, attempt=1,
                         duration=3.0, data={"exit_code": 0}),
        ]
        exporter = TraceExporter(events)
        trace = exporter.export()
        llm_events = [e for e in trace if e.get("cat") == "llm"]
        assert len(llm_events) == 2
        assert llm_events[0]["pid"] == PID_ORCHESTRATOR
        assert llm_events[1]["pid"] == PID_ORCHESTRATOR

    def test_sandbox_span(self):
        events = [
            _make_event("sandbox_start", 0, step_id=1, attempt=1),
            _make_event("sandbox_complete", 2, step_id=1, attempt=1,
                         duration=2.0, data={"exit_code": 0}),
        ]
        exporter = TraceExporter(events)
        trace = exporter.export()
        sandbox_events = [e for e in trace if e.get("cat") == "sandbox"]
        assert len(sandbox_events) == 2
        assert sandbox_events[0]["pid"] == PID_SANDBOX
        assert sandbox_events[1]["pid"] == PID_SANDBOX

    def test_rewrite_span(self):
        events = [
            _make_event("rewrite_start", 0, step_id=2, attempt=1),
            _make_event("rewrite_complete", 1, step_id=2, attempt=1),
        ]
        exporter = TraceExporter(events)
        trace = exporter.export()
        rw_events = [e for e in trace if e.get("cat") == "rewrite"]
        assert len(rw_events) == 2
        assert rw_events[0]["ph"] == PH_DURATION_BEGIN
        assert rw_events[1]["ph"] == PH_DURATION_END
        assert rw_events[0]["pid"] == PID_ARCHITECT

    def test_verification_span(self):
        events = [
            _make_event("verification_start", 0, step_id=1),
            _make_event("verification_complete", 1, step_id=1,
                         data={"passed": True}),
        ]
        exporter = TraceExporter(events)
        trace = exporter.export()
        v_events = [e for e in trace if e.get("cat") == "verification"]
        assert len(v_events) == 2
        assert v_events[0]["ph"] == PH_DURATION_BEGIN
        assert v_events[1]["ph"] == PH_DURATION_END
        assert v_events[1]["args"]["passed"] is True

    def test_counter_events_for_llm(self):
        events = [
            _make_event("llm_call_start", 0, step_id=1, attempt=1),
            _make_event("llm_call_complete", 3, step_id=1, attempt=1,
                         duration=3.0, data={"exit_code": 0}),
            _make_event("llm_call_start", 5, step_id=2, attempt=1),
            _make_event("llm_call_complete", 8, step_id=2, attempt=1,
                         duration=3.0, data={"exit_code": 0}),
        ]
        exporter = TraceExporter(events)
        trace = exporter.export()
        counters = [e for e in trace if e["ph"] == PH_COUNTER
                     and e["name"] == "LLM Metrics"]
        assert len(counters) == 2
        assert counters[0]["args"]["total_calls"] == 1
        assert counters[1]["args"]["total_calls"] == 2

    def test_counter_events_for_sandbox(self):
        events = [
            _make_event("sandbox_start", 0, step_id=1, attempt=1),
            _make_event("sandbox_complete", 2, step_id=1, attempt=1,
                         duration=2.0, data={"exit_code": 0}),
        ]
        exporter = TraceExporter(events)
        trace = exporter.export()
        counters = [e for e in trace if e["ph"] == PH_COUNTER
                     and e["name"] == "Sandbox Metrics"]
        assert len(counters) == 1
        assert counters[0]["args"]["total_runs"] == 1

    def test_full_run_produces_valid_trace(self):
        events = _fixture_events()
        exporter = TraceExporter(events)
        trace = exporter.export()

        # All events must have required fields
        for te in trace:
            assert "ph" in te
            assert "pid" in te

            # Non-metadata events should have ts
            if te["ph"] != PH_METADATA:
                assert "ts" in te
                assert isinstance(te["ts"], (int, float))

        # Should have metadata + span events
        assert len(trace) > 3  # at least the 3 metadata events

    def test_timestamps_are_relative(self):
        events = [
            _make_event("step_start", 10, step_id=1,
                         data={"title": "Test"}),
            _make_event("step_complete", 15, step_id=1, duration=5.0,
                         data={"files_written": []}),
        ]
        exporter = TraceExporter(events)
        trace = exporter.export()
        step_events = [e for e in trace if e.get("cat") == "step"]
        # First event should be at ts=0 since run_start == first event
        assert step_events[0]["ts"] == pytest.approx(0.0, abs=1)
        # Second should be ~5 seconds later
        assert step_events[1]["ts"] == pytest.approx(5_000_000, abs=1000)

    def test_thread_ids_differ_per_step(self):
        events = [
            _make_event("step_start", 0, step_id=1,
                         data={"title": "A"}),
            _make_event("step_start", 0, step_id=2,
                         data={"title": "B"}),
            _make_event("step_complete", 5, step_id=1, duration=5.0,
                         data={"files_written": []}),
            _make_event("step_complete", 5, step_id=2, duration=5.0,
                         data={"files_written": []}),
        ]
        exporter = TraceExporter(events)
        trace = exporter.export()
        step_events = [e for e in trace if e.get("cat") == "step"]
        tids = {e["tid"] for e in step_events}
        assert len(tids) == 2  # Different tids for step 1 and 2


class TestExportJson:
    def test_writes_valid_json_file(self, tmp_path):
        events = _fixture_events()
        exporter = TraceExporter(events)
        out_path = os.path.join(str(tmp_path), "trace.json")
        result = exporter.export_json(out_path)

        assert result == out_path
        assert os.path.exists(out_path)

        with open(out_path) as f:
            data = json.load(f)
        assert isinstance(data, list)
        assert len(data) > 0

    def test_creates_parent_directories(self, tmp_path):
        events = [_make_event("goal_received", 0, data={"goal": "test"})]
        exporter = TraceExporter(events)
        out_path = os.path.join(str(tmp_path), "sub", "dir", "trace.json")
        exporter.export_json(out_path)
        assert os.path.exists(out_path)

    def test_chrome_trace_schema_fields(self, tmp_path):
        """Verify trace events have the required Chrome Trace Event fields."""
        events = [
            _make_event("step_start", 0, step_id=1,
                         data={"title": "Test"}),
            _make_event("llm_call_start", 1, step_id=1, attempt=1),
            _make_event("llm_call_complete", 3, step_id=1, attempt=1,
                         duration=2.0, data={"exit_code": 0}),
            _make_event("step_complete", 4, step_id=1, duration=4.0,
                         data={"files_written": []}),
        ]
        exporter = TraceExporter(events)
        trace = exporter.export()

        for te in trace:
            # Every trace event must have ph and pid
            assert "ph" in te, f"Missing 'ph' in {te}"
            assert "pid" in te, f"Missing 'pid' in {te}"
            assert te["ph"] in ("B", "E", "X", "C", "M"), \
                f"Invalid phase '{te['ph']}'"

            # Duration and counter events need ts
            if te["ph"] in (PH_DURATION_BEGIN, PH_DURATION_END,
                            PH_COMPLETE, PH_COUNTER):
                assert "ts" in te, f"Missing 'ts' in {te}"

            # All events need tid
            assert "tid" in te, f"Missing 'tid' in {te}"


import pytest
