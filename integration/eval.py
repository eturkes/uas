#!/usr/bin/env python3
"""Prompt evaluation system for UAS.

Runs prompt cases through the Architect Agent, checks expected outcomes,
and generates an assessment report.  Runs inside the uas-engine container
by default; use ``--local`` for direct subprocess mode.

Usage:
    python3 integration/eval.py                # Run all cases (container mode)
    python3 integration/eval.py -k hello       # Run cases matching 'hello'
    python3 integration/eval.py --list         # List available cases
    python3 integration/eval.py -v             # Verbose (show architect logs)
    python3 integration/eval.py --local        # Use local subprocess mode
    python3 integration/eval.py --clean        # Remove previous workspaces first
"""

import argparse
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
PROMPTS_FILE = os.path.join(SCRIPT_DIR, "prompts.json")
RESULTS_FILE = os.path.join(SCRIPT_DIR, "eval_results.json")

UAS_AUTH_DIR = os.path.join(REPO_ROOT, ".uas_auth")
CLAUDE_JSON = os.path.join(UAS_AUTH_DIR, "claude.json")
IMAGE_TAG = "uas-engine:latest"


def load_prompts(filter_pattern=None):
    with open(PROMPTS_FILE) as f:
        prompts = json.load(f)
    if filter_pattern:
        prompts = [
            p for p in prompts
            if re.search(filter_pattern, p["name"], re.IGNORECASE)
        ]
    return prompts


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
    """Run the architect subprocess (container or local) for one case.

    Returns a dict with keys:

    - ``exit_code``: subprocess return code, or ``-1`` on Python-level
      exception.
    - ``elapsed``: wall-clock seconds the subprocess ran.
    - ``stderr_tail``: last 2000 chars of captured stderr, or an empty
      string when verbose mode streams stderr live.
    - ``error``: only present when an exception was raised launching
      the subprocess; signals the orchestrator to short-circuit.

    ``extra_env`` is merged into the subprocess env (container or
    local) for callers that need to override config knobs. Currently
    unused at the call site but reserved for later sections.
    """
    output_file = os.path.join(workspace, "output.json")
    start = time.monotonic()
    try:
        if engine and not local:
            # Container mode — run architect inside uas-engine.
            # PYTHONPATH=/uas is required because the eval invokes
            # python3 with -P (sandboxing flag that suppresses
            # cwd-prepending), so the architect package would not
            # otherwise be importable from /uas.
            container_env = {
                "UAS_GOAL": case["goal"],
                "UAS_WORKSPACE": "/workspace",
                "UAS_OUTPUT": "/workspace/output.json",
                "PYTHONPATH": "/uas",
            }
            if verbose:
                container_env["UAS_VERBOSE"] = "1"
            if extra_env:
                container_env.update(extra_env)
            cmd = [
                engine, "run", "--rm",
                "--privileged",
                "-e", "IS_SANDBOX=1",
                "-v", f"{UAS_AUTH_DIR}:/root/.claude:Z",
                "-v", f"{CLAUDE_JSON}:/root/.claude.json:Z",
                "-v", f"{workspace}:/workspace:Z",
            ]
            for k, v in container_env.items():
                cmd.extend(["-e", f"{k}={v}"])
            cmd.extend([
                "--entrypoint", "", "-w", "/uas", IMAGE_TAG,
                "python3", "-P", "-m", "architect.main",
            ])
            proc = subprocess.run(
                cmd,
                capture_output=not verbose,
                text=True,
                stdin=subprocess.DEVNULL,
            )
        else:
            # Local subprocess mode.
            env = os.environ.copy()
            env["UAS_GOAL"] = case["goal"]
            env["UAS_WORKSPACE"] = workspace
            env["UAS_OUTPUT"] = output_file
            env["PYTHONPATH"] = REPO_ROOT
            env["CLAUDE_CONFIG_DIR"] = UAS_AUTH_DIR
            if local:
                env["UAS_SANDBOX_MODE"] = "local"
            if verbose:
                env["UAS_VERBOSE"] = "1"
            if extra_env:
                env.update(extra_env)
            proc = subprocess.run(
                [sys.executable, "-P", "-m", "architect.main"],
                env=env,
                cwd=workspace,
                capture_output=not verbose,
                text=True,
                stdin=subprocess.DEVNULL,
            )
        elapsed = time.monotonic() - start
        stderr_tail = ""
        if not verbose and proc.stderr:
            stderr_tail = proc.stderr[-2000:]
        return {
            "exit_code": proc.returncode,
            "elapsed": elapsed,
            "stderr_tail": stderr_tail,
        }
    except Exception as e:
        return {
            "exit_code": -1,
            "elapsed": time.monotonic() - start,
            "stderr_tail": "",
            "error": str(e),
        }


def collect_metrics(workspace) -> dict:
    """Read ``output.json`` from the workspace and project Section 1 fields.

    Returns an empty dict if ``output.json`` is missing or unparseable.
    Otherwise returns a flat metrics dict containing the raw output
    plus the projected per-run metrics surfaced by the architect's
    ``write_json_output()``.
    """
    output_file = os.path.join(workspace, "output.json")
    if not os.path.exists(output_file):
        return {}
    try:
        with open(output_file) as f:
            output = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        "output": output,
        "step_count": output.get("step_count", 0),
        "step_status_counts": output.get("step_status_counts", {}),
        "attempt_total": output.get("attempt_total", 0),
        "total_elapsed": output.get("total_elapsed", 0.0),
        "total_tokens": output.get(
            "total_tokens", {"input": 0, "output": 0}
        ),
        "total_cost_usd": output.get("total_cost_usd", 0.0),
        "workspace_size_bytes": output.get("workspace_size_bytes", 0),
        "architect_status": output.get("status", "unknown"),
    }


def run_checks(case, workspace, invocation) -> list:
    """Run every check declared on a case and return the result list.

    ``invocation`` is threaded through so future check types
    (Section 3 will add ``exit_code``, which reads
    ``invocation['exit_code']``) can consume it. ``run_check`` does
    not yet accept it; Section 3 updates that signature.
    """
    del invocation  # reserved for Section 3
    return [run_check(check, workspace) for check in case.get("checks", [])]


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
    → ``collect_metrics`` → ``run_checks`` → ``build_result``. The
    pre-refactor result shape is preserved (Section 2 of Phase 1
    PLAN — pure code motion, no behavior change).
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
        # Subprocess raised an exception — short-circuit metrics +
        # checks to match the pre-refactor early-return path.
        return build_result(case, workspace, invocation, {}, [])
    metrics = collect_metrics(workspace)
    checks = run_checks(case, workspace, invocation)
    return build_result(case, workspace, invocation, metrics, checks)


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

    if ctype == "glob_exists":
        pattern = os.path.join(workspace, check["pattern"])
        matches = globmod.glob(pattern, recursive=True)
        return {
            "type": ctype,
            "pattern": check["pattern"],
            "passed": len(matches) > 0,
            "detail": f"found {len(matches)}: {[os.path.relpath(m, workspace) for m in matches[:5]]}" if matches else "no matches",
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

    print(f"Running {len(cases)} prompt case(s)...\n", file=sys.stderr)

    results = []
    for i, case in enumerate(cases, 1):
        label = case["goal"][:70]
        print(f"[{i}/{len(cases)}] {case['name']}: {label}...",
              file=sys.stderr)
        result = run_case(case, verbose=args.verbose, local=args.local,
                          engine=engine)
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
