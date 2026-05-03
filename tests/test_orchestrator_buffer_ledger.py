"""Tests for ``orchestrator.buffer_ledger`` and ``orchestrator.pricing``.

Pure-Python tests against synthetic ``result`` payloads. No
container engine, no live Claude — every test exercises the JSONL
persistence contract, the cost-table lookups, and the read paths
against fixtures the test itself constructs. The ``state_root``
constructor argument lets each test write into a per-test
``tmp_path`` so the suite never touches the canonical
``<repo>/orchestrator/state`` directory.
"""

import json
import os

import pytest

from orchestrator import buffer_ledger, pricing


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_result(
    *,
    model: str | None = None,
    model_usage_keys: tuple[str, ...] = ("claude-opus-4-7",),
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    total_cost_usd: float | None = 0.001234,
    omit_usage: bool = False,
    omit_model_usage: bool = False,
) -> dict:
    """Build a synthetic stream-json terminal ``result`` payload.

    Mirrors what Claude Code emits per the §2 live-spawn capture:
    ``model`` is None on the runs the project sees today; the
    active model id lives under ``modelUsage``'s first key. Tests
    can override individual fields without breaking shape.
    """
    result: dict = {
        "type": "result",
        "model": model,
        "terminal_reason": "end_turn",
        "total_cost_usd": total_cost_usd,
    }
    if not omit_usage:
        result["usage"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_creation_input_tokens,
            "cache_read_input_tokens": cache_read_input_tokens,
        }
    if not omit_model_usage:
        result["modelUsage"] = {
            key: {"inputTokens": input_tokens, "outputTokens": output_tokens}
            for key in model_usage_keys
        }
    return result


def make_metadata(**overrides) -> dict:
    """Build a synthetic ``capture_run_metadata`` payload.

    Includes the ``orchestrator_version`` key so rows look
    indistinguishable from real worker-emitted rows.
    """
    base = {
        "git_sha": "deadbeef" + "0" * 32,
        "git_branch": "main",
        "git_dirty": False,
        "timestamp_utc": "2026-05-01T00:00:00+00:00",
        "env_snapshot": {},
        "config_hash": "n/a",
        "harness_version": "phase1",
        "orchestrator_version": "phase5",
    }
    base.update(overrides)
    return base


def _read_rows(state_root: str, task_id: str) -> list[dict]:
    path = os.path.join(state_root, task_id, "buffer.jsonl")
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


@pytest.fixture
def ledger(tmp_path):
    return buffer_ledger.BufferLedger(state_root=str(tmp_path))


# ---------------------------------------------------------------------------
# Pricing table
# ---------------------------------------------------------------------------

class TestPricingTable:
    """The hard-coded model-id -> rate-row dict."""

    def test_claude_opus_4_7_entry_exists(self):
        assert "claude-opus-4-7" in pricing.PRICING

    def test_opus_entry_has_all_four_rate_fields(self):
        row = pricing.PRICING["claude-opus-4-7"]
        for key in (
            "input_per_m", "output_per_m",
            "cache_write_per_m", "cache_read_per_m",
        ):
            assert key in row
            assert isinstance(row[key], (int, float))
            assert row[key] > 0

    def test_unknown_model_error_subclasses_keyerror(self):
        # Subclassing KeyError preserves backward compatibility for
        # any caller that catches KeyError generically.
        assert issubclass(pricing.UnknownModelError, KeyError)


# ---------------------------------------------------------------------------
# compute_cost()
# ---------------------------------------------------------------------------

