# PLAN — Phase 2: Substrate verification

Phase reference: `ROADMAP.md` §Phase 2 — Substrate verification
(under the May 2026 pivot — see ROADMAP §Pivot for context).

This phase exists to confirm the substrate from Phase 1 plus the
Claude Code v2.1.80 `rate_limits` statusline JSON field actually
support the orchestrator design before Phase 3 starts building.
Two empirical TBDs and two documentation deliverables; no new
mechanism, no architectural commitments beyond what's required to
answer the empirical questions.

This PLAN is a draft. Per the project's decision protocol, Phase 2
work does not begin until the project owner has reviewed and
approved this file.

## Phase exit criteria (from ROADMAP.md)

Both empirical TBDs resolved with documented findings (or §2
explicitly deferred per its cost-vs-information decision step);
substrate-boundary doc exists at `docs/substrate.md`;
expected-delete list (estimated scale) recorded at
`docs/cut_surface.md`.

## Scope discipline (non-negotiable)

- **No new mechanisms.** This phase verifies and documents; it
  does not add to the codebase beyond what's required to run the
  empirical probes. A statusline-probe script and a small probe
  driver are the only new code allowed, and both delete at phase
  close unless they're explicitly absorbed into the substrate.
- **No starting Phase 3 work.** The orchestrator design lives in
  Phase 3. If Phase 2 surfaces a substrate gap, document the gap
  in `docs/substrate.md`; do not begin building against it.
- **No starting Phase 4 work.** Phase 4's per-mechanism verdicts
  are out of scope. §4's preview is sized estimates only — bucket
  counts and line counts, not file-by-file decisions.
- **Sections are linear.** §2 consumes §1's findings (the
  rate_limits read pattern). §3 consumes §1's findings to
  document the substrate's rate_limits API. §4 is independent
  and runs last.

## Section 1 — Verify statusline JSON during `claude --print`

**Goal.** Determine whether headless `claude --print
--dangerously-skip-permissions <prompt>` invocations trigger the
statusline hook, and if so, capture an example `rate_limits`
payload to confirm the schema matches the prior research's claimed
shape (`five_hour.used_percentage`, `five_hour.resets_at`,
`seven_day.used_percentage`, `seven_day.resets_at`).

**Steps.**

1. Author a probe statusline script at `tools/statusline_probe.sh`
   (new file). The script reads stdin (the statusline JSON
   payload Claude Code passes), writes the full payload to
   `/tmp/uas_statusline_probe.json` for inspection, and emits a
   trivial one-line statusline string on stdout (e.g., the model
   name) so Claude Code has something to display.
2. Configure Claude Code to use this script via the project-local
   `.claude/settings.local.json` (least-disruptive scope; does
   not affect global settings). Capture the prior settings so
   they can be restored at phase close. Confirm the
   configuration is loaded by running a trivial interactive
   `claude` invocation and inspecting the probe output once.
3. Run a trivial headless invocation:
   `claude --print --dangerously-skip-permissions "echo done"`.
4. Inspect `/tmp/uas_statusline_probe.json`:
   - Was the file written? If yes, headless `--print` mode does
     fire the statusline hook.
   - Does the payload include a `rate_limits` field? If yes,
     capture the exact schema and a sample.
   - Are `five_hour` / `seven_day` keys present and populated
     with both `used_percentage` and `resets_at`? Note any
     divergence from the prior research's claimed shape.
5. If the statusline hook does NOT fire under `--print`, try
   alternatives in order, capturing what each yields:
   a. `claude --output-format json --print "..."` — does the
      machine-readable response structure include rate_limits?
   b. A separate brief invocation immediately after the worker
      call (`claude --print "/usage" --output-format json` or
      similar) — does this surface rate_limits via the slash
      command output?
   c. Any other read surface that exists in the running Claude
      Code build (settings hooks, custom commands, environment
      injection at invocation time).
6. Document findings in §1's Results subsection (added at
   completion time): which read pattern works, an example
   `rate_limits` payload, schema confirmation, any deviations
   from the prior research's claimed shape.
7. Leave the probe configuration in place if §2 will run
   immediately; otherwise restore the prior
   `.claude/settings.local.json`.

**Acceptance.**

- A documented, working read pattern exists for extracting
  `rate_limits` (or its functional equivalent) from a headless
  Claude Code invocation.
- An example `rate_limits` payload is captured against a real
  account, with both `five_hour` and `seven_day` fields
  populated.
- The read pattern is described in §1 Results in enough detail
  that Phase 3 can consume it without re-discovering the
  mechanism.

**Status:** pending

## Section 2 — Verify percentage-cap behavior at limit

**Goal.** Determine whether `rate_limits.five_hour.used_percentage`
and `seven_day.used_percentage` cap at 100% when the buffer engages,
or climb past 100% to signal overflow magnitude. The answer affects
how Phase 3's quota state machine detects buffer entry, but does
not block Phase 3 design — the state machine can be written to
handle both behaviours.

**Steps.**

1. **Cost-vs-information decision (project owner).** This step
   requires either spending a known-but-small amount of
   paid-buffer to push past the 5-hour cap deliberately, or
   waiting for natural usage to test the boundary and capturing
   data opportunistically. The deliberate-push variant is faster
   and reproducible; the opportunistic variant is free but may
   not happen during Phase 2. The project owner decides before
   running steps 2–4. If "opportunistic" is chosen, mark this
   section's `Status:` as `deferred to natural-trigger`, add a
   note to `docs/substrate.md` documenting the open question and
   the dual-behaviour design constraint it places on Phase 3,
   and skip to §3.
