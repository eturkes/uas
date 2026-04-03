"""LLM-based task decomposition into atomic steps."""

import concurrent.futures
import json
import logging
import os
import re

from orchestrator.llm_client import get_llm_client
from .events import EventType, get_event_log
from hooks import HookEvent, load_hooks, run_hook

MAX_ERROR_LENGTH = int(os.environ.get("UAS_MAX_ERROR_LENGTH", "0"))
_DEFAULT_REWRITE_TRIM = 3000

logger = logging.getLogger(__name__)

MINIMAL_MODE = os.environ.get("UAS_MINIMAL", "").lower() in ("1", "true", "yes")


SPEC_GENERATION_PROMPT = """\
<goal>{goal}</goal>
{research_context}
<instructions>
You are a technical architect. Given the goal above, produce a structured \
project specification that will guide an autonomous coding agent through \
implementation.

Scale the depth to the project's complexity:
- For simple projects (1-3 components), keep sections brief.
- For complex projects (many components, data pipelines, UIs), be thorough.

Write the spec as a Markdown document with these sections:
</instructions>

<format>
# Project Specification

## 1. Overview
One paragraph: what this project is and what problem it solves.

## 2. Goals
Bulleted list of concrete, verifiable outcomes. What MUST be true when the \
project is complete. Each goal should be testable.

## 3. Non-Goals
What is explicitly out of scope. This prevents the implementing agent from \
gold-plating or adding unrequested features. If the goal is narrow, a single \
bullet like "- Production deployment" is fine.

## 4. Architecture
Key components, their responsibilities, and how they interact. For simple \
projects, this can be one sentence ("Single Python script"). For complex \
projects, describe the component graph and data flow.

## 5. Data Model
Core data structures, file formats, and schemas that components must agree on. \
Specify exact column names, JSON keys, and types where relevant. This is the \
contract between producer and consumer components — ambiguity here causes \
integration failures.

## 6. Interface Contracts
How components communicate: file paths, API endpoints, function signatures, \
CLI arguments. Specify the exact names and formats so that independently-built \
components are compatible. For simple projects with no inter-component \
boundaries, write "N/A — single component."

## 7. Acceptance Criteria
Specific, testable conditions for project completion. Not just "it works" — \
measurable outcomes. These will be used to validate the final result.

## 8. Constraints
Required tools, libraries, environment requirements, and technical boundaries. \
Include specific version requirements if known. Prefer modern, best-in-class \
tools over legacy defaults.
</format>

<rules>
- Write a SPECIFICATION, not a description of a finished product.
- Be precise about data formats, file names, and interfaces — vagueness here \
  causes downstream failures.
- Every goal in section 2 must have a corresponding acceptance criterion in \
  section 7.
- Respond with ONLY the Markdown specification. No preamble or explanation.
</rules>
"""


def generate_project_spec(
    goal: str,
    research_context: str = "",
    complexity: str = "medium",
) -> str:
    """Generate a structured project specification from a goal.

    Produces a Markdown document with sections for goals, non-goals,
    architecture, data model, interface contracts, acceptance criteria,
    and constraints.  For trivial goals (single-action tasks), returns
    an empty string to skip the spec overhead.

    Args:
        goal: The user's goal text.
        research_context: Pre-computed research findings to incorporate.
        complexity: Estimated complexity (trivial/simple/medium/complex).

    Returns:
        The specification as a Markdown string, or empty string for
        trivial goals.
    """
    if complexity == "trivial":
        logger.info("  Skipping spec generation for trivial goal.")
        return ""

    client = get_llm_client(role="planner")
    rc_section = ""
    if research_context:
        rc_section = (
            f"\n<research_findings>\n{research_context}\n</research_findings>\n"
        )
    prompt = SPEC_GENERATION_PROMPT.format(
        goal=goal, research_context=rc_section,
    )

    event_log = get_event_log()
    event_log.emit(EventType.LLM_CALL_START, data={"purpose": "generate_spec"})
    try:
        spec = client.generate(prompt, stream=True)
        event_log.emit(
            EventType.LLM_CALL_COMPLETE, data={"purpose": "generate_spec"},
        )
        return spec.strip() if spec.strip() else ""
    except Exception as e:
        logger.warning("Spec generation failed, continuing without spec: %s", e)
        return ""


RESEARCH_PROMPT = """\
<goal>{goal}</goal>

<instructions>
Before planning implementation, research this domain. Use web search to find \
current best practices, relevant standards, and authoritative sources.

Return a structured research summary:
1. **Key findings**: Current best practices, standards, or established \
approaches for this type of task.
2. **Recommended libraries/tools**: What are the current best-in-class \
libraries, frameworks, and tools for this task? Actively look for modern \
replacements that have superseded older defaults — the ecosystem evolves fast \
and what was standard two years ago may now be obsolete. Include current \
version numbers when known.
3. **Tooling & infrastructure**: What are the current best practices for \
project structure, dependency management, testing, linting, and formatting \
in this ecosystem? Prefer the latest widely-adopted tools over legacy ones.
4. **Common pitfalls**: Known failure modes or anti-patterns to avoid, \
including use of deprecated libraries or outdated approaches.
5. **Citations**: URLs or reference names for sources consulted.

Be concise and actionable. Focus on information that directly informs \
implementation decisions. If the domain is straightforward and well-understood, \
say "No additional research needed" and briefly explain why.
</instructions>
"""


def research_goal(goal: str) -> str:
    """Perform domain research before planning implementation.

    Sends the goal to the planner LLM with a research-specific prompt
    to gather best practices, standards, and authoritative sources.
    Returns a structured research summary, or empty string on failure.
    """
    client = get_llm_client(role="planner")
    prompt = RESEARCH_PROMPT.format(goal=goal)
    event_log = get_event_log()
    event_log.emit(EventType.LLM_CALL_START, data={"purpose": "research_goal"})
    try:
        result = client.generate(prompt, stream=True)
        event_log.emit(
            EventType.LLM_CALL_COMPLETE, data={"purpose": "research_goal"}
        )
        return result.strip() if result.strip() else ""
    except Exception as e:
        logger.warning(
            "Research phase failed, continuing without research: %s", e
        )
        return ""


