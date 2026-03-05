# Architect Agent Design

## Overview

The Architect Agent is a high-level autonomous planner that sits above the existing Execution Orchestrator. It takes abstract, natural-language human goals and drives them to completion by decomposing them into atomic sub-tasks, generating UAS-compliant specs, and feeding them sequentially to the Orchestrator.

```
User
 │
 ▼
run_framework.sh
 │
 ▼
Architect Agent (host-side Python)
 ├── Planner      ──► LLM decomposes goal into steps
 ├── Spec Generator ──► writes UAS markdown specs
 ├── State Manager  ──► tracks plan_state.json
 └── Executor      ──► invokes Orchestrator container
      │
      ▼
Orchestrator Container (Podman-in-Podman)
 ├── LLM Client   ──► generates Python code
 ├── Sandbox       ──► executes code in nested container
 └── Retry loop    ──► 3 attempts per task
```

## Planning & Decomposition

The Planner module uses an LLM to break a high-level goal into a flat list of atomic steps. Each step is described in enough detail for the Orchestrator's code-generation LLM to produce a self-contained Python script.

**Key constraint:** The Orchestrator's sandbox is ephemeral (each run starts fresh with no persistent storage). The Architect handles this by capturing `stdout` from completed steps and injecting it as inline context into dependent steps' task descriptions.

### Decomposition Prompt

The LLM receives a structured prompt that enforces:
- Steps must be self-contained
- No assumption of shared state between steps
- Output as a JSON array with `title`, `description`, `depends_on` fields

### Context Propagation

When step N depends on step M, the Architect:
1. Captures the sandbox stdout from step M's successful run
2. Includes that output as literal context in step N's task description
3. The Orchestrator's LLM then has the data it needs to generate correct code

## State Management

All state is persisted to `architect_state/plan_state.json`:

```json
{
  "goal": "human's original goal",
  "created_at": "ISO timestamp",
  "status": "planning|executing|completed|failed|blocked",
  "steps": [
    {
      "id": 1,
      "title": "step name",
      "description": "task description",
      "depends_on": [],
      "status": "pending|executing|completed|failed|blocked",
      "spec_file": "architect_state/specs/step_001.md",
      "rewrites": 0,
      "output": "captured stdout",
      "error": "last error message"
    }
  ]
}
```

The state file is updated after every significant event (step start, completion, failure, rewrite).

## UAS Spec Format

Each step produces a markdown file in `architect_state/specs/`:

```markdown
# UAS Spec: [Step Title]

## Metadata
- **Step:** N of Total
- **Status:** pending
- **Depends On:** [list]

## Objective
[What this step accomplishes]

## Context
[Output from dependency steps, if any]

## Task
[The task description passed to the Orchestrator]

## Acceptance Criteria
- The generated Python script exits with code 0.
- The script's stdout contains the expected output.
```

## Orchestrator Interface

The Architect invokes the Orchestrator as a container subprocess:

```
podman run --rm --privileged \
  -e UAS_TASK="<task from spec>" \
  -e ANTHROPIC_API_KEY="..." \
  uas-orchestrator
```

This mirrors `start_orchestrator.sh` but omits `-it` for non-interactive subprocess capture. The Architect reads:
- **Exit code:** 0 = success, non-zero = failure
- **stdout:** Contains the Orchestrator's full log including sandbox output
- **stderr:** Container-level errors

## Self-Correction Loop

```
For each step:
  attempt 0: original spec
  │
  ├─ Orchestrator succeeds → mark complete, continue
  │
  └─ Orchestrator fails (all 3 internal retries exhausted)
     │
     ├─ rewrites < 2 → LLM analyzes failure, rewrites task, retry
     │
     └─ rewrites == 2 → create ARCHITECT_BLOCKER.md, halt
```

The rewrite prompt sends the LLM:
- The original task description
- The Orchestrator's stdout/stderr (truncated to prevent context overflow)

The LLM produces an improved task description that addresses the specific failure.

## Halt Conditions (ARCHITECT_BLOCKER.md)

The Architect creates `ARCHITECT_BLOCKER.md` and halts when:
1. A step fails after the Orchestrator's 3 retries + 2 Architect spec rewrites
2. No container engine is available
3. The LLM fails to decompose the goal

The blocker file contains the goal, failing step, last error, and required human action.

## File Structure

```
.
├── architect/
│   ├── __init__.py
│   ├── main.py              # Controller loop (entry point)
│   ├── planner.py           # LLM task decomposition + rewrite
│   ├── spec_generator.py    # UAS markdown spec writer
│   ├── executor.py          # Orchestrator subprocess interface
│   └── state.py             # JSON state persistence
├── architect_state/          # Runtime artifacts (gitignored)
│   ├── plan_state.json       # Current plan state
│   └── specs/                # Generated UAS spec files
├── architect_design.md       # This document
├── run_framework.sh          # User entry point
├── orchestrator/             # Existing Orchestrator (unchanged)
└── start_orchestrator.sh     # Existing Orchestrator launcher
```

## Context Window Management

To prevent context collapse on large projects:
- The Planner receives only the goal (no accumulated history)
- The Spec Generator receives only the current step + relevant dependency outputs
- The Rewrite prompt receives only the failing step + truncated error logs (stdout capped at 2000 chars, stderr at 1000 chars)
- The plan state is persisted to disk, not accumulated in LLM context
