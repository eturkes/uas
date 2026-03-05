#!/usr/bin/env python3
"""Prompt evaluation system for UAS.

Runs prompt cases through the Architect Agent, checks expected outcomes,
and generates an assessment report. Uses container isolation by default.

Usage:
    python3 integration/eval.py                # Run all cases (container mode)
    python3 integration/eval.py -k hello       # Run cases matching 'hello'
    python3 integration/eval.py --list         # List available cases
    python3 integration/eval.py -v             # Verbose (show architect logs)
    python3 integration/eval.py --local        # Use local subprocess mode
    python3 integration/eval.py --clean        # Remove previous workspaces first
"""

import argparse
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
PROMPTS_FILE = os.path.join(SCRIPT_DIR, "prompts.json")
RESULTS_FILE = os.path.join(SCRIPT_DIR, "eval_results.json")
DEFAULT_TIMEOUT = 600


def load_prompts(filter_pattern=None):
    with open(PROMPTS_FILE) as f:
        prompts = json.load(f)
    if filter_pattern:
        prompts = [
            p for p in prompts
            if re.search(filter_pattern, p["name"], re.IGNORECASE)
        ]
    return prompts


def run_case(case, verbose=False, local=False):
    """Run a single prompt case and return results."""
    name = case["name"]
    goal = case["goal"]
    timeout = case.get("timeout", DEFAULT_TIMEOUT)

    workspace = os.path.join(WORKSPACES_DIR, name)
    if os.path.exists(workspace):
        shutil.rmtree(workspace)
    os.makedirs(workspace)

    # Copy setup files into workspace
    for filename in case.get("setup_files", []):
        src = os.path.join(DATA_DIR, filename)
        if not os.path.exists(src):
            return {
                "name": name, "goal": goal, "workspace": workspace,
                "checks": [], "passed": False,
                "error": f"Setup file missing: data/{filename}",
                "elapsed": 0,
            }
        shutil.copy2(src, os.path.join(workspace, filename))

    output_file = os.path.join(workspace, "output.json")

    env = os.environ.copy()
    env["UAS_GOAL"] = goal
    env["UAS_WORKSPACE"] = workspace
    env["UAS_OUTPUT"] = output_file
    env["PYTHONPATH"] = REPO_ROOT
    if local:
        env["UAS_SANDBOX_MODE"] = "local"
    if verbose:
        env["UAS_VERBOSE"] = "1"

    result = {"name": name, "goal": goal, "workspace": workspace, "checks": []}

    start = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "architect.main"],
            env=env,
            cwd=workspace,
            timeout=timeout,
            capture_output=not verbose,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        result["exit_code"] = proc.returncode
        result["elapsed"] = time.monotonic() - start
        if not verbose and proc.stderr:
            result["log"] = proc.stderr[-2000:]
    except subprocess.TimeoutExpired:
        result["exit_code"] = -1
        result["elapsed"] = timeout
        result["error"] = f"Timed out after {timeout}s"
        result["passed"] = False
        return result
    except Exception as e:
        result["exit_code"] = -1
        result["elapsed"] = time.monotonic() - start
        result["error"] = str(e)
        result["passed"] = False
        return result

    if os.path.exists(output_file):
        with open(output_file) as f:
            result["output"] = json.load(f)

    all_passed = result["exit_code"] == 0
    for check in case.get("checks", []):
        check_result = run_check(check, workspace)
        result["checks"].append(check_result)
        if not check_result["passed"]:
            all_passed = False

    result["passed"] = all_passed
    return result


def run_check(check, workspace):
    """Run a single check against the workspace."""
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
        matched = bool(re.search(check["pattern"], content))
        detail = "matched" if matched else f"content: {content.strip()[:200]!r}"
        return {
            "type": ctype,
            "path": check["path"],
            "pattern": check["pattern"],
            "passed": matched,
            "detail": detail,
        }

    return {"type": ctype, "passed": False, "detail": "unknown check type"}


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


def main():
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
    args = parser.parse_args()

    cases = load_prompts(args.filter)
    if not cases:
        print("No matching prompt cases found.", file=sys.stderr)
        return 1

    if args.list:
        for c in cases:
            checks = ", ".join(ch["type"] for ch in c.get("checks", []))
            setup = c.get("setup_files", [])
            tag = " (needs data/)" if setup else ""
            print(f"  {c['name']:<25} [{checks}]{tag}")
            print(f"    {c['goal'][:80]}")
        return 0

    if args.clean and os.path.exists(WORKSPACES_DIR):
        shutil.rmtree(WORKSPACES_DIR)

    print(f"Running {len(cases)} prompt case(s)...\n", file=sys.stderr)

    results = []
    for i, case in enumerate(cases, 1):
        label = case["goal"][:70]
        print(f"[{i}/{len(cases)}] {case['name']}: {label}...",
              file=sys.stderr)
        result = run_case(case, verbose=args.verbose, local=args.local)
        results.append(result)
        status = "PASS" if result["passed"] else "FAIL"
        print(f"        -> {status} ({result.get('elapsed', 0):.1f}s)",
              file=sys.stderr)

    with open(RESULTS_FILE, "w") as f:
        json.dump(results, f, indent=2, default=str)

    print_report(results)
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
