# Findings

I added Gemini 2.5 Flash on Vertex AI as a provider, then spent most of my time trying
to break it. This is what I learned, in roughly the order I learned it.

Two notes before the results. Everything here is backed by data in `results/real/`:
raw per-request records for the experiments, run manifests for everything else. The
analysis scripts re-derive their numbers from those files, so any figure can be checked
without spending a cent. Where I got something wrong and corrected it, the correction is
in Appendix B rather than scattered through the text.

Total spend on `evertune-tests`: **$46.15 across 96,414 requests**. Run
`python scripts/spend_report.py` for the breakdown.

---

## How I worked

I started before my GCP access came through. I already had a personal Gemini Developer
API key, so I pointed the provider at that and got moving on my own bill. That bought
about two days: the thinking-token accounting, the snake_case serialization bug, the
empty-200 failure mode and the truncation behaviour all came out of that period. When
`evertune-tests` was ready I re-ran everything that touches capacity or cost against
Vertex, because those numbers don't transfer between endpoints.

The work went in four passes.

**Learn the model.** Cheap, small experiments against a single prompt. What does
`thinking_budget` actually do to the bill? What happens at the output cap? What does the
SDK put on the wire? This is where the expensive surprises live, and finding them early
meant the load tests measured a correctly-configured client instead of a broken one.

**Learn the measurement.** Evertune samples each prompt 100 times and runs it twice,
once with live search off and once on. That shape has properties worth knowing before
it scales, what grounding costs, what temperature does to a brand's measured share,
what logprobs add on top of counting.

**Build the thing and test it locally.** The integration runs as an HTTP service and k6
drives it. I wrote a fake Vertex endpoint that speaks the real wire contract so I could
find our own bottlenecks for free, and injected failures that a real vendor won't
produce on demand.

**Then point it at Vertex.** Sustained soaks and a concurrency sweep to find where the
ceiling actually is, and whether it belongs to us or to Google.

---

## 1. What I learned about the model

### Thinking is on by default, and what it costs depends on the prompt

`thinking_budget` defaults to `-1`, which means the model reasons as much as it likes
before answering. A request with no `thinkingConfig` at all comes back with 212 thinking
tokens, so this isn't theoretical.

Those tokens bill at the output rate and nobody ever sees them. 100 requests per config
on `evertune-tests`/us-central1, 2,048-token cap so nothing was truncated:

| `thinking_budget` | Cost/request | p50 | Output tokens | Of which thinking |
|---|---|---|---|---|
| `0` (off) | **$0.000374** | 1,481 ms | 145.3 | 0 |
| `-1` (default) | $0.001344 | 3,751 ms | 533.6 | 411.2 |

Turning it off is **3.60x cheaper** on this workload, 95% CI [3.00, 4.35], and **77% of
billed output was invisible reasoning**. Data in
`results/real/model/think-off-n100-*` and `think-dyn-n100-*`.

### But that multiplier belongs to the prompt, not to the model

Here is the part that makes the number usable.

The ratio is roughly *(thinking + answer) / answer*. So it is governed by how long the
answer would have been anyway. Ask something that produces two words and thinking
dominates completely. Ask something that produces three paragraphs and thinking gets
diluted.

Measured directly, same model, same region, same day, only the prompt changed
(`results/real/model/thinking-verbosity.json`):

| Prompt | Answer length, thinking off | Answer length, thinking on | Multiplier |
|---|---|---|---|
| "Name the single best brand. Brand name only." | **2.5 tokens** | 224.2 | **38.5x** |
| "Which brands are worth considering?" (the workload) | 145.3 tokens | 533.6 | **3.6x** |

The two prompts are a similar length. What differs is the answer each one asks for. The
first constrains the model to a couple of words, so the reasoning is nearly the entire
bill. The second invites a paragraph, and the same reasoning gets spread across it.

**The same setting is worth 38x on one prompt and 3.6x on another.** That's a 10-fold
swing from prompt shape alone, and it means quoting a single multiplier for "what
thinking costs" says very little without also saying what was asked.

Our brand-recommendation prompts sit at the low end because they produce a decent
paragraph on their own. A terser extraction prompt, "return just the brand", would see
something closer to 38x, and that shape is a plausible thing to build if downstream
wanted structured output.

