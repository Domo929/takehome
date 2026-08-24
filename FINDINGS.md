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

## 0. Two different Google endpoints serve this model

Worth stating plainly, because the distinction decides which numbers transfer and
which do not, and "the Google API" is ambiguous between them.

| | Gemini Developer API | Vertex AI |
|---|---|---|
| Endpoint | `generativelanguage.googleapis.com` | `aiplatform.googleapis.com` |
| Auth | API key | ADC / service account + a GCP project |
| Quota | **Fixed and published per tier**, enforced with 429s | **Dynamic Shared Quota** — not published, varies by region, load and time |
| Capacity guarantees | none | optional Provisioned Throughput |
| Measured here | yes — early findings | yes — project `evertune-tests` |

Both speak the same model and the same `generateContent` contract, which is why one
provider can target either (`GEMINI_BACKEND=developer|vertex`). They are not
interchangeable for capacity work: different quota pools, different endpoints,
different scaling behaviour.

**Both tiers have now been measured.** Early findings were taken on the Developer API
before Vertex credentials were available; the headline experiments have since been
repeated against Vertex (project `evertune-tests`) and the differences were material
enough to correct published numbers — see §4.

The distinction matters in the direction the caveat predicted. Model behaviour —
token economics, thinking mechanics, finish reasons, payload validation — transfers
cleanly. Latency and capacity do not: on identical requests Vertex was **1.36x slower
at p50 and 1.74x slower at p99**, and the thinking cost ratio moved from 6.8x to 4.1x.
Any number in this document that describes performance names its tier.

### What our own key allows

Probing the key used for these measurements: **200 concurrent requests, 200 successes,
zero 429s, ~31 rps sustained.** I stopped there rather than hunting the ceiling, since
it is a personal key with a daily cap. That is well above the free tier (single-digit
RPM), so the measurements here were not distorted by throttling — but it also means
this key's ceiling is unknown, and no throughput claim in this document rests on it.

---

## 0b. The workload, confirmed

These came from Evertune rather than from me. The second one changed the architecture
recommendation, so it is worth stating precisely.

| | Confirmed |
|---|---|
| Real-time responses | Not a concern |
| **Ad-hoc** | New reports are created throughout the day and **kicked off right away** |
| **Scheduled** | Existing reports refresh monthly, weekly or daily per configuration |
| Sampling | Each prompt runs **100 times** |
| Downstream | Not in scope for this exercise; notes welcome |
| Model | 2.5 Flash is fine, no migration pressure |

### This is two workloads, not one

I had assumed "batch" and optimized accordingly. The confirmation says something more
specific: there is a **predictable scheduled tier** and an **ad-hoc tier where someone
just clicked a button and is waiting**. Those want different treatment, and conflating
them leaves either money or responsiveness on the table.

**The ad-hoc tier is a burst problem.** One new report is 100 prompts x 100 runs =
**10,000 requests arriving at once**. At the measured 35.6 rps sustained (§6f):

| Report size | Requests | Time to complete | Cost |
|---|---|---|---|
| 50 prompts | 5,000 | 2.3 min | $1.44 |
| **100 prompts** | **10,000** | **4.7 min** | **$2.88** |
| 200 prompts | 20,000 | 9.4 min | $5.77 |

So a new report takes about five minutes to populate, and two reports created in the
same minute contend for the same quota. That is the scenario the admission control and
the retry budget in this repo actually exist for — not steady-state load, which as §0b
showed is trivially served.

**The scheduled tier is where the money is.** It is predictable, it has no one waiting,
and it therefore qualifies for the Batch API's ~50% discount. Modelling 200 reports on
a mixed cadence (20% daily, 50% weekly, 30% monthly):

| Reports | Scheduled requests/day | All interactive | Scheduled via Batch | Saved |
|---|---|---|---|---|
| 50 | 140,714 | $14,809/yr | $7,405/yr | $7,404 |
| **200** | **562,857** | **$59,237/yr** | **$29,619/yr** | **$29,619** |
| 1,000 | 2,814,286 | $296,187/yr | $148,093/yr | $148,094 |

### Recommendation: route by tier

```
new report created  ->  interactive path  ->  optimize for time-to-first-report
scheduled refresh   ->  Batch API         ->  optimize for cost, ~50% cheaper
```

The provider is already indifferent to which path calls it. What the split needs is a
queue and a scheduler, which is a change to the calling layer rather than to the
integration.

### Throughput is still not the constraint, but bursts are

At 200 reports the scheduled tier is ~563,000 requests/day, which at 35.6 rps is
**4.4 hours of wall clock**. That fits in an overnight window comfortably, and Batch
removes the question entirely by making turnaround someone else's problem.

