#!/usr/bin/env python3
"""LLM-as-judge for open-ended UAS eval cases.

Phase 1 PLAN Section 8 deliverable. Used by ``integration/eval.py``'s
``llm_judge`` check type to grade Tier 3 (``open_ended``) cases where
deterministic checks cannot judge success.

Sends a workspace listing + criteria + goal to Claude N times in
parallel and returns a majority vote. Results are cached at
``integration/.judge_cache.json`` keyed by
``(case_name, criteria_sha256, workspace_content_sha256)`` so re-running
the same eval does not re-pay the LLM cost when nothing changed.
The workspace content hash is mandatory: re-runs that produced
different code do not falsely cache.

Self-contained: no imports from ``architect/`` or ``orchestrator/``.
The only non-stdlib dependency is the ``anthropic`` SDK (already in
``requirements.txt``).
"""

import concurrent.futures
import hashlib
import json
import math
import os
import re

# Default model and sample count. Both overridable per call.
DEFAULT_MODEL = "claude-opus-4-6"
DEFAULT_SAMPLES = 5

# Per-file content budget (chars). Files larger than this are
# truncated and TRUNCATED_SENTINEL is appended.
PER_FILE_BUDGET = 20_000

# Total content budget (chars) across all files. Once exceeded, the
# remaining files are dropped and TRUNCATED_SENTINEL is appended.
TOTAL_BUDGET = 200_000

# Auto-discovery extension allowlist (case-insensitive). Matches the
# PLAN §8 spec verbatim. Used when ``files=None`` is passed to
# ``judge()``.
DISCOVERY_EXTENSIONS = (".py", ".md", ".json", ".txt", ".csv")

# Sentinel emitted by both per-file and total-budget truncation so
# the judge knows when content was elided.
TRUNCATED_SENTINEL = "[truncated]"

# Directories the workspace walker always skips. Hidden state from
# the architect / git / Python tooling, never useful to a judge.
_WORKSPACE_SKIP_DIRS = frozenset({
    ".git", ".uas_state", ".uas_goals",
    "__pycache__", ".pytest_cache", ".ruff_cache",
})

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
JUDGE_CACHE_PATH = os.path.join(SCRIPT_DIR, ".judge_cache.json")

JUDGE_PROMPT_TEMPLATE = """\
You are an impartial judge evaluating whether an autonomous coding
agent's work satisfies a stated goal and explicit success criteria.

Be strict but fair: reply "pass" iff the criteria are clearly met by
the artefacts in the workspace. Otherwise reply "fail".

GOAL:
{goal}

SUCCESS CRITERIA:
{criteria}

WORKSPACE FILES:
{files}

Reply with a single JSON object on its own line, no markdown fences,
no surrounding prose:
{{"verdict": "pass" | "fail", "reason": "<one to three sentences>"}}
"""


def _walk_workspace(workspace, extensions=DISCOVERY_EXTENSIONS):
    """Yield (relpath, abspath) pairs for matching files in deterministic order.

    Skips hidden state directories (``.git``, ``.uas_state``,
    ``.uas_goals``, ``__pycache__``, ``.pytest_cache``, ``.ruff_cache``).
    Returns a sorted list so repeat invocations are reproducible
    (the workspace hash and the prompt builder both depend on it).
    """
    matched = []
    if not os.path.isdir(workspace):
        return matched
    ext_set = {e.lower() for e in extensions}
    for root, dirs, files in os.walk(workspace):
        # In-place sort + filter so os.walk is both deterministic and
        # skips the hidden state dirs.
        dirs[:] = sorted(d for d in dirs if d not in _WORKSPACE_SKIP_DIRS)
        for f in sorted(files):
            ext = os.path.splitext(f)[1].lower()
            if ext in ext_set:
                abspath = os.path.join(root, f)
                relpath = os.path.relpath(abspath, workspace)
                matched.append((relpath, abspath))
    matched.sort()
    return matched


