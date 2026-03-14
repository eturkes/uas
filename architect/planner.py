"""LLM-based task decomposition into atomic steps."""

import concurrent.futures
import json
import logging
import os
import re

from orchestrator.llm_client import get_llm_client
from .events import EventType, get_event_log

MAX_ERROR_LENGTH = int(os.environ.get("UAS_MAX_ERROR_LENGTH", "0"))
_DEFAULT_REWRITE_TRIM = 3000

logger = logging.getLogger(__name__)


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


DECOMPOSITION_PROMPT = """\
<goal>{goal}</goal>

<examples>
Example 1 — Trivial (single step):
Goal: "Print the current date and time"
<analysis>
Complexity: trivial (1 step). Single action, no dependencies, no external packages.
Sub-problems: none. Risk areas: none. Parallelization: N/A.
Failure modes: none expected.
</analysis>
<complexity_assessment>trivial — single standard library call, 1 step</complexity_assessment>
[{{"title": "Print datetime", "description": "Write a Python script that prints the current date and time using the datetime module.", "depends_on": [], "verify": "stdout contains a date/time string", "environment": []}}]

Example 2 — Medium with dependencies:
Goal: "Download a CSV from a URL, clean it, and produce summary statistics"
<analysis>
Complexity: medium. Three distinct phases: download, clean, analyze.
Sub-problems: network reliability for download, data quality for cleaning, correct statistics.
Risk areas: CSV format variability, missing values handling.
Parallelization: none — strictly sequential pipeline.
Failure modes: network timeout, malformed CSV, empty dataset after cleaning.
</analysis>
<complexity_assessment>medium — 3 sequential data processing steps</complexity_assessment>
[
  {{"title": "Download CSV", "description": "Download the CSV file from the given URL using requests and save it to the workspace as raw_data.csv. Validate the response is valid CSV (not HTML error page). Print the number of rows and columns.", "depends_on": [], "verify": "raw_data.csv exists in workspace, has >0 rows, and stdout prints row/column counts", "environment": ["requests"]}},
  {{"title": "Clean data", "description": "Read raw_data.csv from the workspace, handle missing values (drop rows with >50% nulls, fill numeric nulls with median), remove duplicates, and save as cleaned_data.csv. Print cleaning summary showing rows before, rows dropped, rows remaining.", "depends_on": [1], "verify": "cleaned_data.csv exists, row count <= raw_data.csv row count, stdout shows before/after row counts and number of nulls filled", "environment": ["pandas"]}},
  {{"title": "Summary statistics", "description": "Read cleaned_data.csv, compute summary statistics (mean, median, std, min, max for numeric columns), and save results to summary.json and summary.txt. Print the summary table to stdout.", "depends_on": [2], "verify": "summary.json contains keys for each numeric column with mean/median/std/min/max values; summary.txt is human-readable; stdout shows the statistics table", "environment": ["pandas"]}}
]

Example 3 — Complex with parallelism:
Goal: "Scrape product info from two websites and compare prices"
<analysis>
Complexity: medium. Two independent scraping tasks plus a comparison.
Sub-problems: web scraping reliability, product name matching across sites.
Risk areas: website structure changes, rate limiting, inconsistent product naming.
Parallelization: scraping site A and B are fully independent — run in parallel.
Failure modes: blocked by site, empty results, no matching products.
</analysis>
<complexity_assessment>medium — 2 parallel scraping steps + 1 comparison</complexity_assessment>
[
  {{"title": "Scrape site A", "description": "Scrape product names and prices from site A using requests and BeautifulSoup. Save results as site_a_products.json in the workspace. Print count of products found.", "depends_on": [], "verify": "site_a_products.json exists and contains a non-empty list", "environment": ["requests", "beautifulsoup4"]}},
  {{"title": "Scrape site B", "description": "Scrape product names and prices from site B using requests and BeautifulSoup. Save results as site_b_products.json in the workspace. Print count of products found.", "depends_on": [], "verify": "site_b_products.json exists and contains a non-empty list", "environment": ["requests", "beautifulsoup4"]}},
  {{"title": "Compare prices", "description": "Read site_a_products.json and site_b_products.json from the workspace. Match products by name and compare prices. Save comparison to price_comparison.csv and print a summary of which site is cheaper on average.", "depends_on": [1, 2], "verify": "price_comparison.csv exists and contains matched products", "environment": ["pandas"]}}
]
</examples>

<anti_patterns>
Common decomposition mistakes to avoid:
- Over-splitting trivial tasks: a single pip install + script does not need 3 separate steps
- Under-splitting complex tasks: a step that does "download, parse, transform, analyze, and visualize" should be broken down
- Missing dependencies: if step 3 reads a file written by step 1, it must list step 1 in depends_on
- Implicit ordering: steps that must run sequentially need explicit depends_on, even if the order seems obvious
- Overly vague descriptions: "process the data" tells the code-generating LLM nothing — be specific about format, method, and expected output
- Forgetting error boundaries: each step should handle its own errors rather than assuming upstream perfection
- Broken data contracts: when one step produces data (CSV, JSON, database) and another \
consumes it, the descriptions MUST agree on the exact column names, key names, data types, \
and file formats. Never let the consumer step "assume" or "guess" column names — always \
specify that it must read from the actual file or use names established by the producer step. \
For example, if a data source has column "SEX" (numeric 1/2), don't tell a downstream \
visualization step to look for "Sex" (string "Male"/"Female") — the consumer must either \
use the exact source column name or a data-loading step must explicitly perform the mapping.
- Assuming knowledge instead of verifying: don't assume specific API endpoints, library \
interfaces, or data formats are current — they may have changed. \
BAD: "Use the Twitter API v2 endpoint /tweets/search/recent" (may be outdated) \
GOOD: "Query the Twitter/X API documentation to find the current search endpoint, then implement"
</anti_patterns>

<expert_approach>
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
</expert_approach>

<instructions>
You are a task decomposition engine. Given the goal above, break it into \
atomic, independently executable steps that form a directed acyclic graph (DAG).

First, in <analysis> tags, thoroughly reason about:
- Key sub-problems and how they relate to each other
- Risk areas and likely failure modes for each sub-problem
- Parallelization opportunities (which sub-problems are independent?)
- External dependencies (network, APIs, large data) that may fail

Then, in <complexity_assessment> tags, explicitly estimate:
- Complexity category: trivial (1 step), simple (2-3), medium (4-7), complex (8+)
- Justification for the number of steps chosen

When planning steps that produce code, consider what tools and quality
checks would improve the result. You don't need to add separate "lint" or
"test" steps — instead, instruct each code-producing step to install and
run relevant quality tools as part of its workflow. The execution
environment has full network access and can install anything.

Then produce the step DAG as a JSON array.
</instructions>

<rules>
1. Each step MUST be a self-contained Python script task.
2. Steps share a persistent workspace directory for file I/O. \
The path is available via os.environ.get('WORKSPACE', '/workspace'). \
Later steps can read files written by earlier steps from this directory.
3. Each step should be as small and focused as possible — the smaller the \
subtask, the more reliable the execution.
4. Steps can run in parallel when they have no dependency relationship. \
Maximize parallelism by making steps independent whenever possible.
5. Scale the number of steps to the goal's complexity: \
1 step for trivial tasks, 2-3 for simple, 4-7 for medium, 8+ for complex.
6. The execution environment has full unrestricted network access and complete \
autonomy. Install any packages needed (pip, apt-get, etc.) without hesitation.
7. Each step must produce observable output to stdout so downstream steps \
can use the results.
8. Do NOT create steps that require user interaction.
9. When creating any project, the FIRST step must initialize a Git repository \
using `git init -b main` (use "main" as the default branch), add an appropriate \
`.gitignore`, and make an initial commit.
10. All projects must include a README.md and requirements.txt with pinned versions.
11. Never hardcode secrets or API keys in step descriptions — instruct the code \
to read them from environment variables.
12. Always prefer HTTPS URLs. Pin dependency versions. Use context managers for I/O.
13. When a step produces structured data (CSV, JSON, database) that other steps consume, \
the step description MUST explicitly state the column names, key names, or schema. \
Consumer steps MUST reference the exact names from the producer step — never invent \
aliases. If a data-loading/preprocessing step sits between a producer and consumers, \
its description must specify the exact mapping (e.g., "rename column SEX to Sex"). \
Instruct consumer steps to read the actual file headers at runtime rather than \
hardcoding assumed column names.
</rules>

<output_format>
Respond with <analysis> tags, then <complexity_assessment> tags, then ONLY a JSON \
array. Each element:
{{"title": "short name", \
"description": "detailed task for a code-generating LLM", \
"depends_on": [step_numbers], \
"verify": "how to verify this step succeeded beyond exit code 0", \
"environment": ["pip or apt packages needed, if any"]}}

Steps are numbered starting from 1. depends_on references must use 1-based step \
numbers (e.g. step 2 depending on step 1 should have "depends_on": [1]).
</output_format>
"""