*(I also tested a deliberately verbose prompt. Both arms hit the 2,048-token cap, so
those numbers measure the cap rather than the model and I've thrown them out.)*

What does transfer is the share: **roughly 77-80% of billed output is reasoning nobody
reads**, and that held across every configuration I measured. That's the number to plan
with. The multiplier is a property of the workload, and each workload needs its own.

### Thinking and the output cap share one budget

This is the failure mode most likely to cause trouble in production, because it does not
look like a failure.

`thinking_budget` and `max_output_tokens` draw on the same allowance. So a generous
thinking budget can eat the entire cap and leave nothing for the answer. Force it with
`thinking_budget=1024` against `max_output_tokens=128`:

```
finish_reason   MAX_TOKENS
billed output   124 tokens  (118 thinking, 6 visible)
answer          "iRobot (Roomba),"
```

118 tokens of reasoning bought 6 tokens of answer, and the answer is a fragment ending
in a comma (`results/real/model/think-cap512-*`). Nothing about that response is an error. It's an HTTP 200 with text in it.

A provider that returns `response.text` and moves on hands that fragment downstream as a
successful answer. Brand extraction then records one brand from a question that asked
for five. That's why `finish_reason` travels on the response and why `is_usable`
requires `STOP` rather than just non-empty text.

The same mechanism shows up without forcing it. On the same prompt and the same
1,024-token cap: thinking off gives 260 visible tokens, thinking dynamic gives 25.

### One field goes out in snake_case

Inside `generationConfig`, every field the SDK emits is camelCase, `maxOutputTokens`,
`temperature`, `stopSequences`, except the thinking budget:

```json
"generationConfig": {
  "temperature": 1.0,
  "maxOutputTokens": 1024,
  "thinkingConfig": { "thinking_budget": 0 }
}
```

I found this because my mock matched on `thinkingBudget` and quietly ignored the setting.
The same thing could happen in production. If anything sits between the client and
Vertex and normalises field names to camelCase, a gateway, a proxy, or a recorded test
fixture, it would drop the budget on the way through. The request still succeeds. The
model just thinks freely, and the first sign is truncated answers and a larger bill than
expected.

Sending both spellings is not a hedge either. They map to the same protobuf oneof, so
sending both is a hard 400. `test_sdk_serializes_thinking_budget_in_snake_case` pins the
behaviour so an SDK release that normalizes it fails loudly.

### Truncation is quiet, and the right cap depends on grounding

At `max_output_tokens=512`, 3.3% of ungrounded answers hit the cap across 19,223
requests (`results/real/capacity/vertex-soak-*`). Two independent soaks agreed to three significant figures. Drop to 256 and it's
20%.

Grounded answers are a different story. They run 4.6x longer, so the same 512 cap
truncates **half** of them. 1,536 brings that to 1%.

One cap won't serve both conditions. And truncation matters more here than it looks,
because the downstream step isn't really mention-counting: "we wouldn't recommend
BrandA" contains the mention and means the opposite. A fragment cut mid-clause can
invert the sense of what it captured, which is worse than losing the sample outright.

### Comparing against Flash-Lite

The brief asked what stands out about this model compared to others. The cleanest
comparison available holds the vendor, API, region and prompt constant and changes only
the model tier, so that is what I ran: same 11 categories, same prompts, same
vocabularies, 60 samples each.

| | 2.5 Flash | 2.5 Flash-Lite |
|---|---|---|
| Cost per request | $0.000309 | **$0.000027** (11.5x cheaper) |
| Distinct brands found | 148 | 127 (−14%) |
| Brands in the informative 10–90% band | **81** | **57** (−30%) |

Flash-Lite is much cheaper and it resolves less. It finds 14% fewer brands outright,
and 30% fewer in the band where a mention rate is actually informative (§2 explains why
that band is the number that matters). The two models mostly agree on who leads, with
Herman Miller and Steelcase topping office chairs in both, so this reads as a resolution
difference rather than a disagreement about the market.

Worth being clear about what this is and is not. It is **not** a cost lever. Evertune
measures each model as its own target, running the same prompts against each one, so
Flash and Flash-Lite are two different measurements rather than two ways of taking the
same one. Switching between them to save money would be like switching which brand is
being tracked to save money.

What it does tell us is how much the tier choice changes the reported picture. If a
customer asks about visibility on Flash-Lite specifically, they should expect a coarser
picture: roughly a third of the brands that carry a meaningful rate on Flash will read
as always-present or absent on Flash-Lite. That is a caveat to attach to the output, not
a knob to turn.

Data in `results/real/flash-lite-*`, compared against the temperature 1.0 cells of
`results/real/temperature-multi-*`.

## 2. What I learned about the measurement

Evertune runs each prompt 100 times, and runs it twice, once with live search off, once
with it on. The gap between those two answers is the product. That shape has some
properties worth knowing before it scales.

### Grounding is the measurement, and it's almost the entire bill

Grounding means live web search. The model runs real searches before answering and cites
what it found, instead of answering from training data alone. Gemini exposes it as a
tool, `tools: [{google_search: {}}]`.

It is a different axis from thinking, and the two are easy to conflate. Thinking
changes how hard the model reasons over what it already knows. Grounding changes what it
knows. Different API, different billing, and grounded answers aren't reproducible
because the web moves underneath them.

I ran 20 prompts in both conditions, results are paired
(`results/real/measurement/grounding-*`):

| | Ungrounded | Grounded | |
|---|---|---|---|
| Input tokens | 35.2 | **35.2** | 1.00x [1.00, 1.00] |
| Output tokens | 160.7 | 299.4 | 1.86x [1.48, 2.45] |
| p95 latency | 3,256 ms | **10,076 ms** | 3.09x |
| Truncated at 512 | 0 / 20 | **10 / 20** | 50% [30%, 70%] |

Retrieved passages are **not** billed as prompt tokens, input is identical to the byte.
That makes grounded cost easier to forecast than I expected: a flat adder per prompt,
invariant to how much the model read.

The cost is on a separate SKU. I checked it against Google's Cloud Billing Catalog API
rather than the docs, because two sources quoted different numbers and one API call
settles it: SKU `F307-73C9-C204`, **$0.035 per grounded prompt above a 1,500-prompt free
allowance**. Both the $14 and $25 figures I found floating around online are wrong.

So a grounded request costs **123x** an ungrounded one. At 100 samples that's $3.53 per
prompt against $0.03.

### Which means most cost levers don't matter

This is the part I'd want a reviewer to sit with, because it inverts the obvious
analysis.

| Lever | Applies to | Share of a two-condition bill it touches |
|---|---|---|
| Thinking off | both conditions | ~100% of *token* cost |
| Batch API (2x) | ungrounded only, batch has no tool support | **~1%** |
| Context caching | neither, needs 2,048+ input tokens, workload is 35 | **0%** |
| **Grounding SKU** | grounded only | **~99%** |

Every token optimisation in this document works on roughly 1% of the bill. Batch
prediction can't run grounded requests at all, and implicit caching can't engage on a
35-token prompt. It needs 2,048 minimum, so the workload is 58x below the floor.

Flash-Lite is the cleanest example. Switching models looks like an 11.5x win on token
prices. On the real two-condition workload it saves **1.6%** while losing 30% of the
informative measurement. Once a per-prompt SKU dominates, model selection stops being a
cost lever.

### Grounding changes the answers, which is the whole point

One production unit: same prompt, 100 samples, both conditions, $3.67
(`results/real/measurement/production-unit-*`).

| Brand | Ungrounded | Grounded | Delta | 95% CI |
|---|---|---|---|---|
| **Dreame** | 5 | **97** | **+92** | [+86, +97] |
| Ecovacs | 52 | 93 | +41 | [+30, +52] |
| Eufy | 65 | 99 | +34 | [+25, +44] |
| Narwal | 3 | 36 | +33 | [+23, +43] |
| **Anker** | 18 | **3** | **−15** | [−24, −7] |
| **iRobot** | 100 | **85** | **−15** | [−22, −9] |

Dreame shows up in 5% of ungrounded samples and 97% of grounded ones. Anker falls as
Eufy rises, which tracks, Anker's robot vacuums are sold under the Eufy brand.

The ungrounded condition isn't a degraded version of the grounded one. It measures
something real and separately useful: what the model believes when nobody corrects it.

### You can't compare citations across samples

I set out to check whether 100 samples of one prompt retrieve the same web. They
returned 852 source URLs, all unique, zero repeats.

That's an artifact, not a finding, Vertex signs a fresh redirect token per request, so
the same publisher gets a different URL every call. But the real problem is worse than
the one I was looking for. **Source-level comparison is not possible from the response
alone.** It is not just difficult, it is actually impossible. Every question about which
publishers drive a brand's
visibility needs all 852 redirects resolved first, and those tokens expire.

For a product tracking brand visibility over time, provenance has to be resolved at
collection time or it is gone. Resolving redirects was outside what the exercise asked
for, so it is written up in §7 as production work rather than built here.

Retrieval does vary, in the one place it is visible. 100 identical prompts issued 428
searches
across 154 distinct query strings. One dominant query appears in about two thirds of
samples, but the combination differs almost every time. So the grounded condition
carries retrieval variance on top of generation variance. It's the noisier instrument,
and its noise floor belongs to Google.

### Temperature decides whether a share can be expressed at all

Everything ran at 0.7 to start with, which is a convention rather than a result. So I
swept it: 11 categories, 5 temperatures, 60 samples each, 3,300 requests, $1.08
(`results/real/measurement/temperature-multi-*`).

Coverage first. `temperature=0` finds about a third fewer brands, and not one of the 11
categories beat temperature 1.0:

| Temp | Mean distinct brands | Categories beating temp 0 |
|---|---|---|
| **0.00** | **9.4** | |
| 0.70 | 13.1 | 10 / 11 |
| 1.00 | 13.5 | **11 / 11** |
| 1.40 | 14.2 | **11 / 11** |

But coverage isn't the interesting part. Look at where per-brand mention rates actually
land:

| Temp | Rate <5% | Middle | Rate >95% | In the 10–90% band |
|---|---|---|---|---|
| **0.00** | 18 | **7** | 78 | **0 of 103** |
| 0.70 | 16 | 95 | 33 | 74 of 144 |
| 1.00 | 21 | 95 | 32 | 81 of 148 |

At temperature 0, **not one brand out of 103 landed between 10% and 90%**. Every brand
reads as always-named or never-named. At 0.7, half of them sit in that band.

That's the whole argument. Evertune's deliverable is a share, "this brand appears in
40% of answers". Temperature 0 cannot produce one. It produces a yes/no list and
spends 100 samples confirming it. The apparent stability at temperature 0 (drift 0.015
against 0.052 at 0.7) is not a better measurement. It is a narrower one.

**Use 1.0.** Counting brands whose rate lands in the informative band:

| Temperature | Informative brands | Gain over the previous step |
|---|---|---|
| 0.00 | 0 | |
| 0.35 | 57 | +57 |
| 0.70 | 74 | +17 |
| **1.00** | **81** | **+7** |
| 1.40 | 81 | 0 |

The curve flattens at 1.0. 1.0 beats 0.7 by +0.64 brands per category, 95% CI
[+0.09, +1.27], which clears zero but not by much, so anywhere in [0.7, 1.0] measures
about the same thing.

It is worth asking whether values above 1.0 do anything at all, since some APIs clamp
them. They are not clamped here. 1.4 produced 142 distinct brands against 1.0's 139, and
335 distinct answer sets against 294. It is doing something, it just is not finding
anything new.

The tiebreaker: **1.0 is Gemini 2.5 Flash's own default**, so there is no need to argue
that a
tuned-down value preserves whatever calibration Google did. Set it explicitly, though.
`GenerateContentConfig()` leaves it `None`, which means the effective value lives on
Google's side, where they could change it tomorrow and alter the output without anything
in our code changing.

Two things matter more than the exact number. Never sample at 0, which is a natural
choice for anyone wanting reproducibility and the wrong one here. And freeze whatever
value is chosen, then version it with the results. A brand time
series that does not record its temperature is fundamentally two different measurements
that are not comparable.

**The noise floor is about 5 percentage points**, measured as drift between two
independent 30-sample halves at the same setting. Any brand movement smaller than that
isn't a finding. Reporting a 3-point move as a trend is the easiest mistake this product
can make.

### Logprobs are free and see what counting can't

Evertune already takes 100 samples, so the question isn't whether to sample less. It's
what logprobs add on top. Answer: they cost nothing (token counts are identical with
them on or off) and they see below the counting floor.

In a 100-sample run where iRobot won 97 times:

| Token | Mean probability | In any sample? |
|---|---|---|
| iRobot | 0.9308 | yes |
| Roomba | 0.0470 | yes |
| **Roborock** | **0.0183** | **no, 0/100** |
| **Shark** | **0.0023** | **no, 0/100** |

Roborock is a major brand in that category. It held 1.83% of the probability mass and
appeared in zero samples. Counting reports it as absent, which is indistinguishable from
a brand the model has never heard of. Those are very different findings, and only one is
true.

Catching Roborock by counting would take roughly 1,300 samples. Shark would need 10,900,
at about $3.14 each.

Caveat: the prompt was constrained to a single brand name so the first token is a clean
branch point. Real prompts return prose, where brand names are multi-token and appear at
varying positions. This shows the information exists and is free. It doesn't show that a
production extractor is easy to write.

### Structured output does not work where it is needed most

`responseSchema` looked like the fix for extraction. Gemini emits guaranteed-shape JSON,
which removes a downstream parsing call and turns truncation into a detectable parse
error instead of a short-looking answer.

Vertex refuses to combine it with grounding:

```
400 INVALID_ARGUMENT
Unable to submit request because controlled generation is not supported with Search tool
```

Ten out of ten grounded schema requests failed. Function calling doesn't get around it
either. That returns `Multiple tools are supported only when they are all search
tools.` So grounding can't coexist with any non-search tool.

That is backwards from useful. Grounded answers are the ones that change shape: 14 of 20
came back as structured listicles rather than prose. Whether that breaks a given
extractor depends on the extractor, and a pipeline built for listicles would be fine.
But it does mean the two conditions return different formats, so extraction has to be
validated on both. Grounding is where a schema would help most, and it is the one place
it cannot go.

On the ungrounded arm it works and mostly delivers. Extraction is 100% either way, but
structure costs **1.54x output tokens**. The truncation claim holds with a catch: at a
200-token cap prose truncated 0 of 10 while schema truncated 5 of 10, JSON is more
verbose, so it hits the cap sooner. But all 5 were *detected*, because the JSON failed
to parse. You're trading rare silent failures for more frequent loud ones, and the cap
needs to rise about 1.5x.

One thing I wasn't looking for. With a function tool attached and no grounding, the model
declined a question it answers freely otherwise:

> "I can't answer that, as I cannot make specific product recommendations. I can,
> however, record any brands you are considering, along with your sentiment toward them."

`finish_reason=STOP`, no safety block. It reinterpreted its role around the tool it was
given. That's a single observation and I'm not claiming a rate, but tool presence
changing *what the model will say* rather than just how it formats would be a serious
confound for a product whose measurement is the answer content.

---

## 3. What I learned load testing our own code

The thing that has to survive production traffic is our service, not Vertex. So the
integration runs as an HTTP service and k6 drives it over HTTP exactly the way real
traffic would, separate process, separate runtime, no shared event loop to flatter us
with.

```
k6  ->  service/app.py  ->  llm/gemini.py  ->  Vertex (or the mock)
k6  --------------------------------------->  Vertex (or the mock)   [control]
```

Most of this ran against a fake Vertex endpoint that speaks the real `:generateContent`
contract. That isn't a shortcut. It lets me inject 429s, empty 200s, safety blocks and
mid-run capacity collapse, none of which a real vendor produces on demand. It also costs
nothing, so I could iterate.

### The connection pool is a hidden ceiling

Hold concurrency at 64, vary only the HTTP pool, against a backend answering in a flat
500 ms:

| Pool | Throughput | p50 | **Mean** | p90 | Pool ratio |
|---|---|---|---|---|---|
| 8 | 15.3 rps | 516 ms | **3,946 ms** | 9,684 ms | **8.0** |
| 16 | 30.6 rps | 513 ms | 2,036 ms | 5,249 ms | 4.0 |
| 64 | 120.1 rps | 511 ms | 528 ms | 519 ms | 1.0 |
| 128 | 120.7 rps | 510 ms | 524 ms | 519 ms | 0.5 |

Throughput tracks pool size exactly until the pool exceeds concurrency, then flattens.
That part's expected.

The latency columns are where the lesson is. At pool=8 the mean is 3.9 seconds for a
backend answering in 500 ms. An 8x inflation that's pure queueing inside our own
process, invisible in the vendor's response.

**And p50 hides it completely.** It sits at ~515 ms at every pool size, because the
distribution is bimodal: whoever wins a connection sees the true 500 ms and everyone else
waits. A dashboard showing p50 alone would report a starved pool as healthy. p90 and the
mean both catch it, which is the argument for carrying p50, p90, p95 and p99 rather than
a single number.

`llm_pool_saturation_ratio` is in-flight ÷ pool size, so it exceeds 1.0 when
oversubscribed. It moves long before the median does.

### Our layer costs about 2 ms, and it sheds instead of collapsing

Same workload, 50 rps for 30 s, through the service versus straight to the backend:

| Path | p50 | p95 | p99 |
|---|---|---|---|
| direct to backend | 401.7 ms | 515.9 ms | 553.0 ms |
| through our service | 403.6 ms | 515.7 ms | 565.3 ms |
| **difference** | **+1.9 ms** | −0.2 ms | +12.3 ms |

On a ~400 ms request that's about 0.5% at p50.

Push past capacity and it sheds rather than queues. Shedding means refusing a request
outright with a 503 and a `Retry-After`, instead of accepting it and making it wait
behind everything else:

| Offered | Served | 503s | p50 | Our overhead p99 |
|---|---|---|---|---|
| 100 rps | 2,001 | 0 | 404 ms | 0.25 ms |
| 200 rps | 3,993 | 8 | 403 ms | 0.19 ms |
| 300 rps | 4,616 | 1,385 | 386 ms | 0.21 ms |
| 400 rps | 4,358 | 3,643 | 331 ms | 0.19 ms |

Three things in that table. Our overhead never moves across a 4x range of offered load,
so the service isn't what's degrading. p50 *falls* at 400 rps because shed requests
return immediately and admitted ones aren't stuck behind a backlog. That's backpressure
working. And k6 dropped zero iterations throughout, so the generator kept up and these
are real numbers rather than rig artifacts.

Shedding early keeps a saturated service legible. Unbounded queueing turns a throughput
problem into a latency problem and then into a memory problem.

### One process is the ceiling, not the code

An obvious follow-up: when do extra worker processes stop being optional?

The service runs under uvicorn, the standard ASGI server for FastAPI. It can fork into
several worker processes that share one listening socket. Since Python runs one thread
of bytecode at a time per process, worker count is the practical way to use more than
one core.

The naive version of that experiment is confounded, because 4 workers also means 4x the
admission capacity. So I held **total** capacity at 512 throughout, 1 worker at 512, 2
at 256, 4 at 128, leaving process count as the only variable. 400 rps offered:

| Workers | Served rps | Shed | p99 | Our overhead p99 |
|---|---|---|---|---|
| **1** | 155 | **61%** | 12,305 ms | 0.27 ms |
| 2 | 327 | 18% | 5,698 ms | 0.28 ms |
| **4** | **400** | **0%** | **814 ms** | 0.45 ms |

Same capacity, same load, same backend. One process sheds 61% of what four absorb
entirely, and p99 drops 15x.

Our per-request overhead barely moves, 0.27 ms to 0.45 ms. The service isn't slow, and
no amount of optimising the request path closes that gap. A single event loop just
can't schedule the work.

It also explains the admission arithmetic. 512 permits on one process admits far more
concurrent work than the loop can drive, so requests get accepted and then starve: p99
of 12.3 seconds on a code path costing a quarter of a millisecond. Capacity beyond what
one loop can serve is queueing with extra steps.

**Scale by processes, not by concurrency.** `parallelism()` at 128 is per process.

### An instrumentation bug that blamed our own code

Worth including because the number was convincing and wrong.

The first version of the overhead metric computed `total - latency_ms`, where
`latency_ms` is the *final* attempt's vendor latency. On a retried request that charged
us for the failed attempts and their backoff sleep. With 4% injected failures, p99
"overhead" read **1,807 ms** on a path whose real cost is a fraction of a millisecond.

It didn't just report a wrong number. It pointed at the wrong component. Anyone reading
that dashboard goes looking for a stall in the Python layer that doesn't exist.

The fix is to attribute all three costs separately:

```
attempts=4  upstream=506.5ms  backoff=3785.8ms  ours=11.16ms   (total 4303ms)
attempts=1  upstream=467.4ms  backoff=   0.0ms  ours= 0.29ms
```

| | before | after |
|---|---|---|
| our overhead p99 | **1,807 ms** | **0.50 ms** |
| retry backoff p99 | *(hidden inside overhead)* | 1,518 ms |

The lesson generalises: a latency decomposition that ignores retries will blame the
component doing the retrying.

---

## 4. What I learned pointing it at Vertex

### It holds 37 rps for 21 minutes without drama

`evertune-tests`/us-central1, concurrency 64, thinking off, 512-token cap
(`results/real/capacity/vertex-soak-long-*`):

| | 8.7-min run | 20.8-min run |
|---|---|---|
| Requests | 19,223 | **47,677** |
| Sustained throughput | 35.6 rps | 36.9 rps |
| p50 | 1,401 ms | 1,379 ms |
| Retry rate | 0.047% (9) | 0.050% (24) |
| Truncation at 512 | 3.3% | **3.3%** |

Two independent runs agreeing to three significant figures on truncation, and within 7%
on retry rate, is the part I'd stake a production decision on.

One caveat I want to be honest about: both runs happened on the same afternoon. Vertex
uses Dynamic Shared Quota, so the ceiling moves with whatever else is running in the
region, and a 0.05% retry rate reflects the conditions I got rather than a structural
property of the endpoint. Two runs twelve minutes apart is not a test of day-to-day
variability. I'd want a week of these before treating 37 rps as a planning number.

Quota is enforced per minute and a cold connection pool takes tens of seconds to warm,
so both facts set a floor on how long a capacity test has to run. Sub-minute runs
under-report by about 2.5x here. That's worth stating because burst-shaped benchmarks
are the norm and they systematically flatter the system.

**p99 is not settled** and should be quoted as a range, roughly 3.7–9.6 s. Fine for a
batch workload; anything with a tail SLA wants provisioned throughput instead of shared
quota.

### Vertex does rate limit, and hand-rolled retries are why we can see it

Nine requests needed a retry in the first soak, 24 in the second. Not one surfaced to a
caller.

That's the payoff for a decision made early on principle. `llm/retry.py` does retries in
our own code rather than delegating to the SDK's `HttpRetryOptions`, specifically so
retried failures stay visible to instrumentation. With SDK retries enabled those 24 would
have been invisible, and the conclusion would have been the flattering, false "Vertex
never rate limits us."

The error taxonomy covers more than the obvious codes. **499 (`CANCELLED`) maps to a
retryable server error**, not a client error. Despite sitting in the 4xx range, it means
upstream shed the connection rather than that we sent something invalid. Treating it as
a 4xx would fail the request permanently on a condition that's worth another attempt. I
didn't hit it at 37 rps, but it's the kind of thing that only shows up at rates well
past what I ran.

### The concurrency ceiling is 128, and past it the bottleneck is us

Vertex us-central1, 25–75 s measured per stage after discarding a warm-up window
(`results/real/capacity/vertex-knee-*` and `vertex-extreme-*`):

| Concurrency | Throughput | rps per unit | p50 | p99 | Event loop lag | Pool |
|---|---|---|---|---|---|---|
| 8 | 4.2 rps | 0.525 | 1,534 ms | 7,571 ms | ~0 ms | 25% |
| 32 | 17.2 rps | 0.537 | 1,473 ms | 8,049 ms | ~0 ms | 25% |
| 64 | 36.1 rps | 0.565 | 1,410 ms | 4,133 ms | <5 ms | 25% |
| **128** | **73.7 rps** | **0.575** | **1,328 ms** | **3,813 ms** | **<5 ms** | 50% |
| 256 | 63.0 rps | 0.246 | 1,962 ms | 10,818 ms | **457 ms** | 50% |
| 1024 | 43.7 rps | 0.043 | 17,557 ms | 49,443 ms | **4,301 ms** | 50% |

Linear to 128. Rps per unit of concurrency holds between 0.50 and 0.58 across a 16x
range, which is Little's Law behaving (throughput = concurrency ÷ latency, so at a fixed
latency each extra concurrent request should buy a fixed amount of throughput). p50
actually *improves* up to 128.

