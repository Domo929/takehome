"""In-process experiment driver for the model, with a hard spend breaker.

This is not the load test. Load testing our service is k6's job (`loadtest/k6/`),
which drives `service/app.py` over HTTP from a separate process, the way production
traffic arrives. Two things keep this file separate rather than folded into that:

**Spend control that actually stops.** Accumulated *actual* cost, read from reported
`usage_metadata` rather than estimated, is checked before every dispatch, and the run
drains when it trips. k6 has no way to halt itself on spend mid-run. Against someone
else's cloud project that is not a nice-to-have.

**Access to the whole response.** Thinking tokens, finish reasons, grounding sources
and citations are read directly off the SDK object. Going through our own HTTP API
would mean measuring our serialisation alongside the model, and anything we did not
think to expose would simply be invisible.

The brief asks what we can learn about *the model*, not only whether our code holds
up. That is what this driver is for. Every model finding in FINDINGS came from here or
from `scripts/`; every service finding came from k6.

Closed loop vs open loop
------------------------
*Closed loop* holds N requests in flight and issues a new one as each finishes. It
models a batch pipeline with N workers, which is the shape of this workload. Its
weakness is coordinated omission: when the vendor slows down the driver issues fewer
requests, so recorded latency understates what a real arrival stream would see.

*Open loop* issues at a fixed arrival rate regardless of completions. It reproduces
bursts honestly and exposes queue growth, at the cost of being able to overwhelm the
driver itself.

Reporting only closed-loop numbers is the most common way a load test flatters what it
measures, so both are available and the mode is recorded in every manifest.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from harness.budget import BudgetExceeded, CostEstimate, CostGovernor, confirm_or_exit
from harness.workload import Prompt, build_corpus, corpus_fingerprint, mean_input_chars
from llm.errors import LLMError
from llm.gemini import Gemini
from llm.llm import LLM
from llm.metrics import EventLoopLagMonitor, serve as serve_metrics
from llm.pricing import cost_usd


@dataclass
class RequestRecord:
    """One attempt through the provider, success or failure."""

    prompt_id: str
    kind: str
    started_at: float
    latency_ms: float
    ok: bool
    finish_reason: str = ""
    error_class: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cost_usd: float = 0.0
    attempts: int = 1
    # Open loop only: how long the request waited past its scheduled start. Rising
    # schedule lag means the driver, not the service, is falling behind.
    schedule_lag_ms: float = 0.0


@dataclass
class StageResult:
    label: str
    mode: str
    concurrency: int
    arrival_rate: float
    duration_s: float
    records: list[RequestRecord] = field(default_factory=list)
    # Requests completed during warm-up. Kept for cost accounting (they were billed)
    # but excluded from latency and throughput, because a cold connection pool
    # measures TLS handshakes rather than the service.
    warmup_records: list[RequestRecord] = field(default_factory=list)
    warmup_s: float = 0.0
    # Timestamped client-health samples. Without these the claim "the ceiling is our
    # event loop, not the vendor" rests on a live dashboard nobody else can re-read.
    lag_samples: list[tuple[float, float]] = field(default_factory=list)
    pool_samples: list[tuple[float, float]] = field(default_factory=list)

    @property
    def all_records(self) -> list[RequestRecord]:
        return self.warmup_records + self.records

    def windows(
        self,
        width_s: float = 30.0,
        lag_samples: list[tuple[float, float]] | None = None,
        pool_samples: list[tuple[float, float]] | None = None,
    ) -> list[dict[str, Any]]:
        """Bucket the run into fixed windows.

        A single aggregate hides *when* something changed. Quota throttling, warm-up
        effects and drift are all visible only as a shape over time.
        """
        if not self.records:
            return []
        t0 = min(r.started_at for r in self.records)

        def bucket_peaks(samples: list[tuple[float, float]] | None) -> dict[int, float]:
            peaks: dict[int, float] = {}
            for ts, value in samples or []:
                idx = int((ts - t0) // width_s)
                if idx >= 0:
                    peaks[idx] = max(peaks.get(idx, 0.0), value)
            return peaks

        lag_peaks = bucket_peaks(lag_samples)
        pool_peaks = bucket_peaks(pool_samples)
        buckets: dict[int, list[RequestRecord]] = {}
        for r in self.records:
            buckets.setdefault(int((r.started_at - t0) // width_s), []).append(r)

        out = []
        for idx in sorted(buckets):
            rs = buckets[idx]
            oks = [r for r in rs if r.ok]
            lat = sorted(r.latency_ms for r in oks)
            errs: dict[str, int] = {}
            for r in rs:
                if not r.ok and r.error_class:
                    errs[r.error_class] = errs.get(r.error_class, 0) + 1
            out.append({
                "window_start_s": round(idx * width_s, 1),
                "requests": len(rs),
                "successful": len(oks),
                "rps": round(len(oks) / width_s, 2),
                "p50_ms": round(lat[len(lat) // 2], 0) if lat else 0,
                "p99_ms": round(lat[min(len(lat) - 1, int(len(lat) * 0.99))], 0) if lat else 0,
                "errors_by_class": errs,
                "retries": sum(max(0, r.attempts - 1) for r in rs),
                # Peak, not mean: the question these answer is "did we ever become the
                # bottleneck", and an average over 30 s hides exactly the spike that
                # would say yes.
                "event_loop_lag_ms": round(lag_peaks.get(idx, 0.0) * 1000, 1),
                "pool_saturation": round(pool_peaks.get(idx, 0.0), 3),
            })
        return out

    def summary(self) -> dict[str, Any]:
        oks = [r for r in self.records if r.ok]
        lat = sorted(r.latency_ms for r in oks)
        errors: dict[str, int] = {}
        finishes: dict[str, int] = {}
        for r in self.records:
            if not r.ok:
                errors[r.error_class] = errors.get(r.error_class, 0) + 1
            if r.finish_reason:
                finishes[r.finish_reason] = finishes.get(r.finish_reason, 0) + 1

        def pct(p: float) -> float:
            if not lat:
                return 0.0
            idx = min(len(lat) - 1, int(round((p / 100.0) * (len(lat) - 1))))
            return lat[idx]

        total = len(self.records)
        out_tokens = sum(r.output_tokens for r in oks)
        return {
            "label": self.label,
            "mode": self.mode,
            "concurrency": self.concurrency,
            "arrival_rate": self.arrival_rate,
            "duration_s": round(self.duration_s, 3),
            "requests": total,
            "successful": len(oks),
            "error_rate": round((total - len(oks)) / total, 5) if total else 0.0,
            "throughput_rps": round(len(oks) / self.duration_s, 3) if self.duration_s else 0.0,
            "output_tokens_per_s": round(out_tokens / self.duration_s, 1) if self.duration_s else 0.0,
            "latency_ms": {
                "p50": round(pct(50), 1),
                "p90": round(pct(90), 1),
                "p95": round(pct(95), 1),
                "p99": round(pct(99), 1),
                "max": round(lat[-1], 1) if lat else 0.0,
                "mean": round(statistics.fmean(lat), 1) if lat else 0.0,
            },
            "mean_schedule_lag_ms": (
                round(statistics.fmean([r.schedule_lag_ms for r in self.records]), 1)
                if self.mode == "open" and self.records else 0.0
            ),
            "retries": sum(max(0, r.attempts - 1) for r in self.records),
            "warmup_s": self.warmup_s,
            "warmup_requests": len(self.warmup_records),
            # Cost covers warm-up too: those requests were billed even though they
            # are excluded from the latency and throughput figures.
            "cost_usd": round(sum(r.cost_usd for r in self.all_records), 6),
            "tokens": {
                "input": sum(r.input_tokens for r in self.all_records),
                "output": sum(r.output_tokens for r in self.all_records),
                "thinking": sum(r.thinking_tokens for r in self.all_records),
            },
            "errors_by_class": errors,
            "finish_reasons": finishes,
            "windows": self.windows(
                lag_samples=self.lag_samples, pool_samples=self.pool_samples
            ),
        }


async def _one_request(
    provider: LLM,
    prompt: Prompt,
    governor: CostGovernor,
    *,
    scheduled_at: float = 0.0,
) -> RequestRecord:
    await governor.reserve()

    started = time.perf_counter()
    lag_ms = max(0.0, (started - scheduled_at) * 1000.0) if scheduled_at else 0.0
    try:
        result = await provider.ask_generic_question(prompt.system, prompt.question, 0.7)
    except LLMError as err:
        latency_ms = (time.perf_counter() - started) * 1000.0
        return RequestRecord(
            prompt_id=prompt.id, kind=prompt.kind, started_at=started,
            latency_ms=latency_ms, ok=False, error_class=err.error_class,
            schedule_lag_ms=lag_ms,
        )

    latency_ms = (time.perf_counter() - started) * 1000.0

    # These now live on the contract itself, but they are read defensively anyway:
    # a provider built against the pre-grounding contract still satisfies the type,
    # and the harness should degrade rather than crash on one.
    thinking_tokens = getattr(result, "thinking_tokens", 0)
    cost = getattr(result, "cost_usd", None)
    if cost is None:
        cost = cost_usd(getattr(result, "model", None), result.input_tokens, result.output_tokens)
    finish = getattr(result, "finish_reason", None)
    finish_label = getattr(finish, "value", "") if finish is not None else ""
    # Base responses carry no finish reason, so fall back to "has text".
    usable = getattr(result, "is_usable", bool(result.answer))

    await governor.record(
        cost_usd=cost,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        thinking_tokens=thinking_tokens,
    )
    return RequestRecord(
        prompt_id=prompt.id, kind=prompt.kind, started_at=started, latency_ms=latency_ms,
        ok=usable, finish_reason=finish_label,
        input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        thinking_tokens=thinking_tokens, cost_usd=cost,
        attempts=getattr(result, "attempts", 1), schedule_lag_ms=lag_ms,
    )


async def run_closed_loop(
    provider: LLM, prompts: list[Prompt], governor: CostGovernor,
    *, concurrency: int, requests: int | None = None, duration_s: float | None = None,
    label: str, progress_every_s: float = 30.0, warmup_s: float = 0.0,
) -> StageResult:
    """Hold ``concurrency`` requests in flight.

    Either run a fixed number of requests, or — for saturation testing — sustain the
    level for ``duration_s``. Duration is the mode that matters for quota: Vertex
    enforces limits per minute, so a run shorter than a minute cannot trigger a
    per-minute ceiling no matter how hard it pushes. A short burst that reports no
    429s has demonstrated nothing about quota.
    """
    if requests is None and duration_s is None:
        raise ValueError("one of requests or duration_s is required")

    stage = StageResult(
        label=label, mode="closed", concurrency=concurrency, arrival_rate=0.0,
        duration_s=0.0, warmup_s=warmup_s,
    )
    stopped = asyncio.Event()
    started = time.perf_counter()
    issued = 0
    last_progress = started

    async def worker(worker_id: int) -> None:
        nonlocal issued, last_progress
        while not stopped.is_set():
            if duration_s is not None:
                if time.perf_counter() - started >= duration_s:
                    return
            else:
                if issued >= (requests or 0):
                    return
            index = issued
            issued += 1
            try:
                record = await _one_request(provider, prompts[index % len(prompts)], governor)
            except BudgetExceeded as exc:
                if not stopped.is_set():
                    print(f"\n  budget breaker: {exc}")
                stopped.set()
                return
            # Warm-up requests are billed, so they count for cost, but they are
            # excluded from the measurement: the first seconds at a new concurrency
            # are dominated by TLS handshakes and cold pool slots, which is precisely
            # the artifact that made 8-second stages report throughput 2.5x low.
            if warmup_s and (time.perf_counter() - started) < warmup_s:
                stage.warmup_records.append(record)
            else:
                stage.records.append(record)

            now = time.perf_counter()
            if progress_every_s and now - last_progress >= progress_every_s:
                last_progress = now
                _print_progress(stage, now - started)

    await asyncio.gather(*(worker(i) for i in range(concurrency)))
    elapsed = time.perf_counter() - started
    # Report the measured window only, so throughput is requests-after-warmup over
    # seconds-after-warmup rather than a blend of the two regimes.
    stage.duration_s = max(0.001, elapsed - warmup_s)
    return stage


def _print_progress(stage: StageResult, elapsed: float) -> None:
    """Live progress during a long run, so a saturation test is watchable."""
    recent = [r for r in stage.records if r.started_at >= (stage.records[-1].started_at - 30)]
    ok = sum(1 for r in recent if r.ok)
    errs = len(recent) - ok
    lat = sorted(r.latency_ms for r in recent if r.ok)
    p50 = lat[len(lat) // 2] if lat else 0
    print(
        f"    [{elapsed:>5.0f}s] last-30s: {ok:>4} ok  {errs:>3} err  "
        f"p50 {p50:>6.0f}ms   total {len(stage.records)}",
        flush=True,
    )


async def run_open_loop(
    provider: LLM, prompts: list[Prompt], governor: CostGovernor,
    *, arrival_rate: float, duration_s: float, label: str,
) -> StageResult:
    """Dispatch at a fixed rate regardless of completions.

    Requests are scheduled against a wall-clock grid rather than by sleeping between
    dispatches, so the driver's own overhead does not silently reduce the arrival
    rate. The residual difference is recorded as ``schedule_lag_ms``.
    """
    stage = StageResult(
        label=label, mode="open", concurrency=0, arrival_rate=arrival_rate, duration_s=0.0
    )
    interval = 1.0 / arrival_rate
    tasks: list[asyncio.Task] = []
    stopped = asyncio.Event()
    started = time.perf_counter()
    index = 0

    async def dispatch(prompt: Prompt, scheduled_at: float) -> None:
        try:
            record = await _one_request(provider, prompt, governor, scheduled_at=scheduled_at)
        except BudgetExceeded as exc:
            if not stopped.is_set():
                print(f"\n  budget breaker: {exc}")
            stopped.set()
            return
        stage.records.append(record)

    while not stopped.is_set():
        scheduled = started + index * interval
        now = time.perf_counter()
        if scheduled > now:
            await asyncio.sleep(scheduled - now)
        if time.perf_counter() - started >= duration_s:
            break
        tasks.append(asyncio.create_task(dispatch(prompts[index % len(prompts)], scheduled)))
        index += 1

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    stage.duration_s = time.perf_counter() - started
    return stage


def write_ledger(path: Path, stage: StageResult) -> None:
    """Per-request JSONL, so the analysis can be regenerated without re-spending."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for record in stage.records:
            fh.write(json.dumps(asdict(record)) + "\n")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Load harness for the Gemini provider. Dry run unless --confirm is passed."
    )
    p.add_argument("--mode", choices=["closed", "open"], default="closed")
    p.add_argument("--concurrency", type=int, nargs="+", default=[8],
                   help="Closed loop: in-flight request cap. Accepts several for a sweep.")
    p.add_argument("--arrival-rate", type=float, nargs="+", default=[5.0],
                   help="Open loop: requests per second. Accepts several for a sweep.")
    p.add_argument("--requests", type=int, default=None, help="Closed loop: requests per stage.")
    p.add_argument("--duration", type=float, default=30.0,
                   help="Seconds per stage. Used by open loop, and by closed loop when "
                        "--requests is omitted. Sustained duration is what a saturation "
                        "test needs: Vertex quota is per-minute, so a shorter run cannot "
                        "trigger it.")
    p.add_argument("--corpus-size", type=int, default=200)
    p.add_argument("--complex-fraction", type=float, default=0.0)
    p.add_argument(
        "--repeat-prompt", action="store_true",
        help=(
            "Repeat one prompt instead of building distinct ones. This is the real "
            "unit of work (one prompt sampled N times); distinct prompts are the "
            "right shape for throughput measurement. See FINDINGS 0b."
        ),
    )
    p.add_argument("--thinking-budget", type=int, default=None)
    p.add_argument("--max-output-tokens", type=int, default=None)
    p.add_argument("--max-connections", type=int, default=None)
    p.add_argument("--budget-usd", type=float, default=1.0, help="Hard ceiling. The run stops at it.")
    p.add_argument("--est-output-tokens", type=int, default=200,
                   help="Expected output tokens per request, used for the pre-flight estimate.")
    p.add_argument("--confirm", action="store_true", help="Actually spend money.")
    p.add_argument("--warmup-s", type=float, default=0.0,
                   help="Seconds to discard at the start of each stage. Excluded from "
                        "latency and throughput, still counted for cost. Makes short "
                        "stages valid by removing TLS and connection-pool warm-up.")
    p.add_argument("--metrics-port", type=int, default=0, help="Serve /metrics for Prometheus.")
    p.add_argument("--out", type=Path, default=Path("results"))
    p.add_argument("--label", default="run")
    return p.parse_args()