DECOMPOSITION_PROMPT = """\
<research>
You have full network access. If the goal involves:
- An external API: Check its current documentation for endpoints and auth methods
- A library you're unsure about: Verify it exists in the relevant package registry and check its current version
- A domain you're unfamiliar with: Look up best practices and common approaches
- Any technology choice: Research what the current best-in-class tool is — the
  ecosystem evolves fast, and legacy defaults may have been superseded by faster,
  better-maintained alternatives

Use what you learn to make your decomposition more specific and accurate.
Don't guess at API formats or library capabilities — verify when uncertain.
Always specify the most modern, widely-adopted tools and libraries for the job.
</research>
{spec}
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
[{{"title": "Print datetime", "description": "Write a Python script that prints the current date and time using the datetime module.", "depends_on": [], "verify": "stdout contains a date/time string", "environment": [], "outputs": []}}]

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
  {{"title": "Download CSV", "description": "Download the CSV file from the given URL using requests and save it to the workspace as raw_data.csv. Validate the response is valid CSV (not HTML error page). Print the number of rows and columns.", "depends_on": [], "verify": "raw_data.csv exists in workspace, has >0 rows, and stdout prints row/column counts", "environment": ["requests"], "outputs": ["raw_data.csv"]}},
  {{"title": "Clean data", "description": "Read raw_data.csv from the workspace, handle missing values (drop rows with >50% nulls, fill numeric nulls with median), remove duplicates, and save as cleaned_data.csv. Print cleaning summary showing rows before, rows dropped, rows remaining.", "depends_on": [1], "verify": "cleaned_data.csv exists, row count <= raw_data.csv row count, stdout shows before/after row counts and number of nulls filled", "environment": ["pandas"], "outputs": ["cleaned_data.csv"]}},
  {{"title": "Summary statistics", "description": "Read cleaned_data.csv, compute summary statistics (mean, median, std, min, max for numeric columns), and save results to summary.json and summary.txt. Print the summary table to stdout.", "depends_on": [2], "verify": "summary.json contains keys for each numeric column with mean/median/std/min/max values; summary.txt is human-readable; stdout shows the statistics table", "environment": ["pandas"], "outputs": ["summary.json", "summary.txt"]}}
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
  {{"title": "Scrape site A", "description": "Scrape product names and prices from site A using requests and BeautifulSoup. Save results as site_a_products.json in the workspace. Print count of products found.", "depends_on": [], "verify": "site_a_products.json exists and contains a non-empty list", "environment": ["requests", "beautifulsoup4"], "outputs": ["site_a_products.json"]}},
  {{"title": "Scrape site B", "description": "Scrape product names and prices from site B using requests and BeautifulSoup. Save results as site_b_products.json in the workspace. Print count of products found.", "depends_on": [], "verify": "site_b_products.json exists and contains a non-empty list", "environment": ["requests", "beautifulsoup4"], "outputs": ["site_b_products.json"]}},
  {{"title": "Compare prices", "description": "Read site_a_products.json and site_b_products.json from the workspace. Match products by name and compare prices. Save comparison to price_comparison.csv and print a summary of which site is cheaper on average.", "depends_on": [1, 2], "verify": "price_comparison.csv exists and contains matched products", "environment": ["pandas"], "outputs": ["price_comparison.csv"]}}
]

Example 4 — Complex multi-phase project:
Goal: "Build an e-commerce analytics platform with data ingestion, predictive modeling, \
explainability, customer segmentation, and a multi-tab interactive dashboard with \
bilingual support"
<analysis>
Complexity: complex (12+ steps). Three major phases: data preparation, modeling/analysis, \
and dashboard/visualization. Each phase has multiple distinct deliverables. Creation and \
integration must be separated. Integration checkpoints needed between phases.
Sub-problems: realistic data simulation, feature engineering, model training, SHAP \
explainability, customer clustering, 4 dashboard tabs, bilingual i18n.
Risk areas: data leakage in modeling, SHAP compatibility, dashboard tab wiring, \
translation coverage.
Parallelization: within each phase, independent modules can be built in parallel \
(e.g., model training and segmentation can be independent; dashboard tabs can \
be built in parallel after data is ready).
Failure modes: model training references wrong columns, SHAP fails on model type, \
dashboard tabs render empty, translation keys missing.
</analysis>
<complexity_assessment>complex — 12 steps across 3 phases with integration checkpoints</complexity_assessment>
[
  {{"title": "Data simulator", "description": "Create a data simulation module that generates realistic e-commerce transaction data matching the specification. Save simulated data as data/transactions.csv with columns: customer_id, age, region, product_category, channel, first_purchase_value, purchase_count_month3, purchase_count_month6, churned. Print row count and column summary.", "depends_on": [], "verify": "data/transactions.csv has >100 rows, all specified columns present, no nulls in required fields, value ranges are plausible", "environment": ["pandas", "numpy"], "outputs": ["data/transactions.csv"]}},
  {{"title": "Data cleaning pipeline", "description": "Create a cleaning module that reads data/transactions.csv, validates data types, handles missing values, creates derived features (growth_rate, lifetime_value), and saves cleaned_data.csv. Print cleaning report with before/after row counts.", "depends_on": [1], "verify": "cleaned_data.csv exists, derived columns present, no unexpected nulls, row count logged", "environment": ["pandas"], "outputs": ["cleaned_data.csv"]}},
  {{"title": "Bilingual translation strings", "description": "Create a translations module with all UI strings in English and Japanese. Export as translations.json with structure {{\"en\": {{...}}, \"ja\": {{...}}}}. Include labels for all dashboard elements, column display names, and status descriptions.", "depends_on": [], "verify": "translations.json has en and ja keys with identical key sets, no empty values", "environment": [], "outputs": ["translations.json"]}},
  {{"title": "Phase 1 integration checkpoint", "description": "Validate that the data simulator, cleaning pipeline, and translations module work together. Import each module, run the pipeline end-to-end, verify cleaned_data.csv columns match translation keys, print interface summary.", "depends_on": [1, 2, 3], "verify": "all imports succeed, pipeline runs without errors, column-translation alignment verified", "environment": ["pandas"], "outputs": []}},
  {{"title": "Predictive model training", "description": "Train an XGBoost model to predict churned from first-interaction features ONLY (age, region, product_category, channel, first_purchase_value). Save trained model to models/xgb_model.joblib and metrics to models/metrics.json. Print accuracy, F1, and confusion matrix.", "depends_on": [4], "verify": "model file exists, metrics.json shows accuracy > majority-class baseline, confusion matrix is not degenerate", "environment": ["xgboost", "scikit-learn", "joblib"], "outputs": ["models/xgb_model.joblib", "models/metrics.json"]}},
  {{"title": "SHAP explainability", "description": "Load models/xgb_model.joblib and compute SHAP values for the test set. Save SHAP summary plot as outputs/shap_summary.png and feature importance as outputs/shap_importance.json. Print top 5 features.", "depends_on": [5], "verify": "shap_summary.png exists and is >1KB, shap_importance.json has entries for all features", "environment": ["shap", "matplotlib"], "outputs": ["outputs/shap_summary.png", "outputs/shap_importance.json"]}},
  {{"title": "Customer segmentation", "description": "Perform clustering on cleaned_data.csv to discover customer segments with distinct behaviour patterns. Save segment assignments to outputs/segments.csv and profiles to outputs/segment_profiles.json. Print segment sizes and key characteristics.", "depends_on": [4], "verify": "segments.csv has a segment column with 2-5 distinct values, profiles describe each segment", "environment": ["scikit-learn", "pandas"], "outputs": ["outputs/segments.csv", "outputs/segment_profiles.json"]}},
  {{"title": "Phase 2 integration checkpoint", "description": "Validate that model, SHAP, and segmentation outputs are compatible. Load all artifacts, verify SHAP features match model features, verify segment IDs align with customer IDs in cleaned data. Print summary.", "depends_on": [5, 6, 7], "verify": "all artifacts load, feature alignment verified, no orphaned customer IDs", "environment": ["joblib", "pandas"], "outputs": []}},
  {{"title": "Dashboard tab: cohort overview", "description": "Create the cohort overview tab showing customer demographics, channel distribution, and outcome summary charts. Read from cleaned_data.csv and translations.json. Save as src/tab_overview.py.", "depends_on": [8], "verify": "tab_overview.py imports successfully, renders without errors when called with test data", "environment": ["plotly", "dash"], "outputs": ["src/tab_overview.py"]}},
  {{"title": "Dashboard tab: customer simulator", "description": "Create the customer simulator tab allowing users to input customer features and see predicted outcomes using the trained model. Read model from models/xgb_model.joblib. Save as src/tab_simulator.py.", "depends_on": [8], "verify": "tab_simulator.py imports and renders, prediction returns valid probability", "environment": ["plotly", "dash", "joblib"], "outputs": ["src/tab_simulator.py"]}},
  {{"title": "Dashboard tab: insight engine", "description": "Create the insight engine tab displaying SHAP explanations and segment profiles. Read from outputs/shap_importance.json and outputs/segment_profiles.json. Save as src/tab_insights.py.", "depends_on": [8], "verify": "tab_insights.py imports and renders, shows SHAP and segment data", "environment": ["plotly", "dash"], "outputs": ["src/tab_insights.py"]}},
  {{"title": "Dashboard assembly and bilingual toggle", "description": "Assemble all tabs into a unified Dash application with bilingual language toggle. Import tab_overview, tab_simulator, tab_insights, and translations. Save as app.py. Print startup confirmation.", "depends_on": [9, 10, 11, 3], "verify": "app.py starts without import errors, all 3 tabs render, language toggle switches strings", "environment": ["dash"], "outputs": ["app.py"]}}
]
</examples>

<anti_patterns>
Common decomposition mistakes to avoid:
- Over-splitting trivial tasks: a single package install + script does not need 3 separate steps
- Under-splitting complex tasks: a step that does "download, parse, transform, analyze, \
and visualize" should be broken down. A step that requires model training AND \
explainability AND visualization is too large — split them into separate steps \
that save/load intermediate artifacts (e.g., save the trained model with joblib, \
then a separate step loads it and computes SHAP values)
- Overloading steps: A step that requires model training AND statistical \
testing AND visualization will fail to do all three well. Split into \
separate steps: one trains and saves the model, one loads the model and \
runs statistical analyses, one generates visualizations. Each step should \
have ONE primary responsibility.
- The 250-line limit is real: if a step description contains more than 3 \
distinct deliverables, it MUST be split. Count deliverables explicitly in \
your analysis.
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
- Coupling creation and integration: NEVER have a single step that both \
creates a new module AND modifies an existing one to import/use it. \
Split into two steps: (1) create the module with its own tests/verification, \
(2) integrate it into the existing codebase. This is the #1 cause of \
rewrite failures.
- Assuming knowledge instead of verifying: don't assume specific API endpoints, library \
interfaces, or data formats are current — they may have changed. \
BAD: "Use the Twitter API v2 endpoint /tweets/search/recent" (may be outdated) \
GOOD: "Query the Twitter/X API documentation to find the current search endpoint, then implement"
- Data leakage in predictive modeling: when a step trains a model to predict \
an outcome (e.g., churn, final score), it must ONLY use features available at \
prediction time (e.g., baseline features). Using outcome-time measurements \
to predict outcome-time targets is data leakage. The step description must \
explicitly state which features are allowed and why. The verify criteria must \
check that no future-time features are included.
</anti_patterns>

<verification_guidelines>
Write verification criteria that test CORRECTNESS, not just EXISTENCE:
- For data steps: verify row counts, column types, and value ranges match
  expectations. Check for unexpected 100% NaN columns.
- For modeling steps: verify model outperforms a trivial baseline (majority
  class, mean prediction). If it doesn't, the step has FAILED.
- For analysis steps: verify each claimed analysis actually appears in the
  output (not just that the output file exists).
- For integration steps: verify the output works in a clean environment,
  not just the current sandbox.
Anti-pattern: "file exists and is non-empty" -- this catches nothing.
Good pattern: "model_metrics.json accuracy > baseline_accuracy AND all
per_class_f1 values are defined AND confusion matrix is not degenerate
(predicts more than one class)"
</verification_guidelines>

<expert_approach>
## How an Expert Would Approach This
Think like a senior engineer planning this project:

- Always use the latest, best-in-class tools — not legacy defaults. The
  ecosystem evolves fast. Before specifying a library, framework, or tool in a
  step description, consider whether a more modern, faster, or better-maintained
  alternative exists. If you're unsure, instruct the step to research current
  best practices before implementing. This applies to everything: package
  managers, linters, frameworks, data libraries, HTTP clients, ORMs, etc.
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
- For predictive modeling tasks, always specify the temporal boundary: which
  data is available at prediction time vs. which data is the target. Instruct
  the code to explicitly filter features by this boundary.
</expert_approach>

<instructions>
You are a task decomposition engine. Given the goal above, break it into \
atomic, independently executable steps that form a directed acyclic graph (DAG).

If a <project_spec> is provided, use it as your primary reference. The spec's \
architecture, data model, and interface contracts define the boundaries between \
steps. Each step should map to one component or responsibility from the spec. \
Ensure step descriptions use the exact names, formats, and contracts from the \
spec — do not invent alternatives.

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
This directory IS the project root — write files directly here using \
os.path.join(workspace, ...). Do NOT create a nested project subdirectory. \
Later steps can read files written by earlier steps from this directory. \
NEVER use hardcoded absolute paths (like /uas/..., /home/..., /tmp/...) \
in step descriptions. Files written outside the workspace are lost after execution.
3. Each step should be as small and focused as possible — the smaller the \
subtask, the more reliable the execution. Each step is implemented as a \
single Python script; the script MUST stay under ~250 lines so the code \
generator can produce it in one pass without truncation. If a sub-task \
would clearly require a longer script, split it into multiple steps that \
save/load intermediate artifacts via the shared workspace.
4. Steps can run in parallel when they have no dependency relationship. \
Maximize parallelism by making steps independent whenever possible.
5. Scale the number of steps to the goal's complexity: \
1 step for trivial tasks, 2-3 for simple, 5-10 for medium, 10-20 for complex. \
Prefer more, smaller steps over fewer, larger ones.
6. The execution environment has full unrestricted network access and complete \
autonomy. Install any packages, runtimes, or tools needed without hesitation.
7. Each step must produce observable output to stdout so downstream steps \
can use the results.
8. Do NOT create steps that require user interaction.
9. Do NOT include any steps that run `git init` or other git commands — version \
control is managed automatically by the framework. Focus steps on the actual work.
10. All projects must include a README.md and a dependency manifest with pinned versions \
(using the format standard for the target ecosystem).
11. Never hardcode secrets or API keys in step descriptions — instruct the code \
to read them from environment variables.
12. Always prefer HTTPS URLs. Pin dependency versions.
13. When a step produces structured data (CSV, JSON, database) that other steps consume, \
the step description MUST explicitly state the column names, key names, or schema. \
Consumer steps MUST reference the exact names from the producer step — never invent \
aliases. If a data-loading/preprocessing step sits between a producer and consumers, \
its description must specify the exact mapping (e.g., "rename column SEX to Sex"). \
Instruct consumer steps to read the actual file headers at runtime rather than \
hardcoding assumed column names.
14. Use consistent directory names across ALL steps. When a step creates a subdirectory \
(e.g., "outputs/"), every subsequent step that reads or writes output files MUST use \
the exact same directory name — never introduce synonyms like "output/", "results/", \
or "out/". Specify the directory name explicitly in step descriptions so the code \
generator uses the same path. Prefer a single well-named directory for each purpose \
(e.g., "data/" for datasets, "models/" for trained models, "outputs/" for results).
15. Every step MUST include an "outputs" field listing every file path (relative to the \
workspace) that the step creates or modifies. Use glob patterns for dynamic filenames \
(e.g., "data/*.csv"). Steps without depends_on edges but with overlapping outputs will \
be serialized to prevent data races, so declaring outputs accurately is important.
</rules>

<output_format>
Respond with <analysis> tags, then <complexity_assessment> tags, then ONLY a JSON \
array. Each element:
{{"title": "short name", \
"description": "detailed task for a code-generating LLM", \
"depends_on": [step_numbers], \
"verify": "how to verify this step succeeded beyond exit code 0", \
"environment": ["packages needed, if any"], \
"outputs": ["file paths/globs this step creates or modifies, relative to workspace"]}}

Steps are numbered starting from 1. depends_on references must use 1-based step \
numbers (e.g. step 2 depending on step 1 should have "depends_on": [1]).
The "outputs" field lists every file the step writes or modifies. Use glob patterns \
for dynamic filenames (e.g., "data/*.csv"). Steps with overlapping outputs will be \
serialized even if they have no depends_on edge, so accurate outputs are critical.
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


def _format_spec(spec: str) -> str:
    """Wrap project specification in XML tags for the decomposition prompt."""
    if not spec:
        return ""
    return (
        f"\n<project_spec>\n{spec}\n</project_spec>\n"
    )


def decompose_goal(goal: str, spec: str = "",
                    hooks: list | None = None) -> list[dict]:
    _hooks = hooks or []

    # Section 8: PRE_PLAN hook
    if _hooks:
        hook_result = run_hook(HookEvent.PRE_PLAN, {
            "goal": goal,
            "spec": spec[:500] if spec else "",
        }, _hooks)
        if hook_result and hook_result.get("abort"):
            raise ValueError(
                f"PRE_PLAN hook aborted: {hook_result.get('reason', 'no reason')}"
            )

    client = get_llm_client(role="planner")
    prompt = DECOMPOSITION_PROMPT.format(
        goal=goal,
        spec=_format_spec(spec),
    )
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
        step.setdefault("outputs", [])

    # Normalize 0-indexed depends_on to 1-indexed.
    # The LLM sometimes returns 0-based step references despite the prompt
    # requesting 1-based. Detect this by checking if any step references
    # step 0 (which doesn't exist in 1-based numbering).
    has_zero_ref = any(0 in s.get("depends_on", []) for s in steps)
    if has_zero_ref:
        for step in steps:
            step["depends_on"] = [d + 1 for d in step["depends_on"]]

    validate_depends_on(steps)

    # Section 8: POST_PLAN hook — may modify the step list
    if _hooks:
        hook_result = run_hook(HookEvent.POST_PLAN, {
            "goal": goal,
            "steps": steps,
        }, _hooks)
        if hook_result and "steps" in hook_result:
            logger.info("  POST_PLAN hook overrode step list.")
            steps = hook_result["steps"]

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


# ---------------------------------------------------------------------------
# Section 7 — Decomposition depth scaling for complex goals
# ---------------------------------------------------------------------------

# Minimum step counts by complexity category.
MINIMUM_STEPS = {
    "trivial": 1,
    "simple": 2,
    "medium": 4,
    "complex": 8,
}

# Maximum distinct deliverables per step before flagging for a split.
MAX_DELIVERABLES_PER_STEP = 3

# Patterns that indicate a deliverable in a step description.
_DELIVERABLE_PATTERNS = re.compile(
    r"\b(?:create|write|build|generate|produce|save|train|implement|export)\s+"
    r"(?:a\s+|the\s+|an\s+)?(\S+)",
    re.IGNORECASE,
)

# Patterns for distinct output files referenced in a description.
_FILE_OUTPUT_PATTERN = re.compile(
    r"(?:save\s+(?:as|to|into)|write\s+(?:to|into)|export\s+(?:as|to))\s+"
    r"['\"]?(\S+\.\w{1,5})['\"]?",
    re.IGNORECASE,
)


def count_step_deliverables(step: dict) -> int:
    """Count the number of distinct deliverables in a step description.

    Section 7c of PLAN.md. Uses heuristics to count output files mentioned
    and creation verbs to estimate deliverable count. Returns the higher of
    the two counts (file outputs vs. creation actions).
    """
    desc = step.get("description", "")
    if not desc:
        return 0

    # Count distinct output files
    file_matches = set(_FILE_OUTPUT_PATTERN.findall(desc))

    # Count creation-verb targets, deduplicating by the target noun
    action_matches = set(_DELIVERABLE_PATTERNS.findall(desc))

    return max(len(file_matches), len(action_matches))


def flag_overloaded_steps(steps: list[dict]) -> list[int]:
    """Return 0-based indices of steps with more than MAX_DELIVERABLES_PER_STEP deliverables.

    Section 7c of PLAN.md. These steps should be split into smaller pieces.
    """
    overloaded = []
    for i, step in enumerate(steps):
        count = count_step_deliverables(step)
        if count > MAX_DELIVERABLES_PER_STEP:
            logger.info(
                "  Step %d ('%s') has %d deliverables (max %d) — flagged for splitting.",
                i + 1, step.get("title", "Untitled"), count, MAX_DELIVERABLES_PER_STEP,
            )
            overloaded.append(i)
    return overloaded


REDECOMPOSE_PROMPT = """\
<goal>{goal}</goal>

