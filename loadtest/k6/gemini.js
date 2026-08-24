// k6 control harness for Gemini on Vertex AI.
//
// Purpose
// -------
// This is NOT the production client. It is a control variable. The Python harness in
// harness/run.py drives the code we actually ship; this drives the same endpoint from
// a runtime with no GIL, no SDK, and no shared connection pool. Two uses:
//
//   1. If Python plateaus where k6 keeps climbing, the ceiling is our client.
//      If both knee at the same arrival rate, the ceiling is Vertex.
//      Nobody can distinguish those two cases from inside a single harness.
//
//   2. k6's arrival-rate executors are open-loop by construction, so they do not
//      suffer coordinated omission: a slow response never reduces the request rate,
//      which is exactly the bias that makes closed-loop latency look better than it is.
//
// Cost safety
// -----------
// This spends real money against a real project. maxOutputTokens is always set and
// total request count is bounded by (rate x duration), which is known before the run
// starts. Check the printed pre-flight estimate before using TARGET=vertex.

import http from 'k6/http';
import { check } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';
import { scenarios, thresholds } from './scenarios.js';
import { authHeaders, authMode, endpointUrl, modelId } from './lib/auth.js';

export const options = {
  scenarios,
  thresholds,
  discardResponseBodies: false,
  // k6's default trend stats stop at p(95); p(99) is where LLM tail latency lives.
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

// Token accounting mirrors the Python side so the two harnesses are comparable.
// Thinking tokens are tracked separately because they bill at the output rate but do
// not appear in the visible answer.
const inputTokens = new Trend('gemini_input_tokens');
const outputTokens = new Trend('gemini_output_tokens');
const thinkingTokens = new Trend('gemini_thinking_tokens');
const costUsd = new Counter('gemini_cost_usd');
const emptyResponses = new Counter('gemini_empty_responses');
const truncated = new Counter('gemini_truncated_responses');
const rateLimited = new Counter('gemini_rate_limited');
const usableRate = new Rate('gemini_usable_responses');

// One counter per finish reason rather than a plain JS object. k6 runs each VU in an
// isolated runtime and handleSummary in yet another, so module-scope mutable state
// never reaches the summary — only registered metrics cross that boundary. A tally
// kept in a bare object silently reports zero.
const finishStop = new Counter('gemini_finish_STOP');
const finishMaxTokens = new Counter('gemini_finish_MAX_TOKENS');
const finishSafety = new Counter('gemini_finish_SAFETY');
const finishRecitation = new Counter('gemini_finish_RECITATION');
const finishOther = new Counter('gemini_finish_OTHER');

function recordFinishReason(reason) {
  switch (reason) {
    case 'STOP':
      finishStop.add(1);
      break;
    case 'MAX_TOKENS':
      finishMaxTokens.add(1);
      break;
    case 'SAFETY':
      finishSafety.add(1);
      break;
    case 'RECITATION':
      finishRecitation.add(1);
      break;
    default:
      finishOther.add(1);
  }
}

const PRICE_INPUT_PER_1M = Number(__ENV.PRICE_INPUT_PER_1M || 0.3);
const PRICE_OUTPUT_PER_1M = Number(__ENV.PRICE_OUTPUT_PER_1M || 2.5);

const MAX_OUTPUT_TOKENS = Number(__ENV.GEMINI_MAX_OUTPUT_TOKENS || 1024);
// -1 means dynamic thinking. Default to 0 to match the Python provider: an
// unconstrained budget can consume the entire output allowance.
const THINKING_BUDGET = Number(__ENV.GEMINI_THINKING_BUDGET || 0);

const SYSTEM_PROMPT =
  'You are a market research assistant. Answer concisely and name specific brands ' +
  'and products. Do not add disclaimers.';

const CATEGORIES = [
  'robot vacuums', 'electric toothbrushes', 'noise-cancelling headphones',
  'running shoes', 'espresso machines', 'standing desks', 'air purifiers',
  'mechanical keyboards', 'electric kettles', 'cordless drills',
  'wireless earbuds', 'smart thermostats', 'meal kit services', 'cast iron skillets',
  'carry-on luggage', 'mattresses', 'dash cams', 'portable power stations',
  'office chairs', 'sous vide cookers',
];

const TEMPLATES = [
  'Which {c} would you recommend?',
  'What are the best {c} available right now?',
  "I'm shopping for {c}. What should I consider?",
  'Name the top five {c} and say why.',
  'Which brands make the most reliable {c}?',
  'What {c} offer the best value for money?',
  'Compare the leading {c} on the market.',
  'If you had to pick one of the {c}, which would it be?',
];

function buildQuestion(n) {
  const category = CATEGORIES[n % CATEGORIES.length];
  const template = TEMPLATES[Math.floor(n / CATEGORIES.length) % TEMPLATES.length];
  return template.replace('{c}', category);
}

export default function () {
  const seq = __ITER * 1000 + __VU;
  const payload = JSON.stringify({
    contents: [{ role: 'user', parts: [{ text: buildQuestion(seq) }] }],
    systemInstruction: { role: 'user', parts: [{ text: SYSTEM_PROMPT }] },
    generationConfig: {
      temperature: 0.7,
      maxOutputTokens: MAX_OUTPUT_TOKENS,
      // The SDK emits this key in snake_case while every sibling field is camelCase.
      // Both spellings are sent so the budget is honored regardless of which the
      // endpoint matches on; sending only camelCase silently disables the setting.
      thinkingConfig: {
        thinking_budget: THINKING_BUDGET,
        thinkingBudget: THINKING_BUDGET,
      },
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

  const ok = check(res, { 'status is 200': (r) => r.status === 200 });
  if (!ok) {
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
  const promptTokens = usage.promptTokenCount || 0;
  const visible = usage.candidatesTokenCount || 0;
  const thinking = usage.thoughtsTokenCount || 0;
  // Billed output is visible + thinking. Counting only candidatesTokenCount is the
  // accounting error that makes a run look cheaper than the invoice.
  const billedOutput = visible + thinking;

  inputTokens.add(promptTokens);
  outputTokens.add(billedOutput);
  thinkingTokens.add(thinking);
  costUsd.add(
    (promptTokens * PRICE_INPUT_PER_1M + billedOutput * PRICE_OUTPUT_PER_1M) / 1e6
  );

  const candidate = (body.candidates || [])[0] || {};
  const finish = candidate.finishReason || 'UNKNOWN';
  recordFinishReason(finish);

  const parts = ((candidate.content || {}).parts) || [];
  const text = parts.map((p) => p.text || '').join('').trim();

  if (finish === 'MAX_TOKENS') truncated.add(1);
  if (!text) emptyResponses.add(1);

  // A truncated answer counts as unusable, not partial: a fragment silently skews
  // downstream mention counts, which is worse than an outright failure.
  usableRate.add(Boolean(text) && finish === 'STOP');
}

export function handleSummary(data) {
  const metricCount = (name) => ((data.metrics[name] || {}).values || {}).count || 0;
  const trendValue = (name, stat) => ((data.metrics[name] || {}).values || {})[stat] || 0;

  const finishReasons = {
    STOP: metricCount('gemini_finish_STOP'),
    MAX_TOKENS: metricCount('gemini_finish_MAX_TOKENS'),
    SAFETY: metricCount('gemini_finish_SAFETY'),
    RECITATION: metricCount('gemini_finish_RECITATION'),
    OTHER: metricCount('gemini_finish_OTHER'),
  };

  const out = {
    target: __ENV.TARGET || 'mock',
    scenario: __ENV.SCENARIO || 'smoke',
    model: modelId(),
    max_output_tokens: MAX_OUTPUT_TOKENS,
    thinking_budget: THINKING_BUDGET,
    finish_reasons: finishReasons,
    cost_usd: metricCount('gemini_cost_usd'),
    requests: metricCount('http_reqs'),
    rate_limited: metricCount('gemini_rate_limited'),
    empty_responses: metricCount('gemini_empty_responses'),
    truncated_responses: metricCount('gemini_truncated_responses'),
    // If k6 dropped iterations it could not sustain the offered rate, which makes the
    // generator the bottleneck and every other number here suspect.
    dropped_iterations: metricCount('dropped_iterations'),
    auth_mode: authMode(),
    // Must be ~= number of VUs, never ~= number of requests.
    token_refreshes: metricCount('http_reqs{name:token-refresh}'),
    latency_ms: {
      p50: trendValue('http_req_duration', 'med'),
      p90: trendValue('http_req_duration', 'p(90)'),
      p95: trendValue('http_req_duration', 'p(95)'),
      p99: trendValue('http_req_duration', 'p(99)'),
      max: trendValue('http_req_duration', 'max'),
    },
    mean_thinking_tokens: trendValue('gemini_thinking_tokens', 'avg'),
    mean_output_tokens: trendValue('gemini_output_tokens', 'avg'),
  };

  const dropWarning =
    out.dropped_iterations > 0
      ? `  WARNING: ${out.dropped_iterations} dropped iterations — the generator could ` +
        `not sustain the offered rate, so these results understate real load.\n`
      : '';

  const outFile = __ENV.K6_SUMMARY_OUT || 'results/k6-summary.json';

  return {
    stdout:
      `\nk6 control run against ${out.target} (${out.model}), scenario=${out.scenario}\n` +
      `  thinking_budget=${THINKING_BUDGET} max_output_tokens=${MAX_OUTPUT_TOKENS}\n` +
      `  requests=${out.requests} rate_limited=${out.rate_limited} ` +
      `empty=${out.empty_responses} truncated=${out.truncated_responses}\n` +
      `  finish reasons: ${JSON.stringify(finishReasons)}\n` +
      `  p50=${out.latency_ms.p50.toFixed(0)}ms p99=${out.latency_ms.p99.toFixed(0)}ms\n` +
      dropWarning +
      `  ACTUAL COST: $${out.cost_usd.toFixed(4)}\n`,
    [outFile]: JSON.stringify({ ...out, metrics: data.metrics }, null, 2),
  };
}