The ad-hoc tier is different: it is not throughput-limited in aggregate, it is
*latency*-limited per report. Making a new report appear faster means more concurrency
against a shared quota — which is exactly where §6f's ceiling and §6b's limiter matter.

---

## 1. The model retires 2026-10-16 — noted, not blocking

Gemini 2.5 Flash on Vertex is scheduled for retirement on **2026-10-16**, confirmed
against Google's published deprecation schedule. Evertune has confirmed 2.5 is fine for
this exercise, so this is recorded as a risk rather than treated as a blocker.

It is worth one line in a runbook rather than a redesign. The provider takes the model
from configuration, `llm/pricing.py` is a lookup table rather than hardcoded
arithmetic, and the entire load suite re-runs unchanged against a different model. The
migration cost here is a config change plus a re-run of §4 and §6f to confirm the
economics carry over, which is roughly an afternoon.

The thing I would actually check before migrating is whether the thinking-token
behaviour in §4 holds on Gemini 3 Flash. That is the finding most likely to be
model-specific, and it is worth about 4x on the bill.

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

`parallelism()` keeps its signature. It now returns **64**, which is the only
concurrency validated under sustained load (§6f) rather than a value inferred from
pool arithmetic, and it stays capped by the connection pool because a limit above the
pool queues on sockets instead of at the admission gate.

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

## 4. Dynamic thinking costs 4.1x more on Vertex, and is the SDK default *(measured on Vertex and the Developer API)*

Measured on **both** serving tiers: Vertex AI (`evertune-tests`, the production
target) and the Gemini Developer API. Fifteen requests per configuration, identical
brand-recommendation prompts, concurrency 3.

`thinking_budget` and `max_output_tokens` draw on **one shared allowance**. The SDK
default is `thinking_budget=-1`, meaning dynamic and effectively unbounded.

| Tier | `thinking_budget` | usable | rps | p50 | p99 | out tok/req | thinking | $/req |
|---|---|---|---|---|---|---|---|---|
| **Vertex us-central1** | `0` (off) | **15/15** | 1.03 | **1,471 ms** | 9,189 ms | 111.1 | 0 | **0.000288** |
| **Vertex us-central1** | `-1` (default) | 15/15 | 0.64 | 4,106 ms | 7,852 ms | 458.3 | 368.6 | 0.001156 |
| Vertex global | `0` (off) | 15/15 | 1.79 | 1,329 ms | 2,616 ms | 108.9 | 0 | 0.000283 |
| Vertex global | `-1` (default) | 15/15 | 0.70 | 3,339 ms | 5,991 ms | 460.6 | 368.5 | 0.001162 |
| Developer | `0` (off) | 15/15 | 2.87 | 976 ms | 1,507 ms | 79.7 | 0 | 0.000210 |
| Developer | `-1` (default) | 14/15 | 0.70 | 2,862 ms | 5,856 ms | 571.2 | 477.3 | 0.001440 |

**In us-central1, turning thinking off gave 4.0x lower cost and 2.8x better p50.**
Thinking was **80.4% of billed output tokens** — tokens that bill at the output rate
and produce no text anyone reads. The ratio is stable across regions (4.1x in
`global`), which is what makes it a usable planning number.

### Correcting an earlier claim

An earlier version of this document reported **6.3x**, measured on the Developer API
alone. On Vertex the same experiment gives **4.0-4.1x**. Both are real; the ratio is
not a constant of the model. The Developer API happened to produce longer thinking traces
(477 vs 369 tokens per request) and shorter answers (80 vs 109 visible tokens), which
widens the gap.

The direction and the order of magnitude hold on both tiers. The precise multiplier
does not, and quoting a single figure without naming the tier would have been wrong.
This is exactly the transferability caveat from §0 turning out to matter in practice.

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

### Region and tier both change the latency, but not the economics

Evertune runs in **us-central1**, so that is now the provider default. `global` was
used for the first Vertex measurements, which prompted a re-run to check whether the
choice mattered. It does, but not where it counts.

Same project, same prompts, same configuration:

| Config | Region / tier | p50 | rps | out tok/req | $/req |
|---|---|---|---|---|---|
| thinking off | Developer API | 976 ms | 2.87 | 79.7 | 0.000210 |
| thinking off | Vertex `global` | 1,329 ms | 1.79 | 108.9 | 0.000283 |
| thinking off | **Vertex `us-central1`** | **1,471 ms** | **1.03** | **111.1** | **0.000288** |
| dynamic thinking | Vertex `global` | 3,339 ms | 0.70 | 460.6 | 0.001162 |
| dynamic thinking | **Vertex `us-central1`** | **4,106 ms** | **0.64** | **458.3** | **0.001156** |

