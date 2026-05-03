#!/usr/bin/env python3
"""Substrate smoke harness for UAS.

Loads cases from ``integration/cases/<tier>/``, prepares per-case
workspaces (copies declared ``setup_files`` from ``integration/data/``),
runs deterministic checks (``file_exists``, ``file_contains``,
``glob_exists``, ``pytest_pass``, ``exit_code``, ``file_shape``,
``command_succeeds``), persists a self-describing JSONL row per
(case × run), and emits per-case + per-tier aggregate reports.

Phase 4 §3 deleted the architect producer this harness used to spawn;
``invoke_architect`` is now a no-op stub. The harness is a substrate
keep-list item — it validates the substrate (case loader / workspace
setup / deterministic checks / JSONL persistence / resume), not any
producer.

Usage:
    python3 integration/eval.py                # Run all cases
    python3 integration/eval.py -k hello       # Run cases matching 'hello'
    python3 integration/eval.py --list         # List available cases
    python3 integration/eval.py --clean        # Remove previous workspaces first
"""

import argparse
import csv
import glob as globmod
import json
import os
import re
import shutil
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
WORKSPACES_DIR = os.path.join(SCRIPT_DIR, "workspace")
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
# Section 9: per-tier directory layout. Each case lives at
# CASES_DIR/<tier>/<case_name>.json. The tier is taken from the
# parent directory name and is the canonical source of truth.
CASES_DIR = os.path.join(SCRIPT_DIR, "cases")
# Legacy per-invocation summary file. Overwritten on every run. Kept
# for one phase as a compatibility surface; Phase 5 removes it. New
# consumers should read RESULTS_JSONL instead.
RESULTS_FILE = os.path.join(SCRIPT_DIR, "eval_results.json")
# Append-only durable log of every (case × run) result row, stamped
# with capture_run_metadata() at the head. Created on first append.
RESULTS_JSONL = os.path.join(SCRIPT_DIR, "eval_results.jsonl")
# Per-invocation derived view of the JSONL log: per-case mean ± stdev
# across all runs in this invocation. Overwritten each run.
RESULTS_AGGREGATE = os.path.join(SCRIPT_DIR, "eval_results_aggregate.json")

UAS_AUTH_DIR = os.path.join(REPO_ROOT, ".uas_auth")
CLAUDE_JSON = os.path.join(UAS_AUTH_DIR, "claude.json")
IMAGE_TAG = "uas-engine:latest"

# Provenance helpers live in integration/provenance.py since Phase 3
# §1; re-exported here so existing callers and monkeypatch targets
# (tests use ``ev.HARNESS_VERSION``, ``ev.capture_run_metadata``,
# ``ev._git_capture``, ``ev._hash_active_config``,
# ``ev._SECRET_ENV_PATTERN``) keep resolving without modification.
from integration.provenance import (  # noqa: E402
    HARNESS_VERSION,
    _SECRET_ENV_PATTERN,
    _git_capture,
    _hash_active_config,
    capture_run_metadata,
)


def append_result_row(row, *, run_metadata, run_index,
                      output_path=None) -> None:
    """Append a single self-describing result row to the JSONL log.

    The row written to disk is a flat dict combining ``run_metadata``
    (git SHA, branch, dirty flag, timestamp, env snapshot, config
    hash, harness version) with ``run_index`` and the per-case row
    fields. Section 6's multi-run loop calls this once per
    (case × run_index); Section 5's single-iteration mode passes
    ``run_index=0``.

    The file is created on first append (``"a"`` mode). Each line is
    a single JSON object with no internal newlines, ``default=str``
    so non-serialisable values like ``datetime`` round-trip as strings
    rather than crashing the writer.

    ``output_path`` defaults to ``RESULTS_JSONL`` but can be overridden
    via the ``--results-out`` CLI flag for scratch / CI runs.
    """
    target = output_path if output_path is not None else RESULTS_JSONL
    record = {**run_metadata, "run_index": run_index, **row}
    parent = os.path.dirname(target)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str))
        f.write("\n")


def load_prior_rows(path, run_metadata) -> list:
    """Return JSONL rows from ``path`` that resume the current session.

    A row is considered resumable iff its ``git_sha``, ``git_dirty``,
    and ``harness_version`` all match ``run_metadata``. Rows from
    other commits, other dirty states, or earlier harness versions
    are ignored — they would corrupt the noise floor if silently
    merged.

    Missing files yield an empty list. Corrupt JSON lines and rows
    missing any of the three gating keys are skipped with a stderr
    warning; the rest of the file still loads.
    """
    if not path or not os.path.isfile(path):
        return []
    target_sha = run_metadata.get("git_sha")
    target_dirty = run_metadata.get("git_dirty")
    target_hv = run_metadata.get("harness_version")
    matched = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError) as exc:
                print(
                    f"  [resume] skipping corrupt JSONL line {lineno} "
                    f"in {path}: {exc}",
                    file=sys.stderr,
                )
                continue
            if (row.get("git_sha") == target_sha
                    and row.get("git_dirty") == target_dirty
                    and row.get("harness_version") == target_hv
                    and "run_index" in row
                    and "name" in row):
                matched.append(row)
    return matched


