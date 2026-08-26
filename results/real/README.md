# Measurement data

Every number in `FINDINGS.md` comes from a file in here. Four folders, by what the run
was trying to learn.

Each experiment writes two things: a `*-manifest.json` with the aggregates, percentiles,
time-series windows and cost reconciliation, and where the analysis needs it, a
`*.jsonl` with one line per request.

Manifests are committed for every run. Per-request records are committed for the
experiments, since the analysis scripts re-derive their results from them. Per-request
records for the load runs are not: that is 20 MB of one line per request for throughput
tests whose manifests already contain every derived figure.

## `model/`: how Gemini itself behaves

| File | What it measured |
|---|---|
| `think-off-n100-*`, `think-dyn-n100-*` | Thinking on vs off, n=100 each. The 3.6x cost ratio. |
| `thinking-verbosity.json` | The same setting on a terse prompt vs ours. Why 3.6x is not portable. |
| `think-cap512-*` | Thinking starving the output cap. HTTP 200 with no answer. |
| `uscentral-tb0-*`, `uscentral-tb-1-*` | The original n=15 thinking runs, us-central1. |
| `vertex-tb0-*`, `vertex-tb-1-*` | Same, `global` region. Shows cost is region-portable. |
| `real-tb0-*`, `real-tb-1-*` | Same, Developer API. Earliest runs. |
| `review-probes-*` | Thinking default with no config set. Tools alongside grounding. |
| `flash-lite-*` | 2.5 Flash-Lite across 11 categories, for the model comparison. |

## `measurement/`: properties of the brand-tracking method

| File | What it measured |
|---|---|
| `grounding-*` | Grounded vs ungrounded, 20 prompts, paired. |
| `production-unit-*` | One prompt, 100 samples, both conditions. The real unit of work. |
| `temperature-multi-*` | 11 categories x 5 temperatures x 60 samples. The main sweep. |
| `temperature-2026*` | The earlier single-category pilot. |
| `structured-output-*` | `responseSchema` vs prose, and whether it works with grounding. |
| `logprobs-experiment.json` | What logprobs see that 100 samples miss. |

## `capacity/`: Vertex under sustained load

| File | What it measured |
|---|---|
| `vertex-soak-long-*` | 47,677 requests over 20.8 minutes. The headline soak. |
| `vertex-soak-*` | The shorter 8.7-minute soak that it replicates. |
| `vertex-knee-*` | Concurrency 8 to 128. Where throughput stops scaling. |
| `vertex-extreme-*` | Concurrency 256 and 1024. Where it collapses. |
| `vertex-http2-*` | HTTP/2 at the same concurrencies. A negative result. |
| `vertex-sweep-*`, `vertex-sweep2-*` | Early exploratory sweeps. |

## `local/`: runs against the mock, or pure analysis

| File | What it measured |
|---|---|
| `pool-experiment.txt` | Connection pool size vs throughput and latency. |
| `adaptive-experiment.txt` | Adaptive limiter vs fixed caps against collapsing capacity. |
| `cost-model.txt` | Cost projection output. |
| `spend.txt` | Spend ledger snapshot. |

## Re-deriving the numbers

```bash
python scripts/confidence.py            # bootstrap intervals on the headline ratios
python scripts/temperature_analysis.py  # the temperature sweep, re-analysed
python scripts/spend_report.py          # what was spent, by account
```

None of these issue a request.

## Late additions

`measurement/tool-refusal-*` settles whether attaching the search tool changes the
model's willingness to answer. It doesn't. 50 paired prompts, zero refusals in either
arm. What it does change is verbosity: 2.3x longer answers and truncation going from 4%
to 44%.

`capacity/vertex-dsq-offpeak-*` is the same configuration as `vertex-soak-long`, re-run
32 hours later at 05:37 UTC instead of Monday afternoon. It exists to test whether
Dynamic Shared Quota moves with regional demand. It does not appear to: off-peak was
14% slower rather than faster, and no run in this folder has ever recorded a rate-limit
error.

## Finding the actual ceiling

`capacity/k6-vertex-ceiling.json` is the run that answers "how much will Vertex take?".
550 requests per second, zero rejections, zero failed requests. It does not find Vertex's
limit, it finds the top of the ramp I configured, which is a different thing and the file
says so.

Three supporting runs, all free, narrow down what the real constraint is:

- `local/k6-rig-calibration.json` puts a floor under the test rig. k6 on this machine
  delivers a 4,000 rps schedule with no dropped iterations, so at 550 rps it was coasting.
- `local/notls-sweep-manifest.json` runs the Python client against the mock over plain
  HTTP with latency tuned to match Vertex. Removing TLS moves the collapse from about 256
  concurrent to about 512.
- `local/k6-mock-611-concurrent.json` rules out the mock as the bottleneck: k6 holds 611
  concurrent against it at 400 rps with a clean p50.
- `local/multiprocess-experiment.json` is the one that matters. The same 512 concurrent
  requests give 67 rps through one process and 307 rps through four.
