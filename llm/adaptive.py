"""Adaptive concurrency limiting. Off by default.

The case for it and the measurements behind it are in FINDINGS 6b, including the
reason it ships disabled: its justification is that Dynamic Shared Quota moves, and I
have not proven that it does on any timescale that matters.

Two design choices that are not obvious from the code:

**Latency is the primary signal, errors are an override.** The obvious design is AIMD
on 429s -- additive-increase/multiplicative-decrease, creep up on success and halve on
rejection. That fails here because Vertex often does not reject excess load, it just
slows down, so an error-watching controller sees a healthy service and keeps climbing.
A long-term baseline of fastest observed round-trips is compared against a short-term
average; drift between them means queueing, and the limit drops before any error
appears.

**Gradient rather than additive increase.** Adding one permit per success climbs from
16 to 64 in roughly a thousand requests, which is slower than any traffic spike. The
gradient form multiplies toward estimated capacity plus an allowance proportional to
sqrt(limit), reaching a new operating point in tens of requests.
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
    # Low values stop the limit chasing individual slow requests.
    smoothing: float = 0.2
    # Bounds how far latency alone can cut the limit in one step.
    min_gradient: float = 0.5
    # Without this the limit inflates while idle, and the first real burst is
    # admitted against a number nothing ever tested.
    utilisation_threshold: float = 0.5
    # So a genuinely slower backend eventually becomes the new normal rather than
    # looking like permanent congestion.
    baseline_decay: float = 0.99
    # Without re-probing, one fast early sample pins the baseline forever.
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
            # A rejection's latency says nothing about speed; it would poison the
            # gradient.
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

        # Queue allowance proportional to sqrt(limit): stops oscillation at small
        # limits while still permitting growth.
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
