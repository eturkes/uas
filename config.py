"""Layered configuration system for UAS.

Merges four layers (later overrides earlier):

1. Built-in defaults (hardcoded)
2. User-global config: ``~/.config/uas/config.toml``
3. Project-level config: ``{workspace}/.uas/config.toml``
4. ``UAS_*`` environment variables (checked live at access time)

Config keys use snake_case matching env var names without the ``UAS_`` prefix
(e.g. ``max_parallel``, ``sandbox_mode``, ``model``).
"""

import logging
import os

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Built-in defaults (layer 1)
# ---------------------------------------------------------------------------

DEFAULTS: dict = {
    # Core
    "workspace": "/workspace",
    "model": "",
    "model_planner": "",
    "model_coder": "",
    "sandbox_mode": "container",

    # Execution
    "max_parallel": 0,
    "max_context_length": 0,
    "max_error_length": 0,
    "llm_timeout": None,

    # Retry
    "persistent_retry": True,
    "rate_limit_wait": 120,
    "rate_limit_max_wait": 600,
    "rate_limit_retries": 3,
    "usage_limit_wait": 3600,
    "usage_limit_retries": 5,
    "best_of_n": 1,

    # Retention
    "keep_last_runs": 10,
    "max_run_age_days": 30,

    # Fuzzy functions
    "fuzzy_enabled": True,

    # TDD enforcement
    "tdd_enforce": True,

    # Context janitor (post-edit formatting)
    "context_janitor": {
        "formatter": "ruff",  # "ruff" | "black" | "none"
    },

    # Flags
    "minimal": False,
    "verbose": False,
    "dry_run": False,
    "explain": False,
    "resume": False,
    "no_llm_guardrails": False,

    # Invocation / runtime (env-var only in practice)
    "goal": "",
    "goal_file": "",
    "task": "",
    "output": "",
    "report": "",
    "trace": "",
    "events": "",
    "run_id": "",
    "step_id": "",
    "spec_attempt": 0,
    "workspace_files": "",
    "step_environment": "",
    "step_spec": "",
    "host_workspace": "",
    "truncation_detected": "",
    "test_files": "",
}

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

_config: dict = {}
_loaded: bool = False

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SENTINEL = object()


def _resolve_dotted(d: dict, key: str) -> object:
    """Resolve a dotted key path against *d*; return ``_SENTINEL`` if missing."""
    val: object = d
    for part in key.split("."):
        if not isinstance(val, dict) or part not in val:
            return _SENTINEL
        val = val[part]
    return val


def _coerce(value: str, reference: object) -> object:
    """Coerce an env-var string to match the type of *reference*."""
    if isinstance(reference, bool):
        return value.lower() in ("1", "true", "yes")
    if isinstance(reference, int):
        try:
            return int(value)
        except (ValueError, TypeError):
            return reference
    if isinstance(reference, float):
        try:
            return float(value)
        except (ValueError, TypeError):
            return reference
    # str or None reference -> return string as-is
    return value


def _merge_toml(target: dict, path: str) -> None:
    """Merge a TOML file into *target* (in-place). No-op if file missing."""
    if tomllib is None or not os.path.isfile(path):
        return
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        target.update(data)
    except Exception as exc:
        logger.warning("Failed to load config from %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_config(workspace: str | None = None) -> dict:
    """Load and merge config layers 1-3.

    Env vars (layer 4) are checked live at access time via :func:`get` so
    that test monkeypatching and subprocess env inheritance work correctly.

    Returns the merged dict (layers 1-3 only).
    """
    global _config, _loaded

    merged = dict(DEFAULTS)

    # Layer 2: user-global
    user_path = os.path.expanduser("~/.config/uas/config.toml")
    _merge_toml(merged, user_path)

    # Layer 3: project-level
    ws = workspace or os.environ.get("UAS_WORKSPACE", "/workspace")
    project_path = os.path.join(ws, ".uas", "config.toml")
    _merge_toml(merged, project_path)

    _config = merged
    _loaded = True
    return merged


def get(key: str, default: object = _SENTINEL) -> object:
    """Get a config value.

    Priority: ``UAS_<KEY>`` env var > project TOML > user TOML > built-in
    default.  Env vars are checked at call time (not cached).

    Dotted keys (e.g. ``"context_janitor.formatter"``) walk nested TOML
    tables; the env-var equivalent uses underscores
    (``UAS_CONTEXT_JANITOR_FORMATTER``).
    """
    if not _loaded:
        load_config()

    # Layer 4: env var (always live)
    env_key = f"UAS_{key.upper().replace('.', '_')}"
    env_val = os.environ.get(env_key)
    if env_val is not None:
        ref = _resolve_dotted(_config, key)
        if ref is _SENTINEL:
            ref = _resolve_dotted(DEFAULTS, key)
        if ref is _SENTINEL:
            ref = ""
        return _coerce(env_val, ref)

    # Layers 1-3 (merged at load time)
    if "." in key:
        val = _resolve_dotted(_config, key)
        if val is not _SENTINEL:
            return val
        if default is not _SENTINEL:
            return default
        fallback = _resolve_dotted(DEFAULTS, key)
        return None if fallback is _SENTINEL else fallback

    if key in _config:
        return _config[key]

    if default is not _SENTINEL:
        return default
    return DEFAULTS.get(key)
