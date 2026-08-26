"""Prometheus instrumentation.

Three metrics exist to answer specific questions rather than because they were easy to
emit:

`llm_pool_saturation_ratio` - the connection pool is the most common way an async LLM
client silently caps out, and nothing in the vendor's response reveals it (FINDINGS 3).
Exceeds 1.0 when oversubscribed.

`llm_event_loop_lag_seconds` - is the ceiling us or them? If lag climbs with load, the
client is the bottleneck and vendor-side numbers are meaningless (FINDINGS 4).

`llm_spend_usd_total` - computed from real usage_metadata, never estimated, so the cost
governor trips on money actually spent.
"""

from __future__ import annotations

import asyncio
from collections import deque
import time

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server

REGISTRY = CollectorRegistry(auto_describe=True)

# Spans sub-second replies through a 120s timeout ceiling. Roughly Fibonacci so the
# long tail stays legible without an unreasonable bucket count.
_LATENCY_BUCKETS = (
    0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0, 34.0, 55.0, 90.0, 120.0, float("inf"),
)

request_duration = Histogram(
    "llm_request_duration_seconds",
    "End-to-end duration of a completed ask_generic_question call.",
    ["provider", "model", "outcome", "finish_reason"],
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)

requests_total = Counter(
    "llm_requests_total",
    "Completed requests by outcome.",
    ["provider", "model", "outcome", "finish_reason", "error_class"],
    registry=REGISTRY,
)

tokens_total = Counter(
    "llm_tokens_total",
    "Tokens consumed. kind=input|output|thinking. output includes thinking.",
    ["provider", "model", "kind"],
    registry=REGISTRY,
)

spend_usd_total = Counter(
    "llm_spend_usd_total",
    "Dollars spent, derived from reported usage metadata.",
    ["provider", "model"],
    registry=REGISTRY,
)

inflight_requests = Gauge(
    "llm_inflight_requests",
    "Requests currently awaiting a vendor response.",
    ["provider"],
    registry=REGISTRY,
)

pool_size = Gauge(
    "llm_pool_size",
    "Configured max HTTP connections for this provider's transport.",
    ["provider"],
    registry=REGISTRY,
)

pool_saturation_ratio = Gauge(
    "llm_pool_saturation_ratio",
    "In-flight requests divided by pool size. At 1.0 the pool is the ceiling.",
    ["provider"],
    registry=REGISTRY,
)

retry_attempts_total = Counter(
    "llm_retry_attempts_total",
    "Retries performed, by the error class that triggered them.",
    ["provider", "reason"],
    registry=REGISTRY,
)

retry_budget_tokens = Gauge(
    "llm_retry_budget_tokens",
    "Remaining retry budget. Zero means retries are being shed.",
    ["provider"],
    registry=REGISTRY,
)

empty_responses_total = Counter(
    "llm_empty_responses_total",
    "HTTP 200 responses carrying no usable text, by finish reason.",
    ["provider", "model", "finish_reason"],
    registry=REGISTRY,
)

grounding_degraded_total = Counter(
    "llm_grounding_degraded_total",
    "Requests that asked for live search and came back without it. These succeed, so "
    "nothing else surfaces them, but each one is an ungrounded answer about to be "
    "recorded as a grounded measurement.",
    ["provider", "model"],
    registry=REGISTRY,
)

unbilled_attempt_cost_usd = Counter(
    "llm_unbilled_attempt_cost_usd_total",
    "Upper-bound cost of attempts that failed after the vendor may already have done "
    "billable work. Invisible to usage metadata, so the spend breaker would otherwise "
    "under-count. Negligible for token-only requests; material for grounded ones.",
    ["provider", "model"],
    registry=REGISTRY,
)

event_loop_lag = Gauge(
    "llm_event_loop_lag_seconds",
    "Scheduling delay observed by a fixed-interval probe. Rising lag means the "
    "client, not the vendor, is the constraint.",
    registry=REGISTRY,
)

budget_remaining_usd = Gauge(
    "llm_budget_remaining_usd",
    "Dollars left before the cost governor halts the run.",
    registry=REGISTRY,
)


