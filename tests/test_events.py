"""Tests for architect.events module."""

import json
import os
import threading

from architect.events import Event, EventLog, EventType, get_event_log, reset_event_log


class TestEvent:
    def test_to_dict_strips_none(self):
        e = Event(timestamp="2024-01-01T00:00:00Z", event_type="goal_received")
        d = e.to_dict()
        assert "step_id" not in d
        assert "attempt" not in d
        assert "duration" not in d
        assert d["timestamp"] == "2024-01-01T00:00:00Z"
        assert d["event_type"] == "goal_received"

    def test_to_dict_includes_present_fields(self):
        e = Event(
            timestamp="2024-01-01T00:00:00Z",
            event_type="step_start",
            step_id=1,
            attempt=2,
            duration=3.5,
            data={"key": "value"},
        )
        d = e.to_dict()
        assert d["step_id"] == 1
        assert d["attempt"] == 2
        assert d["duration"] == 3.5
        assert d["data"] == {"key": "value"}


class TestEventLog:
    def test_emit_appends_event(self):
        log = EventLog()
        log.emit(EventType.GOAL_RECEIVED, data={"goal": "test"})
        assert len(log) == 1
        assert log.events[0].event_type == "goal_received"
        assert log.events[0].data == {"goal": "test"}

    def test_emit_returns_event(self):
        log = EventLog()
        event = log.emit(EventType.STEP_START, step_id=1)
        assert isinstance(event, Event)
        assert event.step_id == 1

    def test_emit_sets_timestamp(self):
        log = EventLog()
        event = log.emit(EventType.GOAL_RECEIVED)
        assert event.timestamp  # non-empty
        assert "T" in event.timestamp  # ISO format

    def test_query_by_event_type(self):
        log = EventLog()
        log.emit(EventType.GOAL_RECEIVED)
        log.emit(EventType.STEP_START, step_id=1)
        log.emit(EventType.STEP_START, step_id=2)
        log.emit(EventType.STEP_COMPLETE, step_id=1)

        results = log.query(event_type=EventType.STEP_START)
        assert len(results) == 2
        assert all(e.event_type == "step_start" for e in results)

    def test_query_by_step_id(self):
        log = EventLog()
        log.emit(EventType.STEP_START, step_id=1)
        log.emit(EventType.STEP_START, step_id=2)
        log.emit(EventType.STEP_COMPLETE, step_id=1)

        results = log.query(step_id=1)
        assert len(results) == 2
        assert all(e.step_id == 1 for e in results)

    def test_query_by_both_filters(self):
        log = EventLog()
        log.emit(EventType.STEP_START, step_id=1)
        log.emit(EventType.STEP_COMPLETE, step_id=1)
        log.emit(EventType.STEP_START, step_id=2)

        results = log.query(event_type=EventType.STEP_START, step_id=1)
        assert len(results) == 1

    def test_query_no_filters_returns_all(self):
        log = EventLog()
        log.emit(EventType.GOAL_RECEIVED)
        log.emit(EventType.STEP_START, step_id=1)
        assert len(log.query()) == 2

    def test_persist_to_disk(self, tmp_path):
        events_path = os.path.join(str(tmp_path), ".uas_state", "events.jsonl")
        log = EventLog(events_path=events_path)
        log.emit(EventType.GOAL_RECEIVED, data={"goal": "test"})
        log.emit(EventType.STEP_START, step_id=1)

        assert os.path.exists(events_path)
        with open(events_path) as f:
            lines = f.readlines()
        assert len(lines) == 2

        first = json.loads(lines[0])
        assert first["event_type"] == "goal_received"
        assert first["data"] == {"goal": "test"}

        second = json.loads(lines[1])
        assert second["event_type"] == "step_start"
        assert second["step_id"] == 1

    def test_thread_safety(self):
        log = EventLog()
        errors = []

        def emitter(n):
            try:
                for i in range(50):
                    log.emit(EventType.STEP_START, step_id=n)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=emitter, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(log) == 200  # 4 threads * 50 events


class TestEventType:
    def test_all_event_types_have_values(self):
        for et in EventType:
            assert isinstance(et.value, str)
            assert len(et.value) > 0

    def test_event_types_are_unique(self):
        values = [et.value for et in EventType]
        assert len(values) == len(set(values))


class TestSingleton:
    def test_get_event_log_returns_same_instance(self):
        reset_event_log()
        log1 = get_event_log()
        log2 = get_event_log()
        assert log1 is log2
        reset_event_log()

    def test_get_event_log_with_path(self, tmp_path):
        reset_event_log()
        events_path = os.path.join(str(tmp_path), ".uas_state", "events.jsonl")
        log = get_event_log(events_path=events_path)
        assert log.events_path == events_path
        reset_event_log()

    def test_reset_clears_singleton(self):
        reset_event_log()
        log1 = get_event_log()
        reset_event_log()
        log2 = get_event_log()
        assert log1 is not log2
        reset_event_log()