<current_plan>
{plan_json}
</current_plan>

<instructions>
The current plan has only {num_steps} steps, but this goal is classified as \
{complexity} complexity and requires at least {min_steps} steps. The plan \
under-decomposes the goal.

{overloaded_note}

Re-decompose the goal into at least {min_steps} steps. Follow these rules:
- Each step must have ONE primary responsibility
- No step should have more than 3 distinct deliverables
- Separate creation from integration (never create a module and wire it in the same step)
- Mark phase boundaries with integration checkpoint steps

Return the same JSON array format as the original plan.
</instructions>
"""


def enforce_minimum_steps(
    goal: str,
    steps: list[dict],
    complexity: str,
    research_context: str = "",
) -> list[dict]:
    """Re-prompt if the plan has fewer steps than the complexity minimum.

    Section 7a of PLAN.md. If the initial decomposition returns fewer steps
    than MINIMUM_STEPS[complexity], asks the LLM to re-decompose with an
    explicit minimum. Also flags overloaded steps (>3 deliverables) in the
    re-prompt. Returns the original steps if already sufficient or on failure.
    """
    min_steps = MINIMUM_STEPS.get(complexity, 4)
    overloaded = flag_overloaded_steps(steps)

    if len(steps) >= min_steps and not overloaded:
        return steps

    reason_parts = []
    if len(steps) < min_steps:
        reason_parts.append(
            f"plan has {len(steps)} steps but minimum is {min_steps}"
        )
    if overloaded:
        reason_parts.append(
            f"steps {', '.join(str(i + 1) for i in overloaded)} have >3 deliverables"
        )
    logger.info(
        "  Enforcing minimum steps: %s. Re-decomposing...",
        "; ".join(reason_parts),
    )

    overloaded_note = ""
    if overloaded:
        overloaded_descs = []
        for idx in overloaded:
            s = steps[idx]
            overloaded_descs.append(
                f"- Step {idx + 1} ('{s.get('title', '')}') has too many "
                f"deliverables and should be split."
            )
        overloaded_note = (
            "The following steps are overloaded (>3 deliverables each):\n"
            + "\n".join(overloaded_descs)
        )

    plan_json = json.dumps(
        [{"step": i + 1, "title": s.get("title", ""), "description": s.get("description", "")}
         for i, s in enumerate(steps)],
        indent=2,
    )

    prompt = REDECOMPOSE_PROMPT.format(
        goal=goal,
        plan_json=plan_json,
        num_steps=len(steps),
        complexity=complexity,
        min_steps=min_steps,
        overloaded_note=overloaded_note,
    )

    event_log = get_event_log()
    client = get_llm_client(role="planner")
    event_log.emit(EventType.LLM_CALL_START,
                   data={"purpose": "enforce_minimum_steps"})
    try:
        response = client.generate(prompt)
    except Exception as e:
        logger.warning("  Re-decomposition failed: %s — keeping original plan.", e)
        return steps
    event_log.emit(EventType.LLM_CALL_COMPLETE,
                   data={"purpose": "enforce_minimum_steps"})

    try:
        new_steps = parse_steps_json(response)
    except ValueError:
        logger.warning("  Could not parse re-decomposed steps — keeping original plan.")
        return steps

    if not new_steps or len(new_steps) < len(steps):
        logger.warning(
            "  Re-decomposition produced %d steps (need >= %d) — keeping original.",
            len(new_steps) if new_steps else 0, min_steps,
        )
        return steps

    # Fill defaults
    for step in new_steps:
        step.setdefault("depends_on", [])
        step.setdefault("verify", "")
        step.setdefault("environment", [])
        step.setdefault("outputs", [])

    # Normalize 0-indexed depends_on
    has_zero_ref = any(0 in s.get("depends_on", []) for s in new_steps)
    if has_zero_ref:
        for step in new_steps:
            step["depends_on"] = [d + 1 for d in step["depends_on"]]

    try:
        validate_depends_on(new_steps)
    except ValueError as e:
        logger.warning("  Re-decomposed plan has invalid deps: %s — keeping original.", e)
        return steps

    logger.info(
        "  Re-decomposition: %d → %d steps.",
        len(steps), len(new_steps),
    )
    return new_steps


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


def decompose_goal_with_voting(
    goal: str,
    n_samples: int = 3,
    spec: str = "",
    complexity: str | None = None,
    hooks: list | None = None,
) -> list[dict]:
    """Generate multiple decomposition plans and select the best one.

    Uses a complexity gate: trivial/simple goals skip voting entirely.
    Medium/complex goals generate n_samples plans in parallel and pick
    the highest-scoring one.

    Args:
        spec: Structured project specification to guide decomposition.
            Empty string means no spec (trivial goals).
        complexity: Pre-computed complexity estimate. If None, will be
            estimated via LLM call.
        hooks: Hook configurations loaded from .uas/hooks.toml.

    Returns (steps, complexity) tuple-style via the steps list, with the
    estimated complexity stored in the module-level for the caller to read.
    """
    _hooks = hooks or []
    event_log = get_event_log()

    # 2c: Complexity estimation gate
    if complexity is None:
        complexity = estimate_complexity(goal)
    event_log.emit(EventType.COMPLEXITY_ESTIMATE, data={"complexity": complexity})
    logger.info("  Estimated complexity: %s", complexity)

    # Store for caller access
    decompose_goal_with_voting.last_complexity = complexity

    if complexity in ("trivial", "simple"):
        logger.info("  Skipping voting for %s goal, using single decomposition.", complexity)
        return decompose_goal(goal, spec=spec, hooks=_hooks)

    # Section 8: PRE_PLAN hook (for voting path)
    if _hooks:
        hook_result = run_hook(HookEvent.PRE_PLAN, {
            "goal": goal,
            "spec": spec[:500] if spec else "",
            "complexity": complexity,
        }, _hooks)
        if hook_result and hook_result.get("abort"):
            raise ValueError(
                f"PRE_PLAN hook aborted: {hook_result.get('reason', 'no reason')}"
            )

    # 2a: Generate N plans in parallel
    logger.info("  Generating %d plans for voting...", n_samples)

    spec_formatted = _format_spec(spec)

    def _generate_plan(suffix_idx: int) -> list[dict] | None:
        """Generate a single plan variant. Returns None on failure."""
        try:
            client = get_llm_client(role="planner")
            suffix = _VOTING_SUFFIXES[suffix_idx] if suffix_idx < len(_VOTING_SUFFIXES) else ""
            prompt = DECOMPOSITION_PROMPT.format(
                goal=goal, spec=spec_formatted,
            ) + suffix
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
        return decompose_goal(goal, spec=spec, hooks=_hooks)

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

    # Section 8: POST_PLAN hook — may modify the step list
    if _hooks:
        hook_result = run_hook(HookEvent.POST_PLAN, {
            "goal": goal,
            "steps": best_plan,
        }, _hooks)
        if hook_result and "steps" in hook_result:
            logger.info("  POST_PLAN hook overrode step list.")
            best_plan = hook_result["steps"]

    return best_plan


# Initialize the attribute for complexity storage
decompose_goal_with_voting.last_complexity = None


# ---------------------------------------------------------------------------
# Section 1 — Goal-coverage matrix
# ---------------------------------------------------------------------------

EXTRACT_REQUIREMENTS_PROMPT = """\
<goal>{goal}</goal>

