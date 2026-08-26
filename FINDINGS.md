# Findings

I added Gemini 2.5 Flash on Vertex AI as a provider, then spent most of my time trying
to break it. This is what I learned, in roughly the order I learned it.

Two notes before the results. Everything here is backed by data in `results/real/`:
raw per-request records for the experiments, run manifests for everything else. The
analysis scripts re-derive their numbers from those files, so any figure can be checked
without spending a cent. Where I got something wrong and corrected it, the correction is
in Appendix B rather than scattered through the text.

Total spend on `evertune-tests`: **$53.33 across 127,340 requests**. Run
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
ceiling actually is, and whether it belongs to us or to Google. It turned out to be ours,
and it took two more experiments and a careful read of Google's quota documentation to
establish that properly.

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
| `0` (off) | **$0.000374** | 1,527 ms | 145.3 | 0 |
| `-1` (default) | $0.001344 | 3,778 ms | 533.6 | 411.2 |

Turning it off is **3.60x cheaper** on this workload, 95% CI [3.00, 4.36], and **77% of
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
| Distinct category/brand pairs | 148 | 127 (-14%) |
| Brands in the informative 10-90% band | **81** | **57** (-30%) |

Flash-Lite is much cheaper and it resolves less. It finds 14% fewer brands outright,
and 30% fewer in the band where a mention rate is actually informative (section 2
explains why that band is the number that matters). The two models mostly agree on who leads, with
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

Data in `results/real/model/flash-lite-*`, compared against the temperature 1.0
cells of `results/real/measurement/temperature-multi-*`.

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

So a grounded request costs **95x** an ungrounded one. At 100 samples that's $3.54 per
prompt against $0.04.

### Which means most cost levers don't matter

This is the part I'd want a reviewer to sit with, because it inverts the obvious
analysis.

Every figure below comes from the paired n=20 grounding run, which measured both
conditions on the same prompts, so the shares are internally consistent rather than
stitched together from separate runs.

| Component of a two-condition bill | Cost | Share |
|---|---|---|
| Ungrounded tokens | $0.000412 | 1.14% |
| Grounded tokens | $0.000759 | 2.10% |
| **Grounding SKU** | **$0.035000** | **96.76%** |

Which turns every lever into a small number:

| Lever | Applies to | Most of the bill it can remove |
|---|---|---|
| Thinking off | both token arms | **3.2%** |
| Batch (50% off) | ungrounded tokens only, batch has no tool support | **0.6%** |
| Context caching | neither, floor is 2,048 input tokens, workload is 35 | **0%** |

Every token optimisation in this document fights over about 3% of the bill, and the
single biggest one is worth less than a rounding error on the grounding line. Batch
prediction can't run grounded requests at all, and implicit caching can't engage on a
35-token prompt. It needs 2,048 minimum, so the workload is 58x below the floor.

#### Could we pad the prompt to reach the cache floor?

Fair question, and 2,048 tokens isn't a lot. But the arithmetic says no, and it says so
without spending anything.

| Prompt | Cache hit rate | Cost/request | vs today |
|---|---|---|---|
| 35 tokens (today) | can't engage | $0.000373 | baseline |
| padded to 2,048 | 0% | $0.000977 | 2.62x |
| padded to 2,048 | 50% | $0.000700 | 1.88x |
| padded to 2,048 | 90% | $0.000479 | 1.28x |
| padded to 2,048 | **100%** | $0.000424 | **1.14x** |

Even a perfect cache hit rate loses. The cached rate is $0.03 per million against $0.30
uncached, a 10x discount, but padding multiplies the token count by 58. Ten times cheaper
on fifty-eight times as many tokens is 5.9x more money. You'd have to pay for the padding
forever to unlock a discount worth less than the padding.

Break-even sits at 350 input tokens. The floor sits at 2,048. So there's a dead band
between them where caching would pay for itself and Google won't let you use it.

**But this flips if the prompt grows on its own.** Evertune samples the same prompt 100
times, which is close to the ideal caching workload: one prefix, many hits, all within
the TTL. If a future prompt carries a long system message, few-shot examples or a brand
list and lands past 2,048 tokens naturally, implicit caching is already on and would take
**51% off the request at 2,048 tokens and 65% at 5,000**. Nothing to build, and no reason
to pad to get there.

Worth flagging as a trigger rather than an action: if the prompt ever crosses 2,048
tokens, the cost model in this section changes and should be re-derived.

Flash-Lite is the cleanest example. Switching models looks like an 11.5x win on token
prices. On the real two-condition workload it saves **1.6%** while losing 30% of the
informative measurement. Once a per-prompt SKU dominates, model selection stops being a
cost lever.

### Attaching the tool doesn't make the model refuse, but it does change the shape

During earlier probing one grounded request declined a question that the same model
answers happily with no tool attached. n=1, so it could have been nothing, but it was
worth settling. If attaching a tool changes the model's *willingness* to answer, then the
grounded-versus-ungrounded delta measures two things at once and Evertune's headline
number carries a confound.

It doesn't replicate. 50 paired prompts, same prompt to both arms, only the tool differs
(`results/real/measurement/tool-refusal-*`):

| | Refusals | Truncated | Mean answer | Mean output tokens |
|---|---|---|---|---|
| Tool attached | **0 / 50** | 22 (44%) | 1,599 chars | 373.8 |
| No tool | **0 / 50** | 2 (4%) | 660 chars | 166.0 |

Zero refusals either way. With 50 samples and nothing observed, the 95% upper bound on
the true rate is about 6%, so a common effect is ruled out. A rare one isn't, and I'd
rather say that than claim the question is closed.

The rest of the table is the real result. Attaching the tool makes the model **2.25x more
verbose in billed tokens** (2.42x in characters, and tokens are what you pay for), and
truncation goes from 4% to 44%. That's a much bigger operational problem
than a refusal would have been, and it's the same finding as the earlier grounding run,
now on a second sample. The grounded arm needs a larger output cap, not the same one.

