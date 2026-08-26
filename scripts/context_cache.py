"""Does implicit context caching actually engage above the 2,048-token floor?

FINDINGS argues from arithmetic that padding a prompt to reach the cache floor can
never pay: the cached rate is 10x cheaper per token but padding multiplies the token
count by 58, so ten times cheaper on fifty-eight times as many tokens is more money.
That conclusion holds whether or not caching works, which is why it was safe to write
without measuring.

What it does not establish is the other half: that a prompt which grows past 2,048
tokens on its own gets the discount. That claim rested entirely on Google's
documentation, and this document has already been wrong once by trusting a rate I read
instead of a rate I queried.

So this sends the same long prefix repeatedly and watches `cached_content_token_count`.
Two arms:

  above  ~2,200 input tokens, over the documented floor. Caching should engage.
  below  ~600 input tokens, under it. Caching should never engage, at any hit rate.

The below arm is the control. Without it, "we saw caching" and "we saw caching because
the prompt was long enough" are indistinguishable.

Costs about three cents. Nothing here needs a big sample: implicit caching either
reports cached tokens or it does not.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import time

from google.genai import types

from llm.gemini import Gemini
from llm.pricing import PRICING

REPO = pathlib.Path(__file__).resolve().parent.parent

QUESTION = "Which robot vacuum brands are worth considering?"

# A stable prefix is the whole point: implicit caching keys on a shared prefix, so the
# padding has to be byte-identical across requests. Real filler rather than one token
# repeated, because tokenisers collapse degenerate input and that would make the token
# count unpredictable.
_PARAGRAPH = (
    "You are assisting with a market research study of consumer appliance brands. "
    "The study tracks how often each brand is mentioned in answers to buying "
    "questions, how those mentions are phrased, and whether the sentiment attached "
    "to them is positive, neutral or negative. Responses are sampled repeatedly so "
    "that a mention rate can be estimated rather than a single answer recorded. "
)


# Measured, not assumed. The first run of this script targeted 2,200 tokens using the
# usual 4-characters-per-token rule of thumb and landed at 1,641, which is under the
# 2,048 floor. It reported zero cache hits, and the honest reading of that result was
# "the prompt was too short" rather than "caching does not work". Prose this dense runs
# closer to 5 characters per token.
CHARS_PER_TOKEN = 5.4


def build_prefix(target_tokens: int) -> str:
    repeats = max(1, int(target_tokens * CHARS_PER_TOKEN / len(_PARAGRAPH)) + 1)
    return _PARAGRAPH * repeats


async def run_arm(provider: Gemini, label: str, prefix: str, n: int) -> list[dict]:
    rows: list[dict] = []
    config = types.GenerateContentConfig(
        system_instruction=prefix,
        temperature=1.0,
        max_output_tokens=64,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )
    for i in range(n):
        started = time.perf_counter()
        raw = await provider._client.aio.models.generate_content(
            model=provider.model, contents=QUESTION, config=config
        )
        usage = raw.usage_metadata
        cached = int(getattr(usage, "cached_content_token_count", 0) or 0)
        rows.append(
            {
                "arm": label,
                "i": i,
                "input_tokens": int(getattr(usage, "prompt_token_count", 0) or 0),
                "cached_tokens": cached,
                "output_tokens": int(getattr(usage, "candidates_token_count", 0) or 0),
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        )
        # Sequential and paced. Implicit caching needs the prefix to already be warm,
        # and firing concurrently means the first N requests all miss.
        await asyncio.sleep(0.3)
    return rows


def summarise(rows: list[dict]) -> dict:
    n = len(rows)
    hits = [r for r in rows if r["cached_tokens"] > 0]
    inp = sum(r["input_tokens"] for r in rows) / n
    p = PRICING["gemini-2.5-flash"]
    # What the discount was actually worth, at the rates in llm/pricing.py.
    full = sum(r["input_tokens"] for r in rows) * p.input_per_1m / 1e6
    billed = sum(
        (r["input_tokens"] - r["cached_tokens"]) * p.input_per_1m
        + r["cached_tokens"] * p.input_per_1m * p.cached_input_multiplier
        for r in rows
    ) / 1e6
    return {
        "arm": rows[0]["arm"],
        "n": n,
        "mean_input_tokens": round(inp, 1),
        "requests_with_cache_hit": len(hits),
        "mean_cached_tokens_when_hit": (
            round(sum(r["cached_tokens"] for r in hits) / len(hits), 1) if hits else 0
        ),
        "cached_share_of_input": round(
            sum(r["cached_tokens"] for r in rows) / sum(r["input_tokens"] for r in rows), 4
        ),
        "input_cost_full_rate_usd": round(full, 6),
        "input_cost_billed_usd": round(billed, 6),
        "input_saving_pct": round((1 - billed / full) * 100, 1) if full else 0.0,
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=20, help="requests per arm")
    ap.add_argument("--above-tokens", type=int, default=2200)
    ap.add_argument("--below-tokens", type=int, default=600)
    ap.add_argument("--project", default="evertune-tests")
    ap.add_argument("--location", default="us-central1")
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--out", default="results/real/model/context-cache")
    ap.add_argument("--yes", action="store_true", help="required: spends real money")
    args = ap.parse_args()

    p = PRICING["gemini-2.5-flash"]
    est = (
        args.n * (args.above_tokens + args.below_tokens) * p.input_per_1m
        + 2 * args.n * 64 * p.output_per_1m
    ) / 1e6
    print(f"{2 * args.n} requests: {args.n} above the 2,048-token floor, {args.n} below.")
    print(f"estimated ${est:.4f}")
    if not args.yes:
        print("\nrefusing to run without --yes")
        return

    provider = Gemini(
        backend="vertex", project=args.project, location=args.location,
        model=args.model, thinking_budget=0, max_output_tokens=64,
    )
    started = time.time()
    above = await run_arm(provider, "above_floor", build_prefix(args.above_tokens), args.n)
    below = await run_arm(provider, "below_floor", build_prefix(args.below_tokens), args.n)

    s_above, s_below = summarise(above), summarise(below)
    # A run whose "above" arm never cleared the floor tests nothing, and its zero-hit
    # result reads exactly like a real negative. Say so rather than reporting it.
    FLOOR = 2048
    calibrated = s_above["mean_input_tokens"] >= FLOOR
    if not calibrated:
        print(
            f"\n!! the above-floor arm averaged {s_above['mean_input_tokens']:.0f} "
            f"input tokens, under the {FLOOR} floor. This run cannot say anything "
            f"about caching. Raise --above-tokens and re-run."
        )
    manifest = {
        "experiment": "context-cache",
        "question": "does implicit caching engage above 2,048 input tokens?",
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "duration_s": round(time.time() - started, 1),
        "project": args.project,
        "location": args.location,
        "model": args.model,
        "requests": len(above) + len(below),
        "floor_tokens": 2048,
        "above_arm_cleared_floor": None,  # filled below
        "arms": [s_above, s_below],
        "modelled_cost_usd": round(
            sum(
                (r["input_tokens"] * p.input_per_1m + r["output_tokens"] * p.output_per_1m)
                / 1e6
                for r in above + below
            ),
            6,
        ),
    }

    manifest["above_arm_cleared_floor"] = calibrated

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = REPO / f"{args.out}-{stamp}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_name(out.name + "-manifest.json").write_text(json.dumps(manifest, indent=2))
    out.with_name(out.name + "-records.json").write_text(
        json.dumps({"above_floor": above, "below_floor": below}, indent=2)
    )

    print(f"\n{'arm':<14}{'n':>4}{'mean in':>10}{'hits':>7}{'cached/req':>12}{'saving':>9}")
    for s in (s_above, s_below):
        print(
            f"{s['arm']:<14}{s['n']:>4}{s['mean_input_tokens']:>10.0f}"
            f"{s['requests_with_cache_hit']:>7}{s['mean_cached_tokens_when_hit']:>12.0f}"
            f"{s['input_saving_pct']:>8.1f}%"
        )
    print(f"\ncost ${manifest['modelled_cost_usd']:.4f}")
    print(f"wrote {out.with_name(out.name + '-manifest.json').relative_to(REPO)}")


if __name__ == "__main__":
    asyncio.run(main())