<instructions>
Extract every discrete, testable requirement from the goal above. Each \
requirement should be a single deliverable or capability that the final output \
must include. Be exhaustive — if the goal mentions it, list it.

Guidelines:
- Split compound requirements (e.g., "build a dashboard with 3 tabs" → one \
requirement per tab).
- Include non-functional requirements (bilingual support, performance targets) \
only if they are explicitly stated.
- Keep each requirement to one short sentence.

Return ONLY a JSON array of strings, e.g.:
["data simulator from spec", "cleaning pipeline", "XGBoost predictive model"]
</instructions>
"""

VERIFY_COVERAGE_PROMPT = """\
<requirements>
{requirements_json}
</requirements>

<steps>
{steps_json}
</steps>

<instructions>
For each requirement, determine whether at least one step in the plan \
addresses it. A step "covers" a requirement if completing that step would \
deliver or substantially contribute to the requirement.

Return ONLY a JSON array of objects:
[{{"requirement": "...", "covered": true/false, "covering_steps": [step_numbers]}}]

Use 1-based step numbers matching the step list order.
</instructions>
"""

FILL_GAPS_PROMPT = """\
<goal>{goal}</goal>

<uncovered_requirements>
{uncovered_json}
</uncovered_requirements>

<existing_steps>
{steps_json}
</existing_steps>

<instructions>
The existing plan is missing coverage for the requirements listed above. \
Generate new steps to fill these gaps. Each new step must:
1. Address one or more of the uncovered requirements.
2. Be a self-contained Python script task.
3. Have correct depends_on references (1-based, referencing existing step \
numbers or other new steps).
4. NOT duplicate work already covered by existing steps.

Number the new steps starting from {next_step_number}.

