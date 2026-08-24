# Findings

> **Status.** Everything below marked *(validated)* was produced against the fake
> Vertex endpoint in `mock/`, which exercises the full HTTP path, the real SDK, and
> the real connection pool at zero cost. Sections marked *(pending credentials)* are
> designed and ready to run but need access to a real project. I have been explicit
> about which is which rather than presenting harness-validated numbers as vendor
> measurements.

---

## 1. The model retires in seven weeks

Gemini 2.5 Flash on Vertex is scheduled for retirement on **2026-10-16**, confirmed
against Google's published deprecation schedule. That is roughly seven weeks from
this write-up. The documented upgrade path is Gemini 3 Flash.

I am leading with this because it reframes the task. Nothing below changes — the
integration works and the evidence stands — but "add Gemini 2.5 Flash" is a
seven-week bet unless the abstraction makes the next swap cheap. So the real
deliverable is a provider layer where replacing the model is a config change and a
re-run of the same load suite, not an integration project. That is what I built:
the harness, the metrics, and the cost governor are all model-agnostic, and
`llm/pricing.py` is a lookup table rather than hardcoded arithmetic.

**What I would want to know:** whether Evertune has a migration window in mind, and
whether the intent is to standardize on Gemini 3 Flash directly instead.

---

## 2. Changes to the abstraction, and why

The existing `SimpleResponse` cannot represent things Gemini routinely does. Three
changes, each forced by a real behavior rather than taste:

**`answer` is now `str | None`.** Gemini returns HTTP 200 with no text when it stops
for `MAX_TOKENS` or when a safety filter fires. Typing it `str` puts a `None` into
code that believes it holds a string, and the failure surfaces somewhere unrelated.

**`output_tokens` includes thinking tokens.** Gemini reports `thoughtsTokenCount`
separately from `candidatesTokenCount` but bills both at the output rate. A provider
that reports only `candidatesTokenCount` under-reports spend, silently, in a way that
only shows up on the invoice. `visible_output_tokens` is available when you want just
the user-facing text.

**`finish_reason` is carried on the response.** Without it, a truncated fragment is
indistinguishable from a complete answer. For brand-mention counting a fragment is
worse than an error, because it looks like success and quietly skews the counts —
hence `is_usable`, which requires both text *and* `finish_reason == STOP`.

`parallelism()` keeps its signature but the value is now derived from the connection
pool rather than hardcoded. A static integer is the wrong shape for a service governed
by Dynamic Shared Quota with no published per-project ceiling; the honest answer is
whatever load testing measured, which is what `harness/` produces.

---

## 3. The connection pool is the ceiling, and it is invisible *(validated)*

Holding concurrency fixed at 64 and varying only the HTTP connection pool, against a
service answering in a flat 500 ms:

| Pool | Throughput | p50 latency | Predicted (`pool / 0.5 s`) |
|---|---|---|---|
| 8 | 15.4 rps | 4162 ms | 16 rps |
| 16 | 30.6 rps | 2176 ms | 32 rps |
| 64 | 110.5 rps | 519 ms | concurrency-bound |
| 128 | 108.0 rps | 526 ms | concurrency-bound |

Throughput tracks pool size exactly until the pool exceeds concurrency, then flattens.
The important part is the latency column: at pool=8 the client reports **4.2 seconds**
for a service responding in **500 ms**. That 8× inflation is queueing inside our own
process. Nothing in the vendor's response reveals it, and the obvious reading of
"p50 is 4 seconds" is to blame Vertex.

This is why `llm_pool_saturation_ratio` (in-flight ÷ pool size) is a first-class
metric. It turns the most commonly missed bottleneck in async LLM clients into a
number on a dashboard.

**Consequence for `parallelism()`:** any value above the pool size is a lie. The
provider derives one from the other so they cannot drift apart.

---

## 4. Thinking and output share one budget *(validated against the documented model)*

`thinking_budget` and `max_output_tokens` draw on a single allowance. Set the budget
at or above the cap and the model can spend the entire allowance reasoning, returning
`finish_reason=MAX_TOKENS`, no text, and a full bill.

This is reproduced as a test (`test_thinking_budget_at_cap_starves_the_answer`) and
modeled in the fake endpoint, where thinking demand is a function of question
difficulty rather than answer length — which is why a generous budget can consume
everything.

The SDK default is `thinking_budget=-1`, meaning dynamic and effectively unbounded.
For a short-answer brand-recommendation workload that is a live footgun: it is the
default, it costs output-rate tokens, and its failure mode is an empty response rather
than an error. The provider defaults to `0` and requires opting in.

**Pending credentials:** the cost and latency delta between `thinking_budget=0` and
`-1` on the real model, as a factorial across `max_output_tokens`. Experiment 5 in the
matrix is built and ready.

---

## 5. The SDK serializes one field in snake_case *(validated)*

Inside `generationConfig`, every field the SDK emits is camelCase — `maxOutputTokens`,
`temperature`, `stopSequences` — except the thinking budget, which goes out as
`thinkingConfig.thinking_budget`.

Captured off the wire:

```json
"generationConfig": {
  "temperature": 0.5,
  "maxOutputTokens": 4096,
  "thinkingConfig": { "thinking_budget": 256 }
}
```

I found this because my mock matched on `thinkingBudget` and silently ignored the
setting, which made a test fail in a confusing way. That is exactly the production
risk: anything between the client and Vertex that normalizes on camelCase — a
gateway, a proxy, a policy layer, a recording fixture — will drop the budget. The
model then thinks freely, and the symptom is truncated answers and unexplained cost
rather than an error.

Pinned by `test_sdk_serializes_thinking_budget_in_snake_case` so a future SDK release
that normalizes it fails loudly instead of silently changing behavior. The k6 harness
sends both spellings.

---

## 6. Why k6 as a control, and what it showed *(validated)*

The Python harness cannot tell you whether a plateau is the client or the vendor. So
k6 runs the same workload against the same endpoint from a runtime with no GIL, no
shared pool, and open-loop arrival-rate executors.

At 20 rps offered load with a 128-connection pool:

| | Subject (Python) | Control (k6) |
|---|---|---|
| p50 | 405.7 ms | 407 ms |
| p99 | 727.1 ms | 691.5 ms |
| pool saturation, peak | 8.6 % | — |
| event loop lag, peak | 2.14 ms | — |

They agree within noise. That is the *correct* result at this load, and it is the
point: it establishes that the two harnesses measure the same thing when there is
headroom, which is what makes divergence at high load meaningful rather than an
artifact of using two different tools.

The open-loop property matters independently. A closed-loop harness stops issuing
requests when the service slows, so its recorded latency understates what real
arrival traffic experiences — coordinated omission. k6's arrival-rate executors
dispatch on a wall-clock schedule regardless of completions. The Python harness
implements both modes and records `schedule_lag_ms` in open mode so the driver's own
slippage is visible rather than assumed away.

`dropped_iterations` is a hard threshold. If k6 cannot sustain the offered rate, the
run exits non-zero, because a generator-bound run's numbers are meaningless.

---

## 7. Cost control *(validated)*

Confirmed rates: **$0.30 / 1M input, $2.50 / 1M output**, thinking billed at the
output rate.

Four mechanisms, verified by deliberately trying to overspend:

- **Pre-flight refusal.** An estimate exceeding the ceiling stops the run before any
  request is sent. Verified: a 400-request run against a $0.02 ceiling refused to
  start.
- **Runtime breaker.** Verified: with an estimate tuned to pass, the run tripped at
  47/400 requests having spent $0.0241 against a $0.0200 ceiling. The $0.004 overshoot
  is in-flight requests that cannot be recalled, bounded by concurrency ×
  cost-per-request.
- **Actual accounting.** Spend comes from reported `usage_metadata`, including empty
  and failed responses, because Gemini bills for those too.
- **Reconciliation.** Estimated vs actual with error percentage, printed every run.
  Observed error on mock runs ranged −9.5 % to −12 % with a reasonable estimate.

The Grafana cost dashboard tracks burn rate and projected time-to-exhaustion, so a
long run can be stopped on judgment rather than discovered afterwards.

---

## 8. What I would do before production

**Measure, then set `parallelism()`.** Run experiments 2–4 against the real project to
find the knee, and treat the result as a measured operating point with a re-measure
cadence, not a constant.

**Confirm the quota model.** Dynamic Shared Quota publishes no per-project ceiling.
`gcloud alpha services quota list` on the real project would establish whether there is
a hard limit at all, and `usage_metadata.trafficType` (already parsed and recorded)
distinguishes on-demand from provisioned throughput per request.

**Characterize ADC token refresh.** Tokens live about an hour. A fleet refreshing on
the same schedule is a thundering-herd risk that is invisible while the refresh sits
inside the SDK. The k6 sidecar makes the boundary explicit; a soak long enough to
cross it would show whether it matters.

**Decide the truncation policy deliberately.** `is_usable` currently discards
truncated answers. For mention counting I believe a fragment is worse than a failure,
but that is a product decision and it belongs to whoever owns the downstream counts.

**Open questions I would want answered:**

1. Are logprobs load-bearing? `together.py` requests `logprobs=1` and never reads the
   result. Vertex supports `response_logprobs`/`logprobs` with quota caveats. If
   mention confidence matters, that changes the provider surface.
2. What is the real traffic shape — sustained batch, or bursty on-demand? It decides
   whether to optimize for the closed-loop or open-loop number.
3. Is the 2.5 Flash retirement already planned for, or should this target Gemini 3
   Flash directly?

---

## 9. What is not yet proven

I would rather be explicit than imply coverage I do not have. Against a real endpoint,
still unmeasured:

- actual Vertex throughput ceiling and where 429s begin
- real p99 under sustained load, and tail behavior at the knee
- true thinking-token cost and latency multiplier on real content
- safety-filter false-positive rate on competitor and brand prompts
- `temperature=0` determinism (reported elsewhere as non-deterministic; worth verifying)
- whether `global` and regional endpoints differ in absorbed load

The harness, metrics, dashboards, and cost governor for all of these are built and
validated end to end. They need credentials, not code.