def parse_steps_json(response: str) -> list[dict]:
    # Strip <analysis> and <complexity_assessment> tags if present (LLM reasoning preamble)
    text = re.sub(r"<analysis>.*?</analysis>", "", response, flags=re.DOTALL).strip()
    text = re.sub(r"<complexity_assessment>.*?</complexity_assessment>", "", text, flags=re.DOTALL).strip()

    # Direct parse
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    # Code fence extraction
    match = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group(1).strip())
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    # Bracket extraction
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1:
        try:
            result = json.loads(text[start : end + 1])
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not parse steps from LLM response:\n{text[:500]}")


def validate_depends_on(steps: list[dict]) -> None:
    """Validate depends_on references: no out-of-range refs, no circular deps."""
    n = len(steps)
    for i, step in enumerate(steps):
        deps = step.get("depends_on", [])
        if not isinstance(deps, list):
            raise ValueError(f"Step {i + 1} has non-list depends_on: {deps}")
        for dep in deps:
            if not isinstance(dep, int):
                raise ValueError(
                    f"Step {i + 1} depends_on contains non-integer: {dep}"
                )
            if dep < 1 or dep > n:
                raise ValueError(
                    f"Step {i + 1} depends on step {dep}, "
                    f"but only steps 1-{n} exist."
                )
            if dep == i + 1:
                raise ValueError(f"Step {i + 1} depends on itself.")

    # Cycle detection using DFS
    adj = {i + 1: step.get("depends_on", []) for i, step in enumerate(steps)}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {i + 1: WHITE for i in range(n)}

    def dfs(node):
        color[node] = GRAY
        for neighbor in adj[node]:
            if color[neighbor] == GRAY:
                raise ValueError(
                    f"Circular dependency detected involving "
                    f"step {node} -> step {neighbor}."
                )
            if color[neighbor] == WHITE:
                dfs(neighbor)
        color[node] = BLACK

    for node in range(1, n + 1):
        if color[node] == WHITE:
            dfs(node)


def topological_sort(steps: list[dict]) -> list[list[int]]:
    """Sort steps into execution levels using Kahn's algorithm.

    Returns a list of levels, where each level contains step IDs
    that can run concurrently (all dependencies are in earlier levels).

    Raises ValueError if the graph contains a cycle.
    """
    if not steps:
        return []

    in_degree = {}
    dependents = {}
    for step in steps:
        sid = step["id"]
        deps = step.get("depends_on", [])
        in_degree[sid] = len(deps)
        dependents.setdefault(sid, [])
        for dep in deps:
            dependents.setdefault(dep, []).append(sid)

    levels = []
    ready = [sid for sid, deg in in_degree.items() if deg == 0]

    while ready:
        levels.append(sorted(ready))
        next_ready = []
        for sid in ready:
            for dep_id in dependents.get(sid, []):
                in_degree[dep_id] -= 1
                if in_degree[dep_id] == 0:
                    next_ready.append(dep_id)
        ready = next_ready

    placed = sum(len(level) for level in levels)
    if placed != len(steps):
        raise ValueError(
            f"Topological sort failed: placed {placed}/{len(steps)} steps. "
            "Cycle detected in dependency graph."
        )

    return levels


def decompose_goal(goal: str) -> list[dict]:
    client = get_llm_client(role="planner")
    prompt = DECOMPOSITION_PROMPT.format(goal=goal)
    event_log = get_event_log()
    event_log.emit(EventType.LLM_CALL_START, data={"purpose": "decompose_goal"})
    response = client.generate(prompt, stream=True)
    event_log.emit(EventType.LLM_CALL_COMPLETE, data={"purpose": "decompose_goal"})
    steps = parse_steps_json(response)
    if not steps:
        raise ValueError("LLM returned an empty step list")
    for step in steps:
        if "title" not in step or "description" not in step:
            raise ValueError(f"Step missing required fields: {step}")
        step.setdefault("depends_on", [])
        step.setdefault("verify", "")
        step.setdefault("environment", [])

    # Normalize 0-indexed depends_on to 1-indexed.
    # The LLM sometimes returns 0-based step references despite the prompt
    # requesting 1-based. Detect this by checking if any step references
    # step 0 (which doesn't exist in 1-based numbering).
    has_zero_ref = any(0 in s.get("depends_on", []) for s in steps)
    if has_zero_ref:
        for step in steps:
            step["depends_on"] = [d + 1 for d in step["depends_on"]]

    validate_depends_on(steps)
    return steps


COMPLEXITY_PROMPT = """\
<goal>{goal}</goal>

Rate the complexity of this goal for an autonomous code-generation system.
Categories:
- trivial: single action, 1 step (e.g. print a value, run one command)
- simple: 2-3 straightforward steps with clear dependencies
- medium: 4-7 steps, may involve external APIs, data processing pipelines, or multi-file projects
- complex: 8+ steps, significant architecture, multiple interacting components

Respond with ONLY one word: trivial, simple, medium, or complex.
"""

# Prompt suffixes used to elicit diverse decomposition plans for voting.
_VOTING_SUFFIXES = [
    "",  # Plan A: default
    (
        "\n\n<approach_hint>Approach this from the angle of SIMPLICITY: "
        "minimize the number of steps and prefer combining related work "
        "into single steps where safe.</approach_hint>"
    ),
    (
        "\n\n<approach_hint>Approach this from the angle of ROBUSTNESS: "
        "prefer more granular steps with explicit error handling between "
        "phases, even if it means more steps.</approach_hint>"
    ),
]


def estimate_complexity(goal: str) -> str:
    """Make a quick LLM call to estimate goal complexity.

    Returns one of: 'trivial', 'simple', 'medium', 'complex'.
    Falls back to 'medium' on parse failure.
    """
    client = get_llm_client(role="planner")
    prompt = COMPLEXITY_PROMPT.format(goal=goal)
    event_log = get_event_log()
    event_log.emit(EventType.LLM_CALL_START, data={"purpose": "estimate_complexity"})
    try:
        response = client.generate(prompt).strip().lower()
    except Exception as e:
        logger.warning("Complexity estimation failed, defaulting to medium: %s", e)
        return "medium"
    event_log.emit(EventType.LLM_CALL_COMPLETE, data={"purpose": "estimate_complexity"})

    for category in ("trivial", "simple", "medium", "complex"):
        if category in response:
            return category
    logger.warning("Could not parse complexity '%s', defaulting to medium.", response)
    return "medium"


