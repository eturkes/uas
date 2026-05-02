"""Tests for ``orchestrator.rate_ledger.RateLedger`` (Phase 3 §3).

Pure-Python tests against synthetic event sequences. No container
engine, no live Claude — every test exercises the JSONL persistence
contract and the ``current_status`` / ``internal_count`` /
``check_divergence`` read paths against fixtures the test itself
constructs. The ``state_root`` constructor argument lets each test
write into a per-test ``tmp_path`` so the suite never touches the
canonical ``<repo>/orchestrator/state`` directory.
"""

import json
import os

import pytest

from orchestrator import rate_ledger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_event(
    *,
    rate_type: str = "five_hour",
    status: str = "allowed",
    resets_at: int = 1_700_000_000,
    overage_status: str = "allowed",
    overage_resets_at: int = 1_700_000_000,
    is_using_overage: bool = False,
    uuid_: str = "u",
    session_id: str = "s",
) -> dict:
    """Build a synthetic stream-json ``rate_limit_event`` payload.

    Mirrors the schema documented in ``docs/substrate.md`` §8 and
    confirmed by Phase 3 §2's live spawn — every key is present so
    tests can override individual fields without breaking shape.
    """
    return {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": status,
            "resetsAt": resets_at,
            "rateLimitType": rate_type,
            "overageStatus": overage_status,
            "overageResetsAt": overage_resets_at,
            "isUsingOverage": is_using_overage,
        },
        "uuid": uuid_,
        "session_id": session_id,
    }


def make_metadata(**overrides) -> dict:
    """Build a synthetic ``capture_run_metadata`` payload.

    Includes the ``orchestrator_version`` key so rows look
    indistinguishable from real worker-emitted rows.
    """
    base = {
        "git_sha": "deadbeef" + "0" * 32,
        "git_branch": "main",
        "git_dirty": False,
        "timestamp_utc": "2026-05-01T00:00:00+00:00",
        "env_snapshot": {},
        "config_hash": "n/a",
        "harness_version": "phase1",
        "orchestrator_version": "phase3",
    }
    base.update(overrides)
    return base


def _read_rows(state_root: str, task_id: str) -> list[dict]:
    path = os.path.join(state_root, task_id, "rate_limits.jsonl")
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


@pytest.fixture
def ledger(tmp_path):
    return rate_ledger.RateLedger(state_root=str(tmp_path))


# ---------------------------------------------------------------------------
# record()
# ---------------------------------------------------------------------------

class TestRecord:
    """Persistence layer: file creation, schema, and append semantics."""

    def test_creates_file_and_appends_one_row_per_event(
        self, ledger, tmp_path,
    ):
        events = [
            make_event(rate_type="five_hour"),
            make_event(rate_type="seven_day"),
        ]
        ledger.record(events, run_metadata=make_metadata(), task_id="t1")

        path = os.path.join(str(tmp_path), "t1", "rate_limits.jsonl")
        assert os.path.isfile(path)
        rows = _read_rows(str(tmp_path), "t1")
        assert len(rows) == 2
        assert all(r["event"] == "rate_limit" for r in rows)
        assert all(r["task_id"] == "t1" for r in rows)
        assert (
            rows[0]["rate_limit_event"]["rate_limit_info"]["rateLimitType"]
            == "five_hour"
        )
        assert (
            rows[1]["rate_limit_event"]["rate_limit_info"]["rateLimitType"]
            == "seven_day"
        )

    def test_stamps_run_metadata_including_orchestrator_version(
        self, ledger, tmp_path,
    ):
        ledger.record(
            [make_event()],
            run_metadata=make_metadata(git_sha="abc123"),
            task_id="t1",
        )
        rows = _read_rows(str(tmp_path), "t1")
        assert rows[0]["git_sha"] == "abc123"
        assert rows[0]["harness_version"] == "phase1"
        assert rows[0]["orchestrator_version"] == "phase3"

    def test_empty_events_list_is_noop(self, ledger, tmp_path):
        ledger.record([], run_metadata=make_metadata(), task_id="t1")
        # No directory should be created when there's nothing to write.
        assert not os.path.exists(os.path.join(str(tmp_path), "t1"))

    def test_appends_across_multiple_calls(self, ledger, tmp_path):
        ledger.record(
            [make_event()], run_metadata=make_metadata(), task_id="t1",
        )
        ledger.record(
            [make_event()], run_metadata=make_metadata(), task_id="t1",
        )
        assert len(_read_rows(str(tmp_path), "t1")) == 2

    def test_round_trip_preserves_original_event_shape(
        self, ledger, tmp_path,
    ):
        evt = make_event(
            rate_type="five_hour",
            status="warning",
            resets_at=1_700_005_000,
            is_using_overage=True,
            overage_status="exceeded",
            overage_resets_at=1_700_009_000,
            uuid_="abc-def",
            session_id="sess-42",
        )
        ledger.record(
            [evt], run_metadata=make_metadata(), task_id="t1",
        )
        round_tripped = _read_rows(
            str(tmp_path), "t1",
        )[0]["rate_limit_event"]
        assert round_tripped == evt

    def test_isolates_tasks(self, ledger, tmp_path):
        ledger.record(
            [make_event()], run_metadata=make_metadata(), task_id="ta",
        )
        ledger.record(
            [make_event(), make_event()],
            run_metadata=make_metadata(), task_id="tb",
        )
        assert len(_read_rows(str(tmp_path), "ta")) == 1
        assert len(_read_rows(str(tmp_path), "tb")) == 2


