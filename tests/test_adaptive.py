"""Tests for the adaptive concurrency limiter.

The behaviours asserted here are the ones that decide whether the controller is
useful or actively harmful in production.
"""

from __future__ import annotations

import pytest

from llm.adaptive import AdaptiveConfig, AdaptiveLimiter, Outcome


def cfg(**kw) -> AdaptiveConfig:
    base = dict(initial_limit=16.0, min_limit=1.0, max_limit=256.0, smoothing=0.3)
    base.update(kw)
    return AdaptiveConfig(**base)


def test_drops_reduce_the_limit_immediately():
    """A rejection is unambiguous, so the response is multiplicative and instant."""
    lim = AdaptiveLimiter(cfg(initial_limit=64.0, backoff_ratio=0.7))
    start = lim.state.limit
    lim.observe(outcome=Outcome.DROP, rtt_s=0.1, inflight=64)
    assert lim.state.limit == pytest.approx(start * 0.7)

    for _ in range(5):
        lim.observe(outcome=Outcome.DROP, rtt_s=0.1, inflight=int(lim.state.limit))
    assert lim.state.limit < start * 0.2


def test_latency_inflation_alone_reduces_the_limit():
    """The case a 429-only controller misses entirely.

    Vertex often absorbs excess load by getting slower rather than rejecting, so a
    controller keyed on error codes sees a healthy service and keeps climbing. Here
    every request succeeds and latency degrades 5x; the limit must still come down.
    """
    lim = AdaptiveLimiter(cfg(initial_limit=64.0))

    for _ in range(30):
        lim.observe(outcome=Outcome.SUCCESS, rtt_s=0.10, inflight=64)
    settled = lim.state.limit

    for _ in range(60):
        lim.observe(outcome=Outcome.SUCCESS, rtt_s=0.50, inflight=int(lim.state.limit))

    assert lim.state.limit < settled * 0.9, (
        f"limit {lim.state.limit:.1f} did not fall from {settled:.1f} despite 5x latency"
    )
    assert lim.state.gradient < 1.0
    assert lim.state.drops == 0


def test_healthy_traffic_grows_the_limit_quickly():
    """Convergence speed is the difference between useful and academic.

    Plain additive increase needs on the order of a thousand successes to climb from
    16 to 64. The gradient form should get there in tens.
    """
    lim = AdaptiveLimiter(cfg(initial_limit=16.0, max_limit=256.0))
    for i in range(60):
        lim.observe(outcome=Outcome.SUCCESS, rtt_s=0.10, inflight=int(lim.state.limit))
        if lim.state.limit >= 64:
            break
    assert lim.state.limit >= 64, f"only reached {lim.state.limit:.1f} in 60 samples"
    assert i < 60


def test_limit_does_not_grow_while_idle():
    """Growth must be earned.

    If the limit inflates during quiet periods, the first real burst is admitted
    against a number no traffic ever justified.
    """
    lim = AdaptiveLimiter(cfg(initial_limit=16.0, utilisation_threshold=0.5))
    start = lim.state.limit
    for _ in range(50):
        lim.observe(outcome=Outcome.SUCCESS, rtt_s=0.05, inflight=1)
    assert lim.state.limit == pytest.approx(start), "limit grew without load to justify it"


def test_limit_respects_bounds():
    lim = AdaptiveLimiter(cfg(initial_limit=8.0, min_limit=2.0, max_limit=32.0))
    for _ in range(200):
        lim.observe(outcome=Outcome.DROP, rtt_s=0.1, inflight=8)
    assert lim.state.limit >= 2.0

    lim = AdaptiveLimiter(cfg(initial_limit=8.0, min_limit=2.0, max_limit=32.0))
    for _ in range(500):
        lim.observe(outcome=Outcome.SUCCESS, rtt_s=0.05, inflight=int(lim.state.limit))
    assert lim.state.limit <= 32.0


def test_terminal_failures_do_not_move_the_limit():
    """A safety block says nothing about capacity, so it must not be treated as one."""
    lim = AdaptiveLimiter(cfg(initial_limit=16.0))
    start = lim.state.limit
    for _ in range(20):
        lim.observe(outcome=Outcome.IGNORE, rtt_s=0.1, inflight=16)
    assert lim.state.limit == pytest.approx(start)


def test_recovers_after_pressure_lifts():
    """Backing off is only half of it; unclaimed throughput is also a failure."""
    lim = AdaptiveLimiter(cfg(initial_limit=64.0))
    for _ in range(10):
        lim.observe(outcome=Outcome.DROP, rtt_s=0.1, inflight=int(lim.state.limit))
    bottom = lim.state.limit
    assert bottom < 10

    for _ in range(80):
        lim.observe(outcome=Outcome.SUCCESS, rtt_s=0.05, inflight=int(lim.state.limit))
    assert lim.state.limit > bottom * 3, (
        f"recovered only to {lim.state.limit:.1f} from {bottom:.1f}"
    )


def test_admission_sheds_past_the_limit():
    lim = AdaptiveLimiter(cfg(initial_limit=3.0))
    assert lim.try_acquire()
    assert lim.try_acquire()
    assert lim.try_acquire()
    assert not lim.try_acquire(), "admitted a fourth against a limit of three"
    lim.release()
    assert lim.try_acquire()


async def test_blocking_acquire_times_out_rather_than_hanging():
    lim = AdaptiveLimiter(cfg(initial_limit=1.0))
    assert await lim.acquire(timeout_s=0.1)
    assert not await lim.acquire(timeout_s=0.1)
    lim.release()
    assert await lim.acquire(timeout_s=0.1)


def test_baseline_reprobe_prevents_permanent_congestion_verdict():
    """One unusually fast early sample must not pin the baseline forever.

    Without periodic re-probing, a backend that legitimately becomes slower is read
    as permanently congested and the limit is throttled indefinitely.
    """
    lim = AdaptiveLimiter(cfg(initial_limit=32.0, baseline_reset_after_s=0.0))
    lim.observe(outcome=Outcome.SUCCESS, rtt_s=0.01, inflight=32)
    for _ in range(40):
        lim.observe(outcome=Outcome.SUCCESS, rtt_s=0.20, inflight=int(lim.state.limit))
    # With the baseline re-probed to the prevailing rate, this is the new normal and
    # the gradient should return to healthy rather than sitting at the floor.
    assert lim.state.gradient > 0.9
