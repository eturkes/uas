# Orchestrator design notes (Phase 3)

This document captures the Phase 3 orchestrator's surface decisions
and persistence layout. The authoritative phase scope is
`ROADMAP.md` §Phase 3 — Orchestrator core; this file records the
choices made under that scope at PLAN approval time so later
sections (and Phase 4 / 5 readers) do not have to reconstruct them
from `PLAN.md` git history.

Substrate context: `docs/substrate.md` (the eight Phase 1 / Phase 2
components the orchestrator consumes); cut context:
`docs/cut_surface.md` (what Phase 4 will delete and therefore what
Phase 3 must avoid depending on).

## Decisions committed at §1 approval

- **Module location.** New code lives under `orchestrator/`,
  coexisting with `orchestrator/sandbox.py` (KEEP) and
  `orchestrator/main.py` (CUT-in-Phase-4) for the duration of
  Phase 3. New files do **not** import from `orchestrator/main.py`,
  so Phase 4's deletion leaves the new code intact.
- **Entry point.** `uas-orchestrate` shell wrapper at repo root,
  parallel to `uas-eval`. Each invocation runs to the next policy
  boundary or task completion; no long-running daemon process.
- **Rate-limit read pattern.** Option 1 (ternary-only) per
  `docs/substrate.md` §8 and ROADMAP §Pivot. The orchestrator
  parses `rate_limit_event` from each worker's own stream-json
  output. TUI companion / internal accumulator alternatives are
  deferred to Phase 5+ if ternary transitions feel too sharp on
  real-task experience.
- **Version key.** `ORCHESTRATOR_VERSION = "phase3"` lives in
  `integration/provenance.py` and is recorded as a separate
  provenance key alongside `harness_version`. Eval continues to
  emit `harness_version="phase1"` only — orchestrator callers
  opt into `orchestrator_version` via
  `capture_run_metadata(include_orchestrator_version=True)` so
  prior eval JSONL rows remain resume-eligible.
- **Auth / provenance lift target.** `integration/auth.py` and
  `integration/provenance.py` as peer modules in the existing
  `integration/` package. Eval re-exports the lifted names so
  existing callers and monkeypatch targets resolve unchanged.
- **NEEDS-PHASE-3-DECISION trio.** Fates of `uas_config.py`,
  `uas_hooks.py`, and `uas.example.toml` are deferred to natural
  trigger — decided when the first orchestrator section actually
  wants to consume one. Initial expectation: small task-specific
  TOML files via `tomllib` are sufficient; the layered loader is
  not consumed; trio defaults to CUT for Phase 4 if §2–§8 close
  without consumption.

## Package layout

    <repo>/
    ├── uas-orchestrate              # entry-point shell wrapper
    ├── orchestrator/
    │   ├── __init__.py
    │   ├── cli.py                   # argparse skeleton (§1)
    │   ├── worker.py                # headless-worker primitive (§2)
    │   ├── container.py             # engine-image precondition (§2)
    │   ├── rate_ledger.py           # usage-limit ledger (§3)
    │   ├── pricing.py               # model -> per-token cost (§4)
    │   ├── buffer_ledger.py         # paid-buffer ledger (§4)
    │   ├── policy.py                # 3-state policy machine (§5)
    │   ├── policy.default.toml      # committed policy defaults (§5)
    │   ├── task.py                  # Task / Subtask / Decision (§6, §7)
    │   ├── workspace.py             # resume-safe workspace setup (§6)
    │   ├── cases/                   # task spec TOMLs (§6, §8)
    │   ├── state/                   # per-task persistent state (runtime)
    │   ├── sandbox.py               # KEEP (Phase 1 substrate)
    │   └── main.py                  # CUT-in-Phase-4 (do not import)
    ├── integration/
    │   ├── auth.py                  # lifted from eval.py (§1)
    │   ├── provenance.py            # lifted from eval.py (§1)
    │   └── eval.py                  # re-exports lifted names

## Subcommand catalog

`uas-orchestrate <subcommand> <task>` where subcommands are:

- `start`   — create the task fresh from
  `orchestrator/cases/<task>.toml`; if a `task_events.jsonl`
  already exists for the task, route to `resume` instead (§7).
- `status`  — print the recovered state summary (subtasks per
  status, total spend, last decision) without spawning workers.
- `resume`  — replay `task_events.jsonl`, re-enqueue any
  `in_flight` subtasks, then continue the orchestrator loop.
  Phase 5 §5 added `--ack-checkpoint <id>` for clearing one
  declared `[[checkpoints]]` entry before re-entering the loop;
  plain `resume` re-pauses at any pending checkpoint position by
  design.
- `pause`   — record a `policy_pause` decision and exit cleanly
  so the active 5h or 7d window can drain.
- `halt`    — record a `policy_halt` decision and stop the
  orchestrator. Resume requires explicit operator intervention.

## Per-task state-directory layout

    <repo>/orchestrator/state/<task_id>/
    ├── task_events.jsonl    # append-only timeline of decisions (§6, §7)
    ├── rate_limits.jsonl    # one row per worker rate_limit_event (§3)
    ├── buffer.jsonl         # one row per worker terminal result (§4)
    ├── policy.toml          # optional per-task policy override (§5)
    └── resume_summary.md    # human-readable digest, rewritten per CLI invocation (Phase 5 §4)

The per-task workspace itself lives at
`<repo>/integration/workspace/<task_id>/` per substrate doc §5,
created via `orchestrator/workspace.py::setup_task_workspace` —
the resume-safe variant that never destroys existing files.

## Open questions

- **NEEDS-PHASE-3-DECISION trio fates** (`uas_config.py`,
  `uas_hooks.py`, `uas.example.toml`) — pending natural-trigger
  decisions in §2 onward; default to CUT for Phase 4 if no
  section consumes them.
- **Statusline-probe absorption** —
  `tools/statusline_probe.sh` is currently kept on disk per
  Phase 2 §2 as deferred natural-trigger capture support; whether
  it migrates into `orchestrator/` proper or stays a tool remains
  to be decided when §2 actually tests the read pattern.
- **Percentage-cap behaviour** — the dual-behaviour design
  constraint logged in `docs/substrate.md` § Open questions still
  applies: §3's `current_status()` must tolerate either capped or
  uncapped `used_percentage` values until natural-trigger capture
  resolves which Claude Code emits.