PLAN_SELECTION_PROMPT = """\
<goal>{goal}</goal>

<candidate_plans>
{plans_text}
</candidate_plans>

<instructions>
You are selecting the best execution plan for the goal above. Each candidate plan
is a list of steps that will be executed as Python scripts in isolated workspaces.

Evaluate each plan on these criteria:
1. **Correctness**: Does the plan actually accomplish the goal? Are the steps logically sound?
2. **Completeness**: Does the plan cover all aspects of the goal without missing steps?
3. **Dependencies**: Are step dependencies correct? Will each step have what it needs?
4. **Parallelism**: Does the plan exploit parallelism where safe to do so?
5. **Risk**: Does the plan minimize failure risk (e.g., not combining unrelated work)?

Reason through each plan briefly, then select the best one.

Return ONLY a JSON object:
{{"selected_plan": <0-based index of the best plan>, "reasoning": "<brief explanation>"}}
</instructions>
"""


def select_best_plan(goal: str, plans: list[list[dict]]) -> tuple[list[dict], int]:
    """Use the LLM to select the best plan from candidates.

    Returns (best_plan, best_index). Falls back to score_plan() on failure.
    """
    # Format plans for the prompt
    plan_sections = []
    for i, plan in enumerate(plans):
        steps_desc = []
        for j, step in enumerate(plan):
            deps = step.get("depends_on", [])
            deps_str = f" (depends on: {deps})" if deps else ""
            steps_desc.append(
                f"  Step {j + 1}: {step.get('title', 'Untitled')}{deps_str}\n"
                f"    {step.get('description', '')}"
            )
        plan_sections.append(f"Plan {i}:\n" + "\n".join(steps_desc))

    plans_text = "\n\n".join(plan_sections)
    prompt = PLAN_SELECTION_PROMPT.format(goal=goal, plans_text=plans_text)

    event_log = get_event_log()
    event_log.emit(EventType.LLM_CALL_START, data={"purpose": "select_best_plan"})
    try:
        client = get_llm_client(role="planner")
        response = client.generate(prompt)
        event_log.emit(
            EventType.LLM_CALL_COMPLETE, data={"purpose": "select_best_plan"}
        )

        # Parse JSON from response (may be wrapped in code fences)
        text = response.strip()
        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence_match:
            text = fence_match.group(1)
        else:
            # Try to find a JSON object directly
            brace_match = re.search(r"\{.*\}", text, re.DOTALL)
            if brace_match:
                text = brace_match.group(0)

        result = json.loads(text)
        selected = int(result["selected_plan"])
        if 0 <= selected < len(plans):
            reasoning = result.get("reasoning", "")
            logger.info("  LLM selected plan %d: %s", selected, reasoning)
            return plans[selected], selected
        logger.warning(
            "  LLM selected invalid plan index %d, falling back to score_plan().",
            selected,
        )
    except Exception as e:
        logger.warning("  LLM plan selection failed (%s), falling back to score_plan().", e)

    # Fallback: use heuristic scoring
    scored = [(score_plan(p), i, p) for i, p in enumerate(plans)]
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][2], scored[0][1]


def score_plan(steps: list[dict]) -> float:
    """Score a decomposition plan for selection during voting.

    Score = parallelism_ratio * 0.4 + specificity * 0.3 + compactness * 0.3

    - parallelism_ratio: 1 - (num_levels / num_steps), higher = more parallel
    - specificity: avg description length / 500, capped at 1.0
    - compactness: 1 / num_steps, fewer steps = higher score
    """
    n = len(steps)
    if n == 0:
        return 0.0

    # Parallelism: compute execution levels
    indexed = [{**s, "id": i + 1} for i, s in enumerate(steps)]
    try:
        levels = topological_sort(indexed)
        num_levels = len(levels)
    except ValueError:
        num_levels = n  # Invalid DAG, worst-case parallelism

    parallelism_ratio = max(0.0, 1.0 - (num_levels / n)) if n > 1 else 0.0

    # Specificity: average description length, capped
    avg_desc_len = sum(len(s.get("description", "")) for s in steps) / n
    specificity = min(avg_desc_len / 500.0, 1.0)

    # Compactness: fewer steps is better
    compactness = 1.0 / n

    return parallelism_ratio * 0.4 + specificity * 0.3 + compactness * 0.3


def decompose_goal_with_voting(goal: str, n_samples: int = 3) -> list[dict]:
    """Generate multiple decomposition plans and select the best one.

    Uses a complexity gate: trivial/simple goals skip voting entirely.
    Medium/complex goals generate n_samples plans in parallel and pick
    the highest-scoring one.

    Returns (steps, complexity) tuple-style via the steps list, with the
    estimated complexity stored in the module-level for the caller to read.
    """
    event_log = get_event_log()

    # 2c: Complexity estimation gate
    complexity = estimate_complexity(goal)
    event_log.emit(EventType.COMPLEXITY_ESTIMATE, data={"complexity": complexity})
    logger.info("  Estimated complexity: %s", complexity)

    # Store for caller access
    decompose_goal_with_voting.last_complexity = complexity

    if complexity in ("trivial", "simple"):
        logger.info("  Skipping voting for %s goal, using single decomposition.", complexity)
        return decompose_goal(goal)

    # 2a: Generate N plans in parallel
    logger.info("  Generating %d plans for voting...", n_samples)

    def _generate_plan(suffix_idx: int) -> list[dict] | None:
        """Generate a single plan variant. Returns None on failure."""
        try:
            client = get_llm_client(role="planner")
            suffix = _VOTING_SUFFIXES[suffix_idx] if suffix_idx < len(_VOTING_SUFFIXES) else ""
            prompt = DECOMPOSITION_PROMPT.format(goal=goal) + suffix
            response = client.generate(prompt)
            steps = parse_steps_json(response)
            if not steps:
                return None
            for step in steps:
                if "title" not in step or "description" not in step:
                    return None
                step.setdefault("depends_on", [])
                step.setdefault("verify", "")
                step.setdefault("environment", [])
            # Normalize 0-indexed depends_on
            has_zero_ref = any(0 in s.get("depends_on", []) for s in steps)
            if has_zero_ref:
                for step in steps:
                    step["depends_on"] = [d + 1 for d in step["depends_on"]]
            validate_depends_on(steps)
            return steps
        except Exception as e:
            logger.warning("  Plan generation variant %d failed: %s", suffix_idx, e)
            return None

    plans: list[list[dict]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_samples) as executor:
        futures = {
            executor.submit(_generate_plan, i): i
            for i in range(n_samples)
        }
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result is not None:
                plans.append(result)

    if not plans:
        logger.warning("  All voting plans failed, falling back to single decomposition.")
        return decompose_goal(goal)

    if len(plans) == 1:
        logger.info("  Only 1 valid plan generated, using it directly.")
        event_log.emit(EventType.VOTING_COMPLETE, data={
            "plans_generated": 1,
            "plans_valid": 1,
            "winning_score": score_plan(plans[0]),
        })
        return plans[0]

    # 2b: LLM-based plan selection (falls back to score_plan heuristic)
    for i, plan in enumerate(plans):
        logger.info("  Plan %d: %d steps", i + 1, len(plan))

    best_plan, best_idx = select_best_plan(goal, plans)
    logger.info("  Selected plan %d (%d steps).", best_idx + 1, len(best_plan))

    event_log.emit(EventType.VOTING_COMPLETE, data={
        "plans_generated": n_samples,
        "plans_valid": len(plans),
        "winning_plan": best_idx,
        "winning_score": score_plan(best_plan),
    })

    return best_plan


