# Runbook

Everything here runs against a fake endpoint at zero cost until you deliberately
point it at a real one. Read the cost section before switching targets.

## Setup

```bash
make venv          # .venv + dependencies
make test          # 10 tests, no network, no spend
```

Requires Python 3.11+ (validated on 3.14.7), Docker for the observability stack, and
[k6](https://grafana.com/docs/k6/latest/set-up/install-k6/) v1+ for the control
harness (validated on v2.2.0).

## The two harnesses

| | Subject | Control |
|---|---|---|
| What | `harness/run.py` driving `llm/gemini.py` | `loadtest/k6/gemini.js` |
| Runtime | Python, asyncio, shared httpx pool, GIL | Go, no GIL, independent pool |
| Loop | closed or open | open only (arrival-rate executors) |
| Purpose | measures the code we ship | independent reference for the same endpoint |

The subject is what production runs, so its ceiling is the product's ceiling. The
control exists to answer a question the subject cannot answer alone: when throughput
plateaus, is the constraint our client or the vendor? If the control keeps climbing
where the subject flattens, the bottleneck is ours. If both knee at the same arrival
rate, it is Vertex. Running only one harness leaves that ambiguous, and the
literature on this problem is mostly people guessing.

## Quick start against the fake endpoint

```bash
make mock-up       # fake Vertex on :8088
make obs-up        # Prometheus :9090, Grafana :3000 (anonymous admin)
make sweep-mock    # closed-loop sweep, metrics on :9464
make k6-constant   # k6 control at 20 rps, remote-writes to Prometheus
```

Grafana has three dashboards under the *Takehome* folder: **Live Run**, **Cost &
Burn**, and **Subject vs Control**.

## Cost controls

Spending happens against someone else's cloud project, so the guards are mechanical
rather than advisory:

1. **Dry run is the default.** Every harness invocation prints a projected cost and
   exits. Nothing is sent without `--confirm`.
2. **Pre-flight refusal.** If the estimate already exceeds `--budget-usd`, the run
   refuses to start.
3. **Runtime breaker.** Before every dispatch, accumulated *actual* spend (from
   reported `usage_metadata`, never estimated) is checked against the ceiling. When it
   trips, the run drains in-flight requests and stops. Overshoot is bounded by
   concurrency × cost-per-request, because requests already in flight cannot be
   recalled.
4. **`max_output_tokens` is always set**, so a runaway generation cannot escalate.
5. **`thinking_budget=0` by default**, since thinking tokens bill at the output rate.

Reconciliation is printed at the end of every run: estimated versus actual, with the
error percentage. A consistently wrong estimator is itself worth knowing about.

```
Cost reconciliation
  requests completed  1,500
  input tokens        20,955
  output tokens       267,806 (of which thinking: 0)
  ACTUAL COST         $0.6783
  budget remaining    $4.3217
  estimate error      -12.0% (estimated $0.7707)
```

## Pointing at a real endpoint

Two backends are supported. They are **not** interchangeable for capacity work:
different quota pools, different endpoints, different scaling. Numbers from the
developer API are a functional smoke test, not scale evidence.

**Vertex (the production target).**

```bash
gcloud auth application-default login
export GEMINI_BACKEND=vertex
export GOOGLE_CLOUD_PROJECT=<project>
export GOOGLE_CLOUD_LOCATION=global
unset GEMINI_BASE_URL          # or it keeps talking to the mock

.venv/bin/python -m harness.run --mode closed --concurrency 8 \
    --requests 50 --budget-usd 0.50 --metrics-port 9464   # dry run
# add --confirm to actually spend
```

**Gemini Developer API (for iteration on a personal key).**

```bash
export GEMINI_BACKEND=developer
export GOOGLE_API_KEY=<key>
unset GEMINI_BASE_URL
```

For k6 against real Vertex, start the token sidecar first — k6 has no Google
credential chain:

```bash
.venv/bin/python loadtest/k6/token-refresher/sidecar.py &
TARGET=vertex GOOGLE_CLOUD_PROJECT=<project> SCENARIO=constant RATE=5 DURATION=60s \
  k6 run --out experimental-prometheus-rw loadtest/k6/gemini.js
```

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `GEMINI_BACKEND` | `vertex` | `vertex` or `developer` |
| `GEMINI_MODEL` | `gemini-2.5-flash` | |
| `GEMINI_MAX_OUTPUT_TOKENS` | `1024` | shared with the thinking budget |
| `GEMINI_THINKING_BUDGET` | `0` | `-1` is dynamic and unbounded |
| `GEMINI_MAX_CONNECTIONS` | `256` | the real throughput ceiling |
| `GEMINI_PARALLELISM` | `max_connections / 2.5` | derived, not independent |
| `GEMINI_MAX_ATTEMPTS` | `4` | |
| `GEMINI_ATTEMPT_TIMEOUT_S` | `60` | per attempt |
| `GEMINI_TOTAL_DEADLINE_S` | `180` | across all retries |
| `GEMINI_BASE_URL` | unset | set to redirect at the mock |

The fake endpoint is tunable at runtime, so one server can drive many scenarios:

```bash
curl -X POST http://127.0.0.1:8088/__configure -H 'Content-Type: application/json' \
  -d '{"rate_limit_probability":0.1,"safety_probability":0.02,"knee_concurrency":32}'
curl http://127.0.0.1:8088/__stats     # inflight, peak_inflight, response mix
```

## Reproducing the pool-ceiling experiment

```bash
make mock-up
make pool-experiment
```

Holds concurrency at 64 and varies only the connection pool. Throughput tracks
`pool_size / latency` exactly until the pool exceeds concurrency:

| Pool | Throughput | p50 | Predicted |
|---|---|---|---|
| 8 | 15.4 rps | 4162 ms | 16 rps |
| 16 | 30.6 rps | 2176 ms | 32 rps |
| 64 | 110.5 rps | 519 ms | concurrency-bound |
| 128 | 108.0 rps | 526 ms | concurrency-bound |

The service was answering in a flat 500 ms throughout. At pool=8 the client reports
4.2 s. That inflation is entirely queueing inside our own process, and nothing in the
vendor's response indicates it — which is why `llm_pool_saturation_ratio` is a
first-class metric rather than a debug print.

## Troubleshooting

**k6 reports `dropped_iterations > 0.`** The generator could not sustain the offered
rate, so the run understates real load. Raise `MAX_VUS`. Every other number in that
run is suspect until this is zero; it is a threshold, so the run exits non-zero.

**Grafana shows no subject data.** The Python harness only exposes metrics while it is
running, and it is scraped at 2 s. For a finished run, query with a lookback
(`last_over_time(llm_requests_total[10m])`) rather than an instant query.

**`llm_pool_saturation_ratio` sits near 1.0.** The pool is the ceiling. Raise
`GEMINI_MAX_CONNECTIONS`; `parallelism()` follows automatically.

**`llm_event_loop_lag_seconds` climbing.** The Python client is the constraint, not
the vendor. Compare against the k6 control before drawing any conclusion about Vertex.
