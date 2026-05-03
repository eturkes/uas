"""Paid-buffer ledger for Phase 3 §4.

Persists per-call token usage and computed cost from Claude Code
worker terminal ``result`` events to a per-task append-only JSONL
file. Mirrors ``orchestrator/rate_ledger.py``'s layout: same
``state_root`` constructor argument, same per-task directory under
``<state_root>/<task_id>/`` (filename ``buffer.jsonl``), same
``event="..."`` discriminator pattern (``"buffer"`` here).

Cost is computed via ``orchestrator.pricing.compute_cost`` against
the project's hard-coded model-pricing table. Claude Code's own
``result.total_cost_usd`` figure is recorded alongside as
``claude_reported_cost_usd`` for traceability — the policy machine
(§5) reads the locally-computed ``cost_usd`` so its threshold
behaviour is grounded in the table the project controls, not in an
upstream number that might silently change semantics.

Model id is sourced from ``result["modelUsage"]``'s first key per
the Phase 3 §2 hand-off note — Claude Code emits ``result["model"]
= None`` on the runs §2 captured. The fallback chain (model →
modelUsage first key) is documented inline in
``_extract_model_id``.
"""

import json
import os

from orchestrator import pricing

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_ROOT = os.path.join(SCRIPT_DIR, "state")


def _extract_model_id(result: dict) -> str:
    """Resolve the active Claude model id from a worker's terminal result.

    Tries ``result["model"]`` first (older SDK versions populate it);
    falls back to the first key of ``result["modelUsage"]`` per the
    §2 hand-off note ("Claude Code emits the active model id under
    ``result["modelUsage"]`` keyed by model id"). Raises ``ValueError``
    with a descriptive message when neither path yields a non-empty
    string — distinguishes the "result shape unexpected" failure
    mode from ``UnknownModelError`` (model id resolved but not
    priced).
    """
    model = result.get("model")
    if isinstance(model, str) and model:
        return model
    model_usage = result.get("modelUsage")
    if isinstance(model_usage, dict) and model_usage:
        first_key = next(iter(model_usage))
        if isinstance(first_key, str) and first_key:
            return first_key
    raise ValueError(
        "cannot resolve model id from result: "
        "result['model'] is empty/None and result['modelUsage'] "
        "is empty/missing"
    )


