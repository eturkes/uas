"""Tests for integration/llm_judge.py.

Phase 1 PLAN Section 8. Mocked SDK throughout — no real LLM calls,
no network. Validates:

- Workspace walking (skip dirs, deterministic order, extension filter).
- Per-file and total budget truncation with the [truncated] sentinel.
- Verdict parsing (happy path, multiple JSON candidates, prose around
  JSON, no JSON, missing verdict key, missing reason).
- Workspace content hashing (stable, content-sensitive).
- Cache key shape and components.
- Cache load/save round-trip with corruption-tolerance.
- ``judge()`` end-to-end: majority math (5/0, 0/5, 3/2, 2/3, 4/1, 1/4),
  cache hit short-circuit, cache miss persists, exception in one
  sample, all-exception case.
- Eval.py wiring: the ``llm_judge`` check type calls ``judge()`` and
  surfaces the verdict + majority into the result row.
"""

import hashlib
import json
import os
import sys
from unittest import mock

import pytest

# integration/ is not a package; add to sys.path so we can import
# llm_judge and eval directly. Mirrors the pattern in
# tests/test_eval_checks.py and tests/test_eval_metadata.py.
_INTEG_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "integration")
)
if _INTEG_DIR not in sys.path:
    sys.path.insert(0, _INTEG_DIR)

import eval as ev  # noqa: E402
import llm_judge as lj  # noqa: E402


# ============================================================
# Workspace walking
# ============================================================