Then it collapses. At 1024 the throughput is below the c=64 level, p50 is 13x worse, and
p99 reaches 49 seconds. Pushing 8x harder than the optimum delivers 40% less work.

The last column explains all of it. The pool sat at 50% throughout, raised to 2,048 so
it couldn't be the constraint, while event loop lag went from under 5 ms to **4.3
seconds**. At c=1024, a quarter of the 17.5 s p50 is our own scheduler delay before a
request reaches the network.

So the answer to "is the ceiling us or them" flips depending on the operating point. Up
to 128
it's Vertex and our client is idle. Past 128 the ceiling is a single Python process and
Vertex isn't the limiting factor at all.

Without `llm_event_loop_lag_seconds` the c=1024 result reads as "Vertex collapses under
load," which is both wrong and the kind of wrong that gets escalated to a vendor.

### TLS is what costs us, and HTTP/2 doesn't fix it

If the ceiling is our event loop, what is it busy doing? TLS.

Against a local backend with no TLS, c=1024 loses only 8% of peak throughput. Against
Vertex the same concurrency loses 41%. Same client, same code. The difference is
encryption work on the event loop.

HTTP/2 multiplexes many requests over a few connections, so it should help. It fixed the
symptom precisely: **event loop lag went from 4,301 ms to 2 ms.** That confirms the
diagnosis.

