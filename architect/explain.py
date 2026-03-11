"""Decision explanation layer for UAS runs.

Uses the provenance graph, event log, and code tracker data to generate
human-readable explanations of what happened during a run, answering
"why" questions about the execution without additional LLM calls.
"""

import difflib
import json
import os
import re
from collections import defaultdict
from typing import Optional


# Failure taxonomy keywords
_FAILURE_PATTERNS = {
    "dependency_error": [
        "ModuleNotFoundError", "ImportError", "No module named",
        "package", "pip install", "not installed",
    ],
    "logic_error": [
        "TypeError", "ValueError", "AttributeError", "KeyError",
        "IndexError", "ZeroDivisionError", "AssertionError",
    ],
    "environment_error": [
        "PermissionError", "FileNotFoundError", "IsADirectoryError",
        "OSError", "IOError", "disk", "space", "memory",
    ],
    "network_error": [
        "ConnectionError", "TimeoutError", "URLError", "HTTPError",
        "socket", "Connection refused", "DNS", "SSL",
    ],
    "timeout": [
        "timeout", "timed out", "Timeout", "TIMEOUT", "exceeded",
    ],
    "format_error": [
        "JSONDecodeError", "UAS_RESULT", "parse", "format",
        "unexpected", "invalid syntax", "SyntaxError",
    ],
}


def classify_failure_heuristic(error_text: str) -> str:
    """Classify a failure by type using keyword matching on error messages."""
    if not error_text:
        return "unknown"
    scores = {}
    for category, keywords in _FAILURE_PATTERNS.items():
        score = sum(1 for kw in keywords if kw in error_text)
        if score > 0:
            scores[category] = score
    if not scores:
        return "unknown"
    return max(scores, key=scores.get)


_CANONICAL_ERROR_TYPES = {
    "dependency_error", "logic_error", "environment_error",
    "network_error", "timeout", "format_error", "unknown",
}


def classify_failure(error_text: str, step_context: Optional[dict] = None) -> str:
    """Classify a failure, preferring LLM-generated error_type from reflections.

    If step_context is provided and has reflections with a valid error_type,
    uses the most recent reflection's classification. Otherwise falls back
    to keyword-based heuristic.
    """
    if step_context is not None:
        reflections = step_context.get("reflections")
        if reflections:
            error_type = reflections[-1].get("error_type", "")
            if error_type in _CANONICAL_ERROR_TYPES:
                return error_type
    return classify_failure_heuristic(error_text)


def compute_critical_path(steps: list[dict]) -> list[int]:
    """Compute the critical path: the longest chain of dependent steps
    determining total wall-clock time.

    Uses dynamic programming on the DAG. Each step's "weight" is its
    elapsed time.
    """
    if not steps:
        return []

    step_by_id = {s["id"]: s for s in steps}
    # dp[sid] = (longest path time ending at sid, predecessor)
    dp: dict[int, tuple[float, Optional[int]]] = {}

    def _longest(sid: int) -> tuple[float, Optional[int]]:
        if sid in dp:
            return dp[sid]
        s = step_by_id[sid]
        elapsed = s.get("elapsed", 0.0)
        deps = s.get("depends_on", [])
        if not deps:
            dp[sid] = (elapsed, None)
            return dp[sid]
        best_time = 0.0
        best_pred = None
        for dep_id in deps:
            if dep_id not in step_by_id:
                continue
            dep_time, _ = _longest(dep_id)
            if dep_time > best_time:
                best_time = dep_time
                best_pred = dep_id
        dp[sid] = (best_time + elapsed, best_pred)
        return dp[sid]

    for s in steps:
        _longest(s["id"])

    if not dp:
        return []

    # Find the step with the longest path
    end_id = max(dp, key=lambda sid: dp[sid][0])
    # Trace back the path
    path = []
    current = end_id
    while current is not None:
        path.append(current)
        current = dp[current][1]
    path.reverse()
    return path


