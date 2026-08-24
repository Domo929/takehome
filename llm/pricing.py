"""Token pricing and cost computation.

Rates are dollars per 1M tokens. Thinking tokens bill at the output rate, and
``SimpleResponse.output_tokens`` already includes them, so cost is a straight
two-term calculation.

Rates verified 2026-08 for Vertex AI. They are configuration, not constants: check
them before trusting any cost number this repo prints.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    input_per_1m: float
    output_per_1m: float
    note: str = ""

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        """Cost in USD. ``output_tokens`` must already include thinking tokens."""
        return (
            input_tokens * self.input_per_1m + output_tokens * self.output_per_1m
        ) / 1_000_000


# Keyed by the model id the provider reports.
PRICING: dict[str, ModelPricing] = {
    "gemini-2.5-flash": ModelPricing(
        input_per_1m=0.30,
        output_per_1m=2.50,
        note="Thinking tokens bill at the output rate.",
    ),
    "gemini-2.5-flash-lite": ModelPricing(input_per_1m=0.10, output_per_1m=0.40),
    "gemini-2.5-pro": ModelPricing(input_per_1m=1.25, output_per_1m=10.00),
}

_FALLBACK = ModelPricing(
    input_per_1m=0.30,
    output_per_1m=2.50,
    note="Unknown model; assuming gemini-2.5-flash rates.",
)


def pricing_for(model: str | None) -> ModelPricing:
    """Look up pricing, tolerating version suffixes like ``gemini-2.5-flash-002``."""
    if not model:
        return _FALLBACK
    if model in PRICING:
        return PRICING[model]
    for known, price in PRICING.items():
        if model.startswith(known):
            return price
    return _FALLBACK


def cost_usd(model: str | None, input_tokens: int, output_tokens: int) -> float:
    return pricing_for(model).cost(input_tokens, output_tokens)
