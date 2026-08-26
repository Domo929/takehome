# Findings

I added Gemini 2.5 Flash on Vertex AI as a provider, then spent most of my time trying
to break it. This is what I learned, in roughly the order I learned it.

One note before the results. Everything here is backed by data in `results/real/`: raw
per-request records for the experiments, run manifests for everything else. Nothing is
typed by hand. `make verify` re-derives all 86 headline figures from those files and
exits non-zero if this document disagrees with its own data, so any number here can be
checked in a few seconds without spending a cent. Appendix B covers how that came about
and what it caught.

Total spend on `evertune-tests`: **$55.44 across 128,494 requests**. Run
`python scripts/spend_report.py` for the breakdown.

---

## Environment

| | |
|---|---|
| CPU | AMD Ryzen AI 7 350, 8 cores / 16 threads, boost 5.09 GHz |
| Memory | 30 GB |
| OS | Fedora Linux 44, kernel 7.1.8 |
| Python | 3.14.7, `google-genai` 2.19.0 |
| Load generator | k6 v2.2.0 |
| Network to `us-central1` | 8.8 ms RTT, 0% loss |
| Host layout | k6, the service and the mock all ran on this one machine |

| | |
|---|---|
| Implementation, experiments, write-up | Claude Opus 5 |
| Adversarial review | Gemini 3 Pro, plus a second Opus 5 pass |
| Period | 3 working days, 69 commits |
| Diff against the provided repo | 128 files, ~34,400 lines |

---

## Where to find what you asked for

It is a long document, so here is the map. The left column is the brief.

