"""List prices per million tokens (Anthropic, October 2026). Estimates only; billing comes
from the Usage and Cost API, which the nightly reconciliation compares against the ledger."""

from __future__ import annotations

# model name -> (input, output, cache read) in USD per million tokens
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
CACHE_WRITE_MULTIPLIER = {"5m": 1.25, "1h": 2.0}


def model_name(model: str) -> str:
    return model.split("/", 1)[-1]


def estimate_cost(
    model: str,
    *,
    input_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
    output_tokens: int,
    ttl: str = "1h",
) -> tuple[float, str]:
    """Return (usd, cost_basis). cost_basis is 'list' or 'unknown'."""
    price = PRICES.get(model_name(model))
    if price is None:
        return 0.0, "unknown"
    pin, pout, pread = price
    usd = (
        input_tokens * pin
        + cache_creation_input_tokens * pin * CACHE_WRITE_MULTIPLIER.get(ttl, 1.25)
        + cache_read_input_tokens * pread
        + output_tokens * pout
    ) / 1_000_000
    return round(usd, 6), "list"
