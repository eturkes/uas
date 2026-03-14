# PLAN.md - Implicit Intelligence Improvements for UAS

> **Core Principle:** Instruct the LLM to do things autonomously — don't replace its
> judgment with hardcoded decision trees. The LLM is the agent; our job is to give it
> the right instructions, context, and capabilities so it naturally does what a
> sophisticated user would have asked for explicitly.
>
> Each section below is a self-contained unit of work to be executed sequentially.
> When a section is complete, replace `[ ]` with `[x]` on its heading line.
> A coding agent should read this file, find the first unchecked section, implement
> it, mark it done, and stop.

---

## [x] Section 1: Autonomy Awareness — Make the LLM Know What It Can Do

**Goal:** Agents should inherently understand they have full autonomy — network,
root, packages, commands, web search — and be instructed to use these capabilities
proactively. Currently, capability descriptions are buried and passive. The LLM
should be told plainly: you can do anything, and you should.

**Files to modify:**
- `orchestrator/main.py` — Rewrite `<environment>` and `<role>` sections in `build_prompt()`
- `orchestrator/claude_config.py` — Rewrite `CLAUDE_MD_TEMPLATE`

**Implementation:**

1. In `build_prompt()`, replace the `<environment>` section with:
   ```xml
   <environment>
   You are running inside an isolated, disposable container. You have FULL AUTONOMY:
   - ROOT ACCESS. Install any system packages with apt-get. No sudo needed.
   - UNRESTRICTED NETWORK. Fetch any URL, call any API, clone any repo. No firewall, no proxy.
   - PACKAGE INSTALLATION. pip install anything you need. Do it proactively at the top of your script.
   - COMMAND EXECUTION. Run any shell command via subprocess. No restrictions whatsoever.
   - WEB SEARCH. If you need to look something up — current library versions, API docs, best practices — you can and should use the network.
   - FILESYSTEM. Full read/write. Workspace: os.environ.get("WORKSPACE", "/workspace").

   This container is disposable. Nothing here affects the host. Be bold, not cautious.
   {env_setup}
   </environment>
   ```

2. Replace the `<role>` section with:
   ```xml
   <role>
   You are an expert engineer with full system access in a disposable container.
   Generate a complete, self-contained Python script.

   Before writing code, think about what you need:
   - What packages does this task require? Install them.
   - Are there tools that would improve quality (linters, formatters, test runners)?
     Install and use them if it would meaningfully improve the result.
   - Is there information you're uncertain about (API formats, library versions,
     current best practices)? Use the network to check.

   Act like a senior engineer who sets up their own environment before starting work.

   CRITICAL OUTPUT FORMAT: Your response must contain exactly ONE fenced code block
   tagged as ```python ... ```. The script must be complete and self-contained.
   Do NOT use any XML tags, tool_call blocks, or analysis sections.
   </role>
   ```

3. In `CLAUDE_MD_TEMPLATE` in `claude_config.py`, add a new `## Autonomy` section
   at the very top (before `## Role`):
   ```
   ## Autonomy
   You are in an isolated, disposable container with full autonomy:
   - Root access, unrestricted network, no firewalls, no permission boundaries
   - Install any packages, run any commands, access any URLs, clone any repos
   - If you need a tool, install it. If you need information, fetch it.
   - If you're unsure about a library version or API, check PyPI or the docs.
   Act decisively. Never hedge about permissions or capabilities.
   ```

4. In the existing `## Environment` section of `CLAUDE_MD_TEMPLATE`, change
   "Packages are NOT pre-installed beyond the standard library -- install what you need"
   to "No packages are pre-installed. Proactively install everything you need, including
   dev tools (linters, formatters, test runners) when they'd improve quality."

**Acceptance criteria:**
- Prompt and CLAUDE.md both convey full autonomy assertively
- LLM is explicitly told it can use the network to look things up
- LLM is explicitly told to install tools proactively, not just task-specific packages
- No hedging language ("if available", "when possible")
- The word "disposable" or "isolated" appears to encourage bold action

---

## [x] Section 2: Implicit Research — Tell the LLM to Research Before Coding

**Goal:** Instead of building a separate research pipeline in Python, instruct the
code-generation LLM to research the problem domain itself before writing code.
The LLM has network access — tell it to use it.

**Files to modify:**
- `orchestrator/main.py` — Enhance `build_prompt()` with research instructions

**Implementation:**

1. In `build_prompt()`, add a new `<approach>` section between `<environment>` and
   `<task>`. This section instructs the LLM on *how* to approach the task:
   ```xml
   <approach>
   Before writing code, reason through these questions:
   1. What is the best approach for this task? Are there multiple strategies?
      Pick the most robust one.
   2. What packages or tools does this require? What are their current stable
      versions? If you're not sure, check PyPI (https://pypi.org/pypi/PACKAGE/json)
      or use pip's --dry-run flag to verify availability.
   3. Are there known pitfalls, breaking changes, or deprecations in the
      libraries you plan to use? If uncertain, check the docs.
   4. If the task involves an external API or data source, what is its current
      format/schema? Don't assume — verify if possible.

   Encode your research findings directly into your code as comments or as
   defensive checks. Don't produce a separate research document — just write
   better code because you researched first.
   </approach>
   ```

