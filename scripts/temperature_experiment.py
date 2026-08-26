"""Sweep temperature across categories and measure what it does to brand share.

Temperature controls how much 100 samples of one prompt differ from each other, so it
sets the noise floor of the whole measurement. It was inherited as 0.7 and never
justified. Results and the recommendation are in FINDINGS 2.

Each temperature runs as two independent halves so cross-batch drift is measured
directly rather than inferred - that drift is the noise floor.

Vocabularies are closed and hand-written per category. That cannot discover an
unexpected brand, which is the accepted cost: a capitalisation heuristic scored "Pro"
and "Options" as brands earlier (FINDINGS 2), and for measuring how rates move,
precision beats recall.
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
QUESTION_TEMPLATE = "Which {category} brands are worth considering?"

# Categories chosen to span brand structures, because the n=50 pilot found the driver
# was parent/sub-brand attribution ("Eufy (Anker)"). Whether that generalises is the
# whole point of running more than one category.
#
# Vocabularies are closed and hand-written. That cannot discover an unexpected brand,
# which is the accepted cost: a capitalisation heuristic scored "Pro" and "Options" as
# brands earlier (FINDINGS 2), and for measuring how rates MOVE, precision beats
# recall.
CATEGORY_BRANDS: dict[str, set[str]] = {
    "robot vacuum": {
        "roborock", "irobot", "roomba", "shark", "eufy", "dreame", "ecovacs", "deebot",
        "dyson", "samsung", "narwal", "wyze", "neato", "bissell", "tineco", "anker",
        "yeedi", "switchbot", "xiaomi", "levoit", "proscenic", "ilife", "viomi", "eureka",
    },
    "wireless earbud": {
        "sony", "bose", "apple", "airpods", "samsung", "galaxy buds", "jabra",
        "sennheiser", "anker", "soundcore", "beats", "google", "pixel buds", "nothing",
        "technics", "jbl", "skullcandy", "bang & olufsen", "1more", "earfun", "denon",
    },
    "air purifier": {
        "levoit", "vesync", "coway", "blueair", "dyson", "molekule", "winix",
        "honeywell", "austin air", "iqair", "rabbit air", "alen", "germguardian",
        "xiaomi", "philips", "sharp", "medify", "shark", "bissell", "toshiba",
    },
    "electric toothbrush": {
        "oral-b", "braun", "philips", "sonicare", "quip", "burst", "colgate",
        "waterpik", "aquasonic", "fairywill", "suri", "laifen", "usmile", "panasonic",
        "xiaomi", "shyn", "moon",
    },
    "espresso machine": {
        "breville", "sage", "de'longhi", "delonghi", "gaggia", "rancilio", "rocket",
        "lelit", "profitec", "ecm", "la marzocco", "jura", "nespresso", "smeg",
        "cuisinart", "flair", "bambino", "ascaso", "philips",
    },
    "mechanical keyboard": {
        "keychron", "ducky", "logitech", "razer", "corsair", "varmilo", "leopold",
        "hhkb", "topre", "nuphy", "glorious", "akko", "wooting", "steelseries", "drop",
        "gmmk", "epomaker", "das keyboard", "cherry",
    },
    "office chair": {
        "herman miller", "aeron", "steelcase", "leap", "gesture", "haworth",
        "humanscale", "secretlab", "branch", "autonomous", "x-chair", "sihoo", "hon",
        "ikea", "markus", "duramont", "flexispot", "knoll", "embody",
    },
    "running shoe": {
        "nike", "adidas", "brooks", "asics", "hoka", "saucony", "new balance", "on",
        "altra", "mizuno", "salomon", "topo", "puma", "reebok", "under armour",
    },
    "standing desk": {
        "uplift", "fully", "jarvis", "flexispot", "autonomous", "vari", "ikea",
        "bekant", "steelcase", "herman miller", "branch", "secretlab", "fezibo",
        "effydesk", "desky", "progressive automations",
    },
    "dash cam": {
        "viofo", "garmin", "nextbase", "blackvue", "thinkware", "vantrue", "rexing",
        "70mai", "cobra", "anker", "roav", "kingslim", "redtiger", "miofive",
    },
    "cast iron skillet": {
        "lodge", "field", "le creuset", "staub", "smithey", "finex", "stargazer",
        "victoria", "utopia", "cuisinart", "tramontina", "butter pat", "griswold",
        "calphalon", "made in",
    },
}

def brands_in(text: str, vocab: set[str]) -> frozenset[str]:
    low = (text or "").lower()
    return frozenset(b for b in vocab if re.search(rf"(?<![\w-]){re.escape(b)}(?![\w-])", low))


def aside_brands(text: str, vocab: set[str]) -> frozenset[str]:
    """Brands mentioned only inside parentheses.

    This is the mechanism the pilot found, made measurable without needing to know in
    advance which brands are parent companies. "Eufy (Anker)" attributes Anker in an
    aside; a standalone "Anker RoboVac" does not. If temperature sensitivity tracks
    aside-rate across categories, the phrasing explanation holds; if it does not, the
    pilot found something category-specific.
    """
    low = (text or "").lower()
    inside = " ".join(re.findall(r"\(([^)]*)\)", low))
    outside = re.sub(r"\([^)]*\)", " ", low)
    in_paren = {b for b in vocab if re.search(rf"(?<![\w-]){re.escape(b)}(?![\w-])", inside)}
    standalone = {b for b in vocab if re.search(rf"(?<![\w-]){re.escape(b)}(?![\w-])", outside)}
    return frozenset(in_paren - standalone)


def entropy_bits(counts: Counter) -> float:
    total = sum(counts.values())
    if total <= 0:
        return 0.0
    return -sum(
        (n / total) * math.log2(n / total) for n in counts.values() if n
    )


async def sample_at(
    provider: Gemini, category: str, temperature: float, n: int, conc: int
) -> list[dict]:
    vocab = CATEGORY_BRANDS[category]
    question = QUESTION_TEMPLATE.format(category=category)
    sem = asyncio.Semaphore(conc)
    rows: list[dict] = []

    async def one(i: int) -> None:
        async with sem:
            try:
                r = await provider.ask_generic_question(SYSTEM, question, temperature)
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                rows.append(
                    {"i": i, "category": category, "temperature": temperature,
                     "error": repr(exc)}
                )
                return
            rows.append(
                {
                    "i": i,
                    # Which half this sample belongs to, assigned by index rather than
                    # completion order so the split is deterministic.
                    "batch": 0 if i < n // 2 else 1,
                    "category": category,
                    "temperature": temperature,
                    "output_tokens": r.output_tokens,
                    "cost_usd": r.cost_usd,
                    "truncated": r.finish_reason is FinishReason.MAX_TOKENS,
                    "brands": sorted(brands_in(r.answer, vocab)),
                    "aside_brands": sorted(aside_brands(r.answer, vocab)),
                    "answer": r.answer,
                }
            )

    await asyncio.gather(*(one(i) for i in range(n)))
    return rows


def analyse(rows: list[dict], category: str, temperature: float) -> dict:
    ok = [r for r in rows if "error" not in r]
    if not ok:
        return {"category": category, "temperature": temperature, "n": 0,
                "errors": len(rows)}

    all_brands: Counter = Counter()
    asides: Counter = Counter()
    for r in ok:
        all_brands.update(r["brands"])
        asides.update(r["aside_brands"])

    def rates(batch: int) -> dict[str, float]:
        sub = [r for r in ok if r["batch"] == batch]
        if not sub:
            return {}
        c: Counter = Counter()
        for r in sub:
            c.update(r["brands"])
        return {b: c[b] / len(sub) for b in all_brands}

    a, b = rates(0), rates(1)
    # Mean absolute change in per-brand rate between two independent halves at the
    # SAME temperature. Nothing changed between them, so this is the noise floor.
    drift = (
        sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in all_brands) / len(all_brands)
        if all_brands else 0.0
    )

    return {
        "category": category,
        "temperature": temperature,
        "n": len(ok),
        "errors": len(rows) - len(ok),
        "distinct_brands": len(all_brands),
        "distinct_answer_brand_sets": len({frozenset(r["brands"]) for r in ok}),
        "mean_brands_per_answer": round(sum(len(r["brands"]) for r in ok) / len(ok), 2),
        "entropy_bits": round(entropy_bits(all_brands), 3),
        "cross_batch_drift": round(drift, 4),
        "mean_output_tokens": round(sum(r["output_tokens"] for r in ok) / len(ok), 1),
        "truncated": sum(1 for r in ok if r["truncated"]),
        "cost_usd": round(sum(r["cost_usd"] or 0.0 for r in ok), 6),
        "brand_counts": all_brands.most_common(),
        "aside_counts": asides.most_common(),
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--temperatures", type=float, nargs="+", default=[0.0, 0.35, 0.7, 1.0, 1.4]
    )
    ap.add_argument("--categories", nargs="+", default=sorted(CATEGORY_BRANDS))
    ap.add_argument("--samples", type=int, default=60)
    ap.add_argument("--concurrency", type=int, default=32)
    ap.add_argument("--project", default="evertune-tests")
    ap.add_argument("--location", default="us-central1")
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--max-output-tokens", type=int, default=1024)
    ap.add_argument("--budget-usd", type=float, default=1.20)
    ap.add_argument("--out", default="results/real/temperature-multi")
    ap.add_argument("--base-url", default=None, help="Point at the mock to validate.")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    unknown = [c for c in args.categories if c not in CATEGORY_BRANDS]
    if unknown:
        print(f"Unknown categories: {unknown}")
        return

    total = len(args.categories) * len(args.temperatures) * args.samples
    est = total * 0.00029
    print(f"Temperature x category sweep")
    print(f"  temperatures: {args.temperatures}")
    print(f"  categories:   {len(args.categories)}")
    print(f"  {args.samples} samples per cell, {total} requests total")
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
    spent = 0.0
    for ci, category in enumerate(args.categories, 1):
        for t in args.temperatures:
            if spent > args.budget_usd:
                print(f"  budget ceiling reached at ${spent:.2f}; stopping")
                break
            batch = await sample_at(
                provider, category, t, args.samples, args.concurrency
            )
            rows.extend(batch)
            summary = analyse(batch, category, t)
            summaries.append(summary)
            spent += summary.get("cost_usd", 0.0)
        print(f"  [{ci}/{len(args.categories)}] {category:<22} ${spent:.3f}", flush=True)

    actual = sum(r["cost_usd"] or 0.0 for r in rows if "error" not in r)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    raw = out.with_name(f"{out.name}-{stamp}.jsonl")
    raw.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    manifest = {
        "experiment": "temperature-multi",
        "prompt_template": QUESTION_TEMPLATE,
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "duration_s": round(time.time() - started, 1),
        "project": args.project,
        "location": args.location,
        "model": args.model,
        "samples_per_cell": args.samples,
        "temperatures": args.temperatures,
        "categories": args.categories,
        "requests": len(rows),
        "modelled_cost_usd": round(actual, 6),
        "cells": summaries,
    }
    mf = out.with_name(f"{out.name}-{stamp}-manifest.json")
    mf.write_text(json.dumps(manifest, indent=2) + "\n")

    temps = args.temperatures
    print(f"\nDistinct brands found, by category and temperature")
    print(f"  {'category':<22}" + "".join(f"{t:>7.2f}" for t in temps))
    for c in args.categories:
        cells = {s["temperature"]: s for s in summaries if s["category"] == c}
        line = "".join(
            f"{cells[t]['distinct_brands']:>7}" if t in cells and cells[t].get("n")
            else "      -" for t in temps
        )
        print(f"  {c:<22}{line}")

    print(f"\nCross-batch drift (noise floor), by category and temperature")
    print(f"  {'category':<22}" + "".join(f"{t:>7.2f}" for t in temps))
    for c in args.categories:
        cells = {s["temperature"]: s for s in summaries if s["category"] == c}
        line = "".join(
            f"{cells[t]['cross_batch_drift']:>7.3f}" if t in cells and cells[t].get("n")
            else "      -" for t in temps
        )
        print(f"  {c:<22}{line}")

    print(f"\nRequests {len(rows)}   modelled cost ${actual:.4f}   ->  {mf}")
    print("Run scripts/temperature_analysis.py for the per-brand and aside analysis.")


if __name__ == "__main__":
    asyncio.run(main())
