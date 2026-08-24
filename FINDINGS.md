# Findings

> **Status.** Findings are labelled by the evidence behind them.
>
> *(measured on real Gemini)* — live requests against the **Gemini Developer API**.
> Real model, real billing, real failure modes. Not Vertex: different quota pool and
> endpoint, so absolute throughput and latency are not capacity numbers for
> production. Model behaviour and token economics do transfer.
>
> *(validated)* — produced against the fake Vertex endpoint in `mock/`, which
> exercises the full HTTP path, the real SDK, and the real connection pool at zero
> cost. Used for mechanism proofs where the vendor is deliberately held constant.
>
> *(pending credentials)* — designed and ready, needs a real Vertex project.
>
> I have kept these separate rather than presenting harness numbers as vendor
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

## 2. Working within the provided contract

I treated `llm/llm.py` as an immutable contract. It is unchanged, byte for byte.

That was a deliberate call, and initially the wrong one — my first pass widened
`SimpleResponse` to add `finish_reason`, make `answer` nullable, and carry timing.
Every one of those changes was avoidable:

**Thinking-token accounting needs no contract change.** Gemini reports
`thoughtsTokenCount` separately from `candidatesTokenCount` but bills both at the
output rate. Computing `output_tokens` as visible + thinking keeps the *inherited*
field meaning "total billed output", which is what a caller reading only the base
contract already assumes. Reporting `candidatesTokenCount` alone is an undercount
that surfaces on the invoice rather than in the code.

**`answer` does not need to be nullable.** Gemini returns HTTP 200 with no text on
`MAX_TOKENS` and on safety blocks. Rather than widen the type and push a `None` into
callers that believe they hold a string, the provider raises `LLMEmptyResponseError`
or `LLMContentBlockedError`. A returned response always carries text, so `answer: str`
stays true.

**Only `finish_reason` genuinely had nowhere to go.** Without it, a truncated fragment
is indistinguishable from a complete answer — and for brand-mention counting a
fragment is worse than an error, because it looks like success and quietly skews the
counts.

So `GeminiResponse` (in `llm/response.py`) extends `LLM.SimpleResponse` additively:
every new field has a default, no inherited field changes type or meaning, and
`isinstance(response, LLM.SimpleResponse)` holds. Base-contract callers work
untouched; callers that know they are talking to Gemini get the metadata. This is
pinned by `test_base_contract_is_unmodified_and_honored`, which asserts the base
dataclass still has exactly its three original fields — so a future edit to
`llm/llm.py` fails the suite rather than passing silently.

`parallelism()` keeps its signature. The value is derived from the connection pool
rather than hardcoded, because a static integer is the wrong shape for a service
governed by Dynamic Shared Quota with no published per-project ceiling. The honest
answer is whatever load testing measured.

**What I would propose if I owned the interface:** promote `finish_reason` onto
`SimpleResponse`. Truncation is not Gemini-specific — Together exposes the same
concept as `choices[0].finish_reason`, and the existing provider silently discards
it. That is a change for the contract's owner to make deliberately, not one to take
unilaterally inside a vendor integration.

**Every file the exercise shipped is still at its original path.** Nothing was
renamed, moved, or restructured: `llm/llm.py` is byte-identical, `llm/together.py`
differs by one import line, and the original brief is preserved verbatim in
`README.md`. Additions live in new files and new directories. A reviewer can diff this
branch against the starting commit and see only additions plus one bug fix, which
keeps the review cheap and makes the integration easy to reason about.

The one edit to `llm/together.py` is a one-line bug fix, not a contract change: it did
`from llm import LLM` inside `llm/together.py`, a circular import that resolves only
by accident of import order. It is now `from .llm import LLM`.

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

## 4. Dynamic thinking costs 6.3x more and is the SDK default *(measured on real Gemini)*

These are live measurements against the Gemini Developer API, not the mock. See the
caveat at the end of this section about what does and does not transfer to Vertex.

`thinking_budget` and `max_output_tokens` draw on **one shared allowance**. The SDK
default is `thinking_budget=-1`, meaning dynamic and effectively unbounded. Fifteen
requests per configuration, identical brand-recommendation prompts, concurrency 3:

| `thinking_budget` | usable | throughput | p50 | p99 | billed output | thinking | cost |
|---|---|---|---|---|---|---|---|
| `0` (off) | **15/15** | 2.87 rps | **977 ms** | **1,507 ms** | 1,195 | 0 | **$0.0032** |
| `-1` (default) | **14/15** | 0.71 rps | 2,862 ms | 5,857 ms | 7,997 | 6,682 | $0.0202 |

Turning thinking off on this workload gave **6.3x lower cost, 2.9x better p50, 3.9x
better p99, and 4.1x the throughput** — and it eliminated the one unusable response.

**83.6% of billed output tokens were thinking** (6,682 of 7,997). Those tokens bill at
the output rate and produce no text the user ever sees. On a single-request probe the
split was starker still: 176 thinking tokens to produce 21 visible ones, for a
question whose answer was a five-brand list either way. The two answers were
substantively identical:

```
thinking_budget=0   ->  "iRobot (Roomba), Shark, Roborock, and Eufy."
thinking_budget=-1  ->  "iRobot (Roomba), Roborock, Eufy, Shark, and Ecovacs."
```

### The failure mode is silent, not loud

With dynamic thinking, **1 of 15 responses came back `finish_reason=MAX_TOKENS`** —
HTTP 200, a partial answer, billed in full. Forcing the collision makes it obvious.
With `thinking_budget=1024` against `max_output_tokens=128`:

```
finish_reason   MAX_TOKENS
billed output   124 tokens  (118 thinking, 6 visible)
answer          "iRobot (Roomba),"
```

**118 tokens of reasoning bought 6 tokens of answer**, and the answer is a fragment
ending in a comma. Nothing about that response is an error. It is a 200 with text in
it. A provider that returns `response.text` and moves on hands that fragment
downstream as a successful answer, and a brand-mention counter then records exactly
one brand from a question that asked for five.

This is why `finish_reason` is carried on `GeminiResponse` and why `is_usable`
requires `STOP` rather than merely non-empty text. In the run above it correctly
marked that response unusable.

### What this means for configuration

`thinking_budget=0` is the default in this provider, opt-in only. For a
short-answer extraction workload the reasoning is not buying accuracy — it is buying
latency and 6x the bill. A workload that genuinely needs reasoning should enable it
deliberately **and** raise `max_output_tokens` well above the thinking budget, because
the two share one allowance.

### Caveat on transferability

These numbers come from the **Gemini Developer API**, not Vertex: a different quota
pool, a different endpoint, and free-tier rate limits. The absolute throughput and
latency figures are **not** Vertex capacity numbers and should not be read as such.

What should transfer is the *relative* effect, because it is a property of the model
and its billing rather than of the serving tier: thinking tokens bill at the output
rate, they share the output allowance, and the default is unbounded. Confirming the
ratio on Vertex is experiment 5 in the matrix and is ready to run.

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

## 6. Load testing the integration, not the vendor *(validated)*

The brief asks for two things: a working integration, **and** evidence it will not
fall over when real traffic is pointed at it. The thing that has to survive that
traffic is *our code*. In production, requests arrive at us and we call Vertex.

So the integration is deployed as a service (`service/app.py`) and k6 drives it over
HTTP exactly as production traffic would. The same k6 script can bypass us and hit the
vendor directly, and the difference between those two runs is what our layer costs.

```
A:  k6  ->  service/app.py  ->  llm/gemini.py  ->  Vertex
B:  k6  ------------------------------------->  Vertex
                                            A - B = our overhead
```

An earlier version of this harness had the Python driver call the provider in-process
and compared that against k6 hitting the vendor. That measures the library, not the
service: the driver was both load generator and system under test, and nothing on the
receiving side — connection handling, admission, backpressure, framework cost — was
exercised at all. `harness/run.py` is still there and still useful, but it is now
scoped to what it actually models: an in-process batch client.

### Our layer costs about 2 ms

Same workload, same backend, 50 rps for 30 s:

| Path | p50 | p95 | p99 |
|---|---|---|---|
| direct to backend | 401.7 ms | 515.9 ms | 553.0 ms |
| through our service | 403.6 ms | 515.7 ms | 565.3 ms |
| **difference** | **+1.9 ms** | −0.2 ms | **+12.3 ms** |

On a request that takes ~400 ms, the integration adds roughly **0.5 % at p50**.