class TestComputeCost:
    """Token math against the published rate table."""

    def test_one_million_input_tokens_costs_input_per_m(self):
        row = pricing.PRICING["claude-opus-4-7"]
        cost = pricing.compute_cost(
            "claude-opus-4-7",
            {"input_tokens": 1_000_000},
        )
        assert cost == pytest.approx(row["input_per_m"])

    def test_one_million_output_tokens_costs_output_per_m(self):
        row = pricing.PRICING["claude-opus-4-7"]
        cost = pricing.compute_cost(
            "claude-opus-4-7",
            {"output_tokens": 1_000_000},
        )
        assert cost == pytest.approx(row["output_per_m"])

    def test_one_million_cache_write_tokens_costs_cache_write_per_m(self):
        row = pricing.PRICING["claude-opus-4-7"]
        cost = pricing.compute_cost(
            "claude-opus-4-7",
            {"cache_creation_input_tokens": 1_000_000},
        )
        assert cost == pytest.approx(row["cache_write_per_m"])

    def test_one_million_cache_read_tokens_costs_cache_read_per_m(self):
        row = pricing.PRICING["claude-opus-4-7"]
        cost = pricing.compute_cost(
            "claude-opus-4-7",
            {"cache_read_input_tokens": 1_000_000},
        )
        assert cost == pytest.approx(row["cache_read_per_m"])

    def test_mixed_usage_sums_independently(self):
        row = pricing.PRICING["claude-opus-4-7"]
        cost = pricing.compute_cost(
            "claude-opus-4-7",
            {
                "input_tokens": 100_000,
                "output_tokens": 50_000,
                "cache_creation_input_tokens": 10_000,
                "cache_read_input_tokens": 200_000,
            },
        )
        expected = (
            100_000 * row["input_per_m"] / 1_000_000.0
            + 50_000 * row["output_per_m"] / 1_000_000.0
            + 10_000 * row["cache_write_per_m"] / 1_000_000.0
            + 200_000 * row["cache_read_per_m"] / 1_000_000.0
        )
        assert cost == pytest.approx(expected)

    def test_zero_usage_costs_zero(self):
        cost = pricing.compute_cost("claude-opus-4-7", {})
        assert cost == 0.0

    def test_missing_keys_treated_as_zero(self):
        # Only input_tokens supplied; cache + output keys absent.
        row = pricing.PRICING["claude-opus-4-7"]
        cost = pricing.compute_cost(
            "claude-opus-4-7",
            {"input_tokens": 1_000_000},
        )
        assert cost == pytest.approx(row["input_per_m"])

    def test_none_token_values_treated_as_zero(self):
        # Some SDK versions emit `null` rather than omit the field.
        cost = pricing.compute_cost(
            "claude-opus-4-7",
            {
                "input_tokens": None,
                "output_tokens": None,
                "cache_creation_input_tokens": None,
                "cache_read_input_tokens": None,
            },
        )
        assert cost == 0.0

    def test_unknown_model_id_raises(self):
        with pytest.raises(pricing.UnknownModelError):
            pricing.compute_cost(
                "claude-opus-99-9", {"input_tokens": 100},
            )

    def test_unknown_model_error_caught_by_keyerror(self):
        # Confirm the KeyError compatibility property in the wild.
        try:
            pricing.compute_cost("not-a-model", {"input_tokens": 1})
        except KeyError:
            pass
        else:
            pytest.fail("expected KeyError-compatible raise")


# ---------------------------------------------------------------------------
# _extract_model_id()
# ---------------------------------------------------------------------------

class TestExtractModelId:
    """Model-id resolution from terminal-result payloads."""

    def test_uses_top_level_model_when_present(self):
        result = make_result(
            model="claude-opus-4-7",
            model_usage_keys=("something-else",),
        )
        assert (
            buffer_ledger._extract_model_id(result) == "claude-opus-4-7"
        )

    def test_falls_back_to_model_usage_first_key(self):
        result = make_result(model=None)
        assert (
            buffer_ledger._extract_model_id(result) == "claude-opus-4-7"
        )

    def test_empty_string_model_falls_back(self):
        result = make_result(model="")
        assert (
            buffer_ledger._extract_model_id(result) == "claude-opus-4-7"
        )

    def test_raises_when_no_model_id_resolvable(self):
        result = make_result(model=None, omit_model_usage=True)
        with pytest.raises(ValueError):
            buffer_ledger._extract_model_id(result)

    def test_raises_when_model_usage_empty_dict(self):
        result = make_result(model=None)
        result["modelUsage"] = {}
        with pytest.raises(ValueError):
            buffer_ledger._extract_model_id(result)