One request in that run came back `MAX_TOKENS` with zero output tokens and still billed
the full $0.035 grounding charge before the retry succeeded. Worth knowing that a failed
grounded request costs the same as a successful one, so retries on the grounded arm are
95x more expensive than retries on the ungrounded arm.

### Grounding changes the answers, which is the whole point

One production unit: same prompt, 100 samples, both conditions, **$3.67**
(`results/real/measurement/production-unit-*`). That is $0.17 of tokens across all 200
requests and $3.50 of grounding SKU, which is the whole point of section 5.

Every brand whose interval excludes zero, not a selection:

| Brand | Ungrounded | Grounded | Delta | 95% CI |
|---|---|---|---|---|
| **Dreame** | 5 | **97** | **+92** | [+86, +97] |
| Ecovacs | 52 | 93 | +41 | [+30, +52] |
| Eufy | 65 | 99 | +34 | [+24, +44] |
| Narwal | 3 | 36 | +33 | [+23, +44] |
| Samsung | 6 | 33 | +27 | [+16, +37] |
| Shark | 56 | 80 | +24 | [+11, +37] |
| **Anker** | 18 | **3** | **-15** | [-23, -7] |
| **iRobot** | 100 | **85** | **-15** | [-22, -8] |
| Dyson | 4 | 19 | +15 | [+7, +23] |
| Xiaomi | 1 | 14 | +13 | [+6, +20] |
| Roborock | 100 | 90 | -10 | [-16, -5] |

Three more moved too little to call: Neato -3 [-11, +5], Roomba -2 [-5, +0], Deebot
+2 [-12, +16]. Reproduce the whole table with `python scripts/confidence.py`.

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
for, so it is written up in section 7 as production work rather than built here.

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

| Temp | Rate <5% | Middle | Rate >95% | In the 10-90% band |
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
them. They are not clamped here. 1.4 produced 142 distinct brand names against 1.0's 139, and
335 distinct answer sets against 294. It is doing something, it just is not finding
anything new.

(Two units are in play and they are easy to confuse. A brand *name* counted once
globally, and a *category/brand pair*, which counts Samsung in TVs separately from
Samsung in phones. The pair is the one that matters for a per-category share, so the
tables above use it.)

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

### Is 100 samples the right number?

I took n=100 as given, then went looking for whether it holds up. Evertune has published
the rationale: margin of error is "a massive 27 points" at 5 repetitions, 12 points at
12, and "about 6 points" at 100, with diminishing returns past that.

That reproduces. For a proportion at n=100 the 95% margin of error is:

| True mention rate | 95% margin of error |
|---|---|
| 5% | +/- 4.3 points |
| 10% | +/- 5.9 points |
| 30% | +/- 9.0 points |
| 50% | **+/- 9.8 points** |

So "about 6 points" is right for a brand sitting near 10%, which is where most brands in
a crowded category sit. The number to keep in mind is that it roughly doubles in the
middle of the range. A brand at 50% carries +/- 10 points at n=100, and two brands 8
points apart there are not distinguishable. That isn't an argument for more samples,
n=400 would only halve it, and the cost scales linearly while the error scales with the
square root. It's an argument for reporting the interval next to the number.

Which is the thread back to temperature. At temperature 0 the margin of error collapses
toward zero, because every brand lands at 0% or 100% and repeated sampling returns the
same answer every time. A measurement can have a tiny margin of error and still be
useless. Precision is not accuracy, and n=100 only buys something when the underlying
process actually varies.

### Logprobs are free and see what counting can't

Evertune already takes 100 samples, so the question isn't whether to sample less. It's
what logprobs add on top. Answer: they cost nothing (token counts are identical with
them on or off) and they see below the counting floor.

In a 100-sample run where iRobot won 97 times:

| First token | Reads as | Mean probability | In any sample? |
|---|---|---|---|
| `i` | iRobot | 0.9308 | yes, 97/100 |
| `Room` | Roomba | 0.0470 | yes, 3/100 |
| `Rob` | Roborock | **0.0183** | **no, 0/100** |
| `Shark` | Shark | **0.0023** | **no, 0/100** |

The middle column is my reading, not the API's. The prompt was constrained to return a
single brand name, so `Rob` is almost certainly Roborock, but the model emits tokens and
the mapping to a brand is an inference.

Roborock is a major brand in that category. It held 1.83% of the probability mass and
appeared in zero samples. Counting reports it as absent, which is indistinguishable from
a brand the model has never heard of. Those are very different findings, and only one is
true.

How many samples would counting need? Two thresholds, because they answer different
questions:

| | Roborock (1.83%) | Shark (0.23%) |
|---|---|---|
| 95% chance of seeing it once | 162 | 1,308 |
| Enough hits for a stable rate (~25) | 1,366 | 10,930 |
| Cost of the larger, ungrounded | $0.51 | $4.08 |

At Evertune's 100 samples, neither is reachable. The logprob is already in the response.

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
given. Tool presence changing *what the model will say* rather than just how it formats
would be a serious confound for a product whose measurement is the answer content, so I
went and tested it: 50 paired prompts, zero refusals in either arm. It did not replicate,
and section 2 has the numbers. What that run did find is that attaching the tool makes
answers 2.25x longer in billed tokens, which is a real problem and a different one.

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

`llm_pool_saturation_ratio` is in-flight / pool size, so it exceeds 1.0 when
oversubscribed. It moves long before the median does.

### Our layer costs about 2 ms, and it sheds instead of collapsing

Same workload, 50 rps for 30 s, through the service versus straight to the backend:

| Path | p50 | p95 | p99 |
|---|---|---|---|
| direct to backend | 401.7 ms | 515.9 ms | 553.0 ms |
| through our service | 403.6 ms | 515.7 ms | 565.3 ms |
| **difference** | **+1.9 ms** | -0.2 ms | +12.3 ms |

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