Two conclusions, with different confidence levels.

**Cost is portable; latency is not.** Per-request cost differs by at most 2% across
regions, because token counts barely move (111.1 vs 108.9 output tokens). The
thinking-budget ratio is 4.0x in us-central1 against 4.1x in global. So the economic
findings in §6c transfer, which is the more useful half.

Latency does not transfer. us-central1 was 1.11x slower at p50 with thinking off and
1.23x slower with it on, and sustained throughput was **0.58x** — meaningfully worse
for the same offered concurrency. Combined with the Developer API being faster still,
the same request has a p50 ranging from 976 ms to 4,106 ms depending purely on which
tier and region it lands in.

**A caution on the tails.** The p99 figures at n=15 are effectively "the slowest of
fifteen requests", which is not a p99 in any meaningful sense. One us-central1 cell
showed a 3.51x p99 ratio; I do not believe that number and would not report it as a
finding. Characterizing tails properly needs hundreds of samples per cell, which is
listed in §9 rather than claimed here.

The practical consequence is simply that **any latency figure must name its tier and
region**, and a benchmark that omits them is not comparable to one that does.

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
once. **On Vertex that assumption fails**: Gemini there is governed by Dynamic Shared
Quota, which publishes no per-project ceiling and moves with regional demand, so any
constant is a guess with a shelf life.

Note the asymmetry with the Developer API (§0), where quota *is* fixed and published
per tier. Adaptive limiting is therefore a Vertex-motivated feature. Against a fixed,
knowable limit a tuned constant is the simpler and better answer, which is part of why
this is off by default.

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
what Dynamic Shared Quota means — and that is a Vertex property, not a Gemini one. On
the Developer API, where limits are published per tier, I would not enable this.

One caveat on the evidence: the capacity change here was *simulated* by reconfiguring
the fake backend. It is a fair test of the controller's dynamics, but it is not
evidence that Vertex's quota actually moves on the timescale modelled. Confirming that
needs a real project, and would change the recommendation if it turned out DSQ is
stable in practice for a single tenant.

### Caveat on the shed counts

The adaptive rows show very large shed counts. That is an artifact of the experiment's
driver, which retries immediately after a 10 ms sleep and therefore spins against a
closed gate. In the service a shed request is one 503 with `Retry-After`, not a spin
loop. The shed counts should be read as "the gate was closed a lot during degradation",
not as a per-request cost.

Adaptive limiting is **off by default** (`GEMINI_ADAPTIVE=true` to enable), because a
fixed limit is easier to reason about and the case for switching should be made with
measurements from the real backend rather than assumed.

## 6c. Cost at the stated workload: an 8.2x spread *(measured tokens, us-central1)*

Given batch semantics and thousands of prompts per day, the levers that matter reduce
cost per request. Token counts are measured on **Vertex us-central1** from
`results/real/`: 35.3 input / 111.1 output with thinking off, and 35.3 / 458.3 with
dynamic thinking.

| Configuration | $/request | vs naive |
|---|---|---|
| interactive, dynamic thinking (the defaults) | 0.00115634 | 1.0x |
| thinking off | 0.00028834 | 4.0x |
| thinking off + context caching | 0.00028159 | 4.1x |
| thinking off + Batch API | 0.00014417 | 8.0x |
| **thinking off + Batch + caching** | **0.00014117** | **8.2x** |

At 50,000 prompts/day that is **$21,103/year against $2,576/year** — the same work,
the same model, for 12% of the bill.

Three levers, in order of size:

**Thinking off (4.0x).** Measured on us-central1, §4. Output tokens are ~8x the price of
input and, with dynamic thinking, ~4x the volume, so this is where the money is.

**Batch API (2x), on the scheduled tier only.** Vertex bills batch prediction at
roughly half the interactive rate in exchange for asynchronous turnaround. Per §0b the
workload splits: scheduled refreshes have nobody waiting and qualify; ad-hoc reports
are kicked off on creation and do not. Applying it only where it fits is worth about
**$29,600/year at 200 reports** — and getting that split right is the single
highest-value decision in this document. Had I kept assuming a uniform batch workload,
I would have recommended Batch for the ad-hoc path too and made new reports take up to
a day to appear.

**Context caching (~1.02x here).** Every request in a sweep carries the same system
prompt, and cache hits bill input at a fraction of the normal rate. The effect is small
because inputs are tiny — about 35 tokens. It would matter considerably more if the
system prompt grew to include brand lists or few-shot examples, which is a plausible
direction. Implicit caching is on by default for Gemini 2.5, so the discount may
already be arriving unrequested; the provider reads `cached_content_token_count` so
the model reflects it rather than overstating spend.

