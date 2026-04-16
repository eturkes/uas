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

# Bumped manually when eval.py's output schema changes. Stamped on
# every JSONL row in Section 5 so old logs can be migrated or skipped.
HARNESS_VERSION = "phase1"

# Secret-suffix filter for env_snapshot in capture_run_metadata().
# Anchored to end-of-string so legitimate names like UAS_KEY_NAME
# are not falsely filtered.
_SECRET_ENV_PATTERN = re.compile(
    r"(_TOKEN|_KEY|_SECRET|_PASSWORD)$", re.IGNORECASE
)


def _git_capture(args, default="unknown"):
    """Run a git command from REPO_ROOT and return stripped stdout.

    Returns ``default`` on any failure (no git binary, not a repo,
    timeout, non-zero exit). Used by ``capture_run_metadata`` so a
    bad git environment never crashes the eval.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", REPO_ROOT, *args],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return default
    if proc.returncode != 0:
        return default
    return proc.stdout.strip()


def _hash_active_config():
    """Compute SHA-256 of the canonicalised JSON dump of uas_config.load_config().

    Loaded via ``importlib.util`` from ``REPO_ROOT/uas_config.py`` so
    eval.py can run from a checkout where ``uas_config`` is not
    importable via the normal sys.path. Returns ``"unavailable"`` on
    any error.
    """
    import hashlib
    import importlib.util
    config_path = os.path.join(REPO_ROOT, "uas_config.py")
    if not os.path.isfile(config_path):
        return "unavailable"
    try:
        spec = importlib.util.spec_from_file_location(
            "uas_config", config_path
        )
        if spec is None or spec.loader is None:
            return "unavailable"
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cfg = mod.load_config()
        canonical = json.dumps(cfg, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    except Exception:
        return "unavailable"


def capture_run_metadata() -> dict:
    """Capture per-invocation reproducibility metadata.

    Returns a dict with the following keys:

    - ``git_sha``: full SHA from ``git rev-parse HEAD``, or
      ``"unknown"``.
    - ``git_branch``: ``git rev-parse --abbrev-ref HEAD`` output, or
      ``"unknown"``.
    - ``git_dirty``: True iff ``git status --porcelain`` is non-empty.
    - ``timestamp_utc``: ISO-8601 UTC timestamp at the moment of
      capture.
    - ``env_snapshot``: dict of every ``UAS_*`` env var set in
      ``os.environ`` at capture time, with secret-suffixed keys
      (``_TOKEN``, ``_KEY``, ``_SECRET``, ``_PASSWORD``, case-
      insensitive) filtered out. ``ANTHROPIC_API_KEY`` is excluded
      implicitly because it does not match the ``UAS_*`` prefix.
    - ``config_hash``: SHA-256 hex of the canonicalised JSON dump of
      ``uas_config.load_config()``, or ``"unavailable"``.
    - ``harness_version``: ``HARNESS_VERSION`` constant.

    Section 5's persistence layer stamps this dict onto every JSONL
    row so any benchmark line can be traced to a specific commit and
    config state.
    """
    import datetime
    git_porcelain = _git_capture(["status", "--porcelain"], default="")
    env_snapshot = {
        k: v
        for k, v in os.environ.items()
        if k.startswith("UAS_") and not _SECRET_ENV_PATTERN.search(k)
    }
    return {
        "git_sha": _git_capture(["rev-parse", "HEAD"]),
        "git_branch": _git_capture(["rev-parse", "--abbrev-ref", "HEAD"]),
        "git_dirty": bool(git_porcelain),
        "timestamp_utc": datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat(),
        "env_snapshot": env_snapshot,
        "config_hash": _hash_active_config(),
        "harness_version": HARNESS_VERSION,
    }


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

# Default model used by the eval harness for the architect subprocess.
# Project policy (set during Phase 1 Section 10): the eval / measurement
# instrument runs on Haiku 4.5 to keep dev iteration cheap and to
# preserve the user's weekly Opus quota for real-world UAS task
# execution. Real-world UAS use (architect/orchestrator invoked
# directly, outside this harness) is unaffected and continues to use
# whatever uas_config / UAS_MODEL the user has set. Override here by
# exporting UAS_MODEL (or UAS_MODEL_PLANNER / UAS_MODEL_CODER) before
# invoking uas-eval.
EVAL_MODEL_DEFAULT = "claude-haiku-4-5-20251001"
EVAL_MODEL_ENV_VARS = ("UAS_MODEL", "UAS_MODEL_PLANNER", "UAS_MODEL_CODER")

# OAuth token refresh.  Claude Max OAuth tokens last ~8 hours.
# Long benchmark runs (--runs 3 over 35 cases is ~30 hours on Haiku)
# outlive a single token cycle.  Four-stage fallback ensures the eval
# can survive multi-day runs even as a detached background process:
#   1. Self-refresh: exchange the eval token's own refresh_token at
#      the Anthropic OAuth endpoint — no external dependency.
#   2. Borrow from ~/.claude/ if it has a valid token.
#   3. Force-refresh ~/.claude/ via ``claude -p ping``, then borrow.
#   4. Give up and log the failure.
_OAUTH_REFRESH_BUFFER = 3600  # seconds — refresh when < 1 hour left
_DEFAULT_CLAUDE_CREDS = os.path.expanduser("~/.claude/.credentials.json")
_OAUTH_TOKEN_ENDPOINT = "https://console.anthropic.com/v1/oauth/token"
_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


def _read_token_expiry(creds_path):
    """Return seconds remaining on the OAuth access token, or 0."""
    try:
        with open(creds_path) as f:
            creds = json.load(f)
        exp = creds.get("claudeAiOauth", {}).get("expiresAt", 0) / 1000
        return max(0.0, exp - time.time())
    except Exception:
        return 0.0


def _self_refresh_oauth(creds_path):
    """Exchange the refresh token in *creds_path* for a new access token.

    Hits the Anthropic OAuth token endpoint directly — no CLI, no
    interactive session, works in detached ``nohup`` processes.
    Returns True on success, False on any failure.
    """
    try:
        with open(creds_path) as f:
            creds = json.load(f)
        oauth = creds.get("claudeAiOauth", {})
        refresh_token = oauth.get("refreshToken")
        if not refresh_token:
            print("  [oauth] Self-refresh skip: no refreshToken in creds",
                  file=sys.stderr)
            return False
        import httpx  # urllib.request hits Cloudflare 1010
        resp = httpx.post(
            _OAUTH_TOKEN_ENDPOINT,
            json={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": _OAUTH_CLIENT_ID,
            },
            headers={
                "User-Agent": "claude-code/1.0",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            body_snippet = resp.text[:300].replace("\n", " ")
            print(f"  [oauth] Self-refresh HTTP {resp.status_code}: "
                  f"{body_snippet}", file=sys.stderr)
            return False
        body = resp.json()
        new_access = body.get("access_token")
        new_refresh = body.get("refresh_token")
        expires_in = body.get("expires_in", 28800)
        if not new_access:
            print("  [oauth] Self-refresh response missing access_token",
                  file=sys.stderr)
            return False
        oauth["accessToken"] = new_access
        if new_refresh:
            oauth["refreshToken"] = new_refresh
        oauth["expiresAt"] = int((time.time() + expires_in) * 1000)
        creds["claudeAiOauth"] = oauth
        with open(creds_path, "w") as f:
            json.dump(creds, f)
        return True
    except Exception as e:
        print(f"  [oauth] Self-refresh exception: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return False


def _maybe_refresh_oauth():
    """Ensure eval-harness OAuth token has at least 1 hour of life.

    Called between cases by the main loop.  Four-stage fallback:

    1. Eval token (``UAS_AUTH_DIR``) valid for >1 hour → no-op.
    2. Self-refresh: exchange the eval token's own ``refreshToken``
       at the Anthropic OAuth endpoint.  Works in detached processes.
    3. Default token (``~/.claude/``) valid for >1 hour → copy it.
    4. Default token also near expiry → call ``claude -p ping`` to
       refresh it, then copy.
    """
    eval_creds = os.path.join(UAS_AUTH_DIR, ".credentials.json")
    remaining = _read_token_expiry(eval_creds)
    if remaining > _OAUTH_REFRESH_BUFFER:
        return  # plenty of time

    # Stage 2: self-refresh using the refresh token.
    if _self_refresh_oauth(eval_creds):
        new_rem = _read_token_expiry(eval_creds)
        print(f"  [oauth] Self-refreshed — {new_rem/3600:.1f}h "
              f"remaining", file=sys.stderr)
        return

    # Stage 3: borrow from ~/.claude/
    default_remaining = _read_token_expiry(_DEFAULT_CLAUDE_CREDS)
    if default_remaining <= _OAUTH_REFRESH_BUFFER:
        # Stage 4: force-refresh ~/.claude/ via claude -p
        claude_path = shutil.which("claude")
        if not claude_path:
            print("  [oauth] Token expiring, claude CLI not found",
                  file=sys.stderr)
            return
        try:
            proc = subprocess.run(
                [claude_path, "-p", "ping",
                 "--model", EVAL_MODEL_DEFAULT],
                capture_output=True, text=True,
                timeout=120, stdin=subprocess.DEVNULL,
            )
            if proc.returncode != 0:
                print(f"  [oauth] CLI refresh failed (exit "
                      f"{proc.returncode}): "
                      f"{proc.stderr[:200]}", file=sys.stderr)
                return
        except Exception as e:
            print(f"  [oauth] CLI refresh error: {e}",
                  file=sys.stderr)
            return
        default_remaining = _read_token_expiry(_DEFAULT_CLAUDE_CREDS)

    # Copy valid default credentials into eval auth dir.
    if default_remaining > _OAUTH_REFRESH_BUFFER:
        try:
            shutil.copy2(_DEFAULT_CLAUDE_CREDS, eval_creds)
            new_rem = _read_token_expiry(eval_creds)
            print(f"  [oauth] Token refreshed — {new_rem/3600:.1f}h "
                  f"remaining", file=sys.stderr)
        except Exception as e:
            print(f"  [oauth] Copy failed: {e}", file=sys.stderr)
    else:
        print("  [oauth] Could not obtain valid token",
              file=sys.stderr)


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
            #
            # Forward every ``UAS_*`` env var the parent process has
            # set into the container so user-level configuration
            # (notably ``UAS_MODEL``) reaches the architect. The
            # per-case overrides below (``UAS_GOAL``, ``UAS_WORKSPACE``,
            # ``UAS_OUTPUT``) win because they are applied after the
            # parent forward.
            container_env = {
                k: v
                for k, v in os.environ.items()
                if k.startswith("UAS_")
            }
            container_env.update({
                "UAS_GOAL": case["goal"],
                "UAS_WORKSPACE": "/workspace",
                "UAS_OUTPUT": "/workspace/output.json",
                "PYTHONPATH": "/uas",
            })
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
            # Use the image's default entrypoint (entrypoint.sh). It
            # detects non-interactive mode via UAS_GOAL, runs
            # ``python3 -P -m architect.main``, and on EXIT its trap
            # chowns /workspace to UAS_HOST_UID:UAS_HOST_GID — the
            # standard project pattern that other shell wrappers
            # (install.sh, quick_test.sh, start_orchestrator.sh,
            # run_container.sh) all use. Without this, the architect
            # subprocess runs as root inside the container and leaves
            # root-owned files in the host workspace dir, which
            # subsequently breaks ``setup_workspace``'s rmtree.
            cmd.append(IMAGE_TAG)
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

    ``invocation`` is threaded through so check types like ``exit_code``
    can read the architect's return code without re-invoking the
    subprocess. ``case`` is threaded through so check types like
    ``llm_judge`` can read the case goal and name for prompt assembly
    and cache keying.
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


def _import_llm_judge():
    """Lazy-import the ``judge`` callable from the sibling llm_judge module.

    Handles three invocation contexts in order of preference:

    1. ``llm_judge`` already in ``sys.modules`` (tests import it
       directly via ``sys.path`` injection — reuse that instance so
       monkey-patches the test set up still apply).
    2. ``python3 -m integration.eval`` (Phase 1 §10's planned
       wrapper) — the package context exists, the sibling import
       succeeds.
    3. ``python3 integration/eval.py`` — no package context; load by
       absolute file location and cache into ``sys.modules`` so
       subsequent calls reuse the same instance.
    """
    if "llm_judge" in sys.modules:
        return sys.modules["llm_judge"].judge
    try:
        from integration.llm_judge import judge as judge_fn
        return judge_fn
    except ImportError:
        pass
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "llm_judge",
        os.path.join(SCRIPT_DIR, "llm_judge.py"),
    )
    if spec is None or spec.loader is None:
        raise ImportError("cannot locate integration/llm_judge.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["llm_judge"] = mod
    spec.loader.exec_module(mod)
    return mod.judge


def run_check(check, workspace, invocation=None, case=None):
    """Run a single check against the workspace.

    ``invocation`` is the dict returned by ``invoke_architect`` and is
    consumed by check types that need the architect subprocess result
    (currently only ``exit_code``). It is optional so the function can
    be called from tests with synthetic data.

    ``case`` is the full case dict (name, goal, checks, …). It is
    threaded through by ``run_checks`` and consumed by check types
    that need case-level metadata (currently only ``llm_judge``).
    Optional for the same testing reason.

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

    ``llm_judge``
        Required: ``criteria`` (str — explicit success criteria the
        judge prompt is built around). Optional: ``files`` (list of
        workspace-relative paths to include verbatim in the prompt;
        defaults to auto-discovery of every ``.py``, ``.md``,
        ``.json``, ``.txt``, ``.csv`` file under the workspace),
        ``samples`` (int, default 5), ``model`` (default
        ``claude-opus-4-6``). Calls ``integration/llm_judge.judge``
        with N parallel samples and majority-votes the result.
        Requires ``case`` to be passed in (the orchestrator does this
        automatically via ``run_checks``).
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

    if ctype == "llm_judge":
        criteria = check.get("criteria")
        if not criteria:
            return {
                "type": ctype, "passed": False,
                "detail": "llm_judge check requires 'criteria' field",
            }
        if case is None:
            return {
                "type": ctype, "passed": False,
                "detail": "llm_judge check requires case context",
            }
        try:
            judge_fn = _import_llm_judge()
        except Exception as e:
            return {
                "type": ctype, "passed": False,
                "detail": f"llm_judge import failed: {e}",
            }
        files = check.get("files")
        samples = check.get("samples", 5)
        model = check.get("model", "claude-opus-4-6")
        try:
            result = judge_fn(
                case_goal=case.get("goal", ""),
                workspace=workspace,
                criteria=criteria,
                files=files,
                samples=samples,
                model=model,
                case_name=case.get("name"),
            )
        except Exception as e:
            return {
                "type": ctype, "passed": False,
                "detail": f"judge error: {type(e).__name__}: {e}",
            }
        votes = result.get("votes") or []
        pass_votes = sum(1 for v in votes if v)
        detail_parts = [
            f"majority={result.get('majority', 0.0):.2f}",
            f"votes={pass_votes}/{result.get('samples_used', len(votes))}",
        ]
        if result.get("cached"):
            detail_parts.append("(cached)")
        reasons = result.get("reasons") or []
        if reasons and reasons[0]:
            detail_parts.append(f"reason: {reasons[0][:120]}")
        return {
            "type": ctype,
            "passed": bool(result.get("passed")),
            "detail": "; ".join(detail_parts),
            "majority": result.get("majority", 0.0),
            "votes": votes,
            "samples_used": result.get("samples_used", len(votes)),
            "cached": bool(result.get("cached")),
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
    # Project policy: default the eval harness to Haiku for cost.
    # Done before argparse / metadata capture so the env_snapshot
    # captured into the JSONL log accurately reflects the model the
    # architect actually saw. ``setdefault`` semantics — any value
    # already set in the parent shell wins.
    for _key in EVAL_MODEL_ENV_VARS:
        if _key not in os.environ:
            os.environ[_key] = EVAL_MODEL_DEFAULT

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
    _default_runs = int(os.environ.get("UAS_EVAL_RUNS", "3"))
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