2. This section should appear for ALL tasks (not gated behind a flag). The LLM
   will naturally skip research for trivial tasks ("print hello world") and do
   more for complex ones. Trust the LLM's judgment.

3. Remove the `env_setup` special-casing for packages in `build_prompt()`. Instead
   of telling the LLM exactly which packages to install (via the `environment` field),
   include the package list as a *hint*:
   ```
   Suggested packages for this task: {pkgs}
   Install these if appropriate, but use your own judgment — add or substitute
   packages if you know a better option.
   ```
   This way the environment field becomes a suggestion, not a directive.

**Acceptance criteria:**
- Every prompt includes the `<approach>` section
- LLM is told to verify package versions and API formats via the network
- Package environment field becomes a suggestion, not a mandate
- No separate research LLM call — the code-generation LLM does its own research
- Works without any feature flags

---

## [x] Section 3: Proactive Tool Discovery — Let the LLM Find Its Own Tools

**Goal:** Instead of hardcoded keyword→tool mappings, instruct the LLM to discover,
evaluate, and install development tools on its own based on what it judges useful
for the task at hand. The LLM should use the network to find tools if needed.

**Files to modify:**
- `orchestrator/main.py` — Add tool discovery instructions to prompt
- `architect/planner.py` — Add tool-awareness to decomposition prompt

**Implementation:**

1. In `build_prompt()`, add to the `<approach>` section (from Section 2):
   ```
   5. Would any development tools improve the quality of your output?
      Consider linters, formatters, type checkers, test runners, or
      domain-specific tools. Install and use them if they'd catch bugs
      or improve code quality. You can search for tools with
      `pip search` alternatives (e.g., check PyPI directly) or simply
      install well-known tools in the relevant domain.
   ```

2. In `architect/planner.py`, add to `DECOMPOSITION_PROMPT` (in the instructions,
   not as a new field schema):
   ```
   When planning steps that produce code, consider what tools and quality
   checks would improve the result. You don't need to add separate "lint" or
   "test" steps — instead, instruct each code-producing step to install and
   run relevant quality tools as part of its workflow. The execution
   environment has full network access and can install anything.
   ```

3. Do NOT add a `tools` field to the step schema. Do NOT add keyword detection
   functions. The LLM decides what tools to use based on the task, and it
   explains its choices in the code comments or step descriptions.

**Acceptance criteria:**
- Prompt tells the LLM to consider and install dev tools proactively
- Planner prompt tells the LLM to bake quality tooling into step descriptions
- No hardcoded tool lists or keyword→tool mappings
- No new step schema fields — tool choices live in descriptions
- The LLM is trusted to make appropriate tool decisions

---

## [x] Section 4: Workspace Understanding — Let the LLM Analyze Context

**Goal:** Before generating code, give the LLM rich context about the existing
workspace. Currently `scan_workspace()` produces a shallow file listing. Instead,
provide actual file contents (imports, schemas, structure) and let the LLM
decide what's relevant.

**Files to modify:**
- `orchestrator/main.py` — Enhance `scan_workspace()` to include file content previews

**Implementation:**

1. Enhance `scan_workspace()` (keep the same function name) to include content previews:
   - For each text file (`.py`, `.json`, `.csv`, `.yaml`, `.md`, etc.) up to a budget:
     - Read the first 30 lines of the file
     - Include them in the listing as an indented preview
   - For binary files: just show name and size (as currently done)
   - Budget: stay within `max_chars` (increase default from 4000 to 8000)
   - Prioritize Python files first, then data files, then others
   - Skip files in `_SKIP_DIRS` (as currently done)

2. Format the enhanced listing so the LLM can naturally understand the codebase:
   ```
   === workspace contents ===
   app.py (1234 bytes, Python):
     import flask
     from models import User
     app = Flask(__name__)
     @app.route("/")
     def index():
     ...

   data.csv (5678 bytes, CSV):
     id,name,email,created_at
     1,Alice,alice@example.com,2024-01-01
     2,Bob,bob@example.com,2024-01-02
     ...

   config.json (234 bytes, JSON):
     {"database_url": "sqlite:///app.db", "port": 8080}

   utils/ (directory)
   ```

3. In `build_prompt()`, the `<workspace_state>` section should use this enhanced
   listing. The LLM will naturally detect frameworks, coding patterns, and data
   schemas from the actual content — no need for hardcoded framework detection.

4. Do NOT add separate framework detection, coding style analysis, or schema
   extraction functions. The LLM will do that analysis itself from the raw
   content previews.

**Acceptance criteria:**
- Workspace scan includes first 30 lines of text files
- Python files are prioritized in the listing
- Total output stays within budget (8000 chars default)
- No hardcoded framework/style detection — LLM reads the previews directly
- Binary files show name and size only

---