# ---------------------------------------------------------------------------
# record()
# ---------------------------------------------------------------------------

class TestRecord:
    """Persistence layer: file creation, schema, return value."""

    def test_creates_file_and_writes_one_row(self, ledger, tmp_path):
        ledger.record(
            make_result(),
            run_metadata=make_metadata(),
            task_id="t1",
            subtask_id="s1",
        )
        path = os.path.join(str(tmp_path), "t1", "buffer.jsonl")
        assert os.path.isfile(path)
        rows = _read_rows(str(tmp_path), "t1")
        assert len(rows) == 1
        assert rows[0]["event"] == "buffer"
        assert rows[0]["task_id"] == "t1"
        assert rows[0]["subtask_id"] == "s1"

    def test_returns_computed_cost(self, ledger):
        cost = ledger.record(
            make_result(input_tokens=1_000_000, output_tokens=0),
            run_metadata=make_metadata(),
            task_id="t1",
            subtask_id="s1",
        )
        # 1M input @ $15/M = $15.00 (output zeroed to isolate input rate)
        assert cost == pytest.approx(15.00)

    def test_persists_cost_matches_compute_cost(self, ledger, tmp_path):
        result = make_result(
            input_tokens=200,
            output_tokens=100,
            cache_creation_input_tokens=50,
            cache_read_input_tokens=300,
        )
        expected = pricing.compute_cost("claude-opus-4-7", result["usage"])
        ledger.record(
            result, run_metadata=make_metadata(),
            task_id="t1", subtask_id="s1",
        )
        rows = _read_rows(str(tmp_path), "t1")
        assert rows[0]["cost_usd"] == pytest.approx(expected)

    def test_records_resolved_model_id(self, ledger, tmp_path):
        ledger.record(
            make_result(),
            run_metadata=make_metadata(),
            task_id="t1", subtask_id="s1",
        )
        rows = _read_rows(str(tmp_path), "t1")
        assert rows[0]["model"] == "claude-opus-4-7"

    def test_records_usage_dict_round_trip(self, ledger, tmp_path):
        usage = {
            "input_tokens": 7,
            "output_tokens": 3,
            "cache_creation_input_tokens": 2,
            "cache_read_input_tokens": 9,
        }
        result = make_result(
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cache_creation_input_tokens=usage["cache_creation_input_tokens"],
            cache_read_input_tokens=usage["cache_read_input_tokens"],
        )
        ledger.record(
            result, run_metadata=make_metadata(),
            task_id="t1", subtask_id="s1",
        )
        rows = _read_rows(str(tmp_path), "t1")
        assert rows[0]["usage"] == usage

    def test_records_claude_reported_cost(self, ledger, tmp_path):
        ledger.record(
            make_result(total_cost_usd=0.0567),
            run_metadata=make_metadata(),
            task_id="t1", subtask_id="s1",
        )
        rows = _read_rows(str(tmp_path), "t1")
        assert rows[0]["claude_reported_cost_usd"] == pytest.approx(0.0567)

    def test_claude_reported_cost_absent_records_none(self, ledger, tmp_path):
        result = make_result(total_cost_usd=None)
        # Strip the field entirely to simulate older payloads.
        del result["total_cost_usd"]
        ledger.record(
            result, run_metadata=make_metadata(),
            task_id="t1", subtask_id="s1",
        )
        rows = _read_rows(str(tmp_path), "t1")
        assert rows[0]["claude_reported_cost_usd"] is None

    def test_stamps_run_metadata(self, ledger, tmp_path):
        ledger.record(
            make_result(),
            run_metadata=make_metadata(git_sha="cafef00d"),
            task_id="t1", subtask_id="s1",
        )
        rows = _read_rows(str(tmp_path), "t1")
        assert rows[0]["git_sha"] == "cafef00d"
        assert rows[0]["harness_version"] == "phase1"
        assert rows[0]["orchestrator_version"] == "phase5"

    def test_appends_across_multiple_calls(self, ledger, tmp_path):
        ledger.record(
            make_result(), run_metadata=make_metadata(),
            task_id="t1", subtask_id="s1",
        )
        ledger.record(
            make_result(), run_metadata=make_metadata(),
            task_id="t1", subtask_id="s2",
        )
        rows = _read_rows(str(tmp_path), "t1")
        assert len(rows) == 2
        assert [r["subtask_id"] for r in rows] == ["s1", "s2"]

    def test_isolates_tasks(self, ledger, tmp_path):
        ledger.record(
            make_result(), run_metadata=make_metadata(),
            task_id="ta", subtask_id="s1",
        )
        ledger.record(
            make_result(), run_metadata=make_metadata(),
            task_id="tb", subtask_id="s1",
        )
        ledger.record(
            make_result(), run_metadata=make_metadata(),
            task_id="tb", subtask_id="s2",
        )
        assert len(_read_rows(str(tmp_path), "ta")) == 1
        assert len(_read_rows(str(tmp_path), "tb")) == 2