# Allowed tier values for the case schema. The order is the natural
# difficulty progression and is used by Section 9 case authors as a
# reference. Cases without a tier silently default to "trivial" for
# backward compat with pre-Section-7 prompt files.
ALLOWED_TIERS = ("trivial", "moderate", "hard", "open_ended")

# OAuth helpers re-exported from integration/auth.py so the
# existing test surface (``ev._maybe_refresh_oauth``) keeps
# resolving. The post-§5 harness does not call out to Claude — the
# producer was deleted in §3 — so the refresh path is dead in
# practice; the orchestrator owns the live consumer of these
# helpers.
from integration.auth import (  # noqa: E402
    _DEFAULT_CLAUDE_CREDS,
    _OAUTH_CLIENT_ID,
    _OAUTH_REFRESH_BUFFER,
    _OAUTH_TOKEN_ENDPOINT,
    _maybe_refresh_oauth,
    _read_token_expiry,
    _self_refresh_oauth,
)


def load_prompts(filter_pattern=None, tier=None):
    """Load and filter cases from ``CASES_DIR/<tier>/<case>.json``.

    The directory layout is::

        integration/cases/<tier>/<case_name>.json

    where ``<tier>`` is one of ``ALLOWED_TIERS``. The tier is taken
    from the parent directory name and overrides any ``"tier"`` field
    inside the JSON file. Cases are returned in
    ``ALLOWED_TIERS``-canonical order, sorted alphabetically by file
    name within each tier, so iteration is deterministic across runs.

    ``filter_pattern`` is a case-insensitive regex against case name.
    ``tier`` is an exact-match filter against the case's ``tier``
    field. Tier directories that do not exist are silently skipped,
    so a partially-populated ``cases/`` tree returns whatever is
    present.
    """
    cases = []
    for tier_name in ALLOWED_TIERS:
        tier_dir = os.path.join(CASES_DIR, tier_name)
        if not os.path.isdir(tier_dir):
            continue
        for entry in sorted(os.listdir(tier_dir)):
            if not entry.endswith(".json"):
                continue
            path = os.path.join(tier_dir, entry)
            with open(path) as f:
                case = json.load(f)
            # Directory name is the canonical tier; ignore any
            # in-file tier field that disagrees.
            case["tier"] = tier_name
            cases.append(case)
    if filter_pattern:
        cases = [
            c for c in cases
            if re.search(filter_pattern, c["name"], re.IGNORECASE)
        ]
    if tier:
        cases = [c for c in cases if c.get("tier") == tier]
    return cases


class SetupFileMissing(Exception):
    """Raised by setup_workspace when a declared setup_file is absent."""

    def __init__(self, filename):
        self.filename = filename
        super().__init__(f"Setup file missing: data/{filename}")


def setup_workspace(case) -> str:
    """Create or reset the case workspace and copy declared setup files.

    Returns the absolute workspace path. Raises ``SetupFileMissing`` if
    a declared ``setup_files`` entry is not present in ``DATA_DIR``.
    """
    workspace = os.path.join(WORKSPACES_DIR, case["name"])
    if os.path.exists(workspace):
        shutil.rmtree(workspace)
    os.makedirs(workspace)
    for filename in case.get("setup_files", []):
        src = os.path.join(DATA_DIR, filename)
        if not os.path.exists(src):
            raise SetupFileMissing(filename)
        shutil.copy2(src, os.path.join(workspace, filename))
    return workspace


def invoke_architect(case, workspace, *, local, engine, verbose,
                     extra_env=None) -> dict:
    """No-op producer stub — Phase 4 §5 removed the architect subprocess.

    The post-pivot eval harness is a substrate (case loader + workspace
    setup + deterministic checks + JSONL persistence + resume); it does
    not spawn a producer. Cases that need pre-populated workspace state
    declare ``setup_files`` and `setup_workspace` copies them in. The
    function is retained as a monkey-patchable seam for tests that
    inject a fake producer.
    """
    return {"exit_code": 0, "elapsed": 0.0, "stderr_tail": ""}


def run_checks(case, workspace, invocation) -> list:
    """Run every check declared on a case and return the result list.

    ``invocation`` is threaded through so the ``exit_code`` check type
    can read the producer-stub's return code without re-invoking it.
    ``case`` is threaded through so check types that need case-level
    metadata can access it.
    """
    return [
        run_check(check, workspace, invocation=invocation, case=case)
        for check in case.get("checks", [])
    ]


