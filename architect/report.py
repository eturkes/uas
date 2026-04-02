"""HTML report generator for UAS runs.

Produces a self-contained HTML file with interactive tabs for
overview, timeline, step details, and provenance exploration.
"""

import difflib
import html as html_mod
import json
import os
from typing import Optional

from jinja2 import Environment, FileSystemLoader

from .planner import topological_sort

_TEMPLATE_DIR = os.path.dirname(os.path.abspath(__file__))


def _mermaid_dag(state: dict) -> str:
    """Generate a Mermaid DAG definition from state steps."""
    steps = state.get("steps", [])
    if not steps:
        return "graph TD\n  empty[No steps]"

    lines = ["graph TD"]
    for s in steps:
        sid = s["id"]
        title = s["title"].replace('"', "'")
        status = s.get("status", "pending")
        node_id = f"s{sid}"
        lines.append(f'  {node_id}["{sid}. {title}"]')

        if status == "completed":
            lines.append(f"  style {node_id} fill:#28a745,color:#fff")
        elif status == "failed":
            lines.append(f"  style {node_id} fill:#dc3545,color:#fff")
        elif status == "executing":
            lines.append(f"  style {node_id} fill:#17a2b8,color:#fff")

        for dep in s.get("depends_on", []):
            lines.append(f"  s{dep} --> {node_id}")

    return "\n".join(lines)


def _mermaid_provenance(provenance: dict) -> str:
    """Generate a Mermaid graph from provenance data."""
    nodes = provenance.get("nodes", {})
    edges = provenance.get("edges", [])
    if not nodes:
        return "graph LR\n  empty[No provenance data]"

    lines = ["graph LR"]

    for nid, node in nodes.items():
        label = node.get("label", nid).replace('"', "'")
        ntype = node.get("node_type", "entity")
        safe_id = f"n{nid[:12]}"
        if ntype == "entity":
            lines.append(f'  {safe_id}["{label}"]')
        elif ntype == "activity":
            lines.append(f'  {safe_id}{{{{"{label}"}}}}')
        elif ntype == "agent":
            lines.append(f'  {safe_id}(["{label}"])')

    for edge in edges:
        src = f"n{edge['source'][:12]}"
        tgt = f"n{edge['target'][:12]}"
        etype = edge.get("edge_type", "")
        short = etype.replace("was", "").replace("With", "")[:10]
        lines.append(f"  {src} -->|{short}| {tgt}")

    return "\n".join(lines)


def _timeline_data(state: dict, events: list[dict]) -> list[dict]:
    """Build timeline entries for the Gantt chart from state steps."""
    steps = state.get("steps", [])
    entries = []
    for s in steps:
        sid = s["id"]
        timing = s.get("timing", {})
        elapsed = s.get("elapsed", 0.0)
        llm_t = timing.get("llm_time", 0.0)
        sandbox_t = timing.get("sandbox_time", 0.0)
        entries.append({
            "step_id": sid,
            "title": s["title"],
            "status": s.get("status", "pending"),
            "elapsed": round(elapsed, 2),
            "llm_time": round(llm_t, 2),
            "sandbox_time": round(sandbox_t, 2),
        })
    return entries


def _summary_metrics(state: dict) -> dict:
    """Compute summary metrics from state."""
    steps = state.get("steps", [])
    completed = sum(1 for s in steps if s["status"] == "completed")
    failed = sum(1 for s in steps if s["status"] == "failed")
    total_llm = sum(s.get("timing", {}).get("llm_time", 0.0) for s in steps)
    total_sandbox = sum(s.get("timing", {}).get("sandbox_time", 0.0) for s in steps)
    total_rewrites = sum(s.get("rewrites", 0) for s in steps)
    total_cost = sum(s.get("cost_usd", 0.0) for s in steps)
    total_input = sum(s.get("token_usage", {}).get("input", 0) for s in steps)
    total_output = sum(s.get("token_usage", {}).get("output", 0) for s in steps)
    return {
        "total_steps": len(steps),
        "completed": completed,
        "failed": failed,
        "total_elapsed": round(state.get("total_elapsed", 0.0), 1),
        "total_llm_time": round(total_llm, 1),
        "total_sandbox_time": round(total_sandbox, 1),
        "total_rewrites": total_rewrites,
        "total_cost_usd": round(state.get("total_cost_usd", total_cost), 4),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
    }


