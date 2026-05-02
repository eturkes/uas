"""Three-state policy machine for Phase 3 §5.

Consumes the §3 RateLedger snapshot and the §4 BufferLedger total to
issue a single next-action verdict per call: ``go`` /
``pause_until`` / ``wrap_up`` / ``halt``. Configured from a
committed TOML default (``orchestrator/policy.default.toml``) merged
with an optional per-task override at
``<state_root>/<task_id>/policy.toml``.

Ablatable per CLAUDE.md core principle 2: setting ``enabled = false``
in either the default or a per-task override short-circuits
``decide()`` to ``{"action": "go"}`` regardless of cap state. Tested.

Rule order (evaluated top-to-bottom; first match wins):

1. ``buffer_total >= buffer.hard_stop_usd``    → ``halt``
2. ``rate_status.seven_day.status != "allowed"`` and
   ``seven_day.soft_cap_action == "wrap_up"``  → ``wrap_up``
3. ``rate_status.five_hour.status != "allowed"`` and
   ``five_hour.soft_cap_action == "pause"``    → ``pause_until``
4. (default)                                   → ``go``

The ``soft_cap_action`` validators currently restrict each cap to
exactly one accepted value (5h: ``"pause"``, 7d: ``"wrap_up"``).
The TOML schema exists for §5+ informed iteration that may add
new rule pairings; until then, "configure soft_cap_action to
something else" is rejected at load time so the policy can never
sit in a "valid config but rule-doesn't-fire" state.

Per the Phase 3 §4 hand-off note (PLAN.md git history),
``buffer_total`` should be sourced from
``BufferLedger.total_spent_reported(task_id)`` rather than
``total_spent(task_id)``: Claude's own ``total_cost_usd`` tracks
real buffer drain ~5× more accurately than the locally-priced
sum on the §4 trace. The committed ``hard_stop_usd = 200.00``
default is calibrated against the reported figure.

Time semantics. ``rate_status.five_hour.resetsAt`` is an ISO-8601
UTC timestamp (Claude Code's stream-json schema). ``decide()``
parses it to a unix epoch float for the ``until`` field so the
orchestrator's main loop can compare against wall-clock time
without re-implementing the parsing. ``until = None`` is returned
on a missing or malformed timestamp; the loop interprets that as
"sleep for an implementation-chosen duration then re-evaluate"
(decision deferred to §7/§8).
"""

import os
import sys
import tomllib
from datetime import datetime, timezone
from typing import Literal, TypedDict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_POLICY_PATH = os.path.join(SCRIPT_DIR, "policy.default.toml")
DEFAULT_STATE_ROOT = os.path.join(SCRIPT_DIR, "state")

# Allowed soft_cap_action values per cap. Restricting to a single
# value each is intentional — see module docstring.
_VALID_FIVE_HOUR_ACTIONS: tuple[str, ...] = ("pause",)
_VALID_SEVEN_DAY_ACTIONS: tuple[str, ...] = ("wrap_up",)

ActionVerdict = Literal["go", "pause_until", "wrap_up", "halt"]


class PolicyDecision(TypedDict):
    action: ActionVerdict
    reason: str
    until: float | None


class PolicyError(ValueError):
    """Raised on malformed, missing, or unknown-value policy TOML."""