| You asked for | It's in |
|---|---|
| How the integration behaves under realistic load | [4. Pointing it at Vertex](#4-what-i-learned-pointing-it-at-vertex), and [3](#3-what-i-learned-load-testing-our-own-code) for our own code first |
| Quirks and failure modes of this model | [1. What I learned about the model](#1-what-i-learned-about-the-model) |
| Parameters that mattered | Thinking budget and output cap in [1](#1-what-i-learned-about-the-model), temperature in [2](#2-what-i-learned-about-the-measurement) |
| What surprised me vs other LLMs | [What surprised me, measured against the provider already in this repo](#what-surprised-me-measured-against-the-provider-already-in-this-repo) |
| Decisions and tradeoffs | [6. Changing the provided contract](#6-changing-the-provided-contract), plus the reasoning inline where each decision was made |
| Things I tried that didn't work | HTTP/2 in [4](#our-client-tops-out-at-74-rps-per-process-and-tls-is-half-of-why), the adaptive limiter in [4](#adaptive-concurrency-it-works-and-it-ships-off), structured output in [2](#structured-output-does-not-work-where-it-is-needed-most), context caching in [5](#could-we-pad-the-prompt-to-reach-the-cache-floor) |
| What I'd do next in production | [7. What I'd do before production](#7-what-id-do-before-production) |
| What I'd want to know first | [8. Open questions](#8-open-questions) |
| The numbers behind all of it | [Appendix A](#appendix-a-evidence-and-how-to-check-it), and `make verify` re-derives all 86 |
| How the numbers were checked | [Appendix B](#appendix-b-how-the-numbers-were-checked) |

If you only read one section, read [5. What it costs to run](#5-what-it-costs-to-run).
It's the finding most likely to change a decision.

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

**Build it and test it locally.** The integration runs as an HTTP service and k6
drives it. I also wrote a fake Vertex endpoint, which is where most of the iteration
happened: free, and it will produce failures on demand that Google won't.

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

### What surprised me, measured against the provider already in this repo

`llm/together.py` shipped with the exercise and it is OpenAI-shaped, so it is a fair
statement of what this codebase already assumes an LLM provider looks like. Every line of
it has a Gemini equivalent that looks similar and behaves differently. That gap is where
my time went.

**`response.choices[0].message.content` is a string. Gemini's answer is a list that can
be empty on a 200.** The text lives in `candidates[0].content.parts[]` and has to be
joined, and a response can arrive with 200, no error and no parts at all. The SDK's
`.text` property *raises* on some blocked payloads rather than returning empty, so the
happy path has two ways to produce nothing while looking fine. Hence
`LLMEmptyResponseError` rather than a `None` the caller never asked for.

**`response.usage.completion_tokens` is one number. Gemini has two, and the bill is the
sum.** `candidates_token_count` is the obvious analogue, and it is the wrong one:
`thoughts_token_count` is billed at the same output rate and is invisible in the answer.
On the default configuration that is 411 of 534 output tokens, so reading the obvious
field understates the bill by **3.6x**. Nothing errors. The number just comes out low, and
it stays low until an invoice disagrees.

**Together's `finish_reason` is available and the shipped code ignores it, which is
survivable there and is not here.** Gemini truncates at the output cap, and truncation is
silent: the answer looks like a shorter answer. It ran 3.3% on ungrounded traffic and 44%
once a search tool was attached. Without checking `finish_reason`, a brand-tracking
pipeline quietly measures whichever brands happened to fit in the token budget.

**`temperature` passes straight through in both, and only one of them cares.** At
temperature 0 Gemini stops being able to express a share at all: 0 of 103 brands landed
between 10% and 90%. The parameter looks identical in both APIs and is a measurement
decision in one of them.

**`parallelism()` returns a constant, and Vertex publishes no per-project ceiling to put
in it.** Capacity is drawn from a shared regional pool and metered in tokens per minute
at the organisation level. There is no per-project QPS number to look up and no quota
increase to request. Both of those are things an OpenAI-shaped integration expects to
find.

**And one thing has no analogue at all.** Grounding is a separate SKU billed per prompt,
not per token, at 94x the token cost of the request it attaches to. Every cost intuition
carried over from a token-priced API is wrong by two orders of magnitude on that arm. It
is also why Batch and context caching, the two obvious levers, do nothing here.

Two smaller ones worth recording because they cost me time. One field inside
`thinkingConfig` serialises in snake_case while every sibling is camelCase, and sending
both spellings is a hard 400 rather than a merge. And 499 `CANCELLED` needs a deliberate
decision: I first classified it retryable on the reasoning that upstream had shed the
connection, but Google's error table defines it as client cancellation and their retry
guidance covers only 408, 429 and 5xx. It is terminal by default now, with an override,
because I never observed one and reasoning against a documented contract is a bad trade.

The through-line: Gemini's failure modes are mostly **HTTP 200 responses that are wrong in
a way the type system cannot see.** An OpenAI-shaped client ported field by field compiles,
runs, passes a smoke test, and misreports cost and truncation in production. That is what
`tests/test_gemini.py` exists to pin.

## 2. What I learned about the measurement

The gap between the two conditions is the product. Everything below is about what that
shape does when you scale it: what the second condition costs, what temperature does to
a number that is supposed to be a share, and how big a change has to be before it means
anything.

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

The obvious analysis is backwards here.

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
| Context caching | neither today, floor is 2,048 input tokens and the prompt is 35 | **0%** |

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

**But this flips if the prompt grows on its own**, and that half I did measure rather
than assume (`results/real/model/context-cache-*`). Two arms, same prefix repeated, 20
requests each:

| Arm | Mean input tokens | Requests with a cache hit | Cached per hit | Input saving |
|---|---|---|---|---|
| Above the floor | 2,933 | **18 / 20** | 2,038 tokens | **56.3%** |
| Below the floor | 621 | **0 / 20** | 0 | 0% |

Caching engages, it engages only above the floor, and the discount is real: 56% off the
input side. The two misses in the top arm are the first request, which warms the prefix,
and one later blip. Nothing to build, and no reason to pad to get there.

The below-floor arm is the part that makes this a measurement rather than an anecdote.
Without it, "we saw caching" and "we saw caching *because* the prompt was long enough"
look identical.

Worth flagging as a trigger rather than an action: if the prompt ever crosses 2,048
tokens, this section's cost model changes and should be re-derived. Evertune samples one
prompt 100 times, which is close to the ideal shape for caching, so the discount would
land immediately.

*(First attempt at this got zero hits in both arms and I nearly wrote it up as "caching
does not engage." The prefix had come out at 1,641 tokens, under the floor, because I
sized it with the usual four-characters-per-token rule and this prose runs closer to
five and a half. The script now asserts the arm cleared the floor and says so loudly if
it did not, because a mis-calibrated run and a real negative look the same.)*

One thing is deliberately not on that list: **the model**. Section 1 has the argument,
but the short version is that a cheaper model is a different measurement, not a cheaper
way to take the same one.

Every lever that *is* a lever lives inside a single model, and on this workload they all
share the same 3%.

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

Dreame shows up in 5% of ungrounded samples and 97% of grounded ones.

The ungrounded condition isn't a degraded version of the grounded one. It measures
something real and separately useful: what the model believes when nobody corrects it.

### Two rows in that table are wrong, and one has the wrong sign

I wrote "Anker falls as Eufy rises, which tracks, Anker's robot vacuums are sold under
the Eufy brand," and then left both in the table as separate findings. Spotting the
relationship and not applying it is worse than missing it.

Roomba is iRobot's product line. Deebot and Yeedi are Ecovacs'. Eufy is Anker's. The
extractor counts each string separately, so the table measures **strings, not companies**.
Resolving product lines to their parent:

| Entity | Reported delta | After resolution | |
|---|---|---|---|
| iRobot | **-15** (significant) | **-2** | not a finding |
| Anker | **-15** (significant) | **+34** | **sign flips** |
| Ecovacs | +41 | +36 | holds |

The Anker row matters most. As reported it loses 15 points when grounding is on. As a
company it gains 34, because 99% of grounded answers name Eufy. Report the first number
and you tell Anker their visibility collapsed in a quarter when it doubled.

This isn't an extractor bug, it's a missing step. In the ungrounded arm iRobot and Roomba
appear together in **100 of 100** answers, so summing them gives a 200% share, which is
visibly nonsense. Eufy and Anker never separate either: every Anker mention is inside a
Eufy mention.

Entity resolution is presumably solved somewhere in Evertune's pipeline, since it's core
to the product. I'm raising it because nothing in the provider contract carries it, and a
provider-level measurement that skips it produces confidently wrong numbers rather than
obviously broken ones. Worth confirming which layer owns it.

For this document I've left the table as measured and flagged the two affected rows,
rather than silently applying a mapping I invented. The mapping itself is a business-data
question, not an engineering one.

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

### How big does a change have to be before it's real?

Splitting each 60-sample cell into independent halves and measuring per-brand rate
drift gives about **5 points** at temperature 1.0. That is a real number and it is the
wrong threshold, for three reasons. It's a **mean**, and a threshold needs a tail. It was
measured at **n=30 per half**, while production samples 100. And it's one number when the
noise depends on where the brand sits.

Simulating two independent samples of the same brand at the same setting, which is the
question a "did this move?" alert is really asking:

| True mention rate | n | Mean gap by chance | 95th percentile |
|---|---|---|---|
| 10% | 30 | 6.1 pts | 16.7 pts |
| 10% | **100** | 3.4 pts | **8.0 pts** |
| 30% | **100** | 5.2 pts | **13.0 pts** |
| 50% | **100** | 5.6 pts | **14.0 pts** |

So at production sample sizes the threshold is **8 points for a niche brand and 14
for a mid-share one**, not 5. My original figure would have called a 6-point move real.
For a brand sitting near 50%, one run in three moves further than that with nothing
changing at all.

The shape of the answer matters more than the number. Noise is largest in the middle of
the range, which is exactly the band where the metric is informative, so the brands worth
watching are the ones hardest to call. A single global threshold is the wrong instrument.
Reporting each rate with its interval costs nothing, since the sample size is already
known at collection time, and it removes the question entirely.

Reproduce with `python scripts/noise_floor.py`.

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

So "about 6 points" holds for a brand near 10%, where most brands in a crowded category
sit. It roughly doubles in the middle of the range: at 50% the margin is +/- 10 points, so
two brands 8 points apart are not distinguishable. More samples won't fix that, since cost
scales linearly and error with the square root. Report the interval instead.

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
structure costs **1.54x output tokens**, so it hits the cap sooner: at 200 tokens, prose
truncated 0 of 10 and schema 5 of 10. All 5 were *detected* though, because the JSON
failed to parse. That's rare silent failures traded for frequent loud ones, and a cap
that needs to rise about 1.5x.

One thing I wasn't looking for. The tool in question was a single function declaration,
`record_brands`, described as "Record the brands mentioned, with sentiment", taking an
array of name/sentiment objects. Nothing about it asks the model to stop answering.

With it attached and grounding off, the model declined a question it answers freely
otherwise:

> "I can't answer that, as I cannot make specific product recommendations. I can,
> however, record any brands you are considering, along with your sentiment toward them."

Read the reply against the tool description and the mechanism is visible: it has adopted
the tool's job description as its own role. "Record the brands mentioned" became the
thing it does, and answering the question became something it does not.

`finish_reason=STOP`, no safety block. It reinterpreted its role around the tool it was
given. Tool presence changing *what the model says* rather than how it formats would be a
serious confound for a product measured on answer content, so I tested it: 50 paired
prompts, zero refusals either arm. It didn't replicate. What that run did find is that
attaching the tool makes answers **2.25x longer in billed tokens**, which is a real
problem and a different one. Section 2 has the numbers.

---

## 3. What I learned load testing our own code

The thing that has to survive production traffic is our service, not Vertex. So k6
drives it over HTTP the way real traffic would: separate process, separate runtime, no
shared event loop to flatter the numbers.

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

Section 4 reaches the same conclusion by another route: the Python harness with no
service in the middle got 67 rps from one process and 307 from four, at the same total
concurrency. Different rig, different generator, same answer. Worth more than either
alone, because the held-capacity control here is easy to get subtly wrong.

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

Four things came out of this section, and the rest is evidence for them.

1. Sustained load is boring: 47,677 requests over 21 minutes, 0.05% retries, nothing surfaced to a caller.
2. **Every throughput number I measured was a fact about our client, not about Vertex.** No run in this document has ever seen a 429.
3. Our ceiling is one Python event loop, worth about 74 rps per process. TLS costs roughly half of it.
4. On the real production shape, 100% of served requests produced a usable sample, and what sheds is driven by the grounded latency tail rather than by the mean.

### The soak: stable and unremarkable

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
on retry rate. That is the part I would stake a production decision on. **The throughput
number is not**, for reasons the next section gets to.

Three operational notes. Quota is enforced per minute and a cold pool takes tens of
seconds to warm, so sub-minute runs under-report by about 2.5x here; burst-shaped
benchmarks systematically flatter the system. **p99 is not settled** and should be quoted
as a range, roughly 3.7 to 9.6 s, which is fine for batch and wrong for a tail SLA. And
all 33 retries across both runs succeeded on a later attempt without reaching a caller.

Those retries are not rate limits. **No run in this document recorded a single 429**,
across roughly 97,000 requests spanning three soaks, a concurrency sweep and the k6
ceiling run. What they were I can't say, because the per-request records capture the retry
count but not the reason. That is a gap in my instrumentation rather than a fact about
Google, and the fix is one field.

It does justify the design, though. `llm/retry.py` retries in our own code rather than
delegating to the SDK's `HttpRetryOptions`, so retries stay visible. With SDK retries on,
those 33 would have been invisible and the run would have looked perfectly clean. A 0.05%
retry rate is a small thing to know, but not knowing it is how slow upstream degradation
hides until it pages someone.

### Our client tops out at ~74 rps per process, and TLS is half of why

Vertex us-central1, 25-75 s per stage after discarding warm-up
(`results/real/capacity/vertex-knee-*`, `vertex-extreme-*`):

| Concurrency | Throughput | rps per unit | p50 | p99 | Event loop lag | Pool |
|---|---|---|---|---|---|---|
| 8 | 4.2 rps | 0.525 | 1,534 ms | 7,571 ms | ~0 ms | 25% |
| 32 | 17.2 rps | 0.537 | 1,473 ms | 8,049 ms | ~0 ms | 25% |
| 64 | 36.1 rps | 0.565 | 1,410 ms | 4,133 ms | <5 ms | 25% |
| **128** | **73.7 rps** | **0.575** | **1,328 ms** | **3,813 ms** | **<5 ms** | 50% |
| 256 | 63.0 rps | 0.246 | 1,962 ms | 10,818 ms | **457 ms** | 50% |
| 1024 | 43.7 rps | 0.043 | 17,557 ms | 49,443 ms | **4,301 ms** | 50% |

Linear to 128, then it collapses: at 1024, throughput is below the c=64 level, p50 is 13x
worse, and pushing 8x harder than the optimum delivers 40% less work.

Flat latency across a 16x range is the tell. If Vertex were rationing anything, 16x more
concurrency would show up as rising latency or rejections. Neither happens, so nothing is
saturated in the top half of that table. The last column explains the collapse: the pool
sat at 50% throughout (raised to 2,048 so it couldn't be the constraint) while event loop
lag went from under 5 ms to **4.3 seconds**. At c=1024, a quarter of the 17.5 s p50 is our
own scheduler delay before a request reaches the network. Without
`llm_event_loop_lag_seconds` that reads as "Vertex collapses under load," which is both
wrong and the kind of wrong that gets escalated to a vendor.

Four experiments isolate the cause, three of them free.

**TLS costs about half the usable concurrency.** Same client against the mock over plain
HTTP, latency tuned to match Vertex (`results/real/local/notls-sweep-manifest.json`):

| Concurrency | No TLS (mock) | With TLS (Vertex) |
|---|---|---|
| 128 | 76.7 rps | 73.7 rps |
| 256 | **147.0 rps** | **63.0 rps** |
| 512 | 67.0 rps | (not run) |

The columns track until 256, where the TLS run falls off a cliff and plaintext keeps
scaling. So TLS moves the knee from about 512 down to about 256, which is the cost of
handshakes and record encryption on the thread that dispatches requests.

**HTTP/2 confirms the diagnosis and doesn't fix it.** It fixed the symptom precisely,
event loop lag from 4,301 ms to 2 ms, but throughput got worse: 55.2 rps at c=1024 against
73.7 at c=128 on HTTP/1.1. Fewer connections means less transport-layer parallelism, and
that costs more than the handshakes save. Shipped off behind `GEMINI_HTTP2`, kept as
evidence rather than as a recommendation.

**The mock isn't the bottleneck.** It is Python too, so it could have been. k6 against the
same server at 400 rps offered achieved **361 rps at p50 1,354 ms**, ramping to 583 VUs,
with the mock's own counter peaking at **611 concurrent**
(`results/real/local/k6-mock-611-concurrent.json`). Our client managed 67 rps at 512
concurrent against that same server.

**So the ceiling is per process.** Same 512 total concurrency, same backend, same machine,
only process count changes (`results/real/local/multiprocess-experiment.json`):

| | Throughput | p50 | p99 |
|---|---|---|---|
| 1 process at c=512 | 67 rps | 6,008 ms | 13,206 ms |
| **4 processes at c=128 each** | **307 rps** | **1,380 ms** | ~2,850 ms |

**4.6x the throughput and 4.4x better p50 from the same 512 requests.** Each process
turned in 74 to 78 rps, the single-process c=128 figure repeated four times, scaling
linearly because nothing is shared.

So the constraint is one Python event loop in two parts: TLS crypto costs about half the
usable concurrency, and underneath that the loop runs out of dispatch capacity past 256 in
flight regardless. Both are per process, so neither is fixed by threads or a bigger pool.
**Plan on roughly 74 rps per process.** Want 550? About 8 processes.

### What Vertex actually takes

Everything above measures us. To measure Google I needed a client that isn't ours, so I
pointed k6 straight at Vertex with no service in between. That control arm was in the repo
from the start and had only ever run as a ten-request smoke test.

A ramp to 550 rps, output capped at 64 tokens to bound the bill
(`results/real/capacity/k6-vertex-ceiling.json`):

| | |
|---|---|
| Peak offered rate | **550 rps**, held 20 s |
| Model generations | 26,743 |
| HTTP requests, including one token fetch per VU | 27,443 |
| **Rate limits (429)** | **0** |
| Failed requests | **0.000%** at the HTTP layer |
| Answers that finished cleanly | **28.8%**, the rest hit the 64-token cap |
| Dropped iterations | 0 |
| p50 / p95 / p99 | 803 ms / 1,081 ms / 1,380 ms |
| Cost | $3.85 |

| Path | Peak | 429s |
|---|---|---|
| Python client to Vertex | 73.7 rps | 0 |
| **k6 to Vertex** | **550 rps** (as configured) | **0** |

Same endpoint, same project, same day: **7.5x more throughput from a different client.**

Two honest limits on that number. **"0% failed" means HTTP, not usable**, since 19,038 of
26,743 answers hit the 64-token cap. The cap was deliberate and truncation doesn't affect
whether Vertex accepts load, but this is evidence about admission and throughput only.
And **550 is a lower bound, not a ceiling**: the ramp's top stage says `target: 550`, so
k6 dispatched 550 and stopped. The generator wasn't the constraint either, with zero
dropped iterations, 429 of 700 VUs in use, 0.92 ms average `http_req_blocked`, and
`http_req_waiting` at 811 ms which is 99.9% of the response time. Separately, k6 on this
machine delivers a 4,000 rps schedule against the mock with zero drops, so at 550 it was
coasting.

I also re-ran the soak configuration 32 hours later at 05:37 UTC to test whether capacity
moves (`results/real/capacity/vertex-dsq-offpeak-*`). It gave 31.9 rps against 36.9, so
throughput varies by about 14%, and slower off-peak rather than faster. Two runs can't
carry a claim about why, and all three sat at concurrency 64, nowhere near any vendor
limit. **No run here observed contention, and none was designed to.**

Throughput in all three is just concurrency divided by latency, within about 10%:

| Run | Concurrency / mean latency | Measured rps |
|---|---|---|
| soak | 64 / 1.627 s = 39.3 | 35.6 |
| soak-long | 64 / 1.592 s = 40.2 | 36.9 |
| off-peak | 64 / 1.754 s = 36.5 | 31.9 |

That is Little's Law doing exactly what it says. The number was set by how many requests I
chose to hold in flight.

**So I stopped ramping**, and read the quota documentation instead. Google's on-demand
tier doesn't ration by requests per second. It rations by **tokens per minute** at the
organisation level, and the baselines are published:

| Tier | Org spend, rolling 30 days | Baseline TPM, Flash |
|---|---|---|
| 1 | $10 to $250 | 2,000,000 |
| 2 | $250 to $2,000 | 4,000,000 |
| 3 | above $2,000 | 10,000,000 |

The docs say plainly that "there's no separate requests-per-minute (RPM) limit for each
tier," so an rps ceiling isn't the unit the system meters in. The run I already paid for
converts better than the one I was about to buy: at 34.2 input and 53.4 output tokens,
550 rps is **2.89M TPM against a Tier 1 baseline of 2M**, already 1.45x the published
floor on best-effort burst, accepted without a rejection.

Three more reasons a ramp would produce an unusable number. Google documents an
**acceleration limit** separate from capacity and warns that sharp usage increases trip
it, which a ramping load test is by construction. A **429 is documented as transient
contention**, not a fixed quota, so the rejection point moves with what other customers
are doing. And 429s on pay-as-you-go are **excluded from the SLA error rate** entirely;
Provisioned Throughput is the only thing anyone commits to.

So the planning table comes from published baselines and measured token shapes. It has to
use the blended shape, because production runs both arms and a grounded answer is 3.9x
longer (549 output tokens against 120, same prompt):

| Arm | Tokens/request |
|---|---|
| Ungrounded | 150.6 |
| Grounded | 580.1 |
| **Blended 50/50, as production runs it** | **365.3** |

| Tier | Sustained rps | Processes at 74 rps each | 20,000-request refresh |
|---|---|---|---|
| 1 | **91** | 1.2 | 3.7 min |
| 2 | **182** | 2.5 | 1.8 min |
| 3 | **456** | 6.2 | 0.7 min |

Even the entry tier finishes a report refresh in under four minutes, so capacity is not
the interesting problem. But at Tier 1 the vendor's token budget binds before our process
count does, which inverts the sizing advice for a small deployment: one process is enough,
and the lever is the tier rather than the fleet.

One config note. Everything here ran against `us-central1`, the region the project was set
up in. Google's published baselines are quoted for the **global** endpoint, which routes to
whichever region has capacity. My own small region comparison points the same way, 1,329 ms
against 1,471 ms p50 with thinking off, and 3,339 against 4,106 with it on. **Worth trying
global**, unless data residency rules it out. It's a config change and no code, and I left
the default alone so every measurement here stays comparable.

### The production shape, end to end

Everything above tests one piece at a time. This runs what Evertune would actually run:
through our service, both conditions mixed, production caps, one prompt sampled repeatedly
(`results/real/capacity/k6-production-shape.json`). k6 to `service/app.py` on 4 uvicorn
workers to real Vertex, 9 rps offered for two minutes, 50/50 grounded, cap 1,536.

| | |
|---|---|
| Offered / served | 1,109 / 1,074 |
| **Shed with 503** | **35 (3.2%)** |
| Rate limited by Vertex | 0 |
| Grounded share achieved | 49% |
| Grounding silently degraded | **0** |
| Truncated at 1,536 | **0** |
| **Usable samples** | **100.0%** |
| Cost | $0.76 |

**Every served request produced a countable sample**: no truncation, no silent
degradation, no answer arriving in the wrong condition. That is what the earlier runs
could not show, since they measured HTTP success on an arm truncating 71% of the time.
It also confirms the 1,536 cap from section 1.

The 3.2% that never got in is the finding.

| | |
|---|---|
| End-to-end p50 / p90 / p99 | 8,179 ms / 20,182 ms / **39,058 ms** |
| Our overhead p50 | **0.38 ms** |
| Queue wait p99 | **0 ms** |
| Vertex p50 | 8,593 ms |

Our overhead is a third of a millisecond and the queue never formed, so shedding is
admission control working rather than a system falling over. The tail drove it:

| At 9 rps and this latency | Concurrent requests needed |
|---|---|
| p50, 8.2 s | 74 |
| p90, 20.2 s | 182 |
| **p99, 39.1 s** | **352** |

Capacity was 128, so the mean fits and the tail does not. **Grounded latency has a tail
long enough that mean-based capacity planning under-provisions by 3x.** Size a mixed
workload on the grounded p99, roughly 5x more headroom than the average suggests. The
ad-hoc burst case in section 5 is where this bites, because a report kicked off by a click
arrives all at once against exactly this profile.

What it does not show: two minutes is not a soak, 1,074 requests is not a report, and 9 rps
was chosen to bound cost. Vertex never pushed back, at 138k tokens per minute or about 7%
of the entry-tier baseline.

### Adaptive concurrency: it works, and it ships off

`parallelism()` returning a constant assumes capacity is discovered once. Vertex uses
Dynamic Shared Quota, which publishes no per-project ceiling and is documented as moving
with regional demand, so on paper a constant has a shelf life. I built a gradient limiter
keyed on **latency** rather than error codes, because Vertex often doesn't reject excess
load, it just slows down, and a controller watching only 429s sees a healthy service and
keeps climbing.

Three configurations against a mock whose capacity collapses mid-run and recovers
(`results/real/local/adaptive-experiment.txt`):

| Phase | Config | rps | p50 | p99 | Errors |
|---|---|---|---|---|---|
| Healthy | fixed-high (64) | 356.2 | 239 ms | 818 ms | 50 |
| Healthy | adaptive | 181.2 | **163 ms** | **548 ms** | 23 |
| Degraded | fixed-high (64) | 15.7 | 3,139 ms | 6,291 ms | **1,468** |
| Degraded | adaptive | **19.2** | **169 ms** | 4,207 ms | **5** |
| Recovered | fixed-high (64) | 357.6 | 241 ms | 1,453 ms | 51 |
| Recovered | adaptive | 215.6 | **161 ms** | **693 ms** | 31 |

1,569 errors on the fixed cap against 59 on adaptive, across all phases. The degraded row
is the point: adaptive wins on *both* axes there, because a fixed cap holding 64 in flight
against a backend that can't serve them produces a queue and a 3.1-second p50, not 64
answers. The healthy-phase gap is the real cost, since Vertex did take 550 rps, so a
limiter settling at 181 leaves genuine throughput on the table when nothing is wrong.
(The 64,488 shed requests in that file are an artifact of k6 offering open-loop load with
no ceiling, not a forecast.)

**It ships off.** The capacity collapse was simulated by reconfiguring the mock, which
tests the controller's dynamics and assumes its premise. And the premise looks wrong:
Google's guidance for this tier is backoff, traffic smoothing and the global endpoint, and
contention is documented as transient rather than as a ceiling to discover. A controller
hunting a quota wall solves a problem four runs and 97,000 requests give no evidence for.
If throughput is ever the issue, the answer is more processes.

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

One number stands out. The entire grounded arm through the paid SKU at a million
requests a day is **$12.8M a year**, against a company that has raised about $20M. So
either real per-model volume is well below that, or the search-augmented layer is sourced
elsewhere. Evertune's own methodology writing points at the second, describing the search
layer as coming from consumer app surfaces and the API as the way to isolate base-model
knowledge.

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

**Pin temperature and re-baseline once.** The code now uses 1.0; historical shares were
collected at 0.7. Across 157 category/brand pairs, 7 move by more than their own 95%
noise band, the largest by 28 points. Not a wholesale shift, but a stored series spanning
the change shows real steps on those brands that are config artifacts, not market moves.
One re-baseline run per prompt, then freeze the value and record it with every result.

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

**Set truncation policy per condition.** 1,536 for grounded, 512 for ungrounded. The
provider takes one cap per instance, so running both arms at the right cap means two
provider instances or a per-request override, and today it is neither.

And decide deliberately what a truncated answer is worth. `is_usable` discards them in
the harness but not at the service boundary: `/ask` returns a truncated answer as HTTP
200, because it is not empty and nothing raises. It now carries
a `usable: false` flag so a caller can tell, but the policy question is still open and
belongs to whoever owns the pipeline. Dropping them reduces the sample count silently;
keeping them lets a fragment like `"iRobot,"` count as a mention.

**Size from the token baseline, not a load test.** One process holds about **74 rps**;
four gave 307, scaling linearly because nothing is shared. But Tier 1's token baseline
works out to 91 rps on the blended production shape, since grounded answers are 3.9x
longer. So at the entry tier **Google's budget binds before our process count does** and
one process is enough. Tier 3 takes about six.

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
assumed: their own docs for building a search-augmented app wire in a third-party search
API. So the arm carrying 97% of the bill has no counterpart there, and any comparison
would be ungrounded against ungrounded, the cheap half and the half least likely to
differ.

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

The first two days ran on my own key while GCP access was pending. Nothing in the table below rests on it. Everything
that touches cost, capacity or latency was re-run against `evertune-tests` once the
project was live. Throughput and latency belong to an endpoint, not to a model.

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
4.0x without an interval. The interval turns out to be **[2.36, 7.42]**. That is not
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

# Appendix B: How the numbers were checked

Every figure in this document is re-derived from committed data by
`scripts/verify_findings.py`, and a mismatch fails the check rather than sitting in the
prose. That tooling exists because working numbers drift, and this appendix records what
drifting looked like here and what caught it.

## What the checks caught

Working figures changed as better evidence arrived. Most were fine when written and went
stale later; a few were wrong from the start.

| Figure | Settled at | What resolved it |
|---|---|---|
| Grounding rate | $25/1k to **$35/1k** | Google's billing catalog API |
| Pool latency | mean relabelled as **p50 516 ms** | Re-derived from per-request records |
| Neato grounded delta | **not significant** (11 vs 8) | Recount after a display truncation |
| p99 trend over a soak | **no trend**, 3.7 to 9.6 s | A 20-minute run instead of 8 |
| Context caching | **cannot engage** below 2,048 input tokens | Checking the documented minimum |
| Thinking multiplier | **3.60x here**, 38.5x on a terse prompt | Re-running at n=100 and varying the prompt |
| Noise floor | **8 to 14 points**, not 5 | Simulating the threshold instead of quoting a mean |
| Brand deltas | two rows change, **one flips sign** | Resolving product lines to parent companies |
| Cost model basis | rebased from **n=15 to n=100** | Tracing which run each figure came from |

Two are worth a paragraph because the lesson generalises.

**The grounding rate.** I carried $25 per 1,000 grounded prompts through four sections,
hedged with "verify against an invoice." One query to Google's Cloud Billing Catalog API,
which is the data invoices are generated from, returned $35 and moved every grounded
figure up 40%. The same call confirmed all four token rates to the cent. The
authoritative source cost less to consult than the approximation cost to hedge. It's now
`scripts/verify_pricing.py` and it exits non-zero on a mismatch.

**The Anker row.** The table reported Anker down 15 points when grounded, one sentence
after noting that Anker's vacuums are sold as Eufy. Resolving the product line to its
parent makes it **up 34**. Spotting a relationship and not applying it produces a number
that is confidently wrong rather than obviously broken. It's in section 2 with the data,
because it says something about the measurement.

## What that changed about the tooling

Three habits, each of which came from one of the above:

**Numbers are generated, not typed.** `scripts/verify_findings.py` recomputes 72 figures
from the raw records, including the anchor links and the cited file paths. The pool
latency and the Neato count both surfaced only when something forced a regeneration from
source, so now everything regenerates on demand.

**Intervals travel with estimates.** The thinking multiplier read as a constant at n=15;
bootstrapping it gives [2.36, 7.42], which is not a measurement. `scripts/confidence.py`
puts an interval on anything load-bearing, and that is what surfaced the Neato recount.

**External rates are queried, not remembered.** `scripts/verify_pricing.py` checks all
five rates against Google's catalog on every run.

Most of these share a failure mode: a number that was right when written stays in the
document after the thing it described moves. Nothing errors, and it reads exactly like a
number that is still true. Regenerating from source is the only defence I know of that
does not depend on remembering.

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