Reproduce with `python scripts/cost_model.py --daily 50000`.

**What this does not model:** batch pricing is quoted from Google's published rates
rather than measured. The relative ordering is robust; the absolute figures should be
confirmed against a real invoice.

**Earlier figures corrected twice.** A first version reported 14.1x on Developer API
token counts; rebasing on Vertex `global` gave 8.4x, and on us-central1 it is 8.2x.
Vertex produces longer answers and shorter thinking traces than the Developer API,
which narrows the gap. The two Vertex regions agree within 2%, so the remaining
uncertainty is between tiers, not between regions.

## 6d. Where the service actually saturates *(validated)*

Even though throughput is not the binding constraint, it is worth knowing where the
ceiling is. Backend latency was pinned low so the ceiling would be *our process*
rather than the vendor, capacity raised to 600 and the pool to 800:

| Offered | Served | Shed | p50 | p99 | Verdict |
|---|---|---|---|---|---|
| 100 rps | 99 | 0 | 57 ms | 316 ms | clean |
| 250 rps | **250** | 0 | 57 ms | 431 ms | **clean, at the knee** |
| 500 rps | 205 | 295 | 66 ms | 9,811 ms | saturated |
| 1,000 rps | 279 | 721 | 17 ms | 7,516 ms | saturated |
| 1,500 rps | 155 | 1,345 | 40 ms | 9,969 ms | collapsing |

**A single Python worker serves ~250 rps cleanly**, then degrades: at 1,500 offered it
manages 155 rps, *less* than at 250. Pushing harder makes it slower.

The instrumentation identifies the culprit unambiguously:

- **event loop lag peaked at 179.9 ms** — the process cannot schedule work fast enough
- **pool saturation only reached 75%** — the connection pool is *not* the constraint
- **our per-request overhead stayed at 0.5 ms p99** — the code path is not slow

So the ceiling is the interpreter, not the client design: one event loop, one GIL. The
scaling path is more worker processes, at the cost of one connection pool each and
therefore a per-worker rather than global concurrency limit.

For the stated workload this is academic — 250 rps is roughly 400x the average demand
of a 50,000-prompt day. It is included because "we measured where it breaks and why"
is a different claim from "we never got near the limit".

## 6e. Logprobs are free, and they see brands that sampling cannot *(measured)*

Evertune already runs each prompt 100 times, so this is not a proposal to sample less.
The question is narrower: **given 100 samples we are taking anyway, what do logprobs
add that counting cannot see?**

`together.py` requests `logprobs=1` and never reads the result, which suggested the
concept matters to this system even if the current implementation discards it.

### They cost nothing

Ten requests each way against `evertune-tests`/us-central1, identical prompts:

| | input tokens | output tokens | total |
|---|---|---|---|
| logprobs off | 26.0 | 2.0 | 28.0 |
| logprobs on (`logprobs=5`) | 26.0 | 2.0 | 28.0 |

**Identical.** Logprobs are returned as response metadata and are not billed as output
tokens. There is no token surcharge for turning them on.

Two related observations. `avg_logprobs` is populated on every response **even with
`response_logprobs` unset**, so a coarse per-response confidence score is already
available for free today. And the observed latency was lower with logprobs on (354 ms
against 1,235 ms p50) — I do not believe that is causal, it is almost certainly warm-up
ordering since the off-case ran first, and I would not report it as a finding.

### What 100 samples miss

One prompt constrained to a single brand name, 100 samples at temperature 1.0,
`logprobs=5`, total cost **$0.00146**:

**What counting sees — two brands:**

| Brand | Count |
|---|---|
| iRobot | 97 / 100 |
| Roomba | 3 / 100 |

**What logprobs additionally see at the same branch point:**

| Token | Mean P | In samples? |
|---|---|---|
| `'i'` (iRobot) | 0.9308 | yes |
| `'Room'` (Roomba) | 0.0470 | yes |
| **`'Rob'` (Roborock)** | **0.0183** | **no — 0/100** |
| **`'Shark'`** | **0.0023** | **no — 0/100** |
| `'E'` (Eufy?) | 0.0006 | no |
| `'D'` (Dreame?) | 0.0004 | no |

**Roborock — a major brand in this category — appeared in zero of 100 samples while
carrying 1.83% of the probability mass at the decision point.** Counting reports it as
absent, indistinguishable from a brand the model has never heard of. Those are very
different findings for a brand-tracking product, and only one of them is true.

### The sample sizes counting would need

| Token | P | Chance of 0 hits in 100 | Samples for a ±20% estimate | Cost per prompt |
|---|---|---|---|---|
| `'Rob'` | 0.0183 | 15.8% | 1,341 | $0.39 |
| `'Shark'` | 0.0023 | 79.5% | 10,906 | $3.14 |
| `'E'` | 0.0006 | 94.5% | 43,778 | $12.62 |