A second experiment in section 4 reaches the same conclusion by a different route: the
Python harness with no service in the middle, driven directly rather than through k6,
got 67 rps from one process and 307 from four at the same total concurrency. Different
rig, different load generator, same answer. Two independent measurements agreeing is
worth more here than either one alone, because the shared-capacity control in this
experiment is the kind of thing that is easy to get subtly wrong.

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

### It runs for 21 minutes without drama

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

**The throughput number is not that part.** 37 rps is what one Python process holding 64
requests in flight produces against a 1.4 second backend. It is a fact about my client
and my choice of concurrency, not a limit Vertex imposed, and later sections take that
apart properly: an off-peak re-run 32 hours later gave 31.9 rps, and k6 against the same
endpoint sustained 550. Read the table for stability and error behaviour. Everything it
implies about capacity is answered further down.

Quota is enforced per minute and a cold connection pool takes tens of seconds to warm,
so both facts set a floor on how long a capacity test has to run. Sub-minute runs
under-report by about 2.5x here. That's worth stating because burst-shaped benchmarks
are the norm and they systematically flatter the system.

**p99 is not settled** and should be quoted as a range, roughly 3.7-9.6 s. Fine for a
batch workload; anything with a tail SLA wants provisioned throughput instead of shared
quota.

### Retries are rare, visible, and not what I first said they were

Nine requests needed a second attempt in the first soak, 24 in the second. All 33
eventually succeeded and not one surfaced to a caller.

I originally headed this section "Vertex does rate limit," which the data does not
support. **No run in this document recorded a single 429**, across roughly 97,000 requests
spanning three soaks, a concurrency sweep and the k6 ceiling run. Those 33 retries are
transient failures of some kind, and the honest statement is that the per-request records
capture the retry count but not the reason, so I can't name it. That's a gap in my
instrumentation, not evidence about Google.

What the number does support is the design decision behind it. `llm/retry.py` retries in
our own code rather than delegating to the SDK's `HttpRetryOptions`, so retried failures
stay visible to instrumentation. With SDK retries enabled, those 33 would have been
invisible and the run would have looked perfectly clean. A 0.05% retry rate is a small
thing to know, but not knowing it is how a slow upstream degradation hides until it is
big enough to page someone. The fix for next time is one field: record the error class
that triggered each retry, not just the count.

The error taxonomy covers more than the obvious codes. **499 (`CANCELLED`) maps to a
retryable server error**, not a client error. Despite sitting in the 4xx range, it means
upstream shed the connection rather than that we sent something invalid. Treating it as
a 4xx would fail the request permanently on a condition worth another attempt. I never
saw one, so that mapping is reasoning from the spec rather than from measurement.

### Our client peaks at 128 concurrent, and past it the bottleneck is us

Vertex us-central1, 25-75 s measured per stage after discarding a warm-up window
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
range, which is Little's Law behaving (throughput = concurrency / latency, so at a fixed
latency each extra concurrent request should buy a fixed amount of throughput). p50
actually *improves* up to 128.

Flat latency across a 16x range is the tell. If Vertex were rationing anything, pushing
16x more concurrency at it would show up as rising latency or rejections, and neither
happens. Nothing is saturated in that top half of the table. I'm filling a pipe that has
spare room at both ends, and the throughput number is whatever I chose for concurrency
divided by the model's natural response time.

Then it collapses. At 1024 the throughput is below the c=64 level, p50 is 13x worse, and
p99 reaches 49 seconds. Pushing 8x harder than the optimum delivers 40% less work.

The last column explains all of it. The pool sat at 50% throughout, raised to 2,048 so
it couldn't be the constraint, while event loop lag went from under 5 ms to **4.3
seconds**. At c=1024, a quarter of the 17.5 s p50 is our own scheduler delay before a
request reaches the network.

So the answer to "is the ceiling us or them" is neither, and then us. Up to 128 nobody is
constrained: Vertex isn't pushing back and our client is idle. Past 128 the limit is a
single Python process, and Vertex still isn't the limiting factor. I never located a
vendor ceiling anywhere in this table, which the off-peak probe below confirms from a
different angle.

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

### Adaptive concurrency: it works, but the premise is unproven

`parallelism()` returning a constant assumes capacity is something discovered once.
Vertex uses Dynamic Shared Quota, which publishes no per-project ceiling and is
documented as moving with regional demand, so on paper any constant has a shelf life.
That was the reasoning. Whether it survives contact with measurement is the next
section.

I built a gradient limiter that keys on **latency** rather than error codes, because
Vertex often doesn't reject excess load. It just slows down. A controller watching only
429s sees a healthy service and keeps climbing.

Three configurations against a mock whose capacity collapses mid-run and then recovers
(`results/real/local/adaptive-experiment.txt`):

| Phase | Config | rps | p50 | p99 | Errors |
|---|---|---|---|---|---|
| Healthy | fixed-high (64) | 356.2 | 239 ms | 818 ms | 50 |
| Healthy | adaptive | 181.2 | **163 ms** | **548 ms** | 23 |
| Degraded | fixed-high (64) | 15.7 | 3,139 ms | 6,291 ms | **1,468** |
| Degraded | adaptive | **19.2** | **169 ms** | 4,207 ms | **5** |
| Recovered | fixed-high (64) | 357.6 | 241 ms | 1,453 ms | 51 |
| Recovered | adaptive | 215.6 | **161 ms** | **693 ms** | 31 |

Across all three phases: 1,569 errors on the fixed cap against 59 on adaptive.

I first wrote this up as "half the throughput for fewer errors," which misreads the
table twice.

