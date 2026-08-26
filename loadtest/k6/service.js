// Load test for OUR service (service/app.py).
//
// This is the run that answers "does our integration hold up". Requests arrive at us
// over HTTP exactly as production traffic would, so admission control, backpressure,
// connection handling and framework cost are all exercised.
//
// Point BASE_URL at the mock-backed service to iterate for free, or at a
// Vertex-backed service to measure against the real vendor. The service configures
// its own thinking budget and output cap, so this script does not set them: that is
// the service's concern, not the caller's.

import http from 'k6/http';
import { check } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';
import { scenarios, thresholds } from './scenarios.js';
import { SYSTEM_PROMPT, buildQuestion } from './lib/workload.js';
import {
  SUMMARY_TREND_STATS, baseSummary, dropWarning, rateLimited,
  recordOutcome, recordUsage, usableRate,
} from './lib/metrics.js';

export const options = {
  scenarios,
  thresholds,
  discardResponseBodies: false,
  summaryTrendStats: SUMMARY_TREND_STATS,
};

// The whole point of driving our service rather than the vendor: these separate what
// our layer costs from what we spent waiting on Google.
const serviceOverhead = new Trend('service_overhead_ms');
const serviceQueueWait = new Trend('service_queue_wait_ms');
const serviceUpstream = new Trend('service_upstream_ms');
const serviceRejected = new Counter('service_rejected_503');
const groundedRequests = new Counter('grounded_requests');
// A 200 that came back ungrounded when grounding was asked for. Silent by
// construction, and it corrupts the measurement rather than failing it.
const groundingDegraded = new Counter('grounding_degraded');
// Answer finished cleanly AND arrived in the condition it was requested in.
const usableSamples = new Rate('usable_samples');

const BASE_URL = __ENV.SERVICE_URL || 'http://127.0.0.1:8000';
const GROUNDED = __ENV.GROUNDED === 'true';
// Fraction of requests that ask for grounding, for reproducing the real two-condition
// workload in one run. GROUNDED=true is the all-or-nothing shorthand.
const GROUNDED_FRACTION = GROUNDED ? 1.0 : Number(__ENV.GROUNDED_FRACTION || 0);
// Evertune's unit of work is one prompt sampled many times, not many distinct prompts.
// Distinct prompts are the right shape for a throughput test and the wrong shape for
// anything that depends on caching, retrieval reuse, or answer variance.
const REPEAT_PROMPT = __ENV.REPEAT_PROMPT === 'true';

export function setup() {
  // Ask the service what it is talking to, because k6 cannot tell from outside and
  // the answer decides whether this run's dollar figure is real money or a
  // simulation. A run against the mock that gets filed as spend inflates the ledger;
  // a real run that gets filed as a rehearsal hides it. Both have happened.
  const res = http.get(`${BASE_URL}/health`, { tags: { name: 'health' } });
  try {
    const provider = res.json().provider || {};
    return { billable: provider.billable === true, backend: provider.backend || null };
  } catch (e) {
    return { billable: null, backend: null };
  }
}

