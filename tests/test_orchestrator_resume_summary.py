"""Tests for ``orchestrator.resume_summary`` (Phase 5 §4).

Pure-Python: ``render_resume_summary`` is a pure function so most
content checks compose ``Task`` / ``Subtask`` / ``Decision`` /
``Stage`` instances directly and assert on the returned Markdown
body. ``write_resume_summary`` round-trips through ``tmp_path``;
the small wiring tests for ``cmd_*`` integration live in
``tests/test_orchestrator_loop.py`` to avoid duplicating the
existing CLI fixtures.
"""

import json
import os

import pytest

from orchestrator import buffer_ledger as buffer_ledger_mod
from orchestrator import resume_summary as resume_summary_mod
from orchestrator.task import Checkpoint, Decision, Stage, Subtask, Task

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    *,
    task_id: str = "t1",
    goal: str = "demo goal",
    created_at: str = "2026-05-01T00:00:00+00:00",
    state_root: str = "/tmp/state",
    subtasks: list[Subtask] | None = None,
    decisions: list[Decision] | None = None,
    stages: list[Stage] | None = None,
    checkpoints: list[Checkpoint] | None = None,
    acked_checkpoint_ids: set[str] | None = None,
) -> Task:
    """Construct an in-memory Task without touching disk."""
    return Task(
        task_id=task_id,
        goal=goal,
        workspace_path="/tmp/ws/" + task_id,
        created_at=created_at,
        subtasks=list(subtasks or []),
        decisions=list(decisions or []),
        stages=list(stages or []),
        checkpoints=list(checkpoints or []),
        acked_checkpoint_ids=set(acked_checkpoint_ids or set()),
        state_root=state_root,
    )


def _seed_buffer_row(
    state_root: str,
    task_id: str,
    *,
    cost_usd: float,
    claude_reported: float,
    timestamp: str,
    subtask_id: str = "sx",
) -> None:
    """Append a synthetic row to ``<state_root>/<task_id>/buffer.jsonl``."""
    path = os.path.join(state_root, task_id, "buffer.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    row = {
        "event": "buffer",
        "task_id": task_id,
        "subtask_id": subtask_id,
        "timestamp_utc": timestamp,
        "cost_usd": cost_usd,
        "claude_reported_cost_usd": claude_reported,
    }
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


@pytest.fixture
def empty_ledger(tmp_path):
    return buffer_ledger_mod.BufferLedger(state_root=str(tmp_path / "state"))


# ---------------------------------------------------------------------------
# render_resume_summary — header
# ---------------------------------------------------------------------------


class TestRenderHeader:

    def test_header_carries_task_id(self, empty_ledger):
        task = _make_task(task_id="abc")
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=empty_ledger,
        )
        assert "# Resume summary — `abc`" in body

    def test_header_carries_goal(self, empty_ledger):
        task = _make_task(goal="ship a survey")
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=empty_ledger,
        )
        assert "**Goal.** ship a survey" in body

    def test_generated_timestamp_present(self, empty_ledger):
        task = _make_task()
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=empty_ledger,
        )
        assert "_Generated:" in body
        # ISO-8601 UTC with explicit offset.
        assert "+00:00_" in body


# ---------------------------------------------------------------------------
# render_resume_summary — subtask counts
# ---------------------------------------------------------------------------


class TestRenderSubtaskCounts:

    def test_counts_pending_in_flight_done_failed(self, empty_ledger):
        task = _make_task(subtasks=[
            Subtask(subtask_id="s1", prompt="p", status="pending"),
            Subtask(subtask_id="s2", prompt="p", status="pending"),
            Subtask(subtask_id="s3", prompt="p", status="in_flight"),
            Subtask(subtask_id="s4", prompt="p", status="done"),
            Subtask(subtask_id="s5", prompt="p", status="done"),
            Subtask(subtask_id="s6", prompt="p", status="done"),
            Subtask(subtask_id="s7", prompt="p", status="failed"),
        ])
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=empty_ledger,
        )
        assert (
            "Total: 7 (2 pending, 1 in-flight, 3 done, 1 failed)"
        ) in body

    def test_zero_subtasks_renders_clean(self, empty_ledger):
        task = _make_task()
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=empty_ledger,
        )
        assert "Total: 0 (0 pending, 0 in-flight, 0 done, 0 failed)" in body
        # No section headers for failed / in_flight / pending when empty.
        assert "### Failed subtasks" not in body
        assert "### In-flight subtasks" not in body
        assert "### Next pending subtask" not in body