The degraded row is the point. Adaptive is ahead on *both* axes there, more throughput
and 294x fewer errors, because the fixed cap spends its capacity on requests that come
back as failures. Holding 64 in flight against a backend that can't serve them doesn't
produce 64 answers. It produces a queue and a p50 of 3.1 seconds.

The healthy-phase gap needs a caveat I couldn't give it when I first wrote this section.
356 rps is a number the mock made up, and at the time the highest I had seen from Vertex
was 73.7 rps, so I read adaptive's 181 as comfortably above anything real. That was wrong,
and the k6 run later showed Vertex taking 550 rps without complaint. Against a backend
that fast, adaptive's healthy-phase throttling would be giving up real work.

One number in that file needs a caveat: adaptive shows 64,488 shed requests in the
healthy phase. That is a property of the test, not a forecast. k6 was offering load
open-loop with no ceiling, so everything above the limit gets rejected. In production the
only client is our own harness and we control its arrival rate.

**It still ships off by default**, and the reason is the row that isn't in the table. The
capacity collapse was simulated by reconfiguring the mock. That's a fair test of the
controller's dynamics and a poor test of the premise, because it assumes the thing it was
built for.

So I went and tested the premise.

### Vertex was never the constraint, and I can now say by how much

My first two soaks ran thirty minutes apart on a Monday afternoon in US hours. That's no
test of whether capacity moves. I re-ran the identical configuration 32 hours later at
05:37 UTC, which is the middle of the night in the US and about as far from the first
runs' demand conditions as I can get without waiting for a holiday
(`results/real/capacity/vertex-dsq-offpeak-*`):

| Run | When (UTC) | rps | p50 | p99 | Truncated | Rate limits |
|---|---|---|---|---|---|---|
| soak | Mon 20:58 | 35.6 | 1,401 ms | 7,033 ms | 3.34% | **0** |
| soak-long | Mon 21:28 | 36.9 | 1,379 ms | 6,324 ms | 3.26% | **0** |
| off-peak | Wed 05:37 | 31.9 | 1,464 ms | 8,182 ms | 3.62% | **0** |

Two things, and the second one matters more.

The ceiling moved 14% across a 32-hour gap spanning peak and overnight, and it moved the
*wrong way*. Off-peak was slower, not faster. If Dynamic Shared Quota were handing out
spare regional capacity at 5am, this run should have been the fastest of the three. It
was the slowest. Whatever that 14% is, it isn't demand-driven quota.

**And there were zero rate-limit errors in any of them.** Not a low number, zero, across
roughly 70,000 requests. Every single "error" in those runs is a `MAX_TOKENS` truncation
at the 512 cap, which is a formatting problem, not a capacity one.

Which means the 36.9 rps I've been calling a ceiling isn't a ceiling. Look at what
throughput actually equals here:

| Run | Concurrency / mean latency | Measured rps |
|---|---|---|
| soak | 64 / 1.627 s = 39.3 | 35.6 |
| soak-long | 64 / 1.592 s = 40.2 | 36.9 |
| off-peak | 64 / 1.754 s = 36.5 | 31.9 |

Throughput is just concurrency divided by latency, within about 10%. That's Little's Law
doing exactly what it says, and it means the number was set by how many requests I chose
to hold in flight, not by anything Google imposed. The concurrency sweep agrees: 73.7 rps
at c=128 is 128 divided by roughly the same latency.

So I measured our own client's behaviour and reported it as the vendor's limit. The real
Vertex ceiling is somewhere above where I stopped, and I stopped because our TLS
handshake path fell over at c=256, not because Vertex pushed back.

So I measured our own client's behaviour and reported it as the vendor's limit.

### So I pointed k6 at Vertex and went looking for the wall

Our Python client can't answer this question. One event loop saturates its TLS path
around 128 requests in flight, so it tops out near 74 rps and stops. k6 is Go, holds
thousands of concurrent requests without breaking a sweat, and talks to Vertex directly
with no service in the middle. That control arm was in the repo from the start. It had
only ever run as a ten-request smoke test, which is a fair criticism of the work rather
than of the tool.

A ramp to 550 requests per second, output capped at 64 tokens so the bill stayed bounded,
`evertune-tests`/us-central1 (`results/real/capacity/k6-vertex-ceiling.json`):

| | |
|---|---|
| Peak offered rate | **550 rps**, held 20 s |
| Requests | 26,743 |
| **Rate limits (429)** | **0** |
| Failed requests | **0.000%** |
| Dropped iterations | 0 |
| p50 / p95 / p99 | 803 ms / 1,081 ms / 1,380 ms |
| Cost | $3.85 |

Nothing broke. Vertex took 550 requests a second and about 29,000 output tokens a
second without a single rejection, and p99 stayed under 1.4 seconds the whole way up.

**Which means I did not find Vertex's limit. I found the number I typed into the
config.** The ramp's top stage says `target: 550`, so k6 dispatched 550 and stopped. That
is worth being blunt about, because a table full of zeroes can read like a discovery when
it is really an absence.

Three things say the generator wasn't the constraint either:

| Signal | Value | What it rules out |
|---|---|---|
| `dropped_iterations` | **0** | k6 delivered every scheduled request |
| VUs in use | 429 of 700 | 39% headroom in the pool |
| `http_req_blocked` | 0.92 ms avg | almost no waiting for a connection slot |

And `http_req_waiting` averaged 811 ms, which is Vertex thinking. That single number is
99.9% of the response time. Nothing on my side was working hard.

To put a floor under the rig itself I ran the calibration scenario against the mock, which
ramps to 4,000 rps for free: **57,941 requests, zero dropped iterations, p99 81 ms**. So
k6 on this machine delivers a 4,000 rps schedule without complaint. The Vertex run asked
it for 550, about 14% of that.

So the honest reading of the table is a floor, not a ceiling. Vertex sustained at least
550 rps. Its actual limit is somewhere above, and I stopped because I had answered the
question that mattered, not because Google made me.