def _time_breakdown(steps: list[dict]) -> dict:
    """Compute time breakdown across all steps."""
    total_llm = 0.0
    total_sandbox = 0.0
    total_elapsed = 0.0
    for s in steps:
        timing = s.get("timing", {})
        total_llm += timing.get("llm_time", 0.0)
        total_sandbox += timing.get("sandbox_time", 0.0)
        total_elapsed += s.get("elapsed", 0.0)
    overhead = max(total_elapsed - total_llm - total_sandbox, 0.0)
    return {
        "llm_time": round(total_llm, 1),
        "sandbox_time": round(total_sandbox, 1),
        "overhead": round(overhead, 1),
        "total_elapsed": round(total_elapsed, 1),
    }


def _rewrite_effectiveness(code_versions: dict) -> dict:
    """Assess rewrite effectiveness per step.

    Returns dict mapping step_id -> {
        num_attempts, final_success, error_types_changed,
        converging, verdict
    }
    """
    result = {}
    for step_id, versions in code_versions.items():
        if len(versions) < 2:
            continue
        final_success = versions[-1].get("exit_code") == 0
        # Check if error types change across attempts
        error_types = [
            classify_failure(v.get("error_summary", ""))
            for v in versions
        ]
        error_types_changed = len(set(error_types)) > 1
        # Check if diffs are converging (getting smaller)
        diff_sizes = []
        for i in range(1, len(versions)):
            d = list(difflib.unified_diff(
                versions[i - 1].get("code", "").splitlines(),
                versions[i].get("code", "").splitlines(),
            ))
            diff_sizes.append(len(d))
        converging = (
            all(diff_sizes[j] <= diff_sizes[j - 1]
                for j in range(1, len(diff_sizes)))
            if len(diff_sizes) > 1 else True
        )
        if final_success:
            verdict = "effective"
        elif converging and error_types_changed:
            verdict = "partially_effective"
        else:
            verdict = "ineffective"
        result[step_id] = {
            "num_attempts": len(versions),
            "final_success": final_success,
            "error_types_changed": error_types_changed,
            "converging": converging,
            "verdict": verdict,
        }
    return result


def _context_influence(steps: list[dict], code_versions: dict) -> dict:
    """Check which dependency outputs were referenced in generated code.

    Heuristic: check if filenames from dependency outputs appear in the
    generated code.
    """
    step_by_id = {s["id"]: s for s in steps}
    result = {}
    for s in steps:
        deps = s.get("depends_on", [])
        if not deps:
            continue
        sid = s["id"]
        # Get latest code for this step
        versions = code_versions.get(sid, [])
        code = versions[-1].get("code", "") if versions else ""
        if not code:
            code = s.get("output", "")
        referenced = []
        not_referenced = []
        for dep_id in deps:
            dep = step_by_id.get(dep_id, {})
            dep_files = dep.get("files_written", [])
            found = False
            for f in dep_files:
                basename = os.path.basename(f)
                if basename and basename in code:
                    found = True
                    break
            if found:
                referenced.append(dep_id)
            else:
                not_referenced.append(dep_id)
        if referenced or not_referenced:
            result[sid] = {
                "referenced_deps": referenced,
                "unreferenced_deps": not_referenced,
            }
    return result


