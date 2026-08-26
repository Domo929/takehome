#!/usr/bin/env python3
"""Does adaptive limiting actually beat a fixed one?

A previous candidate submission built an adaptive limiter, measured it against a fixed
cap, and found it lost. That result is worth taking seriously rather than assuming the
clever thing wins, so this experiment is built to give the fixed cap its best shot.

The honest framing: **against constant capacity a well-tuned fixed limit is fine.**
The problem is that "well-tuned" presumes capacity you can discover once. Vertex uses
Dynamic Shared Quota, so the ceiling moves with regional demand. A fixed limit must
therefore be chosen for the worst case, and then it wastes throughput for the rest of
the time.

So this runs three configurations against a backend whose capacity changes mid-run:

  fixed-high   tuned for the good period; suffers when capacity drops
  fixed-low    tuned for the bad period; safe, but leaves throughput unclaimed
  adaptive     no tuning; tracks whatever capacity is currently available

Phases: healthy -> degraded -> healthy. The question is not "which wins one phase"
but which is acceptable across all of them, since production sees all of them.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm.adaptive import AdaptiveConfig  # noqa: E402
from llm.errors import LLMError  # noqa: E402
from llm.gemini import Gemini  # noqa: E402

MOCK = os.getenv("MOCK_BASE_URL", "http://127.0.0.1:8088")
QUESTION = "Which robot vacuum brands are worth considering?"
SYSTEM = "You are a market research assistant. Answer concisely."


def configure_mock(**kw) -> None:
    httpx.post(f"{MOCK}/__configure", json=kw, timeout=5.0).raise_for_status()


class Phase:
    def __init__(self, name: str, seconds: float, **mock_cfg):
        self.name, self.seconds, self.mock_cfg = name, seconds, mock_cfg


class Result:
    def __init__(self, label: str):
        self.label = label
        self.ok = 0
        self.shed = 0
        self.errors = 0
        self.latencies: list[float] = []
        self.limit_samples: list[float] = []
        self.per_phase: dict[str, dict] = {}

    def snapshot_phase(self, name: str, elapsed: float) -> None:
        lat = sorted(self.latencies)
        self.per_phase[name] = {
            "ok": self.ok,
            "shed": self.shed,
            "errors": self.errors,
            "rps": round(self.ok / elapsed, 1) if elapsed else 0.0,
            "p50": round(lat[len(lat) // 2] * 1000, 0) if lat else 0,
            "p99": round(lat[int(len(lat) * 0.99)] * 1000, 0) if lat else 0,
            "mean_limit": round(statistics.fmean(self.limit_samples), 1)
            if self.limit_samples
            else 0,
        }
        self.ok = self.shed = self.errors = 0
        self.latencies = []
        self.limit_samples = []


async def drive(provider: Gemini, result: Result, offered: int, stop: asyncio.Event,
                fixed_limit: int | None) -> None:
    """Keep ``offered`` requests in flight, respecting whichever limit is in force."""
    sem = asyncio.Semaphore(fixed_limit) if fixed_limit else None

    async def one() -> None:
        limiter = provider.limiter
        if limiter is not None:
            if not limiter.try_acquire():
                result.shed += 1
                await asyncio.sleep(0.01)
                return
        started = time.perf_counter()
        try:
            if sem is not None:
                async with sem:
                    await provider.ask_generic_question(SYSTEM, QUESTION, 1.0)
            else:
                await provider.ask_generic_question(SYSTEM, QUESTION, 1.0)
            result.ok += 1
            result.latencies.append(time.perf_counter() - started)
        except LLMError:
            result.errors += 1
        finally:
            if limiter is not None:
                limiter.release()
                result.limit_samples.append(limiter.state.limit)

    async def worker() -> None:
        while not stop.is_set():
            await one()

    await asyncio.gather(*(worker() for _ in range(offered)))


async def run_config(label: str, phases: list[Phase], *, adaptive: bool,
                     fixed_limit: int | None, offered: int) -> Result:
    provider = Gemini(
        backend="vertex",
        project="fake",
        location="global",
        base_url=MOCK,
        max_connections=512,
        thinking_budget=0,
        adaptive=adaptive,
        adaptive_config=AdaptiveConfig(initial_limit=32.0, min_limit=1.0, max_limit=256.0)
        if adaptive
        else None,
    )
    result = Result(label)
    stop = asyncio.Event()
    task = asyncio.create_task(drive(provider, result, offered, stop, fixed_limit))

    for phase in phases:
        configure_mock(**phase.mock_cfg)
        started = time.perf_counter()
        await asyncio.sleep(phase.seconds)
        result.snapshot_phase(phase.name, time.perf_counter() - started)

    stop.set()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    return result


async def main_async(args: argparse.Namespace) -> None:
    # Healthy: plenty of headroom. Degraded: capacity collapses, which the mock
    # expresses the way Vertex does - mostly latency inflation, with some rejection.
    phases = [
        Phase("healthy", args.phase_s, base_latency_s=0.15, latency_sigma=0.1,
              knee_concurrency=100000, saturation_concurrency=100000,
              rate_limit_probability=0.0),
        Phase("degraded", args.phase_s, base_latency_s=0.15, latency_sigma=0.1,
              knee_concurrency=8, saturation_concurrency=40,
              inflation_factor=6.0, rate_limit_probability=0.05),
        Phase("recovered", args.phase_s, base_latency_s=0.15, latency_sigma=0.1,
              knee_concurrency=100000, saturation_concurrency=100000,
              rate_limit_probability=0.0),
    ]

    configs = [
        ("fixed-high (64)", False, 64),
        ("fixed-low (8)", False, 8),
        ("adaptive", True, None),
    ]

    results = []
    for label, adaptive, fixed in configs:
        print(f"  running {label} ...", flush=True)
        results.append(
            await run_config(label, phases, adaptive=adaptive, fixed_limit=fixed,
                             offered=args.offered)
        )

    print("\n" + "=" * 92)
    for phase in phases:
        print(f"\n{phase.name.upper()}")
        print(f"  {'config':<18} {'rps':>8} {'p50':>8} {'p99':>9} {'errors':>8} {'shed':>7} {'limit':>7}")
        for r in results:
            d = r.per_phase[phase.name]
            limit = f"{d['mean_limit']:.0f}" if d["mean_limit"] else "-"
            print(f"  {r.label:<18} {d['rps']:>8.1f} {d['p50']:>8.0f} {d['p99']:>9.0f} "
                  f"{d['errors']:>8} {d['shed']:>7} {limit:>7}")

    print("\n" + "=" * 92)
    print("\nTotals across all phases (what production actually experiences):")
    print(f"  {'config':<18} {'total ok':>10} {'total errors':>14} {'worst p99':>11}")
    for r in results:
        ok = sum(p["ok"] for p in r.per_phase.values())
        err = sum(p["errors"] for p in r.per_phase.values())
        worst = max(p["p99"] for p in r.per_phase.values())
        print(f"  {r.label:<18} {ok:>10} {err:>14} {worst:>10.0f}ms")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--phase-s", type=float, default=12.0)
    p.add_argument("--offered", type=int, default=96,
                   help="Concurrent request slots offered by the driver.")
    args = p.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