def build_result(case, workspace, invocation, metrics, checks) -> dict:
    """Assemble the final result row preserving the pre-refactor shape.

    Pre-refactor key order: ``name, goal, workspace, checks, exit_code,
    elapsed, [log], [output], [error], passed``. The exception path
    (``invocation['error']`` set) early-returns without ``log`` or
    ``output``, matching the original behavior.
    """
    result = {
        "name": case["name"],
        "goal": case["goal"],
        "workspace": workspace,
        "checks": checks,
        "exit_code": invocation["exit_code"],
        "elapsed": invocation["elapsed"],
    }
    if invocation.get("error"):
        result["error"] = invocation["error"]
        result["passed"] = False
        return result
    if invocation.get("stderr_tail"):
        result["log"] = invocation["stderr_tail"]
    if metrics.get("output"):
        result["output"] = metrics["output"]
    all_passed = invocation["exit_code"] == 0 and all(
        c["passed"] for c in checks
    )
    result["passed"] = all_passed
    return result


def run_case(case, verbose=False, local=False, engine=None):
    """Run a single prompt case end-to-end and return a result row.

    Thin orchestrator over ``setup_workspace`` → ``invoke_architect``
    → ``run_checks`` → ``build_result``. The ``verbose`` / ``local`` /
    ``engine`` parameters are retained for back-compat with monkey-
    patching tests (`tests/test_eval_resume.py` injects them) but are
    not consumed in the no-producer flow.
    """
    try:
        workspace = setup_workspace(case)
    except SetupFileMissing as exc:
        return {
            "name": case["name"],
            "goal": case["goal"],
            "workspace": os.path.join(WORKSPACES_DIR, case["name"]),
            "checks": [],
            "passed": False,
            "error": str(exc),
            "elapsed": 0,
        }
    invocation = invoke_architect(
        case, workspace,
        local=local, engine=engine, verbose=verbose,
    )
    if invocation.get("error"):
        return build_result(case, workspace, invocation, {}, [])
    checks = run_checks(case, workspace, invocation)
    return build_result(case, workspace, invocation, {}, checks)