# Initialize the attribute for complexity storage
decompose_goal_with_voting.last_complexity = None


CRITIQUE_PROMPT = """\
<goal>{goal}</goal>

<proposed_steps>
{steps_json}
</proposed_steps>

<instructions>
You are reviewing a task decomposition plan. Analyze the proposed steps and \
identify any issues. Be concise and actionable.
</instructions>

<review_criteria>
1. Are any steps too broad and should be split further?
2. Are there missing steps needed to achieve the goal?
3. Are the dependencies correct? Could any steps be made independent to enable parallelism?
4. Are there missing error handling considerations for external resources (network, files)?
5. Are the verify fields specific enough to catch subtle failures?
6. Are environment/package requirements complete?
7. If the goal involves creating a project, does the plan include initializing a Git repo with `git init -b main`, adding a `.gitignore`, and making an initial commit?
8. Does the plan ensure a README.md and requirements.txt (with pinned versions) are created for any multi-file project?
9. Do step descriptions avoid hardcoding secrets and instead instruct reading from environment variables?
10. Do steps use HTTPS URLs, not plain HTTP?
</review_criteria>

If the plan is good, respond with exactly: PLAN_OK

If there are issues, respond with the corrected JSON array of steps in the same \
format as the original. Include ONLY the JSON array, no explanation.
"""


def critique_and_refine_plan(goal: str, steps: list[dict]) -> list[dict]:
    """Send the plan to the LLM for critique and optionally refine it.

    Single-pass review: if the LLM identifies issues it returns a corrected
    plan, otherwise returns the original steps unchanged.
    """
    client = get_llm_client(role="planner")
    steps_json = json.dumps(steps, indent=2)
    prompt = CRITIQUE_PROMPT.format(goal=goal, steps_json=steps_json)

    event_log = get_event_log()
    try:
        event_log.emit(EventType.LLM_CALL_START, data={"purpose": "critique_plan"})
        response = client.generate(prompt, stream=True)
        event_log.emit(EventType.LLM_CALL_COMPLETE, data={"purpose": "critique_plan"})
    except Exception as e:
        logger.warning("Plan critique failed, using original plan: %s", e)
        return steps

    text = response.strip()
    if "PLAN_OK" in text:
        logger.info("  Plan critique: no issues found.")
        return steps

    # Try to parse refined steps
    try:
        refined = parse_steps_json(text)
        if refined:
            logger.info(
                "  Plan critique: refined %d -> %d steps.",
                len(steps),
                len(refined),
            )
            # Re-validate
            for step in refined:
                if "title" not in step or "description" not in step:
                    raise ValueError(f"Refined step missing required fields: {step}")
                step.setdefault("depends_on", [])
                step.setdefault("verify", "")
                step.setdefault("environment", [])
            validate_depends_on(refined)
            return refined
    except (ValueError, json.JSONDecodeError) as e:
        logger.warning("Could not parse critique response, using original plan: %s", e)

    return steps


REFLECT_PROMPT = """\
<failure_output>
<stdout>{stdout}</stdout>
<stderr>{stderr}</stderr>
</failure_output>

<original_task>{description}</original_task>
{previous_attempts_section}{escalation_instruction}
<instructions>
You are diagnosing and fixing a failed code-generation task. Follow the structured \
reflection process below.
</instructions>

<process>
1. In <diagnosis> tags, analyze: What specifically went wrong? Why? Categorize the \
error as one of: dependency issue, logic error, environment problem, network issue, \
or data format mismatch.

2. In <counterfactual> tags, reason about whether the root cause is in this step \
or propagated from a dependency step. If a prior step produced incorrect output \
that this step consumes, identify which step and what output is wrong.

3. In <strategies> tags, propose 2-3 alternative strategies to solve the task. \
Select the best one with justification. Ensure the chosen strategy follows best \
practices: use HTTPS URLs, pin dependency versions, use context managers for I/O, \
catch specific exceptions, use git init -b main for repos, and never hardcode secrets.

4. After the tags, provide ONLY the improved task description. Be more specific and \
explicit about what the Python code should do. Do not include any explanation.

5. Self-check: before responding, verify your output:
- Is it a valid, actionable task description (not an error analysis)?
- Is it similar in scope to the original task (not vastly longer or shorter)?
- Does it avoid repeating the error output verbatim?
If your output fails these checks, revise it before responding.
</process>
"""

REFLECTION_GEN_PROMPT = """\
<error_output>
<stdout>{stdout}</stdout>
<stderr>{stderr}</stderr>
</error_output>

<task_description>{description}</task_description>
<attempt_number>{attempt}</attempt_number>

<instructions>
Generate a structured reflection on this failure. Respond with ONLY a JSON object \
(no markdown, no code fences, no explanation) with exactly these fields:
{{"error_type": "MUST be exactly one of these canonical types: dependency_error, logic_error, environment_error, network_error, timeout, format_error, unknown. \
Use dependency_error for missing packages/modules/imports. \
Use logic_error for TypeError, ValueError, AttributeError, KeyError, and other programming mistakes. \
Use environment_error for file system, permissions, disk, or memory issues. \
Use network_error for connection failures, DNS, SSL, or HTTP errors. \
Use timeout for operations that exceeded time limits. \
Use format_error for parsing failures, JSON errors, syntax errors, or unexpected output format. \
Use unknown only if the error truly does not fit any other category.",
"root_cause": "brief description of what caused the failure",
"strategy_tried": "what approach was used in this attempt",
"lesson": "what was learned from this failure",
"what_to_try_next": "concrete suggestion for the next attempt",
"recommended_strategy": "MUST be exactly one of: reflect_and_fix, alternative_approach, decompose_into_phases, defensive_rewrite. \
Use reflect_and_fix when the error is a small, localised bug that can be corrected with a targeted change. \
Use alternative_approach when the fundamental approach is flawed and a completely different technique should be tried. \
Use decompose_into_phases when the task is too complex for a single script and should be broken into sequential sub-phases. \
Use defensive_rewrite when multiple prior attempts have failed and the safest, most conservative implementation is needed.",
"confidence": "MUST be exactly one of: high, medium, low. \
Use high when you are confident in your diagnosis and the suggested fix addresses a clear, identifiable issue. \
Use medium when the root cause is likely but not certain, or the fix is a reasonable guess. \
Use low when the error is ambiguous, the root cause is unclear, or you are unsure the suggested approach will work."}}
</instructions>
"""


ROOT_CAUSE_PROMPT = """\
<failed_step>
<description>{description}</description>
<error>{error}</error>
</failed_step>

<completed_dependencies>
{dependency_info}
</completed_dependencies>

<instructions>
A step failed with the error shown above. This step depends on previously completed steps.

Determine: is the root cause of this failure in the current step itself, or was it \
caused by incorrect or incomplete output from one of its dependency steps?

Consider:
- If this step reads files produced by a dependency, are those files likely correct?
- Could the dependency have produced subtly wrong output that causes this step to fail?
- Is the error clearly a code issue in this step (syntax, logic, missing import)?

Respond with ONLY one of:
- SELF (if the root cause is in this step)
- STEP_N (where N is the dependency step number, if the root cause is in that dependency)
</instructions>
"""