async def main_async(args: argparse.Namespace) -> None:
    prompts = build_corpus(
        size=args.corpus_size,
        complex_fraction=args.complex_fraction,
        repeat_prompt=args.repeat_prompt,
    )

    stages = args.concurrency if args.mode == "closed" else args.arrival_rate
    per_stage = args.requests if args.mode == "closed" else None
    if args.mode == "closed":
        if per_stage is not None:
            total_requests = per_stage * len(stages)
        else:
            # Duration mode: estimate from concurrency and an assumed per-request time
            # so the cost pre-flight still means something.
            assumed_s = float(os.getenv("ASSUMED_REQUEST_S", "1.5"))
            total_requests = int(
                sum(c * args.duration / assumed_s for c in args.concurrency)
            )
    else:
        total_requests = int(sum(rate * args.duration for rate in args.arrival_rate))

    est_input_tokens = int(mean_input_chars(prompts) * 0.27) + 32
    estimate = CostEstimate(
        requests=total_requests,
        input_tokens_each=est_input_tokens,
        output_tokens_each=args.est_output_tokens,
        model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
    )
    confirm_or_exit(estimate, confirmed=args.confirm, budget_usd=args.budget_usd)

    provider = Gemini(
        thinking_budget=args.thinking_budget,
        max_output_tokens=args.max_output_tokens,
        max_connections=args.max_connections,
    )
    governor = CostGovernor(
        budget_usd=args.budget_usd,
        model=provider.model,
        expected_cost_per_request=estimate.total_usd / max(1, total_requests),
    )

    if args.metrics_port:
        serve_metrics(args.metrics_port)
        print(f"  metrics on :{args.metrics_port}/metrics")

    lag = EventLoopLagMonitor()
    lag.start()

    manifest: dict[str, Any] = {
        "label": args.label,
        "mode": args.mode,
        "provider": provider.describe(),
        "corpus": {
            "size": len(prompts),
            "fingerprint": corpus_fingerprint(prompts),
            "mean_input_chars": round(mean_input_chars(prompts), 1),
            "complex_fraction": args.complex_fraction,
            "repeat_prompt": args.repeat_prompt,
        },
        "estimate_usd": round(estimate.total_usd, 6),
        "started_at": time.time(),
        "stages": [],
    }

    print(f"\nRunning {args.mode} loop against {provider.backend}:{provider.model}\n")
    try:
        for value in stages:
            if args.mode == "closed":
                if per_stage is not None:
                    print(f"  stage concurrency={value} requests={per_stage} ...", flush=True)
                else:
                    warm = f", {args.warmup_s:.0f}s warm-up discarded" if args.warmup_s else ""
                    print(
                        f"  stage concurrency={value} duration={args.duration}s"
                        f"{warm} ...", flush=True
                    )
                stage_t0 = time.perf_counter()
                stage = await run_closed_loop(
                    provider, prompts, governor,
                    concurrency=int(value),
                    requests=per_stage,
                    duration_s=None if per_stage is not None else args.duration,
                    label=f"{args.label}-c{int(value)}",
                    warmup_s=args.warmup_s,
                )
            else:
                print(f"  stage arrival_rate={value}/s duration={args.duration}s ...", flush=True)
                stage_t0 = time.perf_counter()
                stage = await run_open_loop(
                    provider, prompts, governor,
                    arrival_rate=float(value), duration_s=args.duration,
                    label=f"{args.label}-r{value}",
                )

            # Attach the client-health samples that fall inside this stage, so the
            # manifest can answer "was the client the bottleneck" without a live
            # dashboard.
            stage.lag_samples = [x for x in lag.samples if x[0] >= stage_t0]
            stage.pool_samples = [x for x in lag.pool_samples if x[0] >= stage_t0]
            summary = stage.summary()
            manifest["stages"].append(summary)
            write_ledger(args.out / f"{stage.label}.jsonl", stage)
            print(
                f"    {summary['successful']}/{summary['requests']} ok  "
                f"{summary['throughput_rps']} rps  "
                f"p50 {summary['latency_ms']['p50']}ms  "
                f"p99 {summary['latency_ms']['p99']}ms  "
                f"${summary['cost_usd']:.4f}  ({summary['duration_s']:.0f}s)"
            )
            wins = summary.get("windows") or []
            if len(wins) > 1:
                print("      per-30s window:")
                for w in wins:
                    err = sum(w["errors_by_class"].values())
                    flag = f"  errors={w['errors_by_class']}" if err else ""
                    print(
                        f"        t+{w['window_start_s']:>5.0f}s  {w['rps']:>6.2f} rps  "
                        f"p50 {w['p50_ms']:>6.0f}  p99 {w['p99_ms']:>7.0f}  "
                        f"retries={w['retries']}{flag}"
                    )
            if governor.tripped:
                print("  stopping: budget exhausted")
                break
    finally:
        await lag.stop()

    manifest["actual_usd"] = round(governor.spent_usd, 6)
    manifest["finished_at"] = time.time()
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"{args.label}-manifest.json").write_text(json.dumps(manifest, indent=2))

    print("\nCost reconciliation")
    print(governor.summary(estimate))
    print(f"\nWrote {args.out / f'{args.label}-manifest.json'}")


def main() -> None:
    asyncio.run(main_async(parse_args()))


if __name__ == "__main__":
    main()