# ---------------------------------------------------------------------------
# current_status()
# ---------------------------------------------------------------------------

class TestCurrentStatus:
    """Latest-event-per-rateLimitType read path."""

    def test_missing_file_returns_both_none(self, ledger):
        assert ledger.current_status("never-recorded") == {
            "five_hour": None,
            "seven_day": None,
        }

    def test_five_hour_only(self, ledger):
        ledger.record(
            [make_event(rate_type="five_hour", status="allowed")],
            run_metadata=make_metadata(), task_id="t1",
        )
        st = ledger.current_status("t1")
        assert st["five_hour"] is not None
        assert st["five_hour"]["status"] == "allowed"
        assert st["seven_day"] is None

    def test_five_hour_plus_seven_day_independent(self, ledger):
        ledger.record(
            [
                make_event(rate_type="five_hour", status="allowed"),
                make_event(rate_type="seven_day", status="warning"),
            ],
            run_metadata=make_metadata(), task_id="t1",
        )
        st = ledger.current_status("t1")
        assert st["five_hour"]["status"] == "allowed"
        assert st["seven_day"]["status"] == "warning"

    def test_returns_latest_per_type(self, ledger):
        # First five_hour event "allowed", later "warning"; latest wins.
        ledger.record(
            [make_event(rate_type="five_hour", status="allowed")],
            run_metadata=make_metadata(), task_id="t1",
        )
        ledger.record(
            [make_event(
                rate_type="five_hour",
                status="warning",
                resets_at=1_700_001_000,
            )],
            run_metadata=make_metadata(), task_id="t1",
        )
        snap = ledger.current_status("t1")["five_hour"]
        assert snap["status"] == "warning"
        assert snap["resetsAt"] == 1_700_001_000

    def test_buffer_state_fields_round_trip_into_snapshot(self, ledger):
        ledger.record(
            [make_event(
                rate_type="five_hour",
                status="allowed",
                is_using_overage=True,
                overage_status="exceeded",
                overage_resets_at=1_700_002_000,
            )],
            run_metadata=make_metadata(), task_id="t1",
        )
        snap = ledger.current_status("t1")["five_hour"]
        assert snap["isUsingOverage"] is True
        assert snap["overageStatus"] == "exceeded"
        assert snap["overageResetsAt"] == 1_700_002_000

    def test_skips_malformed_lines(self, ledger, tmp_path):
        # Hand-write a JSONL file with a bad line interleaved.
        path = os.path.join(str(tmp_path), "t1", "rate_limits.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        good = json.dumps({
            "event": "rate_limit",
            "task_id": "t1",
            "rate_limit_event": make_event(),
        })
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(good + "\n")
            fh.write("not json at all\n")
            fh.write("\n")
            fh.write(good + "\n")
        # Reads the two good rows without crashing.
        st = ledger.current_status("t1")
        assert st["five_hour"] is not None

    def test_unknown_rate_type_ignored(self, ledger):
        ledger.record(
            [make_event(rate_type="annual_quota_made_up")],
            run_metadata=make_metadata(), task_id="t1",
        )
        assert ledger.current_status("t1") == {
            "five_hour": None, "seven_day": None,
        }


# ---------------------------------------------------------------------------
# internal_count()
# ---------------------------------------------------------------------------

