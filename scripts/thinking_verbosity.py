"""Is the thinking multiplier a property of the model, or of your prompt?

The ratio is roughly (thinking + answer) / answer, so it should depend on how long the
answer would have been without thinking. If that holds, quoting a single multiplier for
"what thinking costs" is meaningless without saying what you asked.

Terse prompt against our normal one, both thinking conditions. Results in FINDINGS 1.

Note: a third arm with a deliberately verbose prompt hit the output cap in both
conditions, so it measured the cap rather than the model. Raise max_output_tokens well
past 2048 before adding prompts that produce long answers.
"""
import asyncio, json, statistics, sys, time
from llm.gemini import Gemini

SYS = "You are a market research assistant."
PROMPTS = {
    "terse":   ("Name the single best robot vacuum brand. Reply with the brand name only.", 
                "Answer with ONLY a brand name. No punctuation, no explanation."),
    "normal":  ("Which robot vacuum brands are worth considering?", SYS),
    "verbose": ("Compare the top robot vacuum brands across price, durability and "
                "warranty, then rank them and explain each ranking.", SYS),
}

async def arm(prompt, system, budget, n, conc):
    p = Gemini(backend="vertex", project="evertune-tests", location="us-central1",
               thinking_budget=budget, max_output_tokens=2048, max_connections=conc*2)
    sem = asyncio.Semaphore(conc); out=[]
    async def one(_):
        async with sem:
            try:
                r = await p.ask_generic_question(system, prompt, 1.0)
                out.append({"in": r.input_tokens, "out": r.output_tokens,
                            "think": r.thinking_tokens})
            except Exception as e:
                out.append({"error": repr(e)})
    await asyncio.gather(*(one(i) for i in range(n)))
    return [r for r in out if "error" not in r]

def cost(r): return (r["in"]*0.30 + r["out"]*2.50)/1e6

async def main():
    n, conc = 40, 8
    print(f"{'prompt':<10}{'off tok':>9}{'dyn tok':>9}{'thinking':>10}{'ratio':>8}")
    rows={}
    for name,(q,s) in PROMPTS.items():
        off = await arm(q, s, 0, n, conc)
        dyn = await arm(q, s, -1, n, conc)
        ro = statistics.fmean(map(cost,dyn))/statistics.fmean(map(cost,off))
        rows[name]={"off":off,"dyn":dyn,"ratio":ro}
        print(f"{name:<10}{statistics.fmean(r['out'] for r in off):>9.1f}"
              f"{statistics.fmean(r['out'] for r in dyn):>9.1f}"
              f"{statistics.fmean(r['think'] for r in dyn):>10.1f}{ro:>7.2f}x")
    total = sum(cost(r) for v in rows.values() for k in ("off","dyn") for r in v[k])
    print(f"\ncost ${total:.4f}")
    json.dump({k:{"ratio":v["ratio"],
                  "off_tokens":statistics.fmean(r['out'] for r in v['off']),
                  "dyn_tokens":statistics.fmean(r['out'] for r in v['dyn']),
                  "thinking":statistics.fmean(r['think'] for r in v['dyn']),
                  "n":len(v['off'])} for k,v in rows.items()},
              open("results/real/model/thinking-verbosity.json","w"), indent=2)

asyncio.run(main())
