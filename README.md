# UAS

Personal research harness for usage-limit-aware long-horizon
orchestration of headless Claude Code workers. Built by @eturkes
for personal use only — not a product, no users to support, no
shipping deadline. Direction and rationale live in
[`ROADMAP.md`](ROADMAP.md); session protocol lives in
[`CLAUDE.md`](CLAUDE.md).

This is the post-Phase-4 slim system: a Python daemon
(`orchestrator/`) that spawns headless `claude --print` workers in
a sandboxed container against per-task TOML specs, reads
Claude Max usage-limit signals from each worker's stream-json
output, and persists task state across invocations so
multi-day tasks survive 5-hour and weekly window boundaries. A
small substrate (`integration/`) provides OAuth refresh, JSONL
audit logging, provenance capture, workspace isolation, and a
deterministic-check eval harness.

## Quickstart

**Prerequisites.**

- Linux host with `podman` or `docker` on `$PATH`.
- Python 3.12+ (the orchestrator runs on the host; workers run in
  the container).
- A Claude Max subscription. Authentication uses Claude Code's
  OAuth flow; the orchestrator does not consume an Anthropic API
  key.

**One-time setup.**

```bash
# Build the engine image and authenticate Claude Max.
./setup_auth.sh
```

`setup_auth.sh` discovers the container engine, ensures
`uas-engine:latest` is built (or prints the build command), then
launches an interactive `claude` session inside the container so
you can authenticate. Credentials persist to `.uas_auth/` and are
reused by every subsequent run.

If `setup_auth.sh` reports the image is missing, build it via
either path:

```bash
# Path A: lazy build via the eval harness.
./uas-eval

# Path B: manual build.
podman build -t uas-engine:latest -f Containerfile .
```

**Run a task.**

Define the task in `orchestrator/cases/<task>.toml` with
`task_id`, `goal`, and a list of `[[subtasks]]` blocks. See
`orchestrator/cases/synthetic-multistep.toml` for the canonical
shape. Optional per-task policy overrides go in
`orchestrator/cases/<task>-policy.toml`; defaults live in
`orchestrator/policy.default.toml`.

```bash
# Start a task end-to-end.
./uas-orchestrate start <task>

# Status snapshot.
./uas-orchestrate status <task>

# Pause manually (current window drains naturally).
./uas-orchestrate pause <task>

# Resume after a pause or window-boundary stop.
./uas-orchestrate resume <task>

# Hard halt (records a decision, no further spawns).
./uas-orchestrate halt <task>
```

Per-task state persists under `orchestrator/state/<task>/`:
`task_events.jsonl` (append-only audit log),
`rate_limits.jsonl` (one `rate_limit_event` row per worker
spawn), `buffer.jsonl` (paid-buffer ledger from per-call
`usage.*` tokens × pricing table), and `policy.toml` (the active
per-task policy snapshot).

The `--simulate-rate-status {allowed,five_hour_pause,
seven_day_wrap_up}` flag bypasses the live rate-limit read for
testing the policy machine without burning real Claude Max
budget.

**Smoke gate.**

```bash
./uas-eval
```

Runs the substrate self-test
(`integration/cases/trivial/hello-file.json`) — verifies the case
loader, workspace setup, deterministic checks, and JSONL
persistence are unbroken. Exit 0 on all checks pass; non-zero on
any FAIL. Append-only results log at
`integration/eval_results.jsonl`.

## Layout

```
.
├── orchestrator/             # The daemon (Phase 3 deliverables)
│   ├── cli.py                # uas-orchestrate entrypoint
│   ├── worker.py             # Headless `claude --print` spawn primitive
│   ├── container.py          # uas-engine image build / inspect
│   ├── rate_ledger.py        # Usage-limit ledger from stream-json events
│   ├── buffer_ledger.py      # Paid-buffer ledger from per-call usage
│   ├── pricing.py            # Pricing table for buffer ledger
│   ├── policy.py             # Three-state policy machine (free/paid/halt)
│   ├── policy.default.toml   # Default policy thresholds
│   ├── task.py               # Task / Subtask / Decision model + JSONL
│   ├── workspace.py          # Per-task workspace setup
│   ├── sandbox.py            # Phase 1 sandbox primitive (legacy substrate)
│   └── cases/                # Per-task TOML specs
│
├── integration/              # Substrate (Phase 1 keep-list)
│   ├── auth.py               # OAuth 4-stage refresh
│   ├── eval.py               # Substrate self-test harness
│   ├── provenance.py         # git/config/env metadata capture
│   ├── cases/trivial/        # Hello-file substrate smoke case
│   └── data/                 # Case fixture files (setup_files source)
│
├── tests/                    # Pytest suite (orchestrator + eval)
├── docs/                     # Substrate, orchestrator, cut-bucket docs
│
├── uas-eval                  # Eval harness shell wrapper
├── uas-orchestrate           # Orchestrator CLI shell wrapper
├── setup_auth.sh             # OAuth setup + image build pointer
├── Containerfile             # uas-engine:latest image definition
├── framework_settings.json   # Canonical Claude Code settings
├── pytest.ini                # Test runner config
├── requirements.txt          # Python deps (host side)
│
├── README.md, CLAUDE.md, ROADMAP.md, PLAN.md (when active)
├── phase0_audit.md           # Phase 0 mechanism catalog (historical)
└── LICENSE                   # Apache 2.0
```

## Configuration

**Per-task TOML cases** at `orchestrator/cases/<task>.toml`:

```toml
task_id = "<task>"
goal = "<one-paragraph goal>"

[[subtasks]]
subtask_id = "s1-<short-name>"
prompt = """
<the prompt the worker receives, multi-line OK>
"""

[[subtasks]]
subtask_id = "s2-<short-name>"
prompt = """
…
"""
```

**Optional per-task policy overrides** at
`orchestrator/cases/<task>-policy.toml` — merged over
`orchestrator/policy.default.toml`. Define soft-cap thresholds
and actions for the 5-hour / weekly windows and the paid-buffer
budget. See the default file for the schema.

**Environment.** The orchestrator does not consume `UAS_*` config
keys (the pre-prune layered loader was deleted in Phase 4 §6).
Two environment variables still apply:

- `UAS_HOST_UID` / `UAS_HOST_GID` — forwarded into the container
  by `uas-eval` / `uas-orchestrate`. The worker's exit trap
  chowns each per-task workspace back to these values so the
  launching user owns the artefacts after a run.

## Pointers

- [`ROADMAP.md`](ROADMAP.md) — strategic direction, phase
  sequence, completed-phase archive, principles.
- [`CLAUDE.md`](CLAUDE.md) — session protocol for working in this
  repo with Claude.
- [`docs/orchestrator.md`](docs/orchestrator.md) — orchestrator
  daemon design notes (Phase 3).
- [`docs/substrate.md`](docs/substrate.md) — substrate boundary
  catalog (Phase 2).
- [`docs/cut_bucket.md`](docs/cut_bucket.md) — Phase 4 cut
  buckets and rationale, so future readers do not re-introduce
  things the May 2026 pivot intentionally removed.
- [`docs/cut_list.md`](docs/cut_list.md) — Phase 4 closed cut
  list at file granularity.
- [`docs/cut_surface.md`](docs/cut_surface.md) — Phase 2 §4
  sized cut estimate.
- [`phase0_audit.md`](phase0_audit.md) — pre-prune 68-mechanism
  catalog (historical).

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