But throughput got worse, 55.2 rps at c=1024 against 73.7 at c=128 on HTTP/1.1. Fewer
connections means less parallelism at the transport layer, and that costs more than the
handshakes save.

Negative result, shipped off by default behind `GEMINI_HTTP2`. It stays in the repo
because it's the evidence for the TLS diagnosis, not because I recommend it.

### Adaptive concurrency: real, but not proven here

`parallelism()` returning a constant assumes capacity is something discovered once.
Vertex uses Dynamic Shared Quota, which publishes no per-project ceiling and moves with
regional demand, so any constant has a shelf life.

I built a gradient limiter that keys on **latency** rather than error codes, because
Vertex often doesn't reject excess load. It just slows down. A controller watching only
429s sees a healthy service and keeps climbing.

Against a backend whose capacity collapses mid-run, adaptive produced about **30x fewer
errors** than a fixed cap tuned for the good case. But in the healthy phase it managed
176 rps against fixed-high's 360, so the trade is roughly half the peak throughput for a
large drop in errors.

**It ships off by default.** The capacity change in that experiment was simulated by
reconfiguring the mock. It's a fair test of the controller's dynamics, but it isn't
evidence that Vertex's quota actually moves on that timescale. And my two soaks twelve
minutes apart saw the same ceiling, which is weak evidence *against* volatility. Shipping
less machinery is right when the justification is unproven. The code's there if a
multi-hour run later shows capacity moving.