# ---------------------------------------------------------------------------
# render_resume_summary — stages section
# ---------------------------------------------------------------------------


class TestRenderStages:

    def test_no_stages_omits_stages_section(self, empty_ledger):
        task = _make_task(subtasks=[
            Subtask(subtask_id="s1", prompt="p", status="pending"),
        ])
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=empty_ledger,
        )
        assert "### By stage" not in body

    def test_stages_grouped_with_per_stage_counts(self, empty_ledger):
        task = _make_task(
            stages=[
                Stage(stage_id="research", name="Research"),
                Stage(stage_id="synth", name="Synthesis"),
            ],
            subtasks=[
                Subtask(subtask_id="r1", prompt="p", stage_id="research",
                        status="done"),
                Subtask(subtask_id="r2", prompt="p", stage_id="research",
                        status="done"),
                Subtask(subtask_id="r3", prompt="p", stage_id="research",
                        status="pending"),
                Subtask(subtask_id="syn", prompt="p", stage_id="synth",
                        status="pending"),
            ],
        )
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=empty_ledger,
        )
        assert "### By stage" in body
        assert "**`research`** — Research: 1 pending, 0 in-flight, 2 done, 0 failed" in body
        assert "**`synth`** — Synthesis: 1 pending, 0 in-flight, 0 done, 0 failed" in body

    def test_stage_with_no_name_renders_id_only(self, empty_ledger):
        task = _make_task(
            stages=[Stage(stage_id="alpha")],
            subtasks=[
                Subtask(subtask_id="a1", prompt="p", stage_id="alpha",
                        status="pending"),
            ],
        )
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=empty_ledger,
        )
        # No " — Name" suffix when name is empty.
        assert "**`alpha`**: 1 pending" in body
        assert "**`alpha`** —" not in body

    def test_stage_with_no_subtasks_says_no_subtasks(self, empty_ledger):
        task = _make_task(
            stages=[Stage(stage_id="empty", name="Unused stage")],
        )
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=empty_ledger,
        )
        assert "**`empty`** — Unused stage — no subtasks" in body

    def test_ungrouped_subtasks_call_out_when_stages_present(
        self, empty_ledger,
    ):
        task = _make_task(
            stages=[Stage(stage_id="research")],
            subtasks=[
                Subtask(subtask_id="r1", prompt="p", stage_id="research",
                        status="done"),
                Subtask(subtask_id="legacy", prompt="p",
                        stage_id=None, status="pending"),
            ],
        )
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=empty_ledger,
        )
        assert "_ungrouped_: 1 pending" in body


# ---------------------------------------------------------------------------
# render_resume_summary — failed / in-flight / next-pending
# ---------------------------------------------------------------------------


class TestRenderSubtaskSections:

    def test_failed_section_lists_subtask_with_summary(self, empty_ledger):
        task = _make_task(subtasks=[
            Subtask(subtask_id="bad", prompt="p", status="failed",
                    result_summary="exit_code=1; reason=container_oom"),
        ])
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=empty_ledger,
        )
        assert "### Failed subtasks" in body
        assert "`bad` — exit_code=1; reason=container_oom" in body

    def test_failed_section_truncates_long_summary(self, empty_ledger):
        long_summary = "x" * 500
        task = _make_task(subtasks=[
            Subtask(subtask_id="bad", prompt="p", status="failed",
                    result_summary=long_summary),
        ])
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=empty_ledger,
        )
        # 200-char cap + "..." trailer.
        assert "..." in body
        assert "x" * 500 not in body

    def test_failed_section_handles_missing_summary(self, empty_ledger):
        task = _make_task(subtasks=[
            Subtask(subtask_id="bad", prompt="p", status="failed",
                    result_summary=None),
        ])
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=empty_ledger,
        )
        assert "`bad` — (no summary)" in body

    def test_in_flight_section_shows_started_at(self, empty_ledger):
        task = _make_task(subtasks=[
            Subtask(subtask_id="active", prompt="p", status="in_flight",
                    started_at="2026-05-02T10:00:00+00:00"),
        ])
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=empty_ledger,
        )
        assert "### In-flight subtasks" in body
        assert "`active` — started 2026-05-02T10:00:00+00:00" in body

    def test_next_pending_section_shows_first_pending(self, empty_ledger):
        task = _make_task(subtasks=[
            Subtask(subtask_id="s1", prompt="p", status="done"),
            Subtask(subtask_id="s2", prompt="p", status="pending"),
            Subtask(subtask_id="s3", prompt="p", status="pending"),
        ])
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=empty_ledger,
        )
        assert "### Next pending subtask" in body
        assert "`s2`" in body
        assert "plus 1 more pending" in body

    def test_next_pending_includes_stage_id_when_set(self, empty_ledger):
        task = _make_task(
            stages=[Stage(stage_id="alpha")],
            subtasks=[
                Subtask(subtask_id="s1", prompt="p", stage_id="alpha",
                        status="pending"),
            ],
        )
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=empty_ledger,
        )
        assert "`s1` (stage `alpha`)" in body