Put that next to our own numbers and the picture is unambiguous:

| Path | Peak | 429s |
|---|---|---|
| Python client to Vertex | 73.7 rps | 0 |
| **k6 to Vertex** | **550 rps** (as configured) | **0** |

Same endpoint, same project, same region, same day. **7.5x more throughput from a
different client.** The limit I spent three sections characterising is ours.

Two more caveats. The 64-token cap keeps latency and cost down, so this bounds a request
rate rather than a token-per-minute quota; production answers run about 145 tokens, and
the same token throughput would be reached at roughly 240 rps. And a 95-second ramp says
nothing about a sustained hour.

The practical consequence for Evertune: a report refresh of 20,000 requests is about 36
seconds of Vertex's time. Everything I measured before this was our single Python process
queueing in front of a vendor that was idle.

### Why I stopped ramping, and what to plan with instead

The obvious next step was to keep climbing until Vertex said no. I priced it at about $4
with a small output cap. I didn't run it, and the reason is worth more than the number
would have been.

Google's on-demand tier doesn't ration by requests per second. It rations by **tokens per
minute**, at the organisation level, and the baselines are published:

| Tier | Org spend, rolling 30 days | Baseline TPM, Flash |
|---|---|---|
| 1 | $10 to $250 | 2,000,000 |
| 2 | $250 to $2,000 | 4,000,000 |
| 3 | above $2,000 | 10,000,000 |

The docs are explicit that "there's no separate requests-per-minute (RPM) limit for each
tier." So a rate in requests per second isn't the unit the system meters in, and hunting
for one measures the wrong thing.

Convert our run and it gets more interesting. It measured 34.2 input and 53.4 output
tokens per request, so 550 rps is **2.89M TPM against a Tier 1 baseline of 2M**. We were
already running at 1.45x the published floor, on best-effort burst, and Vertex accepted
all of it without a single rejection. The run I already paid for is a better data point
than the one I was about to buy.

Three more reasons the ramp would have produced a number I couldn't use.

**A ramp measures the ramp.** Google documents an acceleration limit separate from
capacity: "You may encounter 429 errors because of acceleration limits if your project has
a sharp increase in usage. To avoid hitting acceleration limits, ramp up your traffic
gradually." A ramping load test is a sharp increase in usage by construction. It would
find its own shape.

**A 429 isn't a ceiling.** Also documented: "If you receive a 429 error, it doesn't
indicate that you've hit a fixed quota. It indicates temporary high contention for a
specific shared resource." So the rejection point moves with what every other customer in
the region is doing.

**And it isn't a promise.** 429s on pay-as-you-go are explicitly excluded from the SLA
error rate. Google's answer to "guarantee me throughput" is Provisioned Throughput, sold
in Generative AI Scale Units on a fixed term. That's the only number anyone commits to.

So here's the planning table, derived from published baselines and our measured token
shape (34.5 in, 145.3 out, measured at n=100) rather than from a load test:

| Tier | Sustained rps | Processes needed at 74 rps each | 20,000-request refresh |
|---|---|---|---|
| 1 | 185 | 2.5 | 1.8 min |
| 2 | 371 | 5.0 | 0.9 min |
| 3 | 927 | 12.5 | 0.4 min |

Every row finishes a report refresh in under two minutes. Capacity is not the interesting
problem here, which is the useful conclusion, and it cost nothing to reach.

### One thing I got wrong by not reading closely enough: the endpoint

Everything in this document ran against `us-central1`, and I never justified that. It was
the default I picked on day one.

Google's published TPM baselines are stated for the **global** endpoint, which "dynamically
routes your requests to the region with the most available capacity at that moment,"
giving access to a multi-region pool and "significantly increasing your potential for
successful bursting and reducing the likelihood of 429 errors." A regional endpoint ties
you to one region's spare capacity.

My own region comparison, small and made for another purpose, points the same way:

| Endpoint | p50, thinking off | p50, thinking dynamic |
|---|---|---|
| `global` | **1,329 ms** | **3,339 ms** |
| `us-central1` | 1,471 ms | 4,106 ms |

Global was faster in both arms. I'd treated that as a curiosity about regional load.
Reading the quota docs makes it look more like the routing working as advertised.

**Recommendation: default to the global endpoint** unless data residency requires
otherwise, which is a question for whoever owns that policy rather than for me. The
provider takes `location` from configuration, so this is a one-line change and no code.
I've left the default at `us-central1` so every measurement in this document stays
comparable, and flagged it here rather than quietly switching it underneath the evidence.

### So what is the constraint, exactly?

"It's us" is not an answer anyone can act on. Four runs narrow it down, and three of them
were free.

**Take TLS out of the picture.** Same Python client, same concurrency points, but pointed
at the mock over plain HTTP with its latency tuned to match Vertex's ~1.4 s
(`results/real/local/notls-sweep-manifest.json`):

| Concurrency | No TLS (mock) | With TLS (Vertex) |
|---|---|---|
| 64 | 39.4 rps | 36.1 rps |
| 128 | 76.7 rps | 73.7 rps |
| 256 | **147.0 rps** | **63.0 rps** |
| 512 | 67.0 rps | (not run) |

The two columns track each other until 256, where the TLS run falls off a cliff and the
plaintext run keeps scaling. So TLS roughly halves the concurrency one process can carry.
It moves the knee from about 512 down to about 256, and that is the cost of doing
handshakes and record encryption on the same thread that dispatches requests.

**But TLS isn't the whole story**, because the plaintext run collapses too, just later.
Something else caps a single process around 256 to 512 in flight.

**Rule out the mock.** It's Python too, so it could have been the thing that broke. k6
against the same server, same 1.4 s latency profile, offered 400 rps for 30 s
(`results/real/local/k6-mock-611-concurrent.json`): it achieved **361 rps with a p50 of
1,354 ms**, ramping to 583 VUs, and the mock's own counter recorded a peak of **611
concurrent requests in flight**. Our client managed 67 rps at 512 concurrent against that
same server. The mock is not the bottleneck.

