"""Prometheus instrumentation.

Metric choices are driven by specific questions the load tests need to answer, not by
what is easy to emit:

``llm_pool_saturation_ratio`` exists because the single most common way an async LLM
client silently caps out is the HTTP connection pool. At the SDK default of 100
connections and ~8s per request, a client cannot exceed ~12 rps no matter how much
concurrency you hand it, and nothing in the vendor's response reveals this. Comparing
in-flight requests against pool size makes it obvious.

``llm_event_loop_lag_seconds`` answers "is the ceiling us or them?". If lag climbs
with load, the client is the bottleneck and vendor-side numbers are meaningless.

``llm_spend_usd_total`` is computed from real ``usage_metadata``, never estimated, so
the cost governor trips on money actually spent.
"""

from __future__ import annotations

import asyncio
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

    def __init__(self, interval_s: float = 0.25) -> None:
        self._interval = interval_s
        self._task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        while True:
            started = time.perf_counter()
            await asyncio.sleep(self._interval)
            event_loop_lag.set(max(0.0, (time.perf_counter() - started) - self._interval))

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
