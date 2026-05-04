# UAS — Personal Research Harness

Maintained by @eturkes for personal use only. Not a product. No users,
no shipping deadline, no scope constraint other than "maximum
reliability on long-horizon autonomous tasks, at any cost."

## Session protocol

On every new session, read these in order before touching code:

1. **This file** (auto-loaded).
2. **`ROADMAP.md`** — strategic direction, current phase pointer,
   principles, baseline metrics, completed-phase archive.
3. **`PLAN.md`** if present — tactical work for the current phase,
   following the project's existing `## Section N — Title` /
   `**Status:**` convention.

Then resume in-progress sections, or start the next pending one.
If the active `PLAN.md` is missing but `ROADMAP.md` marks a phase
as active, write a fresh `PLAN.md` for that phase before starting
work.

## Core principles (non-negotiable)

These exist because the project accumulated 363 commits of reactive
mechanism-adding before a measurement instrument existed. Every
principle below is a direct correction.

1. **No new mechanism without an eval-visible win.** A single failed
   run is not evidence. Benchmark delta or it didn't happen.
2. **Every mechanism must be ablatable.** If you can't cleanly disable
   it, you can't measure whether it earns its keep.
3. **Deletion is as valuable as addition.** When data doesn't support
   a feature, remove it. Shorter code is better code.
4. **Strong verification over strong correction.** Complexity budget
   belongs in checking outputs, not in recovering from bad ones.
5. **The scaffold cannot exceed the model.** Past a certain coupling
   cost, added correction logic trades reliability for fragility.
6. **Measure before you change.** Every potentially impactful change
   runs: baseline eval → change → eval → delta recorded in
   `ROADMAP.md`.

## Commit conventions

- Imperative mood, ~50–70 char subject.
- Existing patterns in this repo: `Add X`, `Fix Y`, `Rename X`,
  `Scope Z via ...`, `Capture Section N blocker`,
  `Mark Section N completed`, `Remove completed PLAN file`.
- One semantic change per commit. Create new commits rather than
  amending published ones.
- PLAN files live only for the duration of a phase — create when the
  phase starts, delete in a final `Remove completed PLAN file` commit
  when the phase closes.

## Decision protocol

### Default: autonomous execution

Default mode is "execute, not ask." When the project owner opens
a session with the standard bootstrap prompt (see § Reusable
session-bootstrap prompt below), drive the next pending PLAN
section to close without going back for owner confirmation,
picks, or judgements.

Concretely:

- **PLAN steps are pre-approved.** Any action explicitly
  specified in a `PLAN.md` section's Steps list is pre-approved.
  Do the work, append to Results, update `**Status:**`, move on.
- **Owner-judgement steps** (readability reviews, utility
  checks, picks between equal-cost options, "owner observes"
  steps): write your own best-judgement read into Results with
  an `_(assistant stand-in — owner async review pending)_`
  marker. The owner can amend on a future session. Do not
  block.
- **Real-budget operations within PLAN scope** (e.g.,
  `./uas-orchestrate start` / `resume`, real Claude Max spend):
  execute. PLAN hard-abort thresholds and policy hard-stops ARE
  the budget gates; do not add a second gate by asking. Surface
  the budget delta in Results on completion, not before.
- **Discovered state that doesn't contradict PLAN intent**
  (a kickoff that ran outside this session, an artefact
  populated unexpectedly, a renamed-but-equivalent file, etc.):
  document the discovery in Results, continue from current
  state, do not roll back.
- **Substrate findings + PLAN gaps surfaced during execution**:
  capture in Results as flagged findings for §7 / Phase 6+. Do
  not stop to ask whether to flag.
- **Pre-flight contradictions that don't change PLAN intent**
  (e.g., pre-flight expected an empty directory and found a
  populated one with state matching PLAN goals): record the
  discovery, pick the obvious resolution, continue. Only
  escalate if no obvious resolution exists.

### Ask before executing

The "Ask before executing" gates remain in force ONLY for:

- **PLAN ambiguity that no reasonable interpretation
  resolves.** "Reasonable" is generous — if you can write a
  coherent stand-in Results note and the section's acceptance
  criteria still make sense, that's reasonable.
- **Deviations from PLAN that materially change the section's
  outcome.** Minor adjustments go in Results without asking.
- **Blast radius outside the active PLAN scope** — rebases,
  remote pushes, force-pushes, touching files unrelated to
  current sections, destructive operations beyond what the
  PLAN authorises, container-engine operations beyond what
  the PLAN specifies.
- **`docs/cut_bucket.md` re-introduction gate** — any "should
  we add X back from the cut bucket?" question. The cut bucket
  is intentional; re-introductions need explicit owner sign-off.
- **Phase transitions** — see § Phase transitions below.

If the assistant finds itself wanting to ask the owner anything
else, the answer is almost always: write the assistant's best
judgement into Results, mark the stand-in, continue, and surface
the wishlist at the end of the session in a single closing
message.

### Long-wait handling

If a PLAN step requires waiting more than ~1 hour (e.g., a
seven_day window reset, an Anthropic rate-limit cooldown), do
not `ScheduleWakeup` or sleep. Instead:

1. Commit progress so far.
2. Write a "next session" pointer into the section's Results,
   naming exactly the command the next session should run
   (typically a single `./uas-orchestrate resume <task>` line).
3. End the session cleanly with a one-line summary.

The project owner re-opens later with the bootstrap prompt and
the assistant resumes from Results. This keeps each session's
context window focused on a bite-size unit of work.

### Phase transitions require a review gate

When `ROADMAP.md` marks a phase as active but no `PLAN.md` exists:
draft the new `PLAN.md`, commit it as a standalone commit with a
subject matching `Add PLAN for Phase N`, then **stop and ask for
user review** before executing Section 1. Phase planning is
strategic work the user wants to see before execution starts. Do
not chain "draft PLAN" and "execute Section 1" in the same session
turn.

## Reusable session-bootstrap prompt

The project owner's standard session-opening prompt is:

> Continue UAS work per CLAUDE.md

This means autonomous mode (per § Decision protocol → Default).
The session ends when:

- The next pending PLAN section closes.
- The assistant hits one of the "Ask before executing" gates.
- A long wait per § Long-wait handling forces a clean session
  close.
- The assistant judges the context window is too full to
  productively continue (commit progress + clean handoff
  note before ending).

The owner does not need to give in-session confirmations,
go-aheads, picks, or judgements under this prompt. If a
question lands anyway, it represents one of the four exit
conditions above.

## Do not

- Add new self-correction mechanisms until the eval harness (Phase 1)
  exists and shows they help.
- Introduce new scope or goals without updating `ROADMAP.md` first.
- Create ad-hoc debugging scaffolding in response to single failed
  runs.
- Touch code outside the scope of the active `PLAN.md` section.
- Treat anecdotes from one failed run as evidence of a general
  pattern. The signal is always noisier than it looks.
