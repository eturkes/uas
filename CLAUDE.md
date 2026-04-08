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

## Do not

- Add new self-correction mechanisms until the eval harness (Phase 1)
  exists and shows they help.
- Introduce new scope or goals without updating `ROADMAP.md` first.
- Create ad-hoc debugging scaffolding in response to single failed
  runs.
- Touch code outside the scope of the active `PLAN.md` section.
- Treat anecdotes from one failed run as evidence of a general
  pattern. The signal is always noisier than it looks.