Return ONLY a JSON array of step objects:
[{{"title": "...", "description": "...", "depends_on": [...], \
"verify": "...", "environment": [...], "outputs": [...]}}]
</instructions>
"""


def extract_requirements(goal: str) -> list[str]:
    """Extract atomic requirements from a goal using LLM.

    Section 1a of PLAN.md.
    """
    client = get_llm_client(role="planner")
    prompt = EXTRACT_REQUIREMENTS_PROMPT.format(goal=goal)
    event_log = get_event_log()
    event_log.emit(EventType.LLM_CALL_START,
                   data={"purpose": "extract_requirements"})
    try:
        response = client.generate(prompt)
    except Exception as e:
        logger.warning("Requirement extraction failed: %s", e)
        return []
    event_log.emit(EventType.LLM_CALL_COMPLETE,
                   data={"purpose": "extract_requirements"})

    # Parse JSON array of strings
    try:
        # Strip markdown fences if present
        text = response.strip()
        if text.startswith("```"):
            text = re.sub(r"^```\w*\n?", "", text)
            text = re.sub(r"\n?```\s*$", "", text)
        reqs = json.loads(text)
        if isinstance(reqs, list) and all(isinstance(r, str) for r in reqs):
            return reqs
    except (json.JSONDecodeError, ValueError):
        pass
    # Fallback: try to find a JSON array in the response
    match = re.search(r"\[.*\]", response, re.DOTALL)
    if match:
        try:
            reqs = json.loads(match.group())
            if isinstance(reqs, list) and all(isinstance(r, str) for r in reqs):
                return reqs
        except (json.JSONDecodeError, ValueError):
            pass
    logger.warning("Could not parse requirements from LLM response.")
    return []


def verify_coverage(
    requirements: list[str],
    steps: list[dict],
) -> list[dict]:
    """Check which requirements are covered by steps.

    Returns a list of dicts with keys: requirement, covered, covering_steps.
    Section 1b of PLAN.md.
    """
    if not requirements:
        return []

    client = get_llm_client(role="planner")
    steps_for_prompt = [
        {"step_number": i + 1, "title": s["title"],
         "description": s["description"]}
        for i, s in enumerate(steps)
    ]
    prompt = VERIFY_COVERAGE_PROMPT.format(
        requirements_json=json.dumps(requirements, indent=2),
        steps_json=json.dumps(steps_for_prompt, indent=2),
    )
    event_log = get_event_log()
    event_log.emit(EventType.LLM_CALL_START,
                   data={"purpose": "verify_coverage"})
    try:
        response = client.generate(prompt)
    except Exception as e:
        logger.warning("Coverage verification failed: %s", e)
        # Fail-open: assume all covered to avoid blocking execution
        return [
            {"requirement": r, "covered": True, "covering_steps": []}
            for r in requirements
        ]
    event_log.emit(EventType.LLM_CALL_COMPLETE,
                   data={"purpose": "verify_coverage"})

    # Parse JSON array
    try:
        text = response.strip()
        if text.startswith("```"):
            text = re.sub(r"^```\w*\n?", "", text)
            text = re.sub(r"\n?```\s*$", "", text)
        matrix = json.loads(text)
        if isinstance(matrix, list):
            return matrix
    except (json.JSONDecodeError, ValueError):
        pass
    match = re.search(r"\[.*\]", response, re.DOTALL)
    if match:
        try:
            matrix = json.loads(match.group())
            if isinstance(matrix, list):
                return matrix
        except (json.JSONDecodeError, ValueError):
            pass
    logger.warning("Could not parse coverage matrix from LLM response.")
    return [
        {"requirement": r, "covered": True, "covering_steps": []}
        for r in requirements
    ]


def fill_coverage_gaps(
    goal: str,
    uncovered: list[str],
    existing_steps: list[dict],
) -> list[dict]:
    """Generate new steps to cover uncovered requirements.

    Returns a list of new step dicts to append to the plan.
    Section 1c of PLAN.md.
    """
    if not uncovered:
        return []

    client = get_llm_client(role="planner")
    next_step = len(existing_steps) + 1
    steps_for_prompt = [
        {"step_number": i + 1, "title": s["title"],
         "description": s["description"],
         "depends_on": s.get("depends_on", [])}
        for i, s in enumerate(existing_steps)
    ]
    prompt = FILL_GAPS_PROMPT.format(
        goal=goal,
        uncovered_json=json.dumps(uncovered, indent=2),
        steps_json=json.dumps(steps_for_prompt, indent=2),
        next_step_number=next_step,
    )
    event_log = get_event_log()
    event_log.emit(EventType.LLM_CALL_START,
                   data={"purpose": "fill_coverage_gaps"})
    try:
        response = client.generate(prompt)
    except Exception as e:
        logger.warning("Gap-filling failed: %s", e)
        return []
    event_log.emit(EventType.LLM_CALL_COMPLETE,
                   data={"purpose": "fill_coverage_gaps"})

    try:
        new_steps = parse_steps_json(response)
    except ValueError:
        logger.warning("Could not parse gap-filling steps.")
        return []

    if not new_steps:
        return []

    for step in new_steps:
        step.setdefault("depends_on", [])
        step.setdefault("verify", "")
        step.setdefault("environment", [])
        step.setdefault("outputs", [])
        if "title" not in step or "description" not in step:
            continue

    # Filter out any steps missing required fields
    new_steps = [s for s in new_steps if "title" in s and "description" in s]

    return new_steps


def ensure_coverage(goal: str, steps: list[dict]) -> tuple[list[dict], list[str]]:
    """Extract requirements, verify coverage, fill gaps if needed.

    Convenience function that chains extract → verify → fill.
    Returns (updated_steps, requirements).
    """
    requirements = extract_requirements(goal)
    if not requirements:
        logger.info("  No requirements extracted, skipping coverage check.")
        return steps, []

    logger.info("  Extracted %d requirements from goal.", len(requirements))

    matrix = verify_coverage(requirements, steps)
    uncovered = [
        entry["requirement"]
        for entry in matrix
        if not entry.get("covered", True)
    ]

    if not uncovered:
        logger.info("  All %d requirements covered.", len(requirements))
        return steps, requirements

    logger.info(
        "  %d/%d requirements uncovered: %s",
        len(uncovered), len(requirements),
        ", ".join(uncovered[:5]) + ("..." if len(uncovered) > 5 else ""),
    )

    new_steps = fill_coverage_gaps(goal, uncovered, steps)
    if new_steps:
        logger.info("  Added %d steps to fill coverage gaps.", len(new_steps))
        steps = steps + new_steps
        # Re-validate dependencies with the expanded step list
        try:
            validate_depends_on(steps)
        except ValueError:
            # Normalize deps: new steps may reference by absolute number
            # which is already correct since they start at len(existing)+1
            logger.debug("Dependency validation after gap-fill — adjusting.")

    return steps, requirements


# ---------------------------------------------------------------------------
# Section 3 — Enforce creation/integration separation
# ---------------------------------------------------------------------------

SPLIT_COUPLED_PROMPT = """\
<steps>
{steps_json}
</steps>

<instructions>
The following step has been flagged as coupling creation and integration — it \
both creates a new module AND modifies/integrates into an existing one. This \
is the #1 cause of rewrite failures.

Flagged step (number {step_number}):
  Title: {step_title}
  Description: {step_description}

Split this into exactly TWO steps:
1. **Creation step**: Create the new module with its own tests/verification. \
   Keep the same depends_on as the original step.
2. **Integration step**: Modify the existing codebase to import and use the \
   new module. This step depends on the creation step (step {step_number}) \
   plus any other dependencies from the original.

Return ONLY a JSON array of exactly 2 step objects:
[
  {{"title": "...", "description": "...", "depends_on": [...], "verify": "...", "environment": [...], "outputs": [...]}},
  {{"title": "...", "description": "...", "depends_on": [{step_number}, ...], "verify": "...", "environment": [...], "outputs": [...]}}
]