class TestInternalCount:
    """In-window counter; resets when ``resetsAt`` flips."""

    def test_missing_file_returns_zero(self, ledger):
        assert ledger.internal_count("nope") == 0

    def test_counts_five_hour_events_in_current_window(self, ledger):
        ledger.record(
            [make_event(resets_at=1000) for _ in range(3)],
            run_metadata=make_metadata(), task_id="t1",
        )
        assert ledger.internal_count("t1") == 3

    def test_excludes_seven_day_events(self, ledger):
        ledger.record(
            [
                make_event(rate_type="five_hour", resets_at=1000),
                make_event(rate_type="seven_day", resets_at=2000),
                make_event(rate_type="seven_day", resets_at=2000),
            ],
            run_metadata=make_metadata(), task_id="t1",
        )
        # Only the single five_hour event is counted.
        assert ledger.internal_count("t1") == 1

    def test_resets_across_window_boundary(self, ledger):
        # 5 events in window A (resets_at=1000); count=5.
        ledger.record(
            [make_event(resets_at=1000) for _ in range(5)],
            run_metadata=make_metadata(), task_id="t1",
        )
        assert ledger.internal_count("t1") == 5

        # Window flips: 2 events with a larger resets_at. Count of
        # the *current* window (B) is now 2; the 5 from window A
        # are excluded.
        ledger.record(
            [make_event(resets_at=2000) for _ in range(2)],
            run_metadata=make_metadata(), task_id="t1",
        )
        assert ledger.internal_count("t1") == 2

    def test_only_latest_window_counted_with_three_windows(self, ledger):
        ledger.record(
            [make_event(resets_at=1000) for _ in range(4)],
            run_metadata=make_metadata(), task_id="t1",
        )
        ledger.record(
            [make_event(resets_at=2000) for _ in range(7)],
            run_metadata=make_metadata(), task_id="t1",
        )
        ledger.record(
            [make_event(resets_at=3000) for _ in range(1)],
            run_metadata=make_metadata(), task_id="t1",
        )
        assert ledger.internal_count("t1") == 1


# ---------------------------------------------------------------------------
# check_divergence()
# ---------------------------------------------------------------------------

class TestCheckDivergence:
    """Stderr alert when the recorded signal looks behind reality."""

    def test_no_data_no_divergence(self, ledger, capsys):
        assert ledger.check_divergence("nope", threshold=10) is False
        assert "[rate_ledger]" not in capsys.readouterr().err

    def test_below_threshold_no_divergence(self, ledger, capsys):
        ledger.record(
            [make_event(status="allowed", resets_at=1000)],
            run_metadata=make_metadata(), task_id="t1",
        )
        assert ledger.check_divergence("t1", threshold=10) is False
        assert "[rate_ledger]" not in capsys.readouterr().err

    def test_status_not_allowed_does_not_fire(self, ledger, capsys):
        # A high in-window count alone is not enough — when the
        # signal already reports non-"allowed", there's no
        # divergence to flag.
        ledger.record(
            [make_event(status="warning", resets_at=1000) for _ in range(20)],
            run_metadata=make_metadata(), task_id="t1",
        )
        assert ledger.check_divergence("t1", threshold=5) is False
        assert "[rate_ledger]" not in capsys.readouterr().err

    def test_above_threshold_with_allowed_fires_and_logs(
        self, ledger, capsys,
    ):
        ledger.record(
            [make_event(status="allowed", resets_at=1000) for _ in range(20)],
            run_metadata=make_metadata(), task_id="t1",
        )
        assert ledger.check_divergence("t1", threshold=5) is True
        err = capsys.readouterr().err
        assert "[rate_ledger]" in err
        assert "internal_count=20" in err
        assert "threshold=5" in err
        assert "task_id='t1'" in err

    def test_threshold_at_count_does_not_fire(self, ledger, capsys):
        # Equality is "<=" not "<": the alert is strictly above.
        ledger.record(
            [make_event(status="allowed", resets_at=1000) for _ in range(5)],
            run_metadata=make_metadata(), task_id="t1",
        )
        assert ledger.check_divergence("t1", threshold=5) is False
        assert "[rate_ledger]" not in capsys.readouterr().err

    def test_window_flip_clears_divergence(self, ledger, capsys):
        # Old window had 20 allowed events (would have triggered);
        # window B has 1 allowed event — count for the current
        # window is 1, no divergence regardless of history.
        ledger.record(
            [make_event(status="allowed", resets_at=1000) for _ in range(20)],
            run_metadata=make_metadata(), task_id="t1",
        )
        ledger.record(
            [make_event(status="allowed", resets_at=2000)],
            run_metadata=make_metadata(), task_id="t1",
        )
        capsys.readouterr()  # discard prior captures
        assert ledger.check_divergence("t1", threshold=5) is False
        assert "[rate_ledger]" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Default state root
# ---------------------------------------------------------------------------

class TestDefaultStateRoot:
    """Sanity checks on the module-level default."""

    def test_default_state_root_is_under_orchestrator(self):
        assert rate_ledger.DEFAULT_STATE_ROOT.endswith(
            os.path.join("orchestrator", "state")
        )

    def test_default_constructor_uses_default_root(self):
        ledger = rate_ledger.RateLedger()
        # Path computation routes through DEFAULT_STATE_ROOT; we
        # don't actually create the directory in this assertion.
        path = ledger._path("some-task")
        assert path.startswith(rate_ledger.DEFAULT_STATE_ROOT)
        assert path.endswith(os.path.join("some-task", "rate_limits.jsonl"))