def _step_details(state: dict) -> list[dict]:
    """Build detailed step info for the Steps tab."""
    details = []
    for s in state.get("steps", []):
        details.append({
            "id": s["id"],
            "title": s["title"],
            "description": s.get("description", ""),
            "depends_on": s.get("depends_on", []),
            "status": s.get("status", "pending"),
            "elapsed": round(s.get("elapsed", 0.0), 1),
            "timing": s.get("timing", {}),
            "output": s.get("output", ""),
            "error": s.get("error", ""),
            "rewrites": s.get("rewrites", 0),
            "files_written": s.get("files_written", []),
            "uas_result": s.get("uas_result"),
            "verify": s.get("verify", ""),
            "token_usage": s.get("token_usage", {}),
            "cost_usd": round(s.get("cost_usd", 0.0), 4),
        })
    return details


def _colorize_diff(diff_text: str) -> str:
    """Convert unified diff text to HTML with colored line spans."""
    if not diff_text:
        return ""
    lines = diff_text.split("\n")
    html_lines = []
    for line in lines:
        escaped = html_mod.escape(line)
        if line.startswith("+") and not line.startswith("+++"):
            html_lines.append(f'<span class="diff-add">{escaped}</span>')
        elif line.startswith("-") and not line.startswith("---"):
            html_lines.append(f'<span class="diff-del">{escaped}</span>')
        elif line.startswith("@@"):
            html_lines.append(f'<span class="diff-hunk">{escaped}</span>')
        else:
            html_lines.append(escaped)
    return "\n".join(html_lines)


def _code_evolution_data(code_versions: dict) -> dict:
    """Prepare code evolution data for the report template.

    Args:
        code_versions: dict mapping step_id (int) to list of version dicts.

    Returns:
        dict mapping step_id to {versions, diffs, effectiveness}.
    """
    result = {}
    for step_id, versions in code_versions.items():
        step_data = {
            "versions": versions,
            "diffs": [],
            "effectiveness": None,
        }
        for i in range(1, len(versions)):
            prev = versions[i - 1]
            curr = versions[i]
            diff_lines = list(difflib.unified_diff(
                prev["code"].splitlines(keepends=True),
                curr["code"].splitlines(keepends=True),
                fromfile=f"v{i-1} (spec:{prev['spec_attempt']}, orch:{prev['orch_attempt']})",
                tofile=f"v{i} (spec:{curr['spec_attempt']}, orch:{curr['orch_attempt']})",
            ))
            diff_text = "".join(diff_lines)
            step_data["diffs"].append({
                "from_idx": i - 1,
                "to_idx": i,
                "diff_html": _colorize_diff(diff_text),
                "error_trigger": prev.get("error_summary", ""),
            })

        # Retry effectiveness
        if len(versions) >= 2:
            final_success = versions[-1].get("exit_code") == 0
            diff_sizes = []
            for i in range(1, len(versions)):
                d = list(difflib.unified_diff(
                    versions[i - 1]["code"].splitlines(),
                    versions[i]["code"].splitlines(),
                ))
                diff_sizes.append(len(d))
            converging = (
                all(diff_sizes[j] <= diff_sizes[j - 1]
                    for j in range(1, len(diff_sizes)))
                if len(diff_sizes) > 1 else True
            )
            step_data["effectiveness"] = {
                "final_success": final_success,
                "num_attempts": len(versions),
                "converging": converging,
            }

        result[step_id] = step_data
    return result


def generate_report(
    state: dict,
    events: list[dict],
    provenance: dict,
    output_path: str,
    specs: Optional[dict[str, str]] = None,
    code_versions: Optional[dict] = None,
    explanation: Optional[str] = None,
) -> str:
    """Generate a self-contained HTML report.

    Args:
        state: The run state dict.
        events: List of event dicts from the event log.
        provenance: Provenance graph dict with nodes and edges.
        output_path: Where to write the HTML file.
        specs: Optional dict of {step_id: spec_content} for step specs.
        code_versions: Optional dict mapping step_id to list of version dicts.
        explanation: Optional markdown explanation text for the Explanation tab.

    Returns:
        The output_path written to.
    """
    env = Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        autoescape=True,
    )
    template = env.get_template("report_template.html")

    context = {
        "goal": state.get("goal", ""),
        "status": state.get("status", "unknown"),
        "metrics": _summary_metrics(state),
        "mermaid_dag": _mermaid_dag(state),
        "mermaid_provenance": _mermaid_provenance(provenance),
        "timeline_data": json.dumps(_timeline_data(state, events)),
        "steps": _step_details(state),
        "events": events,
        "provenance": provenance,
        "specs": specs or {},
        "code_versions": _code_evolution_data(code_versions or {}),
        "explanation": explanation or "",
    }

    html = template.render(**context)

    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(output_path, "w") as f:
        f.write(html)

    return output_path
