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

**Status:** completed

### Section 1 — Results

**Headline finding.** The statusline hook does **not** fire under
headless `claude --print --dangerously-skip-permissions <prompt>`.
A working alternative exists: `claude --print --output-format
stream-json --verbose ...` emits a `rate_limit_event` line in the
stream. Its schema is simpler than the statusline's `rate_limits`
field (status enum + overage state, no `used_percentage`), so it
covers the orchestrator's free / paid / hard-stop transitions but
not percentage-progress. The TUI statusline JSON's `rate_limits`
field is confirmed against the prior research's claimed shape
(both `five_hour` and `seven_day` populated, with
`used_percentage` and `resets_at`).

**Test environment.** Claude Code 2.1.123 (well above the v2.1.80
threshold from the ROADMAP), Opus 4.7 default, project
`/home/eturkes/pro/uas`, real Claude Max account. Probe script
`tools/statusline_probe.sh` wired up via project-local
`.claude/settings.local.json` `statusLine` block, parent-session
PID 72018 used to disambiguate writes from headless subprocesses.

**Findings.**

1. **Statusline hook does not fire under `--print`.** Cleared the
   probe directory, ran `claude --print --dangerously-skip-permissions
   "Reply with the literal word: done"` (which returned `done`,
   exit 0), then inspected `/tmp/uas_statusline_probes/`. Seven
   probe files were written during the run window, **all** with
   `ppid=72018` (the parent interactive session), **none** from
   the `--print` subprocess. A separate `--debug-file` capture of
   a `--print` startup confirms `Registered 0 hooks from 0
   plugins` and `Hooks: Found 0 total hooks in registry`, with no
   statusline reference anywhere in the headless startup log
   (settings file is loaded — `Watching for changes in setting
   files .../settings.local.json` — but the statusLine entry is
   not exercised).

2. **TUI statusline JSON matches the prior research's
   `rate_limits` schema.** Captured from the parent session
   (saved to
   `/tmp/uas_statusline_probe_interactive_baseline.json` during
   the test):

   ```json
   "rate_limits": {
     "five_hour": {
       "used_percentage": 6,
       "resets_at": 1777660800
     },
     "seven_day": {
       "used_percentage": 37,
       "resets_at": 1777885200
     }
   }
   ```

   Both fields populated. `used_percentage` is an integer (not a
   float) in this sample. `resets_at` is a Unix timestamp.
   Surrounding JSON also exposes `cost.total_cost_usd`,
   `context_window.used_percentage`, `model.id`,
   `model.display_name`, `version` (Claude Code), `effort.level`,
   `thinking.enabled`, `fast_mode`, etc. — all useful context
   for the Phase 3 budget ledger.

3. **Working alternative for headless: `rate_limit_event` in
   stream-json.** Running `claude --print --output-format
   stream-json --verbose --dangerously-skip-permissions "..."`
   emits this line in the stream:

   ```json
   {
     "type": "rate_limit_event",
     "rate_limit_info": {
       "status": "allowed",
       "resetsAt": 1777660800,
       "rateLimitType": "five_hour",
       "overageStatus": "allowed",
       "overageResetsAt": 1780272000,
       "isUsingOverage": false
     },
     "uuid": "b608a807-f752-4415-91b3-b82b1d8743ea",
     "session_id": "511b6f13-7c0e-4d1a-934d-e06568950cb1"
   }
   ```

   The minimum flag set is `--print --output-format stream-json
   --verbose` — `--include-hook-events` is **not** required (the
   event is built-in, not a hook lifecycle event). Schema
   differences from the statusline payload, in detail:

   - **No `used_percentage`.** Only ternary `status` (observed
     value `"allowed"`) plus paid-buffer fields
     (`overageStatus`, `isUsingOverage`).
   - **`resetsAt` (camelCase)** vs statusline's `resets_at`
     (snake_case). Same Unix-timestamp semantics.
   - **One `rateLimitType` per event.** This run emitted only
     `five_hour`; whether `seven_day` events also fire is not
     observed in §1 (out of scope — left to §2 and Phase 3
     iteration).
   - **`overageStatus` + `overageResetsAt` + `isUsingOverage`**
     are present here and absent from the statusline schema —
     directly answering "is paid buffer engaged?", which the
     statusline payload requires inferring from
     `used_percentage`.

4. **Negative results worth recording (so they're not re-tried in
   Phase 3).**

   - `claude --print --output-format json "..."` (single-result
     JSON) does **not** include rate-limit info. The full
     payload covers `total_cost_usd`, per-iteration
     `usage.input_tokens` / `cache_read_input_tokens` / etc.,
     `modelUsage` per model, `permission_denials`,
     `terminal_reason`, but no rate-limit field.
   - `claude --print "/usage"` returns the canned string `"You
     are currently using your subscription to power your Claude
     Code usage"`. No actionable data.
   - `claude --print "/extra-usage"` returns `"Please visit
     https://claude.ai/settings/usage to manage extra usage."`.
     No actionable data.

