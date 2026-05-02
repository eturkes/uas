"""Tests for ``orchestrator.policy.Policy`` (Phase 3 §5).

Pure-Python tests against synthetic ``rate_status`` snapshots,
synthetic ``buffer_total`` floats, and TOML fixtures the tests
themselves write to ``tmp_path``. No container engine, no live
Claude — every code path through ``Policy.load`` and
``Policy.decide`` is exercised against constructed inputs.

Coverage:

- ``_parse_iso8601`` helper (Z suffix, +00:00 suffix, naive,
  malformed, non-string).
- ``Policy.load`` — committed default, per-task override merge,
  missing override falls back, missing default raises.
- Validator — every required field, every type constraint, the
  two ``soft_cap_action`` allow-lists, malformed TOML.
- ``Policy.decide`` — every transition cell of the
  (rate_status × buffer_total) table, precedence between rules,
  edge cases (empty rate_status, missing snapshots, missing
  status fields).
- Ablation flag — ``enabled = false`` short-circuits to ``go``.
"""

import os
import textwrap

import pytest

from orchestrator import policy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_DEFAULT_TOML = textwrap.dedent(
    """\
    enabled = true

    [five_hour]
    soft_cap_action = "pause"

    [seven_day]
    soft_cap_action = "wrap_up"

    [buffer]
    hard_stop_usd = 200.00
    warn_usd = 100.00

    [divergence]
    threshold = 50
    """
)


