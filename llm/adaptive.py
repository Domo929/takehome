"""Adaptive concurrency limiting.

Why not a constant
------------------
``parallelism()`` returning a fixed integer assumes the vendor has a fixed capacity
we can discover once. Vertex governs Gemini with Dynamic Shared Quota, which has no
published per-project ceiling and varies with what everyone else in the region is
doing. A number tuned on Tuesday is wrong on Wednesday, and it is wrong in both
directions: too high and we bury a struggling backend, too low and we leave
throughput unclaimed.

Why errors alone are not enough
-------------------------------
The obvious design is AIMD on 429s: back off when rate limited, creep up otherwise.
It fails here for a specific, measured reason — **Vertex frequently does not reject
excess load, it just slows down.** Runs at 500 concurrent have produced zero 429s and
several-fold latency inflation instead. A controller watching only error codes sees a
perfectly healthy service and keeps climbing.

So latency is the primary signal and errors are an override:

* **Latency gradient.** Compare a long-term baseline of the fastest observed
  round-trips against a short-term average. When the short-term average drifts above
  the baseline, requests are queueing somewhere and the limit comes down
  proportionally — before any error appears.
* **Errors.** A 429, 503 or timeout triggers immediate multiplicative decrease. This
  is the safety net, not the main mechanism.

Why gradient rather than plain AIMD
-----------------------------------
Additive increase of one permit per success converges far too slowly to be useful in
a load test or a traffic spike: climbing from 16 to 64 takes on the order of a
thousand successful requests. The gradient form multiplies toward the estimated
capacity and adds an allowance proportional to ``sqrt(limit)``, so it reaches a new
operating point in tens of requests rather than thousands.

Honest scope
------------
Against a backend with genuinely fixed capacity, a well-tuned constant performs about
as well as this does — the constant simply has to be tuned, and re-tuned. Adaptive
limiting earns its place when capacity *moves*, which is precisely the case with
shared quota.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from enum import StrEnum


class Outcome(StrEnum):
    SUCCESS = "success"
    # Vendor pushed back explicitly: 429, 503, or our own timeout.
    DROP = "drop"
    # Failures that say nothing about capacity, e.g. a safety block or a bad request.
    IGNORE = "ignore"


@dataclass
class AdaptiveConfig:
    initial_limit: float = 16.0
    min_limit: float = 1.0
    max_limit: float = 512.0

    # Multiplicative decrease applied on an explicit rejection.
    backoff_ratio: float = 0.7
    # How much of a computed change to apply per sample. Low values keep the limit
    # from chasing individual slow requests.
    smoothing: float = 0.2
    # Floor on the gradient, bounding how aggressively latency alone can cut the
    # limit in one step.
    min_gradient: float = 0.5
    # Growth is suppressed unless in-flight is at least this fraction of the limit.
    # Without it the limit inflates during idle periods and the first burst of real
    # traffic is admitted against a number that was never tested.
    utilisation_threshold: float = 0.5
    # The baseline decays upward slowly so a genuinely slower backend is eventually
    # accepted as the new normal rather than treated as permanent congestion.
    baseline_decay: float = 0.99
    # Re-probe the baseline periodically; without this, one unusually fast early
    # sample pins it forever and everything afterwards looks congested.
    baseline_reset_after_s: float = 60.0


@dataclass
class AdaptiveState:
    limit: float
    inflight: int = 0
    baseline_rtt_s: float | None = None
    short_rtt_s: float | None = None
    gradient: float = 1.0
    samples: int = 0
    drops: int = 0
    last_baseline_reset: float = field(default_factory=time.monotonic)


class AdaptiveLimiter:
    """Concurrency limit that tracks observed capacity.

    Not a rate limiter: it bounds *in-flight* requests. Little's Law then relates that
    to throughput via latency, which is the relationship we actually want to hold when
    the backend slows down.
    """

    def __init__(self, config: AdaptiveConfig | None = None) -> None:
        self.config = config or AdaptiveConfig()
        self.state = AdaptiveState(limit=self.config.initial_limit)
        self._lock = asyncio.Lock()
        self._waiters: list[asyncio.Future[None]] = []

    # -- observation ---------------------------------------------------------

    def observe(self, *, outcome: Outcome, rtt_s: float, inflight: int) -> None:
        """Feed one completed request into the controller."""
        c, s = self.config, self.state
        s.samples += 1

        if outcome is Outcome.IGNORE:
            return

        if outcome is Outcome.DROP:
            s.drops += 1
            s.limit = max(c.min_limit, s.limit * c.backoff_ratio)
            # A rejected request's latency says nothing about service speed, and its
            # short-term average would otherwise poison the gradient.
            s.short_rtt_s = None
            self._clamp()
            return

        now = time.monotonic()
        if now - s.last_baseline_reset > c.baseline_reset_after_s:
            # Periodic re-probe: let the baseline drift back toward current reality.
            s.baseline_rtt_s = rtt_s
            s.last_baseline_reset = now
        elif s.baseline_rtt_s is None or rtt_s < s.baseline_rtt_s:
            s.baseline_rtt_s = rtt_s
        else:
            s.baseline_rtt_s = min(
                s.baseline_rtt_s / c.baseline_decay, max(s.baseline_rtt_s, rtt_s)
            )

        s.short_rtt_s = (
            rtt_s if s.short_rtt_s is None else s.short_rtt_s * 0.8 + rtt_s * 0.2
        )

        baseline = s.baseline_rtt_s or rtt_s
        short = s.short_rtt_s or rtt_s
        # Below 1.0 means we are slower than our best observed time, i.e. queueing.
        s.gradient = max(c.min_gradient, min(1.0, baseline / max(short, 1e-9)))

        # Netflix-style: allow a queue proportional to sqrt(limit), which keeps the
        # controller from oscillating at small limits while still permitting growth.
        queue_allowance = math.sqrt(max(1.0, s.limit))
        target = s.limit * s.gradient + queue_allowance

        growing = target > s.limit
        utilised = inflight >= s.limit * c.utilisation_threshold
        if growing and not utilised:
            # Do not grow on evidence we never gathered.
            return

        s.limit = s.limit * (1 - c.smoothing) + target * c.smoothing
        self._clamp()

    def _clamp(self) -> None:
        c, s = self.config, self.state
        s.limit = max(c.min_limit, min(c.max_limit, s.limit))

    @property
    def limit(self) -> int:
        return max(1, int(self.state.limit))

    def snapshot(self) -> dict[str, float]:
        s = self.state
        return {
            "limit": round(s.limit, 2),
            "inflight": s.inflight,
            "gradient": round(s.gradient, 3),
            "baseline_rtt_ms": round((s.baseline_rtt_s or 0) * 1000, 1),
            "short_rtt_ms": round((s.short_rtt_s or 0) * 1000, 1),
            "samples": s.samples,
            "drops": s.drops,
        }

    # -- admission -----------------------------------------------------------

    def try_acquire(self) -> bool:
        """Non-blocking admission. False means shed."""
        if self.state.inflight >= self.limit:
            return False
        self.state.inflight += 1
        return True

    def release(self) -> None:
        self.state.inflight = max(0, self.state.inflight - 1)
        # Wake one waiter, if anybody is queueing rather than shedding.
        while self._waiters:
            fut = self._waiters.pop(0)
            if not fut.done():
                fut.set_result(None)
                break

    async def acquire(self, timeout_s: float | None = None) -> bool:
        """Blocking admission, for callers that prefer to queue rather than shed."""
        if self.try_acquire():
            return True
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        self._waiters.append(fut)
        try:
            if timeout_s is None:
                await fut
            else:
                await asyncio.wait_for(fut, timeout_s)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if fut in self._waiters:
                self._waiters.remove(fut)
            return False
        return self.try_acquire()