def generate_reflection(step: dict, stdout: str, stderr: str,
                        attempt: int) -> dict:
    """Generate a structured reflection on a step failure via LLM.

    Returns a dict with keys: error_type, root_cause, strategy_tried,
    lesson, what_to_try_next. Falls back to a basic reflection on failure.
    """
    client = get_llm_client(role="planner")
    prompt = REFLECTION_GEN_PROMPT.format(
        description=step["description"],
        stdout=stdout[-2000:] if len(stdout) > 2000 else stdout,
        stderr=stderr[-2000:] if len(stderr) > 2000 else stderr,
        attempt=attempt,
    )

    event_log = get_event_log()
    event_log.emit(EventType.LLM_CALL_START,
                   data={"purpose": "generate_reflection"})
    try:
        response = client.generate(prompt)
        event_log.emit(EventType.LLM_CALL_COMPLETE,
                       data={"purpose": "generate_reflection"})
    except Exception as e:
        logger.warning("Reflection generation failed: %s", e)
        return {
            "attempt": attempt,
            "error_type": "unknown",
            "root_cause": stderr[:200] if stderr else "unknown",
            "strategy_tried": f"attempt {attempt}",
            "lesson": "LLM reflection failed",
            "what_to_try_next": "retry with different approach",
        }

    # Parse JSON from response
    text = response.strip()
    # Strip code fences if present
    if text.startswith("```"):
        text = re.sub(r"```(?:json)?\s*\n?", "", text)
        text = text.rstrip("`").strip()

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            reflection = {
                "attempt": attempt,
                "error_type": data.get("error_type", "unknown"),
                "root_cause": data.get("root_cause", "unknown"),
                "strategy_tried": data.get("strategy_tried", "unknown"),
                "lesson": data.get("lesson", ""),
                "what_to_try_next": data.get("what_to_try_next", ""),
                "recommended_strategy": data.get("recommended_strategy", ""),
                "confidence": data.get("confidence", "medium"),
            }
            return reflection
    except json.JSONDecodeError:
        pass

    # Fallback: extract what we can
    logger.warning("Could not parse reflection JSON, using fallback.")
    return {
        "attempt": attempt,
        "error_type": "unknown",
        "root_cause": stderr[:200] if stderr else "unknown",
        "strategy_tried": f"attempt {attempt}",
        "lesson": text[:200] if text else "",
        "what_to_try_next": "retry with different approach",
    }


def trace_root_cause(step: dict, error: str,
                     completed_outputs: dict,
                     state: dict) -> tuple[str, int | None]:
    """Determine if a failure's root cause is in this step or a dependency.

    Only called when the step has dependencies. Uses an LLM to reason about
    whether the error was caused by incorrect dependency output.

    Returns ("self", None) or ("dependency", step_id).
    """
    if not step.get("depends_on"):
        return ("self", None)

    step_by_id = {s["id"]: s for s in state.get("steps", [])}
    dep_lines = []
    for dep_id in step["depends_on"]:
        dep_step = step_by_id.get(dep_id, {})
        output = completed_outputs.get(dep_id, "")
        if isinstance(output, dict):
            stdout = output.get("stdout", "")
            files = output.get("files", [])
        else:
            stdout = str(output)
            files = []
        dep_lines.append(
            f"Step {dep_id} ({dep_step.get('title', '?')}): "
            f"files={files}, output_preview={stdout[:300]}"
        )

    dependency_info = "\n".join(dep_lines)
    client = get_llm_client(role="planner")
    prompt = ROOT_CAUSE_PROMPT.format(
        description=step["description"],
        error=error[:1000],
        dependency_info=dependency_info,
    )

    event_log = get_event_log()
    event_log.emit(EventType.LLM_CALL_START,
                   data={"purpose": "trace_root_cause"})
    try:
        response = client.generate(prompt).strip().upper()
        event_log.emit(EventType.LLM_CALL_COMPLETE,
                       data={"purpose": "trace_root_cause"})
    except Exception as e:
        logger.warning("Root cause tracing failed: %s", e)
        return ("self", None)

    # Parse response
    match = re.search(r"STEP_(\d+)", response)
    if match:
        dep_id = int(match.group(1))
        if dep_id in step["depends_on"]:
            logger.info("  Root cause traced to dependency step %d.", dep_id)
            return ("dependency", dep_id)
        logger.warning("  Root cause traced to step %d but it's not a dependency.", dep_id)

    return ("self", None)


ESCALATION_INSTRUCTIONS = {
    0: "",
    1: (
        "\n<escalation>IMPORTANT: The previous approach has already failed. "
        "You MUST propose a fundamentally different strategy. Do NOT repeat "
        "or slightly modify the previous approach.</escalation>"
    ),
    3: (
        "\n<escalation>This is the FINAL attempt. Be maximally defensive:\n"
        "- Include diagnostic commands at the start of the script (print Python "
        "version, list installed packages, check network connectivity)\n"
        "- Wrap EVERY external call in try/except with detailed error messages\n"
        "- Validate ALL inputs before using them\n"
        "- Use the simplest possible approach to achieve the goal</escalation>"
    ),
}

DECOMPOSE_STEP_PROMPT = """\
<failed_task>{description}</failed_task>

<failure_output>
<stdout>{stdout}</stdout>
<stderr>{stderr}</stderr>
</failure_output>

<instructions>
A code-generation task has failed multiple times. Break it down into 2-3 smaller, \
more manageable sub-phases that can be expressed as a single sequential script.

Rewrite the task as a detailed, step-by-step description that breaks the work into \
explicit sequential phases within a single script. Each phase should be simple enough \
to be unlikely to fail. Include explicit error handling between phases.

Provide ONLY the improved task description. No explanation.
</instructions>
"""


def _is_confused_output(result: str, original_desc: str, error: str) -> bool:
    """Check if the LLM output shows structural signs of confusion."""
    # Excessive length (>3x original, minimum threshold 2000)
    if len(result) > max(len(original_desc) * 3, 2000):
        return True
    # Repeating the error verbatim (>200 chars of error text found in result)
    if error and len(error) > 200 and error[:200] in result:
        return True
    return False


