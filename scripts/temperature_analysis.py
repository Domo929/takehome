#!/usr/bin/env python3
"""Analyse the temperature x category sweep. Reads committed data, spends nothing.

The pilot (FINDINGS 0e, one category) found that a brand's measured share swings with
temperature, and that the mechanism looked like phrasing: Anker was reported almost
only as a parenthetical attribution inside "Eufy (Anker)", and temperature changed how
often the model bothered with the aside.

That is a hypothesis built on one brand in one category. This tests it across eleven,
and states in advance what would falsify it: if aside-mentioned brands are not more
temperature-sensitive than directly-mentioned ones, the pilot found something
category-specific and the general claim should be withdrawn.
"""

from __future__ import annotations

import argparse
import glob
import json
import pathlib
import random
import statistics
from collections import Counter, defaultdict

REPO = pathlib.Path(__file__).resolve().parent.parent


def load(pattern: str) -> list[dict]:
    hits = sorted(glob.glob(str(REPO / pattern)))
    if not hits:
        raise SystemExit(f"no data matching {pattern}")
    rows = [json.loads(line) for line in pathlib.Path(hits[-1]).read_text().splitlines() if line]
    return [r for r in rows if "error" not in r]


def rate_table(rows: list[dict]) -> dict[tuple[str, str], dict[float, float]]:
    """(category, brand) -> {temperature: mention rate}."""
    counts: dict[tuple[str, str, float], int] = Counter()
    totals: dict[tuple[str, float], int] = Counter()
    for r in rows:
        key = (r["category"], r["temperature"])
        totals[key] += 1
        for b in r["brands"]:
            counts[(r["category"], b, r["temperature"])] += 1
    out: dict[tuple[str, str], dict[float, float]] = defaultdict(dict)
    for (cat, brand, t), n in counts.items():
        out[(cat, brand)][t] = n / totals[(cat, t)]
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pattern", default="results/real/temperature-multi-*.jsonl")
    ap.add_argument("--seed", type=int, default=20260825)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    rows = load(args.pattern)
    temps = sorted({r["temperature"] for r in rows})
    cats = sorted({r["category"] for r in rows})
    print(f"{len(rows):,} samples, {len(cats)} categories, temperatures {temps}\n")

    rates = rate_table(rows)

    # --- 1. Does temperature 0 systematically under-count? ---------------------
    print("Coverage: distinct brands at each temperature, relative to temp 0")
    per_temp_cov = {
        t: [len({b for r in rows if r["category"] == c and r["temperature"] == t
                 for b in r["brands"]}) for c in cats]
        for t in temps
    }
    base = per_temp_cov[temps[0]]
    for t in temps:
        cov = per_temp_cov[t]
        ratio = statistics.fmean(x / y if y else 1.0 for x, y in zip(cov, base))
        wins = sum(1 for x, y in zip(cov, base) if x > y)
        print(f"  {t:>4.2f}  mean {statistics.fmean(cov):>5.1f} brands   "
              f"{ratio:>5.2f}x vs temp 0   higher than temp 0 in {wins}/{len(cats)} categories")

    # --- 2. Noise floor ---------------------------------------------------------
    print("\nNoise floor: mean cross-batch drift by temperature")
    manifest = sorted(glob.glob(str(REPO / "results/real/temperature-multi-*-manifest.json")))[-1]
    cells = json.loads(pathlib.Path(manifest).read_text())["cells"]
    for t in temps:
        d = [c["cross_batch_drift"] for c in cells if c["temperature"] == t and c.get("n")]
        print(f"  {t:>4.2f}  {statistics.fmean(d):.4f}")

    # --- 3. Is the rate distribution bimodal? -----------------------------------
    # This turned out to be the real story. A brand-visibility product needs a SHARE,
    # so a rate stuck at 0% or 100% carries no information regardless of how stable
    # it is.
    print("\nRate distribution: where do per-brand mention rates actually land?")
    print(f"  {'temp':>5}{'<5%':>7}{'middle':>8}{'>95%':>7}{'at extremes':>13}"
          f"{'in 10-90% band':>16}")
    for t in temps:
        counts: Counter = Counter()
        totals: Counter = Counter()
        for r in rows:
            if r["temperature"] != t:
                continue
            totals[r["category"]] += 1
            for b in r["brands"]:
                counts[(r["category"], b)] += 1
        vals = [n / totals[c] for (c, b), n in counts.items()]
        lo = sum(1 for x in vals if x < 0.05)
        hi = sum(1 for x in vals if x > 0.95)
        band = sum(1 for x in vals if 0.10 <= x <= 0.90)
        print(f"  {t:>5.2f}{lo:>7}{len(vals) - lo - hi:>8}{hi:>7}"
              f"{(lo + hi) / len(vals):>12.0%}{band:>10} / {len(vals):<4}")

    # --- 4. The mechanism test --------------------------------------------------
    # Classify each (category, brand) by how often it appeared ONLY inside
    # parentheses across the whole sweep, then compare temperature sensitivity.
    aside_hits: Counter = Counter()
    brand_hits: Counter = Counter()
    for r in rows:
        for b in r["brands"]:
            brand_hits[(r["category"], b)] += 1
        for b in r["aside_brands"]:
            aside_hits[(r["category"], b)] += 1

    def swing(series: dict[float, float]) -> float:
        """Max - min mention rate across temperatures, in percentage points."""
        vals = [series.get(t, 0.0) for t in temps]
        return max(vals) - min(vals)

    rows_out = []
    for key, series in rates.items():
        n = brand_hits[key]
        if n < 20:  # too rare for a stable rate at 60 samples per cell
            continue
        aside_share = aside_hits[key] / n
        rows_out.append((key, swing(series), aside_share, n))

    mostly_aside = [x for x in rows_out if x[2] >= 0.5]
    mostly_direct = [x for x in rows_out if x[2] < 0.1]
    print(f"\nMechanism test: is temperature sensitivity larger for aside-mentioned brands?")
    print(f"  brands analysed: {len(rows_out)} "
          f"({len(mostly_aside)} mostly-aside, {len(mostly_direct)} mostly-direct)")

    def summarise(group, label):
        if not group:
            print(f"  {label:<16} (none)")
            return None
        sw = [g[1] for g in group]
        draws = sorted(statistics.fmean(rng.choice(sw) for _ in sw) for _ in range(5000))
        mean = statistics.fmean(sw)
        print(f"  {label:<16} mean swing {mean:>5.1%}  "
              f"95% CI [{draws[125]:.1%}, {draws[-126]:.1%}]  n={len(sw)}")
        return mean

    a = summarise(mostly_aside, "mostly aside")
    d = summarise(mostly_direct, "mostly direct")
    if a is not None and d is not None:
        print(f"  ratio: {a / d:.2f}x")

    print("\n  Largest swings overall")
    for (cat, brand), sw, aside_share, n in sorted(rows_out, key=lambda x: -x[1])[:12]:
        series = rates[(cat, brand)]
        cells_str = "".join(f"{series.get(t, 0.0):>7.0%}" for t in temps)
        tag = "aside" if aside_share >= 0.5 else ("direct" if aside_share < 0.1 else "mixed")
        print(f"    {brand:<14}{cat:<20}{cells_str}   {sw:>5.1%}  {tag}")


if __name__ == "__main__":
    main()
