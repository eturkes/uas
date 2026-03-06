# UAS Enhancement Plan: Maximizing Computational Power and Intelligence

This plan contains sequential sections to be implemented one at a time in dedicated Claude Code sessions. Each section is self-contained and builds on the previous ones.

---

## Section 1: Enhanced Task Decomposition with Structured Reasoning

**Goal:** Replace the basic decomposition prompt with a SOTA structured reasoning approach that produces higher-quality, more granular step DAGs. Inspired by MAKER's Maximal Agentic Decomposition (break tasks into the smallest reliable units), Pre-Act's rolling plan refinement, and ReAcTree's hierarchical subgoal trees.

### Changes

#### 1a. Rewrite `DECOMPOSITION_PROMPT` in `architect/planner.py`

The current prompt is minimal and produces coarse decompositions. Replace it with a structured prompt that uses XML tags, provides explicit reasoning instructions, includes few-shot examples, and enforces richer step metadata.

New prompt should:
- Use XML tags to separate `<instructions>`, `<rules>`, `<output_format>`, and `<examples>` sections (per Anthropic's official best practices: XML tags reduce misinterpretation when mixing instructions, context, and data)
- Add a **complexity assessment** phase: before decomposing, have the LLM assess complexity and choose an appropriate granularity level (1 step for trivial, 2-3 for simple, 4-7 for medium, 8+ for complex). Per MAKER: "the smaller the subtask, the more reliable the execution"
- Add **verification criteria** per step: each step should include a `verify` field describing how to check if the step succeeded (beyond just exit code 0)
- Add **resource hints** per step: each step should include an `environment` field listing expected pip/apt packages needed
- Add a **reasoning preamble**: ask the LLM to first reason about the goal in `<analysis>` tags before producing the JSON, implementing a chain-of-thought pattern. Per Anthropic: "Prefer general instructions ('think thoroughly') over prescriptive steps — Claude's reasoning frequently exceeds what a human would prescribe"
- Include 2-3 diverse few-shot examples (single-step trivial task, multi-step with dependencies, complex pipeline with parallel-ready steps). Per Anthropic: "3-5 examples dramatically improve accuracy and consistency"
- Instruct the LLM to maximize parallelism in the DAG by making steps independent whenever possible

#### 1b. Add plan validation and self-critique in `architect/planner.py`

After the initial decomposition, add a **plan critique** step (self-correction pattern: generate draft -> review against criteria -> refine). Send the decomposition back to the LLM with a critique prompt asking it to identify potential issues (missing steps, wrong dependencies, steps that are too broad, missing error handling for external resources). If the critique identifies issues, apply a single refinement pass.

Add a new function `critique_and_refine_plan(goal, steps)` that:
- Sends the goal + proposed steps to the LLM with a critique prompt
- Parses any suggested modifications
- Returns the refined steps (or original if no issues found)
- This is a single pass, not iterative, to avoid over-engineering

#### 1c. Update step schema in `architect/state.py`

Add optional fields to the step schema: `verify` (str), `environment` (list[str]). These will be populated during decomposition and used downstream.

### Files to modify
- `architect/planner.py` — rewrite `DECOMPOSITION_PROMPT`, add `critique_and_refine_plan()`
- `architect/state.py` — extend step schema in `add_steps()`
- `architect/main.py` — call critique step after `decompose_goal()` if more than 1 step
- `tests/test_planner.py` — update tests for new prompt structure and critique function

---

## Section 2: Enriched Context Propagation and Working Memory

**Goal:** Transform the primitive string-based context passing between steps into a structured workspace memory system that gives downstream steps rich, actionable context. Inspired by LangChain's context engineering patterns (typed state objects with selective exposure), JetBrains research on observation masking, and Factory.ai's incremental summary approach.

### Changes

#### 2a. Implement structured context builder in `architect/main.py`

Replace the simple `build_context()` function with a richer context system that:
- Reads actual workspace files written by dependency steps (not just stdout)
- Produces structured context using XML tags: `<previous_step_output>`, `<workspace_files>`, `<step_summary>` (per Anthropic: XML tags are the recommended way to structure complex multi-section context)
- For files: includes file names, sizes, and first N lines of content for small text files
- Applies intelligent truncation: prioritize structured data (JSON, CSV headers) over raw text
- Includes the `verify` field from completed dependency steps so the current step knows what was validated
- Uses **observation masking** for old step outputs: replace older outputs with placeholders like `[Step N output omitted - produced files: X, Y]` while keeping recent outputs in full (JetBrains finding: 52% cost reduction, equal or better quality vs. LLM summarization)

#### 2b. Add workspace file scanning in `architect/executor.py`

Add a function `scan_workspace_files(workspace_path)` that:
- Lists all files in the workspace directory (non-recursive for safety)
- Returns a dict of `{filename: {size, type, preview}}` where preview is first 500 chars for text files
- This gives steps actual visibility into what prior steps produced on disk, not just what they printed

#### 2c. Enhance context length management

Replace the simple char-based truncation with a smarter approach:
- Use a priority system: verification results > structured data (JSON/CSV) > stdout > stderr
- For JSON files, include the schema/keys rather than raw content when truncating
- Increase `UAS_MAX_CONTEXT_LENGTH` default from 4000 to 8000 chars
- Add a `summarize_context()` function that, when context exceeds the limit, sends it to the LLM for compression instead of blind truncation
- Always preserve across compressions: original goal, current plan state, file paths touched, error messages encountered (per Factory.ai best practices)

### Files to modify
- `architect/main.py` — rewrite `build_context()`
- `architect/executor.py` — add `scan_workspace_files()`, update context assembly
- `architect/state.py` — no changes needed (uses existing fields)
- `tests/test_build_context.py` — update/add tests

---

## Section 3: Advanced Code Generation Prompts with Self-Verification

**Goal:** Upgrade the orchestrator's code generation prompt to produce more robust, self-verifying Python scripts using SOTA prompt engineering techniques.

### Changes

#### 3a. Rewrite `build_prompt()` in `orchestrator/main.py`

Replace the current prompt with a structured, XML-tagged prompt that:
- Uses a clear role definition: `You are an expert Python engineer generating production-quality scripts.`
- Structures sections with XML tags: `<role>`, `<environment>`, `<task>`, `<constraints>`, `<verification>`, `<previous_error>`
- Adds a **self-verification requirement**: the generated script must include a verification section at the end that checks its own output (e.g., verifying files exist, checking output format, validating data integrity)
- Adds **environment setup instructions**: if the step has `environment` hints from decomposition, include explicit `pip install` / `apt-get` instructions at the top of the script
- Adds a **structured output requirement**: scripts should print a machine-readable summary line at the end in the format `UAS_RESULT: {"status": "ok", "files_written": [...], "summary": "..."}`
- Includes guidance on common failure modes: "Wrap network requests in retries with exponential backoff", "Always use `os.path.join(workspace, ...)` for file paths", "Check if files exist before reading them"
- For retry attempts, include a **root cause analysis** instruction: "Before writing the fix, analyze the error in `<analysis>` tags to identify the root cause, then write the corrected script"

#### 3b. Add result parsing in `orchestrator/main.py`

After successful sandbox execution, parse the `UAS_RESULT: {...}` line from stdout if present. Pass this structured result back through the orchestrator's return value so the architect can use it for richer context propagation.

Add a function `parse_uas_result(stdout)` that extracts the JSON from the structured output line.

#### 3c. Update error feedback prompt

When retrying, instead of just appending the raw error, structure the feedback:
- Include the full script that failed (not just the error)
- Add explicit instructions: "Do NOT repeat the same approach. Identify what went wrong and use a fundamentally different strategy if the same approach failed twice."
- On the 3rd attempt, add: "This is the final attempt. Be maximally defensive: add try/except around every external call, validate all inputs, and include detailed error messages."

### Files to modify
- `orchestrator/main.py` — rewrite `build_prompt()`, add `parse_uas_result()`, update retry logic
- `orchestrator/parser.py` — no changes (code extraction unchanged)
- `architect/executor.py` — parse structured results from orchestrator output
- `tests/test_orchestrator_main.py` — update prompt tests

---

## Section 4: Intelligent Error Recovery with Reflection

**Goal:** Replace the simple spec rewrite mechanism with a reflection-based error recovery system inspired by ReVeal (iterative generation-verification), Reflexion (linguistic self-critique), CHIEF (hierarchical failure attribution), and MAKER (red-flagging confused outputs).

### Changes

#### 4a. Implement reflection-based rewrite in `architect/planner.py`

Replace `rewrite_task()` with a `reflect_and_rewrite()` function that uses a structured reflection prompt:
- **Diagnosis phase**: Ask the LLM to analyze the failure in `<diagnosis>` tags — what specifically went wrong, why, and what category of error it is (dependency issue, logic error, environment problem, network issue, data format mismatch). Per CHIEF: first determine whether the error originated in this step or propagated from a previous step
- **Strategy phase**: Ask the LLM to propose 2-3 alternative strategies in `<strategies>` tags and select the best one with justification
- **Rewrite phase**: Generate the improved task description based on the selected strategy
- Include the original goal and all previous step outputs as context, not just the failing step's info
- **Red-flagging** (from MAKER): automatically discard LLM outputs that show structural signs of confusion (excessive length relative to expected output, wrong format, repeating prior errors verbatim) and resample

#### 4b. Add progressive error escalation

Instead of treating all failures equally, implement escalating recovery strategies (tiered recovery inspired by research synthesis):
1. **First failure**: Reflection-based rewrite with error context and root cause analysis
2. **Second failure**: Alternative approach — explicitly instruct: "The previous approach failed. Propose a fundamentally different strategy."
3. **Third failure**: Decompose the failing step into 2-3 sub-steps (break down further, per MAKER's MAD principle)
4. **Final failure**: Generate a minimal diagnostic script that prints environment info, checks dependencies, and tests assumptions — then use that output to inform one last rewrite

Add this logic to `execute_step()` in `architect/main.py`.

#### 4c. Increase max retries intelligently

Change `MAX_SPEC_REWRITES` from 2 to 4, with each rewrite using the progressive escalation from 4b. The total attempt budget becomes: 3 orchestrator attempts * 5 architect rewrites = 15 total attempts per step before declaring blocker, significantly increasing the chance of recovery. Per LLMLOOP research: up to 5 self-debugging attempts are effective, with diminishing returns beyond that.

### Files to modify
- `architect/planner.py` — replace `rewrite_task()` with `reflect_and_rewrite()`, add `decompose_failing_step()`
- `architect/main.py` — update `execute_step()` with progressive escalation logic
- `tests/test_planner.py` — add tests for reflection and decomposition

---

## Section 5: Workspace Scratchpad and Inter-Session Memory

**Goal:** Implement a file-based scratchpad/memory system that persists insights across steps, enabling the agent to build cumulative knowledge during a run.

### Changes

#### 5a. Implement scratchpad in `architect/state.py`

Add a `scratchpad.md` file in the `.state/` directory that accumulates learnings:
- After each step completes: append a summary of what was done, what worked, what files were created
- After each failure: append what went wrong and what was tried
- This file is included (truncated) in the context for all subsequent steps

Add functions:
- `append_scratchpad(entry: str)` — append a timestamped entry
- `read_scratchpad(max_chars: int = 2000) -> str` — read the most recent entries up to char limit

#### 5b. Integrate scratchpad into context pipeline

In `architect/main.py`:
- After each step completes or fails, write a scratchpad entry summarizing the result
- In `build_context()`, always include the scratchpad content as a `<scratchpad>` section, giving all steps visibility into the run's history
- Use tail-based reading (most recent entries first) to prioritize recent context

#### 5c. Add environment discovery scratchpad

On the first step's execution, prepend a lightweight environment probe:
- Python version, available packages, disk space, network connectivity
- Write results to scratchpad so all subsequent steps know the environment
- This prevents repeated failures from wrong assumptions about the environment

### Files to modify
- `architect/state.py` — add `append_scratchpad()`, `read_scratchpad()`
- `architect/main.py` — integrate scratchpad into step execution and context building
- `architect/executor.py` — no changes
- `tests/test_state.py` — add scratchpad tests

---

## Section 6: CLAUDE.md Integration for Claude Code CLI

**Goal:** Add a `.claude/CLAUDE.md` file that is automatically placed in the workspace to guide the Claude Code CLI instances used by the orchestrator, maximizing code generation quality.

### Changes

#### 6a. Create CLAUDE.md template

Create a CLAUDE.md template string in `orchestrator/llm_client.py` (or a new `orchestrator/claude_config.py`) that will be written to the workspace before orchestrator execution. This file should contain:
- Role definition for the code generation context
- Coding standards: always use `os.environ.get('WORKSPACE', '/workspace')`, always print results, always handle errors with informative messages
- Environment context: Python 3.12, full network access, root permissions
- Instruction to produce self-contained scripts with all imports
- Instruction to use `subprocess.run` for package installation rather than assuming packages exist
- Instruction to include a `UAS_RESULT` summary line at the end of stdout

#### 6b. Write CLAUDE.md to workspace before execution

In `architect/executor.py`, before calling the orchestrator:
- Write `.claude/CLAUDE.md` to the workspace directory (creating `.claude/` if needed)
- This ensures every Claude Code CLI invocation sees these persistent instructions
- Only write if the file doesn't already exist or needs updating

### Files to modify
- `orchestrator/llm_client.py` or new `orchestrator/claude_config.py` — CLAUDE.md template
- `architect/executor.py` — write CLAUDE.md before orchestrator runs

---

## Section 7: Parallel Execution Optimization and Step Merging

**Goal:** Improve the parallel execution system to maximize throughput and add intelligent step merging for trivially combinable steps.

### Changes

#### 7a. Add step merging in `architect/planner.py`

Add a `merge_trivial_steps(steps)` function that combines steps that:
- Are in the same execution level (no dependency relationship)
- Are both simple enough to combine (heuristic: description < 200 chars each, no complex dependencies)
- Can be expressed as a single script (e.g., "create file A" + "create file B" → "create files A and B")

This reduces LLM calls and sandbox invocations for simple goals. Apply this as an optional post-processing step after decomposition.

#### 7b. Add resource-aware parallel execution limits

In `architect/main.py`, add a `UAS_MAX_PARALLEL` environment variable (default: 4) to cap the number of concurrent orchestrator invocations. This prevents resource exhaustion when the DAG has many independent steps.

#### 7c. Add step-level timing and performance tracking

Enhance the state to track per-step metrics:
- LLM call time vs sandbox execution time (helps identify bottlenecks)
- Number of LLM tokens used (if available from Claude CLI output)
- Write these to the JSON output for analysis

### Files to modify
- `architect/planner.py` — add `merge_trivial_steps()`
- `architect/main.py` — add `UAS_MAX_PARALLEL`, enhance timing tracking
- `architect/state.py` — add timing fields
- `tests/test_parallel.py` — add merge and throttle tests

---

## Section 8: Verification Loop and Success Validation

**Goal:** Add a post-execution verification phase that validates step outputs against their success criteria, enabling the agent to catch subtle failures that exit code 0 misses.

### Changes

#### 8a. Add verification executor in `architect/main.py`

After a step succeeds (exit code 0), if the step has a `verify` field:
- Generate a small verification script based on the `verify` criteria
- Run it in the sandbox
- If verification fails, treat it as a step failure and enter the rewrite loop
- This catches cases where the script runs fine but produces wrong output

Add a function `verify_step_output(step, workspace)` that:
- Takes the step's `verify` field and workspace path
- Generates a Python verification script
- Runs it and returns pass/fail

#### 8b. Add workspace state validation

After all steps complete, run a final validation pass:
- Check that all files mentioned in step outputs actually exist
- Verify the workspace isn't empty (sanity check)
- Write a `VALIDATION.md` to the workspace summarizing what was produced

#### 8c. Integrate UAS_RESULT parsing

If a step produced a `UAS_RESULT` JSON line, parse it and validate:
- Check `status` field is "ok"
- Verify `files_written` files actually exist
- Use `summary` for richer context propagation

### Files to modify
- `architect/main.py` — add `verify_step_output()`, final validation
- `orchestrator/main.py` — ensure UAS_RESULT parsing is in place (from Section 3)
- `architect/executor.py` — pass verification results upstream

---

## Implementation Notes

- Each section should be implemented in order, as later sections may depend on changes from earlier ones
- After implementing each section, run `python3 -m pytest tests/` to verify existing tests still pass
- Add new tests for each new function/behavior
- Keep the codebase lean — this is a research tool, not a production system
- Do NOT commit or push to Git — changes will be reviewed manually first

## Research Sources

### Anthropic / Claude Official
- [Anthropic Prompting Best Practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices)
- [Claude Extended Thinking Tips](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/extended-thinking-tips)
- [Claude Think Tool](https://www.anthropic.com/engineering/claude-think-tool)
- [Claude Code Best Practices](https://code.claude.com/docs/en/best-practices)
- [Building Agents with Claude Agent SDK](https://www.anthropic.com/engineering/building-agents-with-the-claude-agent-sdk)

### Task Decomposition & Planning
- [MAKER: Solving a Million-Step LLM Task with Zero Errors](https://arxiv.org/abs/2511.09030) — Maximal Agentic Decomposition, multi-agent voting, red-flagging
- [Pre-Act: Multi-Step Planning and Reasoning](https://arxiv.org/abs/2505.09970) — Rolling plan refinement, 82% goal completion
- [ReAcTree: Hierarchical LLM Agent Trees](https://arxiv.org/abs/2511.02424) — Subgoal trees with dual memory
- [GoalAct: Global Planning and Hierarchical Execution](https://arxiv.org/abs/2504.16563)
- [Recursive Language Models](https://arxiv.org/html/2512.24601v1)
- [AgentOrchestra: Hierarchical Multi-Agent Framework](https://arxiv.org/html/2506.12508v1)
- [LATS: Language Agent Tree Search](https://arxiv.org/abs/2310.04406) — Backtracking via MCTS

### Context & Memory Management
- [Context Engineering for Agents (LangChain)](https://blog.langchain.com/context-engineering-for-agents/)
- [JetBrains: Efficient Context Management](https://blog.jetbrains.com/research/2025/12/efficient-context-management/) — Observation masking
- [Factory.ai: Compressing Context](https://factory.ai/news/compressing-context) — Incremental summaries
- [A-MEM: Agentic Memory for LLM Agents](https://arxiv.org/abs/2502.12110) — Zettelkasten-inspired
- [Mem0: Production-Ready Long-Term Memory](https://arxiv.org/abs/2504.19413)
- [ACON: Context Compression for Long-horizon Agents](https://arxiv.org/abs/2510.00615)
- [ReSum: Context Summarization for Web Agents](https://openreview.net/forum?id=PjIK38mwKm)

### Self-Correction & Error Recovery
- [ReVeal: Self-Evolving Code Agents](https://arxiv.org/html/2506.11442v1) — Generation-verification loop
- [Multi-Agent Reflexion (MAR)](https://arxiv.org/html/2512.20845)
- [CHIEF: Hierarchical Failure Attribution](https://arxiv.org/html/2602.23701) — Counterfactual root cause analysis
- [Where LLM Agents Fail](https://arxiv.org/abs/2509.25370) — Error taxonomy & cascade analysis
- [LLMLOOP: Iterative Code and Test Refinement (ICSME 2025)](https://valerio-terragni.github.io/assets/pdf/ravi-icsme-2025.pdf) — Up to 5 self-debugging attempts effective
- [Amazon Science: Self-Debugging Code Generation](https://www.amazon.science/blog/training-code-generation-models-to-debug-their-own-outputs)
- [SICA: Self-Improving Coding Agent (ICLR 2025)](https://openreview.net/pdf?id=rShJCyLsOr)

### General Agent Architecture
- [Long-Running AI Agents and Task Decomposition](https://zylos.ai/research/2026-01-16-long-running-ai-agents)
- [LLM Code Generation Survey](https://arxiv.org/html/2508.00083v1)
<!-- DONE: Section 1 -->
