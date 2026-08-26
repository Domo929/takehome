"""Does attaching a search tool change whether the model answers at all?

One request during earlier probing declined a question that the same model answers
freely with no tool attached. n=1, so it could have been nothing. It is worth settling
because of what it would mean for the product: Evertune's measurement is the difference
between the grounded and ungrounded answer. If attaching the tool changes the model's
willingness to respond, and not just its access to the web, then that difference carries
a confound and some of what looks like "search changed the answer" is really "the tool
changed the model".

Paired design. The same prompt goes to both conditions, so prompt-to-prompt variation
cancels. The only difference between arms is whether ``tools=[google_search]`` is on the
request.

A refusal here means the model declined or hedged its way out of answering, not that it
returned something short. Truncation is tracked separately because it looks similar in
aggregate and has a completely different cause (FINDINGS 1).

Note on power: with 50 prompts per arm and zero refusals observed, the 95% upper bound
on the true rate is about 6% (rule of three). So this run can rule out a common effect.
It cannot rule out a rare one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import time

from harness.workload import build_corpus
from llm.gemini import Gemini
from llm.llm import FinishReason
from llm.pricing import GROUNDING_FREE_PROMPTS_PER_MONTH, GROUNDING_USD_PER_1K_PROMPTS

REPO = pathlib.Path(__file__).resolve().parent.parent

# Phrases that mark a decline rather than a short answer. Kept deliberately narrow:
# matching "however" or "it depends" would swallow ordinary hedged prose and inflate
# the rate. Every match is written to the manifest so the classification can be
# checked by eye rather than trusted.
_REFUSAL_PATTERNS = [
    r"\bI (?:can(?:no|')t|am (?:un)?able to|'m (?:un)?able to)\b",
    r"\bI (?:do not|don't) have (?:access|the ability|enough)\b",
    r"\bI'm not able to\b",
    r"\bI(?:'m| am) (?:sorry|afraid)\b",
    r"\bas an AI\b",
    r"\bI (?:cannot|can't) (?:provide|recommend|answer|help)\b",
    r"\bunable to (?:provide|recommend|answer|browse|access)\b",
    r"\bI (?:do not|don't) (?:provide|make) (?:specific )?recommendations\b",
]
_REFUSAL_RE = re.compile("|".join(_REFUSAL_PATTERNS), re.IGNORECASE)


def refusal_markers(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in _REFUSAL_RE.finditer(text or "")})


async def run_condition(name: str, prompts, *, grounded: bool, args) -> list[dict]:
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
                r = await provider.ask_generic_question(p.system, p.question, args.temperature)
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                rows.append({"prompt_id": p.id, "condition": name, "error": repr(exc)})
                return
            answer = r.answer or ""
            markers = refusal_markers(answer)
            rows.append(
                {
                    "prompt_id": p.id,
                    "category": p.category,
                    "condition": name,
                    "tool_attached": grounded,
                    "grounded": r.grounded,
                    "refused": bool(markers),
                    "refusal_markers": markers,
                    "answer_chars": len(answer),
                    "empty": not answer.strip(),
                    "truncated": r.finish_reason is FinishReason.MAX_TOKENS,
                    "finish_reason": str(r.finish_reason),
                    "input_tokens": r.input_tokens,
                    "output_tokens": r.output_tokens,
                    "cost_usd": r.cost_usd,
                    "latency_ms": (time.perf_counter() - t0) * 1000.0,
                    "search_queries": r.search_queries,
                    "answer": answer,
                }
            )

    await asyncio.gather(*(one(p) for p in prompts))
    return rows


def summarise(rows: list[dict], label: str) -> dict:
    ok = [r for r in rows if "error" not in r]
    if not ok:
        return {"condition": label, "n": 0, "errors": len(rows)}
    return {
        "condition": label,
        "n": len(ok),
        "errors": len(rows) - len(ok),
        "refusals": sum(1 for r in ok if r["refused"]),
        "empty": sum(1 for r in ok if r["empty"]),
        "truncated": sum(1 for r in ok if r["truncated"]),
        "mean_answer_chars": sum(r["answer_chars"] for r in ok) / len(ok),
        "total_cost_usd": sum(r["cost_usd"] for r in ok),
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prompts", type=int, default=50)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--project", default="evertune-tests")
    ap.add_argument("--location", default="us-central1")
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--max-output-tokens", type=int, default=512)
    ap.add_argument("--out", default="results/real/measurement/tool-refusal")
    ap.add_argument(
        "--yes", action="store_true",
        help="required: this run bills real money against a real project",
    )
    args = ap.parse_args()

    prompts = build_corpus(size=args.prompts, complex_fraction=0.0)
    n = len(prompts)
    billed = max(0, n - GROUNDING_FREE_PROMPTS_PER_MONTH)
    worst = n * GROUNDING_USD_PER_1K_PROMPTS / 1000.0 + 2 * n * 0.000373

    print(f"{2 * n} requests: {n} with the search tool attached, {n} without.")
    print(f"grounding SKU: {n} prompts, {billed} of them billable after the free tier.")
    print(f"expected ~${2 * n * 0.000373:.3f}, worst case ${worst:.2f} if the free tier is spent.")
    if not args.yes:
        print("\nrefusing to run without --yes")
        return

    started = time.time()
    # Sequential, not concurrent: running both arms at once would have them competing
    # for the same quota, which turns a content question into a capacity one.
    with_tool = await run_condition("tool_attached", prompts, grounded=True, args=args)
    without = await run_condition("no_tool", prompts, grounded=False, args=args)

    by_id = {r["prompt_id"]: r for r in without if "error" not in r}
    discordant = []
    for r in with_tool:
        if "error" in r:
            continue
        other = by_id.get(r["prompt_id"])
        if other and r["refused"] != other["refused"]:
            discordant.append(
                {
                    "prompt_id": r["prompt_id"],
                    "refused_with_tool": r["refused"],
                    "refused_without": other["refused"],
                    "markers": r["refusal_markers"] or other["refusal_markers"],
                }
            )

    s_with, s_without = summarise(with_tool, "tool_attached"), summarise(without, "no_tool")
    manifest = {
        "experiment": "tool_refusal",
        "question": "does attaching google_search change whether the model answers?",
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "duration_s": round(time.time() - started, 1),
        "project": args.project,
        "location": args.location,
        "model": args.model,
        "temperature": args.temperature,
        "max_output_tokens": args.max_output_tokens,
        "paired_prompts": n,
        "conditions": [s_with, s_without],
        "discordant_pairs": discordant,
        # Key names match the other experiment manifests so scripts/spend_report.py
        # picks this run up. A run missing from the ledger is a run that silently
        # under-reports what the project was charged.
        "requests": s_with.get("n", 0) + s_without.get("n", 0),
        "modelled_cost_usd": s_with.get("total_cost_usd", 0) + s_without.get("total_cost_usd", 0),
        "grounded_prompts_billed": n,
        "grounding_rate_assumed_usd_per_1k": GROUNDING_USD_PER_1K_PROMPTS,
    }

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = REPO / f"{args.out}-{stamp}"
    out.parent.mkdir(parents=True, exist_ok=True)
    (out.with_name(out.name + "-manifest.json")).write_text(json.dumps(manifest, indent=2))
    (out.with_name(out.name + "-records.json")).write_text(
        json.dumps({"tool_attached": with_tool, "no_tool": without}, indent=2)
    )

    print(f"\n{'condition':<16}{'n':>4}{'refused':>9}{'empty':>7}{'trunc':>7}{'mean chars':>12}")
    for s in (s_with, s_without):
        print(f"{s['condition']:<16}{s['n']:>4}{s['refusals']:>9}{s['empty']:>7}"
              f"{s['truncated']:>7}{s['mean_answer_chars']:>12.0f}")

    r_with, r_without = s_with["refusals"], s_without["refusals"]
    print(f"\ndiscordant pairs (one arm refused, the other did not): {len(discordant)}")
    if not discordant and r_with == r_without == 0:
        print(f"zero refusals in either arm. 95% upper bound on the true rate is "
              f"~{3 / n * 100:.1f}% (rule of three), so a common effect is ruled out "
              f"and a rare one is not.")
    else:
        for d in discordant:
            print(f"  {d['prompt_id']}: with_tool={d['refused_with_tool']} "
                  f"without={d['refused_without']} {d['markers']}")

    print(f"\ncost ${manifest['modelled_cost_usd']:.4f} modelled, "
          f"{n} grounded prompts against the free allowance")
    print(f"wrote {out.with_name(out.name + '-manifest.json').relative_to(REPO)}")


if __name__ == "__main__":
    asyncio.run(main())