def _parse_iso8601(value: object) -> float | None:
    """Parse an ISO-8601 timestamp to a unix epoch float.

    Accepts the ``Z`` suffix Claude Code emits (``2026-05-01T20:00:00Z``)
    by rewriting it to ``+00:00`` before delegating to
    ``datetime.fromisoformat``. Naive timestamps (no tz) are treated
    as UTC. Returns ``None`` on missing / malformed input so callers
    can pass through whatever ``rate_status`` provided without
    pre-validating.
    """
    if not isinstance(value, str) or not value:
        return None
    s = value
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _load_toml(path: str) -> dict:
    if not os.path.isfile(path):
        raise PolicyError(f"policy file not found: {path}")
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"malformed TOML at {path}: {exc}") from exc


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base; override wins on collisions.

    Nested dicts merge key-by-key; non-dict values from override
    replace the base value entirely. A per-task override TOML can
    therefore change a single nested key without restating the
    whole default.
    """
    out = dict(base)
    for key, value in override.items():
        if (
            key in out
            and isinstance(out[key], dict)
            and isinstance(value, dict)
        ):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _require_bool(config: dict, key: str) -> bool:
    value = config.get(key)
    if not isinstance(value, bool):
        raise PolicyError(
            f"top-level {key!r} must be a bool; got {value!r}"
        )
    return value


def _require_table(config: dict, key: str) -> dict:
    value = config.get(key)
    if not isinstance(value, dict):
        raise PolicyError(
            f"[{key}] must be a TOML table; got {value!r}"
        )
    return value


def _require_action(
    table: dict, table_name: str, allowed: tuple[str, ...],
) -> str:
    value = table.get("soft_cap_action")
    if value not in allowed:
        raise PolicyError(
            f"{table_name}.soft_cap_action must be one of "
            f"{list(allowed)}; got {value!r}"
        )
    return value  # type: ignore[return-value]


def _require_number(
    table: dict, table_name: str, key: str,
) -> float:
    value = table.get(key)
    # ``bool`` is a subclass of ``int`` in Python; reject it so
    # ``hard_stop_usd = true`` doesn't silently coerce to ``1.0``.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyError(
            f"{table_name}.{key} must be numeric; got {value!r}"
        )
    return float(value)


def _require_int(
    table: dict, table_name: str, key: str,
) -> int:
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyError(
            f"{table_name}.{key} must be an int; got {value!r}"
        )
    return value


class Policy:
    """Three-state policy machine.

    Construct via ``Policy.load(...)`` rather than the constructor;
    ``load`` runs the validator that produces the constructor's
    typed arguments. Direct construction is exposed for tests that
    want to bypass the TOML round-trip.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        five_hour_soft_cap_action: str,
        seven_day_soft_cap_action: str,
        hard_stop_usd: float,
        warn_usd: float,
        divergence_threshold: int,
    ) -> None:
        self.enabled = enabled
        self.five_hour_soft_cap_action = five_hour_soft_cap_action
        self.seven_day_soft_cap_action = seven_day_soft_cap_action
        self.hard_stop_usd = hard_stop_usd
        self.warn_usd = warn_usd
        self.divergence_threshold = divergence_threshold

    @classmethod
    def load(
        cls,
        *,
        task_id: str | None = None,
        state_root: str | None = None,
        default_path: str | None = None,
    ) -> "Policy":
        """Load policy: per-task override merged over committed default.

        ``task_id`` is the task whose override at
        ``<state_root>/<task_id>/policy.toml`` is consulted; when
        absent or that file is missing, only the committed default
        applies. ``default_path`` overrides the committed default
        location (test-only; production callers omit it). ``state_root``
        defaults to ``orchestrator/state/``.
        """
        path = default_path or DEFAULT_POLICY_PATH
        config = _load_toml(path)

        if task_id is not None:
            root = state_root if state_root is not None else DEFAULT_STATE_ROOT
            override_path = os.path.join(root, task_id, "policy.toml")
            if os.path.isfile(override_path):
                override = _load_toml(override_path)
                config = _deep_merge(config, override)

        return cls._from_config(config)

    @classmethod
    def _from_config(cls, config: dict) -> "Policy":
        enabled = _require_bool(config, "enabled")
        five_hour = _require_table(config, "five_hour")
        seven_day = _require_table(config, "seven_day")
        buffer_section = _require_table(config, "buffer")
        divergence = _require_table(config, "divergence")

        return cls(
            enabled=enabled,
            five_hour_soft_cap_action=_require_action(
                five_hour, "five_hour", _VALID_FIVE_HOUR_ACTIONS,
            ),
            seven_day_soft_cap_action=_require_action(
                seven_day, "seven_day", _VALID_SEVEN_DAY_ACTIONS,
            ),
            hard_stop_usd=_require_number(
                buffer_section, "buffer", "hard_stop_usd",
            ),
            warn_usd=_require_number(
                buffer_section, "buffer", "warn_usd",
            ),
            divergence_threshold=_require_int(
                divergence, "divergence", "threshold",
            ),
        )

    def decide(
        self,
        rate_status: dict,
        buffer_total: float,
        *,
        now: float,
    ) -> PolicyDecision:
        """Return the next-action verdict for one orchestrator step.

        Parameters mirror the substrate APIs the orchestrator's main
        loop already calls:

        - ``rate_status``: the dict returned by
          ``RateLedger.current_status(task_id)``. Shape is
          ``{"five_hour": {status, resetsAt, ...} | None,
          "seven_day": {status, resetsAt, ...} | None}``.
        - ``buffer_total``: the float returned by
          ``BufferLedger.total_spent_reported(task_id)`` per the §4
          hand-off note. Callers using ``total_spent()`` instead
          will get a ~5× under-bill against the threshold default.
        - ``now``: current wall-clock time as a unix epoch float.
          Carried for log-context use and forward-compatibility;
          the current ruleset does not consume it.

        Return shape is the ``PolicyDecision`` TypedDict. ``until``
        is the unix epoch float at which the orchestrator's main
        loop should re-evaluate, or ``None`` for non-pause verdicts
        (or pause verdicts where ``resetsAt`` was missing /
        malformed — the loop's call site decides how to degrade).
        """
        del now  # forward-compatibility; see docstring.

        if not self.enabled:
            return {
                "action": "go",
                "reason": "policy disabled",
                "until": None,
            }

        if buffer_total >= self.hard_stop_usd:
            return {
                "action": "halt",
                "reason": (
                    f"buffer_total {buffer_total:.2f} >= "
                    f"hard_stop_usd {self.hard_stop_usd:.2f}"
                ),
                "until": None,
            }

        seven_day = rate_status.get("seven_day") if rate_status else None
        if (
            isinstance(seven_day, dict)
            and seven_day.get("status") not in (None, "allowed")
            and self.seven_day_soft_cap_action == "wrap_up"
        ):
            return {
                "action": "wrap_up",
                "reason": (
                    f"seven_day.status={seven_day.get('status')!r} "
                    f"and seven_day.soft_cap_action='wrap_up'"
                ),
                "until": None,
            }

        five_hour = rate_status.get("five_hour") if rate_status else None
        if (
            isinstance(five_hour, dict)
            and five_hour.get("status") not in (None, "allowed")
            and self.five_hour_soft_cap_action == "pause"
        ):
            until = _parse_iso8601(five_hour.get("resetsAt"))
            if until is None:
                print(
                    f"[policy] pause_until requested but resetsAt is "
                    f"missing/malformed (got "
                    f"{five_hour.get('resetsAt')!r}); orchestrator "
                    f"loop must choose a fallback wake time.",
                    file=sys.stderr,
                )
            return {
                "action": "pause_until",
                "reason": (
                    f"five_hour.status={five_hour.get('status')!r} "
                    f"and five_hour.soft_cap_action='pause'"
                ),
                "until": until,
            }

        return {
            "action": "go",
            "reason": "all caps allowed",
            "until": None,
        }