Use the SAME step numbering context: the creation step replaces step \
{step_number}, and the integration step becomes step {next_step_number}.
</instructions>
"""

# Heuristic patterns for detecting coupled steps.
_CREATE_PATTERNS = re.compile(
    r"\b(create|write|build|implement|generate|produce|develop|add new)\b",
    re.IGNORECASE,
)
_INTEGRATE_PATTERNS = re.compile(
    r"\b(update|modify|integrate|import into|wire into|add to|hook into|"
    r"incorporate into|connect to|plug into|extend existing)\b",
    re.IGNORECASE,
)


def _step_is_coupled(step: dict) -> bool:
    """Return True if a step description couples creation and integration."""
    text = step.get("description", "") + " " + step.get("title", "")
    return bool(_CREATE_PATTERNS.search(text) and _INTEGRATE_PATTERNS.search(text))


def split_coupled_steps(steps: list[dict]) -> list[dict]:
    """Detect steps that couple creation + integration and split them.

    Section 3b of PLAN.md.

    Uses heuristic detection followed by LLM-assisted splitting. Returns a
    new step list with coupled steps replaced by creation + integration pairs.
    Dependency references in subsequent steps are adjusted to point to the
    integration step (which is the one that "completes" the original work).
    """
    coupled_indices = [i for i, s in enumerate(steps) if _step_is_coupled(s)]
    if not coupled_indices:
        logger.info("  No coupled creation/integration steps detected.")
        return steps

    logger.info(
        "  Detected %d coupled step(s): %s",
        len(coupled_indices),
        ", ".join(str(i + 1) for i in coupled_indices),
    )

    client = get_llm_client(role="planner")
    event_log = get_event_log()

    result: list[dict] = []
    # Map from old 1-based step number → new 1-based step number for the
    # integration half (the step that "replaces" the original in the DAG).
    remap: dict[int, int] = {}
    # Track creation deps to add AFTER the remap pass (since creation_num
    # is a new number that would be incorrectly remapped otherwise).
    # Maps result list index → creation step's new 1-based number.
    _creation_deps: dict[int, int] = {}
    next_new = 1

    for i, step in enumerate(steps):
        old_num = i + 1

        if i not in coupled_indices:
            remap[old_num] = next_new
            result.append(step)
            next_new += 1
            continue

        # Ask LLM to split
        steps_for_prompt = [
            {"step_number": j + 1, "title": s["title"],
             "description": s["description"]}
            for j, s in enumerate(steps)
        ]
        prompt = SPLIT_COUPLED_PROMPT.format(
            steps_json=json.dumps(steps_for_prompt, indent=2),
            step_number=old_num,
            step_title=step["title"],
            step_description=step["description"],
            next_step_number=old_num + 1,
        )

        event_log.emit(EventType.LLM_CALL_START,
                       data={"purpose": "split_coupled_step",
                             "step": old_num})
        try:
            response = client.generate(prompt)
        except Exception as e:
            logger.warning("  Split failed for step %d: %s — keeping as-is.", old_num, e)
            remap[old_num] = next_new
            result.append(step)
            next_new += 1
            continue
        event_log.emit(EventType.LLM_CALL_COMPLETE,
                       data={"purpose": "split_coupled_step",
                             "step": old_num})

        pair = _parse_split_response(response)
        if pair is None or len(pair) != 2:
            logger.warning("  Could not parse split for step %d — keeping as-is.", old_num)
            remap[old_num] = next_new
            result.append(step)
            next_new += 1
            continue

        creation, integration = pair
        for s in (creation, integration):
            s.setdefault("depends_on", [])
            s.setdefault("verify", "")
            s.setdefault("environment", [])

        creation_num = next_new
        integration_num = next_new + 1

        # Creation step gets the original's old dependencies (remapped later)
        creation["depends_on"] = step.get("depends_on", [])[:]
        # Integration step gets the original's old dependencies (remapped later);
        # the creation dep is added after remap to avoid incorrect remapping.
        integration["depends_on"] = step.get("depends_on", [])[:]

        remap[old_num] = integration_num  # downstream should depend on integration
        result.append(creation)
        integration_idx = len(result)
        result.append(integration)
        _creation_deps[integration_idx] = creation_num
        next_new += 2

        logger.info(
            "  Split step %d → %d (create: %s) + %d (integrate: %s)",
            old_num, creation_num, creation["title"],
            integration_num, integration["title"],
        )

    # Remap depends_on in all steps (old step numbers → new step numbers)
    for step in result:
        step["depends_on"] = sorted(set(
            remap.get(d, d) for d in step.get("depends_on", [])
        ))

    # Add creation step dependencies to integration steps (post-remap)
    for result_idx, creation_num in _creation_deps.items():
        step = result[result_idx]
        if creation_num not in step["depends_on"]:
            step["depends_on"] = sorted(set(step["depends_on"] + [creation_num]))

    return result


def _parse_split_response(response: str) -> list[dict] | None:
    """Parse the LLM response for split_coupled_steps into a 2-element list."""
    text = response.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list) and len(parsed) == 2:
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    match = re.search(r"\[.*\]", response, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group())
            if isinstance(parsed, list) and len(parsed) == 2:
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return None


# ---------------------------------------------------------------------------
# Section 5 — Integration checkpoint steps
# ---------------------------------------------------------------------------

CHECKPOINT_TEMPLATE = """\
Write a Python script that validates the interface between completed modules. \
For each module produced by steps {step_ids}:
1. Import the module
2. Call its main function(s) with minimal valid inputs
3. Print the return type, shape (if DataFrame), and column names (if applicable)
4. Assert no errors occur
This is a validation step — it must not modify any files."""


def _find_phase_boundaries(
    steps: list[dict], levels: list[list[int]]
) -> list[int]:
    """Return level indices after which to insert integration checkpoints.

    A boundary is placed after a level where parallel work completes (>=2
    steps in the level) or where a subsequent step converges dependencies
    from multiple prior steps.  Returns sorted indices with a minimum
    spacing of 2 levels between them.

    Section 5a helper.
    """
    if len(levels) < 3:
        return []

    step_by_id = {s["id"]: s for s in steps}
    candidates: list[int] = []

    for i in range(1, len(levels) - 1):
        # Pattern 1: parallel completion — >=2 steps finishing at this level
        if len(levels[i]) >= 2:
            candidates.append(i)
            continue

        # Pattern 2: convergence — a step in the next level depends on
        # >=2 steps from earlier levels (work is merging)
        if i + 1 < len(levels):
            prior_ids: set[int] = set()
            for j in range(i + 1):
                prior_ids.update(levels[j])
            for sid in levels[i + 1]:
                deps = set(step_by_id[sid].get("depends_on", []))
                if len(deps & prior_ids) >= 2:
                    candidates.append(i)
                    break

    # Enforce minimum spacing of 2 levels between checkpoints
    result: list[int] = []
    for c in candidates:
        if not result or c - result[-1] >= 2:
            result.append(c)

    return result


def insert_integration_checkpoints(steps: list[dict]) -> list[dict]:
    """Insert checkpoint steps at phase boundaries in the step DAG.

    Section 5a of PLAN.md.  For plans with 7+ steps, finds natural phase
    boundaries where parallel work converges and inserts validation steps
    that check cross-module interfaces before downstream steps proceed.

    Checkpoint steps are appended to the end of the step list with
    dependencies on all steps in the preceding phase.  Steps after the
    boundary gain an additional dependency on the checkpoint.
    """
    if len(steps) < 7:
        logger.info(
            "  Plan has %d steps (< 7), skipping integration checkpoints.",
            len(steps),
        )
        return steps

    # Assign temporary IDs for topological sort (1-based index)
    for i, step in enumerate(steps):
        step["id"] = i + 1

    try:
        levels = topological_sort(steps)
    except ValueError:
        logger.warning(
            "  Could not topologically sort steps; skipping checkpoints."
        )
        for step in steps:
            del step["id"]
        return steps

    if len(levels) < 3:
        logger.info(
            "  Plan has %d level(s) (< 3), skipping integration checkpoints.",
            len(levels),
        )
        for step in steps:
            del step["id"]
        return steps

    boundaries = _find_phase_boundaries(steps, levels)
    if not boundaries:
        # Fallback: insert at midpoint for plans with enough levels
        mid = len(levels) // 2
        if 0 < mid < len(levels) - 1:
            boundaries = [mid]

    if not boundaries:
        for step in steps:
            del step["id"]
        return steps

    logger.info(
        "  Inserting %d integration checkpoint(s) at level boundaries: %s",
        len(boundaries),
        [b + 1 for b in boundaries],
    )

    step_by_id = {s["id"]: s for s in steps}
    new_checkpoints: list[dict] = []

    for boundary_idx in boundaries:
        # All step IDs in the phase up to and including the boundary level
        preceding_ids: list[int] = []
        for lvl_idx in range(boundary_idx + 1):
            preceding_ids.extend(levels[lvl_idx])
        preceding_set = set(preceding_ids)

        # All step IDs after the boundary
        following_ids: list[int] = []
        for lvl_idx in range(boundary_idx + 1, len(levels)):
            following_ids.extend(levels[lvl_idx])

        # Create checkpoint step (numbered after all existing + prior new)
        checkpoint_num = len(steps) + len(new_checkpoints) + 1
        checkpoint = {
            "title": (
                f"Integration checkpoint: validate phase "
                f"{boundary_idx + 1} outputs"
            ),
            "description": CHECKPOINT_TEMPLATE.format(
                step_ids=", ".join(str(sid) for sid in sorted(preceding_ids)),
            ),
            "depends_on": sorted(preceding_ids),
            "verify": (
                "All imports succeed, function calls return without error, "
                "data shapes are valid"
            ),
            "environment": [],
        }
        new_checkpoints.append(checkpoint)

        # Add checkpoint as dependency for following steps whose deps
        # overlap with the preceding phase
        for fid in following_ids:
            s = step_by_id[fid]
            if any(d in preceding_set for d in s.get("depends_on", [])):
                if checkpoint_num not in s["depends_on"]:
                    s["depends_on"] = sorted(
                        set(s["depends_on"] + [checkpoint_num])
                    )

    # Clean up temporary IDs (add_steps assigns final IDs)
    for step in steps:
        del step["id"]

    result = steps + new_checkpoints
    logger.info(
        "  Plan expanded from %d to %d steps.", len(steps), len(result),
    )

    return result


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
1. Are any steps too broad and should be split further? Each step is implemented \
as a single Python script that must stay under ~250 lines. If a step would \
clearly exceed that, split it into smaller steps that save/load intermediate \
artifacts via the shared workspace.
2. Are there missing steps needed to achieve the goal?
3. Are the dependencies correct? Could any steps be made independent to enable parallelism?
4. Are there missing error handling considerations for external resources (network, files)?
5. Are the verify fields specific enough to catch subtle failures?
6. Are environment/package requirements complete? Do they use the current
   best-in-class tools and libraries, not legacy or outdated alternatives?
7. Does the plan correctly avoid git commands (version control is managed by the framework)?
8. Does the plan ensure a README.md and a dependency manifest (with pinned versions) are created for any multi-file project?
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
    # Heuristic: flag steps that are likely overloaded
    overload_warnings = []
    for i, step in enumerate(steps):
        desc = step.get("description", "")
        if len(desc) > 1500:
            # Count distinct output files mentioned in the description
            file_mentions = re.findall(
                r'[\w/]+\.(?:csv|json|txt|png|html|py|joblib|pkl|md)\b', desc
            )
            distinct_files = len(set(file_mentions))
            if distinct_files > 2:
                overload_warnings.append(
                    f"Step {i + 1} (\"{step.get('title', '')}\") has a "
                    f"{len(desc)}-char description mentioning {distinct_files} "
                    f"distinct output files — likely overloaded and should be "
                    f"split into smaller steps with one primary responsibility each."
                )

    client = get_llm_client(role="planner")
    steps_json = json.dumps(steps, indent=2)
    prompt = CRITIQUE_PROMPT.format(goal=goal, steps_json=steps_json)

    if overload_warnings:
        overload_section = (
            "\n<overload_warnings>\n"
            "The following steps appear overloaded and should be split:\n"
            + "\n".join(f"- {w}" for w in overload_warnings)
            + "\n</overload_warnings>\n"
        )
        prompt += overload_section

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
{previous_attempts_section}
<instructions>
This step has failed {attempts} time(s). Based on the failure history above, \
decide your strategy:
- If the core approach is sound but has a fixable bug, fix the bug.
- If the approach itself is flawed, design a completely new approach.
- If the task is too complex for a single script, break it into sequential \
phases within the same script (do phase 1, verify it worked, then phase 2).
- If external resources are unreliable, add defensive fallbacks.

Choose the strategy that best addresses the pattern of failures you see. \
Follow the structured reflection process below.
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
catch specific exceptions, and never hardcode secrets. Do NOT include git commands — version control is managed by the framework.

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
<verification_criteria>{verify_criteria}</verification_criteria>
<step_output_preview>{step_output}</step_output_preview>
</failed_step>

<completed_dependencies>
{dependency_info}
</completed_dependencies>

<instructions>
A step failed with the error shown above. This step depends on previously completed steps.

Determine: is the root cause of this failure in the current step itself, or was it \
caused by incorrect or incomplete output from one of its dependency steps?

Consider:
- If this step reads files produced by a dependency, are those files likely correct \
and suitable for the step's needs?
- Could the dependency have produced subtly wrong output that causes this step to fail?
- Is the error clearly a code issue in this step (syntax, logic, missing import)?
- IMPORTANT: If this is a MODEL PERFORMANCE issue (low R², accuracy, AUC, RMSE, etc.), \
the most likely cause is that INPUT DATA from a dependency step lacks sufficient \
signal, structure, or quality. Randomly generated, purely noise, or poorly structured \
data will cause low model performance regardless of model configuration or hyperparameters.
- If the verification criteria include quantitative thresholds (e.g., R² > 0.7) and \
the actual metric is far below the threshold, the input data is almost certainly the cause.

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
            "root_cause": stderr or "unknown",
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
        "root_cause": stderr or "unknown",
        "strategy_tried": f"attempt {attempt}",
        "lesson": text or "",
        "what_to_try_next": "retry with different approach",
    }


def trace_root_cause(step: dict, error: str,
                     completed_outputs: dict,
                     state: dict) -> tuple[str, int | None]:
    """Determine if a failure's root cause is in this step or a dependency.

    Only called when the step has dependencies. Uses an LLM to reason about
    whether the error was caused by incorrect dependency output.

    Returns:
        ("self", None) - root cause is in this step
        ("dependency", step_id) - root cause is in a declared dependency
        ("missing_dependency", step_id) - root cause is a step that should
            be a dependency but isn't declared as one
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
            f"description={dep_step.get('description', '')}, "
            f"files={files}, output_preview={stdout}"
        )

    dependency_info = "\n".join(dep_lines)

    # Include verification criteria and step output for better diagnosis
    verify_criteria = step.get("verify", "none specified")
    step_output = step.get("output", "")[-500:] if step.get("output") else "none"

    client = get_llm_client(role="planner")
    prompt = ROOT_CAUSE_PROMPT.format(
        description=step["description"],
        error=error,
        dependency_info=dependency_info,
        verify_criteria=verify_criteria,
        step_output=step_output,
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
        # The LLM identified a step that's not a declared dependency.
        # Check if it's a valid pending/completed step — this indicates
        # a missing dependency in the plan.
        if dep_id in step_by_id:
            logger.warning(
                "  Root cause traced to step %d which is NOT a declared "
                "dependency — likely a missing dependency in the plan.",
                dep_id,
            )
            return ("missing_dependency", dep_id)
        logger.warning(
            "  Root cause traced to step %d but it doesn't exist.",
            dep_id,
        )

    return ("self", None)



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

DECOMPOSE_TRUNCATION_PROMPT = """\
<failed_task>{description}</failed_task>

<failure_output>
<stdout>{stdout}</stdout>
<stderr>{stderr}</stderr>
</failure_output>

<instructions>
This code-generation task has failed multiple times because the generated Python \
script is TOO LONG and gets truncated before completion. The LLM cannot produce \
the full script in a single generation pass.

Rewrite the task description to produce a MUCH SHORTER script. Apply these strategies:

1. **Ruthless scoping**: Identify the 2-3 most important sub-tasks. Describe ONLY \
   those in detail. Defer nice-to-have features (extra plots, verbose formatting, \
   additional analyses) to a brief "if time permits" section.
2. **Modularity via disk**: Instruct the script to save intermediate artifacts \
   (trained models, processed data, computed metrics) to disk using joblib/pickle/CSV. \
   This allows the work to be split across multiple runs if needed.
3. **Compact code patterns**: Explicitly instruct the script to use helper functions, \
   avoid inline comments, prefer dict/list comprehensions, and avoid verbose string \
   formatting.
4. **Target under 250 lines**: State this line budget explicitly in the description.

Provide ONLY the improved task description. No explanation.
</instructions>
"""

REWRITE_QUALITY_PROMPT = """\
You are evaluating the quality of a rewritten task description in an automated code generation pipeline.

<original_description>
{original_description}
</original_description>

<rewritten_description>
{rewritten_description}
</rewritten_description>

<error_that_triggered_rewrite>
{error}
</error_that_triggered_rewrite>

Evaluate whether the rewrite is a good, actionable task description:
- Does the rewrite address the root cause of the error?
- Is it actionable and specific?
- Does it avoid repeating the error verbatim?
- Is it a coherent task description (not an essay or analysis)?

Return ONLY valid JSON: {{"quality": "good", "reason": "..."}} or {{"quality": "poor", "reason": "..."}}
"""


def _is_confused_output(result: str, original_desc: str, error: str) -> bool:
    """Check if the LLM output shows structural signs of confusion."""
    if len(result) > max(len(original_desc) * 3, 2000):
        return True
    if error and len(error) > 200 and error[:200] in result:
        return True
    # Detect rewrites that collapse into success summaries or output
    # reports instead of actionable task descriptions.  A valid task
    # description should contain imperative verbs or action-oriented
    # language, not just a list of results.
    result_lower = result.lower()
    _SUMMARY_SIGNALS = [
        "all checks passed", "all checks pass",
        "all tests passed", "all tests pass",
        "validation passed", "verification passed",
        "no files modified", "uas_result:",
        "here's what was fixed", "here's what was changed",
        "here's what was done", "here is what was fixed",
        "the following changes were made",
        "successfully updated", "successfully fixed",
    ]
    signal_count = sum(1 for s in _SUMMARY_SIGNALS if s in result_lower)
    if signal_count >= 1:
        # Even a single summary signal is suspicious — check whether the
        # text reads as a past-tense report rather than an imperative task.
        _REPORT_PATTERNS = [
            "changed from", "renamed to", "updated to",
            "moved to", "removed;", "scores remain",
        ]
        report_hits = sum(1 for p in _REPORT_PATTERNS if p in result_lower)
        if signal_count >= 2 or report_hits >= 2:
            return True
    # A very short rewrite that lacks imperative verbs is likely a
    # summary, not a task description.
    _ACTION_VERBS = [
        "create ", "write ", "build ", "implement ", "generate ",
        "validate ", "check ", "verify ", "test ", "run ", "install ",
        "parse ", "read ", "load ", "import ", "define ", "add ",
        "fix ", "update ", "ensure ", "compute ", "calculate ",
    ]
    if len(result) < 500 and not any(v in result_lower for v in _ACTION_VERBS):
        return True
    return False


def _check_rewrite_quality(result: str, original_desc: str, error: str) -> bool:
    """Check rewrite quality using structural heuristic (hard gate) + LLM assessment.

    Returns True if the output is confused/poor quality.

    The heuristic always runs first: it catches structural patterns
    (summary signals, missing action verbs) that reliably indicate a
    corrupted rewrite.  The LLM check runs second to catch subtler
    quality issues that the heuristic cannot detect.
    """
    # Hard gate: structural heuristic always applies.  This prevents the
    # LLM quality check from overriding clear structural confusion (e.g.,
    # "all checks pass" summaries accepted as "good" by the LLM).
    if _is_confused_output(result, original_desc, error):
        return True

    if not MINIMAL_MODE:
        try:
            client = get_llm_client(role="planner")
            prompt = REWRITE_QUALITY_PROMPT.format(
                original_description=original_desc,
                rewritten_description=result,
                error=error,
            )

            event_log = get_event_log()
            event_log.emit(EventType.LLM_CALL_START,
                           data={"purpose": "rewrite_quality_check"})
            response = client.generate(prompt)
            event_log.emit(EventType.LLM_CALL_COMPLETE,
                           data={"purpose": "rewrite_quality_check"})

            text = response.strip()
            fence_match = re.search(
                r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL,
            )
            if fence_match:
                text = fence_match.group(1)
            else:
                brace_match = re.search(r"\{.*\}", text, re.DOTALL)
                if brace_match:
                    text = brace_match.group(0)

            data = json.loads(text)
            quality = data.get("quality", "good")
            return quality == "poor"
        except Exception:
            logger.debug("LLM rewrite quality check failed", exc_info=True)

    return False


def reflect_and_rewrite(step: dict, orchestrator_stdout: str,
                        orchestrator_stderr: str,
                        previous_attempts: list[dict] | None = None,
                        reflections: list[dict] | None = None) -> str:
    """LLM-driven task rewrite based on full failure history.

    The LLM receives all prior attempt history and reflections and freely
    chooses the best strategy (fix a bug, try a new approach, decompose,
    add defensive fallbacks) instead of following a hardcoded escalation
    sequence.

    Includes red-flagging: outputs showing signs of confusion are resampled once.

    Args:
        previous_attempts: List of prior attempt summaries for this step,
            each with keys: attempt, error, strategy. Included in the prompt
            so the LLM can see the full history and avoid repeating failed strategies.
        reflections: List of structured reflections from prior failures,
            each with keys: attempt, error_type, root_cause,
            strategy_tried, lesson, what_to_try_next. Included as
            <reflection_history> in the prompt.
    """
    client = get_llm_client(role="planner")

    trim_limit = MAX_ERROR_LENGTH if MAX_ERROR_LENGTH > 0 else _DEFAULT_REWRITE_TRIM
    stdout_trimmed = orchestrator_stdout[-trim_limit:] if len(orchestrator_stdout) > trim_limit else orchestrator_stdout
    stderr_trimmed = orchestrator_stderr[-trim_limit:] if len(orchestrator_stderr) > trim_limit else orchestrator_stderr

    attempts = len(previous_attempts) if previous_attempts else 1

    previous_attempts_section = ""
    if previous_attempts:
        lines = []
        for attempt in previous_attempts:
            lines.append(
                f"- Attempt {attempt['attempt']}: "
                f"error={attempt['error']} | "
                f"strategy={attempt['strategy']}"
            )
        previous_attempts_section = (
            "\n<previous_attempts>\n"
            "Summary of ALL prior attempts for this step (do NOT repeat failed strategies):\n"
            + "\n".join(lines)
            + "\n</previous_attempts>"
        )

    # Include structured reflection history
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
        attempts=attempts,
        previous_attempts_section=previous_attempts_section,
    )

    event_log = get_event_log()
    event_log.emit(EventType.LLM_CALL_START,
                   data={"purpose": "reflect_and_rewrite"})
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
    if _check_rewrite_quality(result, step["description"], stderr_trimmed) or low_confidence:
        logger.warning("  Red-flag detected in rewrite output, resampling...")
        response = client.generate(prompt)
        result = re.sub(r"<diagnosis>.*?</diagnosis>", "", response, flags=re.DOTALL)
        result = re.sub(r"<counterfactual>.*?</counterfactual>", "", result, flags=re.DOTALL)
        result = re.sub(r"<strategies>.*?</strategies>", "", result, flags=re.DOTALL)
        result = result.strip()

        # If the resample is still structurally confused, fall back to the
        # original description to prevent description corruption.
        if _is_confused_output(result, step["description"], stderr_trimmed):
            logger.warning("  Resample still confused, keeping original description.")
            return step["description"]

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
2. The combined task can be implemented in under ~250 lines of Python — if not,
   keep them separate so each script fits in a single code-generation pass
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