class BufferLedger:
    """Per-task paid-buffer cost ledger.

    Method calls accept ``task_id`` and route to
    ``<state_root>/<task_id>/buffer.jsonl``. Pass ``state_root`` at
    construction time; tests pass a ``tmp_path`` so the suite never
    writes into the canonical repo state directory.
    """

    def __init__(self, state_root: str | None = None):
        self._state_root = (
            state_root if state_root is not None else DEFAULT_STATE_ROOT
        )

    def _path(self, task_id: str) -> str:
        return os.path.join(self._state_root, task_id, "buffer.jsonl")

    def record(
        self,
        result: dict,
        *,
        run_metadata: dict,
        task_id: str,
        subtask_id: str,
    ) -> float:
        """Append one cost row and return the computed cost in USD.

        Reads ``result["usage"]`` (input_tokens, output_tokens,
        cache_creation_input_tokens, cache_read_input_tokens — any
        missing keys treated as 0 by ``pricing.compute_cost``);
        resolves the model id via ``_extract_model_id``; computes
        spend via ``pricing.compute_cost``; persists one row merging
        the provenance ``run_metadata``, an ``event="buffer"``
        discriminator, ``task_id`` / ``subtask_id``, the resolved
        ``model``, the original ``usage`` dict, the locally-computed
        ``cost_usd``, and Claude's reported ``claude_reported_cost_usd``
        when present.

        Raises ``pricing.UnknownModelError`` on a model id missing
        from the pricing table, and ``ValueError`` when the model
        id cannot be resolved at all. Both surface loudly rather
        than silently writing a zero-cost row — the policy property
        the §4 PLAN step 1 calls out.
        """
        usage = result.get("usage") or {}
        model = _extract_model_id(result)
        cost = pricing.compute_cost(model, usage)
        claude_reported = result.get("total_cost_usd")

        path = self._path(task_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        row = {
            **run_metadata,
            "event": "buffer",
            "task_id": task_id,
            "subtask_id": subtask_id,
            "model": model,
            "usage": usage,
            "cost_usd": cost,
            "claude_reported_cost_usd": claude_reported,
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
        return cost

    def _iter_rows(self, task_id: str):
        """Yield rows from the ledger in file order.

        Lines that are blank, malformed JSON, or non-dict are
        silently skipped — matches ``rate_ledger._iter_events``'s
        forgiving-reader posture so a single corrupted line cannot
        sink ``total_spent`` for the whole task.
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
                if not isinstance(row, dict):
                    continue
                yield row

    def total_spent(self, task_id: str) -> float:
        """Sum of ``cost_usd`` across every row in the task's ledger."""
        total = 0.0
        for row in self._iter_rows(task_id):
            cost = row.get("cost_usd")
            if isinstance(cost, (int, float)):
                total += cost
        return total

    def total_spent_reported(self, task_id: str) -> float:
        """Sum of ``claude_reported_cost_usd`` across every row.

        Per the Phase 3 §4 hand-off note (PLAN.md git history), the
        §5 policy machine reads this rather than ``total_spent``: a
        live trace showed Claude's own figure ran ~5× above the
        locally-priced cost, so a ``hard_stop_usd`` threshold against
        the local sum would only trip at ~5× the buffer drain the
        operator actually authorised. ``claude_reported_cost_usd``
        tracks real billing; ``cost_usd`` stays the
        pricing-table-grounded figure used for audit and divergence
        detection.

        Rows whose ``claude_reported_cost_usd`` is missing or
        non-numeric (older rows, hard-failure rows where Claude
        emitted no terminal ``result``, synthetic test fixtures)
        contribute zero. The locally-priced row is preserved
        as-is — only its contribution to the reported sum is
        skipped.
        """
        total = 0.0
        for row in self._iter_rows(task_id):
            cost = row.get("claude_reported_cost_usd")
            if isinstance(cost, (int, float)):
                total += cost
        return total

    def total_spent_since(self, task_id: str, *, since_iso: str) -> float:
        """Sum of ``cost_usd`` for rows with ``timestamp_utc >= since_iso``.

        Compares ISO-8601 strings lexicographically — safe because
        ``capture_run_metadata`` stamps every row with a UTC offset
        (``+00:00``) so all timestamps share a common normal form.
        Used by the §5 policy machine for "spend in the current 5h
        window" queries (since_iso is the window's start instant).
        """
        total = 0.0
        for row in self._iter_rows(task_id):
            ts = row.get("timestamp_utc")
            if not isinstance(ts, str) or ts < since_iso:
                continue
            cost = row.get("cost_usd")
            if isinstance(cost, (int, float)):
                total += cost
        return total

    def total_spent_reported_since(
        self, task_id: str, *, since_iso: str,
    ) -> float:
        """Sum of ``claude_reported_cost_usd`` for rows ``timestamp_utc >= since_iso``.

        Mirror of ``total_spent_since`` keyed on Claude's reported
        figure rather than the locally-priced one. Phase 5 §4 uses
        this to compute the "spend this invocation" line of the
        resume summary, anchored to the timestamp of the most-recent
        ``task_resume`` / ``task_create`` decision so the digest
        scopes spend to the current invocation rather than the
        cumulative total. Rows missing or with non-numeric
        ``claude_reported_cost_usd`` contribute zero, matching
        ``total_spent_reported``'s posture.
        """
        total = 0.0
        for row in self._iter_rows(task_id):
            ts = row.get("timestamp_utc")
            if not isinstance(ts, str) or ts < since_iso:
                continue
            cost = row.get("claude_reported_cost_usd")
            if isinstance(cost, (int, float)):
                total += cost
        return total