def reflect_and_rewrite(step: dict, orchestrator_stdout: str,
                        orchestrator_stderr: str,
                        escalation_level: int = 0,
                        previous_attempts: list[dict] | None = None,
                        reflections: list[dict] | None = None) -> str:
    """Reflection-based task rewrite with progressive escalation.

    Uses structured diagnosis/strategy/rewrite phases instead of simple rewriting.
    Includes red-flagging: outputs showing signs of confusion are resampled once.

    Args:
        previous_attempts: List of prior attempt summaries for this step,
            each with keys: attempt, error, strategy. Included in the prompt
            so the LLM can see the full history and avoid repeating failed strategies.
        reflections: List of structured reflections from prior failures
            (Section 3a), each with keys: attempt, error_type, root_cause,
            strategy_tried, lesson, what_to_try_next. Included as
            <reflection_history> in the prompt.
    """
    client = get_llm_client(role="planner")

    trim_limit = MAX_ERROR_LENGTH if MAX_ERROR_LENGTH > 0 else _DEFAULT_REWRITE_TRIM
    stdout_trimmed = orchestrator_stdout[-trim_limit:] if len(orchestrator_stdout) > trim_limit else orchestrator_stdout
    stderr_trimmed = orchestrator_stderr[-trim_limit:] if len(orchestrator_stderr) > trim_limit else orchestrator_stderr

    escalation = ESCALATION_INSTRUCTIONS.get(escalation_level, "")

    previous_attempts_section = ""
    if previous_attempts:
        lines = []
        for attempt in previous_attempts:
            lines.append(
                f"- Attempt {attempt['attempt']}: "
                f"error={attempt['error'][:200]} | "
                f"strategy={attempt['strategy']}"
            )
        previous_attempts_section = (
            "\n<previous_attempts>\n"
            "Summary of ALL prior attempts for this step (do NOT repeat failed strategies):\n"
            + "\n".join(lines)
            + "\n</previous_attempts>"
        )

    # Section 3a: Include structured reflection history
    if reflections:
        ref_lines = []
        for ref in reflections:
            ref_lines.append(
                f"- Attempt {ref['attempt']}: "
                f"error_type={ref['error_type']}, "
                f"root_cause={ref['root_cause']}, "
                f"lesson={ref['lesson']}, "
                f"try_next={ref.get('what_to_try_next', 'N/A')}"
            )
        previous_attempts_section += (
            "\n<reflection_history>\n"
            "Structured reflections from ALL prior failures for this step:\n"
            + "\n".join(ref_lines)
            + "\n</reflection_history>"
        )

    prompt = REFLECT_PROMPT.format(
        description=step["description"],
        stdout=stdout_trimmed,
        stderr=stderr_trimmed,
        escalation_instruction=escalation,
        previous_attempts_section=previous_attempts_section,
    )

    event_log = get_event_log()
    event_log.emit(EventType.LLM_CALL_START,
                   data={"purpose": "reflect_and_rewrite",
                         "escalation_level": escalation_level})
    response = client.generate(prompt)
    event_log.emit(EventType.LLM_CALL_COMPLETE,
                   data={"purpose": "reflect_and_rewrite"})

    # Strip diagnosis, counterfactual, and strategies tags to get just the description
    result = re.sub(r"<diagnosis>.*?</diagnosis>", "", response, flags=re.DOTALL)
    result = re.sub(r"<counterfactual>.*?</counterfactual>", "", result, flags=re.DOTALL)
    result = re.sub(r"<strategies>.*?</strategies>", "", result, flags=re.DOTALL)
    result = result.strip()

    # Red-flagging: check for signs of confusion and resample once.
    # Also resample if the most recent reflection has low confidence.
    low_confidence = (
        reflections and reflections[-1].get("confidence") == "low"
    )
    if low_confidence:
        logger.warning("  Low-confidence reflection, resampling rewrite...")
    if _is_confused_output(result, step["description"], stderr_trimmed) or low_confidence:
        logger.warning("  Red-flag detected in rewrite output, resampling...")
        response = client.generate(prompt)
        result = re.sub(r"<diagnosis>.*?</diagnosis>", "", response, flags=re.DOTALL)
        result = re.sub(r"<counterfactual>.*?</counterfactual>", "", result, flags=re.DOTALL)
        result = re.sub(r"<strategies>.*?</strategies>", "", result, flags=re.DOTALL)
        result = result.strip()

    return result if result else step["description"]


MERGE_EVALUATION_PROMPT = """\
<goal>{goal}</goal>

<steps>
{steps_text}
</steps>

<instructions>
You are evaluating which steps in an execution plan can be safely merged together.
Steps in the same execution level have no dependencies between them and could run
concurrently, but merging related steps reduces overhead.

For each execution level that has multiple steps, decide which pairs of steps
(if any) should be merged. Steps should only be merged if:
1. They are semantically related (working toward the same sub-goal)
2. The combined task remains simple enough for a single script to handle well
3. Merging them does not create an overly complex or unrelated multi-task step

Only propose merges within the same execution level (steps shown together below).
Each step can appear in at most one merge pair.

Return ONLY a JSON object:
{{"merges": [[step_a_id, step_b_id], ...], "reasoning": "<brief explanation>"}}

If no merges are appropriate, return:
{{"merges": [], "reasoning": "<brief explanation>"}}
</instructions>
"""


def merge_steps_with_llm(goal: str, steps: list[dict]) -> list[dict]:
    """Use the LLM to decide which steps to merge based on semantic relatedness.

    Falls back to merge_trivial_steps() on parse failure.
    """
    if len(steps) < 2:
        return steps

    # Assign temporary IDs for topological sort
    indexed = []
    for i, s in enumerate(steps):
        indexed.append({**s, "id": i + 1})

    try:
        levels = topological_sort(indexed)
    except ValueError:
        return steps

    # Only proceed if there are levels with 2+ steps
    multi_levels = [level for level in levels if len(level) >= 2]
    if not multi_levels:
        return steps

    # Format steps grouped by level for the prompt
    level_sections = []
    for level_idx, level in enumerate(levels):
        step_descs = []
        for sid in level:
            s = steps[sid - 1]
            deps = s.get("depends_on", [])
            deps_str = f" (depends on: {deps})" if deps else ""
            step_descs.append(
                f"  Step {sid}: {s.get('title', 'Untitled')}{deps_str}\n"
                f"    {s.get('description', '')}"
            )
        level_sections.append(
            f"Level {level_idx + 1}:\n" + "\n".join(step_descs)
        )

    steps_text = "\n\n".join(level_sections)
    prompt = MERGE_EVALUATION_PROMPT.format(goal=goal, steps_text=steps_text)

    event_log = get_event_log()
    event_log.emit(EventType.LLM_CALL_START, data={"purpose": "merge_steps"})
    try:
        client = get_llm_client(role="planner")
        response = client.generate(prompt)
        event_log.emit(
            EventType.LLM_CALL_COMPLETE, data={"purpose": "merge_steps"}
        )

        # Parse JSON from response (may be wrapped in code fences)
        text = response.strip()
        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence_match:
            text = fence_match.group(1)
        else:
            brace_match = re.search(r"\{.*\}", text, re.DOTALL)
            if brace_match:
                text = brace_match.group(0)

        result = json.loads(text)
        merge_pairs = result.get("merges", [])
        reasoning = result.get("reasoning", "")

        if not merge_pairs:
            logger.info("  LLM merge evaluation: no merges suggested. %s", reasoning)
            return steps

        # Validate merge pairs: each must be a pair of valid step IDs in the same level
        level_of = {}
        for level in levels:
            for sid in level:
                level_of[sid] = tuple(level)

        valid_pairs = []
        used_ids = set()
        for pair in merge_pairs:
            if (
                isinstance(pair, list)
                and len(pair) == 2
                and all(isinstance(x, int) for x in pair)
            ):
                a, b = pair
                if (
                    1 <= a <= len(steps)
                    and 1 <= b <= len(steps)
                    and a != b
                    and a not in used_ids
                    and b not in used_ids
                    and level_of.get(a) == level_of.get(b)
                ):
                    valid_pairs.append((a, b))
                    used_ids.add(a)
                    used_ids.add(b)

        if not valid_pairs:
            logger.warning(
                "  LLM merge returned no valid pairs, falling back to merge_trivial_steps()."
            )
            return merge_trivial_steps(steps)

        # Perform the merges
        merged = []
        merged_ids = set()
        for a_id, b_id in valid_pairs:
            a = steps[a_id - 1]
            b = steps[b_id - 1]
            combined_title = f"{a['title']} + {b['title']}"
            combined_desc = (
                f"Phase 1: {a['description']}\n"
                f"Phase 2: {b['description']}"
            )
            combined_deps = sorted(
                set(a.get("depends_on", []) + b.get("depends_on", []))
            )
            combined_env = sorted(
                set(a.get("environment", []) + b.get("environment", []))
            )
            combined_verify = "; ".join(
                v for v in [a.get("verify", ""), b.get("verify", "")] if v
            )
            merged.append({
                "title": combined_title,
                "description": combined_desc,
                "depends_on": combined_deps,
                "verify": combined_verify,
                "environment": combined_env,
            })
            merged_ids.add(a_id - 1)
            merged_ids.add(b_id - 1)

        # Add unmerged steps
        for i, s in enumerate(steps):
            if i not in merged_ids:
                merged.append(s)

        # Build old (1-based) -> new (1-based) ID mapping
        # merged list order: merged pairs first, then unmerged in original order
        old_to_new: dict[int, int] = {}
        new_id = 1
        for a_id, b_id in valid_pairs:
            old_to_new[a_id] = new_id
            old_to_new[b_id] = new_id  # both old steps map to the merged step
            new_id += 1
        for i, s in enumerate(steps):
            if i not in merged_ids:
                old_to_new[i + 1] = new_id
                new_id += 1

        # Remap depends_on references and remove self-references
        for new_idx, s in enumerate(merged, 1):
            s.setdefault("depends_on", [])
            s.setdefault("verify", "")
            s.setdefault("environment", [])
            remapped = set()
            for dep in s["depends_on"]:
                new_dep = old_to_new.get(dep, dep)
                if new_dep != new_idx:  # remove self-references
                    remapped.add(new_dep)
            s["depends_on"] = sorted(remapped)

        logger.info(
            "  LLM step merging: %d -> %d steps. %s",
            len(steps), len(merged), reasoning,
        )
        return merged

    except Exception as e:
        logger.warning(
            "  LLM merge evaluation failed (%s), falling back to merge_trivial_steps().",
            e,
        )
        return merge_trivial_steps(steps)


