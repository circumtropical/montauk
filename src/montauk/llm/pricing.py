"""Best-effort per-model pricing for cost estimates (spec 16.3).

When a model isn't in the table, estimates are reported as unavailable
rather than invented (spec 16.3).
"""

from __future__ import annotations

# ($ per 1M input tokens, $ per 1M output tokens). Anthropic first-party API
# rates; adjust or extend from Settings-configured pricing metadata.
_PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-fable-5-1": (10.00, 50.00),
}


def _normalize(model: str) -> str:
    m = model.strip().lower()
    # strip a date suffix like -20251001 and any "anthropic/" / "claude-3-5-" noise
    m = m.split("@")[0]
    for known in _PRICES:
        if m == known or m.startswith(known + "-"):
            return known
    return m


def rates_for(model: str, *, override: tuple[float, float] | None = None) -> tuple[float, float] | None:
    if override is not None:
        return override
    return _PRICES.get(_normalize(model))


def estimate_cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    override: tuple[float, float] | None = None,
) -> float | None:
    rates = rates_for(model, override=override)
    if rates is None:
        return None
    in_rate, out_rate = rates
    return round(input_tokens / 1_000_000 * in_rate + output_tokens / 1_000_000 * out_rate, 6)
