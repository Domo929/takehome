# Runbook

How to run everything here, and what each piece is. **Why any of it matters, and what
it found, is in [FINDINGS.md](FINDINGS.md)** — this file deliberately stays out of that
territory.

## Setup

```bash
make venv                 # venv + dependencies
make test                 # 52 tests, no network, no spend
```

Python 3.12+. k6 and Docker are only needed for the load and observability sections.

## Vocabulary

Two terms used throughout that mean something specific:

**VU (virtual user)** — k6's unit of concurrency. One VU issues a request, waits for
the response, then issues the next, in its own isolated JS runtime. VUs are *not* the
thing being held constant here: every scenario drives a fixed arrival rate and draws
VUs from a pool as needed. `dropped_iterations > 0` means the pool ran dry and k6
could not sustain the offered rate.

**Concurrency** — the Python side's equivalent: how many requests the provider or
harness keeps in flight at once. Capped by `provider.parallelism()` in the service.

## The pieces

| Path | What it is |
|---|---|
| `llm/` | The provider. `gemini.py`, plus errors, retry, metrics, pricing, adaptive limiter. |
| `service/app.py` | The provider deployed as an HTTP service. The system under test for load work. |
| `loadtest/k6/service.js` | Load test against our service. |
| `loadtest/k6/vertex.js` | Load test straight at Vertex, bypassing us. The control. |
| `loadtest/k6/lib/` | Shared corpus, metrics and auth, so both scripts stay comparable. |
| `harness/run.py` | In-process experiment driver with a hard spend breaker. Not a load test — see below. |
| `mock/fake_vertex.py` | Fake Vertex endpoint. Speaks the real wire contract, costs nothing. |
| `scripts/` | One-off experiments and reporting. |
| `observability/` | Prometheus + Grafana, dashboards provisioned from disk. |

### Two drivers, two jobs

They are not redundant, and they are not interchangeable.

**`loadtest/k6/` answers "does our service hold up".** Requests arrive over HTTP the
way production traffic does, so admission control, backpressure, connection handling
and framework cost are all exercised. k6 is a separate process in a different runtime,
so it cannot flatter us by sharing our event loop.

**`harness/run.py` answers "how does the model behave".** It calls the provider
in-process, which gives it two things k6 cannot have:

- **A spend breaker that works.** It checks accumulated *actual* cost from reported
  `usage_metadata` before every dispatch and drains when the ceiling trips. k6 has no
  way to stop itself mid-run on spend.
- **The full response.** Thinking tokens, finish reasons, grounding sources and
  citations are read directly rather than round-tripped through our own API, which
  would mean measuring our serialisation as well as the model.

Every model finding in FINDINGS came from `harness/run.py` or `scripts/`. Every
service finding came from k6.

## Running against the fake endpoint ($0)

Everything below spends nothing and needs no credentials.

```bash
make mock-up              # fake Vertex on :8088
make obs-up               # Prometheus :9090, Grafana :3000
make service-up           # our service on :8000

make overhead             # k6 -> service vs k6 -> backend direct
make capacity             # find where the service sheds load
make sweep-mock           # concurrency sweep through the provider
make pool-experiment      # connection pool vs throughput
make calibrate            # the rig's own ceiling
make auth-check           # token fetches are O(VUs), not O(requests)
```

Grafana is at http://localhost:3000, folder *Takehome*, anonymous admin.

Point the service at the mock explicitly:

```bash
GEMINI_BACKEND=vertex GEMINI_BASE_URL=http://127.0.0.1:8088 \
GOOGLE_CLOUD_PROJECT=fake python -m service.app --port 8000
```

### Driving k6 directly

```bash
# our service
SCENARIO=constant RATE=50 DURATION=30s k6 run loadtest/k6/service.js

# the vendor, or the mock standing in for it
TARGET=mock SCENARIO=smoke k6 run loadtest/k6/vertex.js
TARGET=vertex SCENARIO=ramp GOOGLE_CLOUD_PROJECT=... k6 run loadtest/k6/vertex.js
```

Scenarios: `smoke`, `constant`, `ramp`, `spike`, `calibrate`. Knobs: `RATE`,
`DURATION`, `MAX_VUS`, `COMPLEX_FRACTION` (share of long-form prompts), `TEMPERATURE`,
`GROUNDED`.

To record a run so the dashboard can jump to its time window:

```bash
make run ARGS="--scenario ramp"
make runs
```

## Spending real money

### 1. Check access

```bash
make vertex-check         # 7 steps, no generation calls
make preflight            # exactly ONE real request, prints its cost
```

