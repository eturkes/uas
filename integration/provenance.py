"""Reproducibility metadata helpers shared by eval and orchestrator.

Lifted from ``integration/eval.py`` in Phase 3 §1. Eval continues to
stamp ``HARNESS_VERSION`` only, preserving its existing JSONL schema;
orchestrator callers opt into the additional ``ORCHESTRATOR_VERSION``
field via ``capture_run_metadata(include_orchestrator_version=True)``.
"""

import datetime
import hashlib
import importlib.util
import json
import os
import re
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

# Bumped manually when eval.py's output schema changes. Stamped on
# every JSONL row.
HARNESS_VERSION = "phase1"

# Bumped manually when the orchestrator's persisted-event schema
# changes. Stamped on rows the orchestrator writes when callers pass
# include_orchestrator_version=True; eval rows do not include it.
ORCHESTRATOR_VERSION = "phase3"

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


def capture_run_metadata(*, include_orchestrator_version: bool = False) -> dict:
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
    - ``orchestrator_version`` (only when ``include_orchestrator_version
      =True``): ``ORCHESTRATOR_VERSION`` constant. Eval rows omit this
      key so the existing JSONL schema is preserved bit-for-bit.

    Eval's persistence layer stamps this dict onto every JSONL row so
    any benchmark line can be traced to a specific commit and config
    state. The orchestrator stamps it on every persisted task event
    for the same reason.
    """
    git_porcelain = _git_capture(["status", "--porcelain"], default="")
    env_snapshot = {
        k: v
        for k, v in os.environ.items()
        if k.startswith("UAS_") and not _SECRET_ENV_PATTERN.search(k)
    }
    md = {
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
    if include_orchestrator_version:
        md["orchestrator_version"] = ORCHESTRATOR_VERSION
    return md
