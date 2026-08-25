#!/usr/bin/env python3
"""Does temperature 0.7 hold up, or was it just the number everyone uses?

Why this matters more here than usual
-------------------------------------
Evertune samples each prompt 100 times and reads the distribution. Temperature is the
knob that decides how much those 100 samples differ from each other, so it sets the
noise floor of the entire measurement:

* Too low and the samples collapse toward one answer. The distribution is narrow, and
  a brand the model would name 2% of the time never appears - the same blind spot
  FINDINGS 6e found in counting, made worse by construction.
* Too high and the spread widens with noise rather than signal. Brand share moves
  between runs for reasons that have nothing to do with the brands.

0.7 came from convention. Nothing in the repo justified it.

Design
------
One prompt, N samples at each of several temperatures, ungrounded (grounding adds
retrieval variance on top, which would confound this). Each temperature is run as two
independent batches so cross-batch stability can be measured directly rather than
inferred: the question is not only "how many brands appear" but "does the same
temperature give the same answer twice".

Measured per temperature:

1. **Coverage** - distinct brands found across all samples. Rises with temperature,
   then stops rising once the extra spread is noise rather than new brands.
2. **Cross-batch stability** - mean absolute difference in per-brand mention rate
   between the two halves. This is the noise floor: the amount a brand's measured
   share moves when nothing about the world changed.
3. **Entropy** - Shannon entropy over the brand-set distribution, in bits. A summary
   of how spread out the answers are.
4. **Truncation and cost**, which should be flat but are worth confirming.

Decision rule, fixed before the run
-----------------------------------
Pick the lowest temperature at which coverage has stopped growing. Past that point
extra temperature buys variance, not visibility. If cross-batch instability is already
climbing at that temperature, prefer the lower one: a measurement that cannot
reproduce itself is worse than one that misses a rare brand.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import pathlib
import re
import time
from collections import Counter

from llm.gemini import Gemini
from llm.llm import FinishReason

REPO = pathlib.Path(__file__).resolve().parent.parent

SYSTEM = (
    "You are a market research assistant. Answer concisely and name specific brands "
    "and products. Do not add disclaimers."
)
QUESTION = "Which robot vacuum brands are worth considering?"

# Closed vocabulary rather than capitalisation heuristics. An earlier extractor that
# guessed from capitalisation scored "Pro" and "Options" as brands (FINDINGS 0c). A
# fixed list cannot discover an unexpected brand, but for measuring how much answers
# vary, precision matters more than recall.
BRANDS = {
    "roborock", "irobot", "roomba", "shark", "eufy", "dreame", "ecovacs", "deebot",
    "dyson", "samsung", "lg", "narwal", "wyze", "neato", "bissell", "tineco",
    "anker", "yeedi", "switchbot", "miele", "xiaomi", "levoit", "proscenic",
    "ilife", "roidmi", "viomi", "360", "kyvol", "shark ai", "eureka",
}


def brands_in(text: str) -> frozenset[str]:
    low = (text or "").lower()
    return frozenset(b for b in BRANDS if re.search(rf"\b{re.escape(b)}\b", low))


def entropy_bits(counts: Counter) -> float:
    total = sum(counts.values())
    if total <= 0:
        return 0.0
    return -sum(
        (n / total) * math.log2(n / total) for n in counts.values() if n
    )


async def sample_at(
    provider: Gemini, temperature: float, n: int, conc: int
) -> list[dict]:
    sem = asyncio.Semaphore(conc)
    rows: list[dict] = []

    async def one(i: int) -> None:
        async with sem:
            try:
                r = await provider.ask_generic_question(SYSTEM, QUESTION, temperature)
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                rows.append({"i": i, "temperature": temperature, "error": repr(exc)})
                return
            rows.append(
                {
                    "i": i,
                    # Which half of the run this sample belongs to. Assigned by index
                    # rather than by completion order so the split is deterministic
                    # and independent of scheduling.
                    "batch": 0 if i < n // 2 else 1,
                    "temperature": temperature,
                    "output_tokens": r.output_tokens,
                    "cost_usd": r.cost_usd,
                    "truncated": r.finish_reason is FinishReason.MAX_TOKENS,
                    "brands": sorted(brands_in(r.answer)),
                    "answer": r.answer,
                }
            )

    await asyncio.gather(*(one(i) for i in range(n)))
    return rows


def analyse(rows: list[dict], temperature: float) -> dict:
    ok = [r for r in rows if "error" not in r]
    if not ok:
        return {"temperature": temperature, "n": 0, "errors": len(rows)}

    all_brands: Counter = Counter()
    for r in ok:
        all_brands.update(r["brands"])

    def rates(batch: int) -> dict[str, float]:
        sub = [r for r in ok if r["batch"] == batch]
        if not sub:
            return {}
        c: Counter = Counter()
        for r in sub:
            c.update(r["brands"])
        return {b: c[b] / len(sub) for b in all_brands}

    a, b = rates(0), rates(1)
    # Mean absolute difference in per-brand mention rate between two independent
    # batches at the SAME temperature. Nothing about the world changed between them,
    # so whatever this is, it is noise.
    drift = (
        sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in all_brands) / len(all_brands)
        if all_brands
        else 0.0
    )

    return {
        "temperature": temperature,
        "n": len(ok),
        "errors": len(rows) - len(ok),
        "distinct_brands": len(all_brands),
        "distinct_answer_brand_sets": len({frozenset(r["brands"]) for r in ok}),
        "mean_brands_per_answer": sum(len(r["brands"]) for r in ok) / len(ok),
        "entropy_bits": round(entropy_bits(all_brands), 3),
        "cross_batch_drift": round(drift, 4),
        "mean_output_tokens": round(sum(r["output_tokens"] for r in ok) / len(ok), 1),
        "truncated": sum(1 for r in ok if r["truncated"]),
        "cost_usd": round(sum(r["cost_usd"] or 0.0 for r in ok), 6),
        "brand_counts": all_brands.most_common(),
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--temperatures", type=float, nargs="+",
        default=[0.0, 0.35, 0.7, 1.0, 1.4],
    )
    ap.add_argument("--samples", type=int, default=50)
    ap.add_argument("--concurrency", type=int, default=25)
    ap.add_argument("--project", default="evertune-tests")
    ap.add_argument("--location", default="us-central1")
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--max-output-tokens", type=int, default=1024)
    ap.add_argument("--budget-usd", type=float, default=0.50)
    ap.add_argument("--out", default="results/real/temperature")
    ap.add_argument("--base-url", default=None, help="Point at the mock to validate.")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    total = len(args.temperatures) * args.samples
    est = total * 0.00035
    print(f"Temperature sweep: {args.temperatures}")
    print(f"  {args.samples} samples each, {total} requests total")
    print(f"  Estimated ${est:.2f}, ceiling ${args.budget_usd:.2f}")
    if est > args.budget_usd:
        print("Estimate exceeds ceiling; refusing.")
        return
    if not args.yes:
        print("\nRefusing to run without --yes.")
        return

    kwargs = dict(
        backend="vertex", project=args.project, location=args.location,
        model=args.model, thinking_budget=0,
        max_output_tokens=args.max_output_tokens,
        max_connections=args.concurrency * 2,
    )
    if args.base_url:
        kwargs["base_url"] = args.base_url
    provider = Gemini(**kwargs)

    started = time.time()
    rows: list[dict] = []
    summaries: list[dict] = []
    for t in args.temperatures:
        print(f"  temperature={t} ...", flush=True)
        batch = await sample_at(provider, t, args.samples, args.concurrency)
        rows.extend(batch)
        summaries.append(analyse(batch, t))

    actual = sum(r["cost_usd"] or 0.0 for r in rows if "error" not in r)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    raw = out.with_name(f"{out.name}-{stamp}.jsonl")
    raw.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    manifest = {
        "experiment": "temperature",
        "question": QUESTION,
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "duration_s": round(time.time() - started, 1),
        "project": args.project,
        "location": args.location,
        "model": args.model,
        "samples_per_temperature": args.samples,
        "requests": len(rows),
        "modelled_cost_usd": round(actual, 6),
        "by_temperature": summaries,
    }
    mf = out.with_name(f"{out.name}-{stamp}-manifest.json")
    mf.write_text(json.dumps(manifest, indent=2) + "\n")

    print(f"\n{'temp':>6}{'brands':>8}{'sets':>7}{'per ans':>9}{'entropy':>9}"
          f"{'drift':>8}{'out tok':>9}{'trunc':>7}")
    for s in summaries:
        if not s.get("n"):
            continue
        print(f"{s['temperature']:>6.2f}{s['distinct_brands']:>8}"
              f"{s['distinct_answer_brand_sets']:>7}{s['mean_brands_per_answer']:>9.1f}"
              f"{s['entropy_bits']:>9.3f}{s['cross_batch_drift']:>8.3f}"
              f"{s['mean_output_tokens']:>9.0f}{s['truncated']:>7}")

    print("\nBrands by temperature (mentions out of n)")
    names = sorted(
        {b for s in summaries for b, _ in s.get("brand_counts", [])},
        key=lambda b: -sum(dict(s.get("brand_counts", [])).get(b, 0) for s in summaries),
    )
    header = "".join(f"{s['temperature']:>7.2f}" for s in summaries if s.get("n"))
    print(f"  {'brand':<12}{header}")
    for b in names[:14]:
        cells = "".join(
            f"{dict(s.get('brand_counts', [])).get(b, 0):>7}"
            for s in summaries if s.get("n")
        )
        print(f"  {b:<12}{cells}")

    print(f"\nModelled cost ${actual:.4f}  ->  {mf}")


if __name__ == "__main__":
    asyncio.run(main())
