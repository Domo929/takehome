#!/usr/bin/env python3
"""What do logprobs add on top of sampling?

Evertune already runs each prompt 100 times; that is a settled methodological choice
and this experiment does not try to reduce it. The question here is narrower and more
useful: **given 100 samples we are taking anyway, what extra information do logprobs
provide that counting cannot?**

Design
------
The prompt is deliberately constrained to a single brand name. That makes the first
token position an unambiguous branch point: the model's distribution over "which
brand" is directly readable there, rather than tangled up in prose, ordering effects
and multi-token names spread through a paragraph.

That is a simplification of the real workload, and it is the point. It isolates the
mechanism so the result is legible; whether the same signal survives in free-form
prose is a follow-up, not a precondition.

For each of N samples we record:

* the sampled brand, which is what counting sees
* the top-k alternatives at the branch point with their probabilities, which is what
  counting cannot see

Then we compare the two views. The interesting quantity is brands that appear in
**zero** samples but carry consistent probability mass: counting reports those as
absent, indistinguishable from a brand the model has never heard of.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import pathlib
import time
from collections import Counter, defaultdict

from google import genai
from google.genai import types

REPO = pathlib.Path(__file__).resolve().parent.parent

SYSTEM = (
    "You are a market research assistant. Answer with ONLY a brand name, "
    "nothing else. No punctuation, no explanation."
)
QUESTION = "What is the single best robot vacuum brand?"


async def one(client, model: str, top_k: int) -> dict | None:
    cfg = types.GenerateContentConfig(
        system_instruction=SYSTEM,
        temperature=1.0,  # production uses sampling; 0 would defeat the purpose
        max_output_tokens=8,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        response_logprobs=True,
        logprobs=top_k,
    )
    try:
        r = await client.aio.models.generate_content(
            model=model, contents=QUESTION, config=cfg
        )
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:200]}

    cand = r.candidates[0]
    usage = r.usage_metadata
    text = (r.text or "").strip()

    lr = getattr(cand, "logprobs_result", None)
    top_first: list[tuple[str, float]] = []
    if lr is not None:
        tc = getattr(lr, "top_candidates", None) or []
        if tc:
            # Position 0 is the branch point: which brand to name.
            top_first = [
                (c.token, float(c.log_probability)) for c in (tc[0].candidates or [])
            ]

    return {
        "text": text,
        "input_tokens": usage.prompt_token_count or 0,
        "output_tokens": usage.candidates_token_count or 0,
        "avg_logprobs": getattr(cand, "avg_logprobs", None),
        "top_first_token": top_first,
    }


async def main_async(args: argparse.Namespace) -> None:
    client = genai.Client(
        vertexai=True,
        project=args.project,
        location=args.location,
        http_options=types.HttpOptions(api_version="v1"),
    )

    sem = asyncio.Semaphore(args.concurrency)

    async def guarded():
        async with sem:
            return await one(client, args.model, args.top_k)

    print(f"  running {args.n} samples against {args.project}/{args.location} ...")
    started = time.perf_counter()
    results = await asyncio.gather(*(guarded() for _ in range(args.n)))
    elapsed = time.perf_counter() - started

    ok = [r for r in results if r and "error" not in r]
    errs = [r for r in results if r and "error" in r]

    # --- what counting sees -------------------------------------------------
    sampled = Counter(r["text"] for r in ok if r["text"])

    # --- what logprobs additionally see -------------------------------------
    # Aggregate the probability mass each first token carried, across samples.
    mass: dict[str, list[float]] = defaultdict(list)
    for r in ok:
        for tok, lp in r["top_first_token"]:
            mass[tok].append(math.exp(lp))

    mean_p = {t: sum(v) / len(v) for t, v in mass.items()}
    seen_first_tokens = {s.split()[0][: len(s)] for s in sampled}

    def sampled_startswith(tok: str) -> bool:
        t = tok.strip()
        return any(s.startswith(t) for s in sampled if t) if t else False

    near_misses = {
        t: p for t, p in mean_p.items() if not sampled_startswith(t) and t.strip()
    }

    in_tok = sum(r["input_tokens"] for r in ok)
    out_tok = sum(r["output_tokens"] for r in ok)
    cost = (in_tok * 0.30 + out_tok * 2.50) / 1e6

    print(f"\n  {len(ok)}/{args.n} ok in {elapsed:.1f}s, {len(errs)} errors, ${cost:.6f}")

    print(f"\n=== What counting sees ({len(sampled)} distinct answers) ===")
    for name, n in sampled.most_common(15):
        print(f"  {name:<28} {n:>4} / {len(ok)}   ({n/len(ok)*100:>5.1f}%)")

    print(f"\n=== What logprobs additionally see at the branch point ===")
    print(f"  {len(mean_p)} distinct first tokens carried probability mass")
    print(f"  {'token':<20} {'mean P':>9} {'appeared in samples?':>22}")
    for tok, p in sorted(mean_p.items(), key=lambda kv: -kv[1])[:15]:
        flag = "yes" if sampled_startswith(tok) else "NO  <- invisible to counting"
        print(f"  {tok!r:<20} {p:>9.4f} {flag:>22}")

    floor = 1.0 / len(ok) if ok else 0
    below_floor = {t: p for t, p in mean_p.items() if 0 < p < floor}

    print(f"\n=== Resolution ===")
    print(f"  counting floor with n={len(ok)}: {floor*100:.1f}% (anything rarer reads as zero)")
    print(f"  tokens with mass below that floor: {len(below_floor)}")
    for t, p in sorted(below_floor.items(), key=lambda kv: -kv[1])[:8]:
        print(f"    {t!r:<18} P={p:.5f}  ({p*100:.3f}%)")

    print(f"\n=== Near misses: probability mass, zero samples ===")
    if near_misses:
        for t, p in sorted(near_misses.items(), key=lambda kv: -kv[1])[:10]:
            print(f"  {t!r:<20} mean P={p:.4f}  ({p*100:.2f}%) - counting reports ABSENT")
    else:
        print("  none at this sample size")

    ent = [
        -sum(math.exp(lp) * lp for _, lp in r["top_first_token"])
        for r in ok
        if r["top_first_token"]
    ]
    if ent:
        print(f"\n=== Branch-point entropy (is n={len(ok)} enough for this prompt?) ===")
        print(f"  mean {sum(ent)/len(ent):.3f} nats, max {max(ent):.3f}")

    out = REPO / "results" / "real" / "logprobs-experiment.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "n": len(ok),
                "errors": len(errs),
                "elapsed_s": round(elapsed, 1),
                "cost_usd": round(cost, 6),
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "sampled_counts": dict(sampled),
                "mean_p_by_first_token": mean_p,
                "near_misses": near_misses,
                "counting_floor": floor,
            },
            indent=2,
        )
    )
    print(f"\n  wrote {out.relative_to(REPO)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--project", default="evertune-tests")
    ap.add_argument("--location", default="us-central1")
    ap.add_argument("--model", default="gemini-2.5-flash")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
