#!/usr/bin/env python3
"""Does `responseSchema` pay for itself, and does it survive grounding?

Why
---
FINDINGS 8 claims structured output "would likely pay for itself" and that it converts
truncation from a silent failure into a detectable parse error. Both are arguments, not
measurements, and both underpin a recommendation. This tests them.

There is a second reason, which is the more important one. FINDINGS 0c found that
grounded answers change *shape* - 14 of 20 came back as structured listicles - and that
an extractor tuned on ungrounded prose misparses them quietly. Structured output is the
obvious fix, but "obvious fix" is where this document keeps being wrong, so it needs
checking rather than asserting. In particular, grounding and `responseSchema` are both
implemented as request-level features on Gemini, and whether they compose at all is a
factual question with a cheap answer.

Four arms, same prompts:

    prose   ungrounded      the current baseline
    schema  ungrounded      does structure cost tokens or answers?
    prose   grounded        the shape problem, as measured in 0c
    schema  grounded        does structure survive live search?

Measured
--------
1. **Does it compose with grounding.** A hard error here would kill the recommendation
   for the half of the workload that matters.
2. **Token cost.** JSON has syntactic overhead; if structure costs 30% more output
   tokens it is not free.
3. **Extraction reliability.** Parsing JSON is deterministic. The prose arms are
   extracted with the same closed-vocabulary matcher used elsewhere, so the comparison
   is "what a real pipeline would get" rather than "what a perfect parser would get".
4. **Truncation behaviour.** The claim is that a truncated JSON object is malformed and
   therefore detectable, where truncated prose reads as a complete short list. Forced
   directly with a deliberately low output cap.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import time
from collections import Counter

from google.genai import types

from llm.gemini import Gemini
from llm.llm import FinishReason

REPO = pathlib.Path(__file__).resolve().parent.parent

SYSTEM = (
    "You are a market research assistant. Answer concisely and name specific brands "
    "and products. Do not add disclaimers."
)
CATEGORIES = [
    "robot vacuum", "wireless earbud", "air purifier", "espresso machine",
    "mechanical keyboard", "office chair", "running shoe", "standing desk",
    "dash cam", "electric toothbrush",
]
QUESTION = "Which {category} brands are worth considering?"

# The shape a brand-tracking pipeline actually wants. `sentiment` is included because
# FINDINGS 0c established that mention counting is not enough - "we would not recommend
# BrandA" is a mention that means the opposite - and a schema is the cheapest place to
# get that attribution rather than inferring it downstream.
BRAND_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "brands": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "sentiment": {
                        "type": "STRING",
                        "enum": ["recommended", "mentioned", "not_recommended"],
                    },
                    "reason": {"type": "STRING"},
                },
                "required": ["name", "sentiment"],
            },
        }
    },
    "required": ["brands"],
}

VOCAB = {
    "roborock", "irobot", "roomba", "shark", "eufy", "dreame", "ecovacs", "deebot",
    "dyson", "samsung", "narwal", "anker", "xiaomi", "levoit", "tineco", "bissell",
    "sony", "bose", "apple", "airpods", "jabra", "sennheiser", "beats", "jbl",
    "soundcore", "technics", "nothing", "google", "coway", "blueair", "winix",
    "honeywell", "iqair", "rabbit air", "alen", "molekule", "breville", "sage",
    "de'longhi", "delonghi", "gaggia", "rancilio", "rocket", "lelit", "jura",
    "nespresso", "keychron", "ducky", "logitech", "razer", "corsair", "varmilo",
    "leopold", "hhkb", "nuphy", "glorious", "akko", "wooting", "steelseries", "drop",
    "herman miller", "aeron", "steelcase", "haworth", "humanscale", "secretlab",
    "branch", "autonomous", "sihoo", "ikea", "flexispot", "knoll", "nike", "adidas",
    "brooks", "asics", "hoka", "saucony", "new balance", "on", "altra", "mizuno",
    "salomon", "topo", "uplift", "fully", "jarvis", "vari", "fezibo", "viofo",
    "garmin", "nextbase", "blackvue", "thinkware", "vantrue", "rexing", "70mai",
    "oral-b", "braun", "philips", "sonicare", "quip", "burst", "colgate", "waterpik",
}


def prose_brands(text: str) -> set[str]:
    low = (text or "").lower()
    return {b for b in VOCAB if re.search(rf"(?<![\w-]){re.escape(b)}(?![\w-])", low)}


def parse_schema(text: str) -> tuple[set[str], dict[str, str] | None, bool]:
    """Returns (brands, sentiment map, parsed_ok)."""
    try:
        data = json.loads(text)
        entries = data.get("brands", [])
        names = {str(e.get("name", "")).lower() for e in entries if e.get("name")}
        sentiment = {
            str(e["name"]).lower(): e.get("sentiment", "")
            for e in entries if e.get("name")
        }
        return names, sentiment, True
    except (json.JSONDecodeError, AttributeError, TypeError):
        return set(), None, False


async def run_arm(
    provider: Gemini, label: str, *, schema: bool, grounded: bool,
    categories: list[str], samples: int, conc: int, max_tokens: int,
) -> list[dict]:
    sem = asyncio.Semaphore(conc)
    rows: list[dict] = []

    async def one(cat: str, i: int) -> None:
        async with sem:
            question = QUESTION.format(category=cat)
            try:
                if schema:
                    # Bypass ask_generic_question: response_schema is not on the
                    # contract, and the point here is to find out whether it should be.
                    cfg = types.GenerateContentConfig(
                        system_instruction=SYSTEM,
                        temperature=1.0,
                        max_output_tokens=max_tokens,
                        thinking_config=types.ThinkingConfig(thinking_budget=0),
                        response_mime_type="application/json",
                        response_schema=BRAND_SCHEMA,
                        tools=(
                            [types.Tool(google_search=types.GoogleSearch())]
                            if grounded else None
                        ),
                    )
                    raw = await provider._client.aio.models.generate_content(
                        model=provider.model, contents=question, config=cfg
                    )
                    usage = raw.usage_metadata
                    cand = (raw.candidates or [None])[0]
                    parts = getattr(getattr(cand, "content", None), "parts", None) or []
                    text = "".join(getattr(p, "text", "") or "" for p in parts)
                    finish = str(getattr(cand, "finish_reason", "") or "")
                    out_tok = (usage.candidates_token_count or 0) + (
                        usage.thoughts_token_count or 0
                    )
                    in_tok = usage.prompt_token_count or 0
                else:
                    r = await provider.ask_generic_question(
                        SYSTEM, question, 1.0, grounded=grounded
                    )
                    text, finish = r.answer, str(r.finish_reason)
                    in_tok, out_tok = r.input_tokens, r.output_tokens
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                rows.append({"arm": label, "category": cat, "i": i, "error": repr(exc)})
                return

            truncated = "MAX_TOKENS" in finish
            if schema:
                brands, sentiment, ok = parse_schema(text)
            else:
                brands, sentiment, ok = prose_brands(text), None, bool(text.strip())

            rows.append({
                "arm": label, "category": cat, "i": i,
                "schema": schema, "grounded": grounded,
                "input_tokens": in_tok, "output_tokens": out_tok,
                "finish_reason": finish, "truncated": truncated,
                "extract_ok": ok,
                "brands": sorted(brands),
                "sentiment": sentiment,
                "text": text,
            })

    await asyncio.gather(
        *(one(c, i) for c in categories for i in range(samples))
    )
    return rows


def summarise(rows: list[dict], label: str) -> dict:
    ok = [r for r in rows if "error" not in r]
    if not ok:
        return {"arm": label, "n": 0, "errors": len(rows)}
    parsed = [r for r in ok if r["extract_ok"]]
    return {
        "arm": label,
        "n": len(ok),
        "errors": len(rows) - len(ok),
        "extract_ok": len(parsed),
        "extract_rate": round(len(parsed) / len(ok), 4),
        "mean_output_tokens": round(sum(r["output_tokens"] for r in ok) / len(ok), 1),
        "mean_brands": round(sum(len(r["brands"]) for r in ok) / len(ok), 2),
        "truncated": sum(1 for r in ok if r["truncated"]),
        "distinct_brands": len({b for r in ok for b in r["brands"]}),
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=int, default=6, help="per category, per arm")
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--grounded-samples", type=int, default=1,
                    help="per category; grounded costs 123x so this stays small")
    ap.add_argument("--max-output-tokens", type=int, default=1024)
    ap.add_argument("--truncation-cap", type=int, default=200,
                    help="deliberately low, to force the truncation comparison")
    ap.add_argument("--project", default="evertune-tests")
    ap.add_argument("--location", default="us-central1")
    ap.add_argument("--budget-usd", type=float, default=1.00)
    ap.add_argument("--out", default="results/real/structured-output")
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    n_ug = len(CATEGORIES) * args.samples * 2      # prose + schema, ungrounded
    n_g = len(CATEGORIES) * args.grounded_samples * 2  # prose + schema, grounded
    n_tr = len(CATEGORIES) * 2                     # truncation probe, ungrounded
    est = (n_ug + n_tr) * 0.00031 + n_g * 0.0353
    print("Structured output vs prose")
    print(f"  ungrounded: {n_ug} requests   grounded: {n_g}   truncation probe: {n_tr}")
    print(f"  Estimated ${est:.2f} (grounded arm is ${n_g * 0.0353:.2f} of it), "
          f"ceiling ${args.budget_usd:.2f}")
    if est > args.budget_usd:
        print("Estimate exceeds ceiling; refusing.")
        return
    if not args.yes:
        print("\nRefusing to run without --yes.")
        return

    kwargs = dict(
        backend="vertex", project=args.project, location=args.location,
        thinking_budget=0, max_output_tokens=args.max_output_tokens,
        max_connections=args.concurrency * 2,
    )
    if args.base_url:
        kwargs["base_url"] = args.base_url
    provider = Gemini(**kwargs)

    started = time.time()
    rows: list[dict] = []
    arms = [
        ("prose-ungrounded", False, False, args.samples),
        ("schema-ungrounded", True, False, args.samples),
        ("prose-grounded", False, True, args.grounded_samples),
        ("schema-grounded", True, True, args.grounded_samples),
    ]
    for label, schema, grounded, n in arms:
        print(f"  {label} ...", flush=True)
        rows += await run_arm(
            provider, label, schema=schema, grounded=grounded,
            categories=CATEGORIES, samples=n, conc=args.concurrency,
            max_tokens=args.max_output_tokens,
        )

    # Truncation probe: same prompts, cap low enough to force MAX_TOKENS in both arms.
    for label, schema in (("prose-truncated", False), ("schema-truncated", True)):
        print(f"  {label} (cap {args.truncation_cap}) ...", flush=True)
        rows += await run_arm(
            provider, label, schema=schema, grounded=False,
            categories=CATEGORIES, samples=1, conc=args.concurrency,
            max_tokens=args.truncation_cap,
        )

    summaries = [summarise([r for r in rows if r["arm"] == a], a)
                 for a, *_ in arms]
    summaries += [summarise([r for r in rows if r["arm"] == a], a)
                  for a in ("prose-truncated", "schema-truncated")]

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    raw = out.with_name(f"{out.name}-{stamp}.jsonl")
    raw.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    ok_rows = [r for r in rows if "error" not in r]
    # Grounded token cost only; the SKU is added per grounded prompt.
    n_grounded = sum(1 for r in ok_rows if r.get("grounded"))
    tok_cost = sum(
        (r["input_tokens"] * 0.30 + r["output_tokens"] * 2.50) / 1e6 for r in ok_rows
    )
    actual = tok_cost + n_grounded * 0.035

    manifest = {
        "experiment": "structured-output",
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "duration_s": round(time.time() - started, 1),
        "project": args.project,
        "location": args.location,
        "requests": len(rows),
        "grounded_prompts_billed": n_grounded,
        "modelled_cost_usd": round(actual, 6),
        "truncation_cap": args.truncation_cap,
        "arms": summaries,
    }
    mf = out.with_name(f"{out.name}-{stamp}-manifest.json")
    mf.write_text(json.dumps(manifest, indent=2) + "\n")

    print(f"\n  {'arm':<20}{'n':>5}{'err':>5}{'extract':>10}{'out tok':>9}"
          f"{'brands':>8}{'trunc':>7}")
    for s in summaries:
        if not s.get("n"):
            print(f"  {s['arm']:<20}{0:>5}{s.get('errors', 0):>5}   ALL FAILED")
            continue
        print(f"  {s['arm']:<20}{s['n']:>5}{s['errors']:>5}{s['extract_rate']:>10.1%}"
              f"{s['mean_output_tokens']:>9.1f}{s['mean_brands']:>8.2f}{s['truncated']:>7}")

    # Sentiment is the thing prose extraction cannot give you at all.
    sent = Counter()
    for r in ok_rows:
        if r.get("sentiment"):
            sent.update(r["sentiment"].values())
    if sent:
        print(f"\n  Sentiment labels returned (schema arms only): {dict(sent)}")

    print(f"\n  Requests {len(rows)}   modelled ${actual:.4f}   ->  {mf}")


if __name__ == "__main__":
    asyncio.run(main())