To resolve Shark by counting you would need roughly **10,900 samples per prompt**, at
about $3.14 each. Logprobs surfaced it inside the 100 samples already being taken, for
no additional token cost. At 100 prompts per company per day that difference is the
gap between a viable measurement and an impossible one.

### What this is worth

Three capabilities that counting structurally cannot provide, at zero token cost:

1. **Near-miss detection.** "Your brand is considered 1.8% of the time but never
   surfaces" is a different and arguably more actionable finding than "your brand does
   not appear". It is invisible to any amount of counting below ~1,300 samples.
2. **Resolution below the counting floor.** With n=100 the floor is 1%; anything rarer
   reads as zero. Three of the six tokens observed sat below it.
3. **Per-prompt confidence.** Branch-point entropy averaged 0.303 nats here, which is
   low — the model is confident and 100 samples is comfortably enough. A flatter
   distribution would signal that this particular prompt is under-sampled, which is a
   per-prompt judgement that a fixed n=100 cannot make.

### Caveats

The prompt was constrained to a single brand name so the first token is an unambiguous
branch point. Real prompts return prose, where brand names are multi-token, appear at
varying positions, and are subject to ordering effects — extracting the same signal
there is harder and is not demonstrated here. This experiment establishes that the
information exists and is free; it does not establish that a production extractor is
easy to write.

Whether this is worth building depends on a question only Evertune can answer: **does
"almost recommended" matter to the product?** If brand share is the deliverable,
logprobs are a free precision upgrade. If near-miss visibility is a feature customers
would pay for, it is a new capability rather than an optimization.

## 6f. Sustained load: what an 8-second test cannot tell you *(measured)*

This section replaces an earlier version of itself, and the reason is the finding.

My first capacity tests ran for **8.5 seconds**. Vertex enforces quota **per minute**,
so a sub-minute run cannot trigger a per-minute ceiling no matter how hard it pushes.
Reporting "no rate limiting observed" from an 8-second burst was not a weak result, it
was an invalid one. The harness now supports sustained runs with 30-second time-series
windows.

### The sustained run

`evertune-tests`/us-central1, concurrency 64, thinking off, `max_output_tokens=512`,
stopped by the budget breaker at $8:

| | |
|---|---|
| Duration | **8.7 minutes** (523 s) |
| Requests | **19,223** (18,581 usable) |
| Sustained throughput | **35.6 rps** = 2,136 requests/minute, peaking at 2,362 |
| p50 / p90 / p99 | 1,401 / 3,038 / 7,033 ms |
| Rate-limit responses | **8** (0.042%), all recovered by retry |
| Truncated (`MAX_TOKENS`) | 642 (3.3%) |

![Sustained load on Vertex](docs/evidence/soak-evidence.png)

### Short tests understated throughput by 2.5x

| Test | Duration | Concurrency | Throughput |
|---|---|---|---|
| burst | 8.3 s | 32 | 15.4 rps |
| burst | 8.5 s | 128 | 14.2 rps |
| **sustained** | **523 s** | **64** | **35.6 rps** |

The 8-second tests measured connection establishment, not steady state: TLS handshakes
and cold pool slots dominate a burst that short. Every capacity number I reported
before this run was wrong by roughly a factor of two and a half, in the conservative
direction.

### Vertex does rate limit — and hand-rolled retries are why we can see it

Eight requests received rate-limit responses. **Not one surfaced to a caller**: the
retry engine absorbed all of them, and the aggregate error count is zero.

This is the payoff for a decision made early and on principle. `llm/retry.py` does
retries in our own code rather than delegating to `HttpRetryOptions` in the SDK,
specifically so that retried failures remain visible to instrumentation. Had SDK
retries been enabled, these eight would have been invisible — and the conclusion would
have been the flattering, false "Vertex never rate limits us". The `llm_retry_attempts_total`
series is the only place this appears.

The honest headline: **Vertex rate limits at roughly 0.04% at 2,100 requests/minute**,
which is a real ceiling being brushed rather than a ceiling being hit.

### The ceiling is server-side, and that is now proven rather than assumed

Throughput plateaued near 35 rps at concurrency 64. Little's Law says 64 concurrent at
1.4 s should yield ~45 rps, so something limits us. The instrumentation says it is not
us:

| Signal | Peak during run | Reading |
|---|---|---|
| Connection pool saturation | **25%** | pool is not the constraint |
| Event loop lag | **4.7 ms** | the Python process is keeping up |
| Our per-request overhead | 0.5 ms p99 (§6d) | the code path is not slow |

