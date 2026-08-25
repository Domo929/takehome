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
import { Counter, Trend } from 'k6/metrics';
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

const BASE_URL = __ENV.SERVICE_URL || 'http://127.0.0.1:8000';
const GROUNDED = __ENV.GROUNDED === 'true';

export default function () {
  // __VU is the k6 virtual user: one concurrent request slot, each with its own
  // isolated JS runtime. __ITER is that VU's iteration count. Combined they give a
  // unique, deterministic sequence number per request across the whole run.
  const seq = __ITER * 1000 + __VU;

  const payload = JSON.stringify({
    question: buildQuestion(seq),
    system_prompt: SYSTEM_PROMPT,
    temperature: Number(__ENV.TEMPERATURE || 0.7),
    ...(GROUNDED ? { grounded: true } : {}),
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
}

export function handleSummary(data) {
  const out = baseSummary(data, (count, trend) => ({
    target: 'service',
    service_url: BASE_URL,
    grounded: GROUNDED,
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
