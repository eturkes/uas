"""Claude Max OAuth helpers shared by eval and orchestrator.

Lifted from ``integration/eval.py`` in Phase 3 §1 per
``docs/substrate.md`` §2 ("Phase 3 should expose this as a config-
controlled value with a warning if it drifts from the
``~/.claude/.credentials.json`` metadata"). Both consumers import the
private-prefixed names directly so the existing internal call sites
and monkeypatch targets keep working unchanged.
"""

import json
import os
import shutil
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
UAS_AUTH_DIR = os.path.join(REPO_ROOT, ".uas_auth")

_OAUTH_REFRESH_BUFFER = 3600  # seconds — refresh when < 1 hour left
_DEFAULT_CLAUDE_CREDS = os.path.expanduser("~/.claude/.credentials.json")
_OAUTH_TOKEN_ENDPOINT = "https://console.anthropic.com/v1/oauth/token"
_OAUTH_CLIENT_ID_DEFAULT = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_OAUTH_CLIENT_ID = os.environ.get(
    "UAS_OAUTH_CLIENT_ID", _OAUTH_CLIENT_ID_DEFAULT,
)

# Fires the divergence warning at most once per process so a long
# orchestrator session does not flood stderr.
_CLIENT_ID_DIVERGENCE_CHECKED = False


def _check_client_id_divergence() -> None:
    """Warn once if the default-creds metadata pins a different client id.

    Some Claude Code releases embed the OAuth client id in
    ``~/.claude/.credentials.json`` under ``claudeAiOauth.clientId``
    (or a snake_case variant). When the local default credential file
    advertises a different id from ``_OAUTH_CLIENT_ID``, the
    self-refresh path will silently produce 4xx responses; surface it
    explicitly per docs/substrate.md §2 Gap.
    """
    global _CLIENT_ID_DIVERGENCE_CHECKED
    if _CLIENT_ID_DIVERGENCE_CHECKED:
        return
    _CLIENT_ID_DIVERGENCE_CHECKED = True
    try:
        with open(_DEFAULT_CLAUDE_CREDS) as f:
            creds = json.load(f)
    except (OSError, ValueError):
        return
    oauth = creds.get("claudeAiOauth") or {}
    embedded = (
        oauth.get("clientId")
        or oauth.get("client_id")
        or oauth.get("oauthClientId")
    )
    if embedded and embedded != _OAUTH_CLIENT_ID:
        print(
            f"  [oauth] WARNING: configured client id {_OAUTH_CLIENT_ID!r} "
            f"diverges from default-creds metadata {embedded!r}; "
            "self-refresh may fail. Set UAS_OAUTH_CLIENT_ID to align.",
            file=sys.stderr,
        )


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
    _check_client_id_divergence()
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
                [claude_path, "-p", "ping"],
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
