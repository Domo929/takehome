// k6 scenario definitions.
//
// Every scenario uses an arrival-rate executor rather than a VU-count executor. That
// is the whole point of running k6 alongside the Python harness: arrival-rate
// executors dispatch on a wall-clock schedule regardless of whether earlier requests
// have returned, so a slowdown shows up as latency and queue growth instead of
// silently reducing the offered load.
//
// preAllocatedVUs is sized generously. If k6 runs out of VUs it starts dropping
// iterations, and dropped_iterations is then the signal that the *generator*, not the
// service, is the constraint. That metric is watched deliberately.
//
// Pick one with: k6 run --env SCENARIO=ramp loadtest/k6/gemini.js

const SCENARIO = __ENV.SCENARIO || 'smoke';
const RATE = Number(__ENV.RATE || 5);
const DURATION = __ENV.DURATION || '30s';
const MAX_VUS = Number(__ENV.MAX_VUS || 400);

const ALL = {
  // Cheap sanity check that auth, payload shape, and parsing all work.
  smoke: {
    executor: 'constant-arrival-rate',
    rate: 1,
    timeUnit: '1s',
    duration: '10s',
    preAllocatedVUs: 10,
    maxVUs: 20,
  },

  // Fixed offered load. The primary comparison point against the Python harness at
  // the same arrival rate.
  constant: {
    executor: 'constant-arrival-rate',
    rate: RATE,
    timeUnit: '1s',
    duration: DURATION,
    preAllocatedVUs: Math.min(MAX_VUS, Math.max(20, RATE * 12)),
    maxVUs: MAX_VUS,
  },

  // Step ramp to find the knee. Each stage holds long enough for latency to settle,
  // so a transient burst is not mistaken for sustainable throughput.
  ramp: {
    executor: 'ramping-arrival-rate',
    startRate: 1,
    timeUnit: '1s',
    preAllocatedVUs: Math.min(MAX_VUS, 100),
    maxVUs: MAX_VUS,
    stages: [
      { target: 2, duration: '30s' },
      { target: 5, duration: '30s' },
      { target: 10, duration: '30s' },
      { target: 20, duration: '30s' },
      { target: 40, duration: '30s' },
      { target: 80, duration: '30s' },
    ],
  },

  // Short spike from a low baseline. Vertex quota is bursty in ways a smooth ramp
  // will not reveal.
  spike: {
    executor: 'ramping-arrival-rate',
    startRate: 2,
    timeUnit: '1s',
    preAllocatedVUs: Math.min(MAX_VUS, 200),
    maxVUs: MAX_VUS,
    stages: [
      { target: 2, duration: '20s' },
      { target: 60, duration: '10s' },
      { target: 60, duration: '30s' },
      { target: 2, duration: '20s' },
    ],
  },

  // Sustained run. Long enough to cross an ADC token refresh boundary and to expose
  // drift, leaks, and slow queue growth that a 30s run cannot show.
  soak: {
    executor: 'constant-arrival-rate',
    rate: RATE,
    timeUnit: '1s',
    duration: __ENV.DURATION || '20m',
    preAllocatedVUs: Math.min(MAX_VUS, Math.max(20, RATE * 12)),
    maxVUs: MAX_VUS,
  },
};

if (!ALL[SCENARIO]) {
  throw new Error(`Unknown SCENARIO "${SCENARIO}". Options: ${Object.keys(ALL).join(', ')}`);
}

export const scenarios = { [SCENARIO]: ALL[SCENARIO] };

// Thresholds are assertions, not decoration: a run that breaches them exits non-zero
// so a regression fails loudly instead of being buried in a dashboard.
export const thresholds = {
  // dropped_iterations means k6 could not keep up. If this trips, the generator is
  // the bottleneck and every other number in the run is suspect.
  dropped_iterations: ['count < 1'],
  'gemini_usable_responses': ['rate > 0.95'],
  'http_req_failed': ['rate < 0.05'],
  'http_req_duration{name:generateContent}': ['p(99) < 60000'],
};
