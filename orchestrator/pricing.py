"""Per-model token pricing table for Phase 3 §4.

Single hard-coded dict keyed by Claude model id. Each entry's four
``*_per_m`` fields are the Anthropic public list price in US dollars
per one million tokens of the relevant token category at phase
start. ``compute_cost(model_id, usage)`` looks up the rate row and
returns the total cost of one Claude Code worker call.

Per ``ROADMAP.md`` §Model policy this module is bumped in lockstep
with the SDK pin: when Anthropic ships a new default Claude model
the SDK pin in ``integration/llm_judge.py`` and ``uas/fuzzy.py``
moves to the new id, and a new entry lands here. ``UnknownModelError``
on lookup miss is the property that prevents a stale pricing table
from silently zero-costing a real call (PLAN §4 step 1).

Cost formula assumes the four token categories Claude Code's
stream-json ``result.usage`` payload reports
(``input_tokens``, ``output_tokens``,
``cache_creation_input_tokens``, ``cache_read_input_tokens``) and
that they bill independently — i.e. cache-read tokens are not also
counted under ``input_tokens``. That matches Anthropic's published
billing semantics as of phase start. Missing keys are treated as
zero so partial usage payloads (early SDK versions, synthetic test
fixtures) cost out cleanly.

Cache-write rate uses Anthropic's 5-minute-tier price; the 1-hour
tier (which Claude Code can opt into and bills at 2× input rather
than 1.25×) is not modelled separately. Real Claude Code traces
report a nested ``usage.cache_creation = {ephemeral_5m_input_tokens,
ephemeral_1h_input_tokens}`` breakdown alongside the flat
``cache_creation_input_tokens`` total; until that breakdown drives
a policy decision, the ledger under-bills 1h-tier writes by ~40%.
Acceptable for the policy machine's "approximate dollar headroom"
signal; revisit when the §5 policy machine starts caring about
buffer accuracy below the 10% margin.

PLAN-deviation note (§4, May 2026). The §4 PLAN's literal "Initial
entry: claude-opus-4-7" assumed worker calls would only ever
report the single configured default model. The §4 live wire-in
test against a real ``Reply with done`` worker surfaced a second
model id — ``claude-haiku-4-5-20251001`` — appearing in the same
``result.modelUsage`` map alongside Opus, even though the worker
itself was launched with no ``--model`` flag. Claude Code 2026
routes some internal turns through Haiku regardless of the user
default. Adding the Haiku entry alongside Opus is the minimal
deviation that lets the ledger actually function on real workers
while preserving the PLAN's "raise on unknown model" safety
property.
"""

# Anthropic public list prices in USD per million tokens at phase
# start (May 2026, ≤200K-prompt tier for Opus — the orchestrator
# does not yet differentiate the >200K tier; revisit when
# long-context prompts become routine). Bumped in lockstep with
# the SDK pin per ROADMAP §Model policy.
PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-7": {
        "input_per_m": 15.00,
        "output_per_m": 75.00,
        "cache_write_per_m": 18.75,
        "cache_read_per_m": 1.50,
    },
    # Claude Code routes some internal turns through Haiku 4.5
    # regardless of the user's configured default; observed in the
    # §4 live wire-in test alongside the worker's own Opus 4.7
    # invocation. Pricing values are Anthropic's public Haiku 4.5
    # list prices.
    "claude-haiku-4-5-20251001": {
        "input_per_m": 1.00,
        "output_per_m": 5.00,
        "cache_write_per_m": 1.25,
        "cache_read_per_m": 0.10,
    },
}


class UnknownModelError(KeyError):
    """Raised when ``compute_cost`` is asked for an unknown model id.

    Subclass of ``KeyError`` so downstream ``except KeyError`` blocks
    that already exist in caller code keep working; subclassing also
    keeps the failure mode visibly distinct from a generic missing
    dict key on inspection.
    """


def compute_cost(model_id: str, usage: dict) -> float:
    """Compute the dollar cost of one ``result.usage`` payload.

    Parameters mirror Claude Code's stream-json terminal ``result``
    event: ``model_id`` is the Claude model id (sourced by the
    orchestrator from ``result["modelUsage"]``'s first key per the
    Phase 3 §2 hand-off note — the top-level ``result["model"]`` is
    ``None`` on real workers); ``usage`` is the dict at
    ``result["usage"]``.

    Returns the total cost in USD as a float. Missing keys in the
    ``usage`` dict are treated as zero — synthetic fixtures and
    older SDK versions can omit cache fields without crashing the
    ledger. Raises ``UnknownModelError`` when ``model_id`` is not
    in ``PRICING`` so the ledger can never silently record a
    zero-cost call against a model the table does not know.
    """
    rates = PRICING.get(model_id)
    if rates is None:
        raise UnknownModelError(
            f"unknown model id {model_id!r}; "
            f"add an entry to orchestrator/pricing.py PRICING"
        )
    input_tokens = usage.get("input_tokens", 0) or 0
    output_tokens = usage.get("output_tokens", 0) or 0
    cache_write_tokens = usage.get("cache_creation_input_tokens", 0) or 0
    cache_read_tokens = usage.get("cache_read_input_tokens", 0) or 0
    return (
        input_tokens * rates["input_per_m"] / 1_000_000.0
        + output_tokens * rates["output_per_m"] / 1_000_000.0
        + cache_write_tokens * rates["cache_write_per_m"] / 1_000_000.0
        + cache_read_tokens * rates["cache_read_per_m"] / 1_000_000.0
    )