**So the ceiling is per process.** Same total concurrency of 512, same backend, same
machine, only the number of processes changes
(`results/real/local/multiprocess-experiment.json`):

| | Throughput | p50 | p99 |
|---|---|---|---|
| 1 process at c=512 | 67 rps | 6,008 ms | 13,206 ms |
| **4 processes at c=128 each** | **307 rps** | **1,380 ms** | ~2,850 ms |

**4.6x the throughput and 4.4x better p50 from the same 512 concurrent requests.** Each
process turned in 74 to 78 rps, which is the single-process c=128 figure repeated four
times. It scales linearly because nothing is shared.

So the constraint is one Python process's event loop, and it has two parts. TLS crypto is
CPU work on that loop and costs about half the usable concurrency. Underneath that, the
loop itself runs out of dispatch capacity somewhere past 256 in flight, TLS or not. Both
are per process, and Python runs one thread of bytecode at a time, so neither is fixed by
threads or bigger connection pools. The pool experiment already showed that: it sat at
50% while throughput collapsed.

The number to plan with is **roughly 74 rps per process against Vertex**. Want 550? That's
about 8 processes, and the earlier worker test agrees. Vertex has already shown it will
take that and more from a single machine.

That also settles the adaptive limiter, and for a better reason than "we never saw a
429." Google's own guidance for this tier is exponential backoff, traffic smoothing, and
the global endpoint. A gradient controller that infers a capacity ceiling from latency is
solving a problem the vendor says isn't shaped that way: contention is transient, the
limit is measured in tokens per minute, and a sharp ramp trips an acceleration limiter
that has nothing to do with capacity. Retry with backoff and admission control are already
in the provider. The limiter stays off, and if throughput is ever the issue the answer is
more processes, not a cleverer client.

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
create. How long that takes is a deployment choice rather than a vendor limit:

| | Wall clock for one report |
|---|---|
| 1 process, as measured in the soak | 9.0 min |
| 4 processes | 1.1 min |
| Capped by a Tier 1 token baseline | 1.9 min |

Even the slowest row is minutes, and the token baseline binds before anything in our
control does. This is the scenario the admission control and retry budget exist for, but
it isn't a capacity problem.

It is also **$357**, of which $350 is the grounding SKU.

**Scheduled is where the money is**, and cadence dominates everything else. Refresh
frequency is the assumption I am least sure of, so here is the whole range rather than
one number. 200 reports, 100 prompts each:

| Cadence | Grounded/yr | Ungrounded/yr | Batch saves |
|---|---|---|---|
| Daily | $25,822,728 | $272,728 | $136,364 |
| Weekly | $3,678,854 | $38,854 | $19,427 |
| **Monthly** | **$848,966** | **$8,966** | **$4,483** |
| Quarterly | $282,989 | $2,989 | $1,494 |

Two things fall out of that table.

**Cadence moves cost far more than any optimisation in this document.** Daily versus
monthly is a 30x swing. Every engineering lever here, thinking off, Batch, caching,
model tier, put together cannot move the number that much. If cost is a concern, the
first conversation is about refresh frequency, not about tokens.

**Batch only ever touches the ungrounded side.** Batch prediction does not support
tools, so grounded requests run online at full rate no matter how patient the caller is.
At monthly cadence the Batch saving is about $4,500 a year against a grounded bill of
$849,000. Worth taking, since it is nearly free to implement, but it is not a strategy.

*(By "arm" I mean one of the two conditions. The ungrounded arm is every request made
with live search off, the grounded arm is the same prompts with it on.)*

#### A sanity check on my own assumptions

Those numbers rest on prompt counts I made up, so I looked for something public to check
them against. Evertune's published material says the platform runs "millions of prompts a
day" across "11+ AI models."

Taking that at face value and splitting evenly across models, with half of each model's
traffic in the search-augmented arm:

| Total volume | Per model/day | Grounded arm, Gemini alone | Ungrounded arm |
|---|---|---|---|
| 1M requests/day | 90,909 | $580,682/yr | $6,188/yr |
| 3M requests/day | 272,727 | $1,742,045/yr | $18,565/yr |

My modelled monthly-cadence figure was $848,966. It lands between those two, which is
about as much agreement as you can expect from two sets of guesses. The assumptions
aren't verified, but they're not wild either.

The ratio is the part that doesn't move. Grounded costs 94x ungrounded at every scale in
that table, because both sides scale linearly and the per-prompt SKU dominates. That
conclusion holds whatever the real prompt count turns out to be, which is the useful
thing about it.

One number is worth sitting with. If the entire grounded arm ran through the paid
grounding SKU at a million requests a day, that's **$12.8M a year**, against a company
that has raised about $20M. So either the real per-model volume is well below that, or
the search-augmented layer is sourced somewhere other than the billed grounding API.
Evertune's own methodology writing points at the second: it describes the search layer as
coming from consumer app surfaces and the API as the way to isolate base-model knowledge.

That matters for this integration. If Vertex grounding is only ever used for the
foundational arm, or for spot checks rather than the full 100 samples, the cost picture in
this section is far less alarming than the headline suggests. It's the first question I'd
ask before optimising anything.

The $35/1k grounding rate is verified against Google's billing catalog, so that is not
the weak link. The assumptions worth challenging are prompts per report, number of
reports, and above all cadence.

### The token levers, for the arm where they apply

| Configuration | $/request | vs naive |
|---|---|---|
| interactive, dynamic thinking (the defaults) | 0.00134435 | 1.0x |
| thinking off | 0.00037360 | 3.6x |
| **thinking off + Batch** | **0.00018680** | **7.2x** |

At 50,000 ungrounded prompts/day that's $24,534/yr down to $3,409/yr. Same work, same
model, 14% of the bill.

