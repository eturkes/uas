"""Tests for the reproducibility metadata helper in integration/eval.py.

Phase 1 PLAN Section 4. Validates ``capture_run_metadata`` shape,
secret-filtering, git capture, and config-hash reproducibility.
No LLM, no container.
"""

import os
import re
import sys

import pytest  # noqa: F401

_INTEG_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "integration")
)
if _INTEG_DIR not in sys.path:
    sys.path.insert(0, _INTEG_DIR)

import eval as ev  # noqa: E402


class TestCaptureRunMetadata:
    def test_returns_dict_with_expected_keys(self):
        m = ev.capture_run_metadata()
        expected = {
            "git_sha", "git_branch", "git_dirty",
            "timestamp_utc", "env_snapshot", "config_hash",
            "harness_version",
        }
        assert set(m.keys()) == expected

    def test_harness_version_constant(self):
        m = ev.capture_run_metadata()
        assert m["harness_version"] == "phase1"
        assert m["harness_version"] == ev.HARNESS_VERSION

    def test_git_sha_is_full_hex_or_unknown(self):
        m = ev.capture_run_metadata()
        sha = m["git_sha"]
        assert sha == "unknown" or re.match(r"^[0-9a-f]{40}$", sha)

    def test_git_branch_is_string(self):
        m = ev.capture_run_metadata()
        assert isinstance(m["git_branch"], str)
        assert m["git_branch"]

    def test_git_dirty_is_bool(self):
        m = ev.capture_run_metadata()
        assert isinstance(m["git_dirty"], bool)

    def test_timestamp_is_iso_utc(self):
        m = ev.capture_run_metadata()
        ts = m["timestamp_utc"]
        # datetime.isoformat() with timezone.utc produces +00:00
        assert "+00:00" in ts or ts.endswith("Z")

    def test_env_snapshot_only_uas_keys(self, monkeypatch):
        monkeypatch.setenv("UAS_TEST_VAR_SECTION4", "x")
        monkeypatch.setenv("OTHER_VAR_SECTION4", "y")
        m = ev.capture_run_metadata()
        assert "UAS_TEST_VAR_SECTION4" in m["env_snapshot"]
        assert "OTHER_VAR_SECTION4" not in m["env_snapshot"]

    def test_env_snapshot_filters_secret_suffixes(self, monkeypatch):
        # Common secret patterns
        monkeypatch.setenv("UAS_API_KEY", "leaked1")
        monkeypatch.setenv("UAS_PUBLIC_TOKEN", "leaked2")
        monkeypatch.setenv("UAS_DB_SECRET", "leaked3")
        monkeypatch.setenv("UAS_USER_PASSWORD", "leaked4")
        # Edge case: lowercase suffix
        monkeypatch.setenv("UAS_LOWER_token", "leaked5")
        # Non-secret control
        monkeypatch.setenv("UAS_NORMAL", "ok")
        m = ev.capture_run_metadata()
        for k in [
            "UAS_API_KEY",
            "UAS_PUBLIC_TOKEN",
            "UAS_DB_SECRET",
            "UAS_USER_PASSWORD",
            "UAS_LOWER_token",
        ]:
            assert k not in m["env_snapshot"], f"secret leaked: {k}"
        assert m["env_snapshot"].get("UAS_NORMAL") == "ok"
        # And no leaked value made it through any other key
        for v in m["env_snapshot"].values():
            assert "leaked" not in v

    def test_anthropic_api_key_implicitly_excluded(self, monkeypatch):
        # ANTHROPIC_API_KEY does not match UAS_*; the prefix filter
        # implicitly excludes it.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "should_never_appear")
        m = ev.capture_run_metadata()
        assert "ANTHROPIC_API_KEY" not in m["env_snapshot"]
        for v in m["env_snapshot"].values():
            assert "should_never_appear" not in v

    def test_secret_filter_does_not_overmatch(self, monkeypatch):
        # UAS_KEY_NAME is not a secret — _KEY is not a suffix.
        monkeypatch.setenv("UAS_KEY_NAME", "not_a_secret")
        m = ev.capture_run_metadata()
        assert m["env_snapshot"].get("UAS_KEY_NAME") == "not_a_secret"

    def test_config_hash_is_reproducible(self):
        m1 = ev.capture_run_metadata()
        m2 = ev.capture_run_metadata()
        assert m1["config_hash"] == m2["config_hash"]

    def test_config_hash_format(self):
        m = ev.capture_run_metadata()
        h = m["config_hash"]
        assert h == "unavailable" or re.match(r"^[0-9a-f]{64}$", h)


class TestGitCaptureHelper:
    def test_returns_default_on_bad_args(self):
        # Invalid git subcommand → non-zero exit → default returned
        result = ev._git_capture(["this-is-not-a-git-command"])
        assert result == "unknown"

    def test_explicit_default_returned(self):
        result = ev._git_capture(["bogus"], default="custom-default")
        assert result == "custom-default"


class TestHashActiveConfig:
    def test_returns_hex_or_unavailable(self):
        h = ev._hash_active_config()
        assert h == "unavailable" or re.match(r"^[0-9a-f]{64}$", h)

    def test_idempotent(self):
        a = ev._hash_active_config()
        b = ev._hash_active_config()
        assert a == b