# ---------------------------------------------------------------------------
# render_resume_summary — pending checkpoints (Phase 5 §5)
# ---------------------------------------------------------------------------


class TestRenderCheckpoints:

    def test_pending_checkpoint_section_rendered(self, empty_ledger):
        task = _make_task(
            subtasks=[
                Subtask(subtask_id="s1", prompt="p", status="done"),
                Subtask(subtask_id="s2", prompt="p", status="pending"),
                Subtask(subtask_id="s3", prompt="p", status="pending"),
            ],
            checkpoints=[
                Checkpoint(
                    checkpoint_id="review-after-s1",
                    before_subtask="s2",
                    description="Review s1's output before s2.",
                    kind="review-commit",
                ),
            ],
        )
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=empty_ledger,
        )
        assert "## Pending checkpoint" in body
        assert "**`review-after-s1`**" in body
        assert "before `s2`" in body
        assert "kind: `review-commit`" in body
        assert "Review s1's output before s2." in body

    def test_pending_section_omitted_when_no_checkpoints_declared(
        self, empty_ledger,
    ):
        task = _make_task(
            subtasks=[
                Subtask(subtask_id="s1", prompt="p", status="pending"),
            ],
        )
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=empty_ledger,
        )
        assert "## Pending checkpoint" not in body

    def test_pending_section_omitted_when_all_acked(self, empty_ledger):
        task = _make_task(
            subtasks=[
                Subtask(subtask_id="s1", prompt="p", status="done"),
                Subtask(subtask_id="s2", prompt="p", status="pending"),
            ],
            checkpoints=[
                Checkpoint(
                    checkpoint_id="c1", before_subtask="s2",
                ),
            ],
            acked_checkpoint_ids={"c1"},
        )
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=empty_ledger,
        )
        assert "## Pending checkpoint" not in body

    def test_pending_section_omitted_when_position_does_not_match(
        self, empty_ledger,
    ):
        # Checkpoint declared for a subtask that's already done; the
        # next pending subtask is unrelated → not pending.
        task = _make_task(
            subtasks=[
                Subtask(subtask_id="s1", prompt="p", status="done"),
                Subtask(subtask_id="s2", prompt="p", status="pending"),
            ],
            checkpoints=[
                # Position before s1 (already done; loop is past it).
                Checkpoint(checkpoint_id="c1", before_subtask="s1"),
            ],
        )
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=empty_ledger,
        )
        assert "## Pending checkpoint" not in body

    def test_pending_section_without_optional_fields(self, empty_ledger):
        # Description / kind both empty → those lines must be
        # omitted (the section header + the id/before line still
        # render).
        task = _make_task(
            subtasks=[
                Subtask(subtask_id="s1", prompt="p", status="pending"),
            ],
            checkpoints=[
                Checkpoint(checkpoint_id="bare", before_subtask="s1"),
            ],
        )
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=empty_ledger,
        )
        assert "## Pending checkpoint" in body
        assert "**`bare`**" in body
        assert "kind:" not in body

    def test_pending_section_with_no_pending_subtasks_omitted(
        self, empty_ledger,
    ):
        # Defensive: every subtask is done, so there's no "next
        # pending" position to match against.
        task = _make_task(
            subtasks=[
                Subtask(subtask_id="s1", prompt="p", status="done"),
                Subtask(subtask_id="s2", prompt="p", status="done"),
            ],
            checkpoints=[
                Checkpoint(checkpoint_id="c1", before_subtask="s2"),
            ],
        )
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=empty_ledger,
        )
        assert "## Pending checkpoint" not in body


# ---------------------------------------------------------------------------
# render_resume_summary — spend section
# ---------------------------------------------------------------------------


