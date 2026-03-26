"""Generate UAS-compliant markdown spec files for individual steps."""

import os
import re
from .state import get_specs_dir

_MODELING_RE = re.compile(
    r'\b(?:model|train|predict|classif|regress)', re.IGNORECASE,
)


def generate_spec(step: dict, total_steps: int, context: str = "",
                  specs_dir: str = "") -> str:
    """Create a UAS markdown spec and write it to disk.

    *specs_dir* overrides the default location derived from the step's
    run_id (if known).  When omitted, falls back to
    ``get_specs_dir(run_id)`` using the run_id stored on *step*, or
    the legacy ``.state/specs`` directory.

    Returns the path to the written spec file.
    """
    if not specs_dir:
        run_id = step.get("_run_id", "")
        specs_dir = get_specs_dir(run_id) if run_id else get_specs_dir("")
    os.makedirs(specs_dir, exist_ok=True)

    spec = f"# UAS Spec: {step['title']}\n\n"
    spec += "## Metadata\n"
    spec += f"- **Step:** {step['id']} of {total_steps}\n"
    spec += f"- **Status:** {step['status']}\n"
    if step["depends_on"]:
        spec += f"- **Depends On:** {step['depends_on']}\n"
    spec += "\n"

    spec += "## Objective\n"
    spec += f"{step['description']}\n\n"

    if context:
        spec += "## Context\n"
        spec += f"{context}\n\n"

    # Section 7: For modeling steps, prepend a data-quality review directive.
    if _MODELING_RE.search(step.get("description", "")):
        spec += "## Data Quality Review\n"
        spec += (
            "BEFORE writing any modeling code, review the "
            "<data_quality_warnings> section.\n"
            "If critical features are all NaN, you must either:\n"
            "(a) Fix the upstream computation by re-running feature "
            "extraction, OR\n"
            "(b) Report the issue and use only features with valid data.\n"
            "Do NOT silently impute all-NaN columns with 0 -- this produces "
            "meaningless models.\n\n"
        )

    spec += "## Task\n"
    spec += f"Write a Python script that accomplishes the objective above.\n\n"

    if context:
        spec += "Include this context from previous steps:\n"
        spec += f"{context}\n\n"

    spec += "## Acceptance Criteria\n"
    spec += "- The generated Python script exits with code 0.\n"
    spec += "- The script's stdout contains the expected output.\n"

    spec_file = os.path.join(specs_dir, f"step_{step['id']:03d}.md")
    with open(spec_file, "w") as f:
        f.write(spec)

    step["spec_file"] = spec_file
    return spec_file


def build_task_from_spec(step: dict, context: str = "") -> str:
    """Build the task string to pass to the Orchestrator."""
    task = step["description"]
    # Section 7: For modeling steps, prepend data-quality review directive.
    if _MODELING_RE.search(step.get("description", "")):
        task += (
            "\n\nBEFORE writing any modeling code, review the "
            "<data_quality_warnings> section. "
            "If critical features are all NaN, you must either: "
            "(a) fix the upstream computation by re-running feature "
            "extraction, or "
            "(b) report the issue and use only features with valid data. "
            "Do NOT silently impute all-NaN columns with 0."
        )
    if context:
        task += f"\n\nContext from previous steps:\n{context}"
    return task
