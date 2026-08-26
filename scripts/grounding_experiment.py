"""Grounded versus ungrounded on identical prompts.

Evertune's measurement runs each prompt with live search off, then on; the delta is the
product. This measures what that costs and what it changes.

Paired, not independent: the same prompt is asked in both conditions, so latency, token
and brand-set deltas are free of prompt-to-prompt variance. At this sample size the
pairing is doing most of the statistical work.

Deliberately excluded - a repeat arm, since production runs these days apart rather
than hours, and load, since grounding changes the upstream rather than our concurrency
behaviour and FINDINGS 4 already localised our ceiling to TLS.

Results in FINDINGS 2.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import time
from collections import Counter

from harness.workload import build_corpus
from llm.gemini import Gemini
from llm.llm import FinishReason
from llm.pricing import GROUNDING_USD_PER_1K_PROMPTS

REPO = pathlib.Path(__file__).resolve().parent.parent

# Rough brand extractor: capitalised tokens that are not sentence-initial and not
# common English words. This is a heuristic and is labelled as one. With N=20 the
# raw answers are also saved so the delta can be read by eye rather than trusted.
_STOP = {
    "The", "A", "An", "I", "If", "It", "They", "You", "We", "This", "That", "These",
    "For", "In", "On", "At", "With", "And", "But", "Or", "So", "Best", "Top", "Their",
    "Its", "My", "Your", "Our", "There", "Here", "What", "Which", "When", "Why", "How",
    "Compare", "Name", "Consider", "Overall", "Key", "Most", "Some", "Many", "All",
}


def brands(text: str) -> set[str]:
    """Pull probable brand names out of prose. Heuristic, not ground truth."""
    found: set[str] = set()
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        tokens = re.findall(r"\b[A-Z][A-Za-z0-9&'\-]*\b", sentence)
        # Skip the first token of each sentence: capitalisation there is grammatical.
        for tok in tokens[1:] if tokens else []:
            if tok not in _STOP and len(tok) > 2:
                found.add(tok)
    return found


async def run_condition(
    name: str, prompts, grounded: bool, args
) -> list[dict]:
    provider = Gemini(
        backend="vertex",
        project=args.project,
        location=args.location,
        model=args.model,
        thinking_budget=0,
        max_output_tokens=args.max_output_tokens,
        grounded=grounded,
        max_connections=args.concurrency,
    )
    sem = asyncio.Semaphore(args.concurrency)
    rows: list[dict] = []

    async def one(p) -> None:
        async with sem:
            t0 = time.perf_counter()
            try:
                r = await provider.ask_generic_question(p.system, p.question, 1.0)
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                rows.append({"prompt_id": p.id, "condition": name, "error": repr(exc)})
                return
            rows.append(
                {
                    "prompt_id": p.id,
                    "category": p.category,
                    "condition": name,
                    "grounded": r.grounded,
                    "input_tokens": r.input_tokens,
                    "output_tokens": r.output_tokens,
                    "thinking_tokens": r.thinking_tokens,
                    "cost_usd": r.cost_usd,
                    "latency_ms": (time.perf_counter() - t0) * 1000.0,
                    "finish_reason": str(r.finish_reason),
                    "truncated": r.finish_reason is FinishReason.MAX_TOKENS,
                    "search_queries": r.search_queries,
                    "grounding_sources": r.grounding_sources,
                    "answer": r.answer,
                    "brands": sorted(brands(r.answer or "")),
                }
            )

    await asyncio.gather(*(one(p) for p in prompts))
    return rows


def summarise(rows: list[dict], label: str) -> dict:
    ok = [r for r in rows if "error" not in r]
    if not ok:
        return {"condition": label, "n": 0, "errors": len(rows)}
    lat = sorted(r["latency_ms"] for r in ok)
    return {
        "condition": label,
        "n": len(ok),
        "errors": len(rows) - len(ok),
        "mean_input_tokens": sum(r["input_tokens"] for r in ok) / len(ok),
        "mean_output_tokens": sum(r["output_tokens"] for r in ok) / len(ok),
        "p50_latency_ms": lat[len(lat) // 2],
        "p95_latency_ms": lat[min(len(lat) - 1, int(len(lat) * 0.95))],
        "truncated": sum(1 for r in ok if r["truncated"]),
        "total_cost_usd": sum(r["cost_usd"] for r in ok),
        "with_sources": sum(1 for r in ok if r["grounding_sources"]),
        "mean_sources": sum(len(r["grounding_sources"]) for r in ok) / len(ok),
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prompts", type=int, default=20)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--project", default="evertune-tests")
    ap.add_argument("--location", default="us-central1")
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--max-output-tokens", type=int, default=512)
    ap.add_argument("--out", default="results/real/measurement/grounding")
    ap.add_argument(
        "--yes", action="store_true",
        help="required: this run bills real money against a real project",
    )
    args = ap.parse_args()

    prompts = build_corpus(size=args.prompts, complex_fraction=0.0)

    est_grounded = args.prompts * (GROUNDING_USD_PER_1K_PROMPTS / 1000.0 + 0.00029)
    est_plain = args.prompts * 0.00029
    print(f"Prompts:   {len(prompts)} (paired across 2 conditions)")
    print(f"Requests:  {len(prompts) * 2}")
    print(f"Estimated: ${est_plain + est_grounded:.2f} "
          f"(ungrounded ${est_plain:.4f} + grounded ${est_grounded:.2f})")
    if not args.yes:
        print("\nRefusing to run without --yes.")
        return

    started = time.time()
    ungrounded = await run_condition("ungrounded", prompts, False, args)
    grounded = await run_condition("grounded", prompts, True, args)
    rows = ungrounded + grounded

    s_un, s_gr = summarise(ungrounded, "ungrounded"), summarise(grounded, "grounded")

    # Paired brand-set delta.
    by_id: dict[str, dict[str, dict]] = {}
    for r in rows:
        if "error" not in r:
            by_id.setdefault(r["prompt_id"], {})[r["condition"]] = r
    deltas, only_grounded, only_plain = [], Counter(), Counter()
    for pid, pair in by_id.items():
        if len(pair) != 2:
            continue
        a, b = set(pair["ungrounded"]["brands"]), set(pair["grounded"]["brands"])
        union = a | b
        deltas.append(len(a & b) / len(union) if union else 1.0)
        only_grounded.update(b - a)
        only_plain.update(a - b)

    actual = sum(r["cost_usd"] for r in rows if "error" not in r)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    raw = out.with_name(f"{out.name}-{stamp}.jsonl")
    raw.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    manifest = {
        "experiment": "grounding",
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "duration_s": round(time.time() - started, 1),
        "project": args.project,
        "location": args.location,
        "model": args.model,
        "requests": len(rows),
        "modelled_cost_usd": round(actual, 6),
        "grounding_rate_assumed_usd_per_1k": GROUNDING_USD_PER_1K_PROMPTS,
        "grounded_prompts_billed": s_gr.get("n", 0),
        "conditions": [s_un, s_gr],
        "mean_brand_jaccard": round(sum(deltas) / len(deltas), 3) if deltas else None,
        "brands_only_when_grounded": only_grounded.most_common(15),
        "brands_only_when_ungrounded": only_plain.most_common(15),
        "raw": raw.name,
        "reconcile": (
            "Compare grounded_prompts_billed against the Vertex billing console "
            "'Grounding with Google Search' SKU ~24h from now. That settles the "
            "$14 vs $25 question and whether the free monthly allowance applied."
        ),
    }
    mf = out.with_name(f"{out.name}-{stamp}-manifest.json")
    mf.write_text(json.dumps(manifest, indent=2) + "\n")

    print(f"\n{'':<22}{'ungrounded':>14}{'grounded':>14}")
    for key, fmt in (
        ("mean_input_tokens", "{:.1f}"), ("mean_output_tokens", "{:.1f}"),
        ("p50_latency_ms", "{:.0f}"), ("p95_latency_ms", "{:.0f}"),
        ("truncated", "{:.0f}"), ("total_cost_usd", "${:.4f}"),
    ):
        print(f"  {key:<20}{fmt.format(s_un[key]):>14}{fmt.format(s_gr[key]):>14}")
    print(f"  {'input inflation':<20}{'':>14}"
          f"{s_gr['mean_input_tokens'] / max(1e-9, s_un['mean_input_tokens']):>13.2f}x")
    print(f"  {'answers w/ sources':<20}{'':>14}{s_gr['with_sources']:>14}")
    print(f"\nBrand-set overlap (Jaccard, paired): {manifest['mean_brand_jaccard']}")
    print(f"Only when grounded:   {[b for b, _ in only_grounded.most_common(8)]}")
    print(f"Only when ungrounded: {[b for b, _ in only_plain.most_common(8)]}")
    print(f"\nModelled cost: ${actual:.4f}   ->  {mf}")
    print(manifest["reconcile"])


if __name__ == "__main__":
    asyncio.run(main())