These use 145.3 output tokens, the n=100 measurement. Ungrounded output ran 111 to 166
tokens across five different prompt corpora, a 1.4x spread on wording alone, so read the
unit costs as the middle of a range. The ratios are stable, the absolute dollars are not.

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
parameter is keyword-only with a default of `False`. A feature costing 95x per request
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

**Implement Batch for the scheduled ungrounded arm.** Worth $4,483 a year at monthly
cadence and $19,427 at weekly, against a grounded bill of $849,000. Nearly free to
implement and worth taking, but it is housekeeping rather than a strategy, and it has a
clearly bounded home: scheduled refreshes only, never ad-hoc, never grounded.

**Decide the grounding cadence.** At $357 per report the interesting question stops being
"how fast can we serve this" and becomes "does every prompt need the grounded condition,
and how often does a report need refreshing." Worth more than every engineering lever
here combined.

**Set truncation policy per condition.** 1,536 for grounded, 512 for ungrounded. And
decide deliberately whether truncated answers are dropped or retried, `is_usable`
currently discards them, which is safe but silently reduces sample count.

**Run more than one process, and size it from the token baseline.** One process holds
about **74 rps** against Vertex. Four processes gave 307 rps in a controlled test, scaling
linearly because nothing is shared. The published Tier 1 token baseline works out to about
185 rps on the production token shape, so roughly **3 processes reach the point where
Google's metering binds instead of ours**. That is the number to size against, not a load
test.

**Move to the global endpoint.** Google's published throughput baselines are quoted for
it, it routes to whichever region has capacity, and the small region comparison here
already favoured it on latency. One configuration change, no code. Worth confirming
against whatever data residency policy applies.

---

## 8. Open questions

**Does the grounded arm need 100 samples?** 100 is settled for the ungrounded condition.
Grounded answers carry retrieval variance on top of generation variance, so they're
noisier, not quieter. But nobody has told me the sampling policy has to match across
conditions, and at 95x per request that's the largest lever left.

**Does the global endpoint change the numbers?** Everything here ran on `us-central1`.
Google routes the global endpoint to whichever region has capacity, and the published
throughput baselines are quoted for it. Re-running the soak against `global` would say
whether that shows up as throughput or only as fewer rejections under contention.

**Does retrieval variance move brand share?** Grounded answers vary and retrieval varies.
Separating "the model changed its mind" from "different pages came back" needs the same
unit re-run days apart.

**Does temperature interact with prompt wording?** One prompt template, English only. A
differently-phrased question could sit at a different point on that curve.

