# Phase 4 cut buckets and rationale

This document records what Phase 4 cut and why, organised by the
sectional buckets the Phase 4 PLAN executed against (§3 architect
tree, §4 cut orchestrator + uas/, §5 llm_judge + integration
cleanup + eval surgery, §6 root-level cleanup). Future readers
should consult this file before re-introducing any pre-prune
mechanism — the May 2026 pivot intentionally removed each item
listed here, and the rationale for that decision lives below.

For the file-granularity closed cut list with §3/§4/§5/§6 owner
assignments and a leaves-before-roots deletion order, see
[`cut_list.md`](cut_list.md). For the pre-pivot Phase 2 §4 sized
estimate the §1 deliverable finalised, see
[`cut_surface.md`](cut_surface.md). For the pre-prune
68-mechanism catalog, see [`../phase0_audit.md`](../phase0_audit.md).

Final shrinkage (verified at §7): **111 file deletions + 5
in-place edits + 1 new fixture + 1 binary** ≈ **−44,989 net text
lines** (sum of `wc -l` over surviving text-extension files
dropped from ≈ 62,064 to 16,256; tracked file count dropped
from 164 to 55).

## §3 — `architect/` tree

**Cut.** `architect/` package (16 source files, 15,649 lines) plus
62 architect-importing tests (19,726 lines) plus 4 transitive
§4-bucket tests pulled forward when `orchestrator/main.py:25`
(`from architect.git_state import …`) broke their collection
post-§3. **Total: 82 files, 38,119 deletions.**

**Rationale.** `architect/main.py` (6,915 lines) was the
pre-pivot scaffold's failure-handling and validation cascade —
~37 distinct mechanisms (input/output quality, validation
cascade, retry decisions, git checkpoint+rollback, TDD
enforcement, smoke-test entry, holistic validation, post-run
meta-learning, attempt history, truncation detection, etc.).
`architect/planner.py` (3,700 lines) was the coverage-driven
decomposition and replanning engine — ~20 distinct mechanisms
(multi-plan voting, complexity gate, step merging, integration
checkpoints, TDD planning, coverage matrix, fill-gaps, ensure-
goal-coverage, step enrichment, goal expansion, research phase,
generate reflection, classify error heuristic, counterfactual
root-cause, reflect-and-rewrite, decompose failing step, replan
remaining steps, corrective steps).

The May 2026 ROADMAP pivot reframed UAS from "best autonomous
agent harness" to "usage-limit-aware long-horizon orchestrator
for Claude Max". Under the new framing, the orchestrator
delegates subtask execution to headless `claude --print`
workers; Claude Code 2026 (sub-agents, hooks, MCP, skills, 1M
context, native session compaction) duplicates or fights the
architect's mechanisms. The §10 empirical evidence — Opus 4.7
plus all 68 mechanisms enabled spent 38 min and $16 over-
decomposing a hello-file task into TDD steps and never produced
the file — was direct evidence that the scaffold's coupling
cost dominates on at least the trivial tier.

**What replaces it.** The orchestrator's worker primitive
(`orchestrator/worker.py`) spawns one `claude --print` per
subtask. Decomposition is human-authored in the per-task TOML
specs at `orchestrator/cases/<task>.toml`. Verification, when
needed, uses Claude Code's native sub-agents or the eval
harness's deterministic checks (`integration/eval.py:run_check`).

**Mechanism mapping (per `phase0_audit.md` clusters).** Cluster
A (Reflection / retry decision: 11 mechanisms), Cluster B
(Counterfactual + backtrack: 2), Cluster D (Validation cascade:
7), Cluster E (Coverage-driven planning + replan: 6), Cluster F
(TDD pair: 3), Cluster G (Git state pair: 2), Cluster H (Step
DAG transforms: 4), Cluster I (Persistence + recovery: 3),
Cluster J (Post-run learning loop: 3) — **all CUT**, all owned
by §3.

## §4 — cut orchestrator legacy + `uas/` tree

**Cut.** `orchestrator/main.py` (2,051 lines), `llm_client.py`
(418), `claude_config.py` (246), `parser.py` (220) — the
pre-Phase-3 orchestrator. Plus `uas/` package: `fuzzy.py`
(155), `janitor.py` (114), `fuzzy_models.py` (65),
`__init__.py` (0). Plus 6 dedicated tests
(`test_janitor`, `test_claude_config`, `test_llm_client`,
`test_llm_isolation`, `test_parser`, `test_framework_layout`).
**Total: 14 files, 4,917 deletions.**

**Rationale.** `orchestrator/main.py` was the pre-pivot
"orchestrator" in name only — it was the per-step subprocess
driver under the architect, hosting Best-of-N family (4
mechanisms), PyPI version resolution, pre-flight LLM check,
code quality fuzzy, retry-clean prompt. None of these serve
the new orchestrator's scheduling-and-budgeting role. The
pivot's new orchestrator is a wholly new module
(`orchestrator/cli.py` + supporting modules from Phase 3 §1–§8),
not a refactor of the legacy file.

