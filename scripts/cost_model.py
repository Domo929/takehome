"""Project the yearly cost of a brand-tracking workload, and which levers move it.

Throughput is not the constraint: the service sustains ~230 rps, so a 50,000-prompt day
finishes in minutes. Cost is, which makes the useful question "what does a year cost
and what actually reduces it".

Token counts are measured rather than assumed, from the live runs in results/real/ on
Vertex us-central1. Rates are verified against Google's billing catalog - run
scripts/verify_pricing.py.

The headline result is that once grounding is on, none of the token levers matter:
Batch cannot run grounded requests at all and caching needs 2,048+ input tokens against
a workload of ~35. See FINDINGS 5 and 2.
"""

from __future__ import annotations

import argparse

# Measured per successful request on Vertex us-central1, from the n=100 runs in
# results/real/model/think-{off,dyn}-n100-*.
#
# These replaced an earlier n=15 pair (results/real/model/uscentral-tb*). That sample
# was too small to carry a cost model: bootstrapping its thinking ratio gives a 95%
# interval of [2.36, 7.42], and FINDINGS Appendix B retires it by name. It was still
# quietly the basis for every dollar figure here, which understated the ungrounded
# unit cost by about 30%.
PROFILES = {
    "thinking-off": {"input": 34.5, "output": 145.3, "thinking": 0.0},
    "thinking-dynamic": {"input": 34.5, "output": 533.6, "thinking": 411.2},
}

# Output length is a property of the prompt, not of the model, and it moves the unit
# cost more than any lever in this file. Measured means for ungrounded, thinking off:
#
#   111.1  results/real/model/uscentral-tb0      n=15,  distinct prompts, cap 1024
#   119.6  results/real/measurement/production-unit  n=100, ONE prompt,   cap 1536
#   145.3  results/real/model/think-off-n100     n=100, distinct prompts, cap 2048
#   160.7  results/real/measurement/grounding    n=20,  distinct prompts
#   166.0  results/real/measurement/tool-refusal n=50,  distinct prompts, cap 512
#
# That is a 1.5x spread, so treat any single figure below as the middle of a range
# rather than a quote. Re-measure against the real prompt corpus before committing to
# a budget.
OUTPUT_TOKEN_RANGE = (111.1, 166.0)

GROUNDING_USD_PER_1K = 35.0
PRICE_IN = 0.30
PRICE_OUT = 2.50
BATCH_MULTIPLIER = 0.50
CACHED_MULTIPLIER = 0.10

# Implicit caching on Gemini 2.5 Flash has a minimum input size. Below it, nothing is
# cacheable at any price. The measured workload is ~35 input tokens, so this model used
# to report a discount that cannot physically occur.
IMPLICIT_CACHE_MIN_TOKENS = 2048.0
SYSTEM_PROMPT_TOKENS = 25.0


def cost_per_request(profile: str, *, batch: bool, cached: bool) -> float:
    p = PROFILES[profile]
    inp, out = p["input"], p["output"]

    # Caching is all-or-nothing against the minimum: a 35-token prompt is not
    # partially cacheable, it is ineligible.
    eligible = inp >= IMPLICIT_CACHE_MIN_TOKENS
    cacheable = min(SYSTEM_PROMPT_TOKENS, inp) if (cached and eligible) else 0.0
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

        ("thinking off + batch", "thinking-off", True, False),

    ]

    # Grounding is a separate SKU and a separate measurement condition, so it is
    # reported on its own rather than folded into the token levers above.
    print("\nGrounded condition (live search), per request")
    tok = cost_per_request("thinking-off", batch=False, cached=False)
    sku = GROUNDING_USD_PER_1K / 1000.0
    print(f"  {'tokens only (ungrounded)':<38} {tok:>12.8f}")
    print(f"  {'grounding SKU per prompt':<38} {sku:>12.8f}")
    print(f"  {'grounded total':<38} {tok + sku:>12.8f}   {(tok + sku) / tok:>5.0f}x")
    print()
    print("  At 100 samples per prompt, a single grounded prompt costs "
          f"${(tok + sku) * 100:.2f} against ${tok * 100:.2f} ungrounded.")
    print("  Batch cannot run grounded requests at all (no tool support), and caching")
    print("  needs 2,048+ input tokens. Every lever in this model touches the ~1% of a")
    print("  two-condition workload that is not the grounding SKU.")

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
    lo, hi = OUTPUT_TOKEN_RANGE
    p = PROFILES["thinking-off"]
    c_lo = (p["input"] * PRICE_IN + lo * PRICE_OUT) / 1_000_000
    c_hi = (p["input"] * PRICE_IN + hi * PRICE_OUT) / 1_000_000
    print(f"  - Ungrounded output ran {lo:.0f} to {hi:.0f} tokens across five corpora,")
    print(f"    so the unit cost spans ${c_lo:.7f} to ${c_hi:.7f} ({c_hi/c_lo:.2f}x)")
    print(f"    on prompt wording alone. Figures above use {p['output']:.1f}, the n=100 run.")
    print("  - Batch trades up to 24h turnaround for ~50% off. A daily sweep can absorb that.")
    print("  - Context caching is omitted: it needs >= 2,048 input tokens and the")
    print(f"    measured workload is ~{PROFILES['thinking-off']['input']:.0f}. It cannot engage.")
    print("  - Batch does NOT apply to grounded requests: batch prediction has no tool")
    print("    support, so the grounded condition must run online at full rate.")
    print("  - Output dominates: it is ~7x the input rate and, with thinking on, ~14x the volume.")
    print("  - Throughput is not a constraint at these volumes. A 50,000-prompt day")
    print("    completes in under 4 minutes at the measured service capacity.")


if __name__ == "__main__":
    main()
