# Substrate boundary catalog

This document catalogs the substrate the Phase 3 orchestrator
consumes: the Phase 1 deliverables (Phase 4's keep list) plus the
Claude Code v2.1.80+ rate-limit read surfaces from Phase 2 §1.

**Status:** stub. Phase 2 §3 will populate the per-component sections
against ROADMAP §Phase 4 — Prune §Keep list. The Open questions
section below was seeded by Phase 2 §2's deferral and should be
preserved when §3 expands the rest of the doc.

## Open questions

### `used_percentage` cap behaviour at the 5-hour / weekly limit

**Status:** unresolved. Phase 2 §2 deferred to natural-trigger
capture rather than running the deliberate-push probe. See PLAN.md
§Section 2 — Results for the rationale.

**The question.** When the 5-hour or weekly Claude Max window is
exhausted and paid buffer engages, does
`rate_limits.{five_hour,seven_day}.used_percentage` (read from the
TUI statusline JSON payload — see Phase 2 §1 Results, finding 2):

- **Cap at 100%** — the field clamps and buffer state must be
  detected via the stream-json `rate_limit_event`'s `overageStatus`
  / `isUsingOverage` (see §1 Results, finding 3) instead?
- **Climb past 100%** — the field is uncapped and the
  >100 threshold itself signals buffer entry, with magnitude
  readable directly from the percentage value?

**Why it's not blocking.** Headless `claude --print` workers — the
Phase 3 orchestrator's primary execution surface — do not see
`used_percentage` at all (Phase 2 §1, finding 1). They see the
ternary `rate_limit_event` only. `used_percentage` is reachable
only via the optional TUI-statusline companion read pattern, which
is one of three Phase 3 design options (ternary-only;
TUI-statusline companion; internal usage accumulator). If Phase 3
picks ternary-only or internal accumulator, this question is moot.

**Constraint on Phase 3 design.** If Phase 3 picks the
TUI-statusline companion option, its state machine must support
both interpretations until the wild observation arrives — i.e.,
treat any reading at or above 100 as "buffer engaged" without
attempting to read overflow magnitude from the percentage value
unless the climb-past-100 hypothesis is confirmed.

**Natural-trigger capture plan.** When the project owner
organically crosses the 5-hour cap during real-task use (Phase 5+),
capture one TUI statusline payload at that moment via
`tools/statusline_probe.sh` (the §1 probe; re-wire via the
`statusLine` block in `.claude/settings.local.json` per the JSON
snippet in PLAN.md §Section 1 — Results §Probe artefacts) and
append the finding here. No Phase 2 re-open required.