The service also reports its own decomposition per request, and it disagrees with the
k6 delta in an informative way: internal overhead is **0.20 ms p50**, while k6 sees
**1.9 ms**. The gap is the inbound HTTP hop itself — loopback transit, JSON parse,
framework dispatch — which the service cannot see from the inside. Both numbers are
worth having: the internal one localizes regressions, the k6 one is what a caller
actually experiences.

### An instrumentation bug that would have libeled our own code

The first version of the overhead metric computed `total - latency_ms`, where
`latency_ms` is the *final* attempt's vendor latency. On a retried request that
silently charged us for the failed attempts **and** for the deliberate backoff sleep
between them. With a 4% injected failure rate the histogram looked like this:

```
<= 0.0005s : 2027 requests   (93%)
<= 1.0s    :   +31
<= 2.0s    :   +90           <- dragged p99 to 1807 ms
```

p50 read 0.27 ms and p99 read **1807 ms** for a code path whose real cost is a
fraction of a millisecond. The number was not merely wrong, it pointed at the wrong
component: anyone reading that dashboard would have concluded the Python layer was
stalling for seconds and gone looking for a bug that did not exist.

The fix is to attribute all three costs separately. `upstream_total_ms` sums vendor
time across *every* attempt, `retry_backoff_ms` records deliberate sleep, and overhead
is what remains:

```
attempts=4  upstream=506.5ms  backoff=3785.8ms  ours=11.16ms   (total 4303ms)
attempts=1  upstream=467.4ms  backoff=   0.0ms  ours= 0.29ms
```

After the fix, under load with the same failure injection:

| | before | after |
|---|---|---|
| our overhead p50 | 0.27 ms | 0.25 ms |
| our overhead p99 | **1807 ms** | **0.50 ms** |
| retry backoff p99 | *(hidden inside overhead)* | 1518 ms |

The lesson generalizes: a latency decomposition that does not account for retries will
blame the component doing the retrying. The dashboard now stacks four separate layers
— vendor, retry backoff, admission queue, framework — and only the bottom two are code
we can make faster. `test_retried_request_attributes_time_correctly` asserts the
decomposition never claims more time than actually elapsed.

### It sheds load instead of collapsing

Capacity is `provider.parallelism()` = 102 concurrent. With a ~0.4 s backend, Little's
Law puts the ceiling near 255 rps. Measured:

| Offered | Served | 503s | p50 | p99 | our overhead p99 |
|---|---|---|---|---|---|
| 100 rps | 2,001 | 0 | 404 ms | 566 ms | 0.25 ms |
| 200 rps | 3,993 | 8 | 403 ms | 593 ms | 0.19 ms |
| 300 rps | 4,616 | 1,385 | 386 ms | 1,013 ms | 0.21 ms |
| 400 rps | 4,358 | 3,643 | 331 ms | 1,515 ms | 0.19 ms |

Sustained throughput plateaus near **230 rps served**, close to the predicted 255.
Past that the service returns 503 with `Retry-After` rather than queueing.

Three things matter in that table. **Our overhead never moves** — 0.19–0.25 ms p99
across a 4x range of offered load, so the service is not the thing degrading. **p50
falls** at 400 rps because shed requests return immediately and admitted ones are not
queued behind a growing backlog; that is backpressure working. And **zero dropped
iterations** throughout, so the generator kept up and these are real numbers rather
than rig artifacts.

The failure mode is deliberate: shedding early keeps a saturated service legible and
lets callers back off, where unbounded queueing would turn a throughput problem into a
latency problem and then into a memory problem.

### Direct-to-vendor still has a job

Run B is not redundant. It is the only way to answer "is this plateau us or them?"
without guessing, and it is open-loop by construction — k6's arrival-rate executors
dispatch on a wall clock regardless of completions, so a slowdown shows up as queue
growth rather than as a quietly reduced request rate. Closed-loop harnesses
systematically understate latency under saturation, which is the most common way a
load test flatters the system it is measuring.

## 6b. Adaptive concurrency: when it wins, and when it does not *(validated)*

`parallelism()` returning a constant assumes capacity is a property you can discover
once. Vertex governs Gemini with Dynamic Shared Quota, which has no published
per-project ceiling and moves with regional demand, so any constant is a guess with a
shelf life.

`llm/adaptive.py` replaces the guess with a controller. Two design points matter:

**Latency is the primary signal, not errors.** The obvious design is AIMD on 429s.
It fails here for a measured reason: Vertex frequently absorbs excess load by getting
*slower* rather than rejecting. The reference solution recorded zero 429s at 500
concurrent, only latency inflation. A controller watching error codes would see a
healthy service and keep climbing. So the controller compares a baseline of the
fastest recent round-trips against a short-term average, and reduces the limit when
that gradient degrades — before any error appears. Rejections then act as an override
triggering immediate multiplicative decrease.

**Gradient rather than plain additive increase.** Adding one permit per success needs
on the order of a thousand successes to climb from 16 to 64, which is why an earlier
attempt at this could not show a benefit inside a short run. Multiplying toward the
estimated capacity plus a `sqrt(limit)` allowance reaches a new operating point in
tens of requests. `test_healthy_traffic_grows_the_limit_quickly` pins that.

### The experiment

Three configurations against a backend whose capacity collapses mid-run and then
recovers — healthy, degraded, healthy — with the driver offering 96 concurrent slots
throughout:

| | healthy rps | degraded rps | degraded p50 | total ok | total errors | worst p99 |
|---|---|---|---|---|---|---|
| fixed-high (64) | **359.8** | 16.7 | 3,004 ms | **8,730** | **1,531** | 6,569 ms |
| fixed-low (8) | 50.6 | 37.2 | 2,418 ms | 1,656 | 10 | 3,490 ms |
| adaptive | 176.5 | 26.0 | **166 ms** | 4,781 | 45 | **2,867 ms** |

**The fixed-high cap suffers congestion collapse.** When capacity dropped it kept
pushing 64 concurrent into a backend that could take about 8. Throughput fell to
16.7 rps — *below* the cap tuned for the bad case — and it produced 1,431 errors in a
single phase. Pushing harder made it slower.

**The fixed-low cap is safe and wasteful.** It sailed through degradation, and gave
up roughly 7x the available throughput the rest of the time. This is what tuning for
the worst case costs when the worst case is rare.

**Adaptive tracked capacity**, settling near 46 when healthy and 12 when degraded,
then climbing back to 45 on recovery. Its degraded p50 of 166 ms against 2,418 and
3,004 ms is the clearest result in the table: by holding the limit near real capacity
it kept requests out of a queue entirely, so admitted work stayed fast while the
excess was shed cleanly.

**On reproducibility.** A second run (committed as
`results/real/adaptive-experiment.txt`) gave 8,755 / 1,569 errors for fixed-high
against 4,993 / 59 for adaptive — the same shape. The error-rate gap is the robust
finding and reproduces at roughly 30x. The worst-case p99 is noisier: adaptive had the
best tail in the first run (2,867 ms) and the worst in the second (4,207 ms against
fixed-low's 3,978 ms). I would not claim a tail-latency win on two runs. The claims
worth standing behind are the error rate, the degraded-phase p50, and the throughput
recovered relative to a conservative fixed cap.

### Where adaptive loses

It does not win everywhere, and the table shows it. **In the healthy phase it managed
176 rps against fixed-high's 360.** The controller is deliberately conservative — it
will not grow on load it has not seen, and it smooths changes — so it leaves real
throughput unclaimed when conditions are good.

So the trade is roughly half the peak throughput for a ~30x reduction in errors.
Whether that is right depends on what a failure costs. For
brand-mention extraction, a failed request has to be retried anyway, so 1,531 errors
is not 1,531 saved requests — it is requests paid for twice plus operational noise. On
that reading adaptive is the better default. For a workload where dropped work is
genuinely free, fixed-high is defensible.

The honest summary: **against constant capacity a well-tuned fixed limit is fine, and
this is not worth the machinery.** It earns its place when capacity moves, which is
the situation Dynamic Shared Quota creates by definition.

### Caveat on the shed counts

The adaptive rows show very large shed counts. That is an artifact of the experiment's
driver, which retries immediately after a 10 ms sleep and therefore spins against a
closed gate. In the service a shed request is one 503 with `Retry-After`, not a spin
loop. The shed counts should be read as "the gate was closed a lot during degradation",
not as a per-request cost.

Adaptive limiting is **off by default** (`GEMINI_ADAPTIVE=true` to enable), because a
fixed limit is easier to reason about and the case for switching should be made with
measurements from the real backend rather than assumed.

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
