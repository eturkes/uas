"""Usage-limit ledger for Phase 3 §3.

Persists ``rate_limit_event`` payloads emitted by headless Claude
Code workers (§2) to a per-task append-only JSONL file and exposes
the latest-status read pattern the policy machine consumes (§5).

Per ``docs/orchestrator.md`` the per-task layout is
``<state_root>/<task_id>/rate_limits.jsonl`` — one row per
``rate_limit_event``, stamped with the run-metadata provenance
payload plus a synthetic ``event="rate_limit"`` discriminator.
The discriminator follows ``docs/substrate.md`` §3's typed-event
recommendation so future event types (`worker_spawn`,
`policy_transition`, etc. in §6) coexist cleanly in the same JSONL
family.

The ``RateLedger`` class is a stateless wrapper bound to a
``state_root`` path so tests can redirect persistence to a
temporary directory without monkeypatching module globals.
"""

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_ROOT = os.path.join(SCRIPT_DIR, "state")

_FIVE_HOUR = "five_hour"
_SEVEN_DAY = "seven_day"
_KNOWN_RATE_TYPES = (_FIVE_HOUR, _SEVEN_DAY)


class RateLedger:
    """Per-task usage-limit ledger.

    Method calls accept ``task_id`` and route to
    ``<state_root>/<task_id>/rate_limits.jsonl``. Pass
    ``state_root`` at construction time; tests pass a ``tmp_path``
    so the suite never writes into the canonical repo state dir.
    """

    def __init__(self, state_root: str | None = None):
        self._state_root = (
            state_root if state_root is not None else DEFAULT_STATE_ROOT
        )

    def _path(self, task_id: str) -> str:
        return os.path.join(self._state_root, task_id, "rate_limits.jsonl")

    def record(
        self,
        events: list[dict],
        *,
        run_metadata: dict,
        task_id: str,
    ) -> None:
        """Append one row per rate_limit_event to the per-task ledger.

        No-op when ``events`` is empty (a worker can finish without
        emitting any rate_limit_event — early hard-failure, the
        engine refusing to start, etc.). Each row merges the
        provenance ``run_metadata``, an ``event="rate_limit"``
        discriminator, the ``task_id``, and the original event
        nested under ``rate_limit_event`` (so the upstream schema
        round-trips intact).
        """
        if not events:
            return
        path = self._path(task_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for evt in events:
                row = {
                    **run_metadata,
                    "event": "rate_limit",
                    "task_id": task_id,
                    "rate_limit_event": evt,
                }
                fh.write(json.dumps(row, default=str) + "\n")

    def _iter_events(self, task_id: str):
        """Yield (row, rate_limit_info) pairs in file order.

        Lines that are blank, malformed JSON, or do not carry a
        ``rate_limit_event.rate_limit_info`` mapping are silently
        skipped — the ledger has no schema validator on the writer
        side (substrate doc §3 Gap), so the reader degrades.
        """
        path = self._path(task_id)
        if not os.path.isfile(path):
            return
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                evt = row.get("rate_limit_event")
                if not isinstance(evt, dict):
                    continue
                info = evt.get("rate_limit_info")
                if not isinstance(info, dict):
                    continue
                yield row, info

    def current_status(self, task_id: str) -> dict:
        """Return the most-recent event per ``rateLimitType``.

        Shape: ``{"five_hour": <snapshot>|None, "seven_day":
        <snapshot>|None}``. ``<snapshot>`` is a five-key dict
        (``status``, ``resetsAt``, ``isUsingOverage``,
        ``overageStatus``, ``overageResetsAt``) extracted from the
        latest matching row's ``rate_limit_info``. Missing file →
        both keys ``None``. Forward-only scan; ledgers stay small
        enough that a reverse-seek is unnecessary.
        """
        latest: dict = {_FIVE_HOUR: None, _SEVEN_DAY: None}
        for _row, info in self._iter_events(task_id):
            rate_type = info.get("rateLimitType")
            if rate_type not in _KNOWN_RATE_TYPES:
                continue
            latest[rate_type] = {
                "status": info.get("status"),
                "resetsAt": info.get("resetsAt"),
                "isUsingOverage": info.get("isUsingOverage"),
                "overageStatus": info.get("overageStatus"),
                "overageResetsAt": info.get("overageResetsAt"),
            }
        return latest

    def internal_count(self, task_id: str) -> int:
        """Tally five_hour rate_limit_event rows in the current window.

        A "window" is identified by the most recent
        ``rate_limit_info.resetsAt`` value among five_hour events;
        events whose own ``resetsAt`` matches that latest value
        belong to the current window, older ones do not. When a
        window flips (a new event with a larger ``resetsAt``
        arrives), the count effectively resets — exactly the
        behaviour ``check_divergence`` assumes.

        seven_day events are excluded; the divergence check is
        scoped to the 5h cap (the dominant operational signal).
        """
        five_hour_resets: list = []
        for _row, info in self._iter_events(task_id):
            if info.get("rateLimitType") != _FIVE_HOUR:
                continue
            five_hour_resets.append(info.get("resetsAt"))
        if not five_hour_resets:
            return 0
        non_null = [r for r in five_hour_resets if r is not None]
        if not non_null:
            return len(five_hour_resets)
        latest_resets_at = max(non_null)
        return sum(1 for r in five_hour_resets if r == latest_resets_at)

    def check_divergence(self, task_id: str, *, threshold: int) -> bool:
        """Return True iff the recorded signal looks behind reality.

        Trigger condition: the most recent five_hour event still
        reports ``status="allowed"`` while the in-window
        ``internal_count`` exceeds ``threshold``. Indicates worker
        activity has continued past the point where the 5h limit
        should have flipped past ``"allowed"`` — a missed-signal
        case worth surfacing on stderr per ROADMAP §Phase 3
        deliverables ("divergence beyond a configured threshold
        raises an alert").
        """
        status = self.current_status(task_id)
        five_hour = status.get(_FIVE_HOUR)
        if not five_hour:
            return False
        if five_hour.get("status") != "allowed":
            return False
        count = self.internal_count(task_id)
        if count <= threshold:
            return False
        print(
            f"[rate_ledger] divergence: task_id={task_id!r} "
            f"internal_count={count} > threshold={threshold} "
            f"while five_hour.status='allowed'; the recorded "
            f"rate-limit signal may have fallen behind reality.",
            file=sys.stderr,
        )
        return True
