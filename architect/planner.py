"""LLM-based task decomposition into atomic steps."""

import json
import logging
import re

from orchestrator.llm_client import get_llm_client
from .events import EventType, get_event_log

logger = logging.getLogger(__name__)

DECOMPOSITION_PROMPT = """\
<instructions>
You are a task decomposition engine. Given a high-level goal, break it into \
atomic, independently executable steps that form a directed acyclic graph (DAG).

First, reason about the goal in <analysis> tags: assess its complexity, identify \
the key sub-problems, and determine appropriate granularity. Think thoroughly \
about dependencies and which steps can run in parallel.

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
Respond with your analysis in <analysis> tags, then ONLY a JSON array. Each element:
{{"title": "short name", \
"description": "detailed task for a code-generating LLM", \
"depends_on": [step_numbers], \
"verify": "how to verify this step succeeded beyond exit code 0", \
"environment": ["pip or apt packages needed, if any"]}}

Steps are numbered starting from 1. depends_on references must use 1-based step \
numbers (e.g. step 2 depending on step 1 should have "depends_on": [1]).
</output_format>

<examples>
Example 1 — Trivial (single step):
Goal: "Print the current date and time"
<analysis>This is a trivial single-action task requiring no external packages.</analysis>
[{{"title": "Print datetime", "description": "Write a Python script that prints the current date and time using the datetime module.", "depends_on": [], "verify": "stdout contains a date/time string", "environment": []}}]

Example 2 — Medium with dependencies:
Goal: "Download a CSV from a URL, clean it, and produce summary statistics"
<analysis>Three distinct phases: download, clean, analyze. Cleaning depends on download, analysis depends on cleaning. No parallelism possible since each step feeds the next.</analysis>
[
  {{"title": "Download CSV", "description": "Download the CSV file from the given URL using requests and save it to the workspace as raw_data.csv. Print the number of rows and columns.", "depends_on": [], "verify": "raw_data.csv exists in workspace and has >0 rows", "environment": ["requests"]}},
  {{"title": "Clean data", "description": "Read raw_data.csv from the workspace, handle missing values (drop rows with >50% nulls, fill numeric nulls with median), remove duplicates, and save as cleaned_data.csv. Print cleaning summary.", "depends_on": [1], "verify": "cleaned_data.csv exists and has fewer or equal rows to raw_data.csv", "environment": ["pandas"]}},
  {{"title": "Summary statistics", "description": "Read cleaned_data.csv, compute summary statistics (mean, median, std, min, max for numeric columns), and save results to summary.json and summary.txt. Print the summary.", "depends_on": [2], "verify": "summary.json and summary.txt exist in workspace", "environment": ["pandas"]}}
]

Example 3 — Complex with parallelism:
Goal: "Scrape product info from two websites and compare prices"
<analysis>Scraping each website is independent — these can run in parallel. Comparison depends on both. Three steps total, two parallel.</analysis>
[
  {{"title": "Scrape site A", "description": "Scrape product names and prices from site A using requests and BeautifulSoup. Save results as site_a_products.json in the workspace. Print count of products found.", "depends_on": [], "verify": "site_a_products.json exists and contains a non-empty list", "environment": ["requests", "beautifulsoup4"]}},
  {{"title": "Scrape site B", "description": "Scrape product names and prices from site B using requests and BeautifulSoup. Save results as site_b_products.json in the workspace. Print count of products found.", "depends_on": [], "verify": "site_b_products.json exists and contains a non-empty list", "environment": ["requests", "beautifulsoup4"]}},
  {{"title": "Compare prices", "description": "Read site_a_products.json and site_b_products.json from the workspace. Match products by name and compare prices. Save comparison to price_comparison.csv and print a summary of which site is cheaper on average.", "depends_on": [1, 2], "verify": "price_comparison.csv exists and contains matched products", "environment": ["pandas"]}}
]
</examples>

Goal: {goal}
"""


def parse_steps_json(response: str) -> list[dict]:
    # Strip <analysis> tags if present (LLM reasoning preamble)
    text = re.sub(r"<analysis>.*?</analysis>", "", response, flags=re.DOTALL).strip()

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
    client = get_llm_client()
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