def merge_trivial_steps(steps: list[dict]) -> list[dict]:
    """Merge trivially combinable steps within the same execution level.

    Steps are merged if they are in the same execution level (no dependency
    relationship between them), both have short descriptions (< 200 chars),
    and share no complex dependency structures. This reduces LLM calls and
    sandbox invocations for simple goals.
    """
    if len(steps) < 2:
        return steps

    MAX_DESC_LEN = 200

    # Assign temporary IDs for topological sort
    indexed = []
    for i, s in enumerate(steps):
        indexed.append({**s, "id": i + 1})

    try:
        levels = topological_sort(indexed)
    except ValueError:
        return steps

    # Build lookup from temp ID to original index
    id_to_idx = {i + 1: i for i in range(len(steps))}

    merged = []
    merged_ids = set()  # original indices that got merged into another
    # Track old 1-based ID -> new 1-based position in merged list
    old_to_new: dict[int, int] = {}

    for level in levels:
        # Find candidates: short descriptions, in this level
        candidates = []
        non_candidates = []
        for sid in level:
            idx = id_to_idx[sid]
            s = steps[idx]
            if (
                len(s.get("description", "")) < MAX_DESC_LEN
                and idx not in merged_ids
            ):
                candidates.append((idx, s))
            else:
                non_candidates.append((idx, s))

        # Merge candidates in pairs
        i = 0
        while i < len(candidates) - 1:
            idx_a, a = candidates[i]
            idx_b, b = candidates[i + 1]
            combined_title = f"{a['title']} + {b['title']}"
            combined_desc = (
                f"Phase 1: {a['description']}\n"
                f"Phase 2: {b['description']}"
            )
            # Union of depends_on, environment
            combined_deps = sorted(set(a.get("depends_on", []) + b.get("depends_on", [])))
            combined_env = sorted(set(a.get("environment", []) + b.get("environment", [])))
            combined_verify = "; ".join(
                v for v in [a.get("verify", ""), b.get("verify", "")] if v
            )
            merged_step = {
                "title": combined_title,
                "description": combined_desc,
                "depends_on": combined_deps,
                "verify": combined_verify,
                "environment": combined_env,
            }
            new_pos = len(merged) + 1  # 1-based position
            old_to_new[idx_a + 1] = new_pos  # both originals map to merged
            old_to_new[idx_b + 1] = new_pos
            merged.append(merged_step)
            merged_ids.add(idx_a)
            merged_ids.add(idx_b)
            i += 2

        # Remaining unpaired candidate
        if i < len(candidates):
            idx_c, _ = candidates[i]
            old_to_new[idx_c + 1] = len(merged) + 1
            merged.append(candidates[i][1])

        # Non-candidates pass through
        for idx_nc, s in non_candidates:
            old_to_new[idx_nc + 1] = len(merged) + 1
            merged.append(s)

    # Remap depends_on references and remove self-references
    for new_idx, s in enumerate(merged, 1):
        s.setdefault("depends_on", [])
        s.setdefault("verify", "")
        s.setdefault("environment", [])
        remapped = set()
        for dep in s["depends_on"]:
            new_dep = old_to_new.get(dep, dep)
            if new_dep != new_idx:  # remove self-references
                remapped.add(new_dep)
        s["depends_on"] = sorted(remapped)

    if len(merged) < len(steps):
        logger.info(
            "  Step merging: %d -> %d steps.", len(steps), len(merged)
        )

    return merged


REPLAN_PROMPT = """\
<goal>{goal}</goal>

<completed_steps>
{completed_steps_info}
</completed_steps>

<unexpected_result>
Step {step_id} ({step_title}) produced an unexpected result:
{unexpected_detail}
</unexpected_result>

<remaining_steps>
{remaining_steps_json}
</remaining_steps>

<instructions>
The plan is being executed but a step has produced output that doesn't match \
what downstream steps expect. Given what has been accomplished so far, adjust \
the remaining steps to account for the actual output.

Rules:
1. Keep ALL completed steps unchanged — only modify pending steps.
2. Fix dependency references, file names, and descriptions to match actual outputs.
3. You may add, remove, or reorder pending steps if necessary.
4. Preserve the overall goal — adjust HOW to achieve it, not WHAT to achieve.
5. Each step must still be a self-contained Python script task.
6. Maintain valid depends_on references (1-based step numbers).
7. Do not re-number completed steps — new/modified steps should start numbering \
after the last completed step.

Respond with ONLY a JSON array of the REMAINING steps (not completed ones). \
Each element:
{{"title": "short name", \
"description": "detailed task for a code-generating LLM", \
"depends_on": [step_numbers], \
"verify": "how to verify this step succeeded", \
"environment": ["packages needed"]}}
</instructions>
"""