2. **Deliberate-push variant.** Identify the cheapest workload
   that counts as ~1 message against the 5-hour quota (a trivial
   `--print "noop"` invocation is the working assumption).
   Estimate via §1's read pattern: how many trivial calls remain
   before the 5-hour cap, plus 5–10 calls past the cap to capture
   buffer-mode payloads. Estimate buffer spend incurred (trivial
   calls should be sub-cent each; total bound at $5 — if
   projection exceeds $5, abort and switch to opportunistic).
3. Run the planned probe sequence, capturing the statusline
   `rate_limits` payload at each call via §1's read pattern.
   Save each payload to `tools/probe_outputs/` (timestamped JSON
   files; directory deletes at phase close unless absorbed into
   substrate).
4. Once 3+ payloads have been captured in buffer mode (past the
   100% boundary), inspect:
   - Does `five_hour.used_percentage` cap at 100, or climb past?
   - Does `seven_day.used_percentage` shift during this test
     (might happen if weekly reset is also approaching)?
   - Does `resets_at` change behaviour past the cap (e.g., reset
     to `null`, advance, or stay fixed)?
5. Document findings in §2's Results subsection: cap behaviour,
   captured payloads (linked to `tools/probe_outputs/`), any
   surprises.
6. Record the actual buffer cost incurred during the probe in §2
   Results, for the project owner's records.

**Acceptance.**

- The cap behaviour is documented (caps-at-100 OR
  climbs-past-100) with at least 3 captured payloads as evidence
  — *or* the section is explicitly deferred per step 1, the
  deferral is documented in `docs/substrate.md`, and Phase 3's
  state-machine design is acknowledged as needing to handle both
  behaviours.
- If executed, total buffer spend stayed within the bound
  declared in step 2.

**Status:** pending

## Section 3 — Substrate-boundary catalog

**Goal.** Document the surface area of what Phase 1's deliverables
(the Phase 4 keep list) provide to the orchestrator, with explicit
interface boundaries: what each component accepts, what it emits,
and where the orchestrator extends it vs. consumes it as-is.

**Steps.**

1. For each item on the Phase 4 keep list (ROADMAP §Phase 4 —
   Prune §Keep list), survey the current code and document:
   - **Where it lives** (file paths, key functions/classes).
   - **What it accepts** (inputs, configuration knobs,
     environment variables).
   - **What it emits** (return values, files written, side
     effects, log events).
   - **Lifecycle** (when initialized, when torn down,
     persistence across invocations).
   - **Phase-3 consumption pattern** (how the orchestrator will
     use it: as a library function, as a subprocess, via
     configuration, etc.).
   - **Gaps** (where the orchestrator will need extension or
     wrapping rather than direct consumption).
2. Add a section to the catalog covering the Claude Code
   statusline `rate_limits` read pattern from §1, treating it as
   part of the substrate alongside the Phase 1 components. If §2
   ran and produced cap-behaviour findings, include those.
3. Write the catalog into `docs/substrate.md` (new file).
   Structure for readability by the next session: section per
   component, with a one-screen summary table at the top.
4. Confirm the doc is complete enough that the Phase 3 PLAN can
   be drafted against it without re-surveying the codebase.

**Acceptance.**

- `docs/substrate.md` exists and covers all 7 keep-list
  components from the ROADMAP, plus the rate_limits read pattern.
- Each component entry has the 6 fields enumerated in step 1
  (location, accepts, emits, lifecycle, Phase-3 consumption,
  gaps).
- A summary table at the top of the doc lists components with
  one-line descriptions.

**Status:** pending

## Section 4 — Cut-surface preview

**Goal.** Produce a sized estimate of what Phase 4 will delete, as
input to Phase 3's design (Phase 3 needs to know what's going away
so it doesn't accidentally depend on it). This is *not* the
per-mechanism verdict pass — that's Phase 4's job.

**Steps.**

1. Re-read the Phase 0 mechanism catalog. The headline-level
   distillation in ROADMAP §Current state of the codebase
   identifies the major modules and clusters; the row-by-row
   detail is recoverable from the Phase 0 audit's `PLAN.md`
   history if needed.
2. For each major module / mechanism cluster, bucket as:
   - **KEEP** (matches Phase 4's explicit keep list).
   - **CUT** (matches Phase 4's cut-surface estimate, default).
   - **NEEDS-PHASE-3-DECISION** (genuinely ambiguous; flag for
     resolution during orchestrator design).
3. For each bucket, count: approximate lines of code, files,
   distinct mechanisms.
4. Write findings to `docs/cut_surface.md` (new file). Headline
   numbers at the top, then bucket-by-bucket detail. Brief — this
   is a sized estimate, not Phase 4's per-mechanism verdict pass.
5. Cross-check totals: keep + cut + needs-decision should equal
   the Phase 0 catalog scale (68 mechanisms, ~13k lines across
   architect / orchestrator / uas trees).

**Acceptance.**

- `docs/cut_surface.md` exists.
- Sized estimate present for each bucket (lines, files,
  mechanisms).
- The total reconciles to the Phase 0 catalog scale.
- The doc is short — sized estimate, not verdict pass.

**Status:** pending