## [ ] Section 5: Enhanced Decomposition — Implicit Best Practices in Planning

**Goal:** The planner's decomposition prompt should encode best practices that
sophisticated users currently specify manually. The key is to instruct the LLM
to naturally apply these practices, not to enforce them with rigid rules.

**Files to modify:**
- `architect/planner.py` — Enhance `DECOMPOSITION_PROMPT`

**Implementation:**

1. Add the following section to `DECOMPOSITION_PROMPT` (after the anti-patterns):
   ```
   ## How an Expert Would Approach This
   Think like a senior engineer planning this project:

   - If you're unsure about the best library, API format, or approach for part of
     the task, add an early exploration step that investigates options and writes
     findings to a file. Later steps can read that file.
   - If a step produces code, describe what "done" looks like in the `verify` field.
     Don't just say "code works" — be specific about expected outputs.
   - If a step processes external data, mention validation in the description.
     Don't assume the data will be clean or in the expected format.
   - Structure steps so each one produces a concrete, verifiable artifact.
     A step that only "sets up" without producing testable output is a wasted step.
   - For project creation tasks, the first step should produce a complete skeleton
     (directory structure, config files, dependency manifest, .gitignore, README)
     in one shot. Don't spread project boilerplate across multiple steps.
   - You have full network access in the execution environment. If a step needs
     to discover the current version of a library, API endpoint format, or best
     practices, mention that in the step description. The executor can and will
     look things up.
   ```

2. Add a new anti-pattern:
   ```
   Anti-pattern: Assuming knowledge instead of verifying
   BAD: "Use the Twitter API v2 endpoint /tweets/search/recent" (may be outdated)
   GOOD: "Query the Twitter/X API documentation to find the current search endpoint, then implement"
   ```

3. Update the "Medium with dependencies" example to show a `verify` field with
   specific criteria (not just "check output exists").

**Acceptance criteria:**
- Decomposition prompt instructs the LLM to think like a senior engineer
- Research/exploration steps are described as a pattern, not mandated rigidly
- Verification criteria are specific, not generic
- Network access for information discovery is mentioned
- No new schema fields — practices are encoded in descriptions naturally

---

## [ ] Section 6: Smarter Error Context — Let the LLM Diagnose Root Causes

**Goal:** When passing errors back to the LLM for retry, provide better structure
that helps the LLM diagnose the issue. Instead of hardcoded exception→message
mappings, instruct the LLM to do root cause analysis itself.

**Files to modify:**
- `orchestrator/main.py` — Improve error sections in `build_prompt()`

**Implementation:**

1. Restructure the `<previous_error>` sections in `build_prompt()`. Currently,
   all retry attempts get the same kind of error block with different escalation
   language. Instead, give the LLM diagnostic instructions:

   For the first retry (attempt 2):
   ```xml
   <previous_error attempt="1">
   Your previous script failed. Here is the full output:

   {code_section}
   ```
   {previous_error}
   ```

   Before writing the fix, diagnose the root cause:
   - Read the error message carefully. What specific line/operation failed?
   - Is this a missing dependency, a wrong file path, a network issue, a logic error,
     or a data format mismatch?
   - What is the minimal change needed to fix it?

   Write your diagnosis in <analysis> tags, then write the corrected script.
   </previous_error>
   ```

   For the second retry (attempt 3):
   ```xml
   <previous_error attempt="2">
   Your script has failed twice. The previous approach is fundamentally flawed.

   {code_section}
   ```
   {previous_error}
   ```

   Do NOT repeat the same approach. Step back and consider:
   - Is there a completely different way to accomplish this task?
   - Is the task description itself ambiguous? Interpret it more conservatively.
   - Are you relying on an assumption that's incorrect (API format, file location,
     data schema)? Use the network to verify.

   Write your new approach in <analysis> tags, then write a new script from scratch.
   </previous_error>
   ```

   For the final attempt:
   ```xml
   <previous_error attempt="3">
   FINAL ATTEMPT. All previous approaches have failed.

   {code_section}
   ```
   {previous_error}
   ```

   Write the simplest possible script that accomplishes the core goal:
   - Use only the standard library if third-party packages are causing issues.
   - Wrap every external call in try/except with a meaningful fallback.
   - If the task involves network resources that may be unreliable, include
     offline fallback behavior.
   - Validate every input and assumption.

   Write your analysis in <analysis> tags, then write the defensive script.
   </previous_error>
   ```

2. Remove any hardcoded error classification or message rewriting from the
   orchestrator. The LLM does its own diagnosis from the raw error output.

**Acceptance criteria:**
- Each retry level has different diagnostic instructions for the LLM
- LLM is told to diagnose root causes, not given pre-digested error messages
- Second retry explicitly tells the LLM to try a different approach
- Final attempt tells the LLM to simplify and be defensive
- No hardcoded exception→message mappings in the orchestrator

---

## [ ] Section 7: System State Injection — Give the LLM Environmental Context

**Goal:** The LLM should know the current date, Python version, OS, available
disk space, and network status. This is factual context that helps it make better
decisions (e.g., knowing the date prevents using deprecated APIs).