---

## 5. What it costs to run

Evertune confirmed the workload for me, and one detail changed my recommendation.

| | |
|---|---|
| Real-time responses | Not a concern |
| **Ad-hoc** | New reports created through the day, kicked off right away |
| **Scheduled** | Existing reports refresh monthly, weekly or daily |
| Sampling | Each prompt runs **100 times**, the same prompt, not 100 different ones |
| Conditions | Every prompt runs **twice**: live search off, then on |

I'd assumed "batch" and optimised for it. But there are two workloads here, and
conflating them costs either money or responsiveness.

First, some arithmetic, and it is worth being explicit about which parts came from
Evertune and which are mine. Evertune confirmed 100 samples per prompt and two
conditions per prompt. **How many prompts make up a report, and how many reports exist,
are my assumptions.** I have used 100 prompts and 200 reports throughout because they
give round numbers, not because anyone said so.

On those assumptions, one report refreshed once is:

```
100 prompts x 100 samples x 2 conditions = 20,000 requests
```

**Ad-hoc is a burst problem.** Those 20,000 requests arrive at once when someone clicks
create. At the measured 36.9 rps that is a few minutes of wall clock, and it is the
scenario the admission control and retry budget exist for.

It is also **$356**, of which $350 is grounding.

**Scheduled is where the money is**, and cadence dominates everything else. Refresh
frequency is the assumption I am least sure of, so here is the whole range rather than
one number. 200 reports, 100 prompts each:

| Cadence | Grounded/yr | Ungrounded/yr | Batch saves |
|---|---|---|---|
| Daily | $25,760,240 | $210,240 | $105,120 |
| Weekly | $3,669,952 | $29,952 | $14,976 |
| **Monthly** | **$846,912** | **$6,912** | **$3,456** |
| Quarterly | $282,304 | $2,304 | $1,152 |

Two things fall out of that table.

**Cadence moves cost far more than any optimisation in this document.** Daily versus
monthly is a 30x swing. Every engineering lever here, thinking off, Batch, caching,
model tier, put together cannot move the number that much. If cost is a concern, the
first conversation is about refresh frequency, not about tokens.

**Batch only ever touches the ungrounded side.** Batch prediction does not support
tools, so grounded requests run online at full rate no matter how patient the caller is.
At monthly cadence the Batch saving is about $3,500 a year against a grounded bill of
$847,000. Worth taking, since it is nearly free to implement, but it is not a strategy.

*(By "arm" I mean one of the two conditions. The ungrounded arm is every request made
with live search off, the grounded arm is the same prompts with it on.)*

The $35/1k grounding rate is verified against Google's billing catalog, so that is not
the weak link. The assumptions worth challenging are prompts per report, number of
reports, and above all cadence.

### The token levers, for the arm where they apply

| Configuration | $/request | vs naive |
|---|---|---|
| interactive, dynamic thinking (the defaults) | 0.00115634 | 1.0x |
| thinking off | 0.00028834 | 4.0x |
| **thinking off + Batch** | **0.00014417** | **8.0x** |

At 50,000 ungrounded prompts/day that's $21,103/yr down to $2,631/yr. Same work, same
model, 12% of the bill.

Reproduce with `python scripts/cost_model.py --daily 50000`. Verify the rates it uses
with `python scripts/verify_pricing.py`, which checks them against Google's billing
catalog and exits non-zero on a mismatch.

---

## 6. Changing the provided contract

The brief said to deviate from the existing patterns where Gemini does not fit, and to
explain why. This is the one place I did.

I treated `llm/llm.py` as immutable for most of this work and kept it byte-identical.
Most of what I thought needed a contract change didn't. Thinking-token accounting works
fine if `output_tokens` is computed as visible + thinking, which keeps the inherited
field meaning "total billed output", what a base-contract caller already assumes.
`answer` doesn't need to be nullable either; the provider raises
`LLMEmptyResponseError` rather than pushing a `None` into callers who believe they hold
a string.

Grounding is what changed my mind. Two facts:

The contract had no way to express it. Not the request, and worse, not the response.
There was nowhere to report *whether search actually ran*.

And it can't live on a Gemini-specific subclass, because Evertune compares brand
visibility across models. A grounded path that only exists on one provider's concrete
type can't be swapped, which defeats the point.

