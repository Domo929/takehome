# Evertune Take-home: Gemini 2.5 Flash on Vertex AI

Adds Gemini 2.5 Flash as a provider, plus the evidence that it holds up under load.

- **[FINDINGS.md](FINDINGS.md)** — what I learned, what I changed and why, what is
  still unproven.
- **[RUNBOOK.md](RUNBOOK.md)** — how to run everything.

## What is here

```
llm/            provider layer: gemini.py, errors.py, retry.py, metrics.py, pricing.py
harness/        load harness (the subject under test) + cost governor
loadtest/k6/    k6 control harness + ADC token sidecar
mock/           fake Vertex endpoint for $0 iteration
observability/  Prometheus + Grafana, provisioned dashboards
tests/          10 tests, no network, no spend
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

**The model retires 2026-10-16.** Seven weeks out. The integration works, but the
durable value is a provider layer where swapping models is a config change. See
FINDINGS §1.

**The connection pool is the throughput ceiling and it is invisible.** At pool=8
against a service answering in 500 ms, the client reports 4.2 s and caps at 15.4 rps.
The vendor response gives no hint. Instrumented as `llm_pool_saturation_ratio`.

**Thinking tokens bill at the output rate and share the output budget.** Set
`thinking_budget` at or above `max_output_tokens` and you get HTTP 200, no text, and a
full charge. Default here is `0`, opt-in only.

## Cost safety

Dry run by default; `--confirm` to spend. A pre-flight estimate refuses to start when
it already exceeds `--budget-usd`, and a runtime breaker checks accumulated *actual*
spend before every dispatch. Verified by deliberately overspending — see FINDINGS §7.

---

<details>
<summary>Original exercise brief</summary>

This repo contains a small sample of our LLM vendor integration. We'd like you to add
support for Gemini 2.5 Flash on Google Vertex and report back on your findings.

**Setup.** You'll need the `gcloud` CLI installed and configured against our project,
which we will provide for you.

**What to build.** Implement Gemini 2.5 Flash as a provider in this system, and
demonstrably prove it will hold up at production scale. We care about both halves of
that sentence: a working integration *and* the evidence that it will not fall over
when we point real traffic at it.

How you structure the code is up to you — the existing providers are a reference, not
a template. If something about Gemini doesn't fit those patterns, deviate and tell us
why.

For the "prove it works at scale" half: design and run whatever load tests, harnesses,
or experiments you'd want to see before signing off on this for production. Show us
the numbers, the failure modes you uncovered, and the headroom (or lack thereof) you
found.

**Deliverables.** We're less interested in a "completed checklist" and more interested
in what you learned. In your write-up, we'd like to see:

- How the integration behaves under realistic load. Pick a workload, run it, and tell
  us what you observed.
- Anything you discovered about this model — quirks, failure modes, parameters that
  mattered, things that surprised you compared to other LLMs you've used.
- Decisions you made and the tradeoffs behind them. If you tried something that didn't
  work, that's worth including too.
- What you'd want to do next if this were going to production, and what you'd want to
  know before getting there.

</details>
