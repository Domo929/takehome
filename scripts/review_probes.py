#!/usr/bin/env python3
"""Two claims from an external review that rested on assumption rather than measurement.

1. FINDINGS 1 says dynamic thinking is the SDK default. Every run in results/real/ set
   thinking_budget explicitly to 0 or -1, so the default itself was never observed. If
   it is not dynamic, section 4's framing - that the default is the expensive footgun -
   is wrong.

2. FINDINGS 2 says responseSchema cannot be combined with grounding, which is measured.
   The open question is whether a custom function-declaration tool can, since that would
   restore structured extraction on the grounded arm by a different route.

Both are cheap. The point is that the answers are checkable and were not checked.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import time

from google import genai
from google.genai import types

REPO = pathlib.Path(__file__).resolve().parent.parent

SYSTEM = "You are a market research assistant. Answer concisely and name brands."
QUESTION = "Which robot vacuum brands are worth considering?"

BRAND_FN = types.FunctionDeclaration(
    name="record_brands",
    description="Record the brands mentioned, with sentiment.",
    parameters={
        "type": "OBJECT",
        "properties": {
            "brands": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "name": {"type": "STRING"},
                        "sentiment": {"type": "STRING"},
                    },
                    "required": ["name"],
                },
            }
        },
        "required": ["brands"],
    },
)


async def probe(client, model: str, label: str, config) -> dict:
    row: dict = {"probe": label}
    started = time.perf_counter()
    try:
        r = await client.aio.models.generate_content(
            model=model, contents=QUESTION, config=config
        )
    except Exception as exc:  # noqa: BLE001 - the error IS the result here
        row.update(ok=False, error=type(exc).__name__, detail=str(exc)[:300])
        return row

    usage = r.usage_metadata
    cand = (r.candidates or [None])[0]
    parts = getattr(getattr(cand, "content", None), "parts", None) or []
    calls = [p.function_call for p in parts if getattr(p, "function_call", None)]
    gm = getattr(cand, "grounding_metadata", None)
    row.update(
        ok=True,
        latency_ms=round((time.perf_counter() - started) * 1000, 1),
        input_tokens=usage.prompt_token_count or 0,
        output_tokens=usage.candidates_token_count or 0,
        # The number that settles claim 1. Non-zero with no thinking_config means the
        # default reasons on its own.
        thinking_tokens=usage.thoughts_token_count or 0,
        finish_reason=str(getattr(cand, "finish_reason", "")),
        function_calls=[c.name for c in calls],
        function_args=[dict(c.args) for c in calls] if calls else [],
        grounded=gm is not None,
        search_queries=list(getattr(gm, "web_search_queries", None) or []) if gm else [],
        text=("".join(getattr(p, "text", "") or "" for p in parts))[:400],
    )
    return row


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", default="evertune-tests")
    ap.add_argument("--location", default="us-central1")
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--out", default="results/real/model/review-probes")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    print("Two probes: thinking default, and tool calling alongside grounding")
    print("  6 requests, 1 of them grounded. Estimated $0.04.")
    if not args.yes:
        print("\nRefusing to run without --yes.")
        return

    client = genai.Client(
        vertexai=True, project=args.project, location=args.location,
        http_options=types.HttpOptions(api_version="v1"),
    )
    search = types.Tool(google_search=types.GoogleSearch())
    fn_tool = types.Tool(function_declarations=[BRAND_FN])

    def cfg(**kw) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=SYSTEM, temperature=1.0, max_output_tokens=1024, **kw
        )

    probes = [
        # Claim 1. No thinking_config at all: whatever comes back is the real default.
        ("thinking-unset", cfg()),
        ("thinking-explicit-0", cfg(
            thinking_config=types.ThinkingConfig(thinking_budget=0))),
        ("thinking-explicit-dynamic", cfg(
            thinking_config=types.ThinkingConfig(thinking_budget=-1))),
        # Claim 2. Custom tool alone, then alongside search.
        ("function-tool-only", cfg(
            thinking_config=types.ThinkingConfig(thinking_budget=0), tools=[fn_tool])),
        ("function-tool-plus-search", cfg(
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            tools=[fn_tool, search])),
        # Both declarations on one Tool object, which the API models differently.
        ("single-tool-both-features", cfg(
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            tools=[types.Tool(function_declarations=[BRAND_FN],
                              google_search=types.GoogleSearch())])),
    ]

    rows = []
    for label, config in probes:
        row = await probe(client, args.model, label, config)
        rows.append(row)
        if row["ok"]:
            print(f"  {label:<28} OK   thinking={row['thinking_tokens']:>4}  "
                  f"grounded={row['grounded']}  fn_calls={row['function_calls']}")
        else:
            print(f"  {label:<28} FAIL {row['error']}: {row['detail'][:110]}")

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    raw = out.with_name(f"{out.name}-{stamp}.jsonl")
    raw.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    ok = [r for r in rows if r["ok"]]
    grounded_n = sum(1 for r in ok if r.get("grounded"))
    cost = sum(
        (r["input_tokens"] * 0.30 + (r["output_tokens"] + r["thinking_tokens"]) * 2.50)
        / 1e6 for r in ok
    ) + grounded_n * 0.035

    mf = out.with_name(f"{out.name}-{stamp}-manifest.json")
    mf.write_text(json.dumps({
        "experiment": "review-probes",
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "project": args.project, "location": args.location, "model": args.model,
        "requests": len(rows),
        "grounded_prompts_billed": grounded_n,
        "modelled_cost_usd": round(cost, 6),
        "probes": rows,
    }, indent=2) + "\n")

    unset = next((r for r in ok if r["probe"] == "thinking-unset"), None)
    if unset:
        verdict = "DYNAMIC" if unset["thinking_tokens"] > 0 else "OFF"
        print(f"\n  Thinking default: {verdict} "
              f"({unset['thinking_tokens']} thinking tokens with no config set)")

    print(f"\n  Modelled ${cost:.4f}  ->  {mf}")


if __name__ == "__main__":
    asyncio.run(main())
