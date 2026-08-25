# Evertune Take-home: Gemini 2.5 Flash on Vertex AI

Adds Gemini 2.5 Flash as a provider, plus the evidence that it holds up under load.

- **[FINDINGS.md](FINDINGS.md)** — what I learned, what I changed and why, what is
  still unproven.
- **[RUNBOOK.md](RUNBOOK.md)** — how to run everything.

## What is here

```
llm/            provider layer: gemini.py, errors.py, retry.py, metrics.py,
                pricing.py  (llm.py extended for grounding - see FINDINGS 2)
service/        the integration deployed as an HTTP service - the system under test
harness/        in-process batch driver + cost governor + preflight
loadtest/k6/    external load generator (drives the service, or the vendor direct)
mock/           fake Vertex endpoint for $0 iteration
observability/  Prometheus + Grafana, provisioned dashboards
tests/          39 tests, no network, no spend
results/real/   live measurements, raw JSONL + manifests
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

## What I found

**Grounding is the measurement axis, and it is ~99% of the cost.** Evertune runs each
prompt with live search off, then on; the delta is the product. A grounded request
costs **88x** an ungrounded one because live search bills on a separate per-prompt SKU
— and neither the Batch API (no tool support) nor context caching (needs 2,048 input
tokens; the workload is 35) can discount it. I spent a while optimising *token* cost
before realising it is the cheap 1%. See FINDINGS §0c and §6c.

**One production unit, measured: 100 samples of one prompt, both conditions, $2.67.**
Dreame appears in **5 of 100 ungrounded samples and 97 of 100 grounded** (95% CI on the
difference: [+86, +97]). Anker falls 18 → 3 as Eufy rises 65 → 99. That is the two
conditions working: one reports what the model absorbed in training, the other what the
live web says now. Also settled there: 1,536 is the right output cap (1% truncation vs
50% at 512), there is no dedup discount, and 100 grounded prompts at c=25 tripped no
separate search quota. See FINDINGS §0d.

**Citations cannot be compared across samples.** All 852 returned source URLs were
unique — Vertex signs a per-request redirect token, so the same publisher gets a
different URL every call. Provenance has to be resolved at collection time or it is
gone permanently. This is the largest remaining engineering gap.

**The concurrency ceiling is 128, and past it the bottleneck is our own event loop —
not Vertex.** Throughput scales linearly to 73.7 rps at c=128, then *falls* to 63.0 at
256 and 43.7 at 1024. Event loop lag goes <5 ms → 457 ms → 4,301 ms while the
connection pool sits at 50% throughout. Pushing 8x past the optimum delivers 40% less
work. Scaling further means more processes, not more concurrency. See FINDINGS §6g.

**Sustained load: 47,677 requests over 20.8 minutes at 36.9 rps**, with zero failures
reaching a caller. Vertex rate-limited 0.038%, all absorbed by retry — visible only
because retries are hand-rolled rather than delegated to the SDK. The tail is not a
queue in our process: p99 spikes correlate with vendor retry events (6,910 ms in
windows with retries vs 4,717 ms without) while the connection pool sat at 25% and
event loop lag under 5 ms. An apparent p99 trend in a shorter run **did not replicate
and is retracted**. See FINDINGS §6f and
[docs/evidence/](docs/evidence/soak-evidence.png).

**Logprobs are free and reveal brands that 100 samples miss.** Token counts are
identical with them on or off. In a 100-sample run, Roborock appeared **zero times**
while holding 1.83% of the probability mass at the decision point — counting would
need ~1,300 samples to see it, and ~10,900 to resolve Shark at 0.23%. See FINDINGS §6e.

**The workload is batch at thousands of prompts/day, so cost is the problem, not
scale.** A 50,000-prompt day completes in 3.3 minutes at the measured ~250 rps. Moving
to thinking-off + Batch API is an **8.0x cost reduction** — $21,103/year to
$2,631/year at that volume, on tokens measured against Vertex us-central1. That is the
*ungrounded* arm only: Batch has no tool support, so the grounded arm cannot use it.
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

**The provided abstraction was held immutable, then changed deliberately.** I kept
`llm/llm.py` byte-identical until grounding made that untenable: the contract had no
way to say whether live search actually ran, and grounding has to be polymorphic
because Evertune compares across models. The change is strictly additive — original
fields keep their names, order and types; every addition defaults; the new parameter
is keyword-only — and a test pins all of it. Every provided file is still at its
original path. See FINDINGS §2 for the full reasoning, including the fallback if the
contract is considered fixed.

**The connection pool is the throughput ceiling and it is invisible.** At pool=8
against a service answering in 500 ms, the client reports 4.2 s and caps at 15.4 rps.
The vendor response gives no hint. Instrumented as `llm_pool_saturation_ratio`.

**Dynamic thinking costs ~4x more, and it is the SDK default.** Measured against
`evertune-tests` in us-central1, n=15 per config — thin for a precise multiplier, so
read it as the right order of magnitude: turning `thinking_budget` off gave
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