With the client demonstrably idle, the remaining explanation is Vertex. This is the
distinction the whole subject-versus-control design exists to make, and here the
client-side signals settle it without needing the control.

### Stability over time, and a real tail trend

Splitting the run in half:

| | First half | Second half | Change |
|---|---|---|---|
| Throughput | 37.7 rps | 35.3 rps | **−6.2%** |
| p50 | 1,387 ms | 1,424 ms | +2.7% |
| **p99** | **5,106 ms** | **6,996 ms** | **+37.0%** |

Throughput and p50 are essentially flat — 5.1% coefficient of variation across
eighteen windows, and a −0.26 rps-per-window trend that is within noise. p99 rose 37%
over the same period, which looked like a queue forming upstream.

**It was not.** A longer run refutes this; see below. I have left the observation in
place rather than quietly deleting it, because the mistake is the useful part.

### The p99 growth was not real — a 21-minute run refutes the 8.7-minute one

The shorter run showed p99 rising 37% while p50 stayed flat, and I read that as a
queue forming upstream. It seemed like the most interesting result in the section.

It does not replicate. A second run at identical settings, stopped by a $20 breaker
after **20.8 minutes and 47,677 requests**:

| Quarter | Throughput | p50 | p99 |
|---|---|---|---|
| Q1 (0–5 min) | 38.1 rps | 1,376 ms | 4,934 ms |
| Q2 (5–10 min) | 38.7 rps | 1,346 ms | 4,758 ms |
| Q3 (10–16 min) | 34.4 rps | 1,429 ms | **6,574 ms** |
| Q4 (16–21 min) | 35.4 rps | 1,395 ms | **4,966 ms** |

p99 climbs into Q3 and then **comes back down**. Across 42 windows it oscillates
between 3,749 ms and 9,561 ms — a 2.55x range — with a linear trend of +20.8 ms per
window against a standard deviation of 1,798 ms. The trend is a rounding error next to
the noise.

The decisive comparison: the largest swing between adjacent 4-window blocks *within
this run* is **+81%**. My earlier "+37% trend" is comfortably inside the normal
oscillation of this workload. I was fitting a line to a wave and reporting the slope.

**What I actually had was a sampling artifact**, and 8.7 minutes was not long enough to
see it. That is the same lesson as the 8-second tests, one order of magnitude up: the
first mistake was measuring for less time than the quota window, and this one was
measuring for less time than the tail's own period.

### What the tail actually is: transient vendor events, not a queue

The dashboard makes the mechanism obvious in a way the aggregate does not. p99 sits
near 4.7 s for most of the run, then spikes to 9.5 s between roughly t+750 s and
t+900 s, then returns. **The retry series spikes over exactly the same window**, and
throughput dips from ~37 rps to ~28 rps.

Splitting the 42 windows by whether any retry occurred:

| | Windows | Mean p99 |
|---|---|---|
| With retries | 11 | **6,910 ms** |
| Without retries | 31 | **4,717 ms** |

A 47% difference in tail latency, cleanly separated by whether Vertex was pushing back
in that window. So the tail is not a queue growing inside our process — it is **Vertex
having transient degradation episodes lasting a couple of minutes**, during which
latency inflates, a handful of requests are rate-limited or 5xx, and the retry engine
absorbs them.

That reframes the operational advice. The tail is not something to tune away; it is a
property of shared quota. What matters is that the client survives those episodes,
which it does: 47,677 requests, 24 retries, **zero failures reaching a caller**.

### What held up across both runs

The metrics that were stable are stable in both, which is what makes the retraction
credible rather than merely convenient:

| | 8.7-min run | 20.8-min run |
|---|---|---|
| Requests | 19,223 | **47,677** |
| Sustained throughput | 35.6 rps | 36.9 rps |
| p50 | 1,401 ms | 1,379 ms |
| Rate-limit rate | 0.042% (8) | **0.038% (18)** |
| Truncation at 512 cap | 3.3% | **3.3%** |
| Pool saturation peak | 25% | 25% |
| Event loop lag peak | 4.7 ms | <5 ms |

Two independent runs agreeing to three significant figures on truncation and to within
10% on rate-limit rate is the part I would stake a production decision on. Throughput
of ~37 rps at concurrency 64, p50 ~1.4 s, and a rate-limit rate under 0.05% at ~2,200
requests/minute are settled.

**p99 is not settled and should be quoted as a range**, roughly 3.7–9.6 s, rather than
as a single number. For a batch workload that is acceptable; for anything with a tail
SLA it would need provisioned throughput rather than shared quota.

### What this still does not settle