class RunExplainer:
    """Generates human-readable explanations of a UAS run."""

    def __init__(
        self,
        state: dict,
        events: list[dict],
        provenance: dict,
        code_versions: Optional[dict] = None,
    ):
        self._state = state
        self._events = events
        self._provenance = provenance
        self._code_versions = code_versions or {}
        self._steps = state.get("steps", [])

        # Pre-compute analyses
        self._critical_path = compute_critical_path(self._steps)
        self._time_breakdown = _time_breakdown(self._steps)
        self._failure_taxonomy = {}
        for s in self._steps:
            if s.get("error"):
                self._failure_taxonomy[s["id"]] = classify_failure(s["error"])
        self._rewrite_eff = _rewrite_effectiveness(self._code_versions)
        self._context_infl = _context_influence(self._steps, self._code_versions)

    @property
    def critical_path(self) -> list[int]:
        return self._critical_path

    @property
    def failure_taxonomy(self) -> dict:
        return self._failure_taxonomy

    @property
    def rewrite_effectiveness(self) -> dict:
        return self._rewrite_eff

    def explain_run(self) -> str:
        """Natural-language summary of the entire run."""
        goal = self._state.get("goal", "N/A")
        status = self._state.get("status", "unknown")
        total_elapsed = self._state.get("total_elapsed", 0.0)
        steps = self._steps
        completed = sum(1 for s in steps if s["status"] == "completed")
        failed = sum(1 for s in steps if s["status"] == "failed")
        total_rewrites = sum(s.get("rewrites", 0) for s in steps)

        lines = [
            "# Run Explanation",
            "",
            f"**Goal:** {goal}",
            f"**Status:** {status}",
            f"**Total time:** {total_elapsed:.1f}s",
            f"**Steps:** {completed} completed, {failed} failed out of {len(steps)}",
            "",
        ]

        # Time breakdown
        tb = self._time_breakdown
        lines.append("## Time Breakdown")
        lines.append("")
        lines.append(f"- LLM generation: {tb['llm_time']}s")
        lines.append(f"- Sandbox execution: {tb['sandbox_time']}s")
        lines.append(f"- Overhead: {tb['overhead']}s")
        lines.append("")

        # Critical path
        if self._critical_path:
            step_by_id = {s["id"]: s for s in steps}
            cp_time = sum(
                step_by_id[sid].get("elapsed", 0.0)
                for sid in self._critical_path
                if sid in step_by_id
            )
            cp_names = [
                f"Step {sid} ({step_by_id[sid]['title']})"
                for sid in self._critical_path
                if sid in step_by_id
            ]
            lines.append("## Critical Path")
            lines.append("")
            lines.append(
                f"The critical path ({cp_time:.1f}s) runs through: "
                + " -> ".join(cp_names)
            )
            lines.append("")

        # Rewrites
        if total_rewrites > 0:
            lines.append(f"## Rewrites ({total_rewrites} total)")
            lines.append("")
            for s in steps:
                rw = s.get("rewrites", 0)
                if rw > 0:
                    eff = self._rewrite_eff.get(s["id"], {})
                    verdict = eff.get("verdict", "N/A")
                    lines.append(
                        f"- Step {s['id']} ({s['title']}): "
                        f"{rw} rewrites, verdict: {verdict}"
                    )
            lines.append("")

        # Failures
        if self._failure_taxonomy:
            lines.append("## Failures")
            lines.append("")
            step_by_id = {s["id"]: s for s in steps}
            for sid, ftype in self._failure_taxonomy.items():
                s = step_by_id.get(sid, {})
                error_preview = s.get("error", "")[:150]
                lines.append(
                    f"- Step {sid} ({s.get('title', '?')}): "
                    f"**{ftype}** - {error_preview}"
                )
            lines.append("")

        return "\n".join(lines)

    def explain_step(self, step_id: int) -> str:
        """Explain why a step succeeded or failed, what it consumed and produced."""
        step_by_id = {s["id"]: s for s in self._steps}
        s = step_by_id.get(step_id)
        if not s:
            return f"Step {step_id} not found."

        lines = [
            f"# Step {step_id}: {s['title']}",
            "",
            f"**Status:** {s['status']}",
            f"**Elapsed:** {s.get('elapsed', 0.0):.1f}s",
            f"**Description:** {s.get('description', 'N/A')}",
            "",
        ]

        # Dependencies
        deps = s.get("depends_on", [])
        if deps:
            dep_names = [
                f"Step {d} ({step_by_id[d]['title']})"
                for d in deps if d in step_by_id
            ]
            lines.append(f"**Dependencies:** {', '.join(dep_names)}")
            # Context influence
            ci = self._context_infl.get(step_id)
            if ci:
                if ci["referenced_deps"]:
                    lines.append(
                        f"- Referenced outputs from: steps {ci['referenced_deps']}"
                    )
                if ci["unreferenced_deps"]:
                    lines.append(
                        f"- Did not reference outputs from: steps {ci['unreferenced_deps']}"
                    )
            lines.append("")

        # Timing
        timing = s.get("timing", {})
        lines.append("## Timing")
        lines.append(f"- LLM: {timing.get('llm_time', 0.0):.1f}s")
        lines.append(f"- Sandbox: {timing.get('sandbox_time', 0.0):.1f}s")
        lines.append("")

        # On critical path?
        if step_id in self._critical_path:
            cp_idx = self._critical_path.index(step_id)
            lines.append(
                f"This step is on the critical path "
                f"(position {cp_idx + 1}/{len(self._critical_path)})."
            )
            lines.append("")

        # Outputs
        if s.get("files_written"):
            lines.append(f"**Files produced:** {', '.join(s['files_written'])}")
            lines.append("")

        # Rewrites
        rw = s.get("rewrites", 0)
        if rw > 0:
            eff = self._rewrite_eff.get(step_id, {})
            lines.append(f"## Rewrites ({rw})")
            lines.append(f"- Verdict: {eff.get('verdict', 'N/A')}")
            if eff.get("error_types_changed"):
                lines.append("- Error types changed across attempts")
            if eff.get("converging"):
                lines.append("- Code changes were converging")
            lines.append("")

        # Error
        if s.get("error"):
            ftype = self._failure_taxonomy.get(step_id, "unknown")
            lines.append(f"## Failure ({ftype})")
            lines.append(f"```\n{s['error'][:500]}\n```")
            lines.append("")

        return "\n".join(lines)

    def explain_failure(self, step_id: int) -> str:
        """Root cause analysis of a failed step."""
        step_by_id = {s["id"]: s for s in self._steps}
        s = step_by_id.get(step_id)
        if not s:
            return f"Step {step_id} not found."
        if s["status"] != "failed":
            return f"Step {step_id} did not fail (status: {s['status']})."

        error = s.get("error", "")
        ftype = classify_failure(error)
        lines = [
            f"# Failure Analysis: Step {step_id} ({s['title']})",
            "",
            f"**Error type:** {ftype}",
            f"**Rewrites attempted:** {s.get('rewrites', 0)}",
            "",
        ]

        # Error details
        lines.append("## Error")
        lines.append(f"```\n{error[:500]}\n```")
        lines.append("")

        # Rewrite history
        versions = self._code_versions.get(step_id, [])
        if len(versions) > 1:
            lines.append("## Rewrite History")
            lines.append("")
            for i, v in enumerate(versions):
                exit_code = v.get("exit_code", -1)
                err_sum = v.get("error_summary", "")
                status_marker = "ok" if exit_code == 0 else "fail"
                lines.append(
                    f"- v{i} (spec:{v.get('spec_attempt', '?')}, "
                    f"orch:{v.get('orch_attempt', '?')}): "
                    f"[{status_marker}] {err_sum[:100]}"
                )
            lines.append("")

            eff = self._rewrite_eff.get(step_id, {})
            if eff:
                lines.append(f"**Verdict:** {eff.get('verdict', 'N/A')}")
                lines.append("")

        # Root cause suggestion
        lines.append("## Likely Root Cause")
        if ftype == "dependency_error":
            lines.append(
                "A required package or module was not available. "
                "The step's environment list may be incomplete."
            )
        elif ftype == "network_error":
            lines.append(
                "A network request failed. The target service may be "
                "unreachable or the URL may be incorrect."
            )
        elif ftype == "environment_error":
            lines.append(
                "A file system or environment issue occurred. Check "
                "permissions, disk space, or file paths."
            )
        elif ftype == "logic_error":
            lines.append(
                "A programming error in the generated code. The task "
                "description may need to be more specific."
            )
        elif ftype == "timeout":
            lines.append(
                "The operation exceeded its time limit. Consider "
                "breaking the step into smaller subtasks."
            )
        elif ftype == "format_error":
            lines.append(
                "Data format mismatch. The generated code may not "
                "handle the expected input/output format correctly."
            )
        else:
            lines.append(
                "The error could not be automatically classified. "
                "Manual inspection of the error output is recommended."
            )
        lines.append("")

        return "\n".join(lines)

    def explain_critical_path(self) -> str:
        """Explain which steps determined wall-clock time and why."""
        if not self._critical_path:
            return "No critical path (no steps executed)."

        step_by_id = {s["id"]: s for s in self._steps}
        cp_steps = [
            step_by_id[sid] for sid in self._critical_path
            if sid in step_by_id
        ]
        total_cp_time = sum(s.get("elapsed", 0.0) for s in cp_steps)
        total_run = self._state.get("total_elapsed", 0.0)

        lines = [
            "# Critical Path Analysis",
            "",
            f"The critical path consists of {len(cp_steps)} steps "
            f"totaling {total_cp_time:.1f}s",
        ]
        if total_run > 0:
            pct = (total_cp_time / total_run) * 100
            lines[-1] += f" ({pct:.0f}% of wall-clock time)."
        else:
            lines[-1] += "."
        lines.append("")

        for s in cp_steps:
            elapsed = s.get("elapsed", 0.0)
            timing = s.get("timing", {})
            llm = timing.get("llm_time", 0.0)
            sandbox = timing.get("sandbox_time", 0.0)
            lines.append(
                f"- **Step {s['id']}: {s['title']}** ({elapsed:.1f}s) "
                f"- LLM: {llm:.1f}s, Sandbox: {sandbox:.1f}s"
            )
            if s.get("rewrites", 0) > 0:
                lines.append(
                    f"  - {s['rewrites']} rewrites added to this step's time"
                )

        lines.append("")

        # Parallelism opportunity
        non_cp = [
            s for s in self._steps
            if s["id"] not in self._critical_path and s.get("elapsed", 0.0) > 0
        ]
        if non_cp:
            parallel_time = sum(s.get("elapsed", 0.0) for s in non_cp)
            lines.append(
                f"Steps not on the critical path ({len(non_cp)} steps, "
                f"{parallel_time:.1f}s total) ran in parallel and did not "
                f"affect wall-clock time."
            )
            lines.append("")

        return "\n".join(lines)

    def explain_cost(self) -> str:
        """Time and resource breakdown."""
        tb = self._time_breakdown
        steps = self._steps
        total_run = self._state.get("total_elapsed", 0.0)

        lines = [
            "# Cost Analysis",
            "",
            f"**Total wall-clock time:** {total_run:.1f}s",
            "",
            "## Time Breakdown",
            f"- LLM generation: {tb['llm_time']}s",
            f"- Sandbox execution: {tb['sandbox_time']}s",
            f"- Overhead (context building, spec gen): {tb['overhead']}s",
            "",
        ]

        # Per-step cost ranking
        ranked = sorted(steps, key=lambda s: s.get("elapsed", 0.0), reverse=True)
        lines.append("## Most Expensive Steps")
        for s in ranked[:5]:
            elapsed = s.get("elapsed", 0.0)
            if elapsed <= 0:
                continue
            timing = s.get("timing", {})
            pct = (elapsed / total_run * 100) if total_run > 0 else 0
            lines.append(
                f"- Step {s['id']} ({s['title']}): {elapsed:.1f}s "
                f"({pct:.0f}%) - LLM: {timing.get('llm_time', 0.0):.1f}s, "
                f"Sandbox: {timing.get('sandbox_time', 0.0):.1f}s"
            )
        lines.append("")

        # Rewrite cost
        total_rw = sum(s.get("rewrites", 0) for s in steps)
        if total_rw > 0:
            lines.append(f"## Rewrite Cost ({total_rw} total rewrites)")
            for s in steps:
                rw = s.get("rewrites", 0)
                if rw > 0:
                    lines.append(
                        f"- Step {s['id']} ({s['title']}): {rw} rewrites"
                    )
            lines.append("")

        return "\n".join(lines)


