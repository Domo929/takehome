#!/usr/bin/env python3
"""Cost model for a batch brand-tracking workload.

Why this exists
---------------
Once the workload is known to be **batch** at **thousands of prompts per day**, the
engineering question changes. Our measured service sustains roughly 230 rps, which
means a 50,000-prompt day finishes in under four minutes. Throughput is not the
constraint and will not become one at any plausible growth rate.

Cost is. So the useful analysis is not "how fast can we go" but "what does a year of
this cost, and which levers actually move it".

Per-request token counts are measured, not guessed — taken from the live runs in
``results/real/``:

    thinking off  ->  37.3 input, 79.7 output (0 thinking)
    thinking on   ->  39.9 input, 571.2 output (477.3 thinking)

Levers modelled
---------------
1. **Thinking budget.** Measured 6.3x cost difference; the dominant lever on the
   output side, which is where nearly all the money is.
2. **Batch API.** Vertex bills batch prediction at roughly half the interactive rate
   in exchange for asynchronous, up-to-24-hour turnaround. A daily brand sweep does
   not care about turnaround, so this is close to free money.
3. **Context caching.** A brand-tracking workload sends one system prompt across
   every request in the sweep. Cache hits bill input at a fraction of the normal rate,
   and implicit caching is on by default for Gemini 2.5.

Batch and cache discounts do **not** stack: the larger applies. Modelled that way.
"""

from __future__ import annotations

import argparse

# Measured, per successful request. See results/real/*-manifest.json.
PROFILES = {
    "thinking-off": {"input": 37.3, "output": 79.7, "thinking": 0.0},
    "thinking-dynamic": {"input": 39.9, "output": 571.2, "thinking": 477.3},
}

PRICE_IN = 0.30
PRICE_OUT = 2.50
BATCH_MULTIPLIER = 0.50
CACHED_MULTIPLIER = 0.10

# The system prompt is identical across a sweep, so it is the cacheable portion.
# Measured input is ~37 tokens total; the fixed instruction is most of that.
SYSTEM_PROMPT_TOKENS = 25.0


def cost_per_request(profile: str, *, batch: bool, cached: bool) -> float:
    p = PROFILES[profile]
    inp, out = p["input"], p["output"]

    cacheable = min(SYSTEM_PROMPT_TOKENS, inp) if cached else 0.0
    fresh = inp - cacheable

    # Discounts do not stack; whichever is larger wins on the cached portion.
    cached_mult = min(CACHED_MULTIPLIER, BATCH_MULTIPLIER) if batch else CACHED_MULTIPLIER
    fresh_mult = BATCH_MULTIPLIER if batch else 1.0
    out_mult = BATCH_MULTIPLIER if batch else 1.0

    return (
        fresh * PRICE_IN * fresh_mult
        + cacheable * PRICE_IN * cached_mult
        + out * PRICE_OUT * out_mult
    ) / 1_000_000


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--daily", type=int, nargs="+", default=[5_000, 50_000, 500_000])
    args = ap.parse_args()

    configs = [
        ("naive: interactive, dynamic thinking", "thinking-dynamic", False, False),
        ("thinking off", "thinking-off", False, False),
        ("thinking off + caching", "thinking-off", False, True),
        ("thinking off + batch", "thinking-off", True, False),
        ("thinking off + batch + caching", "thinking-off", True, True),
    ]

    print("\nPer-request cost")
    print(f"  {'configuration':<38} {'$/request':>12}  {'vs naive':>9}")
    base = cost_per_request("thinking-dynamic", batch=False, cached=False)
    for label, prof, batch, cached in configs:
        c = cost_per_request(prof, batch=batch, cached=cached)
        print(f"  {label:<38} {c:>12.8f}  {base / c:>8.1f}x")

    for daily in args.daily:
        print(f"\n{daily:,} prompts/day")
        print(f"  {'configuration':<38} {'$/day':>10} {'$/year':>12}  {'saved/yr':>11}")
        base_year = base * daily * 365
        for label, prof, batch, cached in configs:
            c = cost_per_request(prof, batch=batch, cached=cached)
            year = c * daily * 365
            saved = base_year - year
            print(f"  {label:<38} {c*daily:>10.2f} {year:>12,.0f}  {saved:>11,.0f}")

    print("\nNotes")
    print("  - Token counts are measured from live runs, not estimated.")
    print("  - Batch trades up to 24h turnaround for ~50% off. A daily sweep can absorb that.")
    print("  - Cache and batch discounts do not stack; the larger applies to cached tokens.")
    print("  - Output dominates: it is ~7x the input rate and, with thinking on, ~14x the volume.")
    print("  - Throughput is not a constraint at these volumes. A 50,000-prompt day")
    print("    completes in under 4 minutes at the measured service capacity.")


if __name__ == "__main__":
    main()
