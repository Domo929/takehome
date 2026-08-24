#!/usr/bin/env python3
"""One production unit: the same prompt, 100 times, in both measurement conditions.

Why this shape
--------------
Every load test in this repo so far fired *different* prompts, because that is what a
throughput harness wants. Evertune's actual unit of work is the opposite: **one prompt
sampled 100 times**, run once with live search off and once with it on. 100 is a
settled methodological choice, not a parameter to tune.

That difference matters more than it looks. Sampling the same prompt 100 times against
a grounded model asks a question nobody has answered here: **do the 100 samples see the
same web?** Ungrounded, the only variation is the model's own sampling. Grounded, each
call can issue its own searches and retrieve its own sources, so the spread of answers
mixes generation variance with retrieval variance. If retrieval is unstable within a
single burst, then a brand's measured share moves for reasons that have nothing to do
with the model, and no amount of sampling averages that out — it is not noise around a
fixed truth, it is a moving truth.

What this measures
------------------
1. **Retrieval stability.** How many distinct source sets across 100 grounded samples,
   and how concentrated the citations are. The core question above.
2. **Search dedup.** Whether 100 identical prompts issue 100 searches, which decides
   whether a production unit really costs 100x the grounding SKU.
3. **Quota.** 100 grounded prompts in a burst is a pattern we have never run. A search
   quota separate from the generate-content quota would surface here.
4. **Truncation at a realistic cap.** 512 truncated half of all grounded answers, so
   this runs at 1,536 to find out where it actually settles.
5. **Silent degradation.** Whether any sample comes back ungrounded despite asking.

Cost is dominated by the grounding SKU: ~$0.025 per grounded sample, so ~$2.53 for the
grounded arm and roughly three cents for the control.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import time
from collections import Counter

from llm.gemini import Gemini
from llm.llm import FinishReason
from llm.metrics import REGISTRY
from llm.pricing import GROUNDING_USD_PER_1K_PROMPTS

REPO = pathlib.Path(__file__).resolve().parent.parent

SYSTEM = (
    "You are a market research assistant. Answer concisely and name specific brands "
    "and products. Do not add disclaimers."
)
# A single brand-visibility prompt, which is the shape of the real workload.
QUESTION = "Which robot vacuum brands are worth considering?"


def counter(name: str, **labels) -> float:
    total = 0.0
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if sample.name == name and all(
                sample.labels.get(k) == v for k, v in labels.items()
            ):
                total += sample.value
    return total


async def run_arm(name: str, provider: Gemini, n: int, conc: int) -> list[dict]:
    sem = asyncio.Semaphore(conc)
    rows: list[dict] = []
    done = 0

    async def one(i: int) -> None:
        nonlocal done
        async with sem:
            t0 = time.perf_counter()
            try:
                r = await provider.ask_generic_question(SYSTEM, QUESTION, 1.0)
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                rows.append({"i": i, "arm": name, "error": repr(exc)})
                return
            finally:
                done += 1
                if done % 20 == 0:
                    print(f"    {name}: {done}/{n}", flush=True)
            rows.append(
                {
                    "i": i,
                    "arm": name,
                    "grounded": r.grounded,
                    "grounding_requested": r.grounding_requested,
                    "degraded": r.grounding_degraded,
                    "input_tokens": r.input_tokens,
                    "output_tokens": r.output_tokens,
                    "cost_usd": r.cost_usd,
                    "latency_ms": (time.perf_counter() - t0) * 1000.0,
                    "attempts": r.attempts,
                    "finish_reason": str(r.finish_reason),
                    "truncated": r.finish_reason is FinishReason.MAX_TOKENS,
                    "search_queries": sorted(r.search_queries),
                    "sources": sorted(r.grounding_sources),
                    "answer": r.answer,
                }
            )

    await asyncio.gather(*(one(i) for i in range(n)))
    return rows


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p))]


BRAND_HINTS = {
    "roborock", "irobot", "roomba", "shark", "eufy", "dreame", "ecovacs", "deebot",
    "dyson", "samsung", "lg", "narwal", "wyze", "neato", "bissell", "tineco",
    "anker", "yeedi", "switchbot", "miele", "xiaomi",
}


def brands(text: str) -> set[str]:
    """Match against a fixed vocabulary rather than guessing at capitalisation.

    An earlier heuristic that treated capitalised tokens as brands scored "Pro" and
    "Options" as brands. A closed vocabulary cannot discover an unexpected brand, but
    it also cannot invent one, and for measuring *stability across samples* precision
    matters more than recall.
    """
    low = (text or "").lower()
    return {b for b in BRAND_HINTS if re.search(rf"\b{re.escape(b)}\b", low)}


def summarise(rows: list[dict]) -> dict:
    ok = [r for r in rows if "error" not in r]
    if not ok:
        return {"n": 0, "errors": len(rows)}
    lat = [r["latency_ms"] for r in ok]
    return {
        "n": len(ok),
        "errors": len(rows) - len(ok),
        "p50_latency_ms": round(pct(lat, 0.50), 1),
        "p95_latency_ms": round(pct(lat, 0.95), 1),
        "p99_latency_ms": round(pct(lat, 0.99), 1),
        "mean_output_tokens": round(sum(r["output_tokens"] for r in ok) / len(ok), 1),
        "truncated": sum(1 for r in ok if r["truncated"]),
        "degraded": sum(1 for r in ok if r["degraded"]),
        "retried": sum(1 for r in ok if r["attempts"] > 1),
        "cost_usd": round(sum(r["cost_usd"] or 0.0 for r in ok), 6),
    }


def stability(rows: list[dict]) -> dict:
    """How much do 100 samples of one prompt agree with each other?"""
    ok = [r for r in rows if "error" not in r]
    source_sets = [frozenset(r["sources"]) for r in ok if r["sources"]]
    query_sets = [frozenset(r["search_queries"]) for r in ok if r["search_queries"]]
    all_sources: Counter = Counter()
    for r in ok:
        all_sources.update(r["sources"])
    brand_sets = [brands(r["answer"]) for r in ok]
    brand_freq: Counter = Counter()
    for b in brand_sets:
        brand_freq.update(b)

    # Mean pairwise Jaccard on a sample of pairs: full pairwise is 4,950 comparisons,
    # which is fine, but the sample keeps this readable if n grows.
    def mean_jaccard(sets: list[frozenset]) -> float | None:
        if len(sets) < 2:
            return None
        tot = cnt = 0.0
        for i in range(len(sets)):
            for j in range(i + 1, len(sets)):
                union = sets[i] | sets[j]
                tot += len(sets[i] & sets[j]) / len(union) if union else 1.0
                cnt += 1
        return round(tot / cnt, 3)

    return {
        "samples_with_sources": len(source_sets),
        "distinct_source_sets": len(set(source_sets)),
        "distinct_sources_total": len(all_sources),
        "mean_pairwise_source_jaccard": mean_jaccard(source_sets),
        "top_sources": all_sources.most_common(5),
        "distinct_query_sets": len(set(query_sets)),
        "total_searches_issued": sum(len(r["search_queries"]) for r in ok),
        "mean_pairwise_query_jaccard": mean_jaccard(query_sets),
        "brand_frequency": brand_freq.most_common(12),
        "distinct_brand_sets": len({frozenset(b) for b in brand_sets}),
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=int, default=100)
    ap.add_argument("--concurrency", type=int, default=25)
    ap.add_argument("--project", default="evertune-tests")
    ap.add_argument("--location", default="us-central1")
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--max-output-tokens", type=int, default=1536)
    ap.add_argument("--budget-usd", type=float, default=4.00)
    ap.add_argument("--out", default="results/real/production-unit")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    sku = GROUNDING_USD_PER_1K_PROMPTS / 1000.0
    est = args.samples * (sku + 0.0008) + args.samples * 0.0008
    print(f"One production unit: '{QUESTION}'")
    print(f"  {args.samples} grounded + {args.samples} ungrounded, "
          f"concurrency {args.concurrency}, cap {args.max_output_tokens}")
    print(f"  Estimated ${est:.2f}, hard ceiling ${args.budget_usd:.2f}")
    if est > args.budget_usd:
        print("Estimate exceeds ceiling; refusing.")
        return
    if not args.yes:
        print("\nRefusing to run without --yes.")
        return

    def build(grounded_default: bool) -> Gemini:
        return Gemini(
            backend="vertex", project=args.project, location=args.location,
            model=args.model, thinking_budget=0,
            max_output_tokens=args.max_output_tokens,
            grounded=grounded_default, max_connections=args.concurrency * 2,
        )

    started = time.time()
    # One provider instance serves both conditions, which is the whole point of making
    # grounding a per-call argument: one pool, one retry budget, one ledger.
    provider = build(False)

    print("\n  ungrounded arm...", flush=True)
    plain = await run_arm("ungrounded", provider, args.samples, args.concurrency)

    rl_before = counter("llm_retry_attempts_total", reason="rate_limited")
    print("  grounded arm...", flush=True)
    grounded_provider = build(True)
    grounded = await run_arm("grounded", grounded_provider, args.samples, args.concurrency)
    rl_after = counter("llm_retry_attempts_total", reason="rate_limited")

    rows = plain + grounded
    s_plain, s_gr = summarise(plain), summarise(grounded)
    st = stability(grounded)
    st_plain = stability(plain)
    actual = sum(r["cost_usd"] or 0.0 for r in rows if "error" not in r)

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    raw = out.with_name(f"{out.name}-{stamp}.jsonl")
    raw.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    manifest = {
        "experiment": "production-unit",
        "question": QUESTION,
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "duration_s": round(time.time() - started, 1),
        "project": args.project,
        "location": args.location,
        "model": args.model,
        "max_output_tokens": args.max_output_tokens,
        "concurrency": args.concurrency,
        "requests": len(rows),
        "modelled_cost_usd": round(actual, 6),
        "grounded_prompts_billed": s_gr.get("n", 0),
        "grounding_rate_assumed_usd_per_1k": GROUNDING_USD_PER_1K_PROMPTS,
        "rate_limited_during_grounded_arm": rl_after - rl_before,
        "ungrounded": s_plain,
        "grounded": s_gr,
        "grounded_stability": st,
        "ungrounded_stability": st_plain,
        "raw": raw.name,
    }
    mf = out.with_name(f"{out.name}-{stamp}-manifest.json")
    mf.write_text(json.dumps(manifest, indent=2) + "\n")

    print(f"\n{'':<26}{'ungrounded':>13}{'grounded':>13}")
    for k in ("n", "p50_latency_ms", "p95_latency_ms", "p99_latency_ms",
              "mean_output_tokens", "truncated", "degraded", "retried", "cost_usd"):
        print(f"  {k:<24}{s_plain.get(k, 0):>13}{s_gr.get(k, 0):>13}")

    print("\nRetrieval stability across the grounded samples")
    print(f"  samples with sources        {st['samples_with_sources']}")
    print(f"  DISTINCT source sets        {st['distinct_source_sets']}")
    print(f"  distinct sources total      {st['distinct_sources_total']}")
    print(f"  mean pairwise Jaccard       {st['mean_pairwise_source_jaccard']}")
    print(f"  searches issued             {st['total_searches_issued']}")
    print(f"  distinct query sets         {st['distinct_query_sets']}")
    print(f"  mean pairwise query Jaccard {st['mean_pairwise_query_jaccard']}")
    print(f"  rate-limited (grounded arm) {manifest['rate_limited_during_grounded_arm']:.0f}")

    print("\nBrand frequency out of 100 samples")
    gb, pb = dict(st["brand_frequency"]), dict(st_plain["brand_frequency"])
    print(f"  {'brand':<14}{'ungrounded':>12}{'grounded':>10}")
    for b in sorted(set(gb) | set(pb), key=lambda x: -(gb.get(x, 0) + pb.get(x, 0))):
        print(f"  {b:<14}{pb.get(b, 0):>12}{gb.get(b, 0):>10}")

    print(f"\nModelled cost ${actual:.4f}  ->  {mf}")


if __name__ == "__main__":
    asyncio.run(main())