{protected_requirements_block}

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
8. Use the EXACT same directory names that completed steps created (e.g., if step 1 \
used "outputs/", do NOT switch to "output/" or "results/" in later steps).
9. You MUST NOT remove coverage for any protected requirement listed above. \
You may rewrite, merge, split, or reorder steps, but every protected requirement \
must still be addressed by at least one step in the new plan.

Respond with ONLY a JSON array of the REMAINING steps (not completed ones). \
Each element:
{{"title": "short name", \
"description": "detailed task for a code-generating LLM", \
"depends_on": [step_numbers], \
"verify": "how to verify this step succeeded", \
"environment": ["packages needed"], \
"outputs": ["file paths this step creates or modifies"]}}
</instructions>
"""


def _build_replan_prompt(goal: str, state: dict, unexpected_step: dict,
                         unexpected_detail: str,
                         requirements: list[str] | None = None) -> str:
    """Build the re-planning prompt with optional protected requirements."""
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

    if requirements:
        reqs_text = "\n".join(f"- {r}" for r in requirements)
        protected_block = (
            "<protected_requirements>\n"
            "The following requirements MUST each be addressed by at least one "
            "step in the new plan. You may rewrite, merge, split, or reorder "
            "steps, but you MUST NOT remove coverage for any of these:\n"
            f"{reqs_text}\n"
            "</protected_requirements>"
        )
    else:
        protected_block = ""

    return REPLAN_PROMPT.format(
        goal=goal,
        completed_steps_info=completed_steps_info,
        step_id=unexpected_step["id"],
        step_title=unexpected_step["title"],
        unexpected_detail=unexpected_detail,
        remaining_steps_json=remaining_json,
        protected_requirements_block=protected_block,
    )


def _validate_replan_steps(new_steps: list[dict],
                           state: dict) -> list[dict] | None:
    """Validate and normalize re-planned steps.

    Returns the validated steps or None if validation fails.
    """
    if not new_steps:
        return None
    for step in new_steps:
        if "title" not in step or "description" not in step:
            return None
        step.setdefault("depends_on", [])
        step.setdefault("verify", "")
        step.setdefault("environment", [])
        step.setdefault("outputs", [])
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


def replan_remaining_steps(goal: str, state: dict,
                           unexpected_step: dict,
                           unexpected_detail: str,
                           requirements: list[str] | None = None,
                           ) -> list[dict] | None:
    """Incrementally re-plan remaining steps after an unexpected result.

    Instead of re-decomposing from scratch, adjusts pending steps based on
    actual outputs from completed steps. Returns the new list of remaining
    steps, or None if re-planning fails.

    When *requirements* is provided (Section 2 of PLAN.md), the new plan
    is verified for coverage. If any protected requirement is dropped, the
    LLM is retried up to 2 times with the dropped requirements highlighted.
    If still uncovered after retries, ``fill_coverage_gaps()`` adds the
    missing steps.
    """
    client = get_llm_client(role="planner")
    event_log = get_event_log()

    max_coverage_retries = 2
    dropped: list[str] = []

    for attempt in range(1 + max_coverage_retries):
        # On retry, append dropped requirements to the detail so the LLM
        # knows exactly what it missed.
        retry_detail = unexpected_detail
        if attempt > 0 and dropped:
            retry_detail += (
                "\n\nIMPORTANT: Your previous re-plan dropped coverage for "
                "these requirements — you MUST include steps that address "
                "them:\n" + "\n".join(f"- {r}" for r in dropped)
            )

        prompt = _build_replan_prompt(
            goal, state, unexpected_step, retry_detail,
            requirements=requirements,
        )

        event_log.emit(EventType.LLM_CALL_START,
                       data={"purpose": "replan_remaining_steps",
                             "attempt": attempt + 1})
        try:
            response = client.generate(prompt, stream=True)
            event_log.emit(EventType.LLM_CALL_COMPLETE,
                           data={"purpose": "replan_remaining_steps",
                                 "attempt": attempt + 1})
        except Exception as e:
            logger.warning("Re-planning LLM call failed: %s", e)
            return None

        try:
            new_steps = parse_steps_json(response)
            new_steps = _validate_replan_steps(new_steps, state)
            if new_steps is None:
                return None
        except (ValueError, json.JSONDecodeError) as e:
            logger.warning("Could not parse re-planned steps: %s", e)
            return None

        # Section 2a: Verify coverage is preserved
        if not requirements:
            return new_steps

        # Combine completed + new steps for coverage check
        completed_steps = [
            s for s in state.get("steps", [])
            if s["status"] == "completed"
        ]
        all_steps = completed_steps + new_steps
        matrix = verify_coverage(requirements, all_steps)
        dropped = [
            e["requirement"] for e in matrix
            if not e.get("covered", True)
        ]

        if not dropped:
            logger.info("  Re-plan preserves all %d requirements.",
                        len(requirements))
            return new_steps

        logger.info(
            "  Re-plan attempt %d dropped %d requirement(s): %s",
            attempt + 1, len(dropped),
            ", ".join(dropped[:3]) + ("..." if len(dropped) > 3 else ""),
        )

    # Section 2a: Exhausted retries — fill gaps as fallback
    logger.info(
        "  Re-plan retries exhausted, filling %d coverage gap(s).",
        len(dropped),
    )
    gap_steps = fill_coverage_gaps(goal, dropped, new_steps)
    if gap_steps:
        new_steps = new_steps + gap_steps

    return new_steps


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
        parts.append(f"files produced: {', '.join(files_written)}")
    if summary:
        parts.append(f"output summary: {summary}")
    if uas_result and isinstance(uas_result, dict):
        result_summary = uas_result.get("summary", "")
        if result_summary and result_summary != summary:
            parts.append(f"result: {result_summary}")

    # Extract schemas from data files so downstream steps know the exact
    # column names / keys to code against (prevents data contract mismatch).
    if workspace and files_written:
        for fpath in files_written:
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
                                (".uas_state", ".git", "__pycache__")]
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
                           orchestrator_stderr: str,
                           is_truncation: bool = False) -> str:
    """Decompose a failing step into a more granular multi-phase description.

    Args:
        is_truncation: If True, the failure is due to code being too long
            for a single LLM generation pass.  Uses a truncation-specific
            prompt that emphasises code brevity and modularity.
    """
    client = get_llm_client(role="planner")

    trim_limit = MAX_ERROR_LENGTH if MAX_ERROR_LENGTH > 0 else _DEFAULT_REWRITE_TRIM
    stdout_trimmed = orchestrator_stdout[-trim_limit:] if len(orchestrator_stdout) > trim_limit else orchestrator_stdout
    stderr_trimmed = orchestrator_stderr[-trim_limit:] if len(orchestrator_stderr) > trim_limit else orchestrator_stderr

    template = DECOMPOSE_TRUNCATION_PROMPT if is_truncation else DECOMPOSE_STEP_PROMPT
    prompt = template.format(
        description=step["description"],
        stdout=stdout_trimmed,
        stderr=stderr_trimmed,
    )

    result = client.generate(prompt).strip()
    return result if result else step["description"]


# ---------------------------------------------------------------------------
# Section 6b: Corrective step generation from validation issues
# ---------------------------------------------------------------------------

CORRECTIVE_STEPS_PROMPT = """\
<goal>{goal}</goal>