**Files to modify:**
- `orchestrator/main.py` — Add system state collection and injection into prompt

**Implementation:**

1. Add a `collect_system_state() -> str` function:
   ```python
   def collect_system_state() -> str:
       """Collect system state for prompt context."""
       import platform, shutil
       from datetime import datetime
       lines = []
       lines.append(f"- Date: {datetime.now().strftime('%Y-%m-%d')}")
       lines.append(f"- Python: {platform.python_version()}")
       lines.append(f"- OS: {platform.system()} {platform.machine()}")
       try:
           usage = shutil.disk_usage(os.environ.get("WORKSPACE", "/workspace"))
           lines.append(f"- Disk free: {round(usage.free / (1024**3), 1)} GB")
       except Exception:
           pass
       return "\n".join(lines)
   ```

2. In `build_prompt()`, append the system state to the `<environment>` section:
   ```
   System info:
   {system_state}
   ```

3. Cache the system state string once per process (compute in `main()`, pass to
   all `build_prompt()` calls).

4. Also include the current date in the `CLAUDE_MD_TEMPLATE` in `claude_config.py`:
   Add `- Current date: {date}` to the Environment section. Update
   `get_claude_md_content()` to inject the actual date.

**Acceptance criteria:**
- Date, Python version, OS, and disk space appear in prompts
- System state is collected once and cached
- No network probes in system state (that's the LLM's job if it needs to check)
- Current date appears in both orchestrator prompt and CLAUDE.md

---

## [ ] Section 8: Cross-Run Knowledge Base — Persist What Works

**Goal:** The system should remember package versions that worked, error→solution
pairs, and successful approaches across runs. This is infrastructure — it collects
facts, not heuristics. The LLM decides how to use the knowledge.

**Files to modify:**
- `architect/state.py` — Add knowledge base persistence
- `architect/main.py` — Record knowledge after step execution
- `orchestrator/main.py` — Include knowledge in prompts

**Implementation:**

1. In `architect/state.py`, add:
   ```python
   def get_knowledge_base_path() -> str:
       workspace = os.environ.get("UAS_WORKSPACE", "/workspace")
       return os.path.join(workspace, ".state", "knowledge.json")

   def read_knowledge_base() -> dict:
       """Load knowledge base or return empty structure."""
       path = get_knowledge_base_path()
       if os.path.exists(path):
           with open(path, "r", encoding="utf-8") as f:
               return json.load(f)
       return {"package_versions": {}, "lessons": []}

   def append_knowledge(entry_type: str, data: dict):
       """Append an entry to the knowledge base."""
       kb = read_knowledge_base()
       if entry_type == "package_version":
           kb["package_versions"].update(data)
       elif entry_type == "lesson":
           kb["lessons"].append(data)
           # Cap at 50 entries
           if len(kb["lessons"]) > 50:
               kb["lessons"] = kb["lessons"][-50:]
       path = get_knowledge_base_path()
       os.makedirs(os.path.dirname(path), exist_ok=True)
       with open(path, "w", encoding="utf-8") as f:
           json.dump(kb, f, indent=2)
   ```

2. In `architect/main.py`:
   - After a step succeeds: scan stdout for `pip install` output and extract
     installed package versions (regex for `Successfully installed pkg-version`).
     Record them via `append_knowledge("package_version", {...})`.
   - After a step succeeds on a retry: record a lesson:
     ```python
     append_knowledge("lesson", {
         "error_snippet": error_info[:200],
         "solution_snippet": step["description"][:200],
         "step_title": step["title"],
     })
     ```

3. In `orchestrator/main.py`, in `build_prompt()`:
   - Accept `knowledge: dict | None` parameter
   - If knowledge has content, inject it as:
     ```xml
     <prior_knowledge>
     Package versions known to work in this environment:
     {formatted_package_versions}

     Lessons from previous runs:
     {formatted_lessons}

     Use this information to avoid repeating past mistakes and to use known-good versions.
     </prior_knowledge>
     ```
   - Place it after `<environment>` — it's contextual data.

4. In `main()` of the orchestrator, load the knowledge base at startup and pass
   to `build_prompt()`.

**Acceptance criteria:**
- Knowledge base persists to `.state/knowledge.json`
- Package versions are extracted from pip install output (regex, not hardcoded list)
- Error→solution lessons are recorded when retries succeed
- Knowledge appears in prompts for subsequent runs
- List is capped at 50 lessons to prevent unbounded growth
- The LLM decides how to use the knowledge — it's presented as context, not rules

---

## [ ] Section 9: Pre-Execution Sanity Checks — Catch Obvious Failures Fast

**Goal:** Before running generated code in the sandbox, catch issues that will
definitely fail — syntax errors, missing `UAS_RESULT`, use of `input()`. This
saves a sandbox round-trip. Keep checks to things that are objectively wrong,
not stylistic preferences.

**Files to modify:**
- `orchestrator/main.py` — Add pre-execution validation

**Implementation:**