class TestRenderSpend:

    def test_zero_spend_when_no_buffer_rows(self, empty_ledger):
        task = _make_task()
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=empty_ledger,
        )
        assert "Cumulative (Claude-reported): $0.0000" in body
        assert "Cumulative (locally priced): $0.0000" in body
        assert "This invocation (Claude-reported): $0.0000" in body

    def test_cumulative_sums_all_buffer_rows(self, tmp_path):
        state_root = str(tmp_path / "state")
        ledger = buffer_ledger_mod.BufferLedger(state_root=state_root)
        _seed_buffer_row(
            state_root, "t1",
            cost_usd=0.05, claude_reported=0.25,
            timestamp="2026-05-01T00:00:00+00:00",
        )
        _seed_buffer_row(
            state_root, "t1",
            cost_usd=0.10, claude_reported=0.50,
            timestamp="2026-05-01T01:00:00+00:00",
        )
        task = _make_task(state_root=state_root)
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=ledger,
        )
        assert "Cumulative (Claude-reported): $0.7500" in body
        assert "Cumulative (locally priced): $0.1500" in body

    def test_invocation_spend_anchored_to_latest_resume(self, tmp_path):
        state_root = str(tmp_path / "state")
        ledger = buffer_ledger_mod.BufferLedger(state_root=state_root)
        # Pre-resume row.
        _seed_buffer_row(
            state_root, "t1",
            cost_usd=0.05, claude_reported=0.25,
            timestamp="2026-05-01T00:00:00+00:00",
        )
        # Post-resume rows.
        _seed_buffer_row(
            state_root, "t1",
            cost_usd=0.10, claude_reported=0.50,
            timestamp="2026-05-01T11:00:00+00:00",
        )
        _seed_buffer_row(
            state_root, "t1",
            cost_usd=0.20, claude_reported=1.00,
            timestamp="2026-05-01T12:00:00+00:00",
        )
        task = _make_task(state_root=state_root, decisions=[
            Decision(timestamp="2026-05-01T00:00:00+00:00",
                     kind="task_create", note="goal: x"),
            Decision(timestamp="2026-05-01T10:00:00+00:00",
                     kind="task_resume", note="resumed"),
        ])
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=ledger,
        )
        # Invocation = since 2026-05-01T10:00:00 → only the post-resume rows.
        assert "This invocation (Claude-reported): $1.5000" in body
        # Cumulative covers all three.
        assert "Cumulative (Claude-reported): $1.7500" in body


# ---------------------------------------------------------------------------
# render_resume_summary — decisions section
# ---------------------------------------------------------------------------


class TestRenderDecisions:

    def test_no_decisions_says_none_recorded(self, empty_ledger):
        task = _make_task()
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=empty_ledger,
        )
        assert "## Decisions this invocation" in body
        assert "(none recorded yet)" in body

    def test_decisions_filtered_by_invocation_boundary(self, empty_ledger):
        task = _make_task(decisions=[
            Decision(timestamp="2026-05-01T00:00:00+00:00",
                     kind="task_create", note="goal: x"),
            Decision(timestamp="2026-05-01T01:00:00+00:00",
                     kind="worker_spawn", note="spawn s1"),
            Decision(timestamp="2026-05-01T02:00:00+00:00",
                     kind="worker_complete", note="completed s1"),
            Decision(timestamp="2026-05-01T03:00:00+00:00",
                     kind="task_resume", note="resumed"),
            Decision(timestamp="2026-05-01T04:00:00+00:00",
                     kind="worker_spawn", note="spawn s2"),
            Decision(timestamp="2026-05-01T05:00:00+00:00",
                     kind="policy_pause", note="manual pause"),
        ])
        body = resume_summary_mod.render_resume_summary(
            task, buffer_ledger=empty_ledger,
        )
        # Boundary = the latest task_resume @ 03:00. Only events at or after
        # appear under "Decisions this invocation".
        assert "task_resume` — resumed" in body
        assert "worker_spawn` — spawn s2" in body
        assert "policy_pause` — manual pause" in body
        # Pre-boundary events excluded from this invocation list.
        decisions_section = body.split("## Decisions this invocation")[1]
        assert "spawn s1" not in decisions_section
        assert "completed s1" not in decisions_section


# ---------------------------------------------------------------------------
# _suggest_next_action variants
# ---------------------------------------------------------------------------


