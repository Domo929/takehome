# Evertune Take-home: Gemini 2.5 Flash on Vertex AI

Adds Gemini 2.5 Flash as a provider, plus the evidence that it holds up under load.

- **[FINDINGS.md](FINDINGS.md)** — what I learned, what I changed and why, what is
  still unproven.
- **[RUNBOOK.md](RUNBOOK.md)** — how to run everything.

## What is here

```
llm/            provider layer: gemini.py, response.py, errors.py, retry.py,
                metrics.py, pricing.py  (llm.py is UNCHANGED from upstream)
service/        the integration deployed as an HTTP service - the system under test
harness/        in-process batch driver + cost governor + preflight
loadtest/k6/    external load generator (drives the service, or the vendor direct)
mock/           fake Vertex endpoint for $0 iteration
observability/  Prometheus + Grafana, provisioned dashboards
tests/          12 tests, no network, no spend
results/real/   live measurements (n=15 per thinking config)
```

## Try it without credentials

The whole system runs against a fake Vertex endpoint that speaks the real
`:generateContent` contract over real HTTP — including 429s, `MAX_TOKENS`
starvation, safety blocks, and empty 200s.

```bash
make venv && make test
make mock-up && make obs-up
make pool-experiment      # reproduces the connection-pool ceiling
make k6-constant          # k6 control run into Prometheus
```

Grafana at http://localhost:3000, *Takehome* folder.

## Three things worth knowing

**Vertex never rate-limited us — it just got slower.** Zero 429s across 630 requests
up to concurrency 128. Throughput plateaus at ~15 rps around concurrency 32; sixteen
times the concurrency bought nothing while p99 nearly doubled. `parallelism()` now
defaults to the measured knee of 32. See FINDINGS §6f.

**Logprobs are free and reveal brands that 100 samples miss.** Token counts are
identical with them on or off. In a 100-sample run, Roborock appeared **zero times**
while holding 1.83% of the probability mass at the decision point — counting would
need ~1,300 samples to see it, and ~10,900 to resolve Shark at 0.23%. See FINDINGS §6e.

**The workload is batch at thousands of prompts/day, so cost is the problem, not
scale.** A 50,000-prompt day completes in 3.3 minutes at the measured ~250 rps. Moving
to thinking-off + Batch API + context caching is an **8.2x cost reduction** —
$21,103/year to $2,576/year at that volume, on tokens measured against Vertex
us-central1.
See FINDINGS §0b and §6c.

**Two endpoints, one provider.** Gemini is served by both the Gemini Developer API
(API key, fixed published quota) and Vertex AI (GCP project, Dynamic Shared Quota).
`GEMINI_BACKEND` selects. Both have now been measured against
(`evertune-tests` for Vertex). Model behaviour transfers between them; capacity and
latency numbers do not. See FINDINGS §0.

**Concurrency adapts to available capacity.** `parallelism()` as a constant assumes a
ceiling you can discover once; Dynamic Shared Quota moves. The controller keys on
latency rather than error codes, because Vertex absorbs overload by slowing down
rather than rejecting. Against a backend whose capacity collapses mid-run it produced
~30x fewer errors than a fixed cap tuned for the good case, which suffered congestion
collapse. It is off by default. See FINDINGS §6b.

**Our integration adds ~2 ms to a ~400 ms request** and sheds load rather than
collapsing. k6 drives `service/app.py` over HTTP the way production traffic would,
and the same script can bypass us to hit the vendor directly; the difference is our
cost. At 4x the sustainable rate our overhead stayed flat at 0.19 ms p99 while the
service returned 503s instead of queueing. See FINDINGS §6.

**The provided abstraction is untouched.** `llm/llm.py` is byte-identical to
upstream. `GeminiResponse` extends `LLM.SimpleResponse` additively for the metadata
Gemini needs, so base-contract callers keep working — enforced by a test that fails if
the base dataclass ever changes. See FINDINGS §2.

**The model retires 2026-10-16.** Seven weeks out. The integration works, but the
durable value is a provider layer where swapping models is a config change. See
FINDINGS §1.

**The connection pool is the throughput ceiling and it is invisible.** At pool=8
against a service answering in 500 ms, the client reports 4.2 s and caps at 15.4 rps.
The vendor response gives no hint. Instrumented as `llm_pool_saturation_ratio`.

**Dynamic thinking costs 4.0x more, and it is the SDK default.** Measured against
`evertune-tests` in us-central1, n=15 per config: turning `thinking_budget` off gave
4.0x lower cost and 2.8x better p50, with 80% of billed output tokens being invisible
reasoning. The ratio holds across regions and tiers; the latency does not — the same
request ranges from 976 ms to 4,106 ms depending on tier and region alone. Default
here is `0`, opt-in only. See FINDINGS §4.

## Cost safety

Dry run by default; `--confirm` to spend. A pre-flight estimate refuses to start when
it already exceeds `--budget-usd`, and a runtime breaker checks accumulated *actual*
spend before every dispatch. Verified by deliberately overspending — see FINDINGS §7.

---

<details>
<summary>Original exercise brief (unmodified)</summary>

# Evertune Take-home Exercise: Adding Gemini 2.5 Flash

This repo contains a small sample of our LLM vendor integration. We'd like you to add support for Gemini 2.5 Flash on Google Vertex and report back on your findings.

# Setup

You'll need the `gcloud` CLI installed and configured against our project, which we will provide for you.

# What to build

Implement Gemini 2.5 Flash as a provider in this system, and demonstrably prove it will hold up at production scale. We care about both halves of that sentence: a working integration *and* the evidence that it will not fall over when we point real traffic at it.

How you structure the code is up to you — the existing providers are a reference, not a template. If something about Gemini doesn't fit those patterns, deviate and tell us why.

For the "prove it works at scale" half: design and run whatever load tests, harnesses, or experiments you'd want to see before signing off on this for production. Show us the numbers, the failure modes you uncovered, and the headroom (or lack thereof) you found.

# Deliverables

We're less interested in a "completed checklist" and more interested in what you learned. In your write-up, we'd like to see:

- How the integration behaves under realistic load. Pick a workload, run it, and tell us what you observed.
- Anything you discovered about this model — quirks, failure modes, parameters that mattered, things that surprised you compared to other LLMs you've used.
- Decisions you made and the tradeoffs behind them. If you tried something that didn't work, that's worth including too.
- What you'd want to do next if this were going to production, and what you'd want to know before getting there.

</details>
