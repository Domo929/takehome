#!/usr/bin/env python3
"""Bootstrap confidence intervals for the headline ratios, from data already on disk.

Why
---
This document reports ratios like "4.0x cheaper with thinking off" and "1.86x longer
grounded answers" as if they were constants. They are estimates from small samples --
n=15 per thinking configuration, n=20 for the paired grounding run -- and a reader is
entitled to ask how much of the spread is real.

Rather than quietly re-run everything at larger n, this recomputes intervals from the
raw JSONL already committed. No new requests, no new spend, and the intervals either
support the claims or they do not.

Method: percentile bootstrap, 10,000 resamples. For paired data the resampling is over
*pairs*, not over the two arms independently, because pairing is doing the work of
removing prompt-to-prompt variance and resampling independently would throw it away.

A ratio whose 95% interval spans 1.0 is not a demonstrated effect, and this script says
so explicitly rather than leaving the reader to check.
"""

from __future__ import annotations

import argparse
import glob
import json
import pathlib
import random
import statistics

REPO = pathlib.Path(__file__).resolve().parent.parent
RESAMPLES = 10_000


def boot_ratio(
    num: list[float], den: list[float], *, paired: bool, rng: random.Random
) -> tuple[float, float, float]:
    """Percentile CI for mean(num) / mean(den)."""
    point = statistics.fmean(num) / statistics.fmean(den)
    draws = []
    if paired:
        n = len(num)
        for _ in range(RESAMPLES):
            idx = [rng.randrange(n) for _ in range(n)]
            a = statistics.fmean([num[i] for i in idx])
            b = statistics.fmean([den[i] for i in idx])
            draws.append(a / b if b else float("nan"))
    else:
        for _ in range(RESAMPLES):
            a = statistics.fmean([rng.choice(num) for _ in num])
            b = statistics.fmean([rng.choice(den) for _ in den])
            draws.append(a / b if b else float("nan"))
    draws.sort()
    return point, draws[int(0.025 * RESAMPLES)], draws[int(0.975 * RESAMPLES)]


def boot_proportion(k: int, n: int, rng: random.Random) -> tuple[float, float, float]:
    point = k / n
    draws = sorted(
        sum(1 for _ in range(n) if rng.random() < point) / n for _ in range(RESAMPLES)
    )
    return point, draws[int(0.025 * RESAMPLES)], draws[int(0.975 * RESAMPLES)]


def show(label: str, point: float, lo: float, hi: float, unit: str = "x") -> None:
    spans_one = lo <= 1.0 <= hi and unit == "x"
    flag = "   <-- interval spans 1.0, not a demonstrated effect" if spans_one else ""
    print(f"  {label:<44}{point:>8.2f}{unit}  [{lo:.2f}, {hi:.2f}]{flag}")