class EventLoopLagMonitor:
    """Samples event-loop scheduling delay.

    Sleeps for a known interval and records how much longer than that it actually
    took. Under a saturated or CPU-bound loop the overshoot grows, which is the
    clearest available signal that the load generator has become the bottleneck.
    """

    # ~6 hours of history at the default interval, about 11 MB for both series.
    # Bounded because the same monitor runs inside the long-lived service, where an
    # unbounded list grows forever: 691k tuples a day, 20M a month, none of it ever
    # read. The harness only ever slices back to the start of the current stage, so
    # this is far more than it needs.
    DEFAULT_MAX_SAMPLES = 86_400

    def __init__(
        self, interval_s: float = 0.25, max_samples: int | None = None
    ) -> None:
        self._interval = interval_s
        self._task: asyncio.Task[None] | None = None
        cap = self.DEFAULT_MAX_SAMPLES if max_samples is None else max_samples
        # Timestamped samples, so a run can attribute lag to the window it happened in
        # rather than reporting one number for the whole run. Gauges are point-in-time:
        # a scrape that lands between spikes misses them entirely, which is how a
        # diagnostic this important ends up unreproducible from a saved artifact.
        self.samples: deque[tuple[float, float]] = deque(maxlen=cap)
        # Pool saturation is sampled on the same clock so the two can be read against
        # each other: high lag with an idle pool means we are the bottleneck, high lag
        # with a full pool means we are waiting on the vendor.
        self.pool_samples: deque[tuple[float, float]] = deque(maxlen=cap)

    async def _run(self) -> None:
        while True:
            started = time.perf_counter()
            await asyncio.sleep(self._interval)
            lag = max(0.0, (time.perf_counter() - started) - self._interval)
            event_loop_lag.set(lag)
            now = time.perf_counter()
            self.samples.append((now, lag))
            for metric in REGISTRY.collect():
                if metric.name == "llm_pool_saturation_ratio":
                    for sample in metric.samples:
                        self.pool_samples.append((now, sample.value))
                    break

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


def serve(port: int = 9464) -> None:
    """Expose /metrics for Prometheus to scrape."""
    start_http_server(port, registry=REGISTRY)


# --- inbound service metrics -------------------------------------------------
# These describe our own service receiving traffic, as opposed to the llm_* metrics
# above which describe our outbound calls to the vendor. Keeping both lets a single
# request be split into "time we spent" versus "time the vendor spent", which is the
# only way to answer whether our integration adds meaningful cost.

service_request_duration_seconds = Histogram(
    "service_request_duration_seconds",
    "End-to-end duration of an inbound /ask request, measured at our edge.",
    ["outcome"],
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)

service_overhead_seconds = Histogram(
    "service_overhead_seconds",
    "Inbound duration minus upstream vendor duration. Everything that is our fault: "
    "framework, validation, JSON, event-loop scheduling, admission queueing.",
    buckets=(
        0.0005, 0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0,
        float("inf"),
    ),
    registry=REGISTRY,
)

service_queue_wait_seconds = Histogram(
    "service_queue_wait_seconds",
    "Time spent waiting at the admission gate. Growth here is the earliest signal "
    "of saturation, well before latency or errors move.",
    buckets=(
        0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, float("inf"),
    ),
    registry=REGISTRY,
)

service_requests_total = Counter(
    "service_requests_total",
    "Inbound requests by outcome.",
    ["outcome", "finish_reason"],
    registry=REGISTRY,
)

service_admission_rejected_total = Counter(
    "service_admission_rejected_total",
    "Requests shed with 503 because the service was at capacity. Deliberate "
    "backpressure, not an error: shedding beats unbounded queueing.",
    registry=REGISTRY,
)

service_retry_backoff_seconds = Histogram(
    "service_retry_backoff_seconds",
    "Time spent sleeping between retry attempts. Neither our processing cost nor the "
    "vendor's response time, so attributed separately rather than folded into either.",
    buckets=(
        0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0, float("inf"),
    ),
    registry=REGISTRY,
)

service_upstream_seconds = Histogram(
    "service_upstream_seconds",
    "Vendor time summed across all attempts for one inbound request.",
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)

service_inflight = Gauge(
    "service_inflight_requests",
    "Inbound requests currently being served.",
    registry=REGISTRY,
)


# --- adaptive concurrency ----------------------------------------------------

adaptive_limit = Gauge(
    "llm_adaptive_limit",
    "Current concurrency limit chosen by the controller. A constant here would mean "
    "the controller is not reacting.",
    ["provider"],
    registry=REGISTRY,
)

adaptive_gradient = Gauge(
    "llm_adaptive_gradient",
    "Baseline RTT divided by recent RTT. 1.0 is healthy; below 1.0 means requests are "
    "queueing and the limit is being reduced before any error appears.",
    ["provider"],
    registry=REGISTRY,
)

adaptive_baseline_rtt = Gauge(
    "llm_adaptive_baseline_rtt_seconds",
    "Best recent round-trip time, used as the uncongested reference.",
    ["provider"],
    registry=REGISTRY,
)

adaptive_short_rtt = Gauge(
    "llm_adaptive_short_rtt_seconds",
    "Recent round-trip average compared against the baseline.",
    ["provider"],
    registry=REGISTRY,
)

adaptive_drops_total = Counter(
    "llm_adaptive_drops_total",
    "Vendor rejections that triggered multiplicative decrease.",
    ["provider"],
    registry=REGISTRY,
)