export default function () {
  // __VU is the k6 virtual user: one concurrent request slot, each with its own
  // isolated JS runtime. __ITER is that VU's iteration count. Combined they give a
  // unique, deterministic sequence number per request across the whole run.
  const seq = __ITER * 1000 + __VU;
  // Deterministic per-request split rather than random, so the share is exact and
  // the run reproduces.
  //
  // Mixing both counters matters. `seq` is __ITER * 1000 + __VU, and 1000 is a
  // multiple of 100, so `seq % 100` collapses to `__VU % 100` and the split then
  // depends entirely on which VUs an arrival-rate executor happened to allocate.
  // That skewed a nominal 50% to 63% in rehearsal. Odd multipliers avoid it.
  const bucket = (__VU * 37 + __ITER * 61) % 100;
  const grounded = GROUNDED_FRACTION >= 1.0
    ? true
    : GROUNDED_FRACTION <= 0
      ? false
      : bucket < Math.round(GROUNDED_FRACTION * 100);

  const payload = JSON.stringify({
    question: REPEAT_PROMPT ? buildQuestion(0) : buildQuestion(seq),
    system_prompt: SYSTEM_PROMPT,
    temperature: Number(__ENV.TEMPERATURE || 1.0),
    ...(grounded ? { grounded: true } : {}),
  });

  const res = http.post(`${BASE_URL}/ask`, payload, {
    headers: { 'Content-Type': 'application/json' },
    tags: { name: 'ask' },
    timeout: `${__ENV.REQUEST_TIMEOUT_S || 120}s`,
  });

  if (res.status === 429) {
    rateLimited.add(1);
    usableRate.add(false);
    return;
  }
  // 503 is deliberate backpressure, not a fault, so it is counted apart from errors.
  if (res.status === 503) {
    serviceRejected.add(1);
    usableRate.add(false);
    return;
  }
  if (!check(res, { 'status is 200': (r) => r.status === 200 })) {
    usableRate.add(false);
    return;
  }

  let body;
  try {
    body = res.json();
  } catch (e) {
    usableRate.add(false);
    return;
  }

  // The service already parsed and accounted for everything, including how much of
  // the latency was its own rather than the vendor's.
  const thinking = body.thinking_tokens || 0;
  const visible = Math.max(0, (body.output_tokens || 0) - thinking);
  recordUsage(body.input_tokens || 0, visible, thinking);
  serviceOverhead.add(body.overhead_ms || 0);
  serviceQueueWait.add(body.queue_wait_ms || 0);
  serviceUpstream.add(body.upstream_ms || 0);
  recordOutcome(body.answer || '', body.finish_reason || 'UNKNOWN');

  // Success here is not HTTP 200. A sample only counts if the answer finished and
  // the condition it was collected under is the one that was asked for. A truncated
  // answer is a partial brand list, and a request that asked for grounding and
  // silently got none is measuring the wrong thing entirely.
  const clean = body.usable === true;
  const asRequested = (body.grounded === true) === grounded;
  if (grounded) {
    groundedRequests.add(1);
    if (body.grounded !== true) groundingDegraded.add(1);
  }
  usableSamples.add(clean && asRequested);
}

export function handleSummary(data) {
  const setupData = data.setup_data || {};
  const out = baseSummary(data, (count, trend) => ({
    target: 'service',
    service_url: BASE_URL,
    // Provenance for the spend ledger, read from /health at setup.
    billable: setupData.billable,
    backend: setupData.backend,
    grounded: GROUNDED,
    grounded_fraction: GROUNDED_FRACTION,
    repeat_prompt: REPEAT_PROMPT,
    grounded_requests: count('grounded_requests'),
    grounding_degraded: count('grounding_degraded'),
    // The number that matters: share of requests that produced a sample worth
    // counting, as opposed to a 200.
    usable_sample_rate: ((data.metrics.usable_samples || {}).values || {}).rate,
    service_rejected_503: count('service_rejected_503'),
    service_overhead_ms: {
      p50: trend('service_overhead_ms', 'med'),
      p99: trend('service_overhead_ms', 'p(99)'),
    },
    service_queue_wait_ms: { p99: trend('service_queue_wait_ms', 'p(99)') },
  }));

  const outFile = __ENV.K6_SUMMARY_OUT || 'results/k6-service-summary.json';
  return {
    stdout:
      `\nk6 -> our service at ${BASE_URL}, scenario=${out.scenario}\n` +
      `  requests=${out.requests} shed_503=${out.service_rejected_503} ` +
      `rate_limited=${out.rate_limited} truncated=${out.truncated_responses}\n` +
      `  finish reasons: ${JSON.stringify(out.finish_reasons)}\n` +
      `  p50=${out.latency_ms.p50.toFixed(0)}ms p99=${out.latency_ms.p99.toFixed(0)}ms\n` +
      `  our overhead: p50=${out.service_overhead_ms.p50.toFixed(2)}ms ` +
      `p99=${out.service_overhead_ms.p99.toFixed(2)}ms\n` +
      dropWarning(out) +
      `  ACTUAL COST: $${out.cost_usd.toFixed(4)}\n`,
    [outFile]: JSON.stringify({ ...out, metrics: data.metrics }, null, 2),
  };
}