# ---------------------------------------------------------------------------
# record() error paths
# ---------------------------------------------------------------------------

class TestRecordErrors:
    """The 'no silent zero-cost' property in action."""

    def test_unknown_model_propagates(self, ledger, tmp_path):
        result = make_result(model="claude-opus-99-9")
        with pytest.raises(pricing.UnknownModelError):
            ledger.record(
                result, run_metadata=make_metadata(),
                task_id="t1", subtask_id="s1",
            )
        # On error nothing was persisted (the file is opened only
        # after compute_cost succeeds).
        assert not os.path.exists(
            os.path.join(str(tmp_path), "t1", "buffer.jsonl"),
        )

    def test_unresolvable_model_id_raises_value_error(self, ledger):
        result = make_result(model=None, omit_model_usage=True)
        with pytest.raises(ValueError):
            ledger.record(
                result, run_metadata=make_metadata(),
                task_id="t1", subtask_id="s1",
            )

    def test_missing_usage_dict_treated_as_empty(self, ledger, tmp_path):
        # No usage key → treated as empty dict → cost is 0.0; row is
        # still written so the audit trail records the call. The
        # spawn_worker wire-in skips this case via an explicit usage
        # check, but BufferLedger.record itself does not — direct
        # callers that want zero-cost rows can opt in.
        result = make_result(omit_usage=True)
        cost = ledger.record(
            result, run_metadata=make_metadata(),
            task_id="t1", subtask_id="s1",
        )
        assert cost == 0.0
        rows = _read_rows(str(tmp_path), "t1")
        assert rows[0]["usage"] == {}


# ---------------------------------------------------------------------------
# total_spent()
# ---------------------------------------------------------------------------

class TestTotalSpent:
    """Sum of cost_usd across the whole task ledger."""

    def test_missing_file_returns_zero(self, ledger):
        assert ledger.total_spent("never-recorded") == 0.0

    def test_single_row_returns_that_cost(self, ledger):
        cost = ledger.record(
            make_result(input_tokens=1_000_000),
            run_metadata=make_metadata(),
            task_id="t1", subtask_id="s1",
        )
        assert ledger.total_spent("t1") == pytest.approx(cost)

    def test_sums_across_rows(self, ledger):
        c1 = ledger.record(
            make_result(input_tokens=500_000),
            run_metadata=make_metadata(),
            task_id="t1", subtask_id="s1",
        )
        c2 = ledger.record(
            make_result(input_tokens=200_000, output_tokens=100_000),
            run_metadata=make_metadata(),
            task_id="t1", subtask_id="s2",
        )
        assert ledger.total_spent("t1") == pytest.approx(c1 + c2)

    def test_skips_malformed_lines(self, ledger, tmp_path):
        path = os.path.join(str(tmp_path), "t1", "buffer.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        good = json.dumps({
            "event": "buffer",
            "task_id": "t1",
            "cost_usd": 1.50,
            "timestamp_utc": "2026-05-01T00:00:00+00:00",
        })
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(good + "\n")
            fh.write("not json at all\n")
            fh.write("\n")
            fh.write(json.dumps({"event": "buffer", "cost_usd": 2.50}) + "\n")
        # 1.50 + 2.50; the bad line is skipped.
        assert ledger.total_spent("t1") == pytest.approx(4.00)

    def test_skips_rows_without_numeric_cost(self, ledger, tmp_path):
        path = os.path.join(str(tmp_path), "t1", "buffer.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": "buffer", "cost_usd": 0.50}) + "\n")
            fh.write(json.dumps({"event": "buffer"}) + "\n")
            fh.write(json.dumps({"event": "buffer", "cost_usd": None}) + "\n")
            fh.write(json.dumps({"event": "buffer", "cost_usd": "free"}) + "\n")
        assert ledger.total_spent("t1") == pytest.approx(0.50)