**Dynamic Shared Quota volatility.** Two runs twelve minutes apart is not a test of
capacity moving across hours or across a business-hours boundary, which is the actual
justification for the adaptive limiter in §6b. Both runs saw the same ceiling, which is
weak evidence *against* volatility on this timescale.

Given that, the honest recommendation is: **use a fixed limit of 64 and treat the
adaptive limiter as optional.** It is off by default, it is tested, and the case for
enabling it rests on evidence I have not gathered. Shipping less machinery is the right
default when the justification is unproven; the code is there if a multi-hour run later
shows capacity moving, and removing it is a one-line change if it does not.

**On the value 64 specifically.** An earlier version of this document put the operating
point at 32, taken from a sweep whose stages ran 8 seconds each. Those numbers are now
known to be unreliable for the same reason the rest of that sweep was: a short burst
measures connection warm-up, not steady state. Every level in that sweep except 64
lacks a sustained measurement, so the "knee at 32" was an artifact of the shortest
tests in the set. What I can defend is that **67,000 requests across 30 minutes held
concurrency 64 at 36–37 rps** with a 0.04% rate-limit rate and no failures reaching a
caller.

What I have *not* established is whether 64 is optimal or merely sufficient. A
sustained sweep at 32, 64 and 128 would settle it; at several minutes per level that
is roughly $60 of vendor spend, which is hard to justify against a workload averaging
6 rps (§0b).

### A confounder I created and removed

The first sweep used `max_output_tokens=256` and reported a ~20% "error" rate that was
truncation, not failure. Raising the cap to 512 dropped it to 3.6%, and the sustained
run confirms 3.3% at 512:

| `max_output_tokens` | Truncated |
|---|---|
| 256 | **20.0%** |
| 512 | 3.6% |
| 512 (sustained, n=19,223) | **3.3%** |

**A 256-token cap silently truncates one in five brand-recommendation answers**, each
a billed HTTP 200 that only `finish_reason` distinguishes from a complete one.

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

Ordered by what would actually change an outcome.

**1. Implement the Batch API for the scheduled tier.** Per §0b this is the largest
remaining lever — roughly **$29,600/year at 200 reports** — and it now has a clearly
bounded home: scheduled refreshes only, never the ad-hoc path. It is a different API
surface rather than a config flag, so it is real work, but the payoff is unambiguous.

**2. Split the two tiers in the calling layer.** The provider is already indifferent to
which path calls it. What is missing is a queue and a scheduler that routes new-report
bursts to the interactive path and refreshes to batch. Without that split, either new
reports get slow or scheduled work costs double.

**3. Decide the truncation policy deliberately.** §6f measured **3.3% truncation** at a
512-token cap and **20% at 256**. `is_usable` currently discards truncated answers,
which for mention-counting I believe is right — a fragment silently skews counts, and
that is worse than a visible failure. But it is a product decision, and at 3.3% of
tens of thousands of requests it is not a rounding error.

**4. Run a quality evaluation.** The 4.0x cost case for `thinking_budget=0` is measured;
the claim that answers are equivalent is n=1 eyeballing. This is the largest unverified
assumption behind the main recommendation.

**5. Characterize ADC token refresh across an hour boundary.** Tokens live about an
hour and a fleet refreshing in lockstep is a genuine thundering-herd risk. The sidecar
makes the boundary observable; neither soak ran long enough to cross it.

**Already answered, and no longer future work:**

- *Measure and set `parallelism()`* — done, 64, validated across 30 minutes (§6f)
- *Confirm the quota model* — `traffic_type` reports `ON_DEMAND`; rate-limit rate is
  0.04% at ~2,200 requests/minute
- *Establish whether the ceiling is ours or theirs* — theirs; pool peaked at 25% and
  event loop lag under 5 ms (§6f)

## 9. Open questions and things still to confirm

### Answered by Evertune

| Question | Answer | What it changed |
|---|---|---|
| Batch or interactive? | **Both** — scheduled refreshes plus ad-hoc reports kicked off on creation | Drove the two-tier recommendation in §0b. Batch applies to the scheduled tier only, worth ~$29,600/yr at 200 reports. Assuming uniform batch would have made new reports take up to a day. |
| Downstream consumer? | Out of scope, notes welcome | See the note below on structured output |
| 2.5 Flash retirement? | 2.5 is fine | Recorded as a runbook risk rather than a redesign (§1) |

### A note on downstream processing, since it was invited

Not built, because it changes the response contract and that is a product decision.
But two things are worth recording.