1. Add `pre_execution_check(code: str) -> tuple[list[str], list[str]]` that
   returns `(critical_errors, warnings)`:
   - **Syntax check**: Try `compile(code, "<generated>", "exec")`. If it fails,
     return the syntax error as a critical error.
   - **Interactive input check**: Regex for `\binput\s*\(`. Critical error
     (sandbox has no stdin).
   - **UAS_RESULT check**: Verify the string `UAS_RESULT` appears in the code.
     Warning if missing (the code might construct it dynamically).
   - That's it. No import checking, no style checking, no path checking — those
     are the LLM's job. Only check things that are guaranteed failures.

2. In the execution loop, after `extract_code()` and before `run_in_sandbox()`:
   - Call `pre_execution_check()`
   - If there are critical errors: skip sandbox, use the error as `previous_error`
     for the next attempt, log "Pre-execution check failed: {error}"
   - If there are only warnings: log them, proceed with execution

3. When a pre-execution check catches a critical issue, the retry prompt should
   include:
   ```
   Your code was not executed because it has a fatal issue:
   {error}
   Fix this issue and regenerate.
   ```

**Acceptance criteria:**
- Only 2-3 checks, all for guaranteed failures (syntax, input())
- Critical errors skip sandbox execution
- Warnings are logged but don't block execution
- No style/convention checks — that's the LLM's domain
- Error messages tell the LLM exactly what's wrong

---

## [ ] Section 10: Goal Expansion — Clarify Vague Goals Automatically

**Goal:** When a user provides a vague goal, the system should expand it with
reasonable defaults. Let the LLM do the expansion — it's a language task.

**Files to modify:**
- `architect/planner.py` — Add goal expansion function
- `architect/main.py` — Call expansion before decomposition

**Implementation:**

1. In `architect/planner.py`, add:
   ```python
   def expand_goal(goal: str) -> str:
       """Expand a vague goal with reasonable defaults using LLM judgment."""
       client = get_llm_client(role="planner")
       prompt = f"""The user wants to accomplish this goal:
   "{goal}"

   If this goal is already clear and specific, return it unchanged.
   If it's vague or ambiguous, expand it with sensible defaults:
   - What should the output format be?
   - Where should outputs be saved?
   - What quality level is expected?
   - What scope is appropriate (prototype vs production)?

   Return ONLY the goal text (expanded or unchanged). No explanation."""

       try:
           expanded = client.generate(prompt)
           return expanded.strip() if expanded.strip() else goal
       except Exception:
           return goal
   ```

2. In `architect/main.py`, call `expand_goal()` after `get_goal()`:
   ```python
   goal = get_goal(args)
   original_goal = goal
   goal = expand_goal(goal)
   if goal != original_goal:
       logger.info("Expanded goal: %s", goal)
   state["original_goal"] = original_goal
   ```

3. Store both original and expanded goals in state for transparency.

**Acceptance criteria:**
- LLM decides whether and how to expand the goal
- Clear goals pass through unchanged
- Both original and expanded goals are in state
- Single LLM call (no multi-turn)
- If LLM call fails, original goal is used (graceful degradation)

---

## [ ] Section 11: Smarter Retry Context — Accumulate Attempt History

**Goal:** When the LLM retries, it should see all previous attempts and their
errors, not just the most recent one. This prevents it from cycling back to
approaches it already tried.

**Files to modify:**
- `orchestrator/main.py` — Track and include full attempt history in retry prompts

**Implementation:**

1. In `main()`, maintain an `attempt_history: list[dict]` that accumulates across
   retries:
   ```python
   attempt_history = []
   # After each failed attempt:
   attempt_history.append({
       "attempt": attempt,
       "error": previous_error[:500],
       "code_snippet": code[:300] if code else "",
   })
   ```

2. In `build_prompt()`, accept `attempt_history: list[dict] | None`. When
   present and non-empty, include ALL prior attempts in the error section:
   ```xml
   <attempt_history>
   You have tried {len(attempts)} times. Here is what happened:

   Attempt 1: {error_summary_1}
   Code approach: {code_snippet_1}

   Attempt 2: {error_summary_2}
   Code approach: {code_snippet_2}

   Do NOT repeat any of these approaches. Each new attempt must be fundamentally
   different from all previous ones.
   </attempt_history>
   ```

3. This replaces the current `previous_error` / `previous_code` single-attempt
   context. The full history is more useful than just the last failure.

**Acceptance criteria:**
- Full attempt history (errors + code snippets) is passed to retry prompts
- LLM is explicitly told not to repeat previous approaches
- History is truncated per-entry (500 chars error, 300 chars code) to fit context
- Works alongside the existing escalation in `<previous_error>` sections

---

## [ ] Section 12: LLM-Driven Retry Strategy — Let the LLM Choose Its Approach

**Goal:** Instead of hardcoded escalation sequences (reflect → alternative →
decompose → defensive), let the LLM choose its own retry strategy based on
what it learned from the failure. The LLM knows more about the problem than
our static rules.