class TestSuggestNextAction:

    def _suggest(self, *, decisions=None, subtasks=None):
        task = _make_task(decisions=decisions, subtasks=subtasks)
        return resume_summary_mod._suggest_next_action(task)

    def test_no_decisions_suggests_start(self):
        s = self._suggest()
        assert "uas-orchestrate start t1" in s

    def test_policy_pause_suggests_resume(self):
        s = self._suggest(
            decisions=[
                Decision(timestamp="t", kind="task_create", note=""),
                Decision(timestamp="t", kind="policy_pause", note=""),
            ],
        )
        assert "uas-orchestrate resume t1" in s
        assert "5-hour window" in s

    def test_policy_wrap_up_suggests_weekly_resume(self):
        s = self._suggest(
            decisions=[
                Decision(timestamp="t", kind="task_create", note=""),
                Decision(timestamp="t", kind="policy_wrap_up", note=""),
            ],
        )
        assert "7-day window" in s

    def test_policy_halt_suggests_review_threshold(self):
        s = self._suggest(
            decisions=[
                Decision(timestamp="t", kind="task_create", note=""),
                Decision(timestamp="t", kind="policy_halt", note=""),
            ],
        )
        assert "Hard-stop" in s
        assert "hard_stop_usd" in s

    def test_worker_fail_suggests_review(self):
        s = self._suggest(
            decisions=[
                Decision(timestamp="t", kind="task_create", note=""),
                Decision(timestamp="t", kind="worker_fail", note=""),
            ],
        )
        assert "Worker failed" in s

    def test_policy_auto_resume_suggests_pending(self):
        s = self._suggest(
            decisions=[
                Decision(timestamp="t", kind="task_create", note=""),
                Decision(timestamp="t", kind="policy_auto_resume", note=""),
            ],
        )
        assert "Auto-resume" in s

    def test_task_create_with_pending_subtasks_suggests_start(self):
        s = self._suggest(
            decisions=[
                Decision(timestamp="t", kind="task_create", note=""),
            ],
            subtasks=[
                Subtask(subtask_id="s1", prompt="p", status="pending"),
            ],
        )
        assert "uas-orchestrate start t1" in s

    def test_queue_empty_after_completion_suggests_complete(self):
        s = self._suggest(
            decisions=[
                Decision(timestamp="t", kind="task_create", note=""),
                Decision(timestamp="t", kind="worker_complete", note=""),
            ],
            subtasks=[
                Subtask(subtask_id="s1", prompt="p", status="done"),
            ],
        )
        assert "Task complete" in s

    def test_failed_subtasks_remain_says_review_failures(self):
        s = self._suggest(
            decisions=[
                Decision(timestamp="t", kind="task_create", note=""),
                Decision(timestamp="t", kind="worker_complete", note=""),
            ],
            subtasks=[
                Subtask(subtask_id="s1", prompt="p", status="done"),
                Subtask(subtask_id="s2", prompt="p", status="failed"),
            ],
        )
        assert "failed subtasks remain" in s

    def test_task_resume_with_pending_suggests_resume(self):
        s = self._suggest(
            decisions=[
                Decision(timestamp="t", kind="task_create", note=""),
                Decision(timestamp="t", kind="task_resume", note=""),
            ],
            subtasks=[
                Subtask(subtask_id="s1", prompt="p", status="pending"),
            ],
        )
        assert "uas-orchestrate resume t1" in s

    def test_checkpoint_pause_suggests_ack_command(self):
        # Phase 5 §5: the suggestion line must contain the exact
        # ack command including the pending checkpoint id, so a
        # reader can copy/paste without parsing the rest of the
        # digest.
        task = _make_task(
            decisions=[
                Decision(timestamp="t", kind="task_create", note=""),
                Decision(
                    timestamp="t", kind="checkpoint_pause",
                    note="pause",
                ),
            ],
            subtasks=[
                Subtask(subtask_id="s2", prompt="p", status="pending"),
            ],
            checkpoints=[
                Checkpoint(
                    checkpoint_id="review-after-s1",
                    before_subtask="s2",
                    description="Review s1.",
                ),
            ],
        )
        s = resume_summary_mod._suggest_next_action(task)
        assert (
            "./uas-orchestrate resume t1 --ack-checkpoint "
            "review-after-s1"
        ) in s
        # Halt offer is included so the operator has a clear out
        # if they decide not to proceed.
        assert "halt" in s

    def test_checkpoint_pause_without_pending_subtask_falls_back(self):
        # Defensive: if for some reason there's no pending subtask
        # (e.g., log corruption or all subtasks already done), the
        # suggestion still tells the operator a checkpoint is
        # pending.
        task = _make_task(
            decisions=[
                Decision(timestamp="t", kind="task_create", note=""),
                Decision(
                    timestamp="t", kind="checkpoint_pause", note="pause",
                ),
            ],
            subtasks=[
                Subtask(subtask_id="s2", prompt="p", status="done"),
            ],
            checkpoints=[
                Checkpoint(
                    checkpoint_id="ckpt-1", before_subtask="s2",
                ),
            ],
            acked_checkpoint_ids={"ckpt-1"},
        )
        s = resume_summary_mod._suggest_next_action(task)
        # Generic fallback: still tells the operator how to ack.
        assert "checkpoint" in s.lower()
        assert "--ack-checkpoint <id>" in s

    def test_checkpoint_ack_suggests_resume(self):
        s = self._suggest(
            decisions=[
                Decision(timestamp="t", kind="task_create", note=""),
                Decision(timestamp="t", kind="checkpoint_ack", note=""),
            ],
            subtasks=[
                Subtask(subtask_id="s1", prompt="p", status="pending"),
            ],
        )
        assert "Checkpoint acknowledged" in s
        assert "./uas-orchestrate resume t1" in s