def latest(pattern: str) -> pathlib.Path | None:
    hits = sorted(glob.glob(str(REPO / pattern)))
    return pathlib.Path(hits[-1]) if hits else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, default=20260824)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    print(f"Percentile bootstrap, {RESAMPLES:,} resamples, 95% intervals")
    print("Recomputed from committed raw data. No new requests.\n")

    # --- Grounding, paired (section 0c) -----------------------------------------
    f = latest("results/real/grounding-*.jsonl")
    if f:
        rows = [json.loads(line) for line in f.read_text().splitlines() if line]
        pairs: dict[str, dict] = {}
        for r in rows:
            if "error" not in r:
                pairs.setdefault(r["prompt_id"], {})[r["condition"]] = r
        both = [p for p in pairs.values() if len(p) == 2]
        g_out = [p["grounded"]["output_tokens"] for p in both]
        u_out = [p["ungrounded"]["output_tokens"] for p in both]
        g_lat = [p["grounded"]["latency_ms"] for p in both]
        u_lat = [p["ungrounded"]["latency_ms"] for p in both]
        g_in = [p["grounded"]["input_tokens"] for p in both]
        u_in = [p["ungrounded"]["input_tokens"] for p in both]

        print(f"Grounding, paired (n={len(both)} prompts, section 0c)")
        show("output tokens grounded/ungrounded",
             *boot_ratio(g_out, u_out, paired=True, rng=rng))
        show("latency grounded/ungrounded",
             *boot_ratio(g_lat, u_lat, paired=True, rng=rng))
        show("input tokens grounded/ungrounded",
             *boot_ratio(g_in, u_in, paired=True, rng=rng))
        k = sum(1 for p in both if p["grounded"]["truncated"])
        pt, lo, hi = boot_proportion(k, len(both), rng)
        print(f"  {'grounded truncation at 512':<44}{pt:>8.0%}   "
              f"[{lo:.0%}, {hi:.0%}]")
        print()

    # --- Production unit, unpaired arms (section 0d) -----------------------------
    f = latest("results/real/production-unit-*.jsonl")
    if f:
        rows = [json.loads(line) for line in f.read_text().splitlines() if line]
        g = [r for r in rows if r.get("arm") == "grounded" and "error" not in r]
        u = [r for r in rows if r.get("arm") == "ungrounded" and "error" not in r]
        print(f"Production unit (n={len(g)} per arm, section 0d)")
        show("output tokens grounded/ungrounded",
             *boot_ratio([r["output_tokens"] for r in g],
                         [r["output_tokens"] for r in u], paired=False, rng=rng))
        show("latency grounded/ungrounded",
             *boot_ratio([r["latency_ms"] for r in g],
                         [r["latency_ms"] for r in u], paired=False, rng=rng))
        pt, lo, hi = boot_proportion(
            sum(1 for r in g if r["truncated"]), len(g), rng)
        print(f"  {'grounded truncation at 1536':<44}{pt:>8.0%}   "
              f"[{lo:.0%}, {hi:.0%}]")

        # Brand share is the product signal, so it gets an interval too.
        from scripts.production_unit import brands

        print(f"\n  Brand share, 95% CI on the difference (n={len(g)} per arm)")
        gb = [brands(r["answer"]) for r in g]
        ub = [brands(r["answer"]) for r in u]
        names = {b for s in gb + ub for b in s}
        deltas = []
        for name in names:
            gk = sum(1 for s in gb if name in s)
            uk = sum(1 for s in ub if name in s)
            if max(gk, uk) < 10:
                continue
            draws = sorted(
                sum(1 for _ in g if rng.random() < gk / len(g)) / len(g)
                - sum(1 for _ in u if rng.random() < uk / len(u)) / len(u)
                for _ in range(2000)
            )
            lo, hi = draws[50], draws[-51]
            deltas.append((gk / len(g) - uk / len(u), name, gk, uk, lo, hi))
        for d, name, gk, uk, lo, hi in sorted(deltas, key=lambda x: -abs(x[0]))[:8]:
            sig = "" if lo <= 0.0 <= hi else "  significant"
            print(f"    {name:<12} {uk:>3}% -> {gk:>3}%   delta {d:+.0%} "
                  f"[{lo:+.0%}, {hi:+.0%}]{sig}")
        print()

    # --- Thinking, from run manifests (section 4) --------------------------------
    # Explicit paths, not a glob. A glob here silently selected the `global`-region
    # runs over the us-central1 ones and reported a ratio from the wrong pair.
    tb0 = REPO / "results/real/uscentral-tb0-manifest.json"
    tb1 = REPO / "results/real/uscentral-tb-1-manifest.json"
    if not (tb0.exists() and tb1.exists()):
        tb0 = tb1 = None
    if tb0 and tb1:
        def stats(path: pathlib.Path) -> tuple[int, float, float, float]:
            m = json.loads(path.read_text())
            st = m["stages"][0]
            n = st["requests"]
            return (n, st["cost_usd"] / n, st["tokens"]["output"] / n,
                    st["latency_ms"]["p50"])

        n0, c0, o0, l0 = stats(tb0)
        n1, c1, o1, l1 = stats(tb1)
        print(f"Thinking, us-central1 (section 4), n={n0} and n={n1} per configuration")
        print(f"  dynamic/off cost ratio (point)              {c1 / c0:>8.2f}x")
        print(f"  dynamic/off output-token ratio (point)      {o1 / o0:>8.2f}x")
        print(f"  dynamic/off p50 latency ratio (point)       {l1 / l0:>8.2f}x")
        print("  No interval: manifests store per-stage totals, not per-request")
        print("  values, so the sample cannot be resampled. n=15 per configuration")
        print("  is thin for a 4x claim -- treat it as the right order of magnitude")
        print("  rather than a precise multiplier.")


if __name__ == "__main__":
    main()