**`responseSchema` would likely pay for itself.** Gemini can emit guaranteed-shape JSON
directly. If a second model is currently turning prose into structured brand mentions,
having Gemini produce the structure removes that call — plausibly a larger saving than
anything in §6c, since it eliminates an inference rather than discounting one. It also
converts truncation from a silent failure into a parse error: a `MAX_TOKENS` cut in
the middle of JSON is malformed and detectable, whereas a truncated prose list reads
as a complete short list. Given §6f measured **3.3% truncation** even at a 512-token
cap, that distinction is not hypothetical.

**Logprobs are free and additive** (§6e). If downstream ever wants "considered but not
recommended" as a signal, the data is already on the response at no token cost. The
extraction is the hard part, not the acquisition.

### Still open — would change a decision

1. **Does "almost recommended" matter to the product?** §6e shows Roborock holding
   1.83% probability while appearing in 0 of 100 samples. If brand share is the whole
   deliverable this is a free precision upgrade; if near-miss visibility is something
   customers would pay for, it is a new capability. I cannot make that call from
   outside.
2. **What is an acceptable time-to-first-report?** §0b measures ~4.7 minutes for a
   100-prompt report at current throughput. If that is too slow the lever is
   concurrency against a shared quota, which trades directly against §6f's tail
   growth. If it is fine, the ad-hoc tier needs no further work.
3. **How often are reports created concurrently?** Two reports kicked off in the same
   minute contend for the same quota. Burst shape determines whether admission control
   needs a queue with fairness or whether first-come-first-served is adequate.
4. **Does a duplicate answer cause harm?** We retry on 429 and 5xx — §6f recorded 8
   such retries in 19,223 requests. If a retry lands after the original in fact
   succeeded, does double-counting a mention corrupt a distribution built from exactly
   100 samples? That decides whether idempotency keys are needed.

### Would verify myself, given more budget

Ordered by how load-bearing the current claims are:

1. **Where the p99 tail stops growing.** §6f measured +37% p99 across 8.7 minutes with
   p50 flat. It was still climbing when the budget breaker stopped the run. A sweep
   that runs for an hour needs to know whether that plateaus.
2. **Whether Dynamic Shared Quota actually moves.** The adaptive limiter (§6b) is
   justified by capacity varying over time. §6f shows capacity is generous and that
   latency degrades before rejection, which validates keying on latency — but not that
   capacity *moves*. A multi-hour run spanning a business-hours boundary would settle
   it. If DSQ is stable per-tenant, the honest call is a fixed limit at the measured
   knee and deleting the adaptive machinery.
3. **Batch API pricing against a real invoice.** §6c uses Google's published rates.
   The ordering is robust; the absolute figures are unverified, and the two-tier
   recommendation rests on them.
4. **Quality: does thinking off change the answers?** The 4.0x cost argument is
   measured; "the answers look equivalent" is n=1 eyeballing and should not be relied
   on. This is the largest remaining gap in the recommendation.
5. **A Together baseline on identical prompts.** The brief asks for comparison against
   other models and the repo ships the provider. Not run.
6. **Safety-filter false-positive rate on competitor and brand prompts.** A block is
   terminal and silent; the rate matters specifically for brand tracking.
7. **Burst behaviour at report-creation scale.** 10,000 requests arriving at once is
   the real production event and has not been simulated end to end.
8. **Multi-worker scaling.** §6d shows a single worker is event-loop bound; §6f shows
   the ceiling was server-side at concurrency 64. Whether more workers raises the
   client ceiling is untested.

### Operational readiness

**Graceful shutdown.** SIGTERM and SIGINT drain: new requests receive 503 with
`Retry-After` while in-flight work finishes, bounded by `SERVICE_DRAIN_TIMEOUT_S`. For
a batch worker this matters more than usual — dropping a request mid-flight means
paying for tokens whose answer is discarded. Verified with six slow requests in flight;
all six completed.

**Logging is deliberately sparse.** A scheduled sweep issues hundreds of thousands of
requests; one line each is noise. Metrics carry the aggregate, logs carry the
exceptional:

- successes are never logged individually
- failures are logged once with error class, status code, attempt, backoff and retry
  history
- retries log at WARNING, not ERROR — a retried request has not failed yet, but a
  rising retry rate precedes real failure
- **unusable 200s are logged**, tagged `billed_but_unusable`, because nothing else in
  the stack treats them as errors

Pinned by tests, including one asserting five successful requests produce zero log
lines.

**Container.** Non-root, healthchecked, exec-form CMD so uvicorn is PID 1 and receives
SIGTERM directly. Credentials mounted, never baked in.

**Deliberately not built: request IDs.** This workload measures aggregate distributions
across 100 samples per prompt, so individual request identity is not a unit anyone
reasons about.

**Batch API is modelled, not implemented.** Per §0b it now has a clearly defined home —
the scheduled tier — which makes it the highest-value thing to build next.
