// Load test against Vertex directly, bypassing our service.
//
// This is the control. Running it alone tells you about Google, not about us; its
// value is the comparison with service.js on the same corpus and the same scenario.
// If our service plateaus where this keeps climbing, the ceiling is our code. If both
// knee at the same arrival rate, the ceiling is Vertex. That distinction cannot be
// made from inside a single harness.
//
// It is also open-loop by construction (see scenarios.js), so a slow response never
// reduces the request rate - the bias that makes closed-loop latency flatter itself.
//
// SPENDS REAL MONEY. Request count is bounded by rate x duration, which is known
// before the run starts, and maxOutputTokens is always set. Check the estimate first.

import http from 'k6/http';
import { check } from 'k6';
import { scenarios, thresholds } from './scenarios.js';
import { authHeaders, authMode, endpointUrl, modelId, target } from './lib/auth.js';
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

const MAX_OUTPUT_TOKENS = Number(__ENV.GEMINI_MAX_OUTPUT_TOKENS || 1024);
// -1 is dynamic thinking. Default 0 matches the Python provider; an unconstrained
// budget can consume the entire output allowance and leave no answer.
const THINKING_BUDGET = Number(__ENV.GEMINI_THINKING_BUDGET || 0);

export default function () {
  // __VU is the k6 virtual user: one concurrent request slot with its own isolated
  // JS runtime. __ITER is that VU's iteration count. Together they give a unique,
  // deterministic sequence number per request.
  const seq = __ITER * 1000 + __VU;

  const payload = JSON.stringify({
    contents: [{ role: 'user', parts: [{ text: buildQuestion(seq) }] }],
    systemInstruction: { role: 'user', parts: [{ text: SYSTEM_PROMPT }] },
    generationConfig: {
      temperature: Number(__ENV.TEMPERATURE || 1.0),
      maxOutputTokens: MAX_OUTPUT_TOKENS,
      // One spelling only: camelCase and snake_case map to the same protobuf oneof,
      // so sending both is a 400 rather than a merge. See FINDINGS 5.
      thinkingConfig: { thinkingBudget: THINKING_BUDGET },
    },
  });

  const res = http.post(endpointUrl(), payload, {
    headers: authHeaders(),
    tags: { name: 'generateContent', model: modelId() },
    timeout: `${__ENV.REQUEST_TIMEOUT_S || 120}s`,
  });

  if (res.status === 429) {
    rateLimited.add(1);
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

  const usage = body.usageMetadata || {};
  const candidate = (body.candidates || [])[0] || {};
  const parts = (candidate.content || {}).parts || [];
  const text = parts.map((p) => p.text || '').join('').trim();

  recordUsage(
    usage.promptTokenCount || 0,
    usage.candidatesTokenCount || 0,
    usage.thoughtsTokenCount || 0
  );
  recordOutcome(text, candidate.finishReason || 'UNKNOWN');
}

export function handleSummary(data) {
  const out = baseSummary(data, (count) => ({
    // Reported, not assumed. This file is usually pointed at Vertex but TARGET=mock
    // is how the whole path gets exercised for free, and a summary that always says
    // "vertex" turns a $0 rehearsal into evidence about Google.
    target: target(),
    model: modelId(),
    max_output_tokens: MAX_OUTPUT_TOKENS,
    thinking_budget: THINKING_BUDGET,
    auth_mode: authMode(),
    // Derived rather than read from a tagged submetric: k6 only exports submetrics
    // that a threshold references, so counting http_reqs{name:token-refresh}
    // silently returns 0. Every iteration makes exactly one generateContent call,
    // so the excess over iterations is auth traffic. This should be about the VU
    // count. If it approaches the request count, per-VU token caching is broken and
    // the run is measuring Google's OAuth endpoint rather than Vertex.
    token_refreshes: count('http_reqs') - count('iterations'),
  }));

  const outFile = __ENV.K6_SUMMARY_OUT || 'results/k6-vertex-summary.json';
  return {
    stdout:
      `\nk6 -> ${out.target} direct (${out.model}), scenario=${out.scenario}\n` +
      `  thinking_budget=${THINKING_BUDGET} max_output_tokens=${MAX_OUTPUT_TOKENS}\n` +
      `  requests=${out.requests} rate_limited=${out.rate_limited} ` +
      `empty=${out.empty_responses} truncated=${out.truncated_responses}\n` +
      `  finish reasons: ${JSON.stringify(out.finish_reasons)}\n` +
      `  p50=${out.latency_ms.p50.toFixed(0)}ms p99=${out.latency_ms.p99.toFixed(0)}ms\n` +
      dropWarning(out) +
      `  ACTUAL COST: $${out.cost_usd.toFixed(4)}\n`,
    [outFile]: JSON.stringify({ ...out, metrics: data.metrics }, null, 2),
  };
}