def run_check(check, workspace, invocation=None, case=None):
    """Run a single check against the workspace.

    ``invocation`` is the dict returned by ``invoke_architect`` and is
    consumed by check types that need the architect subprocess result
    (currently only ``exit_code``). It is optional so the function can
    be called from tests with synthetic data.

    ``case`` is the full case dict (name, goal, checks, …). It is
    threaded through by ``run_checks`` for check types that need
    case-level metadata. Optional for the same testing reason.

    Supported check types
    ---------------------

    ``file_exists``
        Required: ``path`` (workspace-relative). Passes iff the file
        or directory exists.

    ``file_contains``
        Required: ``path``, ``pattern`` (Python regex). Passes iff
        the file exists and the pattern matches anywhere in its
        content.

    ``glob_exists``
        Required: ``pattern`` (workspace-relative glob, recursive).
        Passes iff at least one path matches.

    ``pytest_pass``
        Required: ``path`` (test file or directory under workspace).
        Optional: ``markers`` (pytest -m expression). Runs
        ``python3 -m pytest <path> -q`` from the workspace; passes
        iff exit code is 0. Returns ``passed=False, detail="pytest
        unavailable"`` if pytest is not importable. Times out after
        120s.

    ``exit_code``
        Optional: ``expected`` (int, default 0). Compares against
        ``invocation['exit_code']``. Requires ``invocation`` to be
        passed in (the orchestrator does this automatically via
        ``run_checks``).

    ``file_shape``
        Required: ``path``, ``format`` (one of ``csv``, ``json``,
        ``jsonl``). Optional shape predicates:
        ``min_rows``, ``max_rows`` (all formats);
        ``min_columns``, ``required_columns`` (CSV only);
        ``required_keys`` (JSON / JSONL — checks first row).
        Passes iff every supplied predicate holds.

    ``command_succeeds``
        Required: ``cmd`` (list of strings). Optional:
        ``cwd_relative`` (workspace-relative subdir). Runs the
        command via ``subprocess.run`` with ``timeout=60``; passes
        iff exit code is 0.
    """
    ctype = check["type"]

    if ctype == "file_exists":
        path = os.path.join(workspace, check["path"])
        exists = os.path.exists(path)
        return {
            "type": ctype,
            "path": check["path"],
            "passed": exists,
            "detail": "found" if exists else "not found",
        }

    if ctype == "file_contains":
        path = os.path.join(workspace, check["path"])
        if not os.path.exists(path):
            return {
                "type": ctype,
                "path": check["path"],
                "pattern": check["pattern"],
                "passed": False,
                "detail": "file not found",
            }
        content = open(path).read()
        # re.MULTILINE so ^/$ anchors match line boundaries, not just
        # the start/end of the file. Case authors consistently expect
        # line-oriented semantics (e.g. "^# Hello from UAS$" matching
        # the first line of a multi-line README); pre-MULTILINE this
        # silently failed every such check.
        matched = bool(re.search(check["pattern"], content, re.MULTILINE))
        detail = "matched" if matched else f"content: {content.strip()[:200]!r}"
        return {
            "type": ctype,
            "path": check["path"],
            "pattern": check["pattern"],
            "passed": matched,
            "detail": detail,
        }

    if ctype == "glob_exists":
        pattern = os.path.join(workspace, check["pattern"])
        matches = globmod.glob(pattern, recursive=True)
        return {
            "type": ctype,
            "pattern": check["pattern"],
            "passed": len(matches) > 0,
            "detail": f"found {len(matches)}: {[os.path.relpath(m, workspace) for m in matches[:5]]}" if matches else "no matches",
        }

    if ctype == "pytest_pass":
        target = check.get("path", ".")
        full_target = os.path.join(workspace, target)
        if not os.path.exists(full_target):
            return {
                "type": ctype, "path": target,
                "passed": False, "detail": "test path not found",
            }
        try:
            import pytest  # noqa: F401
        except ImportError:
            return {
                "type": ctype, "path": target,
                "passed": False, "detail": "pytest unavailable",
            }
        cmd = [sys.executable, "-m", "pytest", target, "-q"]
        markers = check.get("markers")
        if markers:
            cmd.extend(["-m", markers])
        try:
            proc = subprocess.run(
                cmd, cwd=workspace, capture_output=True, text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            return {
                "type": ctype, "path": target,
                "passed": False, "detail": "pytest timed out (120s)",
            }
        if proc.returncode == 0:
            return {
                "type": ctype, "path": target,
                "passed": True, "detail": "all tests passed",
            }
        failed = [
            line.strip() for line in proc.stdout.splitlines()
            if "FAILED" in line
        ]
        detail = f"exit {proc.returncode}"
        if failed:
            shown = failed[:3]
            detail += f"; failed: {'; '.join(shown)}"
            if len(failed) > 3:
                detail += f" (+{len(failed) - 3} more)"
        return {
            "type": ctype, "path": target,
            "passed": False, "detail": detail,
        }

    if ctype == "exit_code":
        expected = check.get("expected", 0)
        if invocation is None:
            return {
                "type": ctype, "expected": expected,
                "passed": False,
                "detail": "exit_code check requires invocation context",
            }
        actual = invocation.get("exit_code")
        return {
            "type": ctype, "expected": expected,
            "passed": actual == expected,
            "detail": f"exit_code={actual}",
        }

    if ctype == "file_shape":
        rel_path = check["path"]
        path = os.path.join(workspace, rel_path)
        fmt = check.get("format", "json")
        if not os.path.exists(path):
            return {
                "type": ctype, "path": rel_path, "format": fmt,
                "passed": False, "detail": "file not found",
            }
        issues = []
        try:
            if fmt == "csv":
                with open(path, newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
                    cols = reader.fieldnames or []
                if "min_rows" in check and len(rows) < check["min_rows"]:
                    issues.append(
                        f"rows={len(rows)} < min_rows={check['min_rows']}"
                    )
                if "max_rows" in check and len(rows) > check["max_rows"]:
                    issues.append(
                        f"rows={len(rows)} > max_rows={check['max_rows']}"
                    )
                if (
                    "min_columns" in check
                    and len(cols) < check["min_columns"]
                ):
                    issues.append(
                        f"cols={len(cols)} < min_columns={check['min_columns']}"
                    )
                if "required_columns" in check:
                    missing = [
                        c for c in check["required_columns"] if c not in cols
                    ]
                    if missing:
                        issues.append(f"missing columns: {missing}")
            elif fmt == "json":
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                rows = data if isinstance(data, list) else [data]
                if "min_rows" in check and len(rows) < check["min_rows"]:
                    issues.append(
                        f"rows={len(rows)} < min_rows={check['min_rows']}"
                    )
                if "max_rows" in check and len(rows) > check["max_rows"]:
                    issues.append(
                        f"rows={len(rows)} > max_rows={check['max_rows']}"
                    )
                if "required_keys" in check:
                    if rows and isinstance(rows[0], dict):
                        missing = [
                            k for k in check["required_keys"]
                            if k not in rows[0]
                        ]
                        if missing:
                            issues.append(f"missing keys: {missing}")
                    elif not rows:
                        issues.append("file empty, cannot check required_keys")
                    else:
                        issues.append(
                            "first row is not an object, cannot check "
                            "required_keys"
                        )
            elif fmt == "jsonl":
                rows = []
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            rows.append(json.loads(line))
                if "min_rows" in check and len(rows) < check["min_rows"]:
                    issues.append(
                        f"rows={len(rows)} < min_rows={check['min_rows']}"
                    )
                if "max_rows" in check and len(rows) > check["max_rows"]:
                    issues.append(
                        f"rows={len(rows)} > max_rows={check['max_rows']}"
                    )
                if "required_keys" in check:
                    if rows and isinstance(rows[0], dict):
                        missing = [
                            k for k in check["required_keys"]
                            if k not in rows[0]
                        ]
                        if missing:
                            issues.append(f"missing keys: {missing}")
                    elif not rows:
                        issues.append("file empty, cannot check required_keys")
            else:
                return {
                    "type": ctype, "path": rel_path, "format": fmt,
                    "passed": False, "detail": f"unknown format: {fmt}",
                }
        except (OSError, json.JSONDecodeError, csv.Error,
                UnicodeDecodeError) as e:
            return {
                "type": ctype, "path": rel_path, "format": fmt,
                "passed": False, "detail": f"parse error: {e}",
            }
        if issues:
            return {
                "type": ctype, "path": rel_path, "format": fmt,
                "passed": False, "detail": "; ".join(issues),
            }
        return {
            "type": ctype, "path": rel_path, "format": fmt,
            "passed": True, "detail": "shape ok",
        }

    if ctype == "command_succeeds":
        cmd = check.get("cmd")
        if not cmd or not isinstance(cmd, list):
            return {
                "type": ctype,
                "passed": False,
                "detail": "command_succeeds check requires 'cmd' as a list",
            }
        cwd_relative = check.get("cwd_relative", "")
        cwd = (
            os.path.join(workspace, cwd_relative)
            if cwd_relative else workspace
        )
        try:
            proc = subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired:
            return {
                "type": ctype, "cmd": cmd,
                "passed": False, "detail": "timed out (60s)",
            }
        except FileNotFoundError as e:
            return {
                "type": ctype, "cmd": cmd,
                "passed": False, "detail": f"command not found: {e}",
            }
        return {
            "type": ctype, "cmd": cmd,
            "passed": proc.returncode == 0,
            "detail": f"exit_code={proc.returncode}",
        }

    return {"type": ctype, "passed": False, "detail": "unknown check type"}


def aggregate_results(all_results) -> dict:
    """Build per-case mean ± stdev across every run iteration.

    Input is the flat list of result dicts from ``run_case`` (one per
    case × run). Each result may carry an embedded ``output`` dict
    holding the Section 1 metrics (per-step timing, token totals,
    attempt counts) — when missing, every metric defaults to zero so
    error-path rows still aggregate cleanly.

    Returns a dict keyed by case name with mean and population stdev
    (``statistics.pstdev``, so ``n_runs == 1`` yields ``stdev = 0.0``)
    for: ``pass_rate``, ``elapsed``, ``llm_time``, ``sandbox_time``,
    ``attempts``, ``tokens_input``, ``tokens_output``. Each entry
    also carries ``n_runs``.
    """
    import statistics
    by_case = {}
    for r in all_results:
        by_case.setdefault(r["name"], []).append(r)
    aggregate = {}
    for name, rows in by_case.items():
        passes = [1.0 if r.get("passed") else 0.0 for r in rows]
        elapseds = [float(r.get("elapsed", 0.0)) for r in rows]
        llm_times = []
        sandbox_times = []
        attempts = []
        tok_in = []
        tok_out = []
        for r in rows:
            out = r.get("output") or {}
            steps = out.get("steps", [])
            llm_t = sum(
                s.get("timing", {}).get("llm_time", 0.0) for s in steps
            )
            sandbox_t = sum(
                s.get("timing", {}).get("sandbox_time", 0.0) for s in steps
            )
            llm_times.append(llm_t)
            sandbox_times.append(sandbox_t)
            attempts.append(out.get("attempt_total", 0))
            tt = out.get("total_tokens") or {"input": 0, "output": 0}
            tok_in.append(tt.get("input", 0))
            tok_out.append(tt.get("output", 0))

        def _ms(values):
            return statistics.mean(values), statistics.pstdev(values)

        pr_m, pr_s = _ms(passes)
        el_m, el_s = _ms(elapseds)
        lt_m, lt_s = _ms(llm_times)
        st_m, st_s = _ms(sandbox_times)
        at_m, at_s = _ms(attempts)
        ti_m, ti_s = _ms(tok_in)
        to_m, to_s = _ms(tok_out)
        aggregate[name] = {
            "n_runs": len(rows),
            "pass_rate_mean": pr_m,
            "pass_rate_stdev": pr_s,
            "elapsed_mean": el_m,
            "elapsed_stdev": el_s,
            "llm_time_mean": lt_m,
            "llm_time_stdev": lt_s,
            "sandbox_time_mean": st_m,
            "sandbox_time_stdev": st_s,
            "attempts_mean": at_m,
            "attempts_stdev": at_s,
            "tokens_input_mean": ti_m,
            "tokens_input_stdev": ti_s,
            "tokens_output_mean": to_m,
            "tokens_output_stdev": to_s,
        }
    return aggregate


def aggregate_by_tier(all_results) -> dict:
    """Per-tier rollup of pass rate across every (case × run) row.

    Each input row should carry a ``tier`` field (the main loop
    stamps it from the case definition; rows from pre-Section-7
    prompt files default to ``"trivial"``).

    Returns a dict keyed by tier name with:

    - ``pass_rate_mean`` / ``pass_rate_stdev``: mean and population
      stdev of the binary pass/fail across all rows in the tier.
    - ``n_cases``: count of distinct case names in the tier.
    - ``n_rows``: total result rows in the tier (cases × runs).
    """
    import statistics
    by_tier = {}
    for r in all_results:
        tier = r.get("tier", "trivial")
        by_tier.setdefault(tier, []).append(r)
    out = {}
    for tier, rows in by_tier.items():
        passes = [1.0 if r.get("passed") else 0.0 for r in rows]
        case_names = {r["name"] for r in rows}
        out[tier] = {
            "pass_rate_mean": statistics.mean(passes),
            "pass_rate_stdev": statistics.pstdev(passes),
            "n_cases": len(case_names),
            "n_rows": len(rows),
        }
    return out


def print_aggregate_report(aggregate, by_tier=None):
    """Print per-case + (optional) per-tier aggregate stats to stderr."""
    if not aggregate:
        return
    print("\n" + "=" * 78, file=sys.stderr)
    print("  UAS Eval Aggregate Report (mean ± stdev across runs)",
          file=sys.stderr)
    print("=" * 78, file=sys.stderr)
    header = (
        f"  {'Case':<25} {'N':>3} {'Pass':>14} {'Wall(s)':>14} "
        f"{'LLM(s)':>14}"
    )
    print(header, file=sys.stderr)
    print("  " + "-" * 76, file=sys.stderr)
    for name in sorted(aggregate.keys()):
        a = aggregate[name]
        pr = f"{a['pass_rate_mean']:.2f}±{a['pass_rate_stdev']:.2f}"
        el = f"{a['elapsed_mean']:.1f}±{a['elapsed_stdev']:.1f}"
        lt = f"{a['llm_time_mean']:.1f}±{a['llm_time_stdev']:.1f}"
        print(
            f"  {name[:25]:<25} {a['n_runs']:>3} {pr:>14} {el:>14} {lt:>14}",
            file=sys.stderr,
        )
    overall_pass = (
        sum(a["pass_rate_mean"] * a["n_runs"] for a in aggregate.values())
        / sum(a["n_runs"] for a in aggregate.values())
    )
    print("  " + "-" * 76, file=sys.stderr)
    print(
        f"  Overall pass rate: {overall_pass:.3f} across "
        f"{len(aggregate)} cases",
        file=sys.stderr,
    )

    if by_tier:
        print("\n  By tier:", file=sys.stderr)
        print("  " + "-" * 76, file=sys.stderr)
        tier_header = (
            f"  {'Tier':<14} {'Cases':>6} {'Rows':>6} {'Pass':>16}"
        )
        print(tier_header, file=sys.stderr)
        # Print in canonical tier order, then any unknown tiers.
        ordered = [t for t in ALLOWED_TIERS if t in by_tier]
        unknown = sorted(set(by_tier) - set(ALLOWED_TIERS))
        for tier in ordered + unknown:
            t = by_tier[tier]
            pr = (
                f"{t['pass_rate_mean']:.2f}±{t['pass_rate_stdev']:.2f}"
            )
            print(
                f"  {tier:<14} {t['n_cases']:>6} {t['n_rows']:>6} "
                f"{pr:>16}",
                file=sys.stderr,
            )
    print("=" * 78, file=sys.stderr)


def print_report(results):
    """Print assessment report."""
    total = len(results)
    passed = sum(1 for r in results if r["passed"])

    print("\n" + "=" * 60)
    print("  UAS Prompt Evaluation Report")
    print("=" * 60)

    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        elapsed = r.get("elapsed", 0)
        print(f"\n  [{status}] {r['name']} ({elapsed:.1f}s)")

        if r.get("error"):
            print(f"         Error: {r['error']}")

        if r.get("output"):
            out = r["output"]
            steps = out.get("steps", [])
            step_info = ", ".join(f"{s['id']}:{s['status']}" for s in steps)
            print(f"         Steps: {len(steps)} [{step_info}]")
            print(f"         Status: {out.get('status', '?')}")

        for c in r.get("checks", []):
            mark = "ok" if c["passed"] else "FAIL"
            print(f"         [{mark}] {c['type']}: {c.get('detail', '')}")

        if not r["passed"] and r.get("exit_code", 0) != 0:
            print(f"         Exit code: {r['exit_code']}")

    print(f"\n{'=' * 60}")
    print(f"  {passed}/{total} passed, {total - passed} failed")
    total_time = sum(r.get("elapsed", 0) for r in results)
    print(f"  Total time: {total_time:.1f}s")
    print(f"  Results: {RESULTS_FILE}")
    print(f"  Workspaces: {WORKSPACES_DIR}/")
    print("=" * 60)


def _find_engine():
    """Return 'podman' or 'docker', whichever is found first."""
    for cmd in ["podman", "docker"]:
        if shutil.which(cmd):
            return cmd
    return None


def _ensure_image(engine):
    """Rebuild uas-engine:latest if missing or stale."""
    import datetime as _dt

    try:
        r = subprocess.run(
            [engine, "image", "inspect", IMAGE_TAG,
             "--format", "{{.Created}}"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            raw = r.stdout.strip()
            raw = re.sub(r'(\.\d{6})\d+', r'\1', raw)
            raw = raw.replace('Z', '+00:00')
            build_time = _dt.datetime.fromisoformat(raw).timestamp()
        else:
            build_time = 0.0
    except Exception:
        build_time = 0.0

    patterns = [
        "Containerfile", "requirements.txt", "entrypoint.sh",
        "architect/*.py", "orchestrator/*.py",
    ]
    latest = 0.0
    for pat in patterns:
        for path in globmod.glob(os.path.join(REPO_ROOT, pat)):
            latest = max(latest, os.path.getmtime(path))

    if build_time > 0 and build_time >= latest:
        return
    print("Rebuilding uas-engine:latest (stale or missing)...",
          file=sys.stderr)
    subprocess.run(
        [engine, "build", "-t", IMAGE_TAG,
         "-f", os.path.join(REPO_ROOT, "Containerfile"), REPO_ROOT],
        check=True,
    )


def main():
    # UAS framework policy: every Claude invocation (including the eval
    # harness) uses the CLI's current default model. We deliberately do
    # NOT inject a default UAS_MODEL — the architect subprocess will
    # fall through to llm_client's no-flag path and pick up Claude's
    # default. Explicit per-shell overrides (UAS_MODEL,
    # UAS_MODEL_PLANNER, UAS_MODEL_CODER) still win.

    parser = argparse.ArgumentParser(description="UAS Prompt Evaluation")
    parser.add_argument("-k", "--filter", help="Run cases matching pattern")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Show architect output")
    parser.add_argument("--list", action="store_true",
                        help="List available cases and exit")
    parser.add_argument("--local", action="store_true",
                        help="Use local subprocess instead of containers")
    parser.add_argument("--clean", action="store_true",
                        help="Remove previous workspaces before running")
    parser.add_argument(
        "--results-out", default=None,
        help="Override path for the append-only JSONL results log "
             "(default: integration/eval_results.jsonl)",
    )
    _default_runs = int(os.environ.get("UAS_EVAL_RUNS", "1"))
    parser.add_argument(
        "--runs", type=int, default=_default_runs,
        help=(
            "Number of times to run the full benchmark for variance "
            f"(default: {_default_runs}, env: UAS_EVAL_RUNS)"
        ),
    )
    parser.add_argument(
        "--tier", default=None, choices=ALLOWED_TIERS,
        help=(
            "Run only cases with this exact tier "
            f"({' / '.join(ALLOWED_TIERS)})"
        ),
    )
    parser.add_argument(
        "--no-resume", action="store_true",
        help=(
            "Disable resume-from-JSONL. By default, rows in the target "
            "results JSONL whose git_sha / git_dirty / harness_version "
            "match the current run are treated as already completed "
            "and not re-executed."
        ),
    )
    args = parser.parse_args()
    if args.runs < 1:
        print("--runs must be >= 1", file=sys.stderr)
        return 1

    cases = load_prompts(args.filter, tier=args.tier)
    if not cases:
        print("No matching prompt cases found.", file=sys.stderr)
        return 1

    if args.list:
        for c in cases:
            checks = ", ".join(ch["type"] for ch in c.get("checks", []))
            setup = c.get("setup_files", [])
            tag = " (needs data/)" if setup else ""
            tier_label = c.get("tier", "trivial")
            print(
                f"  [{tier_label:<10}] {c['name']:<32} [{checks}]{tag}"
            )
            print(f"    {c['goal'][:80]}")
        return 0

    # Capture reproducibility metadata after --list short-circuits.
    # Section 5 stamps this onto every JSONL row.
    run_metadata = capture_run_metadata()
    short_sha = (
        run_metadata["git_sha"][:8]
        if run_metadata["git_sha"] != "unknown" else "unknown"
    )
    short_cfg = (
        run_metadata["config_hash"][:8]
        if run_metadata["config_hash"] != "unavailable" else "unavailable"
    )
    dirty_marker = " (dirty)" if run_metadata["git_dirty"] else ""
    print(
        f"  uas-eval {run_metadata['harness_version']} | "
        f"sha={short_sha}{dirty_marker} | "
        f"branch={run_metadata['git_branch']} | "
        f"config={short_cfg}",
        file=sys.stderr,
    )

    # Discover container engine (unless --local).
    engine = None
    if not args.local:
        engine = _find_engine()
        if engine is None:
            print("WARNING: No container engine found, falling back to "
                  "local mode.", file=sys.stderr)
        else:
            _ensure_image(engine)

    # Seed claude.json if missing.
    if not os.path.isfile(CLAUDE_JSON):
        os.makedirs(UAS_AUTH_DIR, exist_ok=True)
        with open(CLAUDE_JSON, "w", encoding="utf-8") as f:
            f.write("{}")

    if args.clean and os.path.exists(WORKSPACES_DIR):
        shutil.rmtree(WORKSPACES_DIR)

    print(
        f"Running {len(cases)} prompt case(s) × {args.runs} run(s)...\n",
        file=sys.stderr,
    )

    results_jsonl_path = args.results_out or RESULTS_JSONL

    if args.no_resume:
        prior_rows = []
    else:
        prior_rows = load_prior_rows(results_jsonl_path, run_metadata)
    resumable_keys = {
        (r["run_index"], r["name"]) for r in prior_rows
    }
    if prior_rows:
        print(
            f"  [resume] reusing {len(prior_rows)} row(s) from "
            f"{results_jsonl_path} (matching sha/dirty/harness)",
            file=sys.stderr,
        )

    all_results = list(prior_rows)
    first_run_results = [
        r for r in prior_rows if r.get("run_index") == 0
    ]
    for run_index in range(args.runs):
        if args.runs > 1:
            print(
                f"\n=== Run {run_index + 1}/{args.runs} ===",
                file=sys.stderr,
            )
        run_results = []
        for i, case in enumerate(cases, 1):
            if (run_index, case["name"]) in resumable_keys:
                print(
                    f"[{i}/{len(cases)}] {case['name']}: "
                    f"-> SKIP (resumed)",
                    file=sys.stderr,
                )
                continue
            _maybe_refresh_oauth()
            label = case["goal"][:70]
            print(
                f"[{i}/{len(cases)}] {case['name']}: {label}...",
                file=sys.stderr,
            )
            result = run_case(
                case, verbose=args.verbose, local=args.local, engine=engine,
            )
            # Section 7: stamp the case's tier onto the result so the
            # by_tier aggregator can group rows without needing the
            # original case definition.
            result["tier"] = case.get("tier", "trivial")
            run_results.append(result)
            all_results.append(result)
            append_result_row(
                result,
                run_metadata=run_metadata,
                run_index=run_index,
                output_path=results_jsonl_path,
            )
            status = "PASS" if result["passed"] else "FAIL"
            print(
                f"        -> {status} ({result.get('elapsed', 0):.1f}s)",
                file=sys.stderr,
            )
        if run_index == 0:
            first_run_results.extend(run_results)

    # Legacy compatibility: RESULTS_FILE holds the first run's results
    # only, preserving the pre-Section-6 "len == len(cases)" shape for
    # any consumer that expects it.
    with open(RESULTS_FILE, "w") as f:
        json.dump(first_run_results, f, indent=2, default=str)

    # Section 6 + 7: aggregate every (case × run) row both per-case
    # and per-tier, then persist the nested view next to the JSONL log.
    by_case = aggregate_results(all_results)
    by_tier = aggregate_by_tier(all_results)
    aggregate_doc = {"by_case": by_case, "by_tier": by_tier}
    with open(RESULTS_AGGREGATE, "w") as f:
        json.dump(aggregate_doc, f, indent=2, default=str)

    print_report(first_run_results)
    print_aggregate_report(by_case, by_tier=by_tier)

    # Pass condition: every case in every run passed.
    return 0 if all(r["passed"] for r in all_results) else 1


if __name__ == "__main__":
    sys.exit(main())