CRITIQUE_PROMPT = """\
<instructions>
You are reviewing a task decomposition plan. Analyze the proposed steps and \
identify any issues. Be concise and actionable.
</instructions>

<goal>{goal}</goal>

<proposed_steps>
{steps_json}
</proposed_steps>

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
    client = get_llm_client()
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
<instructions>
You are diagnosing and fixing a failed code-generation task. Follow the structured \
reflection process below.
</instructions>

<original_task>{description}</original_task>

<failure_output>
<stdout>{stdout}</stdout>
<stderr>{stderr}</stderr>
</failure_output>
{escalation_instruction}
<process>
1. In <diagnosis> tags, analyze: What specifically went wrong? Why? Categorize the \
error as one of: dependency issue, logic error, environment problem, network issue, \
or data format mismatch. Determine whether the error originated in this step or \
propagated from a previous step.

2. In <strategies> tags, propose 2-3 alternative strategies to solve the task. \
Select the best one with justification. Ensure the chosen strategy follows best \
practices: use HTTPS URLs, pin dependency versions, use context managers for I/O, \
catch specific exceptions, use git init -b main for repos, and never hardcode secrets.

3. After the tags, provide ONLY the improved task description. Be more specific and \
explicit about what the Python code should do. Do not include any explanation.
</process>
"""

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
<instructions>
A code-generation task has failed multiple times. Break it down into 2-3 smaller, \
more manageable sub-phases that can be expressed as a single sequential script.
</instructions>

<failed_task>{description}</failed_task>

<failure_output>
<stdout>{stdout}</stdout>
<stderr>{stderr}</stderr>
</failure_output>

Rewrite the task as a detailed, step-by-step description that breaks the work into \
explicit sequential phases within a single script. Each phase should be simple enough \
to be unlikely to fail. Include explicit error handling between phases.

Provide ONLY the improved task description. No explanation.
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
                        escalation_level: int = 0) -> str:
    """Reflection-based task rewrite with progressive escalation.

    Uses structured diagnosis/strategy/rewrite phases instead of simple rewriting.
    Includes red-flagging: outputs showing signs of confusion are resampled once.
    """
    client = get_llm_client()

    stdout_trimmed = orchestrator_stdout
    stderr_trimmed = orchestrator_stderr

    escalation = ESCALATION_INSTRUCTIONS.get(escalation_level, "")

    prompt = REFLECT_PROMPT.format(
        description=step["description"],
        stdout=stdout_trimmed,
        stderr=stderr_trimmed,
        escalation_instruction=escalation,
    )

    event_log = get_event_log()
    event_log.emit(EventType.LLM_CALL_START,
                   data={"purpose": "reflect_and_rewrite",
                         "escalation_level": escalation_level})
    response = client.generate(prompt)
    event_log.emit(EventType.LLM_CALL_COMPLETE,
                   data={"purpose": "reflect_and_rewrite"})

    # Strip diagnosis and strategies tags to get just the description
    result = re.sub(r"<diagnosis>.*?</diagnosis>", "", response, flags=re.DOTALL)
    result = re.sub(r"<strategies>.*?</strategies>", "", result, flags=re.DOTALL)
    result = result.strip()

    # Red-flagging: check for signs of confusion and resample once
    if _is_confused_output(result, step["description"], stderr_trimmed):
        logger.warning("  Red-flag detected in rewrite output, resampling...")
        response = client.generate(prompt)
        result = re.sub(r"<diagnosis>.*?</diagnosis>", "", response, flags=re.DOTALL)
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


def decompose_failing_step(step: dict, orchestrator_stdout: str,
                           orchestrator_stderr: str) -> str:
    """Decompose a failing step into a more granular multi-phase description."""
    client = get_llm_client()

    stdout_trimmed = orchestrator_stdout
    stderr_trimmed = orchestrator_stderr

    prompt = DECOMPOSE_STEP_PROMPT.format(
        description=step["description"],
        stdout=stdout_trimmed,
        stderr=stderr_trimmed,
    )

    result = client.generate(prompt).strip()
    return result if result else step["description"]