A subclass would also have forced grounding to be chosen at construction time, meaning
one provider instance per condition. Both conditions run on every prompt, so that
permanently halves the effective connection pool and doubles TLS handshakes. And TLS is
where our throughput goes.

The change:

```python
grounded: bool = False                                     # what happened
grounding_sources: list[str] = field(default_factory=list)
def supports_grounding(self) -> bool: return False
async def ask_generic_question(..., *, grounded: bool = False)
```

Constraints I held to: additive only, original three fields keep their names, order and
types, every addition defaults so `SimpleResponse("hi", 1, 2)` still works, and the new
parameter is keyword-only with a default of `False`. A feature costing 123x per request
should never be a silent default.

The important detail is that **the response reports what happened, not what was
requested.** Asking for grounding doesn't guarantee it: the model can decline, retrieval
can fail, and the request still returns 200 with a plausible answer. A contract that
allows the request without allowing the check has the corruption built in.

`supports_grounding()` defaults to `False`, and `Together.ask_generic_question` *raises*
on `grounded=True` rather than quietly answering ungrounded. Same bug one level up.

Once the contract carried grounding, keeping a separate subclass for `finish_reason`,
`thinking_tokens`, `cost_usd` and timing stopped making sense, none of those are
Gemini-specific either. Together exposes `finish_reason` as `choices[0].finish_reason`
and the stock provider throws it away.

Everything the exercise shipped is still at its original path. `llm/together.py` differs
by one import fix, `from llm import LLM` was a circular import that resolved only by
accident of import order, plus the grounding guard.

**If the answer is "the contract is fixed, work around it,"** the fallback is grounding
on the Gemini type only, at the cost of the grounded path not being polymorphic. That's a
reasonable call to make differently and it's about one line of config away.

---

## 7. What I'd do before production

Ordered by what would actually change an outcome.

**Pin temperature and re-baseline once.** The code now uses 1.0, but historical brand
shares were collected at 0.7 and shift by up to 13 points, above the ~5-point noise
floor. A stored series spanning that change shows a step that's a config artifact rather
than a market move. One re-baseline run per tracked prompt, then freeze the value and
record it with every result.

**Resolve grounding redirects at collection time.** Citation URLs are per-request signed
tokens that expire. Resolving them at collection time is the only option. Skip it and
the ability is permanently lost to answer "which publishers drive this brand's
visibility." Biggest
engineering gap here.

**Implement Batch for the scheduled ungrounded arm.** ~$29,600/year at 200 reports, with
a clearly bounded home: scheduled refreshes only, never ad-hoc, never grounded.

**Decide the grounding cadence.** At $356 per report the interesting question stops being
"how fast can we serve this" and becomes "does every prompt need the grounded condition,
and how often does a report need refreshing." Worth more than every engineering lever
here combined.

**Set truncation policy per condition.** 1,536 for grounded, 512 for ungrounded. And
decide deliberately whether truncated answers are dropped or retried, `is_usable`
currently discards them, which is safe but silently reduces sample count.

**Run more than one process.** `parallelism()` is per process and one event loop tops out
well below what the pool allows.

---

## 8. Open questions

**Does the grounded arm need 100 samples?** 100 is settled for the ungrounded condition.
Grounded answers carry retrieval variance on top of generation variance, so they're
noisier, not quieter. But nobody has told me the sampling policy has to match across
conditions, and at 123x per request that's the largest lever left.

**Does attaching a tool change what the model will say?** One request declined a question
it answers freely without a tool attached. n=1, no rate claimed. If it replicates it's a
serious confound for a product whose measurement is the answer content. About a cent to
settle with 20 paired requests.

**Does Dynamic Shared Quota actually move?** That's the entire justification for the
adaptive limiter, and two runs twelve minutes apart isn't a test of it. A multi-hour run
across a business-hours boundary would settle whether to enable it or delete it.

**Does retrieval variance move brand share?** Grounded answers vary and retrieval varies.
Separating "the model changed its mind" from "different pages came back" needs the same
unit re-run days apart.

**Does temperature interact with prompt wording?** One prompt template, English only. A
differently-phrased question could sit at a different point on that curve.

**How does this compare to another vendor's model?** Together is already in the repo and
the harness accepts `--provider together`, but I don't have a key. Worth noting the
comparison I'd most want, grounded versus grounded, isn't available anyway, since
Together has no first-party web search.

---

# Appendix A: Evidence, and how to check it

## Where each number came from

Findings carry one of three evidence classes. I kept them separate rather than
presenting harness numbers as vendor measurements.

**Measured**, live requests against Vertex AI, project `evertune-tests`,
`us-central1`, unless a section says otherwise. Real model, real billing, real failure
modes.

**Validated**, produced against the fake Vertex endpoint in `mock/`, which exercises
the full HTTP path, the real SDK and the real connection pool at zero cost. Used for
mechanism proofs where the vendor is deliberately held constant, and for failure modes
that can't be provoked on demand against a real vendor.

**Measured on the Developer API**, live requests on my own personal key, from before
GCP access came through. Model behaviour and token economics transfer; throughput and
latency don't, and no capacity claim here rests on them.

| Finding | Class | Data |
|---|---|---|
| Thinking costs 3.6x on our prompt | measured | `model/think-*-n100-*` |
| Multiplier depends on prompt shape | measured | `model/thinking-verbosity.json` |
| Thinking/output share one budget | measured | `model/think-cap512-*` |
| snake_case serialization | validated | `test_sdk_serializes_thinking_budget_in_snake_case` |
| Truncation 3.3% @ 512 | measured | `capacity/vertex-soak*` |
| Flash-Lite comparison | measured | `model/flash-lite-*` |
| Grounding cost and behaviour | measured | `measurement/grounding-*` |
| One production unit | measured | `measurement/production-unit-*` |
| Temperature sweep | measured | `measurement/temperature-multi-*` |
| Logprobs | measured | `measurement/logprobs-experiment.json` |
| Structured output | measured | `measurement/structured-output-*` |
| Thinking default, tools + search | measured | `model/review-probes-*` |
| Connection pool ceiling | validated | `local/pool-experiment.txt` |
| Service overhead and shedding | validated | k6 summaries |
| Worker count vs GIL | validated | k6, capacity held constant |
| Sustained soak | measured | `capacity/vertex-soak-long-*` |
| Concurrency ceiling 128 | measured | `capacity/vertex-*` |
| HTTP/2 | measured | `capacity/vertex-http2-*` |
| Adaptive limiter | validated | `local/adaptive-experiment.txt` |
| Pricing rates | verified | Cloud Billing Catalog API |