### 2. Cost controls

These are mechanical, not advisory:

1. **Dry run is the default.** Every harness invocation prints a projected cost and
   exits. Nothing is sent without `--confirm`.
2. **Pre-flight refusal.** If the estimate already exceeds `--budget-usd`, the run
   refuses to start.
3. **Runtime breaker.** Accumulated actual spend — from reported `usage_metadata`,
   never estimated — is checked before every dispatch. When it trips the run drains
   and stops. Overshoot is bounded by concurrency × cost-per-request, because
   in-flight requests cannot be recalled.
4. **`max_output_tokens` is always set.**
5. **`thinking_budget=0` by default.**

Every run ends with a reconciliation, because a consistently wrong estimator is itself
worth knowing about:

```
Cost reconciliation
  requests completed  1,500
  ACTUAL COST         $0.6783
  budget remaining    $4.3217
  estimate error      -12.0% (estimated $0.7707)
```

### 3. Run something

```bash
export GOOGLE_CLOUD_PROJECT=your-project
export GOOGLE_CLOUD_LOCATION=us-central1

python -m harness.run --mode closed --concurrency 64 --duration 300 \
  --warmup-s 20 --budget-usd 5 --confirm --label soak
```

Useful flags: `--repeat-prompt` (one prompt sampled N times, the real unit of work),
`--complex-fraction`, `--thinking-budget`, `--max-output-tokens`, `--warmup-s`
(discard cold-pool warm-up from latency, still count it for cost).

### 4. Account for it

```bash
make spend                # actual spend to date, by account
```

Reads every manifest in `results/`. Probes issued outside the harness are declared
explicitly in `scripts/spend_report.py` rather than quietly omitted.

## Experiments

Each refuses to run without `--yes` and prints its estimate first.

```bash
python scripts/logprobs_experiment.py --yes      # what logprobs add to sampling
python scripts/grounding_experiment.py --yes     # grounded vs ungrounded, paired
python scripts/production_unit.py --yes          # one prompt x 100, both conditions
python scripts/confidence.py                     # bootstrap CIs, no requests
python scripts/cost_model.py --daily 50000       # project cost at volume
make adaptive-demo                               # adaptive vs fixed, mock only
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_BACKEND` | `vertex` | `vertex` or `developer` |
| `GOOGLE_CLOUD_PROJECT` | — | required for Vertex |
| `GOOGLE_CLOUD_LOCATION` | `us-central1` | selects a distinct quota pool |
| `GOOGLE_API_KEY` | — | required for `developer` |
| `GEMINI_MODEL` | `gemini-2.5-flash` | |
| `GEMINI_THINKING_BUDGET` | `0` | `-1` is dynamic |
| `GEMINI_MAX_OUTPUT_TOKENS` | `1024` | |
| `GEMINI_GROUNDED` | `false` | default for calls that do not specify |
| `GEMINI_MAX_CONNECTIONS` | `256` | HTTP pool size |
| `GEMINI_ADAPTIVE` | `false` | adaptive concurrency limiter |
| `GEMINI_HTTP2` | `false` | |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | `text` for local work |

`.env.local` is read at startup and is gitignored. Real exported variables always win
over the file.

## Packaging a copy

```bash
git archive --format=zip -o submission.zip HEAD
```

Exports tracked files at HEAD and nothing else. Notably it excludes `.git`, which
matters because a working clone accumulates local refs and dangling objects that were
never part of any branch — `.env.local` among them. Zipping the directory would ship
those; `git archive` cannot.

Verify before sending:

```bash
unzip -qo submission.zip -d /tmp/check
grep -rE "AIza[0-9A-Za-z_-]{30,}" /tmp/check   # expect no matches
cd /tmp/check && python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pytest tests/                        # expect 52 passed, no network
```

## Troubleshooting

**`GOOGLE_CLOUD_PROJECT is required`** — Vertex needs a project. Set it or pass
`project=`.

**403 from Vertex** — run `make vertex-check`; it isolates which of API enablement,
IAM role or ADC is missing.

**Service returns 503** — deliberate. Capacity is `provider.parallelism()`; the
service sheds rather than queues.

**`dropped_iterations > 0` in a k6 summary** — k6 could not sustain the offered rate,
so every latency number in that run understates real load. Raise `MAX_VUS`.

**Grafana shows no data** — Prometheus scrapes the service on :9464 and k6 pushes via
remote write. Check `make obs-logs`.

**Port already in use after stopping a service** — the process may still be draining.
Check with `ss -lptn 'sport = :8000'`.