**Files to modify:**
- `architect/main.py` — Restructure retry strategy in `execute_step()`
- `architect/planner.py` — Simplify `reflect_and_rewrite()` prompt

**Implementation:**

1. In `architect/planner.py`, modify `reflect_and_rewrite()` to remove the
   fixed escalation levels. Instead of `escalation_level` controlling the prompt,
   give the LLM all context and let it choose:

   Replace the escalation-level-specific prompt fragments with a single prompt
   that includes:
   ```
   This step has failed {attempts} time(s). Here is the full history:
   {attempt_history_with_errors}

   Based on these failures, decide your strategy:
   - If the core approach is sound but has a fixable bug, fix the bug.
   - If the approach itself is flawed, design a completely new approach.
   - If the task is too complex for a single script, break it into sequential
     phases within the same script (do phase 1, verify it worked, then phase 2).
   - If external resources are unreliable, add defensive fallbacks.

   Choose the strategy that best addresses the pattern of failures you see.
   Rewrite the step description accordingly.
   ```

2. In `execute_step()`, simplify the rewrite path:
   - Remove `_select_rewrite_strategy()` and the `_FALLBACK_STRATEGY` dict
   - Always call `reflect_and_rewrite()` with the full attempt history and
     reflections, letting the LLM decide the strategy
   - Keep `should_continue_retrying()` for stagnation detection — that's a valid
     safety check, not a strategy decision

3. Keep the `generate_reflection()` call — it's valuable for cross-step learning
   and stagnation detection. But don't use the reflection's `recommended_strategy`
   to select a hardcoded rewrite path. Instead, pass the reflection text directly
   to `reflect_and_rewrite()` as additional context.

**Acceptance criteria:**
- No hardcoded strategy selection (`_FALLBACK_STRATEGY` dict removed)
- `reflect_and_rewrite()` receives full history and chooses its own approach
- LLM is given a menu of strategies but chooses freely
- Stagnation detection still works (keeps the retry budget guard)
- `_select_rewrite_strategy()` is removed or inlined

---

## [ ] Section 13: Structured Output Reinforcement — Make UAS_RESULT Reliable

**Goal:** Make UAS_RESULT output more reliable without hardcoded auto-appending.
Instead, make the prompt so clear that the LLM virtually never forgets it, and
make the parser more tolerant of variations.

**Files to modify:**
- `orchestrator/main.py` — Strengthen UAS_RESULT prompt and parser

**Implementation:**

1. In `build_prompt()`, replace the current `<verification>` section with a
   more prominent and concrete version:
   ```xml
   <output_contract>
   YOUR SCRIPT MUST PRODUCE THIS OUTPUT. This is not optional.

   At the end of your script, print a result summary as the last line of stdout:

       import json
       result = {
           "status": "ok",
           "files_written": ["list", "of", "files", "you", "created"],
           "summary": "One sentence describing what was accomplished"
       }
       print(f"UAS_RESULT: {json.dumps(result)}")

   If your script encounters an unrecoverable error:

       import json
       result = {"status": "error", "error": "What went wrong and why"}
       print(f"UAS_RESULT: {json.dumps(result)}")
       sys.exit(1)

   The calling system parses this line to determine success or failure.
   If you don't print UAS_RESULT, the system cannot tell if you succeeded.
   </output_contract>
   ```

2. Rename `<verification>` to `<output_contract>` throughout to signal that
   this is a binding requirement, not a suggestion.

3. Improve `parse_uas_result()` in the orchestrator to handle common variations:
   - Case-insensitive match for "UAS_RESULT:" or "uas_result:"
   - Tolerate missing space after colon: `UAS_RESULT:{"status":...}`
   - Search all lines, not just lines matching the exact pattern (the current
     regex already does this, but verify)
   - Try to fix single-quoted JSON by replacing `'` with `"` as a fallback

