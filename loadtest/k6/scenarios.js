// k6 scenario definitions, shared by service.js and vertex.js.
//
// A VU (virtual user) is k6's unit of concurrency: one slot that issues a request,
// waits for the response, then issues the next. Each runs in its own isolated JS
// runtime.
//
// Every scenario here uses an ARRIVAL-RATE executor rather than a VU-count executor,
// and that choice is the reason these numbers can be trusted. An arrival-rate
// executor dispatches on a wall-clock schedule regardless of whether earlier requests
// have returned, so a slowdown surfaces as latency and queue growth. A VU-count
// executor would instead issue fewer requests when the system slows - coordinated
// omission, the most common way a load test flatters what it measures.
//
// VUs are still allocated, but as a pool the schedule draws from rather than as the
// thing being held constant. preAllocatedVUs is sized generously: if k6 runs out it
// drops iterations, and dropped_iterations then means the GENERATOR was the
// constraint, which makes every other number an understatement. Both scripts report
// it for exactly that reason.
//
// Pick one with: k6 run --env SCENARIO=ramp loadtest/k6/service.js

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

  // Rig calibration, not a Gemini test. Ramps hard against a zero-latency mock to
  // find the ceiling of the *test rig itself* (mock server, loopback, k6). Any real
  // experiment must run far below whatever knee this finds, otherwise the harness is
  // measuring its own limits and attributing them to the vendor.
  calibrate: {
    executor: 'ramping-arrival-rate',
    startRate: 100,
    timeUnit: '1s',
    preAllocatedVUs: 600,
    maxVUs: 2000,
    stages: [
      { target: 250, duration: '10s' },
      { target: 500, duration: '10s' },
      { target: 1000, duration: '10s' },
      { target: 2000, duration: '10s' },
      { target: 4000, duration: '10s' },
    ],
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