5. **Read pattern recommended for Phase 3.**

   - **Default surface:** parse `rate_limit_event` from each
     worker's own stream-json output. No extra calls, no
     statusline configuration required, lives inside the
     existing call. Provides ternary status + paid-buffer
     state — sufficient for the free / paid / hard-stop
     transitions in the policy machine.
   - **Substrate gap:** `used_percentage` (percentage-progress
     within each window) is reachable only via TUI statusline.
     The orchestrator may need to (a) accept ternary status as
     sufficient for soft-cap policies, (b) accumulate per-call
     usage against an inferred cap, or (c) periodically spawn a
     short-lived TUI session as a percentage-read companion.
     Decision deferred to Phase 3 design; this gap is logged in
     `docs/substrate.md` (§3 of this PLAN).

**Probe artefacts.**

- `tools/statusline_probe.sh` — committed; lives until phase
  close per PLAN scope-discipline note.
- `.claude/settings.local.json` `statusLine` entry — added then
  **restored** at end of §1 (gitignored anyway). The original
  contained only the `permissions` block; §2 will re-add the
  `statusLine` entry if needed. The block to re-add is:

  ```json
  "statusLine": {
    "type": "command",
    "command": "$CLAUDE_PROJECT_DIR/tools/statusline_probe.sh"
  }
  ```

- Captured payloads embedded inline above (findings 2 and 3); no
  on-disk artefacts retained outside `/tmp` (which is
  expendable). Phase 3's substrate doc (§3) will reference the
  inline payloads here as canonical.

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

**Status:** deferred to natural-trigger

### Section 2 — Results

**Decision (project owner, this session).** Variant B (opportunistic
/ deferred). No deliberate buffer push performed during Phase 2.

**Reasoning recorded.** Three factors made deferral the cheaper
choice without losing meaningful information for Phase 3:

1. **`used_percentage` isn't on the headless surface anyway.** §1
   established that headless `claude --print` workers see only the
   ternary `rate_limit_event` (`status` ∈ allowed / paid-buffer /
   hard-stop, plus `overageStatus` / `isUsingOverage`). They do not
   see `used_percentage`. Cap-vs-overflow behaviour matters only
   for the *companion* TUI-statusline read pattern, which the §1
   Results listed as one of three Phase-3 design options (the
   others being "ternary-only is sufficient" and "internal usage
   accumulator"). It is not on the orchestrator's critical path.
2. **The deliberate-push experiment is heavier than the PLAN text
   implied.** Account state at §1 baseline was
   `five_hour.used_percentage: 6`. Pushing a fresh 5-hour window
   from 6% to ~100% via trivial `--print` calls is hours of
   pinging, not a 10-call probe. The PLAN's $5 spend bound would
   gate it, but the wallclock and message-count cost of actually
   reaching the cap is non-trivial — and the value of doing so in
   this phase is bounded by point 1.
3. **§3 and §4 do not depend on §2's verdict.** Phase 2's exit
   criteria allow §2 to be explicitly deferred provided the gap is
   logged in `docs/substrate.md` and Phase 3's state-machine design
   acknowledges the dual-behaviour constraint. Both are now done
   (this Results note + the stub written to `docs/substrate.md`
   below).

**Constraint placed on Phase 3.** If Phase 3 chooses the
TUI-statusline companion read pattern, its quota state machine must
treat `used_percentage` as either-cap-or-climb until the cap
behaviour is observed in the wild. Two interpretations of a 100+%
reading must be supported until evidence narrows it:

- **Caps-at-100 hypothesis:** the field clamps; buffer engagement
  must be detected via `overageStatus` / `isUsingOverage` from the
  stream-json `rate_limit_event` instead, and percentage cannot be
  used to estimate overflow magnitude.
- **Climbs-past-100 hypothesis:** the field is uncapped; buffer
  magnitude can be read directly from the percentage value, and
  the >100 threshold itself signals buffer entry.

If Phase 3 instead picks ternary-only or the internal accumulator
options from §1's Results, this constraint is moot.

**Buffer spend incurred during §2:** $0.00 (variant B; no probe
calls run).

**Natural-trigger capture plan.** When the project owner
organically pushes the 5-hour window past 100% during real-task use
(Phase 5 or later), capture one TUI statusline payload at that
moment via the §1 probe (`tools/statusline_probe.sh` already wired,
or re-wire from the §1 Results JSON snippet) and append the
finding to `docs/substrate.md` § Open questions. No Phase 2
re-open required.

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