`uas/fuzzy.py` and `uas/janitor.py` supported the Best-of-N
pre-flight check and the context janitor mechanism (#54)
respectively — both architect-coupled adjacent infrastructure
that has no consumer in the post-prune system.

**What replaces it.** `orchestrator/cli.py` (566 lines) is the
new daemon entrypoint with subcommands `start / status / pause /
resume / halt`. Worker spawning lives in `orchestrator/worker.py`
(268 lines). Three-state policy machine in `orchestrator/policy.py`
(359 lines). Task / subtask / decision model in
`orchestrator/task.py` (688 lines). Phase 3 designed and built
each from scratch around the orchestrator's actual job;
preserving the legacy `main.py` would have meant maintaining
two different "orchestrator" identities in one tree.

**Mechanism mapping.** Cluster C (Best-of-N family: 4
mechanisms #46–#49), plus rows #50 (PyPI version), #51 (pre-
flight LLM), #52 (code quality fuzzy), #53 (retry-clean
prompt), #54 (context janitor) — **all CUT**.

## §5 — `integration/llm_judge.py` + ML-class quality + quick_test + `eval.py` surgery

**Cut.** `integration/llm_judge.py` (470 lines, the LLM-as-judge
module), `integration/test_project_quality.py` (245, the
ML-project-class quality gate), `integration/quick_test.sh`
(113, an architect-runner shell wrapper), `tests/test_llm_judge.py`
(899). Plus surgery on `integration/eval.py` removing the
`_import_llm_judge` dispatcher, the `llm_judge` branch in
`run_check`, the architect-coupled `invoke_architect` body
(replaced by a no-op stub), and `collect_metrics`. Plus
restructure of `integration/cases/trivial/hello-file.json` to
use `setup_files` for substrate self-test semantics, plus a
new `integration/data/hello.txt` fixture (15 bytes). **Total: 4
file deletions + eval.py surgery + case restructure + 1 new
fixture, ≈ 1,973 net deletions.**

**Rationale.** ROADMAP §Phase 4 keep list explicitly excludes
the LLM-as-judge module — Claude Code sub-agents fill that role
natively in the post-pivot framing. The ML-project-class
quality gate was a smoke template for one specific project class
(`PROJECT_WORKSPACE`-driven), not on the keep list and not
serving the orchestrator's role. `integration/quick_test.sh` ran
the architect inside the container, which §3 deleted.

The `eval.py` surgery converts the harness from "architect-
runner" to "substrate self-tester". The architect was the
producer the harness historically invoked; with architect gone,
the harness no longer has anything to invoke. The post-prune
harness validates the substrate itself: case loader, workspace
setup, deterministic checks, JSONL persistence, resume. The
hello-file case becomes a tautology that smokes the substrate's
end-to-end plumbing rather than testing any producer.

**What replaces it.** Nothing — the LLM-as-judge module's role
is now Claude Code sub-agents (configured via `.claude/`
settings, not part of UAS). The ML quality gate has no
replacement; it was project-class-specific and not on the
substrate keep-list. The `invoke_architect` no-op stub preserves
the function signature for monkey-patching tests
(`tests/test_eval_resume.py`) but does no work. The hello-file
fixture's pre-population via `setup_files` makes the case a
self-test; future cases that need a producer can spawn an
orchestrator worker explicitly.

**Mechanism mapping.** No phase0_audit.md mechanisms map to §5
(llm_judge was not in the catalog; ML quality gate was not in
the catalog; the eval-runner mechanisms that were cataloged
are part of the substrate keep-list with surgery, not deletion).

## §6 — root-level cleanup (trio + scripts + tools + Containerfile/setup_auth/substrate surgery)