**How does this compare to another vendor's model?** The wiring exists, `--provider
together` is already a flag on the harness, and the run itself is trivially cheap. A
Together account needs a $5 minimum top-up and 200 requests at this workload's token
counts costs about four cents on Llama 3.3 70B, or nothing at all on one of their
zero-priced serverless models. Cost isn't the reason I stopped.

Two things are. Together has **no first-party web search**, which I checked rather than
assumed: their own documentation for building a search-augmented app wires in a
third-party search API and passes the results into the prompt. So the arm that carries
97% of the bill and most of the interesting behaviour has no counterpart there. Any
comparison I ran would be ungrounded against ungrounded, which is the cheap half of the
measurement and the half least likely to differ operationally.

The second is a scope judgement. Evertune tracks 11+ models and treats each as its own
target, so "how does Llama answer this" is a question about their product surface, not
about whether this Gemini integration holds up. I'd rather hand over one provider I've
measured properly than two I've sampled.

If it's wanted, it's an afternoon and a $5 top-up, and the harness already emits
comparable manifests for both.

---

# Appendix A: Evidence, and how to check it

## Where each number came from

Every number carries one of four labels. I kept them separate rather than presenting
harness output as a vendor measurement.

**Measured.** Live requests against Vertex AI, project `evertune-tests`, `us-central1`
unless a section says otherwise. Real model, real billing, real failure modes.

**Validated.** Produced against the fake Vertex endpoint in `mock/`, which exercises the
full HTTP path, the real SDK and the real connection pool at zero cost. Used for two
things: mechanism proofs where the vendor is deliberately held constant, and failure
modes that a real vendor won't produce on demand. A validated number describes our code,
never Google's capacity.

**Verified.** Checked against an authoritative external source rather than measured by
me. Only the pricing rates are in this class, and they come from Google's Cloud Billing
Catalog API, which is the data invoices are generated from.

**Modelled.** Arithmetic on measured unit costs plus a stated assumption. The annual
projections are the only ones, and section 5 lists which inputs are Evertune's and which
are mine.

One thing worth saying plainly, since the early work ran on a personal Gemini Developer
API key while GCP access was pending. Nothing in the table below rests on it. Everything
that touches cost, capacity or latency was re-run against `evertune-tests` once the
project was live, because those numbers don't transfer between endpoints.

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
| Client peaks at c=128 | measured | `capacity/vertex-*` |
| No quota wall found, 0 rate limits | measured | `capacity/vertex-dsq-offpeak-*` |
| Tool attached does not cause refusals | measured | `measurement/tool-refusal-*` |
| HTTP/2 | measured | `capacity/vertex-http2-*` |
| Adaptive limiter | validated | `local/adaptive-experiment.txt` |
| Vertex takes 550 rps, 0 rejections | measured | `capacity/k6-vertex-ceiling.json` |
| Rig itself does 4,000 rps | validated | `local/k6-rig-calibration.json` |
| TLS halves per-process concurrency | validated | `local/notls-sweep-manifest.json` |
| Ceiling is per process, not per host | validated | `local/multiprocess-experiment.json` |
| Pricing rates | verified | Cloud Billing Catalog API, `scripts/verify_pricing.py` |
| Annual cost projections | modelled | measured unit cost x stated cadence, section 5 |

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
the interval to [3.00, 4.36], which is finally usable.

The lesson I'd keep: a point estimate with no interval hides how little it settles, and
n=15 against a quantity with a standard deviation of 223 tokens was never going to
settle anything.

I'd also written that the ratio couldn't be bootstrapped because manifests store
per-stage totals. That was wrong; the per-request ledgers were sitting in the same
directory the whole time. `scripts/confidence.py` now derives it from those.

---

# Appendix B: What I got wrong, and the check that would have caught it

Six numbers in this document changed after I first wrote them down. The individual
mistakes are not very interesting. The pattern is, because five of the six are the same
one: I had a figure from somewhere convenient and I didn't go to the source.

| What I claimed | What's true | What I skipped |
|---|---|---|
| Grounding is $25/1k prompts | **$35/1k**, free tier 1,500 not ~5,000 | Querying the billing catalog |
| Pool p50 of 4,162 ms | That was the mean. p50 is 516 ms | Re-deriving from raw records |
| Neato vanishes when grounded | 11 vs 8, not significant | Recounting instead of reading a truncated summary |
| p99 climbs 37% over a soak | Oscillates 3.7 to 9.6 s, no trend | Running long enough to see a shape |
| Context caching saves ~1.02x | Impossible here, needs 2,048 input tokens | Checking the minimum before modelling |
| Thinking costs 4.0x | 3.60x here, 38.5x on a terse prompt | Asking whether the ratio was portable |

Each one cost minutes to check and I checked none of them until something forced it.

**The grounding rate is the clearest case.** I carried $25 per 1,000 grounded prompts
through four sections, hedged with "verify against an invoice," and shipped a draft that
way. Then I queried Google's Cloud Billing Catalog API, the same data the invoices are
generated from, and got $35. Every grounded figure moved up 40%. The same query confirmed
all four token rates to the cent, so one API call corrected one number and validated four
others. The authoritative source was cheaper to consult than the approximation I used
instead of it. That's now `scripts/verify_pricing.py`, and it exits non-zero on a
mismatch, so the next rate change fails a check rather than sitting in a document.

**Two of them only surfaced because I was doing something else.** The mean-labelled-as-p50
came out of re-running the pool experiment to produce committed evidence for a table that
had none. The Neato story came out of computing confidence intervals, which forced a
recount from the raw counter and revealed that my script's `most_common(12)` had been
printing 0 for every brand below twelfth. I had written a tidy paragraph tying Neato's
absence to the company's 2023 bankruptcy. Clean, plausible, and about a display bug.

Neither was caught by review or by rereading. Both were caught by regenerating the number
from source for an unrelated reason, which is an uncomfortable thing to notice about your
own process.

**The thinking multiplier is the one that would have travelled furthest.** I reported 4.0x
from n=15. Bootstrapping that sample afterwards gives a 95% interval of [2.36, 7.42],
which is not a measurement, it's a rumour with a decimal point. Re-running at n=100 gave
3.60x [3.00, 4.36]. But the useful correction isn't the tighter number. It's that a
verbosity test showed the same setting costing 38.5x on a terse prompt, because the ratio
is roughly (thinking + answer) / answer and therefore governed by how long the answer
would have been anyway. I was about to hand someone a constant that was actually a
property of my prompt. What transfers is the share, about 77% of billed output is
reasoning.

**And one was unfalsifiable from the start.** I modelled context caching at a 1.02x saving
without checking that implicit caching needs 2,048 input tokens. The workload sends 35.
The effect isn't small, it's structurally impossible. The number was tiny enough that it
never looked worth verifying, which is precisely how a wrong assumption reaches a summary
table: it doesn't matter enough to check, so nobody checks it, so it stays.

The habit I'd take forward is narrow. Any number I'm about to put in front of someone else
either comes with an interval, a committed file it can be regenerated from, or a named
source I actually queried. Three of the six above would have failed that test on sight.

## Then I stopped trusting myself and checked all of them

Six corrections in two days is a rate, not an accident, so I went back and re-derived
every headline figure in this document from the raw records rather than re-reading the
prose. Re-reading had already failed to catch any of the six, which makes sense: a stale
number reads exactly like a fresh one.

That pass found five more.

**The cost model was built on a sample I'd publicly retired.** Every dollar figure in
section 5 traced back to an n=15 run whose confidence interval I quote three paragraphs
above as an example of a sample too small to use. It was still quietly the basis for the
annual projections, understating the ungrounded unit cost by 30% and reporting the
grounded multiplier as 123x when the n=100 data says 95x. Rebased.

**The bootstrap wasn't reproducible.** `scripts/confidence.py` takes a seed, so I assumed
it was deterministic. It iterated a *set* of brand names, and Python randomises string
hashing per process, so the resamples were drawn in a different order on every run and
the published intervals moved by a point or two each time. Three runs, three answers, all
from the same seed and the same data. One `sorted()` fixed it.

**A confidence table was showing its top 8 rows.** Exactly the shape of the truncation
bug that produced the Neato story, in the script written to prevent that class of
mistake. Anker's interval was quoted in the prose while the script that produced it
never printed Anker.

**Two unit slips.** A "$3.14 per sample" figure that reconciled against no rate I have
ever used, and a brand count that silently switched between distinct names and
category/brand pairs between adjacent paragraphs.

**And I made a seventh live, then caught it.** Recomputing the production unit's cost, I
added the grounding SKU to per-request costs that already included it and produced $6.17
for a run that cost $3.67. I'd written the corrected figure into the document before the
arithmetic stopped agreeing with itself.

The fix is `scripts/verify_findings.py`. It re-derives 66 figures from the committed
records and exits non-zero when the document disagrees, so drift fails a check instead of
sitting in prose. It caught one on its first run: a p50 quoted with a different estimator
than every other latency number here.

`make verify` runs it. It found the seventh error faster than I did, which is the point.

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