# ---------------------------------------------------------------------------
# total_spent_reported() — added in §5 per §4 hand-off note
# ---------------------------------------------------------------------------

class TestTotalSpentReported:
    """Sum of claude_reported_cost_usd; consumed by the §5 policy machine."""

    def test_missing_file_returns_zero(self, ledger):
        assert ledger.total_spent_reported("never-recorded") == 0.0

    def test_single_row_returns_claude_reported_cost(self, ledger):
        # ``total_cost_usd`` flows through to ``claude_reported_cost_usd``.
        ledger.record(
            make_result(input_tokens=1_000_000, total_cost_usd=0.05),
            run_metadata=make_metadata(),
            task_id="t1", subtask_id="s1",
        )
        assert ledger.total_spent_reported("t1") == pytest.approx(0.05)

    def test_sums_across_rows(self, ledger):
        ledger.record(
            make_result(total_cost_usd=0.10),
            run_metadata=make_metadata(),
            task_id="t1", subtask_id="s1",
        )
        ledger.record(
            make_result(total_cost_usd=0.20),
            run_metadata=make_metadata(),
            task_id="t1", subtask_id="s2",
        )
        ledger.record(
            make_result(total_cost_usd=0.30),
            run_metadata=make_metadata(),
            task_id="t1", subtask_id="s3",
        )
        assert ledger.total_spent_reported("t1") == pytest.approx(0.60)

    def test_skips_rows_with_none_reported_cost(self, ledger):
        # ``total_cost_usd=None`` is what Claude emits on some
        # hard-failure shapes; ``record`` still writes the row.
        ledger.record(
            make_result(total_cost_usd=0.10),
            run_metadata=make_metadata(),
            task_id="t1", subtask_id="s1",
        )
        ledger.record(
            make_result(total_cost_usd=None),
            run_metadata=make_metadata(),
            task_id="t1", subtask_id="s2",
        )
        assert ledger.total_spent_reported("t1") == pytest.approx(0.10)

    def test_skips_rows_missing_reported_cost_field(self, ledger, tmp_path):
        # Older rows from before the field existed should not blow up.
        path = os.path.join(str(tmp_path), "t1", "buffer.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "event": "buffer",
                "cost_usd": 0.50,
                "claude_reported_cost_usd": 0.75,
            }) + "\n")
            # No claude_reported_cost_usd key at all.
            fh.write(json.dumps({
                "event": "buffer",
                "cost_usd": 1.00,
            }) + "\n")
            fh.write(json.dumps({
                "event": "buffer",
                "cost_usd": 2.00,
                "claude_reported_cost_usd": "free",
            }) + "\n")
        assert ledger.total_spent_reported("t1") == pytest.approx(0.75)

    def test_diverges_from_total_spent(self, ledger):
        """Both sums coexist — one is the local figure, one is Claude's.

        Recreates the §4 live-trace finding (~5x divergence) on a
        controlled fixture so future readers can see the property
        the policy hand-off note relied on.
        """
        ledger.record(
            make_result(
                input_tokens=1_000_000,  # local cost = $15.00 at Opus
                output_tokens=0,         # keep arithmetic clean
                total_cost_usd=75.00,    # Claude's reported figure
            ),
            run_metadata=make_metadata(),
            task_id="t1", subtask_id="s1",
        )
        local = ledger.total_spent("t1")
        reported = ledger.total_spent_reported("t1")
        assert local == pytest.approx(15.00)
        assert reported == pytest.approx(75.00)
        assert reported > local