def _read_truncated(abspath, per_file_budget=PER_FILE_BUDGET):
    """Read a file as text, truncating to ``per_file_budget`` chars.

    Reads ``per_file_budget + 1`` chars to detect overflow without
    materialising the whole file. Errors are swallowed into a
    ``[unreadable: …]`` marker so a single bad file does not abort
    the whole prompt build.
    """
    try:
        with open(abspath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(per_file_budget + 1)
    except OSError as e:
        return f"[unreadable: {e}]"
    if len(content) > per_file_budget:
        content = content[:per_file_budget] + "\n" + TRUNCATED_SENTINEL
    return content


def build_workspace_listing(workspace, files=None,
                            per_file_budget=PER_FILE_BUDGET,
                            total_budget=TOTAL_BUDGET):
    """Assemble the workspace listing string for the judge prompt.

    If ``files`` is ``None``: auto-discover via ``_walk_workspace``
    using ``DISCOVERY_EXTENSIONS``.
    If ``files`` is a list: include exactly those workspace-relative
    paths. Any missing path is recorded with a ``[file not found]``
    marker so the judge can see the absence (a frequent failure mode
    for open-ended tasks is "agent forgot to create the file").

    Per-file content is truncated to ``per_file_budget`` chars.
    Cumulative content is truncated to ``total_budget`` chars; once
    exceeded, the remaining files are dropped and a closing
    ``TRUNCATED_SENTINEL`` is emitted.
    """
    if files is None:
        entries = _walk_workspace(workspace)
    else:
        entries = []
        for f in files:
            abspath = os.path.join(workspace, f)
            entries.append((f, abspath if os.path.isfile(abspath) else None))

    if not entries:
        return "(no matching files in workspace)"

    chunks = []
    used = 0
    for relpath, abspath in entries:
        if abspath is None:
            block = f"--- {relpath} ---\n[file not found]"
        else:
            body = _read_truncated(abspath, per_file_budget)
            block = f"--- {relpath} ---\n{body}"
        # +1 accounts for the joining newline between chunks.
        if used + len(block) + 1 > total_budget:
            chunks.append(TRUNCATED_SENTINEL)
            break
        chunks.append(block)
        used += len(block) + 1
    return "\n".join(chunks)


def _build_prompt(case_goal, criteria, workspace_listing):
    return JUDGE_PROMPT_TEMPLATE.format(
        goal=case_goal,
        criteria=criteria,
        files=workspace_listing,
    )


_JSON_OBJ_RE = re.compile(r"\{[^{}]*\}", flags=re.DOTALL)


def _parse_verdict(text):
    """Extract a ``(passed_bool, reason_str)`` pair from a model response.

    Looks for a JSON object containing a ``verdict`` key. Tolerant of
    surrounding prose: scans every ``{...}`` substring, takes the
    last one that parses and contains ``verdict``. Returns
    ``(False, "unparseable: <snippet>")`` on failure so the majority
    vote treats unparseable responses as fails.
    """
    candidates = _JSON_OBJ_RE.findall(text)
    for candidate in reversed(candidates):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "verdict" in obj:
            verdict = str(obj["verdict"]).strip().lower()
            reason = str(obj.get("reason", "")).strip()
            return verdict == "pass", reason
    snippet = text.strip().replace("\n", " ")[:200]
    return False, f"unparseable: {snippet}"


def _load_oauth_token():
    """Read the Claude Code OAuth access token from ``.uas_auth``.

    Returns ``None`` if the credentials file is missing, malformed,
    or does not contain a token. The token has an ``expiresAt``
    timestamp; refreshing is the user's responsibility (re-run
    ``claude`` to mint a fresh one).
    """
    creds_path = os.path.join(
        os.path.dirname(SCRIPT_DIR), ".uas_auth", ".credentials.json"
    )
    if not os.path.isfile(creds_path):
        return None
    try:
        with open(creds_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return (data.get("claudeAiOauth") or {}).get("accessToken")


def _call_anthropic(model, prompt):
    """Single SDK seam: send one prompt to Claude, return the raw text.

    Two auth paths, tried in order:

    1. ``ANTHROPIC_API_KEY`` env var → use the official Anthropic
       Python SDK (``Anthropic()`` constructor reads the env var).
    2. OAuth bearer token at ``.uas_auth/.credentials.json`` → POST
       directly to ``api.anthropic.com/v1/messages`` with the
       ``Authorization: Bearer`` header and the
       ``anthropic-beta: oauth-2025-04-20`` flag. This is the path
       that works for Claude Max subscribers without an API key.

    Both paths are imported lazily so the rest of the module remains
    importable in test environments that have neither.

    Tests monkey-patch this function rather than the SDK or httpx so
    no real auth is needed for ``pytest tests/``.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(
            b.text for b in msg.content if hasattr(b, "text")
        )
    oauth = _load_oauth_token()
    if oauth:
        import httpx
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Authorization": f"Bearer {oauth}",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "oauth-2025-04-20",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=120,
        )
        r.raise_for_status()
        data = r.json()
        return "".join(
            b.get("text", "") for b in (data.get("content") or [])
            if b.get("type") == "text"
        )
    raise RuntimeError(
        "no Anthropic credentials available: set ANTHROPIC_API_KEY or "
        "ensure .uas_auth/.credentials.json holds a valid OAuth token"
    )


def _call_one_sample(model, prompt):
    """Run a single judge sample: call SDK, parse verdict.

    Returns ``(passed_bool, reason_str)``. On any exception inside the
    SDK call, returns ``(False, f"call error: …")`` so a flaky network
    cannot crash the whole judge.
    """
    try:
        text = _call_anthropic(model, prompt)
    except Exception as e:
        return False, f"call error: {type(e).__name__}: {e}"
    return _parse_verdict(text)


def _hash_workspace_content(workspace):
    """Stable SHA-256 of ``(relpath, file bytes)`` over matching files.

    Uses the same ``_walk_workspace`` ordering as the prompt builder so
    cache keys and prompts agree on what counts as "the workspace".
    Files outside ``DISCOVERY_EXTENSIONS`` are not hashed; that means a
    silent change to a ``.html`` file will not invalidate the cache.
    Acceptable for Phase 1 — open-ended cases that need other extensions
    pass them via the ``files=`` arg, which still routes through the
    workspace path so the hash captures their bytes.
    """
    h = hashlib.sha256()
    if not os.path.isdir(workspace):
        return h.hexdigest()
    for relpath, abspath in _walk_workspace(workspace):
        h.update(relpath.encode("utf-8"))
        h.update(b"\0")
        try:
            with open(abspath, "rb") as f:
                h.update(f.read())
        except OSError:
            pass
        h.update(b"\0")
    return h.hexdigest()


def _build_cache_key(case_name, criteria, workspace_hash):
    """Cache key: ``case_name | sha256(criteria) | workspace_hash``.

    ``|`` is safe as a separator because SHA-256 hex output and the
    case name (``[a-z0-9-]``) never contain it.
    """
    crit_hash = hashlib.sha256(criteria.encode("utf-8")).hexdigest()
    return "|".join([case_name or "", crit_hash, workspace_hash])


def _load_cache(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_cache(cache, path):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, sort_keys=True)
    except OSError:
        pass


def judge(case_goal, workspace, criteria, *, files=None,
          samples=DEFAULT_SAMPLES, model=DEFAULT_MODEL,
          case_name=None, cache_path=None, use_cache=True):
    """Run N parallel judge calls and return the majority verdict.

    Parameters
    ----------
    case_goal : str
        The original task goal handed to the architect.
    workspace : str
        Workspace directory the architect produced.
    criteria : str
        Explicit success criteria from the check definition.
    files : list[str] | None
        Workspace-relative paths to include verbatim. ``None``
        triggers auto-discovery via ``DISCOVERY_EXTENSIONS``.
    samples : int
        Number of parallel judge calls. Defaults to ``DEFAULT_SAMPLES``
        (5).
    model : str
        Anthropic model id. Defaults to ``DEFAULT_MODEL``
        (``claude-opus-4-6``).
    case_name : str | None
        Used as part of the cache key.
    cache_path : str | None
        Override ``JUDGE_CACHE_PATH``. Useful for tests.
    use_cache : bool
        Disable caching for tests / debugging.

    Returns
    -------
    dict
        Keys: ``passed`` (bool), ``votes`` (list[bool]), ``reasons``
        (list[str]), ``majority`` (float in [0, 1]), ``cached``
        (bool), ``samples_used`` (int), ``case_name`` (str | None).

        ``passed`` is True iff at least ``ceil(samples / 2)`` votes
        returned ``"pass"``.
    """
    if cache_path is None:
        cache_path = JUDGE_CACHE_PATH

    workspace_hash = _hash_workspace_content(workspace)
    key = _build_cache_key(case_name, criteria, workspace_hash)

    if use_cache:
        cache = _load_cache(cache_path)
        if key in cache:
            entry = cache[key]
            return {**entry, "cached": True}

    workspace_listing = build_workspace_listing(workspace, files=files)
    prompt = _build_prompt(case_goal, criteria, workspace_listing)

    votes = [False] * samples
    reasons = [""] * samples
    if samples > 0:
        with concurrent.futures.ThreadPoolExecutor(max_workers=samples) as ex:
            future_to_index = {
                ex.submit(_call_one_sample, model, prompt): i
                for i in range(samples)
            }
            for fut in concurrent.futures.as_completed(future_to_index):
                i = future_to_index[fut]
                try:
                    passed_v, reason = fut.result()
                except Exception as e:
                    passed_v, reason = False, f"executor error: {e}"
                votes[i] = passed_v
                reasons[i] = reason

    pass_count = sum(1 for v in votes if v)
    threshold = math.ceil(samples / 2) if samples else 1
    majority = (pass_count / samples) if samples else 0.0
    result = {
        "passed": pass_count >= threshold and samples > 0,
        "votes": votes,
        "reasons": reasons,
        "majority": majority,
        "cached": False,
        "samples_used": samples,
        "case_name": case_name,
    }

    # A transient SDK exception (auth 401, network timeout, executor
    # crash) is not a real judge verdict. Caching it would poison every
    # subsequent run against an identical workspace, which is exactly how
    # Phase 1 §10 run_b's Tier 3 cases went stuck-fail. Skip the write
    # whenever any sample carries an infrastructure error prefix; the
    # next invocation will re-call and try to get a real answer.
    has_infra_errors = any(
        isinstance(r, str) and (
            r.startswith("call error:") or r.startswith("executor error:")
        )
        for r in reasons
    )
    if use_cache and not has_infra_errors:
        cache = _load_cache(cache_path)
        # Store everything but the per-call ``cached`` flag — it's a
        # presentation field set fresh on every retrieval.
        cache[key] = {k: v for k, v in result.items() if k != "cached"}
        _save_cache(cache, cache_path)

    return result
