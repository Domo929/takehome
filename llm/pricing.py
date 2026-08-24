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
    # Cache hits bill at a fraction of the input rate. Implicit caching is on by
    # default for Gemini 2.5, so this discount can apply without anyone opting in --
    # which means charging every prompt token at full rate silently *overstates* cost.
    cached_input_multiplier: float = 0.10
    note: str = ""

    def cost(
        self, input_tokens: int, output_tokens: int, cached_tokens: int = 0
    ) -> float:
        """Cost in USD.

        ``output_tokens`` must already include thinking tokens. ``cached_tokens`` is
        the portion of ``input_tokens`` served from cache, and is discounted rather
        than double counted.
        """
        cached = max(0, min(cached_tokens, input_tokens))
        fresh = input_tokens - cached
        return (
            fresh * self.input_per_1m
            + cached * self.input_per_1m * self.cached_input_multiplier
            + output_tokens * self.output_per_1m
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


# Google Search grounding bills per grounded prompt, on a separate SKU from tokens.
# Published rates have appeared as both $14 and $25 per 1,000; the higher figure is
# used so estimates are conservative rather than flattering. There is also a free
# monthly allowance. VERIFY AGAINST A REAL INVOICE before relying on this: unlike the
# token rates, this one is not measured here.
GROUNDING_USD_PER_1K_PROMPTS = 25.0
GROUNDING_FREE_PROMPTS_PER_MONTH = 5_000


def grounding_cost_usd(grounded_prompts: int) -> float:
    """Cost of the grounding SKU alone, excluding tokens."""
    return grounded_prompts * GROUNDING_USD_PER_1K_PROMPTS / 1000.0


def cost_usd(
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    grounded: bool = False,
) -> float:
    """Total cost of one request.

    A grounded request pays for its tokens *and* for the grounding query. Charging
    only tokens understates a grounded workload by a wide margin: at these rates a
    single grounded prompt costs about $0.025, roughly 87x the token cost of an
    ungrounded one.
    """
    total = pricing_for(model).cost(input_tokens, output_tokens, cached_tokens)
    if grounded:
        total += GROUNDING_USD_PER_1K_PROMPTS / 1000.0
    return total