def write_default(tmp_path, body: str = VALID_DEFAULT_TOML) -> str:
    path = os.path.join(str(tmp_path), "policy.default.toml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def write_override(state_root: str, task_id: str, body: str) -> str:
    task_dir = os.path.join(state_root, task_id)
    os.makedirs(task_dir, exist_ok=True)
    path = os.path.join(task_dir, "policy.toml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def make_rate_status(
    *,
    five_hour: dict | None = None,
    seven_day: dict | None = None,
) -> dict:
    """Mirror ``RateLedger.current_status`` shape."""
    return {"five_hour": five_hour, "seven_day": seven_day}


def make_snapshot(
    *,
    status: str = "allowed",
    resets_at: str | None = "2026-05-01T20:00:00Z",
) -> dict:
    return {
        "status": status,
        "resetsAt": resets_at,
        "isUsingOverage": False,
        "overageStatus": "allowed",
        "overageResetsAt": "2026-05-08T00:00:00Z",
    }


NOW = 1_777_500_000.0  # arbitrary fixed unix epoch for deterministic tests


# ---------------------------------------------------------------------------
# _parse_iso8601 helper
# ---------------------------------------------------------------------------

class TestParseISO8601:
    def test_z_suffix(self):
        # 2026-05-01T20:00:00Z = 1777665600 unix epoch.
        assert policy._parse_iso8601("2026-05-01T20:00:00Z") == 1777665600.0

    def test_offset_suffix(self):
        assert (
            policy._parse_iso8601("2026-05-01T20:00:00+00:00")
            == 1777665600.0
        )

    def test_naive_treated_as_utc(self):
        # Naive iso parses in UTC per the helper's contract.
        assert policy._parse_iso8601("2026-05-01T20:00:00") == 1777665600.0

    def test_non_utc_offset_normalised(self):
        # 2026-05-01T20:00:00-05:00 = 2026-05-02T01:00:00Z =
        # 1777665600 (the Z baseline) + 5h × 3600 = 1777683600.
        result = policy._parse_iso8601("2026-05-01T20:00:00-05:00")
        assert result == 1777683600.0

    def test_malformed_returns_none(self):
        assert policy._parse_iso8601("not-a-timestamp") is None

    def test_empty_returns_none(self):
        assert policy._parse_iso8601("") is None

    def test_none_returns_none(self):
        assert policy._parse_iso8601(None) is None

    def test_non_string_returns_none(self):
        assert policy._parse_iso8601(12345) is None
        assert policy._parse_iso8601({"x": 1}) is None


# ---------------------------------------------------------------------------
# _deep_merge helper
# ---------------------------------------------------------------------------

class TestDeepMerge:
    def test_top_level_override_wins(self):
        merged = policy._deep_merge({"a": 1}, {"a": 2})
        assert merged == {"a": 2}

    def test_missing_key_added(self):
        merged = policy._deep_merge({"a": 1}, {"b": 2})
        assert merged == {"a": 1, "b": 2}

    def test_nested_dict_merges_key_by_key(self):
        merged = policy._deep_merge(
            {"buffer": {"hard_stop_usd": 200.0, "warn_usd": 100.0}},
            {"buffer": {"hard_stop_usd": 50.0}},
        )
        assert merged == {
            "buffer": {"hard_stop_usd": 50.0, "warn_usd": 100.0}
        }

    def test_non_dict_replaces_dict(self):
        merged = policy._deep_merge({"a": {"b": 1}}, {"a": "string"})
        assert merged == {"a": "string"}

    def test_dict_replaces_non_dict(self):
        merged = policy._deep_merge({"a": "string"}, {"a": {"b": 1}})
        assert merged == {"a": {"b": 1}}

    def test_does_not_mutate_inputs(self):
        base = {"a": {"b": 1}}
        override = {"a": {"c": 2}}
        merged = policy._deep_merge(base, override)
        assert base == {"a": {"b": 1}}
        assert override == {"a": {"c": 2}}
        assert merged == {"a": {"b": 1, "c": 2}}


# ---------------------------------------------------------------------------
# Policy.load — committed default, override merge, missing files
# ---------------------------------------------------------------------------

class TestPolicyLoad:
    def test_load_committed_default(self, tmp_path):
        path = write_default(tmp_path)
        p = policy.Policy.load(default_path=path)
        assert p.enabled is True
        assert p.five_hour_soft_cap_action == "pause"
        assert p.seven_day_soft_cap_action == "wrap_up"
        assert p.hard_stop_usd == 200.00
        assert p.warn_usd == 100.00
        assert p.divergence_threshold == 50

    def test_committed_default_in_repo_is_loadable(self):
        """Sanity-check the default TOML shipped in the repo."""
        p = policy.Policy.load()
        assert p.enabled is True
        assert p.five_hour_soft_cap_action == "pause"
        assert p.seven_day_soft_cap_action == "wrap_up"
        assert p.hard_stop_usd == 200.00
        assert p.warn_usd == 100.00
        assert p.divergence_threshold == 50

    def test_per_task_override_merges_partial_section(self, tmp_path):
        """Override changes one field; other fields stay default."""
        default_path = write_default(tmp_path)
        state_root = os.path.join(str(tmp_path), "state")
        write_override(
            state_root,
            "task-a",
            "[buffer]\nhard_stop_usd = 50.0\n",
        )
        p = policy.Policy.load(
            task_id="task-a",
            state_root=state_root,
            default_path=default_path,
        )
        # Overridden:
        assert p.hard_stop_usd == 50.0
        # Default preserved (warn_usd not mentioned in override):
        assert p.warn_usd == 100.00
        # Default preserved (other sections untouched):
        assert p.divergence_threshold == 50

    def test_per_task_override_top_level_enabled(self, tmp_path):
        default_path = write_default(tmp_path)
        state_root = os.path.join(str(tmp_path), "state")
        write_override(state_root, "task-a", "enabled = false\n")
        p = policy.Policy.load(
            task_id="task-a",
            state_root=state_root,
            default_path=default_path,
        )
        assert p.enabled is False

    def test_missing_override_file_falls_back_to_default(self, tmp_path):
        default_path = write_default(tmp_path)
        state_root = os.path.join(str(tmp_path), "state")
        # No override file written.
        p = policy.Policy.load(
            task_id="task-a",
            state_root=state_root,
            default_path=default_path,
        )
        assert p.hard_stop_usd == 200.00

    def test_missing_default_path_raises(self, tmp_path):
        nowhere = os.path.join(str(tmp_path), "does-not-exist.toml")
        with pytest.raises(policy.PolicyError, match="not found"):
            policy.Policy.load(default_path=nowhere)

    def test_no_task_id_skips_override_lookup(self, tmp_path):
        """Without ``task_id`` only the default is consulted."""
        default_path = write_default(tmp_path)
        # Even if a state_root with overrides exists, no task_id ⇒ ignored.
        state_root = os.path.join(str(tmp_path), "state")
        write_override(state_root, "task-a", "[buffer]\nhard_stop_usd = 1.0\n")
        p = policy.Policy.load(default_path=default_path)
        assert p.hard_stop_usd == 200.00

    def test_malformed_toml_raises(self, tmp_path):
        path = write_default(tmp_path, body="enabled = ???\n")
        with pytest.raises(policy.PolicyError, match="malformed TOML"):
            policy.Policy.load(default_path=path)


# ---------------------------------------------------------------------------
# Policy.load — validator
# ---------------------------------------------------------------------------

class TestPolicyValidator:
    def test_unknown_five_hour_action_raises(self, tmp_path):
        body = VALID_DEFAULT_TOML.replace(
            'soft_cap_action = "pause"',
            'soft_cap_action = "wrap_up"',
            1,  # only the first occurrence (five_hour)
        )
        path = write_default(tmp_path, body=body)
        with pytest.raises(
            policy.PolicyError, match="five_hour.soft_cap_action",
        ):
            policy.Policy.load(default_path=path)

    def test_unknown_seven_day_action_raises(self, tmp_path):
        body = VALID_DEFAULT_TOML.replace(
            '[seven_day]\nsoft_cap_action = "wrap_up"',
            '[seven_day]\nsoft_cap_action = "pause"',
        )
        path = write_default(tmp_path, body=body)
        with pytest.raises(
            policy.PolicyError, match="seven_day.soft_cap_action",
        ):
            policy.Policy.load(default_path=path)

    def test_halt_in_five_hour_rejected(self, tmp_path):
        body = VALID_DEFAULT_TOML.replace(
            '[five_hour]\nsoft_cap_action = "pause"',
            '[five_hour]\nsoft_cap_action = "halt"',
        )
        path = write_default(tmp_path, body=body)
        with pytest.raises(policy.PolicyError):
            policy.Policy.load(default_path=path)

    def test_missing_soft_cap_action_raises(self, tmp_path):
        body = VALID_DEFAULT_TOML.replace(
            '[five_hour]\nsoft_cap_action = "pause"\n',
            "[five_hour]\n",
        )
        path = write_default(tmp_path, body=body)
        with pytest.raises(
            policy.PolicyError, match="five_hour.soft_cap_action",
        ):
            policy.Policy.load(default_path=path)

    def test_missing_five_hour_table_raises(self, tmp_path):
        body = VALID_DEFAULT_TOML.replace(
            '[five_hour]\nsoft_cap_action = "pause"\n\n', "",
        )
        path = write_default(tmp_path, body=body)
        with pytest.raises(policy.PolicyError, match=r"\[five_hour\]"):
            policy.Policy.load(default_path=path)

    def test_missing_seven_day_table_raises(self, tmp_path):
        body = VALID_DEFAULT_TOML.replace(
            '[seven_day]\nsoft_cap_action = "wrap_up"\n\n', "",
        )
        path = write_default(tmp_path, body=body)
        with pytest.raises(policy.PolicyError, match=r"\[seven_day\]"):
            policy.Policy.load(default_path=path)

    def test_missing_buffer_table_raises(self, tmp_path):
        body = VALID_DEFAULT_TOML.replace(
            "[buffer]\nhard_stop_usd = 200.00\nwarn_usd = 100.00\n\n", "",
        )
        path = write_default(tmp_path, body=body)
        with pytest.raises(policy.PolicyError, match=r"\[buffer\]"):
            policy.Policy.load(default_path=path)

    def test_missing_divergence_table_raises(self, tmp_path):
        body = VALID_DEFAULT_TOML.replace(
            "[divergence]\nthreshold = 50\n", "",
        )
        path = write_default(tmp_path, body=body)
        with pytest.raises(policy.PolicyError, match=r"\[divergence\]"):
            policy.Policy.load(default_path=path)

    def test_missing_hard_stop_raises(self, tmp_path):
        body = VALID_DEFAULT_TOML.replace(
            "hard_stop_usd = 200.00\n", "",
        )
        path = write_default(tmp_path, body=body)
        with pytest.raises(
            policy.PolicyError, match="buffer.hard_stop_usd",
        ):
            policy.Policy.load(default_path=path)

    def test_non_numeric_hard_stop_raises(self, tmp_path):
        body = VALID_DEFAULT_TOML.replace(
            "hard_stop_usd = 200.00",
            'hard_stop_usd = "lots"',
        )
        path = write_default(tmp_path, body=body)
        with pytest.raises(
            policy.PolicyError, match="buffer.hard_stop_usd",
        ):
            policy.Policy.load(default_path=path)

    def test_bool_hard_stop_rejected(self, tmp_path):
        # ``bool`` subclasses ``int`` in Python; ``hard_stop_usd = true``
        # would silently coerce to 1.0 without the explicit reject.
        body = VALID_DEFAULT_TOML.replace(
            "hard_stop_usd = 200.00",
            "hard_stop_usd = true",
        )
        path = write_default(tmp_path, body=body)
        with pytest.raises(
            policy.PolicyError, match="buffer.hard_stop_usd",
        ):
            policy.Policy.load(default_path=path)

    def test_non_numeric_warn_raises(self, tmp_path):
        body = VALID_DEFAULT_TOML.replace(
            "warn_usd = 100.00", 'warn_usd = "approaching"',
        )
        path = write_default(tmp_path, body=body)
        with pytest.raises(
            policy.PolicyError, match="buffer.warn_usd",
        ):
            policy.Policy.load(default_path=path)

    def test_non_int_threshold_raises(self, tmp_path):
        body = VALID_DEFAULT_TOML.replace(
            "threshold = 50", "threshold = 50.5",
        )
        path = write_default(tmp_path, body=body)
        with pytest.raises(
            policy.PolicyError, match="divergence.threshold",
        ):
            policy.Policy.load(default_path=path)

    def test_non_bool_enabled_raises(self, tmp_path):
        body = VALID_DEFAULT_TOML.replace(
            "enabled = true", 'enabled = "yes"',
        )
        path = write_default(tmp_path, body=body)
        with pytest.raises(policy.PolicyError, match="'enabled'"):
            policy.Policy.load(default_path=path)

    def test_missing_enabled_raises(self, tmp_path):
        body = VALID_DEFAULT_TOML.replace("enabled = true\n\n", "")
        path = write_default(tmp_path, body=body)
        with pytest.raises(policy.PolicyError, match="'enabled'"):
            policy.Policy.load(default_path=path)


# ---------------------------------------------------------------------------
# Policy.decide — transition cells
# ---------------------------------------------------------------------------

@pytest.fixture
def default_policy(tmp_path):
    """A loaded Policy from the canonical default TOML."""
    path = write_default(tmp_path)
    return policy.Policy.load(default_path=path)


class TestDecideGo:
    def test_all_caps_allowed_buffer_clean(self, default_policy):
        rs = make_rate_status(
            five_hour=make_snapshot(status="allowed"),
            seven_day=make_snapshot(status="allowed"),
        )
        result = default_policy.decide(rs, 0.0, now=NOW)
        assert result["action"] == "go"
        assert result["until"] is None
        assert "all caps allowed" in result["reason"]

    def test_empty_rate_status(self, default_policy):
        # Both keys missing — treated as no signal ⇒ go.
        result = default_policy.decide({}, 0.0, now=NOW)
        assert result["action"] == "go"

    def test_both_snapshots_none(self, default_policy):
        rs = make_rate_status(five_hour=None, seven_day=None)
        result = default_policy.decide(rs, 0.0, now=NOW)
        assert result["action"] == "go"

    def test_status_field_none_treated_as_allowed(self, default_policy):
        rs = make_rate_status(
            five_hour={"status": None, "resetsAt": None,
                       "isUsingOverage": False,
                       "overageStatus": None, "overageResetsAt": None},
            seven_day=None,
        )
        result = default_policy.decide(rs, 0.0, now=NOW)
        assert result["action"] == "go"

    def test_buffer_below_hard_stop_no_other_triggers(self, default_policy):
        rs = make_rate_status()
        result = default_policy.decide(rs, 199.99, now=NOW)
        assert result["action"] == "go"


class TestDecideHalt:
    def test_buffer_at_or_above_hard_stop(self, default_policy):
        rs = make_rate_status()
        # At the boundary — ``>=`` per the rule.
        result = default_policy.decide(rs, 200.00, now=NOW)
        assert result["action"] == "halt"
        assert "hard_stop_usd" in result["reason"]
        assert result["until"] is None

    def test_buffer_far_above_hard_stop(self, default_policy):
        result = default_policy.decide(make_rate_status(), 1000.0, now=NOW)
        assert result["action"] == "halt"

    def test_halt_wins_over_seven_day_wrap_up(self, default_policy):
        """Buffer hard-stop has highest precedence."""
        rs = make_rate_status(
            seven_day=make_snapshot(status="limit_reached"),
        )
        result = default_policy.decide(rs, 250.0, now=NOW)
        assert result["action"] == "halt"

    def test_halt_wins_over_five_hour_pause(self, default_policy):
        rs = make_rate_status(
            five_hour=make_snapshot(status="limit_reached"),
        )
        result = default_policy.decide(rs, 250.0, now=NOW)
        assert result["action"] == "halt"


class TestDecideWrapUp:
    def test_seven_day_warning(self, default_policy):
        rs = make_rate_status(
            seven_day=make_snapshot(status="warning"),
        )
        result = default_policy.decide(rs, 0.0, now=NOW)
        assert result["action"] == "wrap_up"
        assert "seven_day.status='warning'" in result["reason"]
        assert result["until"] is None

    def test_seven_day_limit_reached(self, default_policy):
        rs = make_rate_status(
            seven_day=make_snapshot(status="limit_reached"),
        )
        result = default_policy.decide(rs, 0.0, now=NOW)
        assert result["action"] == "wrap_up"

    def test_seven_day_wins_over_five_hour_pause(self, default_policy):
        """Seven-day evaluated before five-hour."""
        rs = make_rate_status(
            five_hour=make_snapshot(status="limit_reached"),
            seven_day=make_snapshot(status="warning"),
        )
        result = default_policy.decide(rs, 0.0, now=NOW)
        assert result["action"] == "wrap_up"


class TestDecidePauseUntil:
    def test_five_hour_warning(self, default_policy):
        rs = make_rate_status(
            five_hour=make_snapshot(
                status="warning",
                resets_at="2026-05-01T20:00:00Z",
            ),
        )
        result = default_policy.decide(rs, 0.0, now=NOW)
        assert result["action"] == "pause_until"
        assert result["until"] == 1777665600.0
        assert "five_hour.status='warning'" in result["reason"]

    def test_five_hour_limit_reached(self, default_policy):
        rs = make_rate_status(
            five_hour=make_snapshot(status="limit_reached"),
        )
        result = default_policy.decide(rs, 0.0, now=NOW)
        assert result["action"] == "pause_until"

    def test_resets_at_missing_returns_until_none(
        self, default_policy, capsys,
    ):
        rs = make_rate_status(
            five_hour=make_snapshot(
                status="limit_reached", resets_at=None,
            ),
        )
        result = default_policy.decide(rs, 0.0, now=NOW)
        assert result["action"] == "pause_until"
        assert result["until"] is None
        captured = capsys.readouterr()
        assert "resetsAt is missing/malformed" in captured.err

    def test_resets_at_malformed_returns_until_none(
        self, default_policy, capsys,
    ):
        rs = make_rate_status(
            five_hour=make_snapshot(
                status="limit_reached", resets_at="not-a-timestamp",
            ),
        )
        result = default_policy.decide(rs, 0.0, now=NOW)
        assert result["action"] == "pause_until"
        assert result["until"] is None
        captured = capsys.readouterr()
        assert "resetsAt is missing/malformed" in captured.err


# ---------------------------------------------------------------------------
# Ablation flag
# ---------------------------------------------------------------------------

class TestDecideAblation:
    def test_disabled_returns_go_with_nothing_triggered(self, tmp_path):
        body = VALID_DEFAULT_TOML.replace("enabled = true", "enabled = false")
        path = write_default(tmp_path, body=body)
        p = policy.Policy.load(default_path=path)
        rs = make_rate_status()
        result = p.decide(rs, 0.0, now=NOW)
        assert result == {
            "action": "go",
            "reason": "policy disabled",
            "until": None,
        }

    def test_disabled_short_circuits_buffer_hard_stop(self, tmp_path):
        body = VALID_DEFAULT_TOML.replace("enabled = true", "enabled = false")
        path = write_default(tmp_path, body=body)
        p = policy.Policy.load(default_path=path)
        result = p.decide(make_rate_status(), 1_000_000.0, now=NOW)
        assert result["action"] == "go"
        assert result["reason"] == "policy disabled"

    def test_disabled_short_circuits_seven_day_warning(self, tmp_path):
        body = VALID_DEFAULT_TOML.replace("enabled = true", "enabled = false")
        path = write_default(tmp_path, body=body)
        p = policy.Policy.load(default_path=path)
        rs = make_rate_status(
            seven_day=make_snapshot(status="limit_reached"),
        )
        result = p.decide(rs, 0.0, now=NOW)
        assert result["action"] == "go"

    def test_disabled_short_circuits_five_hour_warning(self, tmp_path):
        body = VALID_DEFAULT_TOML.replace("enabled = true", "enabled = false")
        path = write_default(tmp_path, body=body)
        p = policy.Policy.load(default_path=path)
        rs = make_rate_status(
            five_hour=make_snapshot(status="warning"),
        )
        result = p.decide(rs, 0.0, now=NOW)
        assert result["action"] == "go"

    def test_disabled_via_per_task_override(self, tmp_path):
        default_path = write_default(tmp_path)
        state_root = os.path.join(str(tmp_path), "state")
        write_override(state_root, "task-a", "enabled = false\n")
        p = policy.Policy.load(
            task_id="task-a",
            state_root=state_root,
            default_path=default_path,
        )
        # Default file says enabled; per-task override disables.
        result = p.decide(make_rate_status(), 1_000_000.0, now=NOW)
        assert result["action"] == "go"
        assert result["reason"] == "policy disabled"


# ---------------------------------------------------------------------------
# Per-task TOML override behaviour
# ---------------------------------------------------------------------------

class TestPerTaskOverride:
    def test_lower_hard_stop_triggers_halt_earlier(self, tmp_path):
        default_path = write_default(tmp_path)
        state_root = os.path.join(str(tmp_path), "state")
        write_override(
            state_root, "task-a",
            "[buffer]\nhard_stop_usd = 1.50\nwarn_usd = 0.50\n",
        )
        p = policy.Policy.load(
            task_id="task-a",
            state_root=state_root,
            default_path=default_path,
        )
        # Default would allow this — override does not.
        result = p.decide(make_rate_status(), 1.50, now=NOW)
        assert result["action"] == "halt"

    def test_per_task_divergence_threshold_override(self, tmp_path):
        default_path = write_default(tmp_path)
        state_root = os.path.join(str(tmp_path), "state")
        write_override(
            state_root, "task-a",
            "[divergence]\nthreshold = 5\n",
        )
        p = policy.Policy.load(
            task_id="task-a",
            state_root=state_root,
            default_path=default_path,
        )
        assert p.divergence_threshold == 5