## What's committed

Every run manifest, carrying the aggregates, percentiles, 30-second windows and cost
reconciliation each number derives from. Plus per-request records for the experiments,
so the analysis scripts re-derive their results from the same committed data.

Per-request records for the load runs are excluded. That is 20 MB of one line per
request for throughput tests whose manifests already contain every derived figure.

`results/real/README.md` indexes every file and says what each run was trying to
learn.

## Reproduce without spending anything

```bash
python scripts/confidence.py            # bootstrap CIs on every headline ratio
python scripts/temperature_analysis.py  # the temperature sweep, re-analysed
python scripts/cost_model.py --daily 50000
python scripts/verify_pricing.py        # checks rates against Google's catalog
python scripts/spend_report.py          # what was spent, by account
```

`make mock-up && make service-up` brings up the whole stack locally. `RUNBOOK.md` has
the rest.

## A note on confidence

Headline ratios carry bootstrap intervals and small-n results say so.

The thinking ratio is worth a note. I originally ran it at n=15 per config and quoted
4.0x without an interval. Putting one on it was sobering: **[2.36, 7.42]**. That is not
a measurement, it is a range containing most plausible answers, and I had been treating
it as a fact. Re-running at n=100 moved the point estimate barely (3.60x) and tightened
the interval to [3.00, 4.35], which is finally usable.

The lesson I'd keep: a point estimate with no interval hides how little it settles, and
n=15 against a quantity with a standard deviation of 223 tokens was never going to
settle anything.

I'd also written that the ratio couldn't be bootstrapped because manifests store
per-stage totals. That was wrong; the per-request ledgers were sitting in the same
directory the whole time. `scripts/confidence.py` now derives it from those.

---

# Appendix B: Where I was wrong

Six corrections worth recording, because in most cases the mistake is more useful than
the number.

**The grounding rate.** I carried $25 per 1,000 grounded prompts through four sections,
hedged with "verify against an invoice." Then I queried Google's Cloud Billing Catalog
API, the same data invoices are generated from, and got **$35 per 1,000**, with a
1,500-prompt free allowance rather than the ~5,000 I'd assumed. Every grounded figure
here moved up 40%.

The same query confirmed all four token rates to the cent. So the check that corrected
one number validated four others. The authoritative source was cheaper to consult than
the approximation, and I should have gone there before hedging.

**A mean labelled as a median.** The connection-pool table originally reported 4,162 ms
in a column headed "p50." That was the mean. Real p50 is 516 ms and stays flat at every
pool size. The conclusion survived and the throughput numbers reproduced, but here the
difference between mean and median *is* the finding, I'd have shipped a table that
undersold its own point. Caught by re-running the experiment to produce committed
evidence for a table that previously had none.

**A brand story built on a display artifact.** My analysis script truncated a counter
with `most_common(12)`, so any brand ranking below twelfth printed as 0 rather than its
real count. I wrote a paragraph about Neato appearing only in the ungrounded condition
and tied it to the company's 2023 bankruptcy. Neato is 11 versus 8. Not significant.
The story was clean, plausible, and about nothing. Computing confidence intervals forced
a recount from source, which is the only reason it surfaced.

**A p99 trend that wasn't.** An 8.7-minute soak showed p99 rising 37% between halves and
I wrote it up as a queue forming upstream. A 20.8-minute run refuted it: p99 oscillates
between 3.7 and 9.6 s with no trend. Nine windows was enough to see a shape that wasn't
there.

**A cost saving that can't physically occur.** I modelled context caching at a ~1.02x
saving. Implicit caching on 2.5 Flash needs 2,048 input tokens minimum and the workload
is 35, 58x below the floor. The effect isn't small here, it's impossible. The number was
tiny enough that it never looked worth checking, which is exactly how an unfalsifiable
assumption reaches a headline.

**A multiplier quoted as if it were a constant.** I reported thinking as "4.0x more
expensive" from n=15, which is true for our prompt and misleading everywhere else. The
ratio is roughly (thinking + answer) / answer, so it's governed by how verbose the
un-thought answer would have been. On a terse prompt the same setting costs **38.5x**.
Re-running at n=100 confirmed 3.6x for our workload and tightened the interval, but the
useful correction is that the number was never portable. And I'd have shipped it as
though it were. The share of billed output that is reasoning (~77%) is the part that
actually transfers.

**A mechanism generalised from one brand.** A single-category pilot found Anker swinging
92% to 14% with temperature and I traced it to phrasing, Anker appears mostly inside
"Eufy (Anker)", and temperature changes how often the model bothers with the aside. I
generalised that into a claim about aside-mentioned brands. Across 11 categories only 2
of 116 brands qualified as aside-mentioned, and every one of the largest swings was a
direct mention. True about one brand, generalised on a sample of one.

---

# Appendix C: The model retires 2026-10-16

Gemini 2.5 Flash on Vertex is scheduled for retirement on **2026-10-16**, confirmed
against Google's published deprecation schedule. Evertune has said 2.5 is fine for this
exercise, so I'm recording it as a risk rather than treating it as a blocker.

Migration is cheap by construction. The provider takes the model from configuration,
`llm/pricing.py` is a lookup table rather than hardcoded arithmetic, and the whole load
suite re-runs unchanged against a different model. Call it an afternoon: change the
config, re-run the thinking experiment and one soak to confirm the economics carry over.

The thing I'd actually check first is whether the thinking-token behaviour holds on
Gemini 3 Flash. That's the finding most likely to be model-specific, and it's worth
3.6x on our prompt shape and considerably more on a terser one.