**Cut.** NEEDS-PHASE-3-DECISION trio: `uas_config.py` (216
lines, the layered config loader), `uas_hooks.py` (215, the
lifecycle hook system), `uas.example.toml` (79, the config-key
discovery aid). Plus `tests/test_hooks.py`. Plus 5 root-level
shell scripts: `install.sh` (132), `run_local.sh` (29),
`run_container.sh` (62), `start_orchestrator.sh` (87),
`entrypoint.sh` (81) — all architect-coupled. Plus
`tools/statusline_probe.sh` (27, the Phase 2 §1 probe). Plus
`screenshot.png` (~266 KB binary — referenced only by the
pre-prune README's TUI-dashboard image). Plus surgery on
`Containerfile` (53 → 40 lines), `setup_auth.sh` (3 → 4 lines),
and `docs/substrate.md` (3 edits). **Total: 11 file deletions
+ 1 binary + 3 surgeries, ≈ 1,220 net deletions text + 1 PNG.**

**Rationale.** The trio's fate was deferred at Phase 3 §1
("trio defaults to CUT for Phase 4 if §2–§8 close without
consumption"). Phase 3 §1–§8 closed without consuming the
layered loader (every Phase 3 module reads its task-specific
TOML via `tomllib` directly), so the default-CUT verdict fired.
PLAN §2 confirmed the audit: post-§6 keep-list importer count is
zero across all three (the only soft surface,
`integration/provenance.py:_hash_active_config`, returns
`"unavailable"` when `uas_config.py` is absent — verified live in
§7 where the JSONL row's `config_hash` field showed `"unavailable"`).

The architect-runner shell scripts (`install.sh`,
`run_local.sh`, `run_container.sh`, `start_orchestrator.sh`,
`entrypoint.sh`) all dispatched to `architect.main` or
`architect.state` somewhere in their flow. With the architect
deleted in §3, every dispatch target is gone; the scripts are
non-functional and there are no users to support backward-
compatibly.

`tools/statusline_probe.sh` was the Phase 2 §1 probe used to
investigate Claude Code's statusline rate-limit JSON during
Phase 2 verification. It was kept on disk per Phase 2 §1
Results pending a "natural-trigger capture" of the
percentage-cap question (whether `used_percentage` caps at 100
or climbs past 100 in buffer mode). No keep-list enumeration
includes it; default-cut applies. The natural-trigger capture
plan in `docs/substrate.md` § Open questions retires its
on-disk capture surface; future capture requires re-authoring
the probe.

`screenshot.png` was the pre-prune README's TUI-dashboard
image. The post-prune system has no TUI dashboard
(`architect/dashboard.py` was cut in §3); the README rewrite
in §8 omits the image reference.

The Containerfile surgery slims the engine image to Python 3.12
+ Node + Claude Code CLI + git + uv + the kept `orchestrator/`
tree, with no default ENTRYPOINT. The `IS_SANDBOX=1` env var
that was image-baked moved to per-spawn override in
`orchestrator/worker.py` (the §7 regression repair confirmed
this is the correct shape).

**What replaces it.** Nothing on the trio side — the orchestrator
reads per-task TOML directly via `tomllib`, no layered loader
needed. Nothing on the scripts side — `setup_auth.sh` (KEEP)
points users at `./uas-eval` (lazy build) or manual `podman build`
for the image-build path. The TUI dashboard has no replacement;
the post-prune system is API-only.

**Mechanism mapping.** No phase0_audit.md mechanisms map to §6
proper (the trio was config infrastructure, not a "mechanism" in
the catalog sense; the scripts were pre-prune entrypoints, not
mechanisms). The §6 cuts are the cleanup that closes Phase 4 by
removing the now-unused supporting infrastructure for the cut
mechanisms.

## What was kept and why

For completeness — see `cut_list.md` § "KEEP" for the file-by-
file listing:

- **Phase 1 substrate** (per ROADMAP §Phase 4 keep list 1–7):
  Docker sandbox + `Sandbox.Dockerfile` (`orchestrator/sandbox.py`),
  OAuth 4-stage refresh (`integration/auth.py`), JSONL audit log
  primitive (`integration/eval.py:append_result_row`), provenance
  metadata capture (`integration/provenance.py`), workspace
  isolation pattern, resume-from-JSONL logic, eval harness shell
  wrapper + deterministic check types + hello-file smoke case.
- **Phase 2 substrate addition**: rate-limit read pattern
  (stream-json `rate_limit_event` from `claude --print`).
- **Phase 3 deliverables (entire)**: the orchestrator daemon
  (`orchestrator/cli.py`, `worker.py`, `container.py`,
  `rate_ledger.py`, `buffer_ledger.py`, `pricing.py`,
  `policy.py`, `policy.default.toml`, `task.py`, `workspace.py`,
  `cases/`).
- **Documentation**: ROADMAP, CLAUDE, README (rewritten in §8),
  PLAN (during phase), `phase0_audit.md` (historical),
  `docs/{substrate,orchestrator,cut_surface,cut_list,cut_bucket}.md`,
  LICENSE.
- **Build / run infrastructure**: `Containerfile` (with §6
  surgery), `pytest.ini`, `requirements.txt`, `.gitignore`,
  `.containerignore`, `setup_auth.sh` (with §6 surgery),
  `framework_settings.json`, `uas-eval`, `uas-orchestrate`.

## How to read this in the future

If a future session is tempted to re-introduce any cut item:

1. **Check `cut_list.md`** for the file-granularity decision —
   was the item explicitly cut, kept, or deferred?
2. **Check the relevant § section above** for the rationale —
   what was the cut driven by, and what (if anything) replaces
   it under the post-pivot framing?
3. **Re-read ROADMAP §Phase 6+ standing rules** —
   - Any new orchestrator capability is justified by a real
     long-horizon task experience, not by speculation.
   - If Claude Code itself can do X natively, configure it via
     skills / sub-agents / hooks / MCP rather than re-implementing
     X in UAS.

If the answer to "should this come back?" survives those gates,
the next phase's PLAN should add it explicitly with a new ROADMAP
entry, not silently restore the pre-prune code.