# ---------------------------------------------------------------------------
# write_resume_summary — disk round-trip
# ---------------------------------------------------------------------------


class TestWriteResumeSummary:

    def test_writes_to_state_root_task_dir(self, tmp_path):
        state_root = str(tmp_path / "state")
        task = _make_task(state_root=state_root)
        path = resume_summary_mod.write_resume_summary(
            task, state_root=state_root,
        )
        expected = os.path.join(state_root, "t1", "resume_summary.md")
        assert path == expected
        assert os.path.isfile(expected)

    def test_creates_state_dir_if_missing(self, tmp_path):
        state_root = str(tmp_path / "fresh-state")
        task = _make_task(state_root=state_root)
        # Pre-condition: directory does not exist.
        assert not os.path.isdir(os.path.join(state_root, "t1"))
        resume_summary_mod.write_resume_summary(task, state_root=state_root)
        assert os.path.isdir(os.path.join(state_root, "t1"))

    def test_overwrites_existing_summary(self, tmp_path):
        state_root = str(tmp_path / "state")
        os.makedirs(os.path.join(state_root, "t1"))
        existing = os.path.join(state_root, "t1", "resume_summary.md")
        with open(existing, "w") as fh:
            fh.write("STALE")
        task = _make_task(state_root=state_root)
        resume_summary_mod.write_resume_summary(task, state_root=state_root)
        with open(existing) as fh:
            content = fh.read()
        assert "STALE" not in content
        assert "Resume summary" in content

    def test_default_buffer_ledger_uses_state_root(self, tmp_path):
        state_root = str(tmp_path / "state")
        # Seed a buffer row at the canonical location so the
        # auto-instantiated ledger picks it up.
        _seed_buffer_row(
            state_root, "t1",
            cost_usd=0.10, claude_reported=0.30,
            timestamp="2026-05-01T00:00:00+00:00",
        )
        task = _make_task(state_root=state_root)
        resume_summary_mod.write_resume_summary(
            task, state_root=state_root,
        )
        with open(os.path.join(state_root, "t1", "resume_summary.md")) as fh:
            body = fh.read()
        assert "Cumulative (Claude-reported): $0.3000" in body
        assert "Cumulative (locally priced): $0.1000" in body


# ---------------------------------------------------------------------------
# _invocation_boundary
# ---------------------------------------------------------------------------


class TestInvocationBoundary:

    def test_picks_latest_task_resume(self):
        task = _make_task(decisions=[
            Decision(timestamp="2026-05-01T00:00:00+00:00",
                     kind="task_create", note=""),
            Decision(timestamp="2026-05-01T01:00:00+00:00",
                     kind="task_resume", note=""),
            Decision(timestamp="2026-05-01T02:00:00+00:00",
                     kind="worker_spawn", note=""),
            Decision(timestamp="2026-05-01T03:00:00+00:00",
                     kind="task_resume", note=""),
        ])
        assert resume_summary_mod._invocation_boundary(task) == (
            "2026-05-01T03:00:00+00:00"
        )

    def test_falls_back_to_task_create(self):
        task = _make_task(decisions=[
            Decision(timestamp="2026-05-01T00:00:00+00:00",
                     kind="task_create", note=""),
        ])
        assert resume_summary_mod._invocation_boundary(task) == (
            "2026-05-01T00:00:00+00:00"
        )

    def test_falls_back_to_created_at_when_no_decisions(self):
        task = _make_task(created_at="2026-05-01T00:00:00+00:00")
        # No decisions on the timeline; falls back to task.created_at.
        assert resume_summary_mod._invocation_boundary(task) == (
            "2026-05-01T00:00:00+00:00"
        )