4. In `parse_uas_result()` in `architect/executor.py` (the architect's copy),
   apply the same tolerance improvements.

**Acceptance criteria:**
- Prompt uses "output contract" language (stronger than "verification")
- Concrete code template is shown in the prompt
- Parser tolerates case variations and missing spaces
- Parser attempts single-quote→double-quote fix as fallback
- No auto-appending of UAS_RESULT to generated code

---

## [ ] Section 14: Planner Context Enrichment — Give the Planner Network Access

**Goal:** The planner (decomposition LLM) should be able to use the network to
research the goal before decomposing it. Currently the planner gets only the
raw goal text. For tasks involving APIs, libraries, or unfamiliar domains,
the planner should be told to look things up.

**Files to modify:**
- `architect/planner.py` — Add research instructions to decomposition prompt

**Implementation:**

1. Add to `DECOMPOSITION_PROMPT`, before the goal:
   ```
   You have full network access. If the goal involves:
   - An external API: Check its current documentation for endpoints and auth methods
   - A library you're unsure about: Verify it exists on PyPI and check its current version
   - A domain you're unfamiliar with: Look up best practices and common approaches

   Use what you learn to make your decomposition more specific and accurate.
   Don't guess at API formats or library capabilities — verify when uncertain.
   ```

2. In `orchestrator/llm_client.py`, for the planner role (`role="planner"`):
   - Do NOT pass `--tools ""` (which disables tools). The planner should have
     access to tools (including web search) if the Claude Code CLI provides them.
   - Currently, only `role="coder"` gets `--tools ""`. Verify that planner calls
     don't have tools disabled. If they already don't, this is a no-op — just
     add a code comment explaining why.

3. In the `generate()` method, when `stream=True` (used for planner), ensure
   the `--dangerously-skip-permissions` flag still allows tool use. Add a
   comment explaining the rationale: "Planner streams with tool access so it
   can research APIs and libraries during decomposition."

**Acceptance criteria:**
- Decomposition prompt tells the LLM to use the network for research
- Planner LLM calls do not have tools disabled
- Coder LLM calls still have tools disabled (to force fenced code output)
- Code comments explain the asymmetry (planner has tools, coder doesn't)

---

## [ ] Section 15: Implicit Git Management — Automate Version Control

**Goal:** Automatically manage Git in the workspace so the user doesn't have to
ask for it. Each step's output gets committed, creating a clean history.

**Files to modify:**
- `architect/main.py` — Add Git initialization and per-step commits

**Implementation:**

1. Add `ensure_git_repo(workspace: str)`:
   - If `.git/` doesn't exist and workspace has >1 file: `git init -b main`
   - Create `.gitignore` with common entries (Python, Node, .state/, .claude/)
   - `git add -A && git commit -m "Initial workspace state"`
   - Silently skip if git is not available or init fails

2. Add `git_checkpoint(workspace: str, step_id: int, step_title: str)`:
   - If workspace is not a git repo, skip silently
   - `git add -A` (respecting .gitignore)
   - `git commit -m "Step {step_id}: {step_title}"` — only if there are changes
   - Silently skip on any error (git should never block execution)

3. In `execute_step()`, call `git_checkpoint()` after a step succeeds and its
   output is recorded.

4. In `main()`, call `ensure_git_repo()` before phase 2 execution starts (after
   decomposition, before first step).

5. All git operations should be wrapped in try/except and logged at DEBUG level.
   Git failures should NEVER cause step failures — they're a convenience, not
   a requirement.

**Acceptance criteria:**
- Git repo initialized automatically if workspace has multiple files
- Each successful step gets a commit with a descriptive message
- .state/ and .claude/ are in .gitignore
- All git operations are silent on failure
- No env var gate — this is always on (but always silent on failure)

---

## [ ] Section 16: Output Quality Checks — Validate What Was Produced

**Goal:** After successful code execution, validate the quality of outputs.
Keep checks factual (files exist, formats are valid) — not stylistic.

**Files to modify:**
- `architect/main.py` — Add post-execution output validation

**Implementation:**

1. Add `check_output_quality(step: dict, workspace: str) -> list[str]`:
   - Check all files in `files_written` exist and are non-empty (>0 bytes)
   - For `.json` files: try `json.load()`, return error if invalid JSON
   - For `.csv` files: check first line exists (has headers)
   - For `.py` files: try `compile()`, return error if syntax error
   - Return list of issue strings (empty = clean)

2. Call `check_output_quality()` in `execute_step()`, after UAS_RESULT validation
   but before marking as completed (where the existing guardrail checks run).

3. For issues found:
   - Empty files or invalid formats: treat as step failure (trigger retry).
     Include the validation error in the retry context:
     ```
     Your script reported success but produced invalid output:
     - {issue}
     Fix the output and try again.
     ```
   - Log all issues at INFO level

4. Add a workspace cleanup after execution: remove `__pycache__/` directories
   and `.pyc` files from the workspace root (they're build artifacts, not outputs).

**Acceptance criteria:**
- All claimed output files are verified to exist and be non-empty
- JSON, CSV, and Python files are validated for format correctness
- Invalid outputs trigger a retry with a specific error message
- Cleanup removes __pycache__ and .pyc from workspace
- No style checking — only format validity

---

## [ ] Section 17: Package Version Context — Resolve Current Versions

**Goal:** Help the LLM pin package versions correctly by providing current stable
versions from PyPI. The LLM is told to look things up (Section 2), but we can
also provide pre-fetched version data to save time.

**Files to modify:**
- `orchestrator/main.py` — Add PyPI version resolution

**Implementation:**

1. Add `resolve_versions(packages: list[str]) -> dict[str, str]`:
   - For each package name, query `https://pypi.org/pypi/{name}/json`
   - Extract `info.version` (latest stable)
   - Use `urllib.request` (stdlib) with a 3-second timeout per request
   - Run requests concurrently with `ThreadPoolExecutor`
   - Cache results in a module-level dict for the process lifetime
   - Return dict of `{package: version}`, skipping any that failed

2. In `build_prompt()`, when `environment` packages are specified:
   - Call `resolve_versions()` on packages that don't already have `==` in them
   - Include results in the `<environment>` section:
     ```
     Current stable versions from PyPI (use these for pip install):
     - requests==2.32.3
     - pandas==2.2.1
     ```

3. Also check the knowledge base (Section 8) for cached versions before querying
   PyPI. Prefer knowledge base versions if they exist (they've been tested).

4. If all PyPI queries fail (no network), silently skip — the LLM will either
   know versions from training data or look them up itself (Section 2).

**Acceptance criteria:**
- PyPI is queried for packages without version pins
- Queries run concurrently with 3-second timeout
- Results are cached per-process
- Knowledge base versions are preferred over live PyPI queries
- Network failure degrades gracefully (no crash, no error)

---

## [ ] Section 18: Consolidate Feature Controls

**Goal:** Provide a clean interface for controlling optional behaviors. Most
features from this plan should be on by default — they're improvements, not
experiments. Provide a single escape hatch for minimal mode.

**Files to modify:**
- `orchestrator/main.py` — Add minimal mode check
- `architect/main.py` — Add minimal mode check
- `README.md` — Document new behaviors

**Implementation:**

1. Add a `UAS_MINIMAL=1` env var that disables all optional enhancements:
   - Skips goal expansion (Section 10)
   - Skips knowledge base loading (Section 8)
   - Skips PyPI version resolution (Section 17)
   - Skips git management (Section 15)
   - Uses shorter prompts (omits `<approach>`, `<prior_knowledge>` sections)

2. In both `orchestrator/main.py` and `architect/main.py`, add at the top:
   ```python
   MINIMAL_MODE = os.environ.get("UAS_MINIMAL", "").lower() in ("1", "true", "yes")
   ```
   Use this flag to skip optional enhancements.

3. When `UAS_MINIMAL` is NOT set (default):
   - All features from this plan are active
   - Goal expansion runs
   - Knowledge base is loaded and updated
   - PyPI versions are resolved
   - Git management runs
   - Full prompts with `<approach>`, `<prior_knowledge>`, etc.

4. Update `README.md`:
   - Add a new "Implicit Intelligence" section describing the behavioral changes
   - Document `UAS_MINIMAL` env var
   - Note that the system now automatically: researches before coding, manages git,
     expands vague goals, remembers lessons from past runs, and validates outputs

**Acceptance criteria:**
- `UAS_MINIMAL=1` disables all optional enhancements
- Default behavior (no env var) has all enhancements active
- README documents the new behaviors and the minimal mode escape hatch
- No per-feature env var gates (single toggle for all or nothing)

---

## [ ] Section 19: Integration Testing

**Goal:** Verify that all new features work correctly together. Tests should
be unit-level (no LLM calls, no network) using mocks.

**Files to modify:**
- `tests/` — Add new test files

**Implementation:**

1. Create `tests/test_workspace_scan.py`:
   - Test enhanced `scan_workspace()` with a temp directory containing sample files
   - Verify Python files get content previews
   - Verify binary files show size only
   - Verify budget is respected

2. Create `tests/test_pre_execution.py`:
   - Test `pre_execution_check()` with valid code, syntax errors, input() calls
   - Verify critical vs warning classification
   - Verify missing UAS_RESULT warning

3. Create `tests/test_knowledge_base.py`:
   - Test `read_knowledge_base()` and `append_knowledge()` round-trip
   - Test lesson cap at 50 entries
   - Test graceful handling of missing/corrupt file

4. Create `tests/test_output_quality.py`:
   - Test `check_output_quality()` with valid/invalid JSON, CSV, Python files
   - Test empty file detection
   - Test missing file detection

5. Create `tests/test_goal_expansion.py`:
   - Mock the LLM client
   - Test that `expand_goal()` passes through clear goals unchanged
   - Test that it returns the LLM's expansion for vague goals
   - Test graceful degradation on LLM failure

6. Create `tests/test_version_resolution.py`:
   - Mock `urllib.request.urlopen`
   - Test `resolve_versions()` with successful and failed responses
   - Test caching behavior
   - Test timeout handling

7. All tests use `pytest`, `unittest.mock`, and `tempfile`. No real LLM or
   network calls.

**Acceptance criteria:**
- At least 6 new test files
- All tests pass with `python -m pytest tests/ -x --timeout=10 -q`
- Tests mock all external dependencies (LLM, network, filesystem)
- No flaky tests — everything is deterministic

---

# Reusable Agent Prompt

Give this prompt to a coding agent in a fresh session to implement one section:

```
Read PLAN.md in this repository. Find the first section with an unchecked
checkbox ([ ] in the heading). Implement that section following its
instructions exactly.

When done:
1. Mark the section's checkbox as [x]
2. Ensure all existing tests still pass: python -m pytest tests/ -x --timeout=10 -q
3. Do NOT proceed to the next section — stop after completing one.

Important:
- Read the relevant source files before modifying them
- Follow the implementation instructions precisely
- Don't break existing functionality
- Keep changes minimal and focused on the section's requirements
- If a section references changes from a prior section that isn't implemented
  yet, implement a stub or skip that integration gracefully
```