def load_run_data(workspace_path: str) -> tuple[dict, list[dict], dict, dict]:
    """Load state, events, provenance, and code versions from a workspace.

    Returns (state, events, provenance, code_versions).
    """
    state_dir = os.path.join(workspace_path, ".state")

    # State
    state_file = os.path.join(state_dir, "state.json")
    if not os.path.exists(state_file):
        raise FileNotFoundError(f"No state.json found in {state_dir}")
    with open(state_file) as f:
        state = json.load(f)

    # Events
    events = []
    events_file = os.path.join(state_dir, "events.jsonl")
    if os.path.exists(events_file):
        with open(events_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))

    # Provenance
    provenance = {"nodes": {}, "edges": []}
    prov_file = os.path.join(state_dir, "provenance.json")
    if os.path.exists(prov_file):
        with open(prov_file) as f:
            provenance = json.load(f)

    # Code versions
    code_versions = {}
    cv_dir = os.path.join(state_dir, "code_versions")
    if os.path.isdir(cv_dir):
        for fname in os.listdir(cv_dir):
            if not fname.endswith(".json"):
                continue
            try:
                step_id = int(fname.replace(".json", ""))
            except ValueError:
                continue
            with open(os.path.join(cv_dir, fname)) as f:
                code_versions[step_id] = json.load(f)

    return state, events, provenance, code_versions