<validation_issues>
{issues_json}
</validation_issues>

<completed_steps>
{steps_json}
</completed_steps>

<instructions>
The workspace was validated after all steps completed. The validation found \
the issues listed above. Generate corrective steps to fix these issues. Rules:
1. One step per issue (do NOT bundle multiple fixes into one step).
2. Each step must be a self-contained Python script task.
3. Each step should modify or create specific files to address its issue.
4. Set depends_on to reference the completed step(s) whose output is relevant, \
or leave empty if the fix is independent.
5. Maximum {max_steps} steps. If there are more issues than the limit, \
prioritize the most impactful ones.

Number the steps starting from {next_step_number}.

Return ONLY a JSON array of step objects:
[{{"title": "Fix: ...", "description": "...", "depends_on": [...], \
"verify": "...", "environment": [...], "outputs": [...]}}]
</instructions>
"""

MAX_CORRECTIVE_STEPS_PER_ROUND = 5
MAX_CORRECTION_ROUNDS = 2


def generate_corrective_steps(
    goal: str,
    issues: list[str],
    state: dict,
) -> list[dict]:
    """Generate corrective steps from validation issues.

    Takes a list of issue descriptions from workspace validation and produces
    step dicts that each target one specific issue. Returns at most
    ``MAX_CORRECTIVE_STEPS_PER_ROUND`` steps.

    Section 6b of PLAN.md.
    """
    if not issues:
        return []

    client = get_llm_client(role="planner")
    event_log = get_event_log()

    completed_steps = [
        s for s in state.get("steps", [])
        if s.get("status") == "completed"
    ]
    max_id = max((s["id"] for s in state.get("steps", [])), default=0)
    next_step_number = max_id + 1

    steps_for_prompt = [
        {
            "step_number": s["id"],
            "title": s.get("title", ""),
            "description": s.get("description", ""),
            "files_written": s.get("files_written", []),
        }
        for s in completed_steps
    ]

    capped_issues = issues[:MAX_CORRECTIVE_STEPS_PER_ROUND]
    prompt = CORRECTIVE_STEPS_PROMPT.format(
        goal=goal,
        issues_json=json.dumps(capped_issues, indent=2),
        steps_json=json.dumps(steps_for_prompt, indent=2),
        next_step_number=next_step_number,
        max_steps=MAX_CORRECTIVE_STEPS_PER_ROUND,
    )

    event_log.emit(EventType.LLM_CALL_START,
                   data={"purpose": "generate_corrective_steps"})
    try:
        response = client.generate(prompt)
    except Exception as e:
        logger.warning("Corrective step generation failed: %s", e)
        return []
    event_log.emit(EventType.LLM_CALL_COMPLETE,
                   data={"purpose": "generate_corrective_steps"})

    try:
        new_steps = parse_steps_json(response)
    except ValueError:
        logger.warning("Could not parse corrective steps from LLM response.")
        return []

    if not new_steps:
        return []

    # Enforce per-round cap and fill defaults
    new_steps = new_steps[:MAX_CORRECTIVE_STEPS_PER_ROUND]
    for step in new_steps:
        step.setdefault("depends_on", [])
        step.setdefault("verify", "")
        step.setdefault("environment", [])
        step.setdefault("outputs", [])

    # Filter out malformed steps
    new_steps = [s for s in new_steps if "title" in s and "description" in s]

    return new_steps
