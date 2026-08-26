# Evertune Take-home: Gemini 2.5 Flash on Vertex AI

Gemini 2.5 Flash added as a provider, plus the evidence that it holds up when you point
real traffic at it.

- **[FINDINGS.md](FINDINGS.md)**, what I measured, in the order I measured it, and
  what I'd change before this went to production.
- **[RUNBOOK.md](RUNBOOK.md)**, how to run any of it.

## What's here

```
llm/            the provider: gemini.py, errors.py, retry.py, metrics.py, pricing.py
service/        the provider as an HTTP service - what the load tests actually hit
loadtest/k6/    service.js (load test us), vertex.js (the control), lib/ (shared corpus)
harness/        in-process experiment driver with a hard spend breaker
mock/           fake Vertex endpoint - real wire contract, costs nothing
observability/  Prometheus + Grafana, dashboards provisioned from disk
scripts/        experiments and reporting
tests/          52 tests, no network, no spend
results/real/   the raw data behind every number in FINDINGS
docs/evidence/  rendered dashboards
```

## Try it without credentials

The whole stack runs against a fake Vertex endpoint that speaks the real
`:generateContent` contract over real HTTP. It'll produce 429s, `MAX_TOKENS`
starvation, safety blocks, empty 200s and grounding responses on demand, which is the
point, because a real vendor won't.

```bash
make venv && make test
make mock-up && make obs-up && make service-up
make overhead          # what our layer costs
make capacity          # where it sheds load
```

Grafana at http://localhost:3000, folder *Takehome*.

## The short version

Six things that changed what I'd build. All of it, with the data, is in
**[FINDINGS.md](FINDINGS.md)**.

**Grounding is the measurement, and it's ~99% of the bill.** Evertune runs each prompt
with live search off, then on, and the gap is the product. A grounded request costs 123x
an ungrounded one on a separate per-prompt SKU. Which means most token optimisations
Batch, caching, even switching to a cheaper model, work on about 1% of the spend.

**`temperature=0` can't express a brand share.** Across 11 categories, not one brand in
103 landed between a 10% and 90% mention rate. Every brand reads as always-named or
never-named. It also finds ~35% fewer brands. The right value is 1.0, which is the
model's own default.

**Thinking is on by default, and what it costs depends on what you ask.** 3.6x on our
prompts, 38x on a terse one. The multiplier is roughly (thinking + answer) / answer, so
it swings 10-fold with prompt shape. What holds is that ~77% of billed output is
reasoning nobody reads. And thinking shares its budget with `max_output_tokens`, so a
generous budget returns HTTP 200 with no text at all.

**The concurrency ceiling is ours, not Vertex's.** Throughput scales cleanly to 128, then
collapses. And event loop lag goes from 5 ms to 4.3 seconds while the connection pool
sits at 50%. Past that point you need more processes, not more concurrency.

**Structured output doesn't work where you need it.** `responseSchema` can't be combined
with the search tool, and neither can function calling. So the one condition where
extraction is hardest is the one where you can't have a schema.

**Citations can't be compared across samples.** Every grounding URL is a per-request
signed token that expires. Resolve provenance at collection time or lose it.

Check any of it without spending anything:

```bash
python scripts/confidence.py     # bootstrap CIs from committed data
python scripts/verify_pricing.py # rates against Google's billing catalog
python scripts/spend_report.py   # what this cost, by account
```

## Cost safety

Dry run by default; `--confirm` to spend. A pre-flight estimate refuses to start when
it already exceeds `--budget-usd`, and a runtime breaker checks accumulated *actual*
spend before every dispatch. See FINDINGS §7 and RUNBOOK.

---

<details>
<summary>Original exercise brief (unmodified)</summary>

# Evertune Take-home Exercise: Adding Gemini 2.5 Flash

This repo contains a small sample of our LLM vendor integration. We'd like you to add support for Gemini 2.5 Flash on Google Vertex and report back on your findings.

# Setup

You'll need the `gcloud` CLI installed and configured against our project, which we will provide for you.

# What to build

Implement Gemini 2.5 Flash as a provider in this system, and demonstrably prove it will hold up at production scale. We care about both halves of that sentence: a working integration *and* the evidence that it will not fall over when we point real traffic at it.

How you structure the code is up to you. The existing providers are a reference, not a template. If something about Gemini doesn't fit those patterns, deviate and tell us why.

For the "prove it works at scale" half: design and run whatever load tests, harnesses, or experiments you'd want to see before signing off on this for production. Show us the numbers, the failure modes you uncovered, and the headroom (or lack thereof) you found.

# Deliverables

We're less interested in a "completed checklist" and more interested in what you learned. In your write-up, we'd like to see:

- How the integration behaves under realistic load. Pick a workload, run it, and tell us what you observed.
- Anything you discovered about this model, quirks, failure modes, parameters that mattered, things that surprised you compared to other LLMs you've used.
- Decisions you made and the tradeoffs behind them. If you tried something that didn't work, that's worth including too.
- What you'd want to do next if this were going to production, and what you'd want to know before getting there.

</details>
