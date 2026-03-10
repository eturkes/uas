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
  {{"title": "Download CSV", "description": "Download the CSV file from the given URL using requests and save it to the workspace as raw_data.csv. Print the number of rows and columns.", "depends_on": [], "verify": "raw_data.csv exists in workspace and has >0 rows", "environment": ["requests"]}},
  {{"title": "Clean data", "description": "Read raw_data.csv from the workspace, handle missing values (drop rows with >50% nulls, fill numeric nulls with median), remove duplicates, and save as cleaned_data.csv. Print cleaning summary.", "depends_on": [1], "verify": "cleaned_data.csv exists and has fewer or equal rows to raw_data.csv", "environment": ["pandas"]}},
  {{"title": "Summary statistics", "description": "Read cleaned_data.csv, compute summary statistics (mean, median, std, min, max for numeric columns), and save results to summary.json and summary.txt. Print the summary.", "depends_on": [2], "verify": "summary.json and summary.txt exist in workspace", "environment": ["pandas"]}}
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
</anti_patterns>

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

    # 2b: Score and select
    scored = [(score_plan(p), i, p) for i, p in enumerate(plans)]
    scored.sort(key=lambda x: x[0], reverse=True)

    for score, idx, plan in scored:
        logger.info(
            "  Plan %d: %d steps, score=%.3f",
            idx + 1, len(plan), score,
        )

    best_score, best_idx, best_plan = scored[0]
    logger.info("  Selected plan %d (score=%.3f, %d steps).",
                best_idx + 1, best_score, len(best_plan))

    event_log.emit(EventType.VOTING_COMPLETE, data={
        "plans_generated": n_samples,
        "plans_valid": len(plans),
        "scores": [{"plan": i, "score": round(s, 4), "steps": len(p)}
                   for s, i, p in scored],
        "winning_plan": best_idx,
        "winning_score": round(best_score, 4),
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
{{"error_type": "one of: dependency_error, logic_error, environment_error, network_error, timeout, format_error, unknown",
"root_cause": "brief description of what caused the failure",
"strategy_tried": "what approach was used in this attempt",
"lesson": "what was learned from this failure",
"what_to_try_next": "concrete suggestion for the next attempt"}}
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

    # Red-flagging: check for signs of confusion and resample once
    if _is_confused_output(result, step["description"], stderr_trimmed):
        logger.warning("  Red-flag detected in rewrite output, resampling...")
        response = client.generate(prompt)
        result = re.sub(r"<diagnosis>.*?</diagnosis>", "", response, flags=re.DOTALL)
        result = re.sub(r"<counterfactual>.*?</counterfactual>", "", result, flags=re.DOTALL)
        result = re.sub(r"<strategies>.*?</strategies>", "", result, flags=re.DOTALL)
        result = result.strip()

    return result if result else step["description"]


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
            merged.append(merged_step)
            merged_ids.add(idx_a)
            merged_ids.add(idx_b)
            i += 2

        # Remaining unpaired candidate
        if i < len(candidates):
            merged.append(candidates[i][1])

        # Non-candidates pass through
        for _, s in non_candidates:
            merged.append(s)

    # Renumber depends_on references for the merged list
    # Build mapping: old 1-based index -> new 1-based index
    old_to_new = {}
    new_idx = 1
    remaining_old_indices = []
    for i, s in enumerate(steps):
        if i not in merged_ids:
            remaining_old_indices.append(i)

    # The merged list order: merged steps first (from pair merging), then singles
    # We need to rebuild depends_on properly. Since merged steps combine deps
    # from their constituents and the plan may change shape, strip deps that
    # refer to now-merged steps (their work is in the combined step).
    # For simplicity, re-validate after merge.
    for i, s in enumerate(merged, 1):
        s.setdefault("depends_on", [])
        s.setdefault("verify", "")
        s.setdefault("environment", [])

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
        completed_ids = {
            s["id"] for s in state.get("steps", [])
            if s["status"] == "completed"
        }
        n = len(new_steps)
        new_step_nums = set(range(1, n + 1))
        for i, step in enumerate(new_steps):
            for dep in step.get("depends_on", []):
                if not isinstance(dep, int):
                    raise ValueError(
                        f"New step {i + 1} has non-integer dep: {dep}"
                    )
                # dep is valid if it references a completed step or
                # another step within the new list
                if dep not in completed_ids and dep not in new_step_nums:
                    raise ValueError(
                        f"New step {i + 1} references unknown step {dep}"
                    )
                # Self-reference check: only for deps within the new list
                # (not for deps referencing completed steps)
                if dep == i + 1 and dep not in completed_ids:
                    raise ValueError(
                        f"New step {i + 1} depends on itself."
                    )
        return new_steps
    except (ValueError, json.JSONDecodeError) as e:
        logger.warning("Could not parse re-planned steps: %s", e)
        return None


def enrich_step_descriptions(completed_step: dict,
                             dependent_steps: list[dict]) -> list[int]:
    """Enrich dependent step descriptions with info from a completed step.

    Appends concrete details about files produced, data formats, and
    summaries so downstream steps have better context. This is a
    lightweight operation with no LLM call.

    Returns list of step IDs that were enriched.

    Section 6c of PLAN.md.
    """
    enriched_ids = []
    files_written = completed_step.get("files_written", [])
    summary = completed_step.get("summary", "")
    uas_result = completed_step.get("uas_result", {})

    if not files_written and not summary:
        return enriched_ids

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

    if not parts:
        return enriched_ids

    enrichment = (
        f"\n[Context from step {completed_step['id']} "
        f"({completed_step['title']}): {'; '.join(parts)}]"
    )

    for dep_step in dependent_steps:
        # Avoid duplicate enrichment
        marker = f"[Context from step {completed_step['id']} "
        if marker in dep_step.get("description", ""):
            continue
        dep_step["description"] = dep_step["description"] + enrichment
        enriched_ids.append(dep_step["id"])

    return enriched_ids


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