def replan_remaining_steps(goal: str, state: dict,
                           unexpected_step: dict,
                           unexpected_detail: str) -> list[dict] | None:
    """Incrementally re-plan remaining steps after an unexpected result.

    Instead of re-decomposing from scratch, adjusts pending steps based on
    actual outputs from completed steps. Returns the new list of remaining
    steps, or None if re-planning fails.

    Section 6b of PLAN.md.
    """
    client = get_llm_client(role="planner")
    event_log = get_event_log()

    # Build completed steps info
    completed_info_lines = []
    for s in state.get("steps", []):
        if s["status"] == "completed":
            files = s.get("files_written", [])
            summary = s.get("summary", "")
            completed_info_lines.append(
                f"- Step {s['id']} ({s['title']}): "
                f"files={files}, summary={summary}"
            )
    completed_steps_info = "\n".join(completed_info_lines) or "None yet."

    # Build remaining steps JSON
    remaining = [
        s for s in state.get("steps", [])
        if s["status"] not in ("completed",) and s["id"] != unexpected_step["id"]
    ]
    remaining_json = json.dumps([
        {
            "id": s["id"],
            "title": s["title"],
            "description": s["description"],
            "depends_on": s["depends_on"],
            "verify": s.get("verify", ""),
            "environment": s.get("environment", []),
        }
        for s in remaining
    ], indent=2)

    prompt = REPLAN_PROMPT.format(
        goal=goal,
        completed_steps_info=completed_steps_info,
        step_id=unexpected_step["id"],
        step_title=unexpected_step["title"],
        unexpected_detail=unexpected_detail,
        remaining_steps_json=remaining_json,
    )

    event_log.emit(EventType.LLM_CALL_START,
                   data={"purpose": "replan_remaining_steps"})
    try:
        response = client.generate(prompt, stream=True)
        event_log.emit(EventType.LLM_CALL_COMPLETE,
                       data={"purpose": "replan_remaining_steps"})
    except Exception as e:
        logger.warning("Re-planning LLM call failed: %s", e)
        return None

    try:
        new_steps = parse_steps_json(response)
        if not new_steps:
            return None
        for step in new_steps:
            if "title" not in step or "description" not in step:
                return None
            step.setdefault("depends_on", [])
            step.setdefault("verify", "")
            step.setdefault("environment", [])
        # Normalize 0-indexed depends_on
        has_zero_ref = any(0 in s.get("depends_on", []) for s in new_steps)
        if has_zero_ref:
            for step in new_steps:
                step["depends_on"] = [d + 1 for d in step["depends_on"]]
        # Re-planned steps may reference completed step IDs outside this
        # list, so we can't use validate_depends_on (which assumes a
        # self-contained 1-indexed list).  Instead, validate that deps
        # reference either completed steps or other new steps, and that
        # there are no cycles among the new steps themselves.
        #
        # The LLM may use different numbering schemes for new steps:
        #   - Positional 1-based indices (1, 2, 3, ...)
        #   - Continuation IDs after max completed (e.g. 4, 5, 6, ...)
        #   - Explicit "id" fields in the JSON
        # Accept all of these as valid references.
        completed_ids = {
            s["id"] for s in state.get("steps", [])
            if s["status"] == "completed"
        }
        max_completed = max(completed_ids) if completed_ids else 0
        n = len(new_steps)
        # Build the set of all valid new-step IDs the LLM might use
        positional_ids = set(range(1, n + 1))
        continuation_ids = {max_completed + i + 1 for i in range(n)}
        llm_ids = {s["id"] for s in new_steps if "id" in s}
        valid_new_ids = positional_ids | continuation_ids | llm_ids
        all_valid = completed_ids | valid_new_ids
        for i, step in enumerate(new_steps):
            for dep in step.get("depends_on", []):
                if not isinstance(dep, int):
                    raise ValueError(
                        f"New step {i + 1} has non-integer dep: {dep}"
                    )
                if dep not in all_valid:
                    raise ValueError(
                        f"New step {i + 1} references unknown step {dep}"
                    )
                # Self-reference check using all possible IDs for this step
                self_ids = {i + 1, max_completed + i + 1}
                if "id" in step:
                    self_ids.add(step["id"])
                if dep in self_ids and dep not in completed_ids:
                    raise ValueError(
                        f"New step {i + 1} depends on itself."
                    )
        return new_steps
    except (ValueError, json.JSONDecodeError) as e:
        logger.warning("Could not parse re-planned steps: %s", e)
        return None


def _extract_file_schema(filepath: str) -> str | None:
    """Extract schema info (column names, JSON keys) from a data file.

    Returns a short string like 'columns: [A, B, C] (3 columns)' or None.
    Used to enrich dependent steps with the actual data contract.
    """
    if not os.path.isfile(filepath):
        return None
    try:
        if filepath.endswith((".csv", ".tsv")):
            sep = "\t" if filepath.endswith(".tsv") else ","
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                header = f.readline().strip()
            if not header:
                return None
            cols = [c.strip().strip('"').strip("'") for c in header.split(sep)]
            col_str = str(cols)
            if len(col_str) > 1500:
                col_str = col_str[:1500] + f"...] ({len(cols)} columns total)"
            return f"columns: {col_str}"
        if filepath.endswith(".json"):
            import json as _json
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                data = _json.loads(f.read(8000))
            if isinstance(data, dict):
                return f"keys: {list(data.keys())}"
            if isinstance(data, list) and data and isinstance(data[0], dict):
                return f"list of {len(data)} items, keys: {list(data[0].keys())}"
    except Exception:
        pass
    return None


def enrich_step_descriptions(
    completed_step: dict,
    dependent_steps: list[dict],
    existing_enrichments: dict | None = None,
    workspace: str | None = None,
) -> tuple[list[int], dict[int, str]]:
    """Build enrichment context for dependent steps from a completed step.

    Instead of mutating step descriptions directly, returns enrichment
    data to be stored in state and injected via build_context(). This
    allows the enrichment to be filtered/compressed by the existing
    compression logic rather than being permanently baked into descriptions.

    Returns (enriched_ids, enrichments) where enrichments maps step_id
    to enrichment text.

    Section 6c / Section 11 of PLAN.md.
    """
    enriched_ids = []
    enrichments = {}
    existing = existing_enrichments or {}
    files_written = completed_step.get("files_written", [])
    summary = completed_step.get("summary", "")
    uas_result = completed_step.get("uas_result", {})

    if not files_written and not summary:
        return enriched_ids, enrichments

    # Build enrichment text
    parts = []
    if files_written:
        parts.append(f"files produced: {', '.join(files_written[:10])}")
    if summary:
        parts.append(f"output summary: {summary}")
    if uas_result and isinstance(uas_result, dict):
        result_summary = uas_result.get("summary", "")
        if result_summary and result_summary != summary:
            parts.append(f"result: {result_summary}")

    # Extract schemas from data files so downstream steps know the exact
    # column names / keys to code against (prevents data contract mismatch).
    if workspace and files_written:
        for fpath in files_written[:5]:
            if not fpath.endswith((".csv", ".tsv", ".json")):
                continue
            full = os.path.join(workspace, fpath) if not os.path.isabs(fpath) else fpath
            if not os.path.isfile(full):
                # Try within workspace subdirectories
                base = os.path.basename(fpath)
                for root, _dirs, _files in os.walk(workspace):
                    if base in _files:
                        full = os.path.join(root, base)
                        break
                    _dirs[:] = [d for d in _dirs if d not in
                                (".state", ".git", "__pycache__")]
            schema = _extract_file_schema(full)
            if schema:
                parts.append(f"{os.path.basename(fpath)} {schema}")

    if not parts:
        return enriched_ids, enrichments

    enrichment = (
        f"[Context from step {completed_step['id']} "
        f"({completed_step['title']}): {'; '.join(parts)}]"
    )

    for dep_step in dependent_steps:
        step_id = dep_step["id"]
        # Avoid duplicate enrichment from the same source step
        marker = f"[Context from step {completed_step['id']} "
        existing_text = existing.get(step_id, "")
        if marker in existing_text:
            continue
        enrichments[step_id] = enrichment
        enriched_ids.append(step_id)

    return enriched_ids, enrichments


def decompose_failing_step(step: dict, orchestrator_stdout: str,
                           orchestrator_stderr: str) -> str:
    """Decompose a failing step into a more granular multi-phase description."""
    client = get_llm_client(role="planner")

    trim_limit = MAX_ERROR_LENGTH if MAX_ERROR_LENGTH > 0 else _DEFAULT_REWRITE_TRIM
    stdout_trimmed = orchestrator_stdout[-trim_limit:] if len(orchestrator_stdout) > trim_limit else orchestrator_stdout
    stderr_trimmed = orchestrator_stderr[-trim_limit:] if len(orchestrator_stderr) > trim_limit else orchestrator_stderr

    prompt = DECOMPOSE_STEP_PROMPT.format(
        description=step["description"],
        stdout=stdout_trimmed,
        stderr=stderr_trimmed,
    )

    result = client.generate(prompt).strip()
    return result if result else step["description"]
