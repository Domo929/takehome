"""How far apart do two identical measurements land by pure chance?

FINDINGS originally answered this with one number, five percentage points, taken as the
mean per-brand drift between two 30-sample halves. That number is real but it was used
as a threshold, and a mean is the wrong statistic for a threshold. It also came from
n=30 while production samples 100, and it was quoted globally when the answer depends on
where the brand sits.

This simulates the question an alert actually asks: two independent samples of the same
brand at the same setting, nothing changed. How big a gap should not surprise anyone?

Pure simulation, no data and no requests. The point is that the threshold is knowable in
advance from the sample size, which is why every rate should ship with its interval
rather than be compared against a constant.

    python scripts/noise_floor.py
"""

from __future__ import annotations

import argparse
import random
import statistics

# Matches the seed convention in scripts/confidence.py so runs are reproducible.
SEED = 20260826


def gap_distribution(p: float, n: int, trials: int, rng: random.Random) -> list[float]:
    """Absolute difference in measured rate between two independent n-sample runs."""
    out = []
    for _ in range(trials):
        a = sum(rng.random() < p for _ in range(n)) / n
        b = sum(rng.random() < p for _ in range(n)) / n
        out.append(abs(a - b) * 100)
    out.sort()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trials", type=int, default=20_000)
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    print("Two independent samples of the same brand, same setting, nothing changed.")
    print(f"{args.trials:,} trials per row.\n")
    print(f"{'true rate':>10}{'n':>6}{'mean gap':>11}{'95th pct':>11}{'99th pct':>11}")
    for p in (0.05, 0.10, 0.30, 0.50, 0.70, 0.90):
        for n in (30, 100):
            d = gap_distribution(p, n, args.trials, rng)
            print(
                f"{p:>9.0%}{n:>6}{statistics.fmean(d):>10.1f}"
                f"{d[int(args.trials * 0.95)]:>11.1f}{d[int(args.trials * 0.99)]:>11.1f}"
            )

    print("\nReading this:")
    print("  - At n=100 a brand near 50% can move 14 points on noise alone, 1 run in 20.")
    print("  - A niche brand near 10% only needs 8 points to clear the same bar.")
    print("  - Noise peaks in the middle of the range, which is exactly the band where")
    print("    a share is informative. The brands worth watching are the hardest to call.")
    print("  - So a single global threshold is the wrong instrument. Ship the interval.")


if __name__ == "__main__":
    main()