class TestWalkWorkspace:
    def test_returns_empty_for_missing_workspace(self, tmp_path):
        result = lj._walk_workspace(str(tmp_path / "does-not-exist"))
        assert result == []

    def test_returns_empty_for_empty_workspace(self, tmp_path):
        result = lj._walk_workspace(str(tmp_path))
        assert result == []

    def test_includes_matching_extensions(self, tmp_path):
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.md").write_text("y")
        (tmp_path / "c.json").write_text("{}")
        (tmp_path / "d.txt").write_text("hi")
        (tmp_path / "e.csv").write_text("col\n1")
        result = lj._walk_workspace(str(tmp_path))
        names = [r[0] for r in result]
        assert names == ["a.py", "b.md", "c.json", "d.txt", "e.csv"]

    def test_excludes_non_matching_extensions(self, tmp_path):
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.html").write_text("<p>")
        (tmp_path / "c.bin").write_bytes(b"\x00\x01")
        result = lj._walk_workspace(str(tmp_path))
        names = [r[0] for r in result]
        assert names == ["a.py"]

    def test_skips_hidden_state_dirs(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main")
        (tmp_path / ".uas_state").mkdir()
        (tmp_path / ".uas_state" / "snap.py").write_text("x")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "x.py").write_text("x")
        (tmp_path / ".pytest_cache").mkdir()
        (tmp_path / ".pytest_cache" / "y.py").write_text("y")
        (tmp_path / ".ruff_cache").mkdir()
        (tmp_path / ".ruff_cache" / "z.py").write_text("z")
        (tmp_path / ".uas_goals").mkdir()
        (tmp_path / ".uas_goals" / "g.json").write_text("{}")
        (tmp_path / "real.py").write_text("real")
        result = lj._walk_workspace(str(tmp_path))
        names = [r[0] for r in result]
        assert names == ["real.py"]

    def test_recurses_into_subdirs(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("x")
        (tmp_path / "src" / "lib").mkdir()
        (tmp_path / "src" / "lib" / "util.py").write_text("y")
        result = lj._walk_workspace(str(tmp_path))
        rels = sorted(r[0] for r in result)
        assert rels == [
            os.path.join("src", "app.py"),
            os.path.join("src", "lib", "util.py"),
        ]

    def test_extension_match_is_case_insensitive(self, tmp_path):
        (tmp_path / "X.PY").write_text("x")
        (tmp_path / "Y.JSON").write_text("{}")
        result = lj._walk_workspace(str(tmp_path))
        names = sorted(r[0] for r in result)
        assert names == ["X.PY", "Y.JSON"]

    def test_order_is_deterministic(self, tmp_path):
        # Create files in scrambled order — _walk_workspace should
        # always return them sorted.
        for name in ["zebra.py", "apple.py", "mango.py"]:
            (tmp_path / name).write_text("x")
        first = lj._walk_workspace(str(tmp_path))
        second = lj._walk_workspace(str(tmp_path))
        assert first == second
        assert [r[0] for r in first] == ["apple.py", "mango.py", "zebra.py"]


# ============================================================
# File reading + per-file truncation
# ============================================================


class TestReadTruncated:
    def test_short_file_returned_intact(self, tmp_path):
        f = tmp_path / "small.txt"
        f.write_text("hello world")
        assert lj._read_truncated(str(f), per_file_budget=1000) == "hello world"

    def test_oversize_file_truncated_with_sentinel(self, tmp_path):
        f = tmp_path / "big.txt"
        f.write_text("x" * 100)
        result = lj._read_truncated(str(f), per_file_budget=10)
        assert result.startswith("x" * 10)
        assert lj.TRUNCATED_SENTINEL in result

    def test_unreadable_file_returns_marker(self, tmp_path):
        # Use an unreachable path to trigger OSError.
        result = lj._read_truncated(str(tmp_path / "no-such-file.py"))
        assert "[unreadable:" in result


# ============================================================
# Workspace listing assembly
# ============================================================


class TestBuildWorkspaceListing:
    def test_empty_workspace_returns_no_files_marker(self, tmp_path):
        result = lj.build_workspace_listing(str(tmp_path))
        assert result == "(no matching files in workspace)"

    def test_auto_discovery_includes_all_matching(self, tmp_path):
        (tmp_path / "a.py").write_text("print('a')")
        (tmp_path / "b.md").write_text("# B")
        result = lj.build_workspace_listing(str(tmp_path))
        assert "--- a.py ---" in result
        assert "print('a')" in result
        assert "--- b.md ---" in result
        assert "# B" in result

    def test_explicit_files_only_includes_listed(self, tmp_path):
        (tmp_path / "a.py").write_text("a")
        (tmp_path / "b.py").write_text("b")
        (tmp_path / "c.py").write_text("c")
        result = lj.build_workspace_listing(
            str(tmp_path), files=["a.py", "c.py"]
        )
        assert "--- a.py ---" in result
        assert "--- c.py ---" in result
        assert "--- b.py ---" not in result

    def test_explicit_missing_file_marked(self, tmp_path):
        (tmp_path / "exists.py").write_text("x")
        result = lj.build_workspace_listing(
            str(tmp_path), files=["exists.py", "missing.py"]
        )
        assert "--- exists.py ---" in result
        assert "--- missing.py ---" in result
        assert "[file not found]" in result

    def test_per_file_budget_truncates(self, tmp_path):
        (tmp_path / "big.py").write_text("a" * 5000)
        result = lj.build_workspace_listing(
            str(tmp_path), per_file_budget=100
        )
        assert lj.TRUNCATED_SENTINEL in result
        # First 100 chars should be present.
        assert "a" * 100 in result

    def test_total_budget_drops_remaining_files(self, tmp_path):
        # Three files, each ~50 chars. Total budget 100 → only first
        # one or two should fit, then the trailing sentinel.
        (tmp_path / "a.py").write_text("a" * 50)
        (tmp_path / "b.py").write_text("b" * 50)
        (tmp_path / "c.py").write_text("c" * 50)
        result = lj.build_workspace_listing(
            str(tmp_path), per_file_budget=50, total_budget=100
        )
        assert result.endswith(lj.TRUNCATED_SENTINEL)
        # First file is always emitted unless its single block already
        # exceeds the total budget.
        assert "--- a.py ---" in result
        # Last file should be dropped.
        assert "c" * 50 not in result

    def test_listing_is_deterministic(self, tmp_path):
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.py").write_text("y")
        first = lj.build_workspace_listing(str(tmp_path))
        second = lj.build_workspace_listing(str(tmp_path))
        assert first == second


# ============================================================
# Verdict parsing
# ============================================================


class TestParseVerdict:
    def test_pass_verdict(self):
        text = '{"verdict": "pass", "reason": "all good"}'
        passed, reason = lj._parse_verdict(text)
        assert passed is True
        assert reason == "all good"

    def test_fail_verdict(self):
        text = '{"verdict": "fail", "reason": "missing index.html"}'
        passed, reason = lj._parse_verdict(text)
        assert passed is False
        assert reason == "missing index.html"

    def test_case_insensitive_verdict(self):
        text = '{"verdict": "PASS", "reason": "ok"}'
        passed, _ = lj._parse_verdict(text)
        assert passed is True

    def test_verdict_with_surrounding_prose(self):
        text = (
            "Looking at the workspace, I see the following...\n"
            'Final answer: {"verdict": "pass", "reason": "ok"}\n'
            "Hope this helps!"
        )
        passed, reason = lj._parse_verdict(text)
        assert passed is True
        assert reason == "ok"

    def test_unparseable_text_fails_with_snippet(self):
        text = "I have no opinion on this."
        passed, reason = lj._parse_verdict(text)
        assert passed is False
        assert reason.startswith("unparseable:")
        assert "I have no opinion" in reason

    def test_json_without_verdict_key_fails(self):
        text = '{"score": 0.9, "summary": "looks fine"}'
        passed, reason = lj._parse_verdict(text)
        assert passed is False
        assert reason.startswith("unparseable:")

    def test_missing_reason_returns_empty(self):
        text = '{"verdict": "pass"}'
        passed, reason = lj._parse_verdict(text)
        assert passed is True
        assert reason == ""

    def test_garbage_verdict_value_treated_as_fail(self):
        text = '{"verdict": "maybe", "reason": "uncertain"}'
        passed, _ = lj._parse_verdict(text)
        assert passed is False

    def test_last_json_object_wins(self):
        text = (
            '{"verdict": "fail", "reason": "first"}\n'
            'second pass: {"verdict": "pass", "reason": "second"}'
        )
        passed, reason = lj._parse_verdict(text)
        assert passed is True
        assert reason == "second"


# ============================================================
# Workspace hashing
# ============================================================


class TestHashWorkspace:
    def test_empty_workspace_returns_stable_hash(self, tmp_path):
        h1 = lj._hash_workspace_content(str(tmp_path))
        h2 = lj._hash_workspace_content(str(tmp_path))
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

    def test_missing_workspace_returns_empty_hash(self, tmp_path):
        h = lj._hash_workspace_content(str(tmp_path / "no-dir"))
        # Same as a hashlib.sha256().hexdigest() with no updates.
        assert h == hashlib.sha256().hexdigest()

    def test_content_change_changes_hash(self, tmp_path):
        f = tmp_path / "x.py"
        f.write_text("hello")
        h1 = lj._hash_workspace_content(str(tmp_path))
        f.write_text("goodbye")
        h2 = lj._hash_workspace_content(str(tmp_path))
        assert h1 != h2

    def test_filename_change_changes_hash(self, tmp_path):
        (tmp_path / "a.py").write_text("x")
        h1 = lj._hash_workspace_content(str(tmp_path))
        (tmp_path / "a.py").rename(tmp_path / "b.py")
        h2 = lj._hash_workspace_content(str(tmp_path))
        assert h1 != h2

    def test_skip_dir_change_does_not_change_hash(self, tmp_path):
        (tmp_path / "real.py").write_text("real")
        h1 = lj._hash_workspace_content(str(tmp_path))
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("ref: x")
        h2 = lj._hash_workspace_content(str(tmp_path))
        assert h1 == h2


# ============================================================
# Cache key + load/save
# ============================================================


class TestCacheKey:
    def test_key_format(self):
        key = lj._build_cache_key("hello-file", "x criterion", "abc123")
        parts = key.split("|")
        assert len(parts) == 3
        assert parts[0] == "hello-file"
        assert parts[2] == "abc123"
        # Middle part is sha256 hex of the criteria.
        assert parts[1] == hashlib.sha256(
            b"x criterion"
        ).hexdigest()

    def test_none_case_name_blanks_first_part(self):
        key = lj._build_cache_key(None, "c", "h")
        assert key.startswith("|")

    def test_criteria_change_changes_key(self):
        k1 = lj._build_cache_key("c1", "criteria A", "h")
        k2 = lj._build_cache_key("c1", "criteria B", "h")
        assert k1 != k2

    def test_workspace_hash_change_changes_key(self):
        k1 = lj._build_cache_key("c1", "criteria", "hash1")
        k2 = lj._build_cache_key("c1", "criteria", "hash2")
        assert k1 != k2


class TestCacheIO:
    def test_missing_file_returns_empty_dict(self, tmp_path):
        result = lj._load_cache(str(tmp_path / "no-cache.json"))
        assert result == {}

    def test_corrupt_file_returns_empty_dict(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("not json {{{")
        result = lj._load_cache(str(f))
        assert result == {}

    def test_non_dict_root_returns_empty_dict(self, tmp_path):
        f = tmp_path / "list.json"
        f.write_text("[1, 2, 3]")
        result = lj._load_cache(str(f))
        assert result == {}

    def test_save_then_load_round_trips(self, tmp_path):
        path = str(tmp_path / "cache.json")
        original = {"key1": {"passed": True, "votes": [True, True, False]}}
        lj._save_cache(original, path)
        loaded = lj._load_cache(path)
        assert loaded == original

    def test_save_creates_parent_dir(self, tmp_path):
        path = str(tmp_path / "nested" / "deeper" / "cache.json")
        lj._save_cache({"k": {"v": 1}}, path)
        assert os.path.isfile(path)


# ============================================================
# judge() end-to-end with mocked SDK
# ============================================================


@pytest.fixture
def workspace_with_one_file(tmp_path):
    # Workspace must be a subdir of tmp_path so the cache file
    # (which lives at tmp_path / judge-cache.json) is OUTSIDE the
    # workspace. Otherwise saving the cache mutates the workspace
    # and invalidates the workspace_hash on the next read.
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("print('hi')")
    return str(ws)


def _mk_responses(verdicts):
    """Build a side_effect list for `_call_anthropic` from verdict strings."""
    return [
        json.dumps({"verdict": v, "reason": f"r{i}"})
        for i, v in enumerate(verdicts)
    ]


class TestJudgeMajorityVote:
    def test_5_pass_passes(self, workspace_with_one_file, tmp_path):
        cache = str(tmp_path / "judge-cache.json")
        with mock.patch.object(
            lj, "_call_anthropic",
            side_effect=_mk_responses(["pass"] * 5),
        ):
            result = lj.judge(
                "g", workspace_with_one_file, "c",
                samples=5, cache_path=cache,
            )
        assert result["passed"] is True
        assert sum(result["votes"]) == 5
        assert result["majority"] == 1.0
        assert result["samples_used"] == 5
        assert result["cached"] is False

    def test_5_fail_fails(self, workspace_with_one_file, tmp_path):
        cache = str(tmp_path / "judge-cache.json")
        with mock.patch.object(
            lj, "_call_anthropic",
            side_effect=_mk_responses(["fail"] * 5),
        ):
            result = lj.judge(
                "g", workspace_with_one_file, "c",
                samples=5, cache_path=cache,
            )
        assert result["passed"] is False
        assert sum(result["votes"]) == 0
        assert result["majority"] == 0.0

    def test_3_pass_2_fail_passes(self, workspace_with_one_file, tmp_path):
        cache = str(tmp_path / "judge-cache.json")
        with mock.patch.object(
            lj, "_call_anthropic",
            side_effect=_mk_responses(
                ["pass", "pass", "pass", "fail", "fail"]
            ),
        ):
            result = lj.judge(
                "g", workspace_with_one_file, "c",
                samples=5, cache_path=cache,
            )
        assert result["passed"] is True
        assert sum(result["votes"]) == 3
        assert result["majority"] == 0.6

    def test_2_pass_3_fail_fails(self, workspace_with_one_file, tmp_path):
        cache = str(tmp_path / "judge-cache.json")
        with mock.patch.object(
            lj, "_call_anthropic",
            side_effect=_mk_responses(
                ["pass", "pass", "fail", "fail", "fail"]
            ),
        ):
            result = lj.judge(
                "g", workspace_with_one_file, "c",
                samples=5, cache_path=cache,
            )
        assert result["passed"] is False
        assert sum(result["votes"]) == 2
        assert result["majority"] == 0.4

    def test_4_pass_1_fail_passes(self, workspace_with_one_file, tmp_path):
        cache = str(tmp_path / "judge-cache.json")
        with mock.patch.object(
            lj, "_call_anthropic",
            side_effect=_mk_responses(
                ["pass", "pass", "pass", "pass", "fail"]
            ),
        ):
            result = lj.judge(
                "g", workspace_with_one_file, "c",
                samples=5, cache_path=cache,
            )
        assert result["passed"] is True
        assert sum(result["votes"]) == 4
        assert result["majority"] == 0.8

    def test_zero_samples_fails_safely(
        self, workspace_with_one_file, tmp_path
    ):
        cache = str(tmp_path / "judge-cache.json")
        # Even with zero samples, the function should not raise.
        with mock.patch.object(lj, "_call_anthropic") as patched:
            result = lj.judge(
                "g", workspace_with_one_file, "c",
                samples=0, cache_path=cache, use_cache=False,
            )
            patched.assert_not_called()
        assert result["passed"] is False
        assert result["votes"] == []
        assert result["majority"] == 0.0
        assert result["samples_used"] == 0


class TestJudgeErrorPaths:
    def test_call_exception_counts_as_fail(
        self, workspace_with_one_file, tmp_path
    ):
        cache = str(tmp_path / "judge-cache.json")

        def flaky(model, prompt):
            raise RuntimeError("network down")

        with mock.patch.object(lj, "_call_anthropic", side_effect=flaky):
            result = lj.judge(
                "g", workspace_with_one_file, "c",
                samples=3, cache_path=cache, use_cache=False,
            )
        assert result["passed"] is False
        assert sum(result["votes"]) == 0
        assert all("call error" in r for r in result["reasons"])

    def test_one_call_exception_other_passes_majority_holds(
        self, workspace_with_one_file, tmp_path
    ):
        cache = str(tmp_path / "judge-cache.json")
        responses = list(_mk_responses(["pass", "pass"]))
        responses.append(RuntimeError("flake"))

        def side_effect(model, prompt):
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with mock.patch.object(
            lj, "_call_anthropic", side_effect=side_effect
        ):
            result = lj.judge(
                "g", workspace_with_one_file, "c",
                samples=3, cache_path=cache, use_cache=False,
            )
        # 2 passes + 1 error → ceil(3/2)=2 → still passes.
        assert result["passed"] is True
        assert sum(result["votes"]) == 2

    def test_unparseable_response_counts_as_fail(
        self, workspace_with_one_file, tmp_path
    ):
        cache = str(tmp_path / "judge-cache.json")
        with mock.patch.object(
            lj, "_call_anthropic",
            return_value="i don't know",
        ):
            result = lj.judge(
                "g", workspace_with_one_file, "c",
                samples=5, cache_path=cache, use_cache=False,
            )
        assert result["passed"] is False
        assert all(not v for v in result["votes"])


class TestJudgeCache:
    def test_first_call_misses_cache(
        self, workspace_with_one_file, tmp_path
    ):
        cache = str(tmp_path / "judge-cache.json")
        with mock.patch.object(
            lj, "_call_anthropic",
            side_effect=_mk_responses(["pass"] * 5),
        ) as patched:
            result = lj.judge(
                "g", workspace_with_one_file, "c",
                samples=5, cache_path=cache, case_name="test-case",
            )
        assert patched.call_count == 5
        assert result["cached"] is False
        assert os.path.isfile(cache)

    def test_second_call_hits_cache_no_sdk_calls(
        self, workspace_with_one_file, tmp_path
    ):
        cache = str(tmp_path / "judge-cache.json")
        # Prime the cache.
        with mock.patch.object(
            lj, "_call_anthropic",
            side_effect=_mk_responses(["pass"] * 5),
        ):
            lj.judge(
                "g", workspace_with_one_file, "c",
                samples=5, cache_path=cache, case_name="test-case",
            )
        # Second call should not invoke the SDK.
        with mock.patch.object(lj, "_call_anthropic") as patched:
            result = lj.judge(
                "g", workspace_with_one_file, "c",
                samples=5, cache_path=cache, case_name="test-case",
            )
            patched.assert_not_called()
        assert result["cached"] is True
        assert result["passed"] is True
        assert sum(result["votes"]) == 5

    def test_workspace_change_invalidates_cache(
        self, workspace_with_one_file, tmp_path
    ):
        cache = str(tmp_path / "judge-cache.json")
        # Prime cache with a passing verdict.
        with mock.patch.object(
            lj, "_call_anthropic",
            side_effect=_mk_responses(["pass"] * 5),
        ):
            lj.judge(
                "g", workspace_with_one_file, "c",
                samples=5, cache_path=cache, case_name="test-case",
            )
        # Mutate the workspace — cache must miss now.
        with open(
            os.path.join(workspace_with_one_file, "a.py"), "w"
        ) as f:
            f.write("print('changed')")
        with mock.patch.object(
            lj, "_call_anthropic",
            side_effect=_mk_responses(["fail"] * 5),
        ) as patched:
            result = lj.judge(
                "g", workspace_with_one_file, "c",
                samples=5, cache_path=cache, case_name="test-case",
            )
            assert patched.call_count == 5
        assert result["cached"] is False
        assert result["passed"] is False

    def test_criteria_change_invalidates_cache(
        self, workspace_with_one_file, tmp_path
    ):
        cache = str(tmp_path / "judge-cache.json")
        with mock.patch.object(
            lj, "_call_anthropic",
            side_effect=_mk_responses(["pass"] * 5),
        ):
            lj.judge(
                "g", workspace_with_one_file, "criteria A",
                samples=5, cache_path=cache, case_name="test-case",
            )
        with mock.patch.object(
            lj, "_call_anthropic",
            side_effect=_mk_responses(["fail"] * 5),
        ) as patched:
            result = lj.judge(
                "g", workspace_with_one_file, "criteria B",
                samples=5, cache_path=cache, case_name="test-case",
            )
            assert patched.call_count == 5
        assert result["cached"] is False
        assert result["passed"] is False

    def test_use_cache_false_forces_fresh_call(
        self, workspace_with_one_file, tmp_path
    ):
        cache = str(tmp_path / "judge-cache.json")
        with mock.patch.object(
            lj, "_call_anthropic",
            side_effect=_mk_responses(["pass"] * 5),
        ):
            lj.judge(
                "g", workspace_with_one_file, "c",
                samples=5, cache_path=cache,
            )
        with mock.patch.object(
            lj, "_call_anthropic",
            side_effect=_mk_responses(["fail"] * 5),
        ) as patched:
            result = lj.judge(
                "g", workspace_with_one_file, "c",
                samples=5, cache_path=cache, use_cache=False,
            )
            assert patched.call_count == 5
        assert result["cached"] is False
        assert result["passed"] is False

    def test_all_errors_skip_cache_write(
        self, workspace_with_one_file, tmp_path
    ):
        """A run where every sample errored must NOT create a cache entry.

        Regression guard for the Phase 1 §10 run_b cache-poisoning bug:
        a single transient 401 used to persist as a cached "fail with
        reason: call error: HTTPStatusError 401", short-circuiting
        every subsequent run against the same workspace.
        """
        cache = str(tmp_path / "judge-cache.json")

        def always_raise(model, prompt):
            raise RuntimeError("HTTPStatusError: 401 Unauthorized")

        with mock.patch.object(
            lj, "_call_anthropic", side_effect=always_raise
        ):
            result = lj.judge(
                "g", workspace_with_one_file, "c",
                samples=5, cache_path=cache, case_name="test-case",
            )
        assert result["passed"] is False
        assert all("call error" in r for r in result["reasons"])
        assert not os.path.isfile(cache), (
            "error-only judge run must not write a cache file"
        )

    def test_partial_errors_skip_cache_write(
        self, workspace_with_one_file, tmp_path
    ):
        """Even one errored sample in a mixed run blocks caching.

        Rationale: conservative — any infrastructure error hints that
        the run is not a clean measurement, and we'd rather re-call
        next time than persist a half-degraded verdict.
        """
        cache = str(tmp_path / "judge-cache.json")
        responses = list(_mk_responses(["pass", "pass", "pass", "pass"]))
        responses.append(RuntimeError("flake"))

        def side_effect(model, prompt):
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with mock.patch.object(
            lj, "_call_anthropic", side_effect=side_effect
        ):
            result = lj.judge(
                "g", workspace_with_one_file, "c",
                samples=5, cache_path=cache, case_name="test-case",
            )
        # 4 passes + 1 error → ceil(5/2)=3 → still a pass verdict
        assert result["passed"] is True
        assert sum(result["votes"]) == 4
        assert not os.path.isfile(cache), (
            "any infrastructure error must block cache persistence"
        )

    def test_pre_existing_cache_untouched_on_error(
        self, workspace_with_one_file, tmp_path
    ):
        """Errored runs must not mutate unrelated pre-existing entries."""
        cache = str(tmp_path / "judge-cache.json")
        # Prime the cache with a good entry for criteria A.
        with mock.patch.object(
            lj, "_call_anthropic",
            side_effect=_mk_responses(["pass"] * 5),
        ):
            lj.judge(
                "g", workspace_with_one_file, "criteria A",
                samples=5, cache_path=cache, case_name="test-case",
            )
        original = lj._load_cache(cache)
        assert len(original) == 1
        # Now run with errors under criteria B (different key).
        with mock.patch.object(
            lj, "_call_anthropic",
            side_effect=RuntimeError("boom"),
        ):
            lj.judge(
                "g", workspace_with_one_file, "criteria B",
                samples=5, cache_path=cache, case_name="test-case",
            )
        after = lj._load_cache(cache)
        # Cache still has exactly the criteria-A entry, no B entry.
        assert after == original


# ============================================================
# eval.py integration: llm_judge check type
# ============================================================


class TestRunCheckLlmJudge:
    def test_missing_criteria_fails_loudly(self, tmp_path):
        result = ev.run_check(
            {"type": "llm_judge"},
            str(tmp_path),
            case={"name": "x", "goal": "g"},
        )
        assert result["passed"] is False
        assert "criteria" in result["detail"]

    def test_missing_case_fails_loudly(self, tmp_path):
        result = ev.run_check(
            {"type": "llm_judge", "criteria": "c"},
            str(tmp_path),
        )
        assert result["passed"] is False
        assert "case context" in result["detail"]

    def test_passing_judge_surfaces_majority(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "out.py").write_text("ok")
        cache = str(tmp_path / "judge-cache.json")
        check = {
            "type": "llm_judge",
            "criteria": "out.py exists",
            "samples": 5,
        }
        case = {"name": "tc", "goal": "make out.py"}
        # Patch the symbol on llm_judge directly so the eval branch
        # picks it up via _import_llm_judge → llm_judge module.
        with mock.patch.object(
            lj, "_call_anthropic",
            side_effect=_mk_responses(["pass"] * 5),
        ):
            with mock.patch.object(
                lj, "JUDGE_CACHE_PATH", cache
            ):
                result = ev.run_check(check, str(ws), case=case)
        assert result["type"] == "llm_judge"
        assert result["passed"] is True
        assert result["majority"] == 1.0
        assert result["samples_used"] == 5
        assert "majority=1.00" in result["detail"]
        assert "votes=5/5" in result["detail"]

    def test_failing_judge_surfaces_majority(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "out.py").write_text("ok")
        cache = str(tmp_path / "judge-cache.json")
        check = {
            "type": "llm_judge",
            "criteria": "out.py is empty",
            "samples": 5,
        }
        case = {"name": "tc", "goal": "make out.py"}
        with mock.patch.object(
            lj, "_call_anthropic",
            side_effect=_mk_responses(["fail"] * 5),
        ):
            with mock.patch.object(
                lj, "JUDGE_CACHE_PATH", cache
            ):
                result = ev.run_check(check, str(ws), case=case)
        assert result["passed"] is False
        assert result["majority"] == 0.0
        assert "majority=0.00" in result["detail"]

    def test_run_checks_threads_case_through(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "out.py").write_text("ok")
        cache = str(tmp_path / "judge-cache.json")
        case = {
            "name": "tc",
            "goal": "make out.py",
            "checks": [
                {"type": "llm_judge", "criteria": "out.py exists"},
            ],
        }
        with mock.patch.object(
            lj, "_call_anthropic",
            side_effect=_mk_responses(["pass"] * 5),
        ):
            with mock.patch.object(
                lj, "JUDGE_CACHE_PATH", cache
            ):
                results = ev.run_checks(
                    case, str(ws), invocation={"exit_code": 0},
                )
        assert len(results) == 1
        assert results[0]["type"] == "llm_judge"
        assert results[0]["passed"] is True


class TestExistingCheckTypesUnaffected:
    """Regression: adding case= must not break existing types."""

    def test_file_exists_still_works(self, tmp_path):
        (tmp_path / "x.txt").write_text("hi")
        result = ev.run_check(
            {"type": "file_exists", "path": "x.txt"},
            str(tmp_path),
        )
        assert result["passed"] is True

    def test_run_checks_passes_case_kwarg_safely(self, tmp_path):
        (tmp_path / "x.txt").write_text("hi")
        case = {
            "name": "tc",
            "goal": "g",
            "checks": [{"type": "file_exists", "path": "x.txt"}],
        }
        results = ev.run_checks(
            case, str(tmp_path), invocation={"exit_code": 0},
        )
        assert len(results) == 1
        assert results[0]["passed"] is True