# ---------------------------------------------------------------------------
# total_spent_since()
# ---------------------------------------------------------------------------

class TestTotalSpentSince:
    """Window-scoped sum used by the §5 policy machine."""

    def _seed(self, ledger, task_id, rows):
        """Append rows with explicit timestamps & costs for testing."""
        for ts, cost in rows:
            ledger.record(
                make_result(input_tokens=0, output_tokens=0),
                run_metadata=make_metadata(timestamp_utc=ts),
                task_id=task_id, subtask_id=ts,
            )
        # Rewrite cost_usd directly because make_result with zero
        # tokens always costs 0; we want explicit cost values.
        path = os.path.join(ledger._state_root, task_id, "buffer.jsonl")
        with open(path, "r", encoding="utf-8") as fh:
            existing = [json.loads(l) for l in fh if l.strip()]
        for row, (_ts, cost) in zip(existing, rows):
            row["cost_usd"] = cost
        with open(path, "w", encoding="utf-8") as fh:
            for row in existing:
                fh.write(json.dumps(row) + "\n")

    def test_missing_file_returns_zero(self, ledger):
        assert ledger.total_spent_since(
            "nope", since_iso="2026-01-01T00:00:00+00:00",
        ) == 0.0

    def test_includes_only_rows_at_or_after_since(self, ledger):
        self._seed(ledger, "t1", [
            ("2026-04-01T00:00:00+00:00", 1.00),
            ("2026-04-15T00:00:00+00:00", 2.00),
            ("2026-05-01T00:00:00+00:00", 4.00),
            ("2026-05-15T00:00:00+00:00", 8.00),
        ])
        assert ledger.total_spent_since(
            "t1", since_iso="2026-05-01T00:00:00+00:00",
        ) == pytest.approx(4.00 + 8.00)

    def test_equality_at_boundary_is_included(self, ledger):
        # Lexicographic >= is the contract.
        self._seed(ledger, "t1", [
            ("2026-05-01T00:00:00+00:00", 3.00),
        ])
        assert ledger.total_spent_since(
            "t1", since_iso="2026-05-01T00:00:00+00:00",
        ) == pytest.approx(3.00)

    def test_all_rows_before_since_returns_zero(self, ledger):
        self._seed(ledger, "t1", [
            ("2026-04-01T00:00:00+00:00", 1.00),
            ("2026-04-15T00:00:00+00:00", 2.00),
        ])
        assert ledger.total_spent_since(
            "t1", since_iso="2026-05-01T00:00:00+00:00",
        ) == 0.0

    def test_skips_rows_without_timestamp(self, ledger, tmp_path):
        path = os.path.join(str(tmp_path), "t1", "buffer.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "event": "buffer",
                "cost_usd": 5.00,
                "timestamp_utc": "2026-05-01T00:00:00+00:00",
            }) + "\n")
            fh.write(json.dumps({
                "event": "buffer", "cost_usd": 10.00,
            }) + "\n")
            fh.write(json.dumps({
                "event": "buffer", "cost_usd": 20.00,
                "timestamp_utc": None,
            }) + "\n")
        assert ledger.total_spent_since(
            "t1", since_iso="2026-04-01T00:00:00+00:00",
        ) == pytest.approx(5.00)


# ---------------------------------------------------------------------------
# Default state root
# ---------------------------------------------------------------------------

class TestDefaultStateRoot:
    """Sanity checks on the module-level default."""

    def test_default_state_root_is_under_orchestrator(self):
        assert buffer_ledger.DEFAULT_STATE_ROOT.endswith(
            os.path.join("orchestrator", "state")
        )

    def test_default_constructor_uses_default_root(self):
        led = buffer_ledger.BufferLedger()
        path = led._path("some-task")
        assert path.startswith(buffer_ledger.DEFAULT_STATE_ROOT)
        assert path.endswith(os.path.join("some-task", "buffer.jsonl"))
